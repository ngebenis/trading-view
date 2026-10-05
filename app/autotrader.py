"""Auto-trading berbasis sinyal teknikal — HANYA untuk akun simulasi (PaperBroker).

Setiap siklus:
  1. Cocokkan order limit yang masih OPEN.
  2. Untuk posisi yang dimiliki: jual bila kena stop-loss / take-profit, atau sinyal JUAL.
  3. Untuk simbol tanpa posisi: beli bila sinyal BELI dengan skor >= min_buy_score.
Setiap simbol punya cooldown setelah transaksi otomatis agar tidak bolak-balik.
"""
import json
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .brokers import BrokerError, Order, OrderType, PaperBroker, Side
from .idx_rules import LOT_SIZE, normalize_symbol, round_to_tick
from .market_data import MarketDataError
from .strategy import analyze

WIB = timezone(timedelta(hours=7))


def rupiah(x: float) -> str:
    return f"{x:,.0f}".replace(",", ".")


def is_idx_market_open(now: datetime) -> bool:
    """Jam perdagangan reguler IDX (WIB), termasuk jeda siang."""
    now = now.astimezone(WIB)
    wd, hm = now.weekday(), now.hour * 60 + now.minute
    if wd >= 5:
        return False
    if wd == 4:  # Jumat: sesi 1 09:00-11:30, sesi 2 14:00-15:49
        return 540 <= hm < 690 or 840 <= hm < 950
    return 540 <= hm < 720 or 810 <= hm < 950  # Senin-Kamis: 09:00-12:00, 13:30-15:49


@dataclass
class AutoTraderConfig:
    enabled: bool = False
    symbols: list[str] = field(default_factory=lambda: ["BBCA", "BBRI", "TLKM", "ASII", "BMRI"])
    interval_seconds: int = 300
    position_pct: float = 10.0  # % ekuitas per posisi baru
    max_positions: int = 5
    min_buy_score: int = 2
    max_sell_score: int = -2
    stop_loss_pct: float = 5.0  # 0 = nonaktif
    take_profit_pct: float = 10.0  # 0 = nonaktif
    cooldown_minutes: int = 60
    market_hours_only: bool = False

    def validate(self, max_position_pct: float) -> None:
        self.symbols = sorted({normalize_symbol(s) for s in self.symbols if s.strip()})
        errors = []
        if not self.symbols:
            errors.append("Daftar simbol tidak boleh kosong")
        if self.interval_seconds < 30:
            errors.append("Interval minimal 30 detik")
        if not 0 < self.position_pct <= max_position_pct:
            errors.append(f"Ukuran posisi harus 0 < x <= {max_position_pct:g}% (MAX_POSITION_PCT)")
        if self.max_positions < 1:
            errors.append("Maks. posisi minimal 1")
        if self.min_buy_score < 1 or self.max_sell_score > -1:
            errors.append("Skor beli minimal 1 dan skor jual maksimal -1")
        if self.stop_loss_pct < 0 or self.take_profit_pct < 0 or self.cooldown_minutes < 0:
            errors.append("Stop-loss, take-profit, dan cooldown tidak boleh negatif")
        if errors:
            raise ValueError("; ".join(errors))


class AutoTrader:
    def __init__(self, broker: PaperBroker, provider, path: Path | None, max_position_pct: float, clock=time.time):
        self.broker = broker
        self.provider = provider
        self.path = path
        self.max_position_pct = max_position_pct
        self.clock = clock
        self.log: deque[dict] = deque(maxlen=300)
        self.listeners: list = []  # dipanggil untuk setiap entri log baru (mis. notifikasi Telegram)
        self._seq = 0
        self.last_run: float | None = None
        self.last_trade_at: dict[str, float] = {}
        self._cycle_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.config = self._load()

    # ---- konfigurasi -------------------------------------------------
    def _load(self) -> AutoTraderConfig:
        if self.path is not None and self.path.exists():
            raw = json.loads(self.path.read_text())
            known = {f.name for f in fields(AutoTraderConfig)}
            return AutoTraderConfig(**{k: v for k, v in raw.items() if k in known})
        return AutoTraderConfig()

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(self.config), indent=2))

    def update_config(self, data: dict) -> AutoTraderConfig:
        merged = {**asdict(self.config), **data, "enabled": self.config.enabled}
        cfg = AutoTraderConfig(**merged)
        cfg.validate(self.max_position_pct)
        self.config = cfg
        self._save()
        return cfg

    # ---- loop latar belakang ----------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self.config.enabled = True
        self._save()
        if not self.running:
            self._log("INFO", "-", "Auto-trading dimulai")
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="autotrader", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self.config.enabled = False
        self._save()
        if self.running:
            self._stop.set()
            self._thread.join(timeout=5)
            self._log("INFO", "-", "Auto-trading dihentikan")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_cycle()
            except Exception as exc:  # loop tidak boleh mati karena satu error
                self._log("ERROR", "-", f"Siklus gagal: {exc}")
            self._stop.wait(self.config.interval_seconds)

    # ---- logika trading ----------------------------------------------
    def _log(self, level: str, symbol: str, message: str, **extra) -> None:
        self._seq += 1
        entry = {"id": self._seq, "time": self.clock(), "level": level, "symbol": symbol, "message": message, **extra}
        self.log.appendleft(entry)
        for listener in self.listeners:
            try:
                listener(entry)
            except Exception:  # notifikasi gagal tidak boleh menghentikan trading
                pass

    def _in_cooldown(self, symbol: str, now: float) -> bool:
        last = self.last_trade_at.get(symbol)
        return last is not None and now - last < self.config.cooldown_minutes * 60

    def _submit(self, symbol: str, side: Side, lots: int, price: float, reason: str, now: float) -> None:
        order = Order(symbol, side, lots, OrderType.MARKET, source="auto", created_at=now)
        try:
            self.broker.place_order(order, price)
        except BrokerError as exc:
            self._log("WARN", symbol, f"Order {side.value} ditolak: {exc}")
            return
        self.last_trade_at[symbol] = now
        self._log("TRADE", symbol, f"{side.value} {lots} lot @ {rupiah(order.fill_price)} — {reason}",
                  side=side.value, lots=lots, price=order.fill_price, order_id=order.id)

    def run_cycle(self) -> list[dict]:
        """Jalankan satu siklus. Mengembalikan entri log yang dibuat pada siklus ini."""
        with self._cycle_lock:
            now = self.clock()
            start_id = self._seq

            def created() -> list[dict]:
                return [e for e in self.log if e["id"] > start_id]

            self.last_run = now
            cfg = self.config
            if cfg.market_hours_only and not is_idx_market_open(datetime.fromtimestamp(now, timezone.utc)):
                self._log("INFO", "-", "Bursa tutup, siklus dilewati")
                return created()

            held = set(self.broker.positions)
            universe = sorted(set(cfg.symbols) | held)
            prices, signals = {}, {}
            for sym in universe:
                try:
                    prices[sym] = self.provider.quote(sym).price
                    closes = [c.close for c in self.provider.candles(sym, "1y", "1d")]
                    signals[sym] = analyze(closes)
                except MarketDataError as exc:
                    self._log("WARN", sym, f"Data tidak tersedia: {exc}")

            for o in self.broker.match_open_orders(prices):
                self._log("TRADE", o.symbol, f"Order limit {o.side.value} {o.lots} lot @ {rupiah(o.fill_price)} tereksekusi")

            # 1) Kelola posisi yang ada (keluar lebih dulu agar dana bebas untuk beli).
            for sym in sorted(self.broker.positions):
                if sym not in prices:
                    continue
                pos = self.broker.positions[sym]
                price, avg = prices[sym], pos["avg_price"]
                lots = (pos["shares"] - self.broker._reserved_shares(sym)) // LOT_SIZE
                if lots <= 0:
                    continue
                change = (price - avg) / avg * 100
                sig = signals.get(sym)
                if cfg.stop_loss_pct and change <= -cfg.stop_loss_pct:
                    self._submit(sym, Side.SELL, lots, price, f"stop-loss ({change:.1f}%)", now)
                elif cfg.take_profit_pct and change >= cfg.take_profit_pct:
                    self._submit(sym, Side.SELL, lots, price, f"take-profit (+{change:.1f}%)", now)
                elif sig and sig["score"] <= cfg.max_sell_score and not self._in_cooldown(sym, now):
                    self._submit(sym, Side.SELL, lots, price, f"sinyal JUAL skor {sig['score']}", now)

            # 2) Buka posisi baru.
            for sym in cfg.symbols:
                sig = signals.get(sym)
                if not sig or sym in self.broker.positions or sym in self.broker.open_symbols():
                    continue
                if sig["score"] < cfg.min_buy_score:
                    continue
                if self._in_cooldown(sym, now):
                    self._log("SKIP", sym, f"Sinyal BELI skor {sig['score']} diabaikan (cooldown)")
                    continue
                if len(self.broker.positions) >= cfg.max_positions:
                    self._log("SKIP", sym, f"Sinyal BELI diabaikan: maks. {cfg.max_positions} posisi tercapai")
                    continue
                price = prices[sym]
                acct = self.broker.account(prices)
                budget = min(acct["equity"] * cfg.position_pct / 100, acct["buying_power"])
                unit_cost = round_to_tick(price) * LOT_SIZE * (1 + self.broker.buy_fee)
                lots = int(budget // unit_cost)
                if lots < 1:
                    self._log("SKIP", sym, f"Sinyal BELI diabaikan: dana tidak cukup untuk 1 lot (Rp{rupiah(unit_cost)})")
                    continue
                self._submit(sym, Side.BUY, lots, price, f"sinyal BELI skor {sig['score']}", now)

            if not created():
                self._log("INFO", "-", f"Tidak ada aksi ({len(signals)} simbol dipindai)")
            return created()

    def status(self) -> dict:
        nxt = self.last_run + self.config.interval_seconds if self.running and self.last_run else None
        return {"config": asdict(self.config), "running": self.running, "last_run": self.last_run,
                "next_run": nxt, "log": list(self.log)[:100]}
