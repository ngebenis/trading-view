"""Price feed lewat webhook TradingView.

Skrip Pine (dibuat oleh `pine_script`) dipasang di grafik TradingView dan memanggil `alert()` setiap
bar ditutup. Isi pesannya berisi OHLCV bar terakhir untuk beberapa saham sekaligus:

    {"secret": "...", "type": "bars", "tf": "1",
     "bars": [["IDX:BBCA", 1791340800000, 9025, 9050, 9000, 9050, 123400, 9000], ...]}

Satu baris bar = [simbol, waktu buka (ms), open, high, low, close, volume, close kemarin].
Data itu dikirim TradingView sendiri lewat fitur webhook resminya, jadi aplikasi ini tidak perlu
login ke TradingView. Harga yang masuk dipakai `FeedProvider` selama masih segar; bila feed
berhenti (alert mati, bursa istirahat, server tidak terjangkau) aplikasi kembali ke provider biasa.
"""
import math
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime

from .autotrader import WIB, is_idx_market_open
from .idx_rules import normalize_symbol, tradingview_symbol
from .market_data import INTRADAY_INTERVALS, Candle, MarketDataError, Quote, aggregate_candles

SOURCE = "tradingview"
# timeframe.period Pine -> detik. Feed harian tidak didukung (pakai data harian provider biasa).
TF_SECONDS = {"1": 60, "2": 120, "3": 180, "5": 300, "10": 600, "15": 900, "30": 1800, "60": 3600}
# Tiap saham memakai 2 request.security (bar + close kemarin); batas Pine Script 40 per skrip.
MAX_SYMBOLS = 20
MAX_BARS_PER_MESSAGE = 40
KEEP_DAYS = 7
DEFAULT_SYMBOLS = ["BBCA", "BBRI", "BMRI", "TLKM", "ASII", "IHSG"]


class FeedError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class FeedConfig:
    enabled: bool = False
    symbols: list[str] = field(default_factory=lambda: list(DEFAULT_SYMBOLS))
    # Harga feed dianggap basi bila tidak ada bar baru selama ini (min. 2,5x panjang bar).
    stale_seconds: float = 180

    def validate(self) -> None:
        self.symbols = list(dict.fromkeys(normalize_symbol(s) for s in self.symbols if str(s).strip()))
        errors = []
        if not self.symbols:
            errors.append("Isi minimal satu kode saham")
        if len(self.symbols) > MAX_SYMBOLS:
            errors.append(f"Maksimal {MAX_SYMBOLS} kode per skrip (batas request.security Pine Script)")
        if not 60 <= self.stale_seconds <= 3600:
            errors.append("Batas data basi harus 60–3600 detik")
        if errors:
            raise ValueError("; ".join(errors))


@dataclass
class SymbolFeed:
    seconds: int = 60                 # panjang bar dari TradingView
    prev_close: float | None = None   # close kemarin menurut TradingView
    received_at: float = 0.0          # kapan bar terakhir diterima
    bars: dict[int, Candle] = field(default_factory=dict)  # waktu buka (epoch detik) -> bar

    def last(self) -> Candle | None:
        return self.bars[max(self.bars)] if self.bars else None


def wib_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, WIB).strftime("%Y-%m-%d")


def _number(value, name: str, allow_zero: bool = False) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise FeedError(f"Nilai '{name}' harus angka") from None
    if not math.isfinite(x) or x < 0 or (x == 0 and not allow_zero):
        raise FeedError(f"Nilai '{name}' harus angka positif")
    return x


def parse_bar(row) -> tuple[str, Candle, float | None]:
    """[simbol, waktu ms, o, h, l, c, v, close kemarin] atau objek dengan kunci yang sama."""
    if isinstance(row, dict):
        row = [row.get(k) for k in ("symbol", "time", "open", "high", "low", "close", "volume", "prev_close")]
    if not isinstance(row, list) or len(row) < 6:
        raise FeedError("Bar harus [simbol, waktu, open, high, low, close, volume, close kemarin]")
    row = row + [None] * (8 - len(row))
    sym = normalize_symbol(str(row[0] or ""))
    if not sym:
        raise FeedError("Simbol bar kosong")
    t = _number(row[1], "time")
    t = int(t / 1000 if t > 1e11 else t)  # Pine memakai milidetik
    o, h, l, c = (_number(row[i], k) for i, k in ((2, "open"), (3, "high"), (4, "low"), (5, "close")))
    if not l <= min(o, c) <= max(o, c) <= h:
        raise FeedError(f"Bar {sym} tidak konsisten (low/high di luar open/close)")
    v = int(_number(row[6] if row[6] is not None else 0, "volume", allow_zero=True))
    pc = _number(row[7], "prev_close", allow_zero=True) if row[7] is not None else 0
    return sym, Candle(t, o, h, l, c, v), (pc or None)


class PriceFeed:
    def __init__(self, db=None, clock=time.time):
        self.db = db  # app.db.Database atau None
        self.clock = clock
        self._lock = threading.Lock()
        self._feeds: dict[str, SymbolFeed] = {}
        self.stats = {"messages": 0, "bars": 0, "ignored": 0, "last_message_at": None, "last_error": None}
        self.config = self._load()
        self._restore()

    # ---- konfigurasi -------------------------------------------------
    def _load(self) -> FeedConfig:
        raw = self.db.get_setting("price_feed") if self.db is not None else None
        if raw:
            known = {f.name for f in fields(FeedConfig)}
            return FeedConfig(**{k: v for k, v in raw.items() if k in known})
        return FeedConfig()

    def update_config(self, data: dict) -> FeedConfig:
        cfg = FeedConfig(**{**asdict(self.config), **data})
        cfg.validate()
        self.config = cfg
        if self.db is not None:
            self.db.set_setting("price_feed", asdict(cfg))
        return cfg

    def _restore(self) -> None:
        """Muat bar beberapa hari terakhir dari database (agar grafik tetap utuh setelah restart)."""
        if self.db is None:
            return
        since = self.clock() - KEEP_DAYS * 86400
        self.db.prune_feed_bars(since)
        for r in self.db.load_feed_bars(since):
            f = self._feeds.setdefault(r["symbol"], SymbolFeed())
            if f.bars and f.seconds != r["seconds"]:  # timeframe sempat diganti: pakai yang terbaru
                f.bars.clear()
            f.seconds, f.received_at = r["seconds"], max(f.received_at, r["received_at"])
            f.prev_close = r["prev_close"] or f.prev_close
            f.bars[r["time"]] = Candle(r["time"], r["open"], r["high"], r["low"], r["close"], r["volume"])

    # ---- menerima bar ------------------------------------------------
    def ingest(self, payload: dict) -> dict:
        """Simpan bar dari satu pesan alert. Kode rahasia sudah diperiksa pemanggil."""
        if not self.config.enabled:
            raise FeedError("Price feed nonaktif — aktifkan di tab Webhook", 403)
        tf = str(payload.get("tf") or payload.get("interval") or "1").strip()
        seconds = TF_SECONDS.get(tf)
        if seconds is None:
            raise FeedError(f"Timeframe '{tf}' tidak didukung; pakai grafik 1–60 menit (disarankan 1 menit)")
        rows = payload.get("bars")
        if not isinstance(rows, list) or not rows:
            raise FeedError("Field 'bars' harus daftar bar")
        if len(rows) > MAX_BARS_PER_MESSAGE:
            raise FeedError(f"Maksimal {MAX_BARS_PER_MESSAGE} bar per pesan", 413)
        now = self.clock()
        allowed = set(self.config.symbols)
        accepted, ignored, saved = [], [], []
        try:
            for row in rows:
                sym, bar, prev_close = parse_bar(row)
                # Hanya saham di daftar, dan hanya bar yang baru (bukan kiriman ulang yang sudah lama / dari masa depan).
                if sym not in allowed or not now - 2 * 86400 <= bar.time <= now + 120:
                    ignored.append(sym)
                    continue
                bar = Candle(bar.time // seconds * seconds, bar.open, bar.high, bar.low, bar.close, bar.volume)
                with self._lock:
                    f = self._feeds.setdefault(sym, SymbolFeed())
                    if f.seconds != seconds:  # timeframe grafik diganti: bar lama tidak bisa dicampur
                        f.bars.clear()
                        f.seconds = seconds
                    f.bars[bar.time] = bar
                    f.prev_close = prev_close or f.prev_close
                    f.received_at = now
                accepted.append(sym)
                saved.append((sym, seconds, bar, prev_close, now))
        except FeedError as exc:
            self.stats["last_error"] = {"time": now, "message": str(exc)}
            raise
        if self.db is not None and saved:
            self.db.save_feed_bars(saved)
        self.stats["messages"] += 1
        self.stats["bars"] += len(accepted)
        self.stats["ignored"] += len(ignored)
        self.stats["last_message_at"] = now
        return {"accepted": accepted, "ignored": ignored}

    # ---- membaca -----------------------------------------------------
    def _usable(self, f: SymbolFeed, now: float) -> bool:
        """Feed dipakai bila bar terakhir masih segar, atau (setelah bursa tutup) bila datang hari ini."""
        if not f.bars:
            return False
        stale = max(self.config.stale_seconds, 2.5 * f.seconds)
        if now - f.received_at <= stale:
            return True
        return not is_idx_market_open(datetime.fromtimestamp(now, WIB)) and wib_day(f.received_at) == wib_day(now)

    def feed_for(self, symbol: str) -> SymbolFeed | None:
        if not self.config.enabled:
            return None
        sym = normalize_symbol(symbol)
        with self._lock:
            f = self._feeds.get(sym)
            return f if f is not None and self._usable(f, self.clock()) else None

    def session_bars(self, symbol: str) -> tuple[int, list[Candle]]:
        """(panjang bar, bar feed hari ini) — kosong bila feed tidak aktif untuk simbol ini."""
        f = self.feed_for(symbol)
        if f is None:
            return 0, []
        today = wib_day(self.clock())
        with self._lock:
            return f.seconds, [f.bars[t] for t in sorted(f.bars) if wib_day(t) == today]

    def quote(self, symbol: str, prev_close_fallback=None) -> Quote | None:
        f = self.feed_for(symbol)
        if f is None:
            return None
        with self._lock:
            bar, prev = f.last(), f.prev_close
            market_time = min(bar.time + f.seconds, f.received_at)  # bar terkirim saat ditutup
        if not prev and prev_close_fallback is not None:
            prev = prev_close_fallback()
        prev = prev or bar.open
        change = bar.close - prev
        return Quote(normalize_symbol(symbol), bar.close, prev, change, change / prev * 100 if prev else 0.0,
                     "IDR", SOURCE, market_time)

    def status(self) -> dict:
        now = self.clock()
        rows = []
        with self._lock:
            for sym in self.config.symbols:
                f = self._feeds.get(sym)
                bar = f.last() if f else None
                rows.append({
                    "symbol": sym, "tradingview_symbol": tradingview_symbol(sym),
                    "price": bar.close if bar else None,
                    "bar_time": bar.time if bar else None,
                    "bar_seconds": f.seconds if f else None,
                    "received_at": f.received_at if f and f.received_at else None,
                    "age_seconds": round(now - f.received_at) if f and f.received_at else None,
                    "active": bool(f and self.config.enabled and self._usable(f, now)),
                    "bars_today": sum(1 for t in f.bars if wib_day(t) == wib_day(now)) if f else 0,
                })
        return {"config": asdict(self.config), "symbols": rows, "stats": dict(self.stats),
                "max_symbols": MAX_SYMBOLS}


class FeedProvider:
    """Bungkus provider biasa: harga & bar intraday dari feed TradingView selama feed aktif."""

    def __init__(self, base, feed: PriceFeed):
        self.base = base
        self.feed = feed
        self.name = base.name

    def quote(self, symbol: str) -> Quote:
        def base_prev():
            try:
                return self.base.quote(symbol).prev_close
            except MarketDataError:
                return None

        q = self.feed.quote(symbol, base_prev)
        return q if q is not None else self.base.quote(symbol)

    def candles(self, symbol: str, range_: str = "6mo", interval: str = "1d") -> list[Candle]:
        seconds, bars = self.feed.session_bars(symbol)
        if not bars:
            return self.base.candles(symbol, range_, interval)
        if interval in INTRADAY_INTERVALS:
            target = INTRADAY_INTERVALS[interval]
            if target % seconds:  # feed lebih kasar dari grafik (mis. feed 5 menit, grafik 1 menit)
                return self.base.candles(symbol, range_, interval)
            try:
                base = self.base.candles(symbol, range_, interval)
            except MarketDataError:
                base = []  # sumber lain gagal: tampilkan bar dari feed saja
            return merge_candles(base, aggregate_candles(bars, target), target)
        base = self.base.candles(symbol, range_, interval)
        day = Candle(bars[0].time, bars[0].open, max(b.high for b in bars), min(b.low for b in bars),
                     bars[-1].close, sum(b.volume for b in bars))
        if base and wib_day(base[-1].time) == wib_day(day.time):
            return base[:-1] + [combine(base[-1], day)]
        return base + [day]


def combine(base: Candle, feed: Candle) -> Candle:
    """Gabungkan candle sumber lain (tertunda) dengan candle feed pada periode yang sama."""
    return Candle(base.time, base.open, max(base.high, feed.high), min(base.low, feed.low), feed.close,
                  max(base.volume, feed.volume))


def merge_candles(base: list[Candle], feed: list[Candle], seconds: int) -> list[Candle]:
    out = {c.time: c for c in aggregate_candles(base, seconds)}
    for c in feed:
        out[c.time] = combine(out[c.time], c) if c.time in out else c
    return [out[t] for t in sorted(out)]


def pine_script(secret: str, symbols: list[str]) -> str:
    """Skrip indikator Pine v5 yang mengirim bar terakhir semua saham via alert() tiap bar ditutup."""
    syms = [tradingview_symbol(s) for s in symbols][:MAX_SYMBOLS]
    calls = "\n".join(f'b{i} = feedBar("{s}")' for i, s in enumerate(syms))
    parts = ", ".join(f"b{i}" for i in range(len(syms)))
    return f"""//@version=5
// IDX Trading View — price feed. Dibuat otomatis oleh aplikasi.
// JANGAN dipublikasikan / dibagikan: skrip ini memuat kode rahasia webhook Anda.
// Pasang di grafik 1 menit saham yang ramai (mis. IDX:BBCA), lalu buat alert:
// Condition = indikator ini, "Any alert() function call", Webhook URL = alamat aplikasi.
indicator("IDX Trading View price feed", overlay=true)

feedBar(sym) =>
    [t, o, h, l, c, v] = request.security(sym, timeframe.period, [time, open, high, low, close, volume])
    pc = request.security(sym, "D", close[1], lookahead=barmerge.lookahead_on)
    na(c) ? "" : str.format('["{{0}}",{{1,number,#}},{{2,number,#.####}},{{3,number,#.####}},{{4,number,#.####}},{{5,number,#.####}},{{6,number,#}},{{7,number,#.####}}]', sym, t, o, h, l, c, nz(v), nz(pc))

{calls}

bars = ""
for b in array.from({parts})
    if b != ""
        bars := bars == "" ? b : bars + "," + b

if bars != ""
    alert('{{"secret":"{secret}","type":"bars","tf":"' + timeframe.period + '","bars":[' + bars + ']}}', alert.freq_once_per_bar_close)

plot(close, "Harga", display=display.none)
"""
