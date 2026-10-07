import pytest
from fastapi.testclient import TestClient

from app.autotrader import AutoTraderConfig
from app.backtest import BacktestRequest, run_backtest
from app.brokers import BrokerError, Order, OrderType, PaperBroker, Side
from app.config import Settings
from app.idx_rules import is_index, normalize_symbol, tradingview_symbol, tradingview_url, yahoo_symbol
from app.main import create_app
from app.market_data import DemoProvider, Quote
from app.notifier import WatchConfig, format_signal


@pytest.mark.parametrize("raw", ["IHSG", "ihsg", "^JKSE", "JKSE", "IDX:COMPOSITE", "COMPOSITE", "JCI"])
def test_ihsg_aliases(raw):
    assert normalize_symbol(raw) == "IHSG" and is_index(raw)


def test_index_symbol_mapping():
    assert yahoo_symbol("IHSG") == "^JKSE" and tradingview_symbol("IHSG") == "IDX:COMPOSITE"
    assert yahoo_symbol("lq45") == "^JKLQ45" and tradingview_symbol("LQ45") == "IDX:LQ45"
    assert tradingview_url("IHSG") == "https://www.tradingview.com/symbols/IDX-COMPOSITE/"
    assert yahoo_symbol("BBCA") == "BBCA.JK" and not is_index("BBCA")


def test_index_cannot_be_traded(tmp_path):
    broker = PaperBroker(None, 100_000_000, 0.15, 0.25)
    with pytest.raises(BrokerError, match="indeks"):
        broker.place_order(Order("^JKSE", Side.BUY, 1, OrderType.MARKET), 7000)
    cfg = AutoTraderConfig(symbols=["BBCA", "IHSG"])
    with pytest.raises(ValueError, match="IHSG adalah indeks"):
        cfg.validate(20)
    with pytest.raises(ValueError, match="indeks"):
        run_backtest(BacktestRequest(["LQ45"]), DemoProvider(), AutoTraderConfig(), 0.15, 0.25, 20)


def test_index_allowed_in_signal_watch():
    cfg = WatchConfig(symbols=["^JKSE", "BBCA"])
    cfg.validate()
    assert cfg.symbols == ["BBCA", "IHSG"]


def test_index_signal_message():
    text = format_signal("IHSG", "SELL", {"score": -2, "reasons": ["x"]}, Quote("IHSG", 7123.456, 7200, -76.5, -1.06))
    assert "SINYAL JUAL — IHSG" in text and "Nilai indeks: <b>7.123,46</b> (-1,06%)" in text
    assert "symbols/IDX-COMPOSITE/" in text


def test_api_index(tmp_path):
    client = TestClient(create_app(Settings(data_dir=tmp_path), DemoProvider()))
    q = client.get("/api/quote/^JKSE").json()
    assert q["symbol"] == "IHSG" and q["is_index"] is True and q["tick_size"] is None
    assert q["tradingview_symbol"] == "IDX:COMPOSITE" and 1000 < q["price"] < 50000
    assert client.get("/api/analysis/IHSG").json()["action"] in {"BUY", "SELL", "HOLD"}
    r = client.post("/api/orders", json={"symbol": "IHSG", "side": "BUY", "lots": 1})
    assert r.status_code == 400 and "indeks" in r.json()["detail"]
    r = client.put("/api/autotrader/config", json={"symbols": ["BBCA", "IHSG"]})
    assert r.status_code == 400 and "indeks" in r.json()["detail"]
    assert client.post("/api/backtest", json={"symbols": ["IHSG"]}).status_code == 400
    assert client.put("/api/notifications/config", json={"symbols": ["IHSG", "BBCA"]}).status_code == 200
