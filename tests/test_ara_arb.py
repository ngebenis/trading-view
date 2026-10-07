import json

import pytest
from fastapi.testclient import TestClient

from app.autotrader import AutoTrader
from app.brokers import BrokerError, Order, OrderType, PaperBroker, Side
from app.config import Settings
from app.idx_rules import _parse_pcts, check_auto_rejection, limit_status, price_limits, round_to_tick
from app.main import create_app
from app.market_data import Candle, DemoProvider, Quote
from app.notifier import SignalWatcher
from app.webhooks import TradingViewWebhook
from tests.test_notifier import TOKEN, FakeTelegram


@pytest.mark.parametrize("ref,arb,ara", [
    (6175, 5250, 7400),   # >5.000: ARA 20% (7.410 -> fraksi 25 ke bawah), ARB 15% (5.248,75 -> ke atas)
    (2290, 1950, 2860),   # 200–5.000: ARA 25%
    (620, 530, 775),
    (5000, 4250, 6250),   # tepat 5.000 masih rentang 25%
    (5025, 4280, 6025),   # di atas 5.000 -> 20%
    (200, 170, 270),      # tepat 200 masih rentang 35%
    (150, 128, 202),      # 202,5 -> fraksi 2 -> 202
    (55, 50, 74),         # ARB tidak boleh di bawah Rp50
])
def test_price_limits(ref, arb, ara):
    assert price_limits(ref) == (arb, ara)


def test_price_limits_custom_and_missing():
    assert price_limits(1000, ara_pcts=(10, 10, 10), arb_pcts=(10, 10, 10)) == (900, 1100)
    assert price_limits(None) is None and price_limits(0) is None


def test_limit_status_and_rejection():
    lim = (5250, 7400)
    assert limit_status(7400, lim) == "ARA" and limit_status(5250, lim) == "ARB" and limit_status(6000, lim) is None
    assert limit_status(50, (50, 74)) is None  # di harga minimum, bukan "ARB"
    assert "di atas batas ARA (7.400)" in check_auto_rejection("BUY", "LIMIT", 6000, 7425, lim)
    assert "di bawah batas ARB" in check_auto_rejection("SELL", "LIMIT", 6000, 5225, lim)
    assert check_auto_rejection("BUY", "LIMIT", 6000, 7400, lim) is None      # tepat di batas boleh
    assert "tidak ada penjual" in check_auto_rejection("BUY", "MARKET", 7400, None, lim)
    assert check_auto_rejection("SELL", "MARKET", 7400, None, lim) is None    # jual saat ARA boleh
    assert "tidak ada pembeli" in check_auto_rejection("SELL", "MARKET", 5250, None, lim)
    assert check_auto_rejection("BUY", "MARKET", 5250, None, lim) is None     # beli saat ARB boleh
    assert check_auto_rejection("BUY", "MARKET", 7400, None, None) is None    # tanpa harga acuan


def test_env_parsing(monkeypatch):
    monkeypatch.setenv("X_PCT", "35,25,20")
    assert _parse_pcts("X_PCT", (1, 1, 1)) == (35, 25, 20)
    monkeypatch.setenv("X_PCT", "10")
    assert _parse_pcts("X_PCT", (1, 1, 1)) == (10, 10, 10)
    monkeypatch.delenv("X_PCT")
    assert _parse_pcts("X_PCT", (1, 2, 3)) == (1, 2, 3)
    for bad in ("1,2", "0,10,10", "abc"):
        monkeypatch.setenv("X_PCT", bad)
        with pytest.raises(ValueError):
            _parse_pcts("X_PCT", (1, 1, 1))


def test_paper_broker_enforces_limits():
    b = PaperBroker(None, 1e9, 0.15, 0.25)
    lim = (5250, 7400)
    with pytest.raises(BrokerError, match="ARA"):
        b.place_order(Order("BBCA", Side.BUY, 1, OrderType.LIMIT, 7425), 6000, lim)
    with pytest.raises(BrokerError, match="tidak ada penjual"):
        b.place_order(Order("BBCA", Side.BUY, 1, OrderType.MARKET), 7400, lim)
    b.place_order(Order("BBCA", Side.BUY, 2, OrderType.MARKET), 6000, lim)
    with pytest.raises(BrokerError, match="tidak ada pembeli"):
        b.place_order(Order("BBCA", Side.SELL, 1, OrderType.MARKET), 5250, lim)
    assert b.positions["BBCA"]["shares"] == 200
    assert [o.status.value for o in b.orders()].count("REJECTED") == 3


class LimitProvider:
    """Harga sekarang & penutupan kemarin bisa diatur; candle untuk sinyal BELI."""
    name = "lim"

    def __init__(self, price, prev, closes):
        self.price, self.prev, self.closes = price, prev, closes

    def quote(self, symbol):
        return Quote(symbol, self.price, self.prev, self.price - self.prev, (self.price / self.prev - 1) * 100)

    def candles(self, symbol, range_="1y", interval="1d"):
        return [Candle(i, c, c, c, c, 0) for i, c in enumerate(self.closes)]


def test_autotrader_skips_buy_at_ara():
    import math
    wave = [1000 + 200 * math.sin(i / 8) for i in range(42)]  # pola sinyal BELI
    trader = AutoTrader(PaperBroker(None, 1e8, 0.15, 0.25), LimitProvider(1250, 1000, wave), None, 20)
    trader.update_config({"symbols": ["AAAA"]})
    out = trader.run_cycle()
    assert trader.broker.positions == {}
    assert any(e["level"] == "WARN" and "tidak ada penjual" in e["message"] for e in out)
    trader2 = AutoTrader(PaperBroker(None, 1e8, 0.15, 0.25), LimitProvider(1010, 1000, wave), None, 20)
    trader2.update_config({"symbols": ["AAAA"]})
    trader2.run_cycle()
    assert "AAAA" in trader2.broker.positions


def test_webhook_order_respects_limits():
    hook = TradingViewWebhook(PaperBroker(None, 1e8, 0.15, 0.25), LimitProvider(1250, 1000, [1000] * 40), None, 20)
    hook.update_config({"enabled": True, "mode": "order"})
    alert, _ = hook.accept(json.dumps({"secret": hook.config.secret, "symbol": "AAAA", "action": "buy"}).encode())
    out = hook.process(alert)
    assert out[0]["kind"] == "ERROR" and "ARA" in out[0]["message"]


def test_backtest_replay_quote_uses_previous_close():
    from app.backtest import _ReplayProvider
    day = 86400
    candles = [Candle(1_790_000_000 + i * day, 100 + i, 0, 0, 200 + i, 0) for i in range(3)]
    rp = _ReplayProvider({"AAAA": candles}, "next_open")
    from app.backtest import _day
    rp.exec_day = _day(candles[2].time)
    q = rp.quote("AAAA")
    assert q.price == 102 and q.prev_close == 201  # open hari ini, acuan = close kemarin


def test_notifier_limit_alert_once_per_day():
    tg = FakeTelegram()
    clock = {"t": 1_800_000_000.0}
    provider = LimitProvider(1250, 1000, [1000.0] * 60)
    w = SignalWatcher(provider, None, http=tg.client(), clock=lambda: clock["t"])
    w.update_config({"bot_token": TOKEN, "chat_id": "42", "symbols": ["AAAA"]})
    out = w.scan()
    assert out[0]["kind"] == "ARA" and "AAAA MENYENTUH ARA" in tg.sent[0]["text"]
    assert "rentang hari ini 850 – 1.250" in tg.sent[0]["text"]
    w.scan()
    assert len(tg.sent) == 1  # hari yang sama: tidak dikirim ulang
    clock["t"] += 86400
    w.scan()
    assert len(tg.sent) == 2  # hari berikutnya: dikabarkan lagi
    provider.price = 850
    w.scan()
    assert "MENYENTUH ARB" in tg.sent[-1]["text"]
    w.update_config({"notify_limits": False})
    clock["t"] += 86400
    w.scan()
    assert len(tg.sent) == 3


def test_api_quote_and_order(tmp_path):
    client = TestClient(create_app(Settings(data_dir=tmp_path), DemoProvider()))
    q = client.get("/api/quote/BBCA").json()
    assert (q["arb"], q["ara"]) == price_limits(q["prev_close"]) and q["reference_price"] == q["prev_close"]
    assert q["limit_status"] in (None, "ARA", "ARB")
    over = round_to_tick(q["ara"] * 1.05)  # harga valid (sesuai fraksi) tapi di atas ARA
    r = client.post("/api/orders", json={"symbol": "BBCA", "side": "BUY", "lots": 1, "order_type": "LIMIT",
                                         "limit_price": over})
    assert r.status_code == 400 and "ARA" in r.json()["detail"]
    ihsg = client.get("/api/quote/IHSG").json()
    assert ihsg["ara"] is None and ihsg["limit_status"] is None
    cfg = client.get("/api/config").json()
    assert cfg["ara_pcts"] == [35.0, 25.0, 20.0] and cfg["arb_pcts"] == [15.0, 15.0, 15.0]
