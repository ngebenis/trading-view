import json
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.autotrader import WIB
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.market_data import Candle, DemoProvider, MarketDataError, Quote
from app.price_feed import FeedError, FeedProvider, PriceFeed, parse_bar, pine_script

# Rabu 7 Okt 2026, 10:00 WIB (bursa buka)
OPEN = datetime(2026, 10, 7, 10, 0, tzinfo=WIB).timestamp()


class BaseProvider:
    """Provider 'tertunda': bar 1 menit sampai 09:45 dan quote lama."""
    name = "base"

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def quote(self, symbol):
        self.calls += 1
        if self.fail:
            raise MarketDataError("down")
        return Quote(symbol, 9000, 8900, 100, 1.12, "IDR", "base", OPEN - 900)

    def candles(self, symbol, range_="6mo", interval="1d"):
        if self.fail:
            raise MarketDataError("down")
        start = datetime(2026, 10, 7, 9, 0, tzinfo=WIB).timestamp()
        if interval == "1d":
            return [Candle(int(start - 86400), 8800, 8950, 8750, 8900, 1000),
                    Candle(int(start), 8900, 9010, 8880, 9000, 500)]
        return [Candle(int(start + m * 60), 8900, 9010, 8880, 9000, 10) for m in range(46)]


def bar(sym, t, o, h, l, c, v=100, pc=8900):
    return [sym, int(t * 1000), o, h, l, c, v, pc]


@pytest.fixture
def feed(tmp_path):
    clock = {"t": OPEN}
    f = PriceFeed(Database(tmp_path / "app.db"), clock=lambda: clock["t"])
    f.update_config({"enabled": True, "symbols": ["BBCA", "IHSG"]})
    return f, clock


def test_parse_bar_forms():
    sym, c, pc = parse_bar(["IDX:BBCA", 1_791_340_800_000, 9000, 9050, 8975, 9025, 1200, 8900])
    assert (sym, c.time, c.close, c.volume, pc) == ("BBCA", 1_791_340_800, 9025, 1200, 8900)
    sym, c, pc = parse_bar({"symbol": "IDX:COMPOSITE", "time": 1_791_340_800, "open": 7000, "high": 7010,
                            "low": 6990, "close": 7005})
    assert (sym, c.volume, pc) == ("IHSG", 0, None)
    with pytest.raises(FeedError, match="tidak konsisten"):
        parse_bar(["BBCA", 1_791_340_800, 9000, 8990, 8975, 9025])
    with pytest.raises(FeedError, match="positif"):
        parse_bar(["BBCA", 1_791_340_800, 9000, 9050, 0, 9025])
    with pytest.raises(FeedError):
        parse_bar(["BBCA", 1])


def test_ingest_rules(feed):
    f, _ = feed
    f.update_config({"enabled": False})
    with pytest.raises(FeedError) as e:
        f.ingest({"tf": "1", "bars": [bar("IDX:BBCA", OPEN - 60, 9000, 9050, 9000, 9050)]})
    assert e.value.status == 403
    f.update_config({"enabled": True})
    with pytest.raises(FeedError, match="Timeframe"):
        f.ingest({"tf": "D", "bars": [bar("IDX:BBCA", OPEN - 60, 9000, 9050, 9000, 9050)]})
    out = f.ingest({"tf": "1", "bars": [
        bar("IDX:BBCA", OPEN - 60, 9000, 9050, 9000, 9050),
        bar("IDX:TLKM", OPEN - 60, 3000, 3010, 3000, 3010),     # tidak di daftar
        bar("IDX:BBCA", OPEN - 5 * 86400, 9000, 9050, 9000, 9050),  # terlalu lama
    ]})
    assert out == {"accepted": ["BBCA"], "ignored": ["TLKM", "BBCA"]}
    assert f.stats["messages"] == 1 and f.stats["bars"] == 1 and f.stats["ignored"] == 2


def test_quote_fresh_stale_and_after_close(feed):
    f, clock = feed
    base = BaseProvider()
    p = FeedProvider(base, f)
    assert p.quote("BBCA").source == "base"  # belum ada feed
    f.ingest({"tf": "1", "bars": [bar("IDX:BBCA", OPEN - 60, 9000, 9075, 9000, 9075, pc=8900)]})
    q = p.quote("BBCA")
    assert (q.source, q.price, q.prev_close, q.market_time) == ("tradingview", 9075, 8900, OPEN)
    assert round(q.change_pct, 2) == 1.97
    # Feed berhenti saat bursa buka -> kembali ke provider biasa.
    clock["t"] = OPEN + 600
    assert p.quote("BBCA").source == "base"
    # Setelah bursa tutup hari yang sama, harga terakhir feed tetap dipakai.
    clock["t"] = datetime(2026, 10, 7, 16, 30, tzinfo=WIB).timestamp()
    assert p.quote("BBCA").source == "tradingview"
    # Hari berikutnya sebelum bursa buka: tidak lagi.
    clock["t"] = datetime(2026, 10, 8, 8, 0, tzinfo=WIB).timestamp()
    assert p.quote("BBCA").source == "base"


def test_prev_close_fallback(feed):
    f, _ = feed
    f.ingest({"tf": "1", "bars": [bar("IDX:BBCA", OPEN - 60, 9000, 9075, 9000, 9075, pc=0)]})
    assert FeedProvider(BaseProvider(), f).quote("BBCA").prev_close == 8900  # dari provider biasa
    assert FeedProvider(BaseProvider(fail=True), f).quote("BBCA").prev_close == 9000  # open bar


def test_disabled_feed_is_ignored(feed):
    f, _ = feed
    f.ingest({"tf": "1", "bars": [bar("IDX:BBCA", OPEN - 60, 9000, 9075, 9000, 9075)]})
    f.update_config({"enabled": False})
    assert FeedProvider(BaseProvider(), f).quote("BBCA").source == "base"


def test_intraday_and_daily_candles_merge(feed):
    f, _ = feed
    p = FeedProvider(BaseProvider(), f)
    # Feed mulai 09:44 (tumpang tindih dengan data tertunda) sampai 09:59.
    for m in range(44, 60):
        t = datetime(2026, 10, 7, 9, m, tzinfo=WIB).timestamp()
        f.ingest({"tf": "1", "bars": [bar("IDX:BBCA", t, 9000, 9100 + m, 8990, 9050 + m, v=50)]})
    one = p.candles("BBCA", "1d", "1m")
    assert len(one) == 60 and one[-1].close == 9109 and one[-1].volume == 50
    assert one[44].open == 8900 and one[44].high == 9144  # gabungan: open lama, high dari feed
    five = p.candles("BBCA", "5d", "5m")
    assert [c.time % 300 for c in five] == [0] * len(five) and five[-1].volume == 250 and five[-1].close == 9109
    daily = p.candles("BBCA", "1y", "1d")
    assert len(daily) == 2  # candle hari ini diperbarui, bukan ditambah
    assert (daily[-1].open, daily[-1].high, daily[-1].close) == (8900, 9159, 9109)
    # Sumber lain gagal: grafik intraday tetap tampil dari feed saja.
    assert len(FeedProvider(BaseProvider(fail=True), f).candles("BBCA", "1d", "1m")) == 16


def test_feed_coarser_than_chart_uses_base(feed):
    f, _ = feed
    f.ingest({"tf": "5", "bars": [bar("IDX:BBCA", OPEN - 300, 9000, 9075, 9000, 9075)]})
    assert len(FeedProvider(BaseProvider(), f).candles("BBCA", "1d", "1m")) == 46


def test_persistence_and_status(tmp_path):
    clock = {"t": OPEN}
    db = Database(tmp_path / "app.db")
    f = PriceFeed(db, clock=lambda: clock["t"])
    f.update_config({"enabled": True, "symbols": ["BBCA"]})
    f.ingest({"tf": "1", "bars": [bar("IDX:BBCA", OPEN - 60, 9000, 9075, 9000, 9075)]})
    again = PriceFeed(db, clock=lambda: clock["t"])
    assert again.config.symbols == ["BBCA"] and again.config.enabled
    assert again.quote("BBCA").price == 9075
    row = again.status()["symbols"][0]
    assert row["active"] and row["bars_today"] == 1 and row["age_seconds"] == 0


def test_config_validation(feed):
    f, _ = feed
    with pytest.raises(ValueError, match="Maksimal 20"):
        f.update_config({"symbols": [f"S{i}" for i in range(21)]})
    with pytest.raises(ValueError, match="minimal satu"):
        f.update_config({"symbols": []})
    assert f.update_config({"symbols": ["bbca", "BBCA.JK", "^JKSE"]}).symbols == ["BBCA", "IHSG"]


def test_pine_script():
    src = pine_script("RAHASIA_123", ["BBCA", "IHSG"])
    assert '//@version=5' in src and '"secret":"RAHASIA_123"' in src
    assert 'b0 = feedBar("IDX:BBCA")' in src and 'b1 = feedBar("IDX:COMPOSITE")' in src
    assert "array.from(b0, b1)" in src and "alert.freq_once_per_bar_close" in src
    assert '{1,number,#}' in src  # kurung kurawal format Pine tidak hilang oleh f-string


def test_api_feed_end_to_end(tmp_path):
    client = TestClient(create_app(Settings(data_dir=tmp_path), DemoProvider()))
    secret = client.get("/api/webhooks").json()["config"]["secret"]
    r = client.put("/api/feed/config", json={"enabled": True, "symbols": ["BBCA"]})
    assert r.status_code == 200 and r.json()["config"]["enabled"]
    assert client.put("/api/feed/config", json={"stale_seconds": 5}).status_code == 400
    now = time.time()
    payload = {"secret": secret, "type": "bars", "tf": "1",
               "bars": [bar("IDX:BBCA", now - 60, 1234, 1240, 1230, 1235, pc=1200)]}
    # Webhook alert boleh nonaktif: price feed punya sakelar sendiri.
    r = client.post("/api/webhooks/tradingview", content=json.dumps(payload))
    assert r.status_code == 200 and r.json()["accepted"] == ["BBCA"]
    q = client.get("/api/quote/BBCA").json()
    assert (q["source"], q["price"], q["prev_close"]) == ("tradingview", 1235, 1200)
    assert q["data_age_seconds"] < 120  # waktu tutup bar terakhir
    bad = client.post("/api/webhooks/tradingview", content=json.dumps({**payload, "secret": "salah"}),
                      headers={"X-Forwarded-For": "198.51.100.7"})
    assert bad.status_code == 401
    assert "198.51.100.7" in client.get("/api/webhooks").json()["log"][0]["message"]
    assert client.post("/api/webhooks/tradingview",
                       content=json.dumps({**payload, "tf": "D"})).status_code == 400
    pine = client.get("/api/feed/pine")
    assert pine.status_code == 200 and secret in pine.text and 'feedBar("IDX:BBCA")' in pine.text
    st = client.get("/api/feed").json()
    assert st["symbols"][0]["active"] and st["stats"]["bars"] == 1
    # Lewat tunnel hanya endpoint webhook yang terbuka, status & skrip tidak.
    assert client.get("/api/feed/pine", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 403
