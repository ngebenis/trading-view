from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.autotrader import WIB
from app.brokers import Order, OrderType, PaperBroker, Side
from app.chart_data import build_chart, order_markers, snap_to_bar, wib_day
from app.config import Settings
from app.main import create_app
from app.market_data import Candle, DemoProvider


def ts(y, m, d, h=10):
    return datetime(y, m, d, h, tzinfo=WIB).timestamp()


class Clock:
    def __init__(self):
        self.t = ts(2026, 10, 5)

    def __call__(self):
        return self.t


def fill(broker, clock, when, symbol, side, lots, price, source="manual"):
    clock.t = when
    broker.place_order(Order(symbol, side, lots, OrderType.MARKET, source=source, created_at=when), price)


DAYS = ["2026-09-30", "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06"]  # tanpa akhir pekan 3-4 Okt


def test_wib_day_and_snap():
    assert wib_day(ts(2026, 10, 5, 0)) == "2026-10-05"   # 00:00 WIB masih tgl 5 (UTC tgl 4)
    assert snap_to_bar("2026-10-04", DAYS) == "2026-10-02"  # Minggu -> Jumat
    assert snap_to_bar("2026-10-06", DAYS) == "2026-10-06"
    assert snap_to_bar("2026-09-01", DAYS) is None


def test_markers_grouped_per_day_and_side():
    clock = Clock()
    b = PaperBroker(None, 1e9, 0.15, 0.25, clock=clock)
    fill(b, clock, ts(2026, 10, 1, 9), "BBCA", Side.BUY, 2, 9000)
    fill(b, clock, ts(2026, 10, 1, 14), "BBCA", Side.BUY, 3, 9100, source="auto")
    fill(b, clock, ts(2026, 10, 4), "BBCA", Side.SELL, 1, 9300, source="webhook")  # Minggu
    fill(b, clock, ts(2026, 10, 5), "TLKM", Side.BUY, 1, 3000)                     # saham lain
    b.place_order(Order("BBCA", Side.BUY, 1, OrderType.LIMIT, 8000), 9000)          # OPEN: bukan penanda
    m = order_markers(b, "bbca", DAYS)
    assert m == [
        {"time": "2026-10-01", "side": "BUY", "lots": 5, "count": 2, "avg_price": 9060.0, "sources": ["auto", "manual"]},
        {"time": "2026-10-02", "side": "SELL", "lots": 1, "count": 1, "avg_price": 9300.0, "sources": ["webhook"]},
    ]
    assert order_markers(b, "BBCA", ["2026-10-02"])[0]["time"] == "2026-10-02"  # BUY 1 Okt di luar rentang
    assert len(order_markers(b, "BBCA", ["2026-10-02"])) == 1


class DupProvider:
    """Candle intraday hari berjalan bisa memberi tanggal ganda; yang terakhir dipakai."""
    def candles(self, symbol, range_="1y", interval="1d"):
        out = [Candle(int(ts(2026, 9, d, 9)), 100 + d, 110 + d, 90 + d, 100 + d, 1000) for d in range(1, 30)]
        out.append(Candle(int(ts(2026, 9, 29, 15)), 1, 2, 0.5, 1.5, 7))
        return out


def test_build_chart_dedupes_and_ema():
    c = build_chart(DupProvider(), PaperBroker(None, 1e9, 0.15, 0.25), "x")
    assert len(c["bars"]) == 29 and c["bars"][-1]["close"] == 1.5 and c["bars"][-1]["volume"] == 7
    assert [b["time"] for b in c["bars"]] == sorted(b["time"] for b in c["bars"])
    assert len(c["ema12"]) == 29 - 11 and len(c["ema26"]) == 29 - 25
    assert c["position"] is None and c["markers"] == []


def test_api(tmp_path):
    client = TestClient(create_app(Settings(data_dir=tmp_path), DemoProvider()))
    client.post("/api/orders", json={"symbol": "BBCA", "side": "BUY", "lots": 2})
    c = client.get("/api/chart/BBCA?range=6mo").json()
    assert len(c["bars"]) == 130 and c["markers"][0]["side"] == "BUY" and c["markers"][0]["lots"] == 2
    assert c["markers"][0]["time"] == c["bars"][-1]["time"]  # order hari ini ada di candle terakhir
    assert c["position"]["shares"] == 200
    assert client.get("/api/chart/BBCA?range=10y").status_code == 400
    assert client.get("/api/chart/IHSG").json()["markers"] == []
