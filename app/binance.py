"""Klien API resmi Binance Spot (https://developers.binance.com/docs/binance-spot-api-docs).

- Data pasar (harga, candle, aturan simbol) memakai endpoint publik, tanpa API key.
- Order & saldo memakai endpoint bertanda tangan (HMAC-SHA256) dengan API key Anda sendiri.
  Untuk mencoba tanpa uang sungguhan, pakai Spot Testnet (https://testnet.binance.vision):
  saldo & order di sana palsu, tetapi alurnya sama persis dengan akun asli.

Aplikasi tidak pernah menyimpan password Binance; cukup API key yang Anda buat sendiri dengan izin
"Enable Spot & Margin Trading" (jangan aktifkan izin withdraw).
"""
import hashlib
import hmac
import re
import threading
import time
from dataclasses import dataclass
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from urllib.parse import urlencode

import httpx

from .market_data import Candle, MarketDataError, Quote

# Aset kutipan yang dikenali untuk memisahkan pasangan (BTCUSDT -> BTC / USDT). Urutan: yang panjang dulu.
QUOTE_ASSETS = ("FDUSD", "USDT", "USDC", "TUSD", "BUSD", "BTC", "ETH", "BNB", "TRY", "EUR", "BRL", "IDR")
KLINE_INTERVALS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


class BinanceError(Exception):
    def __init__(self, message: str, code: int | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


def normalize_pair(symbol: str) -> str:
    """'btc/usdt', 'BINANCE:BTCUSDT', 'BTC-USDT' -> 'BTCUSDT'."""
    s = symbol.strip().upper()
    if ":" in s:
        s = s.split(":", 1)[1]
    return re.sub(r"[^A-Z0-9]", "", s)


def split_pair(symbol: str) -> tuple[str, str] | None:
    s = normalize_pair(symbol)
    for q in QUOTE_ASSETS:
        if s.endswith(q) and len(s) > len(q) + 1:
            return s[: -len(q)], q
    return None


def is_crypto_pair(symbol: str) -> bool:
    return split_pair(symbol) is not None


def _dec(x) -> Decimal:
    return Decimal(str(x))


def fmt_decimal(d: Decimal) -> str:
    """Tanpa notasi ilmiah & nol berlebih: Decimal('0.00100000') -> '0.001'."""
    s = format(d.normalize(), "f")
    return s


@dataclass
class SymbolRules:
    """Filter perdagangan Binance untuk satu pasangan (LOT_SIZE, PRICE_FILTER, NOTIONAL)."""
    symbol: str
    base: str
    quote: str
    status: str
    step: Decimal
    min_qty: Decimal
    tick: Decimal
    min_notional: float

    @classmethod
    def from_exchange_info(cls, info: dict) -> "SymbolRules":
        filters = {f["filterType"]: f for f in info.get("filters", [])}
        lot = filters.get("LOT_SIZE", {})
        price = filters.get("PRICE_FILTER", {})
        notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
        return cls(info["symbol"], info["baseAsset"], info["quoteAsset"], info.get("status", "TRADING"),
                   _dec(lot.get("stepSize", "0")), _dec(lot.get("minQty", "0")), _dec(price.get("tickSize", "0")),
                   float(notional.get("minNotional", 0) or 0))

    def floor_qty(self, qty: float) -> Decimal:
        d = _dec(qty)
        if self.step > 0:
            d = (d / self.step).to_integral_value(ROUND_FLOOR) * self.step
        return d

    def round_price(self, price: float) -> Decimal:
        d = _dec(price)
        if self.tick > 0:
            d = (d / self.tick).to_integral_value(ROUND_HALF_UP) * self.tick
        return d

    def check(self, qty: Decimal, price: float) -> str | None:
        """Alasan order ditolak menurut filter, atau None bila lolos."""
        if self.status != "TRADING":
            return f"{self.symbol} sedang tidak diperdagangkan ({self.status})"
        if qty <= 0 or qty < self.min_qty:
            return f"Jumlah minimal {fmt_decimal(self.min_qty)} {self.base} (kelipatan {fmt_decimal(self.step)})"
        if self.min_notional and float(qty) * price < self.min_notional:
            return f"Nilai order minimal {self.min_notional:g} {self.quote}"
        return None

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "base": self.base, "quote": self.quote, "status": self.status,
                "step_size": fmt_decimal(self.step), "min_qty": fmt_decimal(self.min_qty),
                "tick_size": fmt_decimal(self.tick), "min_notional": self.min_notional}


class BinanceAPI:
    RECV_WINDOW = 5000

    def __init__(self, base_url: str, api_key: str = "", api_secret: str = "", client: httpx.Client | None = None,
                 clock=time.time, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.clock = clock
        self._http = client
        self._timeout = timeout
        self._time_offset_ms = 0  # selisih jam server Binance - jam lokal

    @property
    def _client(self) -> httpx.Client:
        if self._http is None:  # dibuat saat pertama dipakai (aplikasi tanpa crypto tidak membuka koneksi)
            self._http = httpx.Client(timeout=self._timeout)
        return self._http

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def _handle(self, resp: httpx.Response):
        try:
            body = resp.json()
        except ValueError:
            body = None
        if resp.status_code >= 400:
            if isinstance(body, dict) and "msg" in body:
                raise BinanceError(f"Binance: {body['msg']}", body.get("code"), resp.status_code)
            if resp.status_code in (403, 451):
                raise BinanceError("Binance menolak akses dari lokasi/jaringan ini (HTTP "
                                   f"{resp.status_code}); coba BINANCE_DATA_URL lain", None, resp.status_code)
            if resp.status_code == 429 or resp.status_code == 418:
                raise BinanceError("Binance membatasi permintaan (rate limit), coba lagi sebentar", None, resp.status_code)
            raise BinanceError(f"Binance HTTP {resp.status_code}", None, resp.status_code)
        return body

    def public(self, path: str, params: dict | None = None):
        try:
            resp = self._client.get(self.base_url + path, params=params or {})
        except httpx.HTTPError as exc:
            raise BinanceError(f"Gagal menghubungi Binance: {type(exc).__name__}") from exc
        return self._handle(resp)

    def sync_time(self) -> None:
        server = self.public("/api/v3/time")["serverTime"]
        self._time_offset_ms = int(server - self.clock() * 1000)

    def signed(self, method: str, path: str, params: dict | None = None, _retry: bool = True):
        if not self.configured:
            raise BinanceError("API key Binance belum diatur")
        query = {k: v for k, v in (params or {}).items() if v is not None}
        query["recvWindow"] = self.RECV_WINDOW
        query["timestamp"] = int(self.clock() * 1000) + self._time_offset_ms
        payload = urlencode(query)
        signature = hmac.new(self.api_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        url = f"{self.base_url}{path}?{payload}&signature={signature}"
        try:
            resp = self._client.request(method, url, headers={"X-MBX-APIKEY": self.api_key})
        except httpx.HTTPError as exc:
            raise BinanceError(f"Gagal menghubungi Binance: {type(exc).__name__}") from exc
        try:
            return self._handle(resp)
        except BinanceError as exc:
            if exc.code == -1021 and _retry:  # jam komputer tidak sinkron dengan server Binance
                self.sync_time()
                return self.signed(method, path, params, _retry=False)
            raise


class BinanceProvider:
    """Penyedia data pasar crypto dari endpoint publik Binance (real-time, tanpa API key)."""

    name = "binance"

    def __init__(self, api: BinanceAPI, quote_ttl: float = 3.0, kline_ttl: float = 10.0, rules_ttl: float = 3600.0):
        self.api = api
        self._ttl = {"quote": quote_ttl, "klines": kline_ttl, "rules": rules_ttl}
        self._cache: dict[tuple, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def _cached(self, kind: str, key: tuple, fetch):
        with self._lock:
            hit = self._cache.get((kind, *key))
        if hit and time.time() - hit[0] < self._ttl[kind]:
            return hit[1]
        try:
            value = fetch()
        except BinanceError as exc:
            if exc.code == -1121:
                raise MarketDataError(f"Pasangan {key[0]} tidak ada di Binance") from exc
            raise MarketDataError(str(exc)) from exc
        with self._lock:
            self._cache[(kind, *key)] = (time.time(), value)
        return value

    def rules(self, symbol: str) -> SymbolRules:
        sym = normalize_pair(symbol)

        def fetch():
            data = self.api.public("/api/v3/exchangeInfo", {"symbol": sym})
            return SymbolRules.from_exchange_info(data["symbols"][0])
        return self._cached("rules", (sym,), fetch)

    def quote(self, symbol: str) -> Quote:
        sym = normalize_pair(symbol)
        t = self._cached("quote", (sym,), lambda: self.api.public("/api/v3/ticker/24hr", {"symbol": sym}))
        price, open_ = float(t["lastPrice"]), float(t["openPrice"])
        pair = split_pair(sym)
        # Crypto diperdagangkan 24 jam: "perubahan hari ini" = perubahan 24 jam terakhir.
        return Quote(sym, price, open_, price - open_, float(t["priceChangePercent"]),
                     pair[1] if pair else "", self.name, t["closeTime"] / 1000)

    def all_prices(self) -> dict[str, float]:
        """Harga terakhir semua pasangan dalam satu permintaan (untuk menilai saldo akun)."""
        rows = self._cached("quote", ("*",), lambda: self.api.public("/api/v3/ticker/price"))
        return {r["symbol"]: float(r["price"]) for r in rows}

    def candles(self, symbol: str, range_: str | None = None, interval: str = "1d", limit: int = 300) -> list[Candle]:
        sym = normalize_pair(symbol)
        if interval not in KLINE_INTERVALS:
            raise MarketDataError(f"Interval harus salah satu dari {', '.join(KLINE_INTERVALS)}")
        limit = max(10, min(int(limit), 1000))
        rows = self._cached("klines", (sym, interval, limit),
                            lambda: self.api.public("/api/v3/klines", {"symbol": sym, "interval": interval, "limit": limit}))
        return [Candle(int(r[0] // 1000), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
                for r in rows]
