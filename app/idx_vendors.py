"""Penyedia data saham IDX berbayar dengan data (hampir) real-time: Invezgo dan GoAPI.

Keduanya diakses dengan API key Anda sendiri lewat API resmi mereka. Endpoint & format mengikuti
SDK resmi masing-masing (github.com/Invezgo/invezgo-python-sdk, github.com/goapi-io/php-sdk).
Seberapa real-time datanya dan izin redistribusinya bergantung pada paket langganan Anda —
pastikan dengan penyedia.

Data yang tidak disediakan penyedia (mis. candle intraday di GoAPI) atau saat penyedia gangguan
otomatis diambil dari provider cadangan (Yahoo) lewat `FallbackProvider`; sumber yang dipakai
selalu terlihat di field `source` quote.
"""
import logging
import threading
import time
from datetime import date, datetime, timedelta

import httpx

from .autotrader import WIB
from .idx_rules import INDICES, is_index, normalize_symbol
from .market_data import INTRADAY_INTERVALS, Candle, MarketDataError, Quote

log = logging.getLogger(__name__)

RANGE_DAYS = {"1d": 1, "5d": 7, "1mo": 31, "3mo": 92, "6mo": 183, "1y": 366, "2y": 731, "5y": 1827, "10y": 3653}


def parse_time(value) -> float | None:
    """Waktu dari penyedia -> epoch. Menerima epoch (detik/ms), ISO 8601, 'YYYY-MM-DD HH:MM[:SS]'
    (dianggap WIB bila tanpa zona waktu), atau 'HH:MM[:SS]' (hari ini, WIB). Hanya tanggal -> None."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value) / 1000 if value > 1e11 else float(value)
    s = str(value).strip()
    if s.isdigit():
        return parse_time(int(s))
    if len(s) == 10 and s[4] == "-":
        return None  # hanya tanggal: jam transaksi tidak diketahui
    try:
        if len(s) <= 8 and ":" in s:  # jam saja
            t = datetime.strptime(s, "%H:%M:%S" if s.count(":") == 2 else "%H:%M").time()
            dt = datetime.combine(datetime.now(WIB).date(), t, WIB)
        else:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T", 1))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=WIB)
        return dt.timestamp()
    except ValueError:
        return None


def parse_day(value) -> float | None:
    """Tanggal candle harian -> epoch jam 09:00 WIB hari itu (sama seperti candle harian Yahoo)."""
    t = parse_time(value)
    if t is not None:
        d = datetime.fromtimestamp(t, WIB).date()
    else:
        try:
            d = date.fromisoformat(str(value)[:10])
        except ValueError:
            return None
    return datetime(d.year, d.month, d.day, 9, 0, tzinfo=WIB).timestamp()


def _num(x) -> float | None:
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


class VendorAPI:
    """HTTP JSON dengan cache sederhana & pesan galat yang jelas."""

    def __init__(self, name: str, base_url: str, headers: dict, client: httpx.Client | None = None, timeout=10.0):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.headers = headers
        self._http = client
        self._timeout = timeout
        self._cache: dict[tuple, tuple[float, object]] = {}
        self._lock = threading.Lock()

    @property
    def client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=self._timeout)
        return self._http

    def get(self, path: str, params: dict | None = None, ttl: float = 0):
        key = (path, tuple(sorted((params or {}).items())))
        with self._lock:
            hit = self._cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        try:
            resp = self.client.get(self.base_url + path, params=params or {}, headers=self.headers)
        except httpx.HTTPError as exc:
            raise MarketDataError(f"Gagal menghubungi {self.name}: {type(exc).__name__}") from exc
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400:
            detail = body.get("message") if isinstance(body, dict) else None
            reason = {401: "API key salah atau belum diisi", 403: "akses ditolak untuk API key ini",
                      402: "endpoint ini butuh paket berlangganan yang lebih tinggi",
                      404: "data tidak ditemukan", 429: "batas permintaan (rate limit/kuota) tercapai"}.get(resp.status_code)
            raise MarketDataError(f"{self.name}: {reason or f'HTTP {resp.status_code}'}" + (f" ({detail})" if detail else ""))
        with self._lock:
            self._cache[key] = (time.time(), body)
        return body


# ---- Invezgo -------------------------------------------------------------------
class InvezgoProvider:
    """https://invezgo.com — data intraday & order book BEI. Auth: header `Authorization: Bearer <key>`."""

    name = "invezgo"

    def __init__(self, api_key: str, base_url: str = "https://api.invezgo.com", client=None,
                 quote_ttl: float = 3.0, intraday_ttl: float = 10.0, daily_ttl: float = 300.0):
        if not api_key:
            raise MarketDataError("INVEZGO_API_KEY belum diisi di .env")
        self.api = VendorAPI("Invezgo", base_url, {"Authorization": f"Bearer {api_key}"}, client)
        self.ttl = {"quote": quote_ttl, "intraday": intraday_ttl, "daily": daily_ttl}

    @staticmethod
    def _code(symbol: str) -> str:
        sym = normalize_symbol(symbol)
        return INDICES[sym][1] if sym in INDICES else sym  # IHSG -> COMPOSITE

    @staticmethod
    def _unwrap(body):
        """Respons berupa objek/daftar langsung; beberapa versi API membungkusnya dalam {"data": ...}."""
        if isinstance(body, dict) and "data" in body and not {"close", "code"} & set(body):
            return body["data"]
        return body

    def _last_trade_time(self, sym: str) -> float | None:
        """Waktu bar intraday terakhir = perkiraan waktu transaksi terakhir (untuk umur data di UI)."""
        if is_index(sym):
            return None
        try:
            rows = self._unwrap(self.api.get(f"/analysis/intraday/{self._code(sym)}", {"market": "RG"},
                                             self.ttl["intraday"]))
        except MarketDataError:
            return None
        times = [parse_time(r.get("date")) for r in rows or [] if isinstance(r, dict)]
        times = [t for t in times if t]
        return max(times) if times else None

    def quote(self, symbol: str) -> Quote:
        sym = normalize_symbol(symbol)
        path = f"/analysis/intraday-index/{self._code(sym)}" if is_index(sym) else f"/analysis/intraday-data/{sym}"
        d = self._unwrap(self.api.get(path, {"market": "RG"}, self.ttl["quote"]))
        if isinstance(d, list):
            d = d[0] if d else {}
        price, prev = _num(d.get("close")), _num(d.get("prev"))
        if not price:
            raise MarketDataError(f"Invezgo: harga {sym} tidak tersedia")
        prev = prev or price
        return Quote(sym, price, prev, price - prev, (price / prev - 1) * 100 if prev else 0.0, "IDR", self.name,
                     self._last_trade_time(sym))

    def candles(self, symbol: str, range_: str = "6mo", interval: str = "1d") -> list[Candle]:
        sym = normalize_symbol(symbol)
        code = self._code(sym)
        today = datetime.now(WIB).date()
        start = today - timedelta(days=RANGE_DAYS.get(range_, 183))
        params = {"from": start.isoformat(), "to": today.isoformat()}
        if interval in INTRADAY_INTERVALS:
            rows = self._unwrap(self.api.get(f"/analysis/chart/multi-time/{code}", {**params, "timeframe": interval},
                                             self.ttl["intraday"]))
            parse = parse_time
        else:
            kind = "index" if is_index(sym) else "stock"
            rows = self._unwrap(self.api.get(f"/analysis/chart/{kind}/{code}", params, self.ttl["daily"]))
            parse = parse_day
        out = []
        for r in rows or []:
            t, o, h, l, c = parse(r.get("date")), *(_num(r.get(k)) for k in ("open", "high", "low", "close"))
            if t is None or None in (o, h, l, c):
                continue
            out.append(Candle(int(t), o, h, l, c, int(_num(r.get("volume")) or 0)))
        out.sort(key=lambda c: c.time)
        if not out:
            raise MarketDataError(f"Invezgo: data candle {sym} kosong")
        return out


# ---- GoAPI ---------------------------------------------------------------------
class GoAPIProvider:
    """https://goapi.io — Stock Market IDX. Auth: header `X-API-KEY`. Tidak ada candle intraday."""

    name = "goapi"
    BATCH = 50
    WANTED_SECONDS = 120  # simbol yang diminta 2 menit terakhir ikut diambil dalam satu permintaan

    def __init__(self, api_key: str, base_url: str = "https://api.goapi.io", client=None,
                 quote_ttl: float = 5.0, daily_ttl: float = 300.0, clock=time.time):
        if not api_key:
            raise MarketDataError("GOAPI_API_KEY belum diisi di .env")
        self.api = VendorAPI("GoAPI", base_url.rstrip("/") + "/stock/idx", {"X-API-KEY": api_key}, client)
        self.quote_ttl, self.daily_ttl, self.clock = quote_ttl, daily_ttl, clock
        self._wanted: dict[str, float] = {}
        self._prices: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _results(body) -> list:
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, dict):
            return data.get("results") or []
        return data if isinstance(data, list) else []

    def _price_row(self, sym: str) -> dict:
        """Harga terkini; satu permintaan /prices untuk semua simbol yang sedang dipantau (hemat kuota)."""
        now = self.clock()
        with self._lock:
            self._wanted[sym] = now
            hit = self._prices.get(sym)
            if hit and now - hit[0] < self.quote_ttl:
                return hit[1]
            self._wanted = {s: t for s, t in self._wanted.items() if now - t < self.WANTED_SECONDS}
            batch = [sym] + [s for s in sorted(self._wanted) if s != sym][: self.BATCH - 1]
        rows = self._results(self.api.get("/prices", {"symbols": ",".join(batch)}))
        with self._lock:
            for r in rows:
                if isinstance(r, dict) and r.get("symbol"):
                    self._prices[normalize_symbol(r["symbol"])] = (now, r)
            hit = self._prices.get(sym)
        if not hit or hit[0] != now:
            raise MarketDataError(f"GoAPI: harga {sym} tidak tersedia")
        return hit[1]

    def _prev_close(self, sym: str, row: dict, price: float) -> float:
        change = _num(row.get("change"))
        if change is not None:
            return price - change
        # Tanpa field perubahan: penutupan hari perdagangan sebelumnya dari data historis.
        today = parse_day(row.get("date")) or self.clock()
        days = [c for c in self.candles(sym, "1mo", "1d") if c.time < today - 3600]
        return days[-1].close if days else price

    def quote(self, symbol: str) -> Quote:
        sym = normalize_symbol(symbol)
        if is_index(sym):
            raise MarketDataError("GoAPI: harga indeks tidak tersedia lewat endpoint harga saham")
        row = self._price_row(sym)
        price = _num(row.get("close"))
        if not price:
            raise MarketDataError(f"GoAPI: harga {sym} tidak tersedia")
        prev = self._prev_close(sym, row, price) or price
        when = parse_time(row.get("last_update") or row.get("updated_at") or row.get("time") or row.get("date"))
        return Quote(sym, price, prev, price - prev, (price / prev - 1) * 100 if prev else 0.0, "IDR", self.name, when)

    def candles(self, symbol: str, range_: str = "6mo", interval: str = "1d") -> list[Candle]:
        sym = normalize_symbol(symbol)
        if interval in INTRADAY_INTERVALS:
            raise MarketDataError("GoAPI: candle intraday tidak tersedia")
        if is_index(sym):
            raise MarketDataError("GoAPI: data historis indeks tidak tersedia")
        end = datetime.fromtimestamp(self.clock(), WIB).date()
        start = end - timedelta(days=RANGE_DAYS.get(range_, 183))
        rows = self._results(self.api.get(f"/{sym}/historical", {"from": start.isoformat(), "to": end.isoformat()},
                                          self.daily_ttl))
        out = []
        for r in rows:
            t = parse_day(r.get("date"))
            o, h, l, c = (_num(r.get(k)) for k in ("open", "high", "low", "close"))
            if t is None or None in (o, h, l, c):
                continue
            out.append(Candle(int(t), o, h, l, c, int(_num(r.get("volume")) or 0)))
        out.sort(key=lambda c: c.time)  # GoAPI mengurutkan terbaru dulu
        if not out:
            raise MarketDataError(f"GoAPI: data historis {sym} kosong")
        return out


# ---- cadangan ------------------------------------------------------------------
class FallbackProvider:
    """Pakai penyedia utama; bila gagal (data tidak tersedia / gangguan) pakai cadangan."""

    def __init__(self, primary, fallback):
        self.primary, self.fallback = primary, fallback
        self.name = primary.name
        self.last_error: str | None = None

    def _try(self, method: str, *args):
        try:
            result = getattr(self.primary, method)(*args)
            self.last_error = None
            return result
        except MarketDataError as exc:
            if self.fallback is None:
                raise
            if str(exc) != self.last_error:
                log.warning("%s gagal, memakai %s: %s", self.primary.name, self.fallback.name, exc)
            self.last_error = str(exc)
            return getattr(self.fallback, method)(*args)

    def quote(self, symbol: str) -> Quote:
        return self._try("quote", symbol)

    def candles(self, symbol: str, range_: str = "6mo", interval: str = "1d") -> list[Candle]:
        return self._try("candles", symbol, range_, interval)


def build_provider(settings, http: httpx.Client | None = None):
    """Provider dari MARKET_DATA_PROVIDER: yahoo / demo / invezgo / goapi."""
    from .market_data import get_provider
    name = settings.market_data_provider.lower()
    if name == "invezgo":
        primary = InvezgoProvider(settings.invezgo_api_key, settings.invezgo_base_url, http)
    elif name == "goapi":
        primary = GoAPIProvider(settings.goapi_api_key, settings.goapi_base_url, http)
    else:
        return get_provider(name)
    fallback = None if settings.vendor_fallback == "none" else get_provider(settings.vendor_fallback)
    return FallbackProvider(primary, fallback)
