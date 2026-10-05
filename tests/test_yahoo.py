from datetime import datetime

import httpx
import pytest

from app.autotrader import WIB
from app.market_data import MarketDataError, YahooProvider


def day(d, h=9):
    return int(datetime(2026, 10, d, h, tzinfo=WIB).timestamp())


def chart(closes_by_day, price, market_time):
    days = sorted(closes_by_day)
    closes = [closes_by_day[d] for d in days]
    return {"chart": {"result": [{
        "meta": {"currency": "IDR", "regularMarketPrice": price, "regularMarketTime": market_time,
                 "gmtoffset": 25200, "chartPreviousClose": 8000, "previousClose": None},
        "timestamp": [day(d) for d in days],
        "indicators": {"quote": [{"open": closes, "high": closes, "low": closes, "close": closes,
                                  "volume": [1000] * len(closes)}]},
    }], "error": None}}


def provider(handler):
    return YahooProvider(client=httpx.Client(transport=httpx.MockTransport(handler)), cache_ttl=0)


def test_quote_uses_yesterdays_close_during_session():
    # Senin 5 Okt sedang berjalan (candle hari ini ada), sesi sebelumnya = Jumat 2 Okt.
    body = chart({1: 8950, 2: 9000, 5: 9100}, 9100, day(5, 10))
    q = provider(lambda r: httpx.Response(200, json=body)).quote("bbca")
    assert q.symbol == "BBCA" and q.price == 9100 and q.prev_close == 9000
    assert q.change_pct == pytest.approx(100 / 9000 * 100)


def test_quote_before_open_compares_last_two_sessions():
    # Belum ada candle hari ini: harga = close Jumat, pembanding = close Kamis.
    body = chart({1: 8950, 2: 9000}, 9000, day(2, 16))
    q = provider(lambda r: httpx.Response(200, json=body)).quote("BBCA")
    assert q.prev_close == 8950


def test_symbol_suffix_and_candles():
    seen = []

    def handler(r):
        seen.append(str(r.url))
        return httpx.Response(200, json=chart({1: 100, 2: 102}, 102, day(2, 16)))

    candles = provider(handler).candles("IDX:TLKM", "1y", "1d")
    assert "/v8/finance/chart/TLKM.JK" in seen[0] and "range=1y" in seen[0]
    assert [c.close for c in candles] == [100, 102]


def test_not_found_and_rate_limit_fallback():
    with pytest.raises(MarketDataError, match="tidak ditemukan"):
        provider(lambda r: httpx.Response(404, json={"chart": {"result": None}})).quote("XXXX")

    hosts = []

    def limited_then_ok(r):
        hosts.append(r.url.host)
        if r.url.host.startswith("query1"):
            return httpx.Response(429)
        return httpx.Response(200, json=chart({1: 100, 2: 102}, 102, day(2, 16)))

    assert provider(limited_then_ok).quote("BBCA").price == 102
    assert hosts == ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]

    with pytest.raises(MarketDataError, match="rate limit"):
        provider(lambda r: httpx.Response(429)).quote("BBCA")
