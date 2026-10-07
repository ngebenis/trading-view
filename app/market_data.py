"""Penyedia data pasar.

- YahooProvider : data harga nyata saham IDX (kode + '.JK'), delay ~10-15 menit.
- DemoProvider  : data sintetis deterministik untuk dicoba tanpa internet.

Stockbit dan Pluang tidak menyediakan API data publik resmi, jadi data harga
diambil dari sumber publik; grafik interaktif di frontend memakai widget
TradingView.
"""
import hashlib
import math
import random
import threading
import time
from dataclasses import dataclass, asdict

import httpx

from .idx_rules import is_index, normalize_symbol, round_to_tick, yahoo_symbol


@dataclass
class Candle:
    time: int  # unix timestamp (detik)
    open: float
    high: float
    low: float
    close: float
    volume: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Quote:
    symbol: str
    price: float
    prev_close: float
    change: float
    change_pct: float
    currency: str = "IDR"
    source: str = ""
    market_time: float | None = None  # waktu (epoch) transaksi terakhir menurut sumber data

    def to_dict(self) -> dict:
        return asdict(self)


INTRADAY_INTERVALS = {"1m": 60, "5m": 300, "15m": 900}


class MarketDataError(Exception):
    pass


class YahooProvider:
    """Endpoint chart Yahoo Finance (tidak resmi, tanpa API key)."""

    name = "yahoo"
    _HOSTS = ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com")
    _PATH = "/v8/finance/chart/{symbol}"

    def __init__(self, timeout: float = 10.0, cache_ttl: float = 30.0, client: httpx.Client | None = None,
                 quote_ttl: float = 5.0, intraday_ttl: float = 15.0):
        self._client = client or httpx.Client(timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        self._cache: dict[tuple, tuple[float, dict]] = {}
        self._lock = threading.Lock()
        self._ttl = cache_ttl            # histori harian
        self._quote_ttl = quote_ttl      # harga terkini (mode live)
        self._intraday_ttl = intraday_ttl

    def _fetch(self, symbol: str, range_: str, interval: str) -> httpx.Response:
        """Coba query1 lalu query2 bila kena rate limit / gangguan server."""
        resp = None
        for host in self._HOSTS:
            resp = self._client.get(host + self._PATH.format(symbol=yahoo_symbol(symbol)),
                                    params={"range": range_, "interval": interval})
            if resp.status_code not in (429, 500, 502, 503, 504):
                break
        return resp

    def _chart(self, symbol: str, range_: str, interval: str, ttl: float | None = None) -> dict:
        key = (normalize_symbol(symbol), range_, interval)
        ttl = self._ttl if ttl is None else ttl
        with self._lock:
            hit = self._cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        sym = normalize_symbol(symbol)
        try:
            resp = self._fetch(symbol, range_, interval)
        except httpx.HTTPError as exc:
            raise MarketDataError(f"Gagal menghubungi Yahoo Finance untuk {sym}: {type(exc).__name__}") from exc
        if resp.status_code == 404:
            raise MarketDataError(f"Kode {sym} tidak ditemukan di Yahoo Finance ({yahoo_symbol(sym)})")
        if resp.status_code == 429:
            raise MarketDataError("Yahoo Finance membatasi permintaan (rate limit), coba lagi beberapa saat")
        try:
            resp.raise_for_status()
            result = resp.json()["chart"]["result"]
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise MarketDataError(f"Gagal mengambil data {sym}: {exc}") from exc
        if not result:
            raise MarketDataError(f"Kode {sym} tidak ditemukan di Yahoo Finance")
        with self._lock:
            self._cache[key] = (time.time(), result[0])
        return result[0]

    def candles(self, symbol: str, range_: str = "6mo", interval: str = "1d") -> list[Candle]:
        data = self._chart(symbol, range_, interval, self._intraday_ttl if interval in INTRADAY_INTERVALS else None)
        q = data["indicators"]["quote"][0]
        out = []
        for i, ts in enumerate(data.get("timestamp") or []):
            o, h, l, c, v = (q[k][i] for k in ("open", "high", "low", "close", "volume"))
            if None in (o, h, l, c):
                continue
            out.append(Candle(ts, o, h, l, c, int(v or 0)))
        return out

    def quote(self, symbol: str) -> Quote:
        data = self._chart(symbol, "5d", "1d", self._quote_ttl)
        meta = data["meta"]
        price = meta["regularMarketPrice"]
        # Penutupan kemarin = close candle harian terakhir SEBELUM hari perdagangan saat ini.
        # (meta.chartPreviousClose adalah close sebelum awal rentang 5 hari, bukan kemarin.)
        tz_offset = meta.get("gmtoffset", 7 * 3600)
        today = (meta.get("regularMarketTime", time.time()) + tz_offset) // 86400
        closes = data["indicators"]["quote"][0]["close"]
        prev = next((c for ts, c in reversed(list(zip(data.get("timestamp") or [], closes)))
                     if c is not None and (ts + tz_offset) // 86400 < today), None)
        prev = prev or meta.get("previousClose") or price
        change = price - prev
        return Quote(
            normalize_symbol(symbol), price, prev, change,
            (change / prev * 100) if prev else 0.0, meta.get("currency", "IDR"), self.name,
            meta.get("regularMarketTime"),
        )


class DemoProvider:
    """Random walk deterministik per simbol, agar aplikasi bisa dipakai offline."""

    name = "demo"

    RANGE_DAYS = {"1mo": 22, "3mo": 66, "6mo": 130, "1y": 250, "2y": 500, "5y": 1250, "10y": 2500}
    HISTORY_DAYS = 2500

    def __init__(self, live: bool = False, clock=time.time):
        self.live = live    # True: harga bergerak sepanjang jam bursa (untuk mencoba mode live offline)
        self.clock = clock
        self._cache: dict[tuple[str, int], list[Candle]] = {}

    def _history(self, sym: str) -> list[Candle]:
        """Satu deret harga per simbol (per hari); semua rentang adalah potongan ujungnya,
        sehingga harga terakhir sama di grafik 1 bulan maupun 5 tahun."""
        today = int(time.time()) // 86400 * 86400
        key = (sym, today)
        if key in self._cache:
            return self._cache[key]
        seed = int(hashlib.sha256(sym.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        index = is_index(sym)
        price = 7000.0 if index else rng.choice([150, 450, 1200, 3500, 8000])
        rnd = (lambda x: round(x, 2)) if index else round_to_tick
        days = self.HISTORY_DAYS
        out = []
        vol, base_drift = (0.008, 0.0001) if index else (0.018, 0.0004)  # indeks bergerak lebih tenang
        for i in range(days):
            drift = base_drift + (0.0006 if index else 0.002) * math.sin(i / 17 + seed % 7)
            close = max(50.0, price * (1 + drift + rng.gauss(0, vol)))
            high = max(price, close) * (1 + abs(rng.gauss(0, 0.006)))
            low = min(price, close) * (1 - abs(rng.gauss(0, 0.006)))
            out.append(Candle(
                today - (days - i) * 86400,
                rnd(price), rnd(high), rnd(low), rnd(close), rng.randint(1_000_000, 50_000_000),
            ))
            price = close
        self._cache = {k: v for k, v in self._cache.items() if k[1] == today}  # buang cache hari lain
        self._cache[key] = out
        return out

    # ---- simulasi live (DemoProvider(live=True)) ----------------------
    SESSION_OPEN, SESSION_CLOSE = 9 * 3600, 16 * 3600  # detik sejak tengah malam WIB

    def _session_bounds(self, now: float) -> tuple[int, int]:
        """(awal sesi hari ini, waktu terakhir yang sudah berjalan) dalam epoch, menurut WIB."""
        wib_midnight = int((now + 7 * 3600) // 86400 * 86400 - 7 * 3600)
        start = wib_midnight + self.SESSION_OPEN
        return start, int(min(now, wib_midnight + self.SESSION_CLOSE))

    def _intraday(self, sym: str, now: float) -> list[Candle]:
        """Candle 1 menit sesi hari ini: random walk dari penutupan kemarin (deterministik per menit)."""
        start, last = self._session_bounds(now)
        if last < start:
            return []
        hist = self._history(sym)
        seed = int(hashlib.sha256(f"{sym}:{start}".encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        index = is_index(sym)
        rnd = (lambda x: round(x, 2)) if index else round_to_tick
        vol = 0.0006 if index else 0.0015
        price = hist[-1].close * (1 + rng.gauss(0, vol * 3))  # celah pembukaan
        out = []
        minutes = (last - start) // 60 + 1
        for m in range(minutes):
            o = price
            price = max(50.0, price * (1 + rng.gauss(0, vol)))
            if m == minutes - 1:  # menit berjalan: bergerak tiap 5 detik
                tick_rng = random.Random(seed ^ int(now // 5))
                price = max(50.0, price * (1 + tick_rng.gauss(0, vol * 2)))
            hi = max(o, price) * (1 + abs(rng.gauss(0, vol / 3)))
            lo = min(o, price) * (1 - abs(rng.gauss(0, vol / 3)))
            out.append(Candle(start + m * 60, rnd(o), rnd(hi), rnd(lo), rnd(price), rng.randint(500, 20_000) * 100))
        return out

    @staticmethod
    def _aggregate(bars: list[Candle], seconds: int) -> list[Candle]:
        out: list[Candle] = []
        for b in bars:
            t = b.time // seconds * seconds
            if out and out[-1].time == t:
                p = out[-1]
                out[-1] = Candle(t, p.open, max(p.high, b.high), min(p.low, b.low), b.close, p.volume + b.volume)
            else:
                out.append(Candle(t, b.open, b.high, b.low, b.close, b.volume))
        return out

    def candles(self, symbol: str, range_: str = "6mo", interval: str = "1d") -> list[Candle]:
        sym = normalize_symbol(symbol)
        if interval in INTRADAY_INTERVALS:
            if not self.live:
                raise MarketDataError("Data intraday demo hanya tersedia dalam mode live")
            return self._aggregate(self._intraday(sym, self.clock()), INTRADAY_INTERVALS[interval])
        days = self.RANGE_DAYS.get(range_, 130)
        hist = self._history(sym)
        if self.live:
            bars = self._intraday(sym, self.clock())
            if bars:  # candle harian hari ini, terbentuk dari pergerakan intraday
                hist = hist + [Candle(bars[0].time, bars[0].open, max(b.high for b in bars),
                                      min(b.low for b in bars), bars[-1].close, sum(b.volume for b in bars))]
        return list(hist[-days:])

    def quote(self, symbol: str) -> Quote:
        sym = normalize_symbol(symbol)
        hist = self._history(sym)
        if self.live:
            bars = self._intraday(sym, self.clock())
            if bars:
                prev, price = hist[-1].close, bars[-1].close
                market_time = min(self.clock(), self._session_bounds(self.clock())[1])
                return Quote(sym, price, prev, price - prev, (price / prev - 1) * 100, "IDR", self.name, market_time)
        price, prev = hist[-1].close, hist[-2].close
        change = price - prev
        return Quote(sym, price, prev, change, change / prev * 100, "IDR", self.name, hist[-1].time)


def get_provider(name: str):
    return DemoProvider(live=True) if name == "demo" else YahooProvider()
