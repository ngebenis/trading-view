import json
import threading

import pytest
from fastapi.testclient import TestClient

from app.autotrader import AutoTrader
from app.brokers import Order, OrderStatus, OrderType, PaperBroker, Side
from app.config import Settings
from app.db import SCHEMA_VERSION, Database
from app.main import create_app
from app.market_data import DemoProvider
from app.notifier import SignalWatcher
from app.webhooks import TradingViewWebhook


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "app.db")


def test_schema_and_settings(db, tmp_path):
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert db.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db.get_setting("x", {"default": 1}) == {"default": 1}
    db.set_setting("x", {"a": [1, 2], "b": "teks"})
    db.set_setting("x", {"a": [3]})
    assert Database(tmp_path / "app.db").get_setting("x") == {"a": [3]}  # koneksi baru, data sama


def test_paper_account_roundtrip_and_reset(db):
    b = PaperBroker(db, 10_000_000, 0.15, 0.25)
    b.place_order(Order("BBCA", Side.BUY, 2, OrderType.MARKET, source="auto"), 9000)
    limit = b.place_order(Order("TLKM", Side.BUY, 1, OrderType.LIMIT, 2800), 2900)
    b.cancel_order(limit.id)
    again = PaperBroker(db, 10_000_000, 0.15, 0.25)
    assert again.cash == pytest.approx(b.cash) and again.positions == b.positions
    assert [(o.symbol, o.status, o.source) for o in again.orders()] == \
        [("TLKM", OrderStatus.CANCELLED, "manual"), ("BBCA", OrderStatus.FILLED, "auto")]
    # Order hanya ditulis saat berubah: 2 baris, bukan seluruh riwayat setiap kali
    assert db.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 2
    # Reset memakai modal dari pengaturan terbaru (PAPER_STARTING_CASH), bukan nilai lama
    bigger = PaperBroker(db, 50_000_000, 0.15, 0.25)
    assert bigger.starting_cash == 10_000_000  # akun lama tetap dihitung dari modal awalnya
    bigger.reset()
    fresh = PaperBroker(db, 50_000_000, 0.15, 0.25)
    assert fresh.cash == 50_000_000 and fresh.orders() == [] and fresh.positions == {}


def test_concurrent_orders_are_all_saved(db):
    b = PaperBroker(db, 1e12, 0.15, 0.25)

    def worker():
        for _ in range(20):
            b.place_order(Order("BBCA", Side.BUY, 1, OrderType.MARKET), 9000)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    again = PaperBroker(db, 1e12, 0.15, 0.25)
    assert len(again.orders()) == 100 and again.positions["BBCA"]["shares"] == 100 * 100


def test_logs_persist_across_restart(db):
    t = AutoTrader(PaperBroker(None, 1e8, 0.15, 0.25), DemoProvider(), db, 20)
    t._log("INFO", "-", "pertama")
    t._log("TRADE", "BBCA", "BUY 1 lot", side="BUY", lots=1)
    t2 = AutoTrader(PaperBroker(None, 1e8, 0.15, 0.25), DemoProvider(), db, 20)
    assert [e["message"] for e in t2.log] == ["BUY 1 lot", "pertama"]
    assert t2.log[0]["level"] == "TRADE" and t2.log[0]["lots"] == 1
    first_id = t2.log[0]["id"]
    t2._log("INFO", "-", "ketiga")
    assert t2.log[0]["id"] > first_id  # id terus naik setelah restart

    w = SignalWatcher(DemoProvider(), db)
    w._record("BUY", "BBCA", "Sinyal BELI", sent=True)
    assert SignalWatcher(DemoProvider(), db).history[0]["sent"] is True

    h = TradingViewWebhook(PaperBroker(None, 1e8, 0.15, 0.25), DemoProvider(), db, 20)
    h._record("ORDER", "BBCA", "BUY 2 lot", order_id="abc")
    assert TradingViewWebhook(None, None, db, 20).log[0]["order_id"] == "abc"
    # aliran log terpisah
    assert {e["message"] for e in db.recent_logs("webhook")} == {"BUY 2 lot"}


def test_migrates_old_json_files_once(tmp_path):
    (tmp_path / "paper_account.json").write_text(json.dumps({
        "cash": 5_000_000.0,
        "positions": {"BBCA": {"shares": 300, "avg_price": 9000.0}},
        "orders": [{"symbol": "BBCA", "side": "BUY", "lots": 3, "order_type": "MARKET", "limit_price": None,
                    "id": "old1", "status": "FILLED", "fill_price": 9000, "fee": 40.5, "created_at": 1.0,
                    "filled_at": 1.0, "message": ""}],  # versi lama: belum ada field "source"
    }))
    (tmp_path / "autotrader.json").write_text(json.dumps({"enabled": False, "symbols": ["TLKM"], "interval_seconds": 120}))
    (tmp_path / "notifications.json").write_text(json.dumps({"chat_id": "42", "_last_action": {"BBCA": "BUY"}}))
    (tmp_path / "webhook.json").write_text(json.dumps({"secret": "s" * 32, "mode": "order"}))
    client = TestClient(create_app(Settings(data_dir=tmp_path), DemoProvider()))
    acct = client.get("/api/account").json()
    assert acct["cash"] == 5_000_000 and acct["positions"][0]["symbol"] == "BBCA"
    assert client.get("/api/orders").json()[0]["source"] == "manual"
    assert client.get("/api/autotrader").json()["config"]["symbols"] == ["TLKM"]
    assert client.get("/api/notifications").json()["last_action"] == {"BBCA": "BUY"}
    assert client.get("/api/webhooks").json()["config"]["secret"] == "s" * 32
    assert sorted(p.name for p in tmp_path.glob("*.migrated")) == [
        "autotrader.json.migrated", "notifications.json.migrated", "paper_account.json.migrated", "webhook.json.migrated"]
    # start berikutnya tidak mengimpor ulang (file lama sudah diganti nama)
    client.post("/api/orders", json={"symbol": "BBCA", "side": "SELL", "lots": 1})
    client2 = TestClient(create_app(Settings(data_dir=tmp_path), DemoProvider()))
    assert len(client2.get("/api/orders").json()) == 2


def test_backtest_history_api(tmp_path):
    client = TestClient(create_app(Settings(data_dir=tmp_path), DemoProvider()))
    r = client.post("/api/backtest", json={"symbols": ["BBCA", "TLKM"], "period": "6mo"}).json()
    assert isinstance(r["id"], int)
    client.post("/api/backtest", json={"symbols": ["ASII"], "period": "6mo", "strategy": {"min_buy_score": 3}})
    items = client.get("/api/backtests").json()
    assert [i["symbols"] for i in items] == [["ASII"], ["BBCA", "TLKM"]]  # terbaru dulu
    assert items[0]["strategy"]["min_buy_score"] == 3 and items[1]["total_return_pct"] == r["metrics"]["total_return_pct"]
    full = client.get(f"/api/backtests/{r['id']}").json()
    assert full["equity_curve"] == r["equity_curve"] and full["id"] == r["id"]
    assert client.delete(f"/api/backtests/{r['id']}").status_code == 200
    assert client.get(f"/api/backtests/{r['id']}").status_code == 404
    assert client.delete("/api/backtests/999").status_code == 404


def test_orders_limit(tmp_path):
    client = TestClient(create_app(Settings(data_dir=tmp_path), DemoProvider()))
    for _ in range(3):
        client.post("/api/orders", json={"symbol": "BBCA", "side": "BUY", "lots": 1})
    assert len(client.get("/api/orders?limit=2").json()) == 2
    assert client.get("/api/orders?limit=0").status_code == 422
