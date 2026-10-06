import math
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.autotrader import AutoTrader, WIB, is_idx_market_open
from app.brokers import PaperBroker
from app.config import Settings
from app.main import create_app
from app.market_data import Candle, DemoProvider, Quote

WAVE = [1000 + 200 * math.sin(i / 8) for i in range(60)]
BUY_SERIES, SELL_SERIES, FLAT = WAVE[:42], WAVE[:52], [1000.0] * 60


class FakeProvider:
    """Harga & histori yang bisa diatur per simbol."""
    name = "fake"

    def __init__(self):
        self.series: dict[str, list[float]] = {}
        self.price: dict[str, float] = {}

    def set(self, sym, closes, price=None):
        self.series[sym] = closes
        self.price[sym] = price if price is not None else closes[-1]

    def candles(self, symbol, range_="1y", interval="1d"):
        return [Candle(i, c, c, c, c, 0) for i, c in enumerate(self.series[symbol])]

    def quote(self, symbol):
        p = self.price[symbol]
        return Quote(symbol, p, p, 0, 0)


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    broker = PaperBroker(tmp_path / "acct.json", 100_000_000, 0.15, 0.25)
    provider, clock = FakeProvider(), Clock()
    trader = AutoTrader(broker, provider, tmp_path / "auto.json", max_position_pct=20, clock=clock)
    trader.update_config({"symbols": ["AAAA"], "position_pct": 10, "stop_loss_pct": 5,
                          "take_profit_pct": 10, "cooldown_minutes": 60})
    return trader, broker, provider, clock


def trades(entries):
    return [e for e in entries if e["level"] == "TRADE"]


def test_buys_on_buy_signal_with_position_sizing(env):
    trader, broker, provider, _ = env
    provider.set("AAAA", BUY_SERIES, price=1000)
    made = trades(trader.run_cycle())
    assert len(made) == 1 and made[0]["side"] == "BUY"
    # 10% dari Rp100jt = Rp10jt; 1 lot @1000 + fee = Rp100.150 -> 99 lot
    assert made[0]["lots"] == 99
    assert broker.positions["AAAA"]["shares"] == 9_900
    assert broker.orders()[0].source == "auto"
    # Siklus berikutnya tidak menambah posisi yang sama
    assert trades(trader.run_cycle()) == []


def test_no_trade_on_neutral_signal(env):
    trader, broker, provider, _ = env
    provider.set("AAAA", FLAT)
    out = trader.run_cycle()
    assert trades(out) == [] and broker.positions == {}
    assert out[0]["message"].startswith("Tidak ada aksi")


def test_sell_signal_respects_cooldown(env):
    trader, broker, provider, clock = env
    provider.set("AAAA", BUY_SERIES, price=1000)
    trader.run_cycle()
    provider.set("AAAA", SELL_SERIES, price=1020)  # +2%: belum kena TP/SL
    assert trades(trader.run_cycle()) == []  # masih cooldown
    clock.t += 3601
    made = trades(trader.run_cycle())
    assert made[0]["side"] == "SELL" and "sinyal JUAL" in made[0]["message"]
    assert broker.positions == {}


@pytest.mark.parametrize("price,reason", [(940, "stop-loss"), (1100, "take-profit")])
def test_stop_loss_and_take_profit_ignore_cooldown(env, price, reason):
    trader, broker, provider, _ = env
    provider.set("AAAA", BUY_SERIES, price=1000)
    trader.run_cycle()
    provider.set("AAAA", FLAT, price=price)
    made = trades(trader.run_cycle())
    assert made[0]["side"] == "SELL" and reason in made[0]["message"]
    assert broker.positions == {}


def test_max_positions(env):
    trader, broker, provider, _ = env
    trader.update_config({"symbols": ["AAAA", "BBBB"], "max_positions": 1})
    provider.set("AAAA", BUY_SERIES, price=1000)
    provider.set("BBBB", BUY_SERIES, price=1000)
    out = trader.run_cycle()
    assert len(trades(out)) == 1
    assert any("maks. 1 posisi" in e["message"] for e in out)


def test_config_validation_and_persistence(env, tmp_path):
    trader, broker, provider, clock = env
    with pytest.raises(ValueError, match="Ukuran posisi"):
        trader.update_config({"position_pct": 50})
    with pytest.raises(ValueError, match="Interval"):
        trader.update_config({"interval_seconds": 5})
    trader.update_config({"symbols": ["bbca.jk", "tlkm", ""]})
    reloaded = AutoTrader(broker, provider, tmp_path / "auto.json", 20, clock)
    assert reloaded.config.symbols == ["BBCA", "TLKM"]


def test_market_hours():
    assert is_idx_market_open(datetime(2026, 10, 5, 10, 0, tzinfo=WIB))       # Senin pagi
    assert not is_idx_market_open(datetime(2026, 10, 5, 12, 30, tzinfo=WIB))  # jeda siang
    assert not is_idx_market_open(datetime(2026, 10, 9, 13, 45, tzinfo=WIB))  # Jumat jeda
    assert not is_idx_market_open(datetime(2026, 10, 10, 10, 0, tzinfo=WIB))  # Sabtu
    assert not is_idx_market_open(datetime(2026, 10, 5, 16, 0, tzinfo=WIB))   # setelah tutup


def test_market_hours_only_skips_cycle(env):
    trader, broker, provider, clock = env
    trader.update_config({"market_hours_only": True})
    clock.t = datetime(2026, 10, 10, 10, 0, tzinfo=WIB).timestamp()  # Sabtu
    provider.set("AAAA", BUY_SERIES, price=1000)
    out = trader.run_cycle()
    assert "Bursa tutup" in out[0]["message"] and broker.positions == {}


def test_api_endpoints(tmp_path):
    client = TestClient(create_app(Settings(data_dir=tmp_path), DemoProvider()))
    st = client.get("/api/autotrader").json()
    assert st["running"] is False and st["config"]["enabled"] is False
    r = client.put("/api/autotrader/config", json={"symbols": ["BBCA", "TLKM"], "interval_seconds": 60})
    assert r.status_code == 200 and r.json()["config"]["symbols"] == ["BBCA", "TLKM"]
    assert client.put("/api/autotrader/config", json={"position_pct": 99}).status_code == 400
    assert client.put("/api/autotrader/config", json={"bogus": 1}).status_code == 400
    assert len(client.post("/api/autotrader/run-once").json()["log"]) >= 1
    assert client.post("/api/autotrader/start").json()["running"] is True
    assert client.post("/api/autotrader/stop").json()["running"] is False
