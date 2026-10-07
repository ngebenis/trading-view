from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.binance import (BinanceAPI, BinanceError, BinanceProvider, SymbolRules, fmt_decimal, is_crypto_pair,
                         normalize_pair, split_pair)
from app.brokers.base import BrokerError, OrderStatus, OrderType, Side
from app.config import Settings
from app.crypto import BinanceBroker, CryptoAutoTrader, CryptoOrder, CryptoPaperBroker, bot_positions
from app.db import Database
from app.main import create_app
from app.market_data import DemoProvider, MarketDataError
from tests.fake_binance import EXCHANGE_INFO, KEY, NOW_MS, SECRET, FakeBinance

URL = "https://fake.binance"


@pytest.fixture
def fake():
    return FakeBinance()


@pytest.fixture
def provider(fake):
    return BinanceProvider(BinanceAPI(URL, client=fake.client()), quote_ttl=0, kline_ttl=0)


def rules(sym="BTCUSDT"):
    return SymbolRules.from_exchange_info(EXCHANGE_INFO[sym])


def make_testnet(fake, db=None, key=KEY, secret=SECRET):
    api = BinanceAPI(URL, key, secret, client=fake.client(), clock=lambda: NOW_MS / 1000)
    return BinanceBroker(api, db, live=False)


# ---- simbol & aturan ---------------------------------------------------------
def test_pair_helpers():
    assert normalize_pair(" btc/usdt ") == "BTCUSDT" == normalize_pair("BINANCE:BTCUSDT") == normalize_pair("btc-usdt")
    assert split_pair("ETHBTC") == ("ETH", "BTC") and split_pair("BTCFDUSD") == ("BTC", "FDUSD")
    assert is_crypto_pair("SOLUSDT") and not is_crypto_pair("BBCA") and not is_crypto_pair("USDT")


def test_symbol_rules():
    r = rules()
    assert (fmt_decimal(r.step), r.min_notional) == ("0.00001", 5.0)
    assert fmt_decimal(r.floor_qty(0.123456789)) == "0.12345"
    assert fmt_decimal(r.round_price(65000.123)) == "65000.12"
    assert fmt_decimal(Decimal("100.000")) == "100"
    assert r.check(Decimal("0.00001"), 65000) == "Nilai order minimal 5 USDT"
    assert "Jumlah minimal" in r.check(Decimal("0"), 65000)
    assert r.check(Decimal("0.0001"), 65000) is None


# ---- API & provider ----------------------------------------------------------
def test_provider_quote_candles_rules(provider):
    q = provider.quote("btc/usdt")
    assert (q.symbol, q.price, q.currency, q.source, q.market_time) == ("BTCUSDT", 65000, "USDT", "binance", NOW_MS / 1000)
    assert round(q.change_pct, 2) == 2.0
    c = provider.candles("BTCUSDT", None, "1h", 50)
    assert len(c) == 50 and c[-1].close == 65000
    assert provider.rules("BTCUSDT").base == "BTC"
    assert provider.all_prices()["ETHUSDT"] == 3200
    with pytest.raises(MarketDataError, match="tidak ada di Binance"):
        provider.quote("XXXUSDT")
    with pytest.raises(MarketDataError, match="Interval"):
        provider.candles("BTCUSDT", None, "2h")


def test_signed_requests_and_time_resync(fake):
    api = BinanceAPI(URL, KEY, SECRET, client=fake.client(), clock=lambda: NOW_MS / 1000)
    assert {b["asset"] for b in api.signed("GET", "/api/v3/account")["balances"]} == {"USDT", "BTC", "BNB"}
    method, path, q = fake.requests[-1]
    assert q["timestamp"] == str(NOW_MS) and q["recvWindow"] == "5000"
    with pytest.raises(BinanceError, match="Signature"):
        BinanceAPI(URL, KEY, "salah", client=fake.client()).signed("GET", "/api/v3/account")
    with pytest.raises(BinanceError, match="belum diatur"):
        BinanceAPI(URL, client=fake.client()).signed("GET", "/api/v3/account")
    # -1021: jam lokal meleset -> sinkronkan dengan /api/v3/time lalu ulangi sekali.
    fake.reject_next = (-1021, "Timestamp for this request is outside of the recvWindow.")
    api.signed("POST", "/api/v3/order", {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET",
                                         "quantity": "0.001", "newClientOrderId": "abc"})
    assert [r[1] for r in fake.requests[-3:]] == ["/api/v3/order", "/api/v3/time", "/api/v3/order"]


# ---- akun simulasi -----------------------------------------------------------
def test_paper_broker_orders_and_account(tmp_path):
    db = Database(tmp_path / "app.db")
    b = CryptoPaperBroker(db, 1000, 0.1)
    o = b.place_order(CryptoOrder("BTCUSDT", Side.BUY, 0.0012345, OrderType.MARKET), 65000, rules())
    assert (o.status, o.quantity, o.fill_price) == (OrderStatus.FILLED, 0.00123, 65000)
    assert round(b.cash, 4) == round(1000 - 0.00123 * 65000 * 1.001, 4)
    with pytest.raises(BrokerError, match="minimal 5 USDT"):
        b.place_order(CryptoOrder("BTCUSDT", Side.BUY, 0.00005, OrderType.MARKET), 65000, rules())
    with pytest.raises(BrokerError, match="tidak cukup"):
        b.place_order(CryptoOrder("BTCUSDT", Side.BUY, 1, OrderType.MARKET), 65000, rules())
    with pytest.raises(BrokerError, match="BTC tidak cukup"):
        b.place_order(CryptoOrder("BTCUSDT", Side.SELL, 0.01, OrderType.MARKET), 65000, rules())
    with pytest.raises(BrokerError, match="USDT"):
        b.place_order(CryptoOrder("ETHBTC", Side.BUY, 1, OrderType.MARKET), 0.05, rules("ETHBTC"))
    lim = b.place_order(CryptoOrder("BTCUSDT", Side.SELL, 0.00123, OrderType.LIMIT, 70000.004), 65000, rules())
    assert (lim.status, lim.limit_price) == (OrderStatus.OPEN, 70000.0)
    assert b.free_quantity("BTCUSDT") == pytest.approx(0)
    acct = b.account({"BTCUSDT": 68000})
    assert acct["positions"][0]["unrealized_pl"] == pytest.approx(0.00123 * 3000)
    assert b.match_open_orders({"BTCUSDT": 70100})[0].fill_price == 70000
    assert b.positions == {}
    again = CryptoPaperBroker(db, 1000, 0.1)  # tersimpan di SQLite
    assert again.cash == pytest.approx(b.cash) and len(again.orders()) == len(b.orders())
    again.reset()
    assert again.cash == 1000 and again.orders() == []
    assert CryptoPaperBroker(db, 1000, 0.1).orders() == []


def test_bot_positions_only_count_auto_orders():
    def filled(side, qty, price, source="auto", t=1.0):
        o = CryptoOrder("BTCUSDT", side, qty, OrderType.MARKET, source=source)
        o.status, o.filled_qty, o.fill_price, o.filled_at = OrderStatus.FILLED, qty, price, t
        return o
    orders = [filled(Side.BUY, 1, 100, t=1), filled(Side.BUY, 1, 200, t=2), filled(Side.BUY, 5, 50, "manual", t=3),
              filled(Side.SELL, 0.5, 300, t=4)]
    pos = bot_positions(orders)["BTCUSDT"]
    assert pos["quantity"] == 1.5 and pos["avg_price"] == 150
    assert bot_positions(orders + [filled(Side.SELL, 1.5, 300, t=5)]) == {}


# ---- Binance (testnet) -------------------------------------------------------
def test_binance_broker_market_limit_cancel_and_reject(fake, tmp_path):
    db = Database(tmp_path / "app.db")
    b = make_testnet(fake, db)
    o = b.place_order(CryptoOrder("BTCUSDT", Side.BUY, 0.0012345, OrderType.MARKET, source="auto"), 65000, rules())
    sent = fake.requests[-1][2]
    assert (sent["quantity"], sent["newOrderRespType"], sent["newClientOrderId"]) == ("0.00123", "FULL", o.id)
    assert o.status == OrderStatus.FILLED and o.fill_price == pytest.approx(65000)
    assert (o.fee_asset, o.filled_qty) == ("BTC", pytest.approx(0.00123 * 0.999))  # fee dipotong dari BTC
    assert b.bot_positions()["BTCUSDT"]["quantity"] == pytest.approx(0.00123 * 0.999)

    lim = b.place_order(CryptoOrder("BTCUSDT", Side.BUY, 0.001, OrderType.LIMIT, 60000.005), 65000, rules())
    assert lim.status == OrderStatus.OPEN and fake.requests[-1][2]["price"] == "60000.01"
    fake.fill_open(lim.id, 60000.01)
    assert [x.id for x in b.refresh_open()] == [lim.id]
    lim2 = b.place_order(CryptoOrder("BTCUSDT", Side.BUY, 0.001, OrderType.LIMIT, 50000), 65000, rules())
    assert b.cancel_order(lim2.id).status == OrderStatus.CANCELLED

    fake.reject_next = (-2010, "Account has insufficient balance for requested action.")
    with pytest.raises(BrokerError, match="insufficient balance"):
        b.place_order(CryptoOrder("BTCUSDT", Side.BUY, 0.01, OrderType.MARKET), 65000, rules())
    assert b.orders()[0].status == OrderStatus.REJECTED
    assert len(make_testnet(fake, db).orders()) == 4  # riwayat tersimpan

    acct = b.account({"BTCUSDT": 66000, "BNBUSDT": 600})
    btc = next(p for p in acct["positions"] if p["asset"] == "BTC")
    assert btc["quantity"] == pytest.approx(fake.balances["BTC"])
    assert btc["bot_quantity"] == pytest.approx(0.00123 * 0.999) and btc["avg_price"] == pytest.approx(65000)
    assert acct["cash"] == pytest.approx(fake.balances["USDT"])


def test_binance_broker_unconfigured(fake):
    b = make_testnet(fake, key="", secret="")
    assert not b.available() and "BINANCE_TESTNET_API_KEY" in b.status()["note"]
    with pytest.raises(BrokerError):
        b.place_order(CryptoOrder("BTCUSDT", Side.BUY, 0.001, OrderType.MARKET), 65000, rules())
    assert not any(r[1] == "/api/v3/order" for r in fake.requests)


# ---- auto-trading crypto -----------------------------------------------------
def make_bot(fake, provider, target="paper", live_allowed=False, **cfg):
    brokers = {"paper": CryptoPaperBroker(None, 1000, 0.1), "testnet": make_testnet(fake), "binance": make_testnet(fake)}
    bot = CryptoAutoTrader(brokers, provider, None, 20, live_allowed, clock=lambda: NOW_MS / 1000)
    bot.update_config({"broker": target, "symbols": ["BTCUSDT"], "max_order_usdt": 100, "cooldown_minutes": 0, **cfg})
    return bot


def test_bot_buys_on_signal_and_sells_on_signal(fake, provider):
    bot = make_bot(fake, provider)
    fake.set_wave("BTCUSDT", 42, 65000)
    log = bot.run_cycle()
    trade = next(e for e in log if e["level"] == "TRADE")
    assert trade["side"] == "BUY" and "sinyal BELI" in trade["message"] and "BTC @" in trade["message"]
    assert trade["title"] == "Auto-trading crypto (Simulasi Crypto (USDT))"
    pos = bot.target.bot_positions()["BTCUSDT"]
    assert pos["quantity"] * pos["avg_price"] <= 100  # dibatasi max_order_usdt
    fake.set_wave("BTCUSDT", 52, 65000)
    bot.update_config({"take_profit_pct": 0, "stop_loss_pct": 0})
    sell = [e for e in bot.run_cycle() if e["level"] == "TRADE"]
    assert sell and sell[0]["side"] == "SELL" and "sinyal JUAL" in sell[0]["message"]
    assert bot.target.bot_positions() == {}


def test_bot_stop_loss_and_budget_too_small(fake, provider):
    bot = make_bot(fake, provider)
    fake.set_wave("BTCUSDT", 42, 65000)
    bot.run_cycle()
    fake.series.pop("BTCUSDT")
    fake.prices["BTCUSDT"] *= 0.9
    log = bot.run_cycle()
    assert any("stop-loss" in e["message"] for e in log)
    small = make_bot(fake, provider, max_order_usdt=2)
    fake.set_wave("BTCUSDT", 42, 65000)
    assert any("terlalu kecil" in e["message"] for e in small.run_cycle())


def test_bot_never_sells_holdings_it_did_not_buy(fake, provider):
    bot = make_bot(fake, provider, target="testnet", take_profit_pct=0, stop_loss_pct=0)
    fake.set_wave("BTCUSDT", 52, 65000)  # sinyal JUAL, akun testnet punya 0,5 BTC milik pengguna
    before = fake.balances["BTC"]
    log = bot.run_cycle()
    assert not any(e["level"] == "TRADE" for e in log) and fake.balances["BTC"] == before


def test_bot_config_rules(fake, provider):
    with pytest.raises(ValueError, match="ENABLE_LIVE_TRADING"):
        make_bot(fake, provider, target="binance")
    with pytest.raises(ValueError, match="batas nilai per order"):
        make_bot(fake, provider, target="binance", live_allowed=True, max_order_usdt=0)
    with pytest.raises(ValueError, match="hanya untuk pasangan …USDT"):
        make_bot(fake, provider, symbols=["ETHBTC"])
    with pytest.raises(ValueError, match="Candle"):
        make_bot(fake, provider, candle_interval="2h")
    bot = make_bot(fake, provider, target="testnet")
    bot.brokers["testnet"].api.api_key = ""
    assert "belum bisa dipakai" in bot.run_cycle()[0]["message"]


# ---- API ---------------------------------------------------------------------
@pytest.fixture
def client(tmp_path, fake):
    settings = Settings(data_dir=tmp_path, binance_data_url=URL, binance_testnet_url=URL,
                        binance_testnet_api_key=KEY, binance_testnet_api_secret=SECRET, crypto_paper_starting_cash=1000)
    return TestClient(create_app(settings, DemoProvider(), binance_http=fake.client()))


def test_api_quote_order_account_chart(client):
    cfg = client.get("/api/crypto/config").json()
    assert cfg["brokers"]["testnet"]["available"] and not cfg["brokers"]["binance"]["available"]
    q = client.get("/api/crypto/quote/btc-usdt").json()
    assert (q["symbol"], q["price"], q["base"], q["rules"]["step_size"]) == ("BTCUSDT", 65000, "BTC", "0.00001")
    assert client.get("/api/crypto/quote/BBCA").status_code == 400
    assert client.get("/api/crypto/quote/XXXUSDT").status_code == 502
    r = client.post("/api/crypto/orders", json={"symbol": "BTCUSDT", "side": "BUY", "quote_amount": 100})
    assert r.status_code == 200 and r.json()["quantity"] == 0.00153  # 100/65000 dibulatkan ke stepSize
    assert client.post("/api/crypto/orders", json={"symbol": "BTCUSDT", "side": "BUY", "quote_amount": 500}).status_code == 400
    acct = client.get("/api/crypto/account").json()
    assert acct["positions"][0]["symbol"] == "BTCUSDT" and acct["quote_asset"] == "USDT"
    chart = client.get("/api/crypto/chart/BTCUSDT?interval=1h&limit=50").json()
    assert len(chart["bars"]) == 50 and chart["bars"][-1]["time"] == NOW_MS // 1000 - 3600 + 7 * 3600
    assert chart["analysis"]["action"] in ("BUY", "SELL", "HOLD")
    assert client.post("/api/crypto/orders", json={"symbol": "BTCUSDT", "side": "BUY", "quantity": 0.001,
                                                   "order_type": "LIMIT"}).status_code == 400
    tn = client.post("/api/crypto/orders", json={"broker": "testnet", "symbol": "BTCUSDT", "side": "BUY", "quantity": 0.001})
    assert tn.status_code == 200 and tn.json()["status"] == "FILLED"
    assert client.get("/api/crypto/account?broker=testnet").json()["cash"] == pytest.approx(1000 - 65)
    assert len(client.get("/api/crypto/orders?broker=testnet").json()) == 1


def test_api_live_gating_and_bot(client):
    live = {"broker": "binance", "symbol": "BTCUSDT", "side": "BUY", "quantity": 0.001}
    assert client.post("/api/crypto/orders", json=live).status_code == 403  # ENABLE_LIVE_TRADING=false
    assert client.put("/api/crypto/autotrader/config", json={"broker": "binance"}).status_code == 400
    assert client.post("/api/crypto/orders", json={**live, "broker": "lain"}).status_code == 404
    st = client.put("/api/crypto/autotrader/config", json={"symbols": ["btc/usdt"], "candle_interval": "4h"}).json()
    assert st["config"]["symbols"] == ["BTCUSDT"] and set(st["targets"]) == {"paper", "testnet", "binance"}
    st = client.post("/api/crypto/autotrader/run-once").json()
    assert st["log"][0]["message"].startswith("Tidak ada aksi")
    # Lewat tunnel, API crypto tidak bisa diakses.
    assert client.get("/api/crypto/account", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 403


def test_api_live_orders_need_flag_and_confirmation(tmp_path, fake):
    settings = Settings(data_dir=tmp_path, binance_data_url=URL, binance_api_url=URL, binance_api_key=KEY,
                        binance_api_secret=SECRET, enable_live_trading=True)
    c = TestClient(create_app(settings, DemoProvider(), binance_http=fake.client()))
    live = {"broker": "binance", "symbol": "BTCUSDT", "side": "BUY", "quantity": 0.001}
    assert c.post("/api/crypto/orders", json=live).status_code == 400
    assert c.post("/api/crypto/orders", json={**live, "confirm_live": True}).json()["status"] == "FILLED"
    c.put("/api/crypto/autotrader/config", json={"broker": "binance", "max_order_usdt": 20})
    assert c.post("/api/crypto/autotrader/start", json={}).status_code == 400
    assert c.post("/api/crypto/autotrader/run-once").status_code == 400
    assert c.post("/api/crypto/autotrader/start", json={"confirm_live": True}).json()["running"]
    c.post("/api/crypto/autotrader/stop")
