"""Alert harga ke Telegram: kabari saat harga saham IDX / pasangan crypto menembus target.

Arah alert ditentukan dari harga saat dipasang (target di atas harga -> "naik tembus", di bawah ->
"turun tembus"), jadi alert tidak langsung terpicu. Harga diperiksa tiap PRICE_ALERT_SECONDS
(bawaan 30 detik) — terpisah dari pemindaian sinyal — dan dikirim lewat bot Telegram yang sama
dengan notifikasi sinyal. Alert sekali-pakai selesai setelah terkirim; alert berulang aktif lagi
setelah harga kembali ke sisi semula, dengan jeda minimal (cooldown) antar pesan.
"""
import html
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime

from .autotrader import WIB, rupiah
from .binance import is_crypto_pair, normalize_pair, split_pair
from .idx_rules import is_index, normalize_symbol, price_limits
from .market_data import MarketDataError
from .notifier import _pct, crypto_links, index_value, stock_links

MAX_ALERTS = 100


class AlertError(Exception):
    pass


@dataclass
class PriceAlert:
    symbol: str
    target: float
    direction: str                  # "above" (naik tembus) / "below" (turun tembus)
    market: str = "idx"             # idx / crypto
    note: str = ""
    repeat: bool = False
    cooldown_minutes: int = 30      # alert berulang: jeda minimal antar pesan
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    created_at: float = field(default_factory=time.time)
    created_price: float | None = None
    status: str = "active"          # active / triggered
    armed: bool = True              # berulang: menunggu harga kembali ke sisi semula
    triggered_at: float | None = None
    triggered_price: float | None = None
    count: int = 0

    def met(self, price: float) -> bool:
        return price >= self.target if self.direction == "above" else price <= self.target


def fmt_price(symbol: str, market: str, x: float, currency: str = "") -> str:
    if market == "crypto":
        from .crypto import price_str
        return f"{price_str(x)} {currency}".strip()
    return index_value(x) if is_index(symbol) else rupiah(x)


def format_alert(a: PriceAlert, quote) -> str:
    cur = quote.currency if a.market == "crypto" else ""
    up = a.direction == "above"
    period = "24 jam" if a.market == "crypto" else "hari ini"
    lines = [f"🎯 <b>{html.escape(a.symbol)} {'naik tembus' if up else 'turun tembus'} "
             f"{fmt_price(a.symbol, a.market, a.target, cur)}</b>",
             f"Harga: <b>{fmt_price(a.symbol, a.market, quote.price, cur)}</b> ({_pct(quote.change_pct)} {period})"]
    if a.created_price:
        lines.append(f"Dipasang saat harga {fmt_price(a.symbol, a.market, a.created_price, cur)}, "
                     f"{datetime.fromtimestamp(a.created_at, WIB):%d/%m %H:%M} WIB")
    if quote.market_time:
        lines.append(f"Sumber: {html.escape(quote.source)} · transaksi {datetime.fromtimestamp(quote.market_time, WIB):%H:%M:%S} WIB")
    if a.note:
        lines.append(f"Catatan: {html.escape(a.note)}")
    if a.repeat:
        lines.append(f"<i>Alert berulang — aktif lagi setelah harga kembali {'di bawah' if up else 'di atas'} target.</i>")
    lines += ["", crypto_links(a.symbol) if a.market == "crypto" else stock_links(a.symbol)]
    return "\n".join(lines)


class PriceAlertWatcher:
    def __init__(self, provider, notifier, db, interval_seconds: float = 30, clock=time.time):
        self.provider = provider        # data saham IDX
        self.notifier = notifier        # SignalWatcher: bot Telegram, riwayat & provider crypto
        self.db = db
        self.interval = interval_seconds
        self.clock = clock
        self.last_run: float | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        raw = db.get_setting("price_alerts", []) if db is not None else []
        known = {f.name for f in fields(PriceAlert)}
        self.alerts: list[PriceAlert] = [PriceAlert(**{k: v for k, v in r.items() if k in known}) for r in raw]

    # ---- penyimpanan & data ------------------------------------------
    def _save(self) -> None:
        if self.db is not None:
            self.db.set_setting("price_alerts", [asdict(a) for a in self.alerts])

    def _market(self, symbol: str) -> tuple[str, str]:
        return ("crypto", normalize_pair(symbol)) if is_crypto_pair(symbol) else ("idx", normalize_symbol(symbol))

    def _quote(self, symbol: str, market: str):
        if market == "crypto":
            crypto = getattr(self.notifier, "crypto_provider", None)
            if crypto is None:
                raise MarketDataError("Data crypto (Binance) belum tersedia")
            return crypto.quote(symbol)
        return self.provider.quote(symbol)

    # ---- kelola alert ------------------------------------------------
    def add(self, symbol: str, target: float, direction: str | None = None, note: str = "",
            repeat: bool = False, cooldown_minutes: int = 30) -> tuple[PriceAlert, list[str]]:
        market, sym = self._market(symbol)
        if not sym:
            raise AlertError("Isi kode saham atau pasangan crypto")
        if not target or target <= 0:
            raise AlertError("Target harga harus lebih dari 0")
        if direction not in (None, "", "auto", "above", "below"):
            raise AlertError("Arah harus 'above' (naik tembus) atau 'below' (turun tembus)")
        if cooldown_minutes < 1:
            raise AlertError("Cooldown minimal 1 menit")
        with self._lock:
            if len([a for a in self.alerts if a.status == "active"]) >= MAX_ALERTS:
                raise AlertError(f"Maksimal {MAX_ALERTS} alert aktif")
        try:
            quote = self._quote(sym, market)
        except MarketDataError as exc:
            raise AlertError(f"Harga {sym} tidak tersedia: {exc}") from exc
        price = quote.price
        if direction in (None, "", "auto"):
            if target == price:
                raise AlertError("Target sama dengan harga sekarang — pilih target di atas atau di bawahnya")
            direction = "above" if target > price else "below"
        alert = PriceAlert(sym, float(target), direction, market, str(note or "")[:200], bool(repeat),
                           int(cooldown_minutes), created_at=self.clock(), created_price=price)
        if alert.met(price):
            raise AlertError(f"Harga sekarang ({fmt_price(sym, market, price, quote.currency)}) sudah "
                             f"{'di atas' if direction == 'above' else 'di bawah'} target")
        notes = []
        if market == "idx" and not is_index(sym):
            limits = price_limits(quote.prev_close)
            if limits and (target > limits[1] or target < limits[0]):
                notes.append(f"Target di luar rentang ARB–ARA hari ini ({rupiah(limits[0])}–{rupiah(limits[1])}); "
                             "baru bisa tercapai di hari bursa berikutnya")
        if not self.notifier.token or not self.notifier.chat_id:
            notes.append("Telegram belum diatur — atur bot token & chat ID di tab Notifikasi agar alert terkirim")
        with self._lock:
            self.alerts.append(alert)
            self._save()
        return alert, notes

    def _find(self, alert_id: str) -> PriceAlert:
        for a in self.alerts:
            if a.id == alert_id:
                return a
        raise AlertError("Alert tidak ditemukan")

    def remove(self, alert_id: str) -> None:
        with self._lock:
            self.alerts.remove(self._find(alert_id))
            self._save()

    def rearm(self, alert_id: str) -> PriceAlert:
        """Aktifkan lagi alert yang sudah terpicu (arah dihitung ulang dari harga sekarang)."""
        with self._lock:
            a = self._find(alert_id)
        try:
            price = self._quote(a.symbol, a.market).price
        except MarketDataError as exc:
            raise AlertError(f"Harga {a.symbol} tidak tersedia: {exc}") from exc
        if price == a.target:
            raise AlertError("Harga sekarang tepat di target")
        with self._lock:
            a.direction = "above" if a.target > price else "below"
            a.status, a.armed, a.created_price, a.created_at = "active", True, price, self.clock()
            self._save()
        return a

    # ---- pemeriksaan -------------------------------------------------
    def check(self) -> list[dict]:
        """Periksa semua alert aktif; kirim Telegram untuk yang tembus target."""
        with self._lock:
            now = self.clock()
            self.last_run = now
            active = [a for a in self.alerts if a.status == "active"]
            if not active:
                return []
            ready = bool(self.notifier.token and self.notifier.chat_id)
            quotes, out = {}, []
            for a in active:
                key = (a.market, a.symbol)
                if key not in quotes:
                    try:
                        quotes[key] = self._quote(a.symbol, a.market)
                    except MarketDataError:
                        quotes[key] = None  # coba lagi siklus berikutnya
                q = quotes[key]
                if q is None:
                    continue
                if not a.met(q.price):
                    if a.repeat and not a.armed:
                        a.armed = True  # harga kembali ke sisi semula
                    continue
                if not a.armed:
                    continue
                if a.repeat and a.triggered_at and now - a.triggered_at < a.cooldown_minutes * 60:
                    continue
                if not ready:
                    continue  # tetap aktif sampai Telegram diatur
                ok, err = self.notifier._send(format_alert(a, q))
                label = f"{'Naik' if a.direction == 'above' else 'Turun'} tembus {fmt_price(a.symbol, a.market, a.target, q.currency)}"
                if not ok:
                    out.append(self.notifier._record("ERROR", a.symbol, f"Alert harga gagal dikirim: {err}"))
                    continue
                a.count += 1
                a.triggered_at, a.triggered_price = now, q.price
                if a.repeat:
                    a.armed = False
                else:
                    a.status = "triggered"
                out.append(self.notifier._record(
                    "TARGET", a.symbol, f"{label} — harga {fmt_price(a.symbol, a.market, q.price, q.currency)}", sent=True))
            self._save()
            return out

    # ---- loop latar belakang ------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if not self.running:
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="price-alerts", daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.check()
            except Exception as exc:  # loop tidak boleh mati
                self.notifier._record("ERROR", "-", f"Pemeriksaan alert harga gagal: {exc}")

    def status(self) -> dict:
        with self._lock:
            alerts = sorted(self.alerts, key=lambda a: (a.status != "active", -a.created_at))
            return {"alerts": [asdict(a) for a in alerts], "interval_seconds": self.interval,
                    "last_run": self.last_run, "running": self.running,
                    "telegram_configured": bool(self.notifier.token and self.notifier.chat_id)}
