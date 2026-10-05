"""Backtest strategi auto-trading dengan data historis.

Backtest memutar ulang *kode AutoTrader yang sama* hari demi hari di atas
PaperBroker in-memory, sehingga hasilnya mencerminkan perilaku bot sungguhan
(ukuran posisi, fee, lot, stop-loss/take-profit, cooldown, maks. posisi).

Alur per hari perdagangan d:
  - Sinyal dihitung dari candle harian s.d. penutupan hari d (maks. 250 candle,
    sama seperti bot live).
  - Order dieksekusi di harga open hari berikutnya ("next_open", default,
    tanpa look-ahead) atau di harga close hari d ("close", lebih optimistis).
  - Ekuitas dicatat dengan harga close hari eksekusi.
"""
from bisect import bisect_right
from dataclasses import asdict, dataclass, field
from datetime import datetime
from math import sqrt

from .autotrader import AutoTrader, AutoTraderConfig, WIB
from .brokers import OrderStatus, PaperBroker, Side
from .idx_rules import LOT_SIZE, normalize_symbol, reject_indices
from .market_data import Candle, MarketDataError, Quote

PERIOD_DAYS = {"6mo": 182, "1y": 365, "2y": 730, "5y": 1826}
# Ambil data lebih panjang dari periode agar indikator sudah "panas" di hari pertama.
FETCH_RANGE = {"6mo": "1y", "1y": "2y", "2y": "5y", "5y": "10y"}
SIGNAL_LOOKBACK = 250
MIN_HISTORY = 35
MAX_SYMBOLS = 30


@dataclass
class BacktestRequest:
    symbols: list[str]
    period: str = "1y"
    initial_cash: float = 100_000_000
    execution: str = "next_open"  # atau "close"
    strategy: dict = field(default_factory=dict)  # override AutoTraderConfig


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts, WIB).strftime("%Y-%m-%d")


class _ReplayProvider:
    """Provider palsu yang hanya "melihat" data sampai hari yang sedang diputar."""

    name = "replay"

    def __init__(self, history: dict[str, list[Candle]], execution: str):
        self.history = history
        self.days = {s: [_day(c.time) for c in cs] for s, cs in history.items()}
        self.execution = execution
        self.decision_day = ""
        self.exec_day = ""

    def _index(self, symbol: str, day: str) -> int:
        """Indeks candle terakhir dengan tanggal <= day, atau -1."""
        return bisect_right(self.days[symbol], day) - 1

    def candles(self, symbol, range_="1y", interval="1d"):
        if symbol not in self.history:
            raise MarketDataError(f"Tidak ada data {symbol}")
        i = self._index(symbol, self.decision_day)
        if i + 1 < MIN_HISTORY:
            raise MarketDataError("histori belum cukup")
        return self.history[symbol][max(0, i + 1 - SIGNAL_LOOKBACK): i + 1]

    def quote(self, symbol):
        if symbol not in self.history:
            raise MarketDataError(f"Tidak ada data {symbol}")
        i = self._index(symbol, self.exec_day)
        if i < 0 or self.days[symbol][i] != self.exec_day:
            raise MarketDataError("tidak ada perdagangan hari ini")  # suspensi / libur
        c = self.history[symbol][i]
        price = c.open if self.execution == "next_open" else c.close
        return Quote(symbol, price, price, 0, 0, source=self.name)

    def close_on(self, symbol: str, day: str) -> float | None:
        i = self._index(symbol, day)
        return self.history[symbol][i].close if i >= 0 else None


def _max_drawdown(values: list[float]) -> float:
    peak, mdd = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        mdd = min(mdd, (v - peak) / peak)
    return mdd * 100


def _round_trips(orders, last_prices: dict[str, float], sell_fee: float) -> list[dict]:
    """Pasangkan BELI -> JUAL per simbol. Posisi yang masih terbuka dinilai di harga terakhir."""
    open_buys: dict[str, list] = {}
    trips = []
    for o in sorted(orders, key=lambda o: o.created_at):
        if o.status != OrderStatus.FILLED:
            continue
        if o.side == Side.BUY:
            open_buys.setdefault(o.symbol, []).append(o)
            continue
        buys = open_buys.pop(o.symbol, [])
        shares = sum(b.lots for b in buys) * LOT_SIZE
        cost = sum(b.lots * LOT_SIZE * b.fill_price + b.fee for b in buys)
        proceeds = o.lots * LOT_SIZE * o.fill_price - o.fee
        trips.append(_trip(o.symbol, buys, shares, cost, proceeds, o.created_at, o.fill_price, closed=True))
    for sym, buys in open_buys.items():
        shares = sum(b.lots for b in buys) * LOT_SIZE
        cost = sum(b.lots * LOT_SIZE * b.fill_price + b.fee for b in buys)
        price = last_prices.get(sym, buys[-1].fill_price)
        proceeds = shares * price * (1 - sell_fee)
        trips.append(_trip(sym, buys, shares, cost, proceeds, None, price, closed=False))
    return sorted(trips, key=lambda t: t["entry_date"])


def _trip(sym, buys, shares, cost, proceeds, exit_ts, exit_price, closed):
    entry_ts = buys[0].created_at
    pl = proceeds - cost
    return {
        "symbol": sym, "lots": shares // LOT_SIZE,
        "entry_date": _day(entry_ts), "entry_price": round(cost / shares, 2) if shares else 0,
        "exit_date": _day(exit_ts) if exit_ts else None, "exit_price": exit_price,
        "pl": round(pl, 2), "pl_pct": round(pl / cost * 100, 2) if cost else 0.0,
        "holding_days": round((exit_ts - entry_ts) / 86400) if closed else None,
        "closed": closed,
    }


def run_backtest(req: BacktestRequest, provider, base_config: AutoTraderConfig,
                 buy_fee_pct: float, sell_fee_pct: float, max_position_pct: float) -> dict:
    if req.period not in PERIOD_DAYS:
        raise ValueError(f"Periode harus salah satu dari {', '.join(PERIOD_DAYS)}")
    if req.execution not in ("next_open", "close"):
        raise ValueError("Eksekusi harus 'next_open' atau 'close'")
    if req.initial_cash < 1_000_000:
        raise ValueError("Modal awal minimal Rp1.000.000")
    reject_indices(req.symbols, "backtest")
    symbols = sorted({normalize_symbol(s) for s in req.symbols if s.strip()})
    if not 1 <= len(symbols) <= MAX_SYMBOLS:
        raise ValueError(f"Jumlah simbol harus 1-{MAX_SYMBOLS}")

    # 1) Data historis
    history, warnings = {}, []
    for sym in symbols:
        try:
            candles = provider.candles(sym, FETCH_RANGE[req.period], "1d")
        except MarketDataError as exc:
            warnings.append(f"{sym}: {exc}")
            continue
        if len(candles) < MIN_HISTORY + 2:
            warnings.append(f"{sym}: data historis terlalu sedikit ({len(candles)} candle)")
            continue
        history[sym] = candles
    if not history:
        raise ValueError("Tidak ada data historis yang bisa dipakai. " + "; ".join(warnings))

    # 2) Kalender perdagangan
    last_ts = max(cs[-1].time for cs in history.values())
    start_day = _day(int(last_ts - PERIOD_DAYS[req.period] * 86400))
    calendar = sorted({_day(c.time) for cs in history.values() for c in cs})
    calendar = [d for d in calendar if d >= start_day]
    if len(calendar) < 2:
        raise ValueError("Periode terlalu pendek untuk data yang tersedia")

    # 3) Bot + broker in-memory, jam disimulasikan
    replay = _ReplayProvider(history, req.execution)
    sim_time = {"t": 0.0}
    clock = lambda: sim_time["t"]  # noqa: E731
    broker = PaperBroker(None, req.initial_cash, buy_fee_pct, sell_fee_pct, clock=clock)
    trader = AutoTrader(broker, replay, None, max_position_pct, clock=clock)
    trader.config = base_config
    cfg = trader.update_config({**req.strategy, "symbols": list(history), "market_hours_only": False})

    def day_ts(day: str) -> float:  # jam 16:00 WIB pada hari tsb
        return datetime.strptime(day, "%Y-%m-%d").replace(hour=16, tzinfo=WIB).timestamp()

    # Titik awal kurva ekuitas
    equity_curve = [{"date": calendar[0], "equity": req.initial_cash}]
    if req.execution == "next_open":
        steps = [(calendar[k], calendar[k + 1]) for k in range(len(calendar) - 1)]
    else:
        steps = [(d, d) for d in calendar[1:]]

    days_invested = 0
    for decision_day, exec_day in steps:
        replay.decision_day, replay.exec_day = decision_day, exec_day
        sim_time["t"] = day_ts(exec_day) - (7 * 3600 if req.execution == "next_open" else 0)  # 09:00 / 16:00
        trader.run_cycle()
        closes = {s: replay.close_on(s, exec_day) for s in history}
        equity = broker.account({s: p for s, p in closes.items() if p is not None})["equity"]
        equity_curve.append({"date": exec_day, "equity": round(equity, 2)})
        days_invested += bool(broker.positions)

    # 4) Benchmark: beli & tahan bobot sama di semua simbol (tanpa fee)
    first_day, last_day = equity_curve[0]["date"], equity_curve[-1]["date"]
    bench_syms = [s for s in history if replay.close_on(s, first_day)]
    bench_curve = []
    for point in equity_curve:
        if not bench_syms:
            break
        ratios = [replay.close_on(s, point["date"]) / replay.close_on(s, first_day) for s in bench_syms]
        bench_curve.append(round(req.initial_cash * sum(ratios) / len(ratios), 2))

    # 5) Statistik
    last_prices = {s: replay.close_on(s, last_day) for s in history}
    trips = _round_trips(broker.orders(), last_prices, broker.sell_fee)
    closed = [t for t in trips if t["closed"]]
    wins = [t for t in closed if t["pl"] > 0]
    gross_win = sum(t["pl"] for t in wins)
    gross_loss = -sum(t["pl"] for t in closed if t["pl"] <= 0)
    values = [p["equity"] for p in equity_curve]
    rets = [values[i] / values[i - 1] - 1 for i in range(1, len(values))]
    mean = sum(rets) / len(rets) if rets else 0.0
    std = sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) if len(rets) > 1 else 0.0
    years = max((datetime.fromisoformat(last_day) - datetime.fromisoformat(first_day)).days / 365.25, 1 / 365.25)
    final = values[-1]
    total_return = (final / req.initial_cash - 1) * 100
    filled = [o for o in broker.orders() if o.status == OrderStatus.FILLED]

    per_symbol = []
    for sym in history:
        st = [t for t in trips if t["symbol"] == sym]
        c0, c1 = replay.close_on(sym, first_day), last_prices[sym]
        per_symbol.append({
            "symbol": sym, "trades": len(st), "wins": sum(1 for t in st if t["closed"] and t["pl"] > 0),
            "pl": round(sum(t["pl"] for t in st), 2),
            "buy_hold_pct": round((c1 / c0 - 1) * 100, 2) if c0 else None,
        })

    return {
        "params": {
            "symbols": list(history), "period": req.period, "execution": req.execution,
            "initial_cash": req.initial_cash, "start": first_day, "end": last_day,
            "trading_days": len(equity_curve) - 1, "buy_fee_pct": buy_fee_pct, "sell_fee_pct": sell_fee_pct,
            "strategy": {k: v for k, v in asdict(cfg).items() if k not in ("enabled", "symbols", "interval_seconds", "market_hours_only")},
        },
        "metrics": {
            "final_equity": round(final, 2),
            "total_return_pct": round(total_return, 2),
            "cagr_pct": round(((final / req.initial_cash) ** (1 / years) - 1) * 100, 2) if final > 0 else -100.0,
            "max_drawdown_pct": round(_max_drawdown(values), 2),
            "sharpe": round(mean / std * sqrt(252), 2) if std else 0.0,
            "trades": len(closed),
            "open_positions": len(trips) - len(closed),
            "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else None,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "avg_trade_pct": round(sum(t["pl_pct"] for t in closed) / len(closed), 2) if closed else None,
            "fees_paid": round(sum(o.fee for o in filled), 2),
            "exposure_pct": round(days_invested / len(steps) * 100, 1),
            "benchmark_return_pct": round((bench_curve[-1] / req.initial_cash - 1) * 100, 2) if bench_curve else None,
            "benchmark_max_drawdown_pct": round(_max_drawdown(bench_curve), 2) if bench_curve else None,
        },
        "equity_curve": [{**p, "benchmark": bench_curve[i] if bench_curve else None} for i, p in enumerate(equity_curve)],
        "trades": trips,
        "per_symbol": sorted(per_symbol, key=lambda r: r["pl"], reverse=True),
        "warnings": warnings,
    }

