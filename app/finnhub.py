"""Data saham Amerika dari Finnhub (https://finnhub.io/docs/api).

- Harga terakhir: endpoint `/quote` (paket gratis: real-time untuk saham AS, batas 60 permintaan/menit).
- Candle: endpoint `/stock/candle`. Sejak 2024 endpoint ini hanya untuk paket berbayar; bila API key Anda
  ditolak (HTTP 403), candle diambil dari endpoint chart Yahoo Finance (tidak resmi, tanpa API key).
- Jam bursa: `/stock/market-status` (termasuk hari libur).

Finnhub hanya menyediakan data, bukan broker, jadi order saham AS memakai akun simulasi lokal
(`us_paper.py`) atau Alpaca bila tersedia.
"""
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

from .alpaca import INTERVALS, normalize_us
from .market_data import Candle, MarketDataError, Quote

RESOLUTION = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "1d": "D"}
YAHOO_INTERVAL = {"1m": ("1m", "5d"), "5m": ("5m", "1mo"), "15m": ("15m", "1mo"), "1h": ("60m", "3mo"),
                  "1d": ("1d", "2y")}
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


class FinnhubError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class FinnhubAPI:
    def __init__(self, base_url: str, key: str = "", client: httpx.Client | None = None, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.key = key
        self._http = client
        self._timeout = timeout

    @property
    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=self._timeout, headers={"User-Agent": "Mozilla/5.0"})
        return self._http

    @property
    def configured(self) -> bool:
        return bool(self.key)

    def get(self, path: str, params: dict | None = None):
        if not self.configured:
            raise FinnhubError("API key Finnhub belum diatur (FINNHUB_API_KEY)")
        try:
            resp = self._client.get(self.base_url + path, params=params or {}, headers={"X-Finnhub-Token": self.key})
        except httpx.HTTPError as exc:
            raise FinnhubError(f"Gagal menghubungi Finnhub: {type(exc).__name__}") from exc
        try:
            body = resp.json()
        except ValueError:
            body = None
        if resp.status_code >= 400:
            msg = body.get("error") if isinstance(body, dict) else None
            if resp.status_code == 401:
                msg = "API key Finnhub ditolak"
            elif resp.status_code == 429:
                msg = "Finnhub membatasi permintaan (60/menit di paket gratis), coba lagi sebentar"
            raise FinnhubError(f"Finnhub: {msg or 'HTTP ' + str(resp.status_code)}", resp.status_code)
        return body


class FinnhubProvider:
    name = "finnhub"
    currency = "USD"

    def __init__(self, api: FinnhubAPI, quote_ttl: float = 5.0, bars_ttl: float = 20.0, status_ttl: float = 60.0,
                 yahoo_client: httpx.Client | None = None):
        self.api = api
        self._ttl = {"quote": quote_ttl, "bars": bars_ttl, "status": status_ttl}
        self._cache: dict[tuple, tuple[float, object]] = {}
        self._lock = threading.Lock()
        self._yahoo = yahoo_client

    def _cached(self, kind: str, key: tuple, fetch):
        with self._lock:
            hit = self._cache.get((kind, *key))
        if hit and time.time() - hit[0] < self._ttl[kind]:
            return hit[1]
        value = fetch()
        with self._lock:
            self._cache[(kind, *key)] = (time.time(), value)
        return value

    def _guard(self, fn):
        try:
            return fn()
        except FinnhubError as exc:
            raise MarketDataError(str(exc)) from exc

    def quote(self, symbol: str) -> Quote:
        sym = normalize_us(symbol)
        if not sym:
            raise MarketDataError(f"'{symbol}' bukan kode saham AS (contoh: AAPL, MSFT, BRK.B)")
        q = self._guard(lambda: self._cached("quote", (sym,), lambda: self.api.get("/quote", {"symbol": sym})))
        if not q or not q.get("c"):  # kode tidak dikenal: semua nilai 0
            raise MarketDataError(f"Tidak ada data harga untuk {sym} di Finnhub")
        price, prev = float(q["c"]), float(q.get("pc") or q["c"])
        change = price - prev
        return Quote(sym, price, prev, change, round(change / prev * 100, 2) if prev else 0.0, "USD",
                     "finnhub", float(q["t"]) if q.get("t") else None)

    def candles(self, symbol: str, range_: str | None = None, interval: str = "1d", limit: int = 300) -> list[Candle]:
        sym = normalize_us(symbol)
        if not sym:
            raise MarketDataError(f"'{symbol}' bukan kode saham AS")
        if interval not in INTERVALS:
            raise MarketDataError(f"Interval harus salah satu dari {', '.join(INTERVALS)}")
        limit = max(10, min(int(limit), 1000))
        try:
            rows = self._cached("bars", (sym, interval, limit), lambda: self._finnhub_bars(sym, interval, limit))
        except FinnhubError as exc:
            if exc.status != 403:
                raise MarketDataError(str(exc)) from exc
            try:  # paket gratis: endpoint candle terkunci
                rows = self._cached("bars", ("yahoo", sym, interval, limit), lambda: self._yahoo_bars(sym, interval))
            except MarketDataError as ye:
                raise MarketDataError("Candle Finnhub butuh paket berbayar dan cadangan Yahoo Finance gagal: "
                                      f"{ye}") from ye
        if not rows:
            raise MarketDataError(f"Tidak ada candle {interval} untuk {sym}")
        return rows[-limit:]

    def _finnhub_bars(self, sym: str, interval: str, limit: int) -> list[Candle]:
        seconds = INTERVALS[interval][1]
        days = limit * 1.6 + 10 if interval == "1d" else max(7.0, limit * seconds / 23400 * 2.2 + 5)
        now = datetime.now(timezone.utc)
        d = self.api.get("/stock/candle", {"symbol": sym, "resolution": RESOLUTION[interval],
                                          "from": int((now - timedelta(days=days)).timestamp()),
                                          "to": int(now.timestamp())})
        if not d or d.get("s") != "ok":
            return []
        return [Candle(int(t), float(o), float(h), float(l), float(c), float(v))
                for t, o, h, l, c, v in zip(d["t"], d["o"], d["h"], d["l"], d["c"], d["v"])]

    def _yahoo_bars(self, sym: str, interval: str) -> list[Candle]:
        step, range_ = YAHOO_INTERVAL[interval]
        client = self._yahoo or self.api._client
        try:
            resp = client.get(YAHOO_URL.format(symbol=sym.replace(".", "-")), params={"range": range_, "interval": step})
            resp.raise_for_status()
            result = resp.json()["chart"]["result"][0]
            q = result["indicators"]["quote"][0]
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise MarketDataError(f"Yahoo Finance: {type(exc).__name__}") from exc
        out = []
        for i, ts in enumerate(result.get("timestamp") or []):
            o, h, l, c = (q[k][i] for k in ("open", "high", "low", "close"))
            if None not in (o, h, l, c):
                out.append(Candle(ts, o, h, l, c, float(q["volume"][i] or 0)))
        return out

    def market_clock(self) -> dict | None:
        """Jam bursa AS dari Finnhub (termasuk hari libur); None bila tidak tersedia."""
        if not self.api.configured:
            return None
        try:
            s = self._cached("status", (), lambda: self.api.get("/stock/market-status", {"exchange": "US"}))
        except FinnhubError:
            return None
        return {"is_open": bool(s.get("isOpen")), "next_open": None, "next_close": None}
