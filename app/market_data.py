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
import time
from dataclasses import dataclass, asdict

import httpx

from .idx_rules import normalize_symbol, round_to_tick, yahoo_symbol


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

    def to_dict(self) -> dict:
        return asdict(self)


class MarketDataError(Exception):
    pass


class YahooProvider:
    name = "yahoo"
    _URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

    def __init__(self, timeout: float = 10.0, cache_ttl: float = 30.0):
        self._client = httpx.Client(timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        self._cache: dict[tuple, tuple[float, dict]] = {}
        self._ttl = cache_ttl

    def _chart(self, symbol: str, range_: str, interval: str) -> dict:
        key = (symbol, range_, interval)
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < self._ttl:
            return hit[1]
        try:
            resp = self._client.get(
                self._URL.format(symbol=yahoo_symbol(symbol)),
                params={"range": range_, "interval": interval},
            )
            resp.raise_for_status()
            result = resp.json()["chart"]["result"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise MarketDataError(f"Gagal mengambil data {symbol}: {exc}") from exc
        if not result:
            raise MarketDataError(f"Simbol {symbol} tidak ditemukan")
        self._cache[key] = (time.time(), result[0])
        return result[0]

    def candles(self, symbol: str, range_: str = "6mo", interval: str = "1d") -> list[Candle]:
        data = self._chart(symbol, range_, interval)
        q = data["indicators"]["quote"][0]
        out = []
        for i, ts in enumerate(data.get("timestamp", [])):
            o, h, l, c, v = (q[k][i] for k in ("open", "high", "low", "close", "volume"))
            if None in (o, h, l, c):
                continue
            out.append(Candle(ts, o, h, l, c, int(v or 0)))
        return out

    def quote(self, symbol: str) -> Quote:
        meta = self._chart(symbol, "5d", "1d")["meta"]
        price = meta["regularMarketPrice"]
        prev = meta.get("chartPreviousClose") or meta.get("previousClose") or price
        change = price - prev
        return Quote(
            normalize_symbol(symbol), price, prev, change,
            (change / prev * 100) if prev else 0.0, meta.get("currency", "IDR"), self.name,
        )


class DemoProvider:
    """Random walk deterministik per simbol, agar aplikasi bisa dipakai offline."""

    name = "demo"

    def candles(self, symbol: str, range_: str = "6mo", interval: str = "1d") -> list[Candle]:
        sym = normalize_symbol(symbol)
        seed = int(hashlib.sha256(sym.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        days = {"1mo": 22, "3mo": 66, "6mo": 130, "1y": 250, "2y": 500, "5y": 1250, "10y": 2500}.get(range_, 130)
        price = rng.choice([150, 450, 1200, 3500, 8000])
        today = int(time.time()) // 86400 * 86400
        out = []
        for i in range(days):
            drift = 0.0004 + 0.002 * math.sin(i / 17 + seed % 7)
            close = max(50.0, price * (1 + drift + rng.gauss(0, 0.018)))
            high = max(price, close) * (1 + abs(rng.gauss(0, 0.006)))
            low = min(price, close) * (1 - abs(rng.gauss(0, 0.006)))
            out.append(Candle(
                today - (days - i) * 86400,
                round_to_tick(price), round_to_tick(high), round_to_tick(low),
                round_to_tick(close), rng.randint(1_000_000, 50_000_000),
            ))
            price = close
        return out

    def quote(self, symbol: str) -> Quote:
        candles = self.candles(symbol, "1mo")
        price, prev = candles[-1].close, candles[-2].close
        change = price - prev
        return Quote(normalize_symbol(symbol), price, prev, change, change / prev * 100, "IDR", self.name)


def get_provider(name: str):
    return DemoProvider() if name == "demo" else YahooProvider()
