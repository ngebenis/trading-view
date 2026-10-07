"""Data untuk grafik Lightweight Charts: candle harian, EMA, volume & penanda transaksi."""
from bisect import bisect_right
from datetime import datetime

from .autotrader import WIB
from .brokers import OrderStatus, PaperBroker
from .idx_rules import normalize_symbol
from .indicators import ema


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


def build_chart(provider, broker: PaperBroker, symbol: str, range_: str = "1y") -> dict:
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
        "bars": bars,
        "ema12": line(ema(closes, 12)),
        "ema26": line(ema(closes, 26)),
        "markers": order_markers(broker, sym, days),
        "position": {"shares": pos["shares"], "avg_price": round(pos["avg_price"], 2)} if pos else None,
    }
