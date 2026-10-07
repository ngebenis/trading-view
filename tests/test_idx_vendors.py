from datetime import datetime
from urllib.parse import parse_qsl

import httpx
import pytest
from fastapi.testclient import TestClient

from app.autotrader import WIB
from app.config import Settings
from app.idx_vendors import FallbackProvider, GoAPIProvider, InvezgoProvider, build_provider, parse_time
from app.main import create_app
from app.market_data import DemoProvider, MarketDataError

NOW = datetime(2026, 10, 7, 10, 15, tzinfo=WIB).timestamp()


class FakeInvezgo:
    """Format respons mengikuti tipe di SDK resmi Invezgo (invezgo/types.py)."""

    def __init__(self, wrap=False):
        self.wrap, self.requests, self.fail = wrap, [], set()

    def handler(self, request: httpx.Request):
        assert request.headers["Authorization"] == "Bearer kunci-invezgo"
        path, q = request.url.path, dict(parse_qsl(request.url.query.decode()))
        self.requests.append((path, q))
        if any(path.startswith(f) for f in self.fail):
            return httpx.Response(402, json={"message": "Advance role user only"})
        body = None
        if path == "/analysis/intraday-data/BBCA":
            body = {"code": "BBCA", "open": 9000, "high": 9100, "low": 8975, "close": 9050, "avg": 9030.5,
                    "volume": 1234500, "prev": 9000, "bid_price": 9025, "offer_price": 9050}
        elif path == "/analysis/intraday/BBCA":
            body = [{"date": "2026-10-07 10:13:00", "open": 9025, "high": 9050, "low": 9025, "close": 9050, "volume": 100},
                    {"date": "2026-10-07 10:14:00", "open": 9050, "high": 9050, "low": 9025, "close": 9050, "volume": 300}]
        elif path == "/analysis/intraday-index/COMPOSITE":
            body = {"code": "COMPOSITE", "open": 7100.5, "high": 7130, "low": 7090, "close": 7123.45, "prev": 7100.0}
        elif path == "/analysis/chart/stock/BBCA":
            body = [{"date": "2026-10-06", "open": 8950, "high": 9025, "low": 8900, "close": 9000, "volume": "2000000"},
                    {"date": "2026-10-05", "open": 8900, "high": 8975, "low": 8875, "close": 8950, "volume": "1500000"}]
        elif path == "/analysis/chart/multi-time/BBCA":
            body = [{"date": "2026-10-07T09:00:00+07:00", "open": 9000, "high": 9025, "low": 8975, "close": 9025, "volume": 10},
                    {"date": "2026-10-07T09:05:00+07:00", "open": 9025, "high": 9050, "low": 9000, "close": 9050, "volume": 20}]
        if body is None:
            return httpx.Response(404, json={"message": "Resource not found"})
        return httpx.Response(200, json={"data": body} if self.wrap else body)

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))


class FakeGoAPI:
    """Format respons mengikuti SDK resmi GoAPI (goapi-io/php-sdk): {"data": {"results": [...]}}."""

    def __init__(self, with_change=True):
        self.with_change, self.requests = with_change, []

    def handler(self, request: httpx.Request):
        assert request.headers["X-API-KEY"] == "kunci-goapi"
        path, q = request.url.path, dict(parse_qsl(request.url.query.decode()))
        self.requests.append((path, q))
        if path == "/stock/idx/prices":
            rows = []
            for s in q["symbols"].split(","):
                if s == "XXXX":
                    continue
                row = {"symbol": s, "company": {"symbol": s, "name": s}, "date": "2026-10-07", "open": 3000,
                       "high": 3050, "low": 2990, "close": 3040 if s != "BBCA" else 9050, "volume": 1000}
                if self.with_change:
                    row.update(change=50, change_pct=0.56)
                rows.append(row)
            return httpx.Response(200, json={"status": "success", "data": {"results": rows}})
        if path == "/stock/idx/BBCA/historical":
            return httpx.Response(200, json={"status": "success", "data": {"results": [  # terbaru dulu
                {"symbol": "BBCA", "date": "2026-10-07", "open": 9000, "high": 9100, "low": 8975, "close": 9050, "volume": 5},
                {"symbol": "BBCA", "date": "2026-10-06", "open": 8950, "high": 9025, "low": 8900, "close": 8975, "volume": 4},
            ]}})
        return httpx.Response(401, json={"status": "error", "message": "Invalid API Key"})

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def test_parse_time():
    assert parse_time(1_791_340_800_000) == 1_791_340_800
    assert parse_time("2026-10-07 10:14:00") == datetime(2026, 10, 7, 10, 14, tzinfo=WIB).timestamp()
    assert parse_time("2026-10-07T03:14:00Z") == datetime(2026, 10, 7, 10, 14, tzinfo=WIB).timestamp()
    assert parse_time("2026-10-07") is None and parse_time("") is None and parse_time("bukan waktu") is None


@pytest.mark.parametrize("wrap", [False, True])
def test_invezgo_quote_index_and_candles(wrap):
    fake = FakeInvezgo(wrap)
    p = InvezgoProvider("kunci-invezgo", "https://api.invezgo.test", fake.client())
    q = p.quote("bbca")
    assert (q.symbol, q.price, q.prev_close, q.source) == ("BBCA", 9050, 9000, "invezgo")
    assert round(q.change_pct, 2) == 0.56
    assert q.market_time == datetime(2026, 10, 7, 10, 14, tzinfo=WIB).timestamp()  # bar intraday terakhir
    assert ("/analysis/intraday-data/BBCA", {"market": "RG"}) in fake.requests
    ihsg = p.quote("IHSG")
    assert (ihsg.price, ihsg.prev_close, ihsg.market_time) == (7123.45, 7100.0, None)
    daily = p.candles("BBCA", "1y", "1d")
    assert [c.close for c in daily] == [8950, 9000] and daily[-1].volume == 2_000_000  # diurutkan, volume str -> int
    assert daily[-1].time == datetime(2026, 10, 6, 9, 0, tzinfo=WIB).timestamp()
    intraday = p.candles("BBCA", "1d", "5m")
    assert len(intraday) == 2 and fake.requests[-1][1]["timeframe"] == "5m"


def test_invezgo_errors_are_clear():
    fake = FakeInvezgo()
    fake.fail.add("/analysis/chart/multi-time")
    p = InvezgoProvider("kunci-invezgo", "https://api.invezgo.test", fake.client())
    with pytest.raises(MarketDataError, match="paket berlangganan.*Advance role"):
        p.candles("BBCA", "1d", "1m")
    with pytest.raises(MarketDataError, match="tidak ditemukan"):
        p.quote("TLKM")
    with pytest.raises(MarketDataError, match="INVEZGO_API_KEY"):
        InvezgoProvider("")


def test_goapi_quote_batches_symbols_and_caches():
    fake = FakeGoAPI()
    clock = {"t": NOW}
    p = GoAPIProvider("kunci-goapi", "https://api.goapi.test", fake.client(), clock=lambda: clock["t"])
    q = p.quote("BBCA")
    assert (q.price, q.prev_close, q.source, q.market_time) == (9050, 9000, "goapi", None)  # hanya tanggal
    p.quote("TLKM")  # simbol kedua: satu permintaan berisi keduanya
    assert fake.requests[-1] == ("/stock/idx/prices", {"symbols": "TLKM,BBCA"})
    n = len(fake.requests)
    p.quote("BBCA")  # sudah ada di cache dari permintaan gabungan
    assert len(fake.requests) == n
    clock["t"] += 10
    p.quote("BBCA")
    assert fake.requests[-1] == ("/stock/idx/prices", {"symbols": "BBCA,TLKM"})
    with pytest.raises(MarketDataError, match="tidak tersedia"):
        p.quote("XXXX")
    with pytest.raises(MarketDataError, match="indeks"):
        p.quote("IHSG")


def test_goapi_prev_close_from_history_and_candles():
    fake = FakeGoAPI(with_change=False)
    p = GoAPIProvider("kunci-goapi", "https://api.goapi.test", fake.client(), clock=lambda: NOW)
    assert p.quote("BBCA").prev_close == 8975  # penutupan 6 Okt dari data historis
    c = p.candles("BBCA", "1mo", "1d")
    assert [x.close for x in c] == [8975, 9050]
    assert fake.requests[-1][1] == {"from": "2026-09-06", "to": "2026-10-07"}
    with pytest.raises(MarketDataError, match="intraday"):
        p.candles("BBCA", "1d", "5m")


def test_fallback_provider():
    class Broken:
        name = "goapi"

        def quote(self, s):
            raise MarketDataError("down")

        def candles(self, *a):
            raise MarketDataError("down")

    fb = FallbackProvider(Broken(), DemoProvider())
    assert fb.name == "goapi" and fb.quote("BBCA").source == "demo" and fb.candles("BBCA", "1mo", "1d")
    assert fb.last_error == "down"
    with pytest.raises(MarketDataError):
        FallbackProvider(Broken(), None).quote("BBCA")


def test_build_provider_choices():
    s = Settings(market_data_provider="goapi", goapi_api_key="kunci-goapi", vendor_fallback="none")
    p = build_provider(s)
    assert isinstance(p, FallbackProvider) and p.fallback is None and p.primary.name == "goapi"
    assert build_provider(Settings(market_data_provider="demo")).name == "demo"
    with pytest.raises(MarketDataError, match="GOAPI_API_KEY"):
        build_provider(Settings(market_data_provider="goapi", goapi_api_key=""))


def test_app_uses_invezgo_with_fallback(tmp_path):
    fake = FakeInvezgo()
    settings = Settings(data_dir=tmp_path, market_data_provider="invezgo", invezgo_api_key="kunci-invezgo",
                        invezgo_base_url="https://api.invezgo.test", vendor_fallback="demo")
    c = TestClient(create_app(settings, vendor_http=fake.client()))
    assert c.get("/api/config").json()["market_data_provider"] == "invezgo"
    q = c.get("/api/quote/BBCA").json()
    assert (q["source"], q["price"], q["ara"]) == ("invezgo", 9050, 10800)
    assert q["data_age_seconds"] is not None
    tlkm = c.get("/api/quote/TLKM").json()  # tidak ada di Invezgo tiruan -> cadangan
    assert tlkm["source"] == "demo"
    chart = c.get("/api/chart/BBCA?interval=5m").json()
    assert len(chart["bars"]) == 2
