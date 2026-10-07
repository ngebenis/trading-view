"""Penerima webhook alert TradingView.

TradingView (paket berbayar) bisa mengirim POST ke URL webhook saat alert terpicu. Isi pesan
alert diisi template JSON (lihat `message_template`), contoh:

    {"secret": "...", "symbol": "{{ticker}}", "action": "{{strategy.order.action}}",
     "price": {{close}}, "message": "{{strategy.order.comment}}"}

TradingView tidak bisa menambah header khusus, jadi kode rahasia dikirim di isi pesan
(atau lewat `?secret=` di URL). Order hanya dieksekusi di akun Paper Trading.
"""
import hashlib
from datetime import datetime
import hmac
import html
import json
import math
import secrets
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field, fields

from .brokers import BrokerError, Order, OrderType, PaperBroker, Side
from .autotrader import WIB, rupiah
from .idx_rules import LOT_SIZE, is_index, limit_status, normalize_symbol, price_limits, round_to_tick
from .market_data import MarketDataError
from .notifier import index_value, stock_links

ACTIONS = {
    "buy": "BUY", "beli": "BUY", "long": "BUY",
    "sell": "SELL", "jual": "SELL", "short": "SELL", "exit": "SELL", "close": "SELL",
}
MODES = ("log", "notify", "order")  # order = notifikasi + order simulasi
DEDUP_SECONDS = 60
AUTH_NOTICE_SECONDS = 600  # peringatan kode rahasia salah: maksimal satu pesan per 10 menit
MAX_BODY = 10_000


class WebhookError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class WebhookConfig:
    enabled: bool = False
    secret: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    mode: str = "notify"
    position_pct: float = 10.0  # ukuran order BELI bila alert tidak menyebut "lots"
    max_lots: int = 1000        # batas atas lot per alert
    allowed_symbols: list[str] = field(default_factory=list)  # kosong = semua
    notify_security: bool = True  # kabari Telegram bila ada alert dengan kode rahasia salah

    def validate(self, max_position_pct: float) -> None:
        self.allowed_symbols = sorted({normalize_symbol(s) for s in self.allowed_symbols if s.strip()})
        errors = []
        if self.mode not in MODES:
            errors.append(f"Mode harus salah satu dari {', '.join(MODES)}")
        if not 0 < self.position_pct <= max_position_pct:
            errors.append(f"Ukuran posisi harus 0 < x <= {max_position_pct:g}% (MAX_POSITION_PCT)")
        if self.max_lots < 1:
            errors.append("Maks. lot minimal 1")
        if len(self.secret) < 16:
            errors.append("Kode rahasia minimal 16 karakter")
        if errors:
            raise ValueError("; ".join(errors))


@dataclass
class Alert:
    symbol: str
    action: str | None   # BUY / SELL / None (informasi saja)
    price: float | None
    lots: int | None
    mode: str | None     # override mode per alert (log/notify/order)
    message: str


def pct(x: float) -> str:
    """+1,25% (format Indonesia, selalu bertanda)."""
    return f"{x:+.2f}%".replace(".", ",")


def parse_alert(payload: dict) -> Alert:
    raw_symbol = str(payload.get("symbol") or payload.get("ticker") or "").strip()
    if not raw_symbol:
        raise WebhookError("Field 'symbol' wajib diisi (gunakan {{ticker}} di TradingView)")
    symbol = normalize_symbol(raw_symbol)
    raw_action = str(payload.get("action") or payload.get("side") or "").strip().lower()
    action = ACTIONS.get(raw_action) if raw_action else None
    if raw_action and action is None:
        raise WebhookError(f"Action '{raw_action}' tidak dikenali (pakai buy/sell)")

    def number(key):
        value = payload.get(key)
        if value in (None, ""):
            return None
        try:
            x = float(value)
        except (TypeError, ValueError):
            raise WebhookError(f"Field '{key}' harus angka") from None
        if not math.isfinite(x) or x <= 0:
            raise WebhookError(f"Field '{key}' harus angka positif")
        return x

    lots = number("lots")
    if lots is not None and lots != int(lots):
        raise WebhookError("Field 'lots' harus bilangan bulat")
    mode = payload.get("mode")
    if mode is not None and mode not in MODES:
        raise WebhookError(f"Field 'mode' harus salah satu dari {', '.join(MODES)}")
    return Alert(symbol, action, number("price"), int(lots) if lots else None, mode,
                 str(payload.get("message") or payload.get("comment") or "")[:500])


class TradingViewWebhook:
    def __init__(self, broker: PaperBroker, provider, db, max_position_pct: float,
                 notifier=None, clock=time.time):
        self.broker = broker
        self.provider = provider
        self.db = db  # app.db.Database atau None
        self.max_position_pct = max_position_pct
        self.notifier = notifier  # SignalWatcher (punya .telegram() & .chat_id)
        self.clock = clock
        self.log: deque[dict] = deque(db.recent_logs("webhook", 200) if db else (), maxlen=200)
        self._recent: dict[str, float] = {}
        self._auth_failures: list[tuple[float, str]] = []  # (waktu, IP) sejak peringatan terakhir
        self._auth_last_notice = 0.0
        self._lock = threading.Lock()
        self.config = self._load()

    # ---- konfigurasi -------------------------------------------------
    def _load(self) -> WebhookConfig:
        raw = self.db.get_setting("webhook") if self.db is not None else None
        if raw:
            known = {f.name for f in fields(WebhookConfig)}
            return WebhookConfig(**{k: v for k, v in raw.items() if k in known})
        cfg = WebhookConfig()
        self.config = cfg
        self._save()
        return cfg

    def _save(self) -> None:
        if self.db is not None:
            self.db.set_setting("webhook", asdict(self.config))

    def update_config(self, data: dict) -> WebhookConfig:
        data = {k: v for k, v in data.items() if k != "secret"}  # rahasia hanya lewat regenerate
        cfg = WebhookConfig(**{**asdict(self.config), **data})
        cfg.validate(self.max_position_pct)
        self.config = cfg
        self._save()
        return cfg

    def regenerate_secret(self) -> str:
        self.config.secret = secrets.token_urlsafe(24)
        self._save()
        return self.config.secret

    def message_template(self, strategy: bool = True) -> str:
        action = "{{strategy.order.action}}" if strategy else "buy"
        return json.dumps({"secret": self.config.secret, "symbol": "{{ticker}}", "action": action,
                           "price": "{{close}}", "message": "{{strategy.order.comment}}" if strategy else "{{interval}}"},
                          ensure_ascii=False).replace('"{{close}}"', "{{close}}")

    # ---- menerima alert ----------------------------------------------
    def check_secret(self, payload: dict, query_secret: str | None) -> None:
        given = str(payload.get("secret") or query_secret or "")
        if not given or not hmac.compare_digest(given.encode(), self.config.secret.encode()):
            raise WebhookError("Kode rahasia salah", 401)

    def authenticate(self, payload: dict, query_secret: str | None) -> None:
        if not self.config.enabled:
            raise WebhookError("Webhook nonaktif", 403)
        self.check_secret(payload, query_secret)

    @staticmethod
    def parse_body(body: bytes) -> dict:
        if len(body) > MAX_BODY:
            raise WebhookError("Pesan alert terlalu besar", 413)
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise WebhookError("Isi alert harus JSON — salin template pesan dari tab Webhook") from None
        if not isinstance(payload, dict):
            raise WebhookError("Isi alert harus objek JSON")
        return payload

    def accept(self, body: bytes, query_secret: str | None = None,
               payload: dict | None = None) -> tuple[Alert, dict]:
        """Validasi cepat (dijalankan sebelum membalas TradingView). Pemrosesan di `process`."""
        if payload is None:
            payload = self.parse_body(body)
        self.authenticate(payload, query_secret)
        alert = parse_alert(payload)
        cfg = self.config
        if cfg.allowed_symbols and alert.symbol not in cfg.allowed_symbols:
            raise WebhookError(f"{alert.symbol} tidak ada di daftar simbol yang diizinkan", 422)
        digest = hashlib.sha256(body).hexdigest()
        now = self.clock()
        with self._lock:
            self._recent = {k: t for k, t in self._recent.items() if now - t < DEDUP_SECONDS}
            if digest in self._recent:
                raise WebhookError("Alert duplikat diabaikan", 409)
            self._recent[digest] = now
        entry = self._record("RECEIVED", alert.symbol, self._describe(alert), alert=asdict(alert))
        return alert, entry

    def _describe(self, alert: Alert) -> str:
        label = {"BUY": "BELI", "SELL": "JUAL"}.get(alert.action, "INFO")
        parts = [label]
        if alert.price:
            parts.append(f"@ {index_value(alert.price) if is_index(alert.symbol) else rupiah(alert.price)}")
        if alert.lots:
            parts.append(f"{alert.lots} lot")
        if alert.message:
            parts.append(f"— {alert.message}")
        return " ".join(parts)

    def _record(self, kind: str, symbol: str, message: str, **extra) -> dict:
        entry = {"time": self.clock(), "kind": kind, "symbol": symbol, "message": message, **extra}
        if self.db is not None:
            entry["id"] = self.db.append_log("webhook", entry)
        self.log.appendleft(entry)
        return entry

    def process(self, alert: Alert) -> list[dict]:
        """Jalankan aksi alert: catat, kirim Telegram, dan/atau order simulasi."""
        mode = alert.mode or self.config.mode
        if mode == "log":
            return []
        quote, quote_error = None, None
        try:  # harga terkini sekali untuk order & isi pesan
            quote = self.provider.quote(alert.symbol) if self.provider is not None else None
        except MarketDataError as exc:
            quote_error = str(exc)
        out = []
        order_result = None
        if mode == "order" and alert.action:
            order_result = self._order(alert, quote, quote_error)
            out.append(order_result)
        out.append(self._notify(alert, order_result, quote))
        return out

    def _order(self, alert: Alert, quote=None, quote_error: str | None = None) -> dict:
        sym = alert.symbol
        if is_index(sym):
            return self._record("SKIP", sym, "Indeks tidak bisa diperdagangkan, order dilewati")
        limits = None
        if quote is not None:
            price, limits = quote.price, price_limits(quote.prev_close)
        elif alert.price:
            price = alert.price  # pakai harga dari TradingView bila data pasar gagal
        else:
            return self._record("ERROR", sym, f"Harga tidak tersedia: {quote_error or 'tidak ada data'}")
        held = self.broker.positions.get(sym, {}).get("shares", 0) - self.broker._reserved_shares(sym)
        if alert.action == "SELL":
            available = held // LOT_SIZE
            if available <= 0:
                return self._record("SKIP", sym, "Alert JUAL diabaikan: tidak punya posisi")
            lots = min(alert.lots or available, available, self.config.max_lots)
        else:
            if alert.lots:
                lots = alert.lots
            else:
                acct = self.broker.account({sym: price})
                budget = min(acct["equity"] * self.config.position_pct / 100, acct["buying_power"])
                lots = int(budget // (round_to_tick(price) * LOT_SIZE * (1 + self.broker.buy_fee)))
            lots = min(lots, self.config.max_lots)
            equity = self.broker.account({sym: price})["equity"]
            cap = equity * self.max_position_pct / 100
            if lots * LOT_SIZE * price > cap:  # batas risiko yang sama dengan order manual
                lots = int(cap // (price * LOT_SIZE))
            if lots < 1:
                return self._record("SKIP", sym, "Alert BELI diabaikan: dana/batas risiko tidak cukup untuk 1 lot")
        side = Side.BUY if alert.action == "BUY" else Side.SELL
        order = Order(sym, side, lots, OrderType.MARKET, source="webhook", created_at=self.clock())
        try:
            self.broker.place_order(order, price, limits)
        except BrokerError as exc:
            return self._record("ERROR", sym, f"Order {side.value} ditolak: {exc}")
        return self._record("ORDER", sym, f"{side.value} {lots} lot @ {rupiah(order.fill_price)} (simulasi)",
                            order_id=order.id)

    def _telegram_ready(self) -> bool:
        n = self.notifier
        return n is not None and bool(n.token and n.chat_id)

    def _send(self, text: str) -> str | None:
        """Kirim pesan; kembalikan pesan galat atau None bila berhasil."""
        try:
            self.notifier.telegram().send(self.notifier.chat_id, text)
            return None
        except Exception as exc:  # NotifierError / jaringan
            return str(exc)

    def format_message(self, alert: Alert, order_result: dict | None, quote) -> str:
        sym = alert.symbol
        money = (lambda x: index_value(x)) if is_index(sym) else rupiah  # noqa: E731
        icon = {"BUY": "🟢", "SELL": "🔴"}.get(alert.action, "📡")
        lines = [f"{icon} <b>Alert TradingView — {html.escape(sym)}</b>",
                 f"Sinyal: {html.escape(self._describe(alert))}"]
        if quote is not None:
            line = f"Harga terkini: <b>{money(quote.price)}</b> ({pct(quote.change_pct)} hari ini)"
            if alert.price:
                line += f" · {pct((quote.price / alert.price - 1) * 100)} dari harga alert"
            lines.append(line)
            limits = None if is_index(sym) else price_limits(quote.prev_close)
            if limits:
                status = limit_status(quote.price, limits)
                lim = f"ARB {rupiah(limits[0])} · ARA {rupiah(limits[1])}"
                if status:
                    lim += f" · ⚠️ <b>sedang {status}</b>"
                lines.append(lim)
        if order_result is not None:
            kind = order_result["kind"]
            label = {"ORDER": "✅ Order simulasi", "SKIP": "⏭ Order dilewati", "ERROR": "❌ Order ditolak"}.get(kind, kind)
            lines.append(f"{label}: {html.escape(order_result['message'])}")
        pos = self.broker.positions.get(sym) if self.broker is not None else None
        if pos and quote is not None:
            cost = pos["shares"] * pos["avg_price"]
            pl = pos["shares"] * quote.price - cost
            lines.append(f"Posisi: {pos['shares'] // LOT_SIZE} lot · avg {rupiah(pos['avg_price'])} · "
                         f"P/L {'+' if pl >= 0 else '-'}Rp{rupiah(abs(pl))} ({pct(pl / cost * 100 if cost else 0)})")
        elif self.broker is not None and order_result is not None and not is_index(sym):
            lines.append("Posisi: tidak ada")
        lines += ["", stock_links(sym)]
        return "\n".join(lines)

    def _notify(self, alert: Alert, order_result: dict | None, quote=None) -> dict:
        if not self._telegram_ready():
            return self._record("SKIP", alert.symbol, "Telegram belum diatur, notifikasi dilewati")
        err = self._send(self.format_message(alert, order_result, quote))
        if err:
            return self._record("ERROR", alert.symbol, f"Telegram gagal: {err}")
        return self._record("NOTIFY", alert.symbol, "Notifikasi Telegram terkirim")

    def report_auth_failure(self, ip: str) -> dict | None:
        """Catat alert dengan kode rahasia salah; kabari Telegram maksimal sekali per 10 menit."""
        now = self.clock()
        self._auth_failures.append((now, ip or "?"))
        entry = self._record("AUTH", "-", f"Alert ditolak: kode rahasia salah (dari {ip or '?'})")
        if not (self.config.notify_security and self._telegram_ready()):
            return entry
        if now - self._auth_last_notice < AUTH_NOTICE_SECONDS:
            return entry  # dikumpulkan ke peringatan berikutnya
        attempts, ips = len(self._auth_failures), sorted({i for _, i in self._auth_failures})
        when = datetime.fromtimestamp(now, WIB).strftime("%d/%m %H:%M")
        text = "\n".join([
            "⚠️ <b>Webhook: alert dengan kode rahasia salah</b>",
            f"{attempts} percobaan ditolak (terakhir {when} WIB) dari: {html.escape(', '.join(ips[:5]))}"
            + (f" dan {len(ips) - 5} lainnya" if len(ips) > 5 else ""),
            "Bila ini bukan Anda (mis. pesan alert lama), alamat webhook mungkin diketahui orang lain —",
            "ganti kode rahasia di tab Webhook lalu perbarui pesan alert di TradingView.",
        ])
        err = self._send(text)
        if err:
            return self._record("ERROR", "-", f"Peringatan keamanan gagal dikirim: {err}")
        self._auth_last_notice, self._auth_failures = now, []
        return self._record("SECURITY", "-", f"Peringatan keamanan terkirim ({attempts} percobaan)")

    def status(self) -> dict:
        cfg = asdict(self.config)
        return {"config": cfg, "template_strategy": self.message_template(True),
                "template_indicator": self.message_template(False), "log": list(self.log)[:100]}
