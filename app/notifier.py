"""Notifikasi Telegram untuk sinyal teknikal & transaksi bot.

- TelegramClient : kirim pesan lewat Telegram Bot API.
- SignalWatcher  : memantau daftar saham di latar belakang dan mengirim pesan saat
                   sinyal BERUBAH menjadi BELI atau JUAL (tidak mengulang sinyal yang sama).
"""
import html
import threading
from concurrent.futures import ThreadPoolExecutor
import time
from collections import deque
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone

import httpx

from .autotrader import WIB, is_idx_market_open, rupiah
from .binance import KLINE_INTERVALS, normalize_pair, split_pair
from .idx_rules import is_index, limit_status, normalize_symbol, price_limits, stockbit_url, tradingview_url
from .market_data import MarketDataError
from .strategy import analyze


class NotifierError(Exception):
    pass


class TelegramClient:
    API = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self, token: str, client: httpx.Client | None = None):
        self.token = token
        self._client = client or httpx.Client(timeout=10)

    def _call(self, method: str, **params) -> dict:
        if not self.token:
            raise NotifierError("Bot token Telegram belum diisi")
        try:
            resp = self._client.post(self.API.format(token=self.token, method=method), json=params)
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise NotifierError(f"Gagal menghubungi Telegram: {type(exc).__name__}") from exc
        if not data.get("ok"):
            raise NotifierError(f"Telegram menolak: {data.get('description', resp.status_code)}")
        return data["result"]

    def send(self, chat_id: str, text: str) -> None:
        if not chat_id:
            raise NotifierError("Chat ID Telegram belum diisi")
        self._call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                   disable_web_page_preview=True)

    def recent_chats(self) -> list[dict]:
        """Chat yang baru mengirim pesan ke bot (untuk menemukan chat ID)."""
        chats = {}
        for upd in self._call("getUpdates", limit=50):
            msg = upd.get("message") or upd.get("channel_post") or upd.get("my_chat_member") or {}
            chat = msg.get("chat")
            if chat:
                name = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
                chats[chat["id"]] = {"id": str(chat["id"]), "name": name or chat.get("username", ""),
                                     "type": chat.get("type", "")}
        return list(chats.values())


# ---- format pesan ----------------------------------------------------------
def index_value(x: float) -> str:
    """7123.456 -> '7.123,46' (format angka Indonesia, 2 desimal)."""
    return f"{x:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def stock_links(symbol: str) -> str:
    return (f'<a href="{tradingview_url(symbol)}">TradingView</a> · '
            f'<a href="{stockbit_url(symbol)}">Stockbit</a>')


def format_signal(symbol: str, action: str, analysis: dict, quote=None) -> str:
    icon, label = {"BUY": ("🟢", "SINYAL BELI"), "SELL": ("🔴", "SINYAL JUAL")}[action]
    lines = [f"{icon} <b>{label} — {html.escape(symbol)}</b>"]
    if quote is not None:
        sign = "+" if quote.change_pct >= 0 else ""
        pct = f"{quote.change_pct:.2f}".replace(".", ",")
        if is_index(symbol):
            lines.append(f"Nilai indeks: <b>{index_value(quote.price)}</b> ({sign}{pct}%)")
        else:
            lines.append(f"Harga: <b>{rupiah(quote.price)}</b> ({sign}{pct}%)")
    lines.append(f"Skor: <b>{analysis['score']:+d}</b>")
    lines += [f"• {html.escape(r)}" for r in analysis["reasons"]]
    lines += ["", stock_links(symbol), "<i>Sinyal otomatis, bukan rekomendasi investasi.</i>"]
    return "\n".join(lines)


def format_limit(symbol: str, status: str, quote, limits: tuple[int, int]) -> str:
    arb, ara = limits
    icon, label, level = ("🚀", "MENYENTUH ARA", ara) if status == "ARA" else ("🧊", "MENYENTUH ARB", arb)
    sign = "+" if quote.change_pct >= 0 else ""
    pct = f"{quote.change_pct:.2f}".replace(".", ",")
    return "\n".join([
        f"{icon} <b>{html.escape(symbol)} {label}</b>",
        f"Harga: <b>{rupiah(quote.price)}</b> ({sign}{pct}%) · batas {rupiah(level)}",
        f"Harga acuan {rupiah(quote.prev_close)} · rentang hari ini {rupiah(arb)} – {rupiah(ara)}",
        "", stock_links(symbol),
    ])


def format_trade(entry: dict) -> str:
    icon = "🛒" if entry.get("side") == "BUY" else "💰"
    return (f"{icon} <b>{html.escape(entry.get('title') or 'Auto-trading (simulasi)')}</b>\n"
            f"{html.escape(entry['symbol'])}: {html.escape(entry['message'])}")


CANDLE_LABEL = {"1m": "1 menit", "5m": "5 menit", "15m": "15 menit", "1h": "1 jam", "4h": "4 jam", "1d": "harian"}


def crypto_links(symbol: str) -> str:
    base, quote = split_pair(symbol) or (symbol, "")
    return (f'<a href="https://www.binance.com/en/trade/{base}_{quote}?type=spot">Binance</a> · '
            f'<a href="https://www.tradingview.com/symbols/{symbol}/?exchange=BINANCE">TradingView</a>')


def _pct(x: float) -> str:
    return f"{x:+.2f}%".replace(".", ",")


def format_crypto_signal(symbol: str, action: str, analysis: dict, quote, interval: str) -> str:
    from .crypto import price_str  # impor lokal: crypto.py ikut memuat modul auto-trader
    icon, label = {"BUY": ("🟢", "SINYAL BELI"), "SELL": ("🔴", "SINYAL JUAL")}[action]
    lines = [f"{icon} <b>{label} — {html.escape(symbol)}</b> (crypto · candle {CANDLE_LABEL.get(interval, interval)})"]
    if quote is not None:
        lines.append(f"Harga: <b>{price_str(quote.price)} {html.escape(quote.currency)}</b> ({_pct(quote.change_pct)} 24 jam)")
    lines.append(f"Skor: <b>{analysis['score']:+d}</b>")
    lines += [f"• {html.escape(r)}" for r in analysis["reasons"]]
    lines += ["", crypto_links(symbol), "<i>Sinyal otomatis, bukan rekomendasi investasi.</i>"]
    return "\n".join(lines)


def format_crypto_move(symbol: str, quote, threshold: float) -> str:
    from .crypto import price_str
    up = quote.change_pct > 0
    return "\n".join([
        f"{'🚀' if up else '📉'} <b>{html.escape(symbol)} {'naik' if up else 'turun'} {_pct(quote.change_pct)} dalam 24 jam</b>",
        f"Harga: <b>{price_str(quote.price)} {html.escape(quote.currency)}</b> · 24 jam lalu {price_str(quote.prev_close)}"
        f" · ambang ±{str(threshold).removesuffix('.0').replace('.', ',')}%",
        "", crypto_links(symbol),
    ])


ORDER_EVENT = {  # (ikon, judul) per kejadian/status
    "FILLED": ("✅", "Terisi"), "OPEN": ("⏳", "Order limit dipasang"), "REJECTED": ("❌", "Ditolak"),
    "CANCELLED": ("🚫", "Dibatalkan"), "filled": ("✅", "Order limit terisi"),
}


def format_order(o: dict, event: str, market: str, account: str, position: dict | None = None) -> str:
    """Pesan order manual. `o` = Order/CryptoOrder.to_dict(); event = placed / filled / cancelled."""
    sym = o["symbol"]
    side = "BELI" if o["side"] == "BUY" else "JUAL"
    key = "filled" if event == "filled" else ("CANCELLED" if event == "cancelled" else o["status"])
    icon, label = ORDER_EVENT.get(key, ("📝", o["status"]))
    price = o.get("fill_price") or o.get("limit_price")
    if market == "crypto":
        from .crypto import price_str, qty_str, usdt
        base, quote = split_pair(sym) or (sym, "")
        qty = o.get("filled_qty") or o["quantity"]
        amount = f"{qty_str(qty)} {base}".replace(".", ",")
        at = f" @ {price_str(price)} {quote}" if price else ""
        value = f" ({usdt(qty * price)} {quote})" if price else ""
        fee = f" · fee {qty_str(o['fee']).replace('.', ',')} {o.get('fee_asset', '')}".rstrip() if o.get("fee") else ""
        links = crypto_links(sym)
    else:
        amount = f"{o['lots']} lot"
        at = f" @ {rupiah(price)}" if price else ""
        value = f" (Rp{rupiah(o['lots'] * 100 * price)})" if price else ""
        fee = f" · fee Rp{rupiah(o['fee'])}" if o.get("fee") else ""
        links = stock_links(sym)
    lines = [f"{'🛒' if o['side'] == 'BUY' else '💰'} <b>Order manual — {side} {html.escape(sym)}</b>",
             f"{icon} {label}: {amount}{at}{value}{fee if key in ('FILLED', 'filled') else ''}",
             f"Tipe {o['order_type'].lower()} · akun {html.escape(account)}"]
    if key == "REJECTED" and o.get("message"):
        lines.append(f"Alasan: {html.escape(o['message'])}")
    if position and key in ("FILLED", "filled"):
        if market == "crypto":
            from .crypto import price_str, qty_str
            lines.append(f"Posisi: {qty_str(position['quantity']).replace('.', ',')} {base} · avg {price_str(position['avg_price'])}")
        else:
            lines.append(f"Posisi: {position['shares'] // 100} lot · avg {rupiah(position['avg_price'])}")
    elif key in ("FILLED", "filled") and o["side"] == "SELL" and market == "idx" and position is None:
        lines.append("Posisi: habis terjual")
    lines += ["", links]
    return "\n".join(lines)


# ---- pemantau sinyal -------------------------------------------------------
@dataclass
class WatchConfig:
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""
    symbols: list[str] = field(default_factory=lambda: ["BBCA", "BBRI", "BMRI", "TLKM", "ASII"])
    interval_seconds: int = 900
    min_buy_score: int = 2
    max_sell_score: int = -2
    market_hours_only: bool = False
    notify_trades: bool = True  # kirim juga transaksi bot auto-trading
    notify_orders: bool = True  # kirim order manual (terisi, limit dipasang/terisi, ditolak, dibatalkan)
    # Laporan harian portofolio (akun simulasi saham & crypto) pada jam tertentu (WIB).
    report_enabled: bool = False
    report_time: str = "16:15"
    report_weekdays_only: bool = True
    report_crypto: bool = True
    # Laporan mingguan: hari (0 = Senin … 6 = Minggu) & jam (WIB).
    weekly_enabled: bool = False
    weekly_day: int = 4
    weekly_time: str = "16:30"
    # Laporan bulanan: tanggal (0 = hari terakhir bulan, atau 1–28) & jam (WIB).
    monthly_enabled: bool = False
    monthly_day: int = 0
    monthly_time: str = "16:45"
    notify_limits: bool = True  # kirim saat saham menyentuh ARA / ARB (sekali per hari per saham)
    # Crypto (Binance): pasar 24 jam, jadi tidak terpengaruh "hanya saat jam bursa".
    crypto_symbols: list[str] = field(default_factory=list)
    crypto_candle_interval: str = "1h"
    crypto_move_pct: float = 5.0  # kabari bila perubahan 24 jam >= ±x% (sekali per hari per arah); 0 = mati

    def validate(self) -> None:
        self.symbols = sorted({normalize_symbol(s) for s in self.symbols if s.strip()})
        self.crypto_symbols = sorted({normalize_pair(s) for s in self.crypto_symbols if str(s).strip()})
        self.bot_token, self.chat_id = self.bot_token.strip(), str(self.chat_id).strip()
        errors = []
        if not self.symbols and not self.crypto_symbols:
            errors.append("Isi minimal satu saham atau pasangan crypto")
        bad = [s for s in self.crypto_symbols if not split_pair(s)]
        if bad:
            errors.append(f"Bukan pasangan crypto: {', '.join(bad)} (contoh: BTCUSDT)")
        if len(self.symbols) + len(self.crypto_symbols) > 60:
            errors.append("Maksimal 60 simbol dipantau")
        if self.crypto_candle_interval not in KLINE_INTERVALS:
            errors.append(f"Candle crypto harus salah satu dari {', '.join(KLINE_INTERVALS)}")
        if not 0 <= self.crypto_move_pct <= 100:
            errors.append("Ambang gerakan crypto harus 0–100%")
        for attr, label in (("report_time", "harian"), ("weekly_time", "mingguan"), ("monthly_time", "bulanan")):
            try:
                hh, mm = (int(x) for x in str(getattr(self, attr)).split(":"))
                if not (0 <= hh < 24 and 0 <= mm < 60):
                    raise ValueError
                setattr(self, attr, f"{hh:02d}:{mm:02d}")
            except ValueError:
                errors.append(f"Jam laporan {label} harus berformat JJ:MM (mis. 16:15)")
        if self.weekly_day not in range(7):
            errors.append("Hari laporan mingguan harus 0 (Senin) – 6 (Minggu)")
        if self.monthly_day not in range(29):
            errors.append("Tanggal laporan bulanan harus 0 (hari terakhir bulan) atau 1–28")
        if self.interval_seconds < 60:
            errors.append("Interval minimal 60 detik")
        if self.min_buy_score < 1 or self.max_sell_score > -1:
            errors.append("Skor beli minimal 1 dan skor jual maksimal -1")
        if errors:
            raise ValueError("; ".join(errors))


def mask(token: str) -> str:
    return f"{token[:4]}…{token[-4:]}" if len(token) > 12 else ("•" * len(token))


class SignalWatcher:
    def __init__(self, provider, db, env_token: str = "", env_chat_id: str = "",
                 http: httpx.Client | None = None, clock=time.time):
        self.provider = provider
        self.crypto_provider = None  # BinanceProvider, dipasang oleh register_crypto
        self.reporter = None  # DailyReporter, dipasang oleh create_app
        self.db = db  # app.db.Database atau None
        self.env_token, self.env_chat_id = env_token, env_chat_id
        self.http = http
        self.clock = clock
        self.history: deque[dict] = deque(db.recent_logs("notifications", 200) if db else (), maxlen=200)
        self.last_action: dict[str, str] = {}
        self.last_limit: dict[str, str] = {}  # simbol -> "YYYY-MM-DD:ARA" yang sudah dikabarkan
        self.last_run: float | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.sync_send = False  # True: kirim langsung (tes); False: di thread latar belakang
        self._executor: ThreadPoolExecutor | None = None
        self.config = self._load()

    # ---- konfigurasi & penyimpanan -----------------------------------
    def _load(self) -> WatchConfig:
        raw = dict(self.db.get_setting("notifications") or {}) if self.db is not None else {}
        if raw:
            self.last_action = raw.pop("_last_action", {})
            self.last_limit = raw.pop("_last_limit", {})
            known = {f.name for f in fields(WatchConfig)}
            return WatchConfig(**{k: v for k, v in raw.items() if k in known})
        return WatchConfig()

    def _save(self) -> None:
        if self.db is not None:
            self.db.set_setting("notifications", {**asdict(self.config), "_last_action": self.last_action,
                                                  "_last_limit": self.last_limit})

    @property
    def token(self) -> str:  # .env lebih diutamakan daripada isian UI
        return self.env_token or self.config.bot_token

    @property
    def chat_id(self) -> str:
        return self.env_chat_id or self.config.chat_id

    def telegram(self) -> TelegramClient:
        return TelegramClient(self.token, self.http)

    def update_config(self, data: dict) -> WatchConfig:
        data = dict(data)
        if self.config.bot_token and data.get("bot_token") == mask(self.config.bot_token):
            data.pop("bot_token")  # UI mengirim balik token yang disamarkan -> jangan timpa
        cfg = WatchConfig(**{**asdict(self.config), **data, "enabled": self.config.enabled})
        cfg.validate()
        watched = set(cfg.symbols) | set(cfg.crypto_symbols)
        self.last_action = {s: a for s, a in self.last_action.items() if s in watched}
        self.config = cfg
        self._save()
        return cfg

    # ---- loop latar belakang ----------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if not (self.token and self.chat_id):
            raise NotifierError("Isi bot token dan chat ID Telegram terlebih dahulu")
        self.config.enabled = True
        self._save()
        if not self.running:
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="signal-watcher", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self.config.enabled = False
        self._save()
        if self.running:
            self._stop.set()
            self._thread.join(timeout=5)

    def shutdown(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan()
            except Exception as exc:  # loop tidak boleh mati
                self._record("ERROR", "-", f"Pemindaian gagal: {exc}")
            self._stop.wait(self.config.interval_seconds)

    # ---- inti --------------------------------------------------------
    def _record(self, kind: str, symbol: str, message: str, sent: bool = False) -> dict:
        entry = {"time": self.clock(), "kind": kind, "symbol": symbol, "message": message, "sent": sent}
        if self.db is not None:
            entry["id"] = self.db.append_log("notifications", entry)
        self.history.appendleft(entry)
        return entry

    def _send(self, text: str) -> tuple[bool, str]:
        try:
            self.telegram().send(self.chat_id, text)
            return True, ""
        except NotifierError as exc:
            return False, str(exc)

    def _signal_change(self, sym: str, analysis: dict, message, price_text: str) -> dict | None:
        """Kirim pesan bila sinyal BERUBAH menjadi BELI/JUAL. `message(action)` membuat isi pesan."""
        cfg = self.config
        score = analysis["score"]
        action = "BUY" if score >= cfg.min_buy_score else "SELL" if score <= cfg.max_sell_score else "HOLD"
        if action == self.last_action.get(sym):
            return None
        if action == "HOLD":
            self.last_action[sym] = action  # sinyal mereda; catat diam-diam
            return None
        ok, err = self._send(message(action))
        label = "BELI" if action == "BUY" else "JUAL"
        if not ok:  # tidak dicatat sebagai terkirim -> dicoba lagi siklus berikutnya
            return self._record("ERROR", sym, f"Sinyal {label} gagal dikirim: {err}")
        self.last_action[sym] = action
        return self._record(action, sym, f"Sinyal {label} skor {score:+d} @ {price_text}", sent=True)

    def scan(self) -> list[dict]:
        """Periksa semua simbol; kirim notifikasi untuk sinyal BELI/JUAL yang baru muncul."""
        with self._lock:
            now = self.clock()
            self.last_run = now
            cfg = self.config
            out = []
            stocks = cfg.symbols
            if stocks and cfg.market_hours_only and not is_idx_market_open(datetime.fromtimestamp(now, timezone.utc)):
                stocks = []
                if not cfg.crypto_symbols:
                    return [self._record("INFO", "-", "Bursa tutup, pemindaian dilewati")]
            for sym in stocks:
                try:
                    analysis = analyze([c.close for c in self.provider.candles(sym, "1y", "1d")])
                    quote = self.provider.quote(sym)
                except MarketDataError as exc:
                    out.append(self._record("WARN", sym, f"Data tidak tersedia: {exc}"))
                    continue
                if cfg.notify_limits and not is_index(sym):
                    out.extend(self._check_limit(sym, quote, now))
                at = index_value(quote.price) if is_index(sym) else rupiah(quote.price)
                rec = self._signal_change(sym, analysis, lambda a: format_signal(sym, a, analysis, quote), at)
                if rec:
                    out.append(rec)
            out.extend(self._scan_crypto(now))
            self._save()
            if not out:
                scanned = len(stocks) + (len(cfg.crypto_symbols) if self.crypto_provider is not None else 0)
                out.append(self._record("INFO", "-", f"Tidak ada sinyal baru ({scanned} simbol dipindai)"))
            return out

    def _scan_crypto(self, now: float) -> list[dict]:
        cfg = self.config
        if not cfg.crypto_symbols:
            return []
        if self.crypto_provider is None:
            return [self._record("WARN", "-", "Data crypto (Binance) belum tersedia, pasangan crypto dilewati")]
        from .crypto import price_str
        out = []
        for sym in cfg.crypto_symbols:
            try:
                candles = self.crypto_provider.candles(sym, None, cfg.crypto_candle_interval, 300)
                analysis = analyze([c.close for c in candles])
                quote = self.crypto_provider.quote(sym)
            except MarketDataError as exc:
                out.append(self._record("WARN", sym, f"Data tidak tersedia: {exc}"))
                continue
            if cfg.crypto_move_pct:
                out.extend(self._check_move(sym, quote, now))
            rec = self._signal_change(
                sym, analysis, lambda a: format_crypto_signal(sym, a, analysis, quote, cfg.crypto_candle_interval),
                f"{price_str(quote.price)} {quote.currency}")
            if rec:
                out.append(rec)
        return out

    def _check_move(self, sym: str, quote, now: float) -> list[dict]:
        """Kabarkan gerakan besar 24 jam — sekali per pasangan per hari per arah."""
        threshold = self.config.crypto_move_pct
        if abs(quote.change_pct) < threshold:
            return []
        direction = "UP" if quote.change_pct > 0 else "DOWN"
        key = f"{datetime.fromtimestamp(now, WIB):%Y-%m-%d}:{direction}"
        if self.last_limit.get(sym) == key:
            return []
        ok, err = self._send(format_crypto_move(sym, quote, threshold))
        if not ok:
            return [self._record("ERROR", sym, f"Notifikasi gerakan 24 jam gagal dikirim: {err}")]
        self.last_limit[sym] = key
        return [self._record("MOVE", sym, f"{'Naik' if direction == 'UP' else 'Turun'} {_pct(quote.change_pct)} dalam 24 jam",
                             sent=True)]

    def _check_limit(self, sym: str, quote, now: float) -> list[dict]:
        """Kabarkan bila saham menyentuh ARA/ARB — sekali per saham per hari per jenis."""
        limits = price_limits(quote.prev_close)
        status = limit_status(quote.price, limits)
        if not status:
            return []
        key = f"{datetime.fromtimestamp(now, WIB):%Y-%m-%d}:{status}"
        if self.last_limit.get(sym) == key:
            return []
        ok, err = self._send(format_limit(sym, status, quote, limits))
        if not ok:
            return [self._record("ERROR", sym, f"Notifikasi {status} gagal dikirim: {err}")]
        self.last_limit[sym] = key
        return [self._record(status, sym, f"Menyentuh {status} @ {rupiah(quote.price)}", sent=True)]

    def send_test(self) -> None:
        when = datetime.fromtimestamp(self.clock(), WIB).strftime("%d/%m/%Y %H:%M")
        self.telegram().send(self.chat_id, f"✅ <b>IDX Trading View</b>\nNotifikasi Telegram aktif ({when} WIB).")
        self._record("TEST", "-", "Pesan uji terkirim", sent=True)

    def _dispatch(self, fn) -> None:
        """Jalankan pengiriman tanpa menahan request (order tetap cepat walau Telegram lambat)."""
        if self.sync_send:
            fn()
            return
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="telegram")
        self._executor.submit(fn)

    def notify_order(self, order: dict, event: str, market: str, account: str, position: dict | None = None) -> None:
        """Kabarkan order manual (saham atau crypto) bila diaktifkan & Telegram sudah diatur."""
        if not self.config.notify_orders or not (self.token and self.chat_id):
            return
        if order.get("source", "manual") != "manual":
            return  # order bot & webhook punya notifikasinya sendiri
        text = format_order(order, event, market, account, position)

        def send():
            ok, err = self._send(text)
            side = "BELI" if order["side"] == "BUY" else "JUAL"
            what = {"filled": "limit terisi", "cancelled": "dibatalkan"}.get(event) or {
                "FILLED": "terisi", "OPEN": "limit dipasang", "REJECTED": "ditolak"}.get(order["status"], order["status"])
            self._record("ORDER" if ok else "ERROR", order["symbol"],
                         f"Order manual {side} {what}" if ok else f"Notifikasi order gagal: {err}", sent=ok)
        self._dispatch(send)

    def on_autotrader_log(self, entry: dict) -> None:
        """Dipanggil AutoTrader untuk setiap entri log; hanya transaksi yang diteruskan."""
        if entry.get("level") != "TRADE" or not self.config.notify_trades or not (self.token and self.chat_id):
            return
        ok, err = self._send(format_trade(entry))
        self._record("TRADE" if ok else "ERROR", entry["symbol"],
                     entry["message"] if ok else f"Notifikasi transaksi gagal: {err}", sent=ok)

    def status(self) -> dict:
        cfg = asdict(self.config)
        cfg["bot_token"] = mask(self.config.bot_token)
        return {
            "config": cfg, "running": self.running, "last_run": self.last_run,
            "next_run": self.last_run + self.config.interval_seconds if self.running and self.last_run else None,
            "configured": bool(self.token and self.chat_id),
            "token_from_env": bool(self.env_token), "chat_id_from_env": bool(self.env_chat_id),
            "last_action": self.last_action, "history": list(self.history)[:100],
            "report_schedule": self.reporter.next_run() if self.reporter else None,
            "report_last_sent": self.reporter.last_sent if self.reporter else None,
            "weekly_schedule": self.reporter.next_weekly() if self.reporter else None,
            "weekly_last_sent": self.reporter.last_weekly_sent if self.reporter else None,
            "monthly_schedule": self.reporter.next_monthly() if self.reporter else None,
            "monthly_last_sent": self.reporter.last_monthly_sent if self.reporter else None,
        }
