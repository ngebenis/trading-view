"""Notifikasi Telegram untuk sinyal teknikal & transaksi bot.

- TelegramClient : kirim pesan lewat Telegram Bot API.
- SignalWatcher  : memantau daftar saham di latar belakang dan mengirim pesan saat
                   sinyal BERUBAH menjadi BELI atau JUAL (tidak mengulang sinyal yang sama).
"""
import html
import json
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .autotrader import WIB, is_idx_market_open, rupiah
from .idx_rules import normalize_symbol
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
def _links(symbol: str) -> str:
    return (f'<a href="https://www.tradingview.com/symbols/IDX-{symbol}/">TradingView</a> · '
            f'<a href="https://stockbit.com/symbol/{symbol}">Stockbit</a>')


def format_signal(symbol: str, action: str, analysis: dict, quote=None) -> str:
    icon, label = {"BUY": ("🟢", "SINYAL BELI"), "SELL": ("🔴", "SINYAL JUAL")}[action]
    lines = [f"{icon} <b>{label} — {html.escape(symbol)}</b>"]
    if quote is not None:
        sign = "+" if quote.change_pct >= 0 else ""
        pct = f"{quote.change_pct:.2f}".replace(".", ",")
        lines.append(f"Harga: <b>{rupiah(quote.price)}</b> ({sign}{pct}%)")
    lines.append(f"Skor: <b>{analysis['score']:+d}</b>")
    lines += [f"• {html.escape(r)}" for r in analysis["reasons"]]
    lines += ["", _links(symbol), "<i>Sinyal otomatis, bukan rekomendasi investasi.</i>"]
    return "\n".join(lines)


def format_trade(entry: dict) -> str:
    icon = "🛒" if entry.get("side") == "BUY" else "💰"
    return (f"{icon} <b>Auto-trading (simulasi)</b>\n"
            f"{html.escape(entry['symbol'])}: {html.escape(entry['message'])}")


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

    def validate(self) -> None:
        self.symbols = sorted({normalize_symbol(s) for s in self.symbols if s.strip()})
        self.bot_token, self.chat_id = self.bot_token.strip(), str(self.chat_id).strip()
        errors = []
        if not self.symbols:
            errors.append("Daftar simbol tidak boleh kosong")
        if self.interval_seconds < 60:
            errors.append("Interval minimal 60 detik")
        if self.min_buy_score < 1 or self.max_sell_score > -1:
            errors.append("Skor beli minimal 1 dan skor jual maksimal -1")
        if errors:
            raise ValueError("; ".join(errors))


def mask(token: str) -> str:
    return f"{token[:4]}…{token[-4:]}" if len(token) > 12 else ("•" * len(token))


class SignalWatcher:
    def __init__(self, provider, path: Path | None, env_token: str = "", env_chat_id: str = "",
                 http: httpx.Client | None = None, clock=time.time):
        self.provider = provider
        self.path = path
        self.env_token, self.env_chat_id = env_token, env_chat_id
        self.http = http
        self.clock = clock
        self.history: deque[dict] = deque(maxlen=200)
        self.last_action: dict[str, str] = {}
        self.last_run: float | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.config = self._load()

    # ---- konfigurasi & penyimpanan -----------------------------------
    def _load(self) -> WatchConfig:
        if self.path is not None and self.path.exists():
            raw = json.loads(self.path.read_text())
            self.last_action = raw.pop("_last_action", {})
            known = {f.name for f in fields(WatchConfig)}
            return WatchConfig(**{k: v for k, v in raw.items() if k in known})
        return WatchConfig()

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({**asdict(self.config), "_last_action": self.last_action}, indent=2))

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
        if cfg.symbols != self.config.symbols:
            self.last_action = {s: a for s, a in self.last_action.items() if s in cfg.symbols}
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
        self.history.appendleft(entry)
        return entry

    def _send(self, text: str) -> tuple[bool, str]:
        try:
            self.telegram().send(self.chat_id, text)
            return True, ""
        except NotifierError as exc:
            return False, str(exc)

    def scan(self) -> list[dict]:
        """Periksa semua simbol; kirim notifikasi untuk sinyal BELI/JUAL yang baru muncul."""
        with self._lock:
            now = self.clock()
            self.last_run = now
            cfg = self.config
            if cfg.market_hours_only and not is_idx_market_open(datetime.fromtimestamp(now, timezone.utc)):
                return [self._record("INFO", "-", "Bursa tutup, pemindaian dilewati")]
            out = []
            for sym in cfg.symbols:
                try:
                    analysis = analyze([c.close for c in self.provider.candles(sym, "1y", "1d")])
                    quote = self.provider.quote(sym)
                except MarketDataError as exc:
                    out.append(self._record("WARN", sym, f"Data tidak tersedia: {exc}"))
                    continue
                score = analysis["score"]
                action = "BUY" if score >= cfg.min_buy_score else "SELL" if score <= cfg.max_sell_score else "HOLD"
                prev = self.last_action.get(sym)
                if action == prev:
                    continue
                if action == "HOLD":
                    self.last_action[sym] = action  # sinyal mereda; catat diam-diam
                    continue
                ok, err = self._send(format_signal(sym, action, analysis, quote))
                label = "BELI" if action == "BUY" else "JUAL"
                if ok:
                    self.last_action[sym] = action
                    out.append(self._record(action, sym, f"Sinyal {label} skor {score:+d} @ {rupiah(quote.price)}", sent=True))
                else:  # tidak dicatat sebagai terkirim -> dicoba lagi siklus berikutnya
                    out.append(self._record("ERROR", sym, f"Sinyal {label} gagal dikirim: {err}"))
            self._save()
            if not out:
                out.append(self._record("INFO", "-", f"Tidak ada sinyal baru ({len(cfg.symbols)} simbol dipindai)"))
            return out

    def send_test(self) -> None:
        when = datetime.fromtimestamp(self.clock(), WIB).strftime("%d/%m/%Y %H:%M")
        self.telegram().send(self.chat_id, f"✅ <b>IDX Trading View</b>\nNotifikasi Telegram aktif ({when} WIB).")
        self._record("TEST", "-", "Pesan uji terkirim", sent=True)

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
        }
