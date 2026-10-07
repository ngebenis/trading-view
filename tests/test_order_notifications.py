import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.market_data import DemoProvider
from tests.fake_binance import KEY, SECRET, FakeBinance
from tests.test_notifier import TOKEN, FakeTelegram

URL = "https://fake.binance"


@pytest.fixture
def app_env(tmp_path):
    tg, binance = FakeTelegram(), FakeBinance()
    settings = Settings(data_dir=tmp_path, binance_data_url=URL, binance_testnet_url=URL,
                        binance_testnet_api_key=KEY, binance_testnet_api_secret=SECRET)
    app = create_app(settings, DemoProvider(), telegram_http=tg.client(), binance_http=binance.client())
    app.state.watcher.sync_send = True
    c = TestClient(app)
    c.put("/api/notifications/config", json={"bot_token": TOKEN, "chat_id": "42"})
    return c, app, tg, binance


def texts(tg):
    return [m["text"] for m in tg.sent]


def test_stock_market_order_rejection_and_history(app_env):
    c, app, tg, _ = app_env
    r = c.post("/api/orders", json={"symbol": "BBCA", "side": "BUY", "lots": 2})
    assert r.status_code == 200
    msg = texts(tg)[-1]
    assert "Order manual — BELI BBCA" in msg and "✅ Terisi: 2 lot @" in msg and "Posisi: 2 lot" in msg
    assert "akun Paper Trading (Simulasi)" in msg and "fee Rp" in msg
    r = c.post("/api/orders", json={"symbol": "TLKM", "side": "SELL", "lots": 1})
    assert r.status_code == 400
    assert "❌ Ditolak: 1 lot" in texts(tg)[-1] and "Alasan: Saham TLKM tidak cukup" in texts(tg)[-1]
    hist = c.get("/api/notifications").json()["history"]
    assert [h["kind"] for h in hist[:2]] == ["ORDER", "ORDER"] and hist[0]["sent"]
    assert hist[0]["message"] == "Order manual JUAL ditolak" and hist[1]["message"] == "Order manual BELI terisi"
    # Penolakan sebelum ke broker (mis. batas risiko) tidak membuat order -> tidak ada pesan.
    n = len(tg.sent)
    assert c.post("/api/orders", json={"symbol": "BBCA", "side": "BUY", "lots": 100000}).status_code == 400
    assert len(tg.sent) == n


def test_stock_limit_open_fill_later_and_cancel(app_env):
    c, app, tg, _ = app_env
    price = c.get("/api/quote/BBCA").json()["price"]
    low = price - 50
    o = c.post("/api/orders", json={"symbol": "BBCA", "side": "BUY", "lots": 1, "order_type": "LIMIT",
                                    "limit_price": low}).json()
    assert o["status"] == "OPEN" and "⏳ Order limit dipasang: 1 lot @" in texts(tg)[-1]
    app.state.autotrader.broker.match_open_orders({"BBCA": low})  # harga turun menyentuh limit
    assert "✅ Order limit terisi: 1 lot" in texts(tg)[-1] and "Posisi: 1 lot" in texts(tg)[-1]
    o2 = c.post("/api/orders", json={"symbol": "BBCA", "side": "BUY", "lots": 1, "order_type": "LIMIT",
                                     "limit_price": low}).json()
    c.delete(f"/api/orders/{o2['id']}")
    assert "🚫 Dibatalkan: 1 lot" in texts(tg)[-1]


def test_can_be_turned_off_and_bot_orders_excluded(app_env):
    c, app, tg, _ = app_env
    c.put("/api/notifications/config", json={"notify_orders": False})
    c.post("/api/orders", json={"symbol": "BBCA", "side": "BUY", "lots": 1})
    assert tg.sent == []
    c.put("/api/notifications/config", json={"notify_orders": True})
    app.state.watcher.notify_order({"symbol": "BBCA", "side": "BUY", "status": "FILLED", "source": "auto"},
                                   "placed", "idx", "Paper")
    assert tg.sent == []  # transaksi bot punya notifikasi sendiri (notify_trades)


def test_crypto_paper_and_testnet_orders(app_env):
    c, app, tg, binance = app_env
    r = c.post("/api/crypto/orders", json={"symbol": "BTCUSDT", "side": "BUY", "quote_amount": 100})
    assert r.status_code == 200
    msg = texts(tg)[-1]
    assert "Order manual — BELI BTCUSDT" in msg and "0,00153 BTC @ 65.000,00 USDT" in msg
    assert "akun Simulasi Crypto (USDT)" in msg and "Posisi: 0,00153 BTC" in msg and "Binance</a>" in msg
    lim = c.post("/api/crypto/orders", json={"broker": "testnet", "symbol": "BTCUSDT", "side": "BUY",
                                             "quantity": 0.001, "order_type": "LIMIT", "limit_price": 60000}).json()
    assert lim["status"] == "OPEN" and "⏳ Order limit dipasang" in texts(tg)[-1] and "Binance Spot Testnet" in texts(tg)[-1]
    binance.fill_open(lim["id"], 60000)
    c.get("/api/crypto/account?broker=testnet")  # sinkron status order dari Binance
    # Status order yang diambil ulang dari Binance tidak memuat rincian fee, jadi jumlahnya jumlah tereksekusi.
    assert "✅ Order limit terisi: 0,001 BTC @ 60.000,00 USDT (60,00 USDT)" in texts(tg)[-1]
    lim2 = c.post("/api/crypto/orders", json={"broker": "testnet", "symbol": "BTCUSDT", "side": "BUY",
                                              "quantity": 0.001, "order_type": "LIMIT", "limit_price": 50000}).json()
    c.delete(f"/api/crypto/orders/{lim2['id']}?broker=testnet")
    assert "🚫 Dibatalkan" in texts(tg)[-1]
    binance.reject_next = (-2010, "Account has insufficient balance for requested action.")
    c.post("/api/crypto/orders", json={"broker": "testnet", "symbol": "BTCUSDT", "side": "BUY", "quantity": 0.01})
    assert "❌ Ditolak" in texts(tg)[-1] and "insufficient balance" in texts(tg)[-1]


def test_sent_in_background_by_default(tmp_path):
    tg = FakeTelegram()
    app = create_app(Settings(data_dir=tmp_path), DemoProvider(), telegram_http=tg.client())
    c = TestClient(app)
    c.put("/api/notifications/config", json={"bot_token": TOKEN, "chat_id": "42"})
    c.post("/api/orders", json={"symbol": "BBCA", "side": "BUY", "lots": 1})
    app.state.watcher._executor.shutdown(wait=True)
    assert len(tg.sent) == 1
