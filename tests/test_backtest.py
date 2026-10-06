import math
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.autotrader import WIB, AutoTraderConfig
from app.backtest import BacktestRequest, run_backtest
from app.config import Settings
from app.main import create_app
from app.market_data import Candle, DemoProvider, MarketDataError, Quote

DAY = 86400
T0 = int(datetime(2024, 1, 1, 9, tzinfo=WIB).timestamp())


def make_candles(n=500, amp=200, period=8.0, gap=5):
    """Gelombang harga harian (kelipatan 5). Open = close kemarin + gap, agar beda dari close."""
    closes = [round((1000 + amp * math.sin(i / period)) / 5) * 5 for i in range(n)]
    return [Candle(T0 + i * DAY, (closes[i - 1] if i else closes[0]) + gap, c + 10, c - 10, c, 1000)
            for i, c in enumerate(closes)]


class HistProvider:
    name = "hist"

    def __init__(self, data):
        self.data = data
        self.requested = []

    def candles(self, symbol, range_="1y", interval="1d"):
        self.requested.append(range_)
        if symbol not in self.data:
            raise MarketDataError("tidak ditemukan")
        return self.data[symbol]

    def quote(self, symbol):  # tidak boleh dipakai backtest
        raise AssertionError("backtest tidak boleh memakai quote live")


def bt(provider, **kw):
    req = BacktestRequest(**{"symbols": ["AAAA"], "period": "1y", "initial_cash": 100_000_000, **kw})
    return run_backtest(req, provider, AutoTraderConfig(), 0.15, 0.25, 20)


def test_trades_metrics_and_accounting():
    r = bt(HistProvider({"AAAA": make_candles()}))
    m, trades = r["metrics"], r["trades"]
    assert m["trades"] > 3 and m["win_rate_pct"] is not None
    assert len(r["equity_curve"]) == r["params"]["trading_days"] + 1
    assert r["equity_curve"][0]["equity"] == 100_000_000
    # Ekuitas akhir = modal + total P/L semua transaksi (transaksi terbuka dinilai net fee jual,
    # sedangkan ekuitas memakai nilai pasar -> selisih tepat sebesar fee jual hipotetis).
    open_fee = sum(t["lots"] * 100 * t["exit_price"] * 0.0025 for t in trades if not t["closed"])
    assert m["final_equity"] == pytest.approx(100_000_000 + sum(t["pl"] for t in trades) + open_fee, abs=5)
    assert m["fees_paid"] > 0 and m["max_drawdown_pct"] <= 0
    assert 0 < m["exposure_pct"] <= 100


def test_next_open_execution_has_no_lookahead():
    candles = make_candles()
    by_day = {datetime.fromtimestamp(c.time, WIB).strftime("%Y-%m-%d"): c for c in candles}
    r = bt(HistProvider({"AAAA": candles}))
    first = r["trades"][0]
    # Masuk di harga OPEN hari eksekusi (bukan close hari sinyal).
    bought_at = by_day[first["entry_date"]].open
    assert first["entry_price"] == pytest.approx(bought_at * 1.0015, abs=0.01)


def test_close_execution_uses_close_price():
    candles = make_candles()
    by_day = {datetime.fromtimestamp(c.time, WIB).strftime("%Y-%m-%d"): c for c in candles}
    first = bt(HistProvider({"AAAA": candles}), execution="close")["trades"][0]
    assert first["entry_price"] == pytest.approx(by_day[first["entry_date"]].close * 1.0015, abs=0.01)


def test_strategy_override_changes_result_and_benchmark():
    p = HistProvider({"AAAA": make_candles()})
    base = bt(p)
    strict = bt(p, strategy={"min_buy_score": 4})
    assert strict["metrics"]["trades"] < base["metrics"]["trades"]
    assert strict["params"]["strategy"]["min_buy_score"] == 4
    assert base["metrics"]["benchmark_return_pct"] is not None
    assert p.requested[0] == "2y"  # data diambil lebih panjang untuk pemanasan indikator


def test_skips_symbols_without_data():
    r = bt(HistProvider({"AAAA": make_candles(), "SHORT": make_candles(20)}), symbols=["AAAA", "SHORT", "NONE"])
    assert r["params"]["symbols"] == ["AAAA"]
    assert len([w for w in r["warnings"] if "IHSG" not in w]) == 2


@pytest.mark.parametrize("kw,msg", [
    ({"period": "3d"}, "Periode"),
    ({"execution": "vwap"}, "Eksekusi"),
    ({"initial_cash": 10}, "Modal"),
    ({"symbols": [f"S{i:03d}" for i in range(31)]}, "simbol"),
    ({"symbols": ["NONE"]}, "Tidak ada data"),
    ({"strategy": {"position_pct": 90}}, "Ukuran posisi"),
])
def test_validation(kw, msg):
    with pytest.raises(ValueError, match=msg):
        bt(HistProvider({"AAAA": make_candles()}), **kw)


def test_api(tmp_path):
    client = TestClient(create_app(Settings(data_dir=tmp_path), DemoProvider()))
    r = client.post("/api/backtest", json={"symbols": ["AAMB", "BBCA"], "period": "6mo"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"params", "metrics", "equity_curve", "trades", "per_symbol"}
    assert body["params"]["symbols"] == ["AAMB", "BBCA"]
    # Default simbol = pengaturan auto-trader
    assert client.post("/api/backtest", json={"period": "6mo"}).json()["params"]["symbols"] == \
        sorted(client.get("/api/autotrader").json()["config"]["symbols"])
    assert client.post("/api/backtest", json={"period": "x"}).status_code == 400
    assert client.post("/api/backtest", json={"strategy": {"bogus": 1}}).status_code == 400
    # Backtest tidak menyentuh akun simulasi
    assert client.get("/api/orders").json() == []


def test_ihsg_benchmark():
    stock = make_candles()
    # IHSG naik linear 6000 -> 6000 + n: return & drawdown mudah dihitung.
    ihsg = [Candle(c.time, 6000 + i, 6000 + i, 6000 + i, 6000 + i, 0) for i, c in enumerate(stock)]
    r = bt(HistProvider({"AAAA": stock, "IHSG": ihsg}))
    curve, m = r["equity_curve"], r["metrics"]
    start = datetime.fromisoformat(curve[0]["date"]).replace(hour=9, tzinfo=WIB).timestamp()
    i0 = next(i for i, c in enumerate(ihsg) if c.time >= start)
    expected = (ihsg[-1].close / ihsg[i0].close - 1) * 100
    assert curve[0]["ihsg"] == 100_000_000
    assert m["ihsg_return_pct"] == pytest.approx(expected, abs=0.01)
    assert m["ihsg_max_drawdown_pct"] == 0.0  # tidak pernah turun
    assert m["beta_vs_ihsg"] is not None
    assert "IHSG" not in r["params"]["symbols"]  # pembanding, bukan saham yang diperdagangkan
    assert not any("IHSG" in w for w in r["warnings"])


def test_ihsg_benchmark_missing_is_a_warning():
    r = bt(HistProvider({"AAAA": make_candles()}))
    assert r["metrics"]["ihsg_return_pct"] is None and r["equity_curve"][0]["ihsg"] is None
    assert any("Pembanding IHSG tidak tersedia" in w for w in r["warnings"])


def test_beta():
    from app.backtest import _beta
    market = [100, 102, 101, 104, 103, 107]
    assert _beta(market, market) == 1.0
    double = [100.0]
    for i in range(1, len(market)):
        double.append(double[-1] * (1 + 2 * (market[i] / market[i - 1] - 1)))
    assert _beta(double, market) == 2.0
    assert _beta([100] * 6, market) == 0.0  # kas saja: tidak terpengaruh pasar
    assert _beta(market, [100] * 6) is None
