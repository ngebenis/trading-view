"""Data untuk grafik Lightweight Charts: candle harian, EMA, volume & penanda transaksi."""
from bisect import bisect_right
from datetime import datetime

from .autotrader import WIB
from .brokers import OrderStatus, PaperBroker
from .idx_rules import normalize_symbol
from .indicators import ema
from .market_data import INTRADAY_INTERVALS

WIB_OFFSET = 7 * 3600
# Rentang data intraday per interval (batas Yahoo: 1m maks 7 hari, 5m/15m maks 60 hari).
INTRADAY_RANGE = {"1m": "1d", "5m": "5d", "15m": "1mo"}


def wib_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, WIB).strftime("%Y-%m-%d")


def snap_to_bar(day: str, bar_days: list[str]) -> str | None:
    """Hari transaksi -> hari candle terdekat yang <= hari itu (libur/akhir pekan ikut candle sebelumnya)."""
    i = bisect_right(bar_days, day) - 1
    return bar_days[i] if i >= 0 else None


def order_markers(broker: PaperBroker, symbol: str, bar_days: list[str]) -> list[dict]:
    """Order terisi untuk satu saham, digabung per (hari candle, sisi)."""
    sym = normalize_symbol(symbol)
    groups: dict[tuple[str, str], dict] = {}
    for o in broker.orders():
        if o.symbol != sym or o.status != OrderStatus.FILLED:
            continue
        day = snap_to_bar(wib_day(o.filled_at or o.created_at), bar_days)
        if day is None:
            continue  # sebelum rentang grafik
        g = groups.setdefault((day, o.side.value), {"time": day, "side": o.side.value, "lots": 0, "value": 0.0,
                                                    "count": 0, "sources": set()})
        g["lots"] += o.lots
        g["value"] += o.lots * o.fill_price
        g["count"] += 1
        g["sources"].add(o.source)
    out = []
    for g in groups.values():
        out.append({"time": g["time"], "side": g["side"], "lots": g["lots"], "count": g["count"],
                    "avg_price": round(g["value"] / g["lots"], 2), "sources": sorted(g["sources"])})
    return sorted(out, key=lambda m: (m["time"], m["side"]))


def intraday_markers(broker: PaperBroker, symbol: str, bar_times: list[int], seconds: int) -> list[dict]:
    """Order terisi pada grafik intraday: ditempel ke bar terakhir yang <= waktu isi (waktu sudah +WIB)."""
    sym = normalize_symbol(symbol)
    groups: dict[tuple[int, str], dict] = {}
    for o in broker.orders():
        if o.symbol != sym or o.status != OrderStatus.FILLED:
            continue
        t = int((o.filled_at or o.created_at) + WIB_OFFSET)
        i = bisect_right(bar_times, t) - 1
        if i < 0 or t - bar_times[i] >= seconds * 2:  # di luar rentang/sesi grafik
            continue
        g = groups.setdefault((bar_times[i], o.side.value), {"time": bar_times[i], "side": o.side.value, "lots": 0,
                                                            "value": 0.0, "count": 0, "sources": set()})
        g["lots"] += o.lots
        g["value"] += o.lots * o.fill_price
        g["count"] += 1
        g["sources"].add(o.source)
    return sorted(({**{k: v for k, v in g.items() if k not in ("value", "sources")},
                    "avg_price": round(g["value"] / g["lots"], 2), "sources": sorted(g["sources"])}
                   for g in groups.values()), key=lambda m: (m["time"], m["side"]))


def build_intraday(provider, broker: PaperBroker, symbol: str, interval: str) -> dict:
    """Grafik intraday. Waktu = epoch + 7 jam, agar sumbu waktu Lightweight Charts menampilkan jam WIB."""
    sym = normalize_symbol(symbol)
    seconds = INTRADAY_INTERVALS[interval]
    bars = {}
    for c in provider.candles(sym, INTRADAY_RANGE[interval], interval):
        t = int(c.time) // seconds * seconds + WIB_OFFSET
        bars[t] = {"time": t, "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}
    rows = [bars[t] for t in sorted(bars)]
    times = [b["time"] for b in rows]
    closes = [b["close"] for b in rows]
    pos = broker.positions.get(sym)
    return {
        "symbol": sym, "interval": interval, "bar_seconds": seconds, "bars": rows,
        "ema12": [{"time": t, "value": round(v, 4)} for t, v in zip(times, ema(closes, 12)) if v is not None],
        "ema26": [{"time": t, "value": round(v, 4)} for t, v in zip(times, ema(closes, 26)) if v is not None],
        "markers": intraday_markers(broker, sym, times, seconds),
        "position": {"shares": pos["shares"], "avg_price": round(pos["avg_price"], 2)} if pos else None,
    }


def build_chart(provider, broker: PaperBroker, symbol: str, range_: str = "1y", interval: str = "1d") -> dict:
    if interval in INTRADAY_INTERVALS:
        return build_intraday(provider, broker, symbol, interval)
    sym = normalize_symbol(symbol)
    candles = provider.candles(sym, range_, "1d")
    # Satu candle per hari (data intraday hari berjalan bisa memberi duplikat tanggal).
    by_day: dict[str, dict] = {}
    for c in candles:
        by_day[wib_day(c.time)] = {"time": wib_day(c.time), "open": c.open, "high": c.high, "low": c.low,
                                   "close": c.close, "volume": c.volume}
    bars = [by_day[d] for d in sorted(by_day)]
    days = [b["time"] for b in bars]
    closes = [b["close"] for b in bars]

    def line(values):
        return [{"time": d, "value": round(v, 4)} for d, v in zip(days, values) if v is not None]

    pos = broker.positions.get(sym)
    return {
        "symbol": sym,
        "interval": "1d",
        "bar_seconds": 86400,
        "bars": bars,
        "ema12": line(ema(closes, 12)),
        "ema26": line(ema(closes, 26)),
        "markers": order_markers(broker, sym, days),
        "position": {"shares": pos["shares"], "avg_price": round(pos["avg_price"], 2)} if pos else None,
    }
