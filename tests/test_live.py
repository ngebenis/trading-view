import json
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from app.autotrader import WIB
from app.brokers import Order, OrderType, PaperBroker, Side
from app.chart_data import WIB_OFFSET, build_chart
from app.config import Settings
from app.main import create_app
from app.market_data import DemoProvider, MarketDataError, YahooProvider


def at(h, m, s=0):
    return datetime(2026, 10, 7, h, m, s, tzinfo=WIB).timestamp()


def test_demo_live_moves_during_session_only():
    clock = {"t": at(10, 30)}
    d = DemoProvider(live=True, clock=lambda: clock["t"])
    q = d.quote("BBRI")
    assert q.market_time == at(10, 30) and q.prev_close == d._history("BBRI")[-1].close
    prices = set()
    for i in range(12):
        clock["t"] = at(10, 30) + 5 * i
        prices.add(d.quote("BBRI").price)
    assert len(prices) > 1  # harga bergerak tiap beberapa detik
    # candle harian hari ini = agregat intraday dan close = harga terkini
    daily = d.candles("BBRI", "1mo", "1d")
    assert daily[-1].close == d.quote("BBRI").price and daily[-1].time == at(9, 0)
    five = d.candles("BBRI", "5d", "5m")
    assert five[0].time == at(9, 0) and all(b.time % 300 == 0 for b in five)
    # sebelum bursa buka: tidak ada data intraday, quote = penutupan kemarin
    clock["t"] = at(8, 0)
    assert d.candles("BBRI", "1d", "1m") == []
    assert d.quote("BBRI").price == d._history("BBRI")[-1].close
    # setelah tutup: harga berhenti di penutupan sesi
    clock["t"] = at(17, 0)
    q1 = d.quote("BBRI").price
    clock["t"] = at(18, 0)
    assert d.quote("BBRI").price == q1 and d.quote("BBRI").market_time == at(16, 0)


def test_demo_not_live_is_deterministic():
    d = DemoProvider()
    assert d.quote("BBCA") == d.quote("BBCA")
    with pytest.raises(MarketDataError):
        d.candles("BBCA", "1d", "5m")


def test_yahoo_market_time_and_short_quote_cache(monkeypatch):
    calls = []
    body = {"chart": {"result": [{"meta": {"regularMarketPrice": 9100, "regularMarketTime": 1_791_340_000,
                                           "gmtoffset": 25200, "currency": "IDR"},
                                  "timestamp": [1_791_250_000], "indicators": {"quote": [
                                      {"open": [9000], "high": [9000], "low": [9000], "close": [9000], "volume": [1]}]}}]}}

    def handler(r):
        calls.append(r.url.params["range"])
        return httpx.Response(200, json=body)

    clock = {"t": 1000.0}
    monkeypatch.setattr("app.market_data.time.time", lambda: clock["t"])
    y = YahooProvider(client=httpx.Client(transport=httpx.MockTransport(handler)), quote_ttl=5, cache_ttl=30)
    assert y.quote("BBCA").market_time == 1_791_340_000
    y.quote("BBCA")
    assert len(calls) == 1          # masih di cache
    clock["t"] += 6
    y.quote("BBCA")
    assert len(calls) == 2          # cache harga terkini hanya 5 detik
    y.candles("BBCA", "1y", "1d")
    clock["t"] += 10
    y.candles("BBCA", "1y", "1d")
    assert calls.count("1y") == 1   # histori harian tetap 30 detik


def test_intraday_chart_and_markers():
    clock = {"t": at(10, 30)}
    d = DemoProvider(live=True, clock=lambda: clock["t"])
    b = PaperBroker(None, 1e9, 0.15, 0.25, clock=lambda: clock["t"])
    clock["t"] = at(10, 12)
    b.place_order(Order("BBRI", Side.BUY, 3, OrderType.MARKET, created_at=at(10, 12)), d.quote("BBRI").price)
    clock["t"] = at(10, 30)
    c = build_chart(d, b, "BBRI", interval="5m")
    assert c["interval"] == "5m" and c["bar_seconds"] == 300
    assert c["bars"][0]["time"] == at(9, 0) + WIB_OFFSET           # jam WIB di sumbu grafik
    assert c["markers"][0]["time"] == at(10, 10) + WIB_OFFSET and c["markers"][0]["lots"] == 3
    daily = build_chart(d, b, "BBRI", "1mo")
    assert daily["interval"] == "1d" and daily["markers"][0]["time"] == "2026-10-07"


def test_stream_endpoint(tmp_path):
    provider = DemoProvider()
    app = create_app(Settings(data_dir=tmp_path, live_focus_seconds=0.1, live_watch_seconds=0.1,
                              live_stream_max_seconds=1), provider)
    client = TestClient(app)
    events = []
    with client.stream("GET", "/api/stream?symbols=bbca,IHSG,BBCA&focus=BBCA") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    hello, *quotes = events
    assert hello == {"symbols": ["BBCA", "IHSG"], "focus": "BBCA"}
    assert {q["symbol"] for q in quotes} == {"BBCA", "IHSG"}
    q = next(q for q in quotes if q["symbol"] == "BBCA")
    assert {"ara", "arb", "limit_status", "market_time", "data_age_seconds", "market_open"} <= set(q)
    assert client.get("/api/stream?symbols=").status_code in (400, 422)


def test_chart_interval_validation(tmp_path):
    client = TestClient(create_app(Settings(data_dir=tmp_path), DemoProvider()))
    assert client.get("/api/chart/BBCA?interval=2h").status_code == 400
    assert client.get("/api/chart/BBCA?interval=5m").status_code == 502  # demo non-live: tidak ada intraday
