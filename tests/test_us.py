import pytest
from fastapi.testclient import TestClient

from app.alpaca import AlpacaAPI, AlpacaProvider, normalize_us, parse_ts, us_market_open
from app.config import Settings
from app.main import create_app
from app.market_data import DemoProvider, MarketDataError
from tests.fake_alpaca import KEY, SECRET, URL, FakeAlpaca
from tests.test_notifier import TOKEN, FakeTelegram


def make(tmp_path, **kw):
    fake, tg = FakeAlpaca(), FakeTelegram()
    settings = Settings(data_dir=tmp_path, alpaca_data_url=URL, alpaca_paper_url=URL, alpaca_live_url=URL,
                        alpaca_paper_api_key=KEY, alpaca_paper_api_secret=SECRET, **kw)
    app = create_app(settings, DemoProvider(), telegram_http=tg.client(), alpaca_http=fake.client())
    app.state.watcher.sync_send = True
    c = TestClient(app)
    c.put("/api/notifications/config", json={"bot_token": TOKEN, "chat_id": "42"})
    return c, app, fake, tg


@pytest.fixture
def env(tmp_path):
    return make(tmp_path)


def test_symbol_and_time_helpers():
    assert normalize_us("nasdaq:aapl") == "AAPL" and normalize_us("BRK-B") == "BRK.B"
    assert normalize_us("BBCA.JK") == "" and normalize_us("TOOLONGX") == "" and normalize_us("BTCUSDT") == ""
    assert parse_ts("2026-10-06T13:30:00.123456789Z") == pytest.approx(1791293400.123456)
    assert us_market_open(1791293400 + 3600)        # Selasa 10:30 ET
    assert not us_market_open(1791293400 - 3600)    # 08:30 ET
    assert not us_market_open(1791293400 + 4 * 86400)  # Sabtu


def test_quote_and_chart(env):
    c, _, fake, _ = env
    q = c.get("/api/us/quote/aapl").json()
    assert q["symbol"] == "AAPL" and q["price"] == 230.0 and q["currency"] == "USD"
    assert q["change"] == 5.0 and q["change_pct"] == 2.22 and q["source"] == "alpaca-iex"
    assert fake.requests[0][2]["feed"] == "iex"
    assert c.get("/api/us/quote/BRK-B").json()["symbol"] == "BRK.B"
    assert c.get("/api/us/quote/BBCA.JK").status_code == 400
    assert c.get("/api/us/quote/ZZZZ").status_code == 502
    ch = c.get("/api/us/chart/AAPL?interval=5m&limit=100").json()
    assert len(ch["bars"]) == 100 and ch["bars"][0]["time"] < ch["bars"][-1]["time"]  # urut naik
    bar_req = [r for r in fake.requests if r[1].endswith("/bars")][-1][2]
    assert bar_req["timeframe"] == "5Min" and bar_req["sort"] == "desc" and bar_req["limit"] == "100"
    assert "analysis" in ch and ch["ema12"]
    assert c.get("/api/us/chart/AAPL?interval=4h").status_code == 400


def test_data_errors_are_clear(tmp_path):
    bad = AlpacaProvider(AlpacaAPI(URL, "x", "y", client=FakeAlpaca().client()))
    with pytest.raises(MarketDataError, match="request is not authorized"):
        bad.quote("AAPL")
    none = AlpacaProvider(AlpacaAPI(URL))
    with pytest.raises(MarketDataError, match="belum diatur"):
        none.quote("AAPL")


def test_config_and_unavailable_accounts(env):
    c, *_ = env
    cfg = c.get("/api/us/config").json()
    assert cfg["brokers"]["paper"]["available"] and not cfg["brokers"]["live"]["available"]
    assert cfg["data_feed"] == "iex" and cfg["market_open"] is True and cfg["market_clock_source"] == "alpaca"
    assert c.get("/api/us/account?broker=live").status_code == 503
    assert c.get("/api/us/account?broker=x").status_code == 404


def test_market_and_limit_orders_with_telegram(env):
    c, app, fake, tg = env
    r = c.post("/api/us/orders", json={"symbol": "AAPL", "side": "BUY", "quantity": 10})
    assert r.status_code == 200 and r.json()["status"] == "FILLED" and r.json()["fill_price"] == 230.0
    msg = tg.sent[-1]["text"]
    assert "Order manual — BELI AAPL" in msg and "10 saham @ $230,00 ($2.300,00)" in msg
    assert "Alpaca Paper Trading" in msg and "tradingview.com/symbols/AAPL" in msg
    post = next(r for r in fake.requests if r[0] == "POST")[3]
    assert post["side"] == "buy" and post["type"] == "market" and post["qty"] == "10" and post["client_order_id"].startswith("tv-")
    acct = c.get("/api/us/account").json()
    assert acct["cash"] == 97700.0 and acct["positions"][0]["symbol"] == "AAPL" and acct["positions"][0]["quantity"] == 10
    # fractional via nilai USD
    r = c.post("/api/us/orders", json={"symbol": "MSFT", "side": "BUY", "notional": 1000}).json()
    assert next(r for r in fake.requests if r[3] and r[3].get("symbol") == "MSFT")[3]["notional"] == "1000.00"
    assert r["status"] == "FILLED"
    # limit di bawah harga -> OPEN, terisi belakangan
    lim = c.post("/api/us/orders", json={"symbol": "AAPL", "side": "BUY", "quantity": 1, "order_type": "LIMIT",
                                         "limit_price": 200}).json()
    assert lim["status"] == "OPEN" and "⏳ Order limit dipasang: 1 saham @ $200,00" in tg.sent[-1]["text"]
    fake.prices["AAPL"] = 199.0
    fake.fill(lim["id"], 199.0)
    orders = c.get("/api/us/orders").json()
    assert orders[0]["status"] == "FILLED" and orders[0]["fill_price"] == 199.0
    assert "✅ Order limit terisi: 1 saham @ $199,00" in tg.sent[-1]["text"]
    n = len(tg.sent)
    c.get("/api/us/orders")
    assert len(tg.sent) == n  # tidak dikirim dua kali


def test_cancel_reject_and_limits(env):
    c, app, fake, tg = env
    lim = c.post("/api/us/orders", json={"symbol": "AAPL", "side": "BUY", "quantity": 1, "order_type": "LIMIT",
                                         "limit_price": 100}).json()
    d = c.delete(f"/api/us/orders/{lim['id']}")
    assert d.status_code == 200 and d.json()["status"] == "CANCELLED" and "🚫 Dibatalkan" in tg.sent[-1]["text"]
    assert c.delete(f"/api/us/orders/{lim['id']}").status_code == 400
    r = c.post("/api/us/orders", json={"symbol": "AAPL", "side": "SELL", "quantity": 5})
    assert r.status_code == 400 and "insufficient qty" in r.json()["detail"]
    assert "❌ Ditolak: 5 saham" in tg.sent[-1]["text"] and "insufficient qty" in tg.sent[-1]["text"]
    # batas risiko sebelum ke Alpaca (20% dari ekuitas 100.000 = 20.000)
    n = len(fake.requests)
    r = c.post("/api/us/orders", json={"symbol": "AAPL", "side": "BUY", "quantity": 1000})
    assert r.status_code == 400 and "melebihi batas 20%" in r.json()["detail"]
    assert not any(q[0] == "POST" and q[1] == "/v2/orders" for q in fake.requests[n:])
    assert c.post("/api/us/orders", json={"symbol": "AAPL", "side": "BUY"}).status_code == 400
    assert c.post("/api/us/orders", json={"symbol": "AAPL", "side": "BUY", "quantity": 1,
                                          "order_type": "LIMIT"}).status_code == 400
    assert c.post("/api/us/orders", json={"symbol": "BBCA.JK", "side": "BUY", "quantity": 1}).status_code == 400


def test_market_closed_order_waits(env):
    c, _, fake, tg = env
    fake.is_open = False
    o = c.post("/api/us/orders", json={"symbol": "AAPL", "side": "BUY", "quantity": 1}).json()
    assert o["status"] == "OPEN" and "menunggu bursa AS buka" in tg.sent[-1]["text"]


def test_notifications_can_be_turned_off(env):
    c, _, _, tg = env
    c.put("/api/notifications/config", json={"notify_orders": False})
    c.post("/api/us/orders", json={"symbol": "AAPL", "side": "BUY", "quantity": 1})
    assert tg.sent == []


def test_live_account_is_locked(tmp_path):
    fake = FakeAlpaca()
    settings = Settings(data_dir=tmp_path, alpaca_data_url=URL, alpaca_paper_url=URL, alpaca_live_url=URL,
                        alpaca_paper_api_key=KEY, alpaca_paper_api_secret=SECRET,
                        alpaca_live_api_key=KEY, alpaca_live_api_secret=SECRET, us_max_order_usd=500)
    body = {"broker": "live", "symbol": "AAPL", "side": "BUY", "quantity": 1}
    c = TestClient(create_app(settings, DemoProvider(), alpaca_http=fake.client()))
    assert c.post("/api/us/orders", json={**body, "confirm_live": True}).status_code == 403
    live = Settings(**{**settings.__dict__, "enable_live_trading": True})
    c = TestClient(create_app(live, DemoProvider(), alpaca_http=fake.client()))
    assert c.post("/api/us/orders", json=body).status_code == 400  # butuh konfirmasi
    r = c.post("/api/us/orders", json={**body, "quantity": 3, "confirm_live": True})
    assert r.status_code == 400 and "melebihi batas akun live" in r.json()["detail"]
    assert not any(q[0] == "POST" for q in fake.requests)
    assert c.post("/api/us/orders", json={**body, "confirm_live": True}).status_code == 200
