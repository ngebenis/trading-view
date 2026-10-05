import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.market_data import DemoProvider


@pytest.fixture
def client(tmp_path):
    settings = Settings(data_dir=tmp_path, paper_starting_cash=1_000_000_000, max_position_pct=20)
    return TestClient(create_app(settings, DemoProvider()))


def test_quote_candles_analysis(client):
    q = client.get("/api/quote/bbca").json()
    assert q["symbol"] == "BBCA" and q["tradingview_symbol"] == "IDX:BBCA"
    assert len(client.get("/api/candles/BBCA?range=3mo").json()) == 66
    a = client.get("/api/analysis/BBCA").json()
    assert a["action"] in {"BUY", "SELL", "HOLD"}


def test_paper_order_flow(client):
    r = client.post("/api/orders", json={"symbol": "BBCA", "side": "BUY", "lots": 1})
    assert r.status_code == 200 and r.json()["status"] == "FILLED"
    acct = client.get("/api/account").json()
    assert acct["positions"][0]["symbol"] == "BBCA"
    assert len(client.get("/api/orders").json()) == 1


def test_risk_limit(client):
    r = client.post("/api/orders", json={"symbol": "BBCA", "side": "BUY", "lots": 100000})
    assert r.status_code == 400 and "batas" in r.json()["detail"]


def test_live_brokers_blocked(client):
    brokers = {b["name"]: b for b in client.get("/api/brokers").json()}
    assert brokers["stockbit"]["available"] is False and brokers["pluang"]["is_live"] is True
    r = client.post("/api/orders", json={"broker": "stockbit", "symbol": "BBCA", "side": "BUY", "lots": 1})
    assert r.status_code == 403
    assert client.get("/api/account?broker=pluang").status_code == 503


def test_index_served(client):
    assert "IDX Trading View" in client.get("/").text
