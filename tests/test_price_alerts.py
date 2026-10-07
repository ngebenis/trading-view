import pytest
from fastapi.testclient import TestClient

from app.binance import BinanceAPI, BinanceProvider
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.market_data import MarketDataError, Quote
from app.notifier import SignalWatcher
from app.price_alerts import AlertError, PriceAlertWatcher
from tests.fake_binance import FakeBinance
from tests.test_notifier import TOKEN, FakeTelegram


class Prices:
    name = "fake"

    def __init__(self, **prices):
        self.prices = prices

    def quote(self, symbol):
        if symbol not in self.prices:
            raise MarketDataError("tidak ada")
        p = self.prices[symbol]
        return Quote(symbol, p, 9000, p - 9000, (p / 9000 - 1) * 100, "IDR", "fake", 1_791_340_800)


@pytest.fixture
def env(tmp_path):
    tg, binance = FakeTelegram(), FakeBinance()
    clock = {"t": 1_791_340_800.0}
    watcher = SignalWatcher(None, None, http=tg.client())
    watcher.update_config({"bot_token": TOKEN, "chat_id": "42", "symbols": ["BBCA"]})
    watcher.crypto_provider = BinanceProvider(BinanceAPI("https://fake.binance", client=binance.client()), quote_ttl=0)
    prices = Prices(BBCA=9000, IHSG=7100.5)
    alerts = PriceAlertWatcher(prices, watcher, Database(tmp_path / "app.db"), clock=lambda: clock["t"])
    return alerts, prices, binance, tg, clock


def test_direction_is_set_from_current_price(env):
    alerts, prices, *_ = env
    up, notes = alerts.add("bbca", 9500)
    down, _ = alerts.add("BBCA", 8500, note="support")
    assert (up.direction, up.created_price, up.market) == ("above", 9000, "idx") and notes == []
    assert down.direction == "below" and down.note == "support"
    with pytest.raises(AlertError, match="sudah di atas target"):
        alerts.add("BBCA", 8000, direction="above")
    with pytest.raises(AlertError, match="sama dengan harga"):
        alerts.add("BBCA", 9000)
    with pytest.raises(AlertError, match="tidak tersedia"):
        alerts.add("TLKM", 3000)


def test_one_shot_alert_fires_once(env):
    alerts, prices, _, tg, _ = env
    alerts.add("BBCA", 9500, note="breakout")
    assert alerts.check() == [] and tg.sent == []
    prices.prices["BBCA"] = 9525
    fired = alerts.check()
    assert [e["kind"] for e in fired] == ["TARGET"] and "Naik tembus 9.500" in fired[0]["message"]
    text = tg.sent[0]["text"]
    assert "BBCA naik tembus 9.500" in text and "Harga: <b>9.525</b>" in text and "Catatan: breakout" in text
    assert alerts.check() == [] and len(tg.sent) == 1
    a = alerts.status()["alerts"][0]
    assert (a["status"], a["triggered_price"], a["count"]) == ("triggered", 9525, 1)


def test_repeat_alert_rearms_after_crossing_back_with_cooldown(env):
    alerts, prices, _, tg, clock = env
    alerts.add("BBCA", 8500, repeat=True, cooldown_minutes=10)
    prices.prices["BBCA"] = 8450
    alerts.check()
    assert len(tg.sent) == 1 and "turun tembus" in tg.sent[0]["text"]
    alerts.check()
    assert len(tg.sent) == 1  # masih di bawah target: tidak berulang
    prices.prices["BBCA"] = 8600
    alerts.check()  # kembali ke atas -> aktif lagi
    prices.prices["BBCA"] = 8400
    alerts.check()
    assert len(tg.sent) == 1  # masih dalam cooldown 10 menit
    clock["t"] += 601
    alerts.check()
    assert len(tg.sent) == 2


def test_waits_for_telegram_and_retries_failed_send(env, tmp_path):
    alerts, prices, *_ = env
    tg = FakeTelegram(fail=True)
    w = SignalWatcher(None, None, http=tg.client())
    a = PriceAlertWatcher(prices, w, None)
    _, notes = a.add("BBCA", 9500)
    assert "Telegram belum diatur" in notes[0]
    prices.prices["BBCA"] = 9600
    assert a.check() == [] and a.alerts[0].status == "active"  # menunggu Telegram diatur
    w.update_config({"bot_token": TOKEN, "chat_id": "42", "symbols": ["BBCA"]})
    assert a.check()[0]["kind"] == "ERROR" and a.alerts[0].status == "active"  # gagal -> coba lagi
    tg.fail = False
    assert a.check()[0]["kind"] == "TARGET"


def test_crypto_alert_and_ara_note(env):
    alerts, prices, binance, tg, _ = env
    a, _ = alerts.add("btc/usdt", 70000)
    assert (a.symbol, a.market, a.direction) == ("BTCUSDT", "crypto", "above")
    _, notes = alerts.add("BBCA", 11000)  # ARA dari harga acuan 9000 = 10.800
    assert "di luar rentang ARB–ARA" in notes[0]
    binance.prices["BTCUSDT"] = 70500
    alerts.check()
    assert "BTCUSDT naik tembus 70.000,00 USDT" in tg.sent[0]["text"] and "Binance</a>" in tg.sent[0]["text"]


def test_persistence_remove_and_rearm(env, tmp_path):
    alerts, prices, *_ = env
    a, _ = alerts.add("BBCA", 9500)
    prices.prices["BBCA"] = 9600
    alerts.check()
    again = PriceAlertWatcher(prices, alerts.notifier, alerts.db)
    assert again.alerts[0].status == "triggered"
    r = again.rearm(a.id)
    assert (r.status, r.direction) == ("active", "below")  # harga kini di atas target
    again.remove(a.id)
    assert again.status()["alerts"] == []
    with pytest.raises(AlertError, match="tidak ditemukan"):
        again.remove(a.id)


def test_api(tmp_path):
    tg = FakeTelegram()
    from app.market_data import DemoProvider
    c = TestClient(create_app(Settings(data_dir=tmp_path), DemoProvider(), telegram_http=tg.client()))
    price = c.get("/api/quote/BBCA").json()["price"]
    r = c.post("/api/alerts", json={"symbol": "BBCA", "target": price * 1.05})
    assert r.status_code == 200 and "Telegram belum diatur" in r.json()["notes"][0]
    assert r.json()["alerts"][0]["direction"] == "above"
    assert c.post("/api/alerts", json={"symbol": "BBCA", "target": price}).status_code == 400
    assert c.post("/api/alerts", json={"symbol": "BBCA", "target": -1}).status_code == 422
    aid = r.json()["created"]
    assert c.post("/api/alerts/check").json()["fired"] == []
    assert c.delete(f"/api/alerts/{aid}").json()["alerts"] == []
    assert c.delete(f"/api/alerts/{aid}").status_code == 400
