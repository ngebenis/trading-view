"""Saham Amerika lewat Alpaca Markets (https://docs.alpaca.markets).

- Data pasar: Market Data API v2 (harga terakhir, candle). Feed gratis `iex` real-time tetapi hanya memuat
  transaksi di bursa IEX; feed `sip` (berbayar) mencakup seluruh bursa AS.
- Order & akun: Trading API v2. Akun *paper* Alpaca (uang simulasi) dan akun live (uang sungguhan) dipisah,
  masing-masing dengan API key milik Anda sendiri. Order tersimpan di Alpaca, bukan di aplikasi ini.

Berbeda dengan saham IDX: harga dalam USD, jumlah boleh pecahan (fractional shares), tanpa lot / ARA / ARB,
dan jam bursa mengikuti waktu New York (09:30–16:00 ET).
"""
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from .brokers.base import BrokerError, BrokerNotAvailable, OrderStatus, OrderType, Side
from .market_data import Candle, MarketDataError, Quote

NY = ZoneInfo("America/New_York")
INTERVALS = {"1m": ("1Min", 60), "5m": ("5Min", 300), "15m": ("15Min", 900), "1h": ("1Hour", 3600),
             "1d": ("1Day", 86400)}
CLIENT_PREFIX = "tv-"  # awalan client_order_id untuk order yang dibuat aplikasi ini
SYMBOL_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")


class AlpacaError(Exception):
    def __init__(self, message: str, status: int | None = None, code: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def normalize_us(symbol: str) -> str:
    """'nasdaq:aapl', 'BRK-B' -> 'AAPL', 'BRK.B'. Kembalikan '' bila bukan kode saham AS."""
    s = symbol.strip().upper()
    if ":" in s:
        s = s.split(":", 1)[1]
    s = s.replace("-", ".")
    return s if SYMBOL_RE.match(s) else ""


def parse_ts(value) -> float | None:
    """'2026-10-06T13:30:00.123456789Z' -> epoch detik (pecahan nanodetik dipotong)."""
    if not value:
        return None
    s = re.sub(r"(\.\d{6})\d+", r"\1", str(value).replace("Z", "+00:00"))
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def us_market_open(now: float | None = None) -> bool:
    """Perkiraan lokal: Senin–Jumat 09:30–16:00 ET. Hari libur bursa tidak diperhitungkan (lihat /v2/clock)."""
    t = datetime.fromtimestamp(now if now is not None else time.time(), NY)
    return t.weekday() < 5 and dtime(9, 30) <= t.time() < dtime(16, 0)


class AlpacaAPI:
    def __init__(self, base_url: str, key: str = "", secret: str = "", client: httpx.Client | None = None,
                 timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.key, self.secret = key, secret
        self._http = client
        self._timeout = timeout

    @property
    def _client(self) -> httpx.Client:
        if self._http is None:  # dibuat saat pertama dipakai
            self._http = httpx.Client(timeout=self._timeout)
        return self._http

    @property
    def configured(self) -> bool:
        return bool(self.key and self.secret)

    def request(self, method: str, path: str, params: dict | None = None, json: dict | None = None):
        if not self.configured:
            raise AlpacaError("API key Alpaca belum diatur")
        headers = {"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret}
        query = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            resp = self._client.request(method, self.base_url + path, params=query, json=json, headers=headers)
        except httpx.HTTPError as exc:
            raise AlpacaError(f"Gagal menghubungi Alpaca: {type(exc).__name__}") from exc
        if resp.status_code == 204:
            return None
        try:
            body = resp.json()
        except ValueError:
            body = None
        if resp.status_code >= 400:
            msg = body.get("message") if isinstance(body, dict) else None
            code = body.get("code") if isinstance(body, dict) else None
            if resp.status_code in (401, 403) and not msg:
                msg = "API key ditolak"
            if resp.status_code == 429:
                msg = "Alpaca membatasi permintaan (rate limit), coba lagi sebentar"
            raise AlpacaError(f"Alpaca: {msg or 'HTTP ' + str(resp.status_code)}", resp.status_code, code)
        return body


class AlpacaProvider:
    """Penyedia data saham AS dari Alpaca Market Data API (cache pendek agar hemat kuota)."""

    name = "alpaca"
    currency = "USD"

    def __init__(self, api: AlpacaAPI, feed: str = "iex", quote_ttl: float = 3.0, bars_ttl: float = 10.0):
        self.api, self.feed = api, feed
        self._ttl = {"quote": quote_ttl, "bars": bars_ttl}
        self._cache: dict[tuple, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def _cached(self, kind: str, key: tuple, fetch):
        with self._lock:
            hit = self._cache.get((kind, *key))
        if hit and time.time() - hit[0] < self._ttl[kind]:
            return hit[1]
        try:
            value = fetch()
        except AlpacaError as exc:
            raise MarketDataError(str(exc)) from exc
        with self._lock:
            self._cache[(kind, *key)] = (time.time(), value)
        return value

    def quote(self, symbol: str) -> Quote:
        sym = normalize_us(symbol)
        if not sym:
            raise MarketDataError(f"'{symbol}' bukan kode saham AS (contoh: AAPL, MSFT, BRK.B)")
        snap = self._cached("quote", (sym,), lambda: self.api.request(
            "GET", f"/v2/stocks/{sym}/snapshot", {"feed": self.feed}))
        trade = (snap or {}).get("latestTrade") or {}
        if "p" not in trade:
            raise MarketDataError(f"Tidak ada data harga untuk {sym} di Alpaca")
        price = float(trade["p"])
        prev = (snap.get("prevDailyBar") or {}).get("c") or (snap.get("dailyBar") or {}).get("o") or price
        prev = float(prev)
        change = price - prev
        return Quote(sym, price, prev, change, round(change / prev * 100, 2) if prev else 0.0, "USD",
                     f"alpaca-{self.feed}", parse_ts(trade.get("t")))

    def candles(self, symbol: str, range_: str | None = None, interval: str = "1d", limit: int = 300) -> list[Candle]:
        sym = normalize_us(symbol)
        if not sym:
            raise MarketDataError(f"'{symbol}' bukan kode saham AS")
        if interval not in INTERVALS:
            raise MarketDataError(f"Interval harus salah satu dari {', '.join(INTERVALS)}")
        limit = max(10, min(int(limit), 1000))
        frame, seconds = INTERVALS[interval]
        # Jendela waktu cukup lebar untuk melewati malam, akhir pekan & libur bursa.
        days = limit * 1.6 + 10 if interval == "1d" else max(7.0, limit * seconds / 23400 * 2.2 + 5)
        start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = self._cached("bars", (sym, interval, limit), lambda: self.api.request(
            "GET", f"/v2/stocks/{sym}/bars", {"timeframe": frame, "start": start, "limit": limit, "sort": "desc",
                                              "feed": self.feed, "adjustment": "split"}))
        rows = (data or {}).get("bars") or []
        if not rows:
            raise MarketDataError(f"Tidak ada candle {interval} untuk {sym} di Alpaca")
        return [Candle(int(parse_ts(r["t"])), float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]),
                       float(r.get("v", 0))) for r in reversed(rows)]


# ---- order & akun -------------------------------------------------------------
STATUS_MAP = {"filled": OrderStatus.FILLED, "canceled": OrderStatus.CANCELLED, "expired": OrderStatus.CANCELLED,
              "replaced": OrderStatus.CANCELLED, "done_for_day": OrderStatus.CANCELLED,
              "rejected": OrderStatus.REJECTED, "suspended": OrderStatus.REJECTED}  # selain itu: OPEN


@dataclass
class UsOrder:
    symbol: str
    side: Side
    order_type: OrderType = OrderType.MARKET
    quantity: float | None = None     # jumlah saham (boleh pecahan)
    notional: float | None = None     # atau nilai dalam USD (hanya order market)
    limit_price: float | None = None
    time_in_force: str = "day"
    broker: str = "paper"
    id: str = field(default_factory=lambda: CLIENT_PREFIX + uuid.uuid4().hex[:24])  # client_order_id
    status: OrderStatus = OrderStatus.OPEN
    fill_price: float | None = None
    filled_qty: float = 0.0
    created_at: float = field(default_factory=time.time)
    filled_at: float | None = None
    message: str = ""
    source: str = "manual"
    exchange_order_id: str | None = None
    alpaca_status: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("side", "order_type", "status"):
            d[k] = getattr(self, k).value
        return d

    @classmethod
    def from_alpaca(cls, r: dict, broker: str) -> "UsOrder":
        cid = r.get("client_order_id") or ""
        return cls(r["symbol"], Side(r["side"].upper()), OrderType(r.get("type", "market").upper()),
                   float(r["qty"]) if r.get("qty") else None, float(r["notional"]) if r.get("notional") else None,
                   float(r["limit_price"]) if r.get("limit_price") else None, r.get("time_in_force", "day"), broker,
                   cid or r["id"], STATUS_MAP.get(r.get("status", ""), OrderStatus.OPEN),
                   float(r["filled_avg_price"]) if r.get("filled_avg_price") else None,
                   float(r.get("filled_qty") or 0), parse_ts(r.get("submitted_at") or r.get("created_at")) or time.time(),
                   parse_ts(r.get("filled_at")), "", "manual", r.get("id"), r.get("status", ""))

    def apply(self, r: dict) -> None:
        other = UsOrder.from_alpaca(r, self.broker)
        self.status, self.fill_price, self.filled_qty = other.status, other.fill_price, other.filled_qty
        self.filled_at, self.exchange_order_id, self.alpaca_status = other.filled_at, other.exchange_order_id, other.alpaca_status


class AlpacaBroker:
    """Akun Alpaca (paper atau live). Order & posisi dibaca langsung dari Alpaca."""

    def __init__(self, api: AlpacaAPI, live: bool):
        self.api, self.is_live = api, live
        self.name = "live" if live else "paper"
        self.display_name = "Alpaca (akun live)" if live else "Alpaca Paper Trading"
        self.on_fill: list = []
        self._pending: dict[str, UsOrder] = {}  # order limit buatan aplikasi yang belum selesai (id client)
        self._lock = threading.Lock()

    def available(self) -> bool:
        return self.api.configured

    def status(self) -> dict:
        note = ""
        if not self.available():
            note = ("Isi ALPACA_LIVE_API_KEY & ALPACA_LIVE_API_SECRET di .env (juga butuh ENABLE_LIVE_TRADING=true)"
                    if self.is_live else
                    "Isi ALPACA_PAPER_API_KEY & ALPACA_PAPER_API_SECRET di .env (buat di alpaca.markets, akun Paper)")
        return {"name": self.name, "display_name": self.display_name, "is_live": self.is_live,
                "available": self.available(), "note": note}

    def _call(self, method: str, path: str, params=None, json=None):
        if not self.available():
            raise BrokerNotAvailable(self.status()["note"])
        try:
            return self.api.request(method, path, params, json)
        except AlpacaError as exc:
            raise BrokerError(str(exc)) from exc

    def place_order(self, o: UsOrder) -> UsOrder:
        o.broker = self.name
        body = {"symbol": o.symbol, "side": o.side.value.lower(), "type": o.order_type.value.lower(),
                "time_in_force": o.time_in_force, "client_order_id": o.id}
        if o.notional:
            body["notional"] = f"{o.notional:.2f}"
        else:
            body["qty"] = f"{o.quantity:.9f}".rstrip("0").rstrip(".")
        if o.order_type == OrderType.LIMIT:
            body["limit_price"] = f"{o.limit_price:.2f}" if o.limit_price >= 1 else f"{o.limit_price:.4f}"
        try:
            r = self._call("POST", "/v2/orders", json=body)
        except BrokerNotAvailable:
            raise
        except BrokerError as exc:
            o.status, o.message = OrderStatus.REJECTED, str(exc)
            raise
        o.apply(r)
        if o.status == OrderStatus.OPEN:
            with self._lock:
                self._pending[o.id] = o
        return o

    def orders(self, limit: int = 100) -> list[UsOrder]:
        rows = self._call("GET", "/v2/orders", {"status": "all", "limit": limit, "direction": "desc"}) or []
        out = [UsOrder.from_alpaca(r, self.name) for r in rows]
        self._detect_fills(out)
        return out

    def _detect_fills(self, orders: list[UsOrder]) -> None:
        """Order limit buatan aplikasi yang terisi belakangan -> panggil pendengar (Telegram)."""
        done = []
        with self._lock:
            for o in orders:
                mine = self._pending.get(o.id)
                if mine is None or o.status == OrderStatus.OPEN:
                    continue
                del self._pending[o.id]
                if o.status == OrderStatus.FILLED:
                    mine.status, mine.fill_price, mine.filled_qty = o.status, o.fill_price, o.filled_qty
                    mine.filled_at = o.filled_at
                    done.append(mine)
        for o in done:
            for listener in self.on_fill:
                try:
                    listener(o)
                except Exception:
                    pass

    def cancel_order(self, order_id: str) -> UsOrder:
        rows = self._call("GET", "/v2/orders", {"status": "open", "limit": 500}) or []
        row = next((r for r in rows if order_id in (r.get("id"), r.get("client_order_id"))), None)
        if row is None:
            raise BrokerError("Order OPEN tidak ditemukan (mungkin sudah terisi atau dibatalkan)")
        self._call("DELETE", f"/v2/orders/{row['id']}")
        o = UsOrder.from_alpaca(row, self.name)
        o.status = OrderStatus.CANCELLED
        with self._lock:
            self._pending.pop(o.id, None)
        return o

    def account(self) -> dict:
        a = self._call("GET", "/v2/account")
        positions = []
        for p in self._call("GET", "/v2/positions") or []:
            positions.append({"symbol": p["symbol"], "quantity": float(p["qty"]), "avg_price": float(p["avg_entry_price"]),
                              "last_price": float(p["current_price"]), "market_value": float(p["market_value"]),
                              "unrealized_pl": float(p["unrealized_pl"]),
                              "unrealized_pl_pct": round(float(p["unrealized_plpc"]) * 100, 2),
                              "day_pl": float(p.get("unrealized_intraday_pl") or 0)})
        equity, last_equity = float(a["equity"]), float(a.get("last_equity") or a["equity"])
        return {"broker": self.name, "currency": a.get("currency", "USD"), "cash": float(a["cash"]),
                "buying_power": float(a["buying_power"]), "equity": equity,
                "market_value": float(a.get("long_market_value") or equity - float(a["cash"])),
                "day_pl": round(equity - last_equity, 2),
                "day_pl_pct": round((equity - last_equity) / last_equity * 100, 2) if last_equity else 0.0,
                "status": a.get("status", ""), "trading_blocked": bool(a.get("trading_blocked")),
                "positions": sorted(positions, key=lambda p: p["symbol"])}

    def market_clock(self) -> dict | None:
        """Jam bursa resmi dari Alpaca (termasuk hari libur); None bila belum diatur / gagal."""
        if not self.available():
            return None
        try:
            c = self.api.request("GET", "/v2/clock")
        except AlpacaError:
            return None
        return {"is_open": bool(c["is_open"]), "next_open": parse_ts(c.get("next_open")),
                "next_close": parse_ts(c.get("next_close"))}
