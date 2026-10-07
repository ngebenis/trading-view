import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.finnhub import FinnhubAPI, FinnhubProvider
from app.main import create_app
from app.market_data import DemoProvider, MarketDataError
from tests.fake_finnhub import BASE, KEY, FakeFinnhub
from tests.test_notifier import TOKEN, FakeTelegram


def make(tmp_path, fake=None, **kw):
    fake, tg = fake or FakeFinnhub(), FakeTelegram()
    settings = Settings(data_dir=tmp_path, finnhub_api_key=KEY, finnhub_base_url=BASE, **kw)
    app = create_app(settings, DemoProvider(), telegram_http=tg.client(), finnhub_http=fake.client())
    app.state.watcher.sync_send = True
    c = TestClient(app)
    c.put("/api/notifications/config", json={"bot_token": TOKEN, "chat_id": "42"})
    return c, app, fake, tg


@pytest.fixture
def env(tmp_path):
    return make(tmp_path)


def test_quote_uses_finnhub_with_header_key(env):
    c, _, fake, _ = env
    q = c.get("/api/us/quote/aapl").json()
    assert (q["symbol"], q["price"], q["change"], q["change_pct"], q["source"]) == ("AAPL", 230.0, 5.0, 2.22, "finnhub")
    assert fake.requests[0][3] == KEY and "token" not in fake.requests[0][2]
    assert c.get("/api/us/quote/ZZZZ").status_code == 502  # Finnhub membalas semua nol
    assert "Tidak ada data harga" in c.get("/api/us/quote/ZZZZ").json()["detail"]
    assert c.get("/api/us/quote/BBCA.JK").status_code == 400


def test_free_plan_candles_fall_back_to_yahoo_and_paid_uses_finnhub(env):
    c, _, fake, _ = env
    ch = c.get("/api/us/chart/AAPL?interval=5m&limit=60").json()
    assert len(ch["bars"]) == 60 and ch["bars"][0]["time"] < ch["bars"][-1]["time"] and ch["ema12"]
    hosts = [r[0] for r in fake.requests]
    assert "query1.finance.yahoo.com" in hosts
    y = next(r for r in fake.requests if r[0] == "query1.finance.yahoo.com")
    assert y[2] == {"range": "1mo", "interval": "5m"}
    fake.candles_allowed = True
    fake.requests.clear()
    p = FinnhubProvider(FinnhubAPI(BASE, KEY, client=fake.client()))
    candles = p.candles("AAPL", interval="1h", limit=50)
    assert len(candles) == 50 and fake.requests[0][2]["resolution"] == "60"
    assert all(r[0] != "query1.finance.yahoo.com" for r in fake.requests)


def test_candles_error_when_both_sources_fail(env):
    c, _, fake, _ = env
    fake.yahoo_ok = False
    r = c.get("/api/us/chart/AAPL")
    assert r.status_code == 502 and "butuh paket berbayar" in r.json()["detail"]


def test_key_errors_and_config(tmp_path):
    fake = FakeFinnhub()
    bad = FinnhubProvider(FinnhubAPI(BASE, "salah", client=fake.client()))
    with pytest.raises(MarketDataError, match="API key Finnhub ditolak"):
        bad.quote("AAPL")
    with pytest.raises(MarketDataError, match="belum diatur"):
        FinnhubProvider(FinnhubAPI(BASE)).quote("AAPL")
    c, *_ = make(tmp_path)
    cfg = c.get("/api/us/config").json()
    assert cfg["data_source"] == "finnhub" and cfg["default_broker"] == "sim" and cfg["market_clock_source"] == "finnhub"
    assert cfg["brokers"]["sim"]["available"] and not cfg["brokers"]["paper"]["available"]


def test_simulated_account_orders_and_telegram(env):
    c, app, fake, tg = env
    r = c.post("/api/us/orders", json={"symbol": "AAPL", "side": "BUY", "notional": 2300})
    assert r.status_code == 200 and r.json()["status"] == "FILLED" and r.json()["fill_price"] == 230.0
    assert r.json()["filled_qty"] == 10.0
    msg = tg.sent[-1]["text"]
    assert "Order manual — BELI AAPL" in msg and "10 saham @ $230,00" in msg and "Simulasi Saham AS" in msg
    a = c.get("/api/us/account").json()
    assert (a["cash"], a["equity"], a["total_pl"]) == (97700.0, 100000.0, 0.0)
    assert a["positions"][0]["quantity"] == 10.0 and a["day_pl"] == 0.0
    fake.prices["AAPL"] = 240.0
    app.state.us["provider"]._cache.clear()
    a = c.get("/api/us/account").json()
    assert a["equity"] == 100100.0 and a["total_pl"] == 100.0 and a["day_pl"] == 100.0
    # jual sebagian, lalu jual melebihi kepemilikan
    assert c.post("/api/us/orders", json={"symbol": "AAPL", "side": "SELL", "quantity": 4}).status_code == 200
    r = c.post("/api/us/orders", json={"symbol": "AAPL", "side": "SELL", "quantity": 7})
    assert r.status_code == 400 and "tidak cukup" in r.json()["detail"] and "❌ Ditolak" in tg.sent[-1]["text"]
    assert c.get("/api/us/account").json()["positions"][0]["quantity"] == 6.0


def test_simulated_limit_orders_fill_later_and_cancel(env):
    c, app, fake, tg = env
    lim = c.post("/api/us/orders", json={"symbol": "AAPL", "side": "BUY", "quantity": 5, "order_type": "LIMIT",
                                         "limit_price": 220}).json()
    assert lim["status"] == "OPEN" and "⏳ Order limit dipasang: 5 saham @ $220,00" in tg.sent[-1]["text"]
    assert c.get("/api/us/account").json()["buying_power"] == 100000.0 - 1100.0  # dana ditahan
    assert c.get("/api/us/orders").json()[0]["status"] == "OPEN"
    fake.prices["AAPL"] = 219.0
    app.state.us["provider"]._cache.clear()
    o = c.get("/api/us/orders").json()[0]
    assert o["status"] == "FILLED" and o["fill_price"] == 220.0
    assert "✅ Order limit terisi: 5 saham @ $220,00" in tg.sent[-1]["text"]
    n = len(tg.sent)
    c.get("/api/us/orders")
    assert len(tg.sent) == n
    lim2 = c.post("/api/us/orders", json={"symbol": "AAPL", "side": "BUY", "quantity": 1, "order_type": "LIMIT",
                                          "limit_price": 100}).json()
    assert c.delete(f"/api/us/orders/{lim2['id']}").json()["status"] == "CANCELLED"
    assert c.delete(f"/api/us/orders/{lim2['id']}").status_code == 400


def test_simulated_balance_limits_and_reset(env):
    c, *_ = env
    r = c.post("/api/us/orders", json={"symbol": "MSFT", "side": "BUY", "quantity": 100})  # $42.000 > 20% ekuitas
    assert r.status_code == 400 and "melebihi batas 20%" in r.json()["detail"]
    r = c.post("/api/us/orders", json={"symbol": "MSFT", "side": "BUY", "notional": 0.5})
    assert r.status_code == 400 and "minimal $1" in r.json()["detail"]
    assert c.post("/api/us/orders", json={"symbol": "ZZZZ", "side": "BUY", "quantity": 1}).status_code == 502  # harga tidak ada
    c.post("/api/us/orders", json={"symbol": "AAPL", "side": "BUY", "quantity": 10})
    assert c.post("/api/us/paper/reset").json()["cash"] == 100000.0
    assert c.get("/api/us/orders").json() == []


def test_account_persists_across_restart(tmp_path):
    c, *_ = make(tmp_path)
    c.post("/api/us/orders", json={"symbol": "AAPL", "side": "BUY", "quantity": 10})
    c2, *_ = make(tmp_path)
    a = c2.get("/api/us/account").json()
    assert a["cash"] == 97700.0 and a["positions"][0]["symbol"] == "AAPL" and len(c2.get("/api/us/orders").json()) == 1


def test_price_alert_uses_finnhub(env):
    c, app, fake, tg = env
    assert c.post("/api/alerts", json={"symbol": "AAPL", "market": "us", "target": 240}).status_code == 200
    fake.prices["AAPL"] = 241.0
    app.state.us["provider"]._cache.clear()
    assert len(c.post("/api/alerts/check").json()["fired"]) == 1
    assert "AAPL naik tembus $240,00" in tg.sent[-1]["text"]
