from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.autotrader import WIB
from app.binance import BinanceAPI, BinanceProvider
from app.brokers import Order, OrderType, PaperBroker, Side
from app.config import Settings
from app.crypto import CryptoOrder, CryptoPaperBroker
from app.daily_report import DailyReporter
from app.db import Database
from app.main import create_app
from app.market_data import DemoProvider, MarketDataError, Quote
from app.notifier import SignalWatcher
from tests.fake_binance import EXCHANGE_INFO, FakeBinance
from tests.test_notifier import TOKEN, FakeTelegram
from app.binance import SymbolRules

WED_1615 = datetime(2026, 10, 7, 16, 15, tzinfo=WIB).timestamp()


class Prices:
    name = "fake"

    def __init__(self, **p):
        self.p = p

    def quote(self, s):
        if s not in self.p:
            raise MarketDataError("x")
        price, prev = self.p[s]
        return Quote(s, price, prev, price - prev, (price / prev - 1) * 100, "IDR", "fake", None)


@pytest.fixture
def env(tmp_path):
    clock = {"t": WED_1615 - 3600}
    tg = FakeTelegram()
    db = Database(tmp_path / "app.db")
    watcher = SignalWatcher(None, db, http=tg.client(), clock=lambda: clock["t"])
    watcher.update_config({"bot_token": TOKEN, "chat_id": "42", "symbols": ["BBCA"], "report_enabled": True})
    paper = PaperBroker(db, 100_000_000, 0.15, 0.25, clock=lambda: clock["t"])
    paper.place_order(Order("BBCA", Side.BUY, 10, OrderType.MARKET, created_at=clock["t"]), 9000)
    prices = Prices(BBCA=(9050, 9000), IHSG=(7123.45, 7100.0))
    binance = FakeBinance()
    crypto = {"provider": BinanceProvider(BinanceAPI("https://fake", client=binance.client()), quote_ttl=0),
              "brokers": {"paper": CryptoPaperBroker(db, 1000, 0.1)}}
    rep = DailyReporter(watcher, paper, prices, db, crypto, clock=lambda: clock["t"])
    return rep, watcher, paper, prices, crypto, tg, clock


def test_schedule_once_per_day_weekdays_only(env):
    rep, watcher, _, _, _, tg, clock = env
    assert not rep.due()  # 15:15, sebelum jadwal 16:15
    clock["t"] = WED_1615
    assert [e["kind"] for e in rep.run_due()] == ["REPORT"] and len(tg.sent) == 1
    assert rep.run_due() == []  # sudah terkirim hari ini
    clock["t"] = WED_1615 + 3 * 86400  # Sabtu
    assert not rep.due()
    watcher.update_config({"report_weekdays_only": False})
    assert rep.due()
    watcher.update_config({"report_enabled": False})
    assert not rep.due() and rep.next_run() is None
    watcher.update_config({"report_enabled": True, "report_time": "9:5"})
    assert watcher.config.report_time == "09:05" and rep.next_run() == "09:05 WIB (setiap hari)"
    with pytest.raises(ValueError, match="JJ:MM"):
        watcher.update_config({"report_time": "25:00"})


def test_content_and_change_since_yesterday(env):
    rep, watcher, paper, prices, _, tg, clock = env
    clock["t"] = WED_1615
    rep.send_now()
    first = tg.sent[0]["text"]
    assert "Laporan portofolio — Rabu, 07/10/2026 16:15 WIB" in first
    assert "• BBCA 10 lot · 9.050 (+0,56%) · P/L +Rp50.000" in first
    assert "pergerakan hari ini +Rp50.000" in first and "Transaksi hari ini: 1 beli, 0 jual" in first
    assert "IHSG: 7.123,45 (+0,33%)" in first and "sejak" not in first  # belum ada pembanding
    assert "Crypto" not in first  # akun crypto belum pernah dipakai
    clock["t"] += 86400
    prices.p["BBCA"] = (9200, 9050)
    rep.send_now()
    second = tg.sent[1]["text"]
    assert "(+Rp150.000 / +0,15% sejak 07/10)" in second and "Transaksi hari ini: tidak ada" in second


def test_crypto_section_and_toggle(env):
    rep, watcher, _, _, crypto, tg, clock = env
    rules = SymbolRules.from_exchange_info(EXCHANGE_INFO["BTCUSDT"])
    crypto["brokers"]["paper"].place_order(CryptoOrder("BTCUSDT", Side.BUY, 0.002, OrderType.MARKET), 65000, rules)
    text = rep.build()
    assert "Crypto — akun simulasi (USDT)" in text and "• BTC 0,002 · 65.000,00 (+2,00% 24j)" in text
    watcher.update_config({"report_crypto": False})
    assert "Crypto" not in rep.build()


def test_failed_send_is_retried(env):
    rep, watcher, *_ = env
    tg = FakeTelegram(fail=True)
    watcher.http = tg.client()
    assert rep.send_now()["kind"] == "ERROR" and rep.last_sent is None


def test_api(tmp_path):
    tg = FakeTelegram()
    app = create_app(Settings(data_dir=tmp_path), DemoProvider(), telegram_http=tg.client())
    c = TestClient(app)
    assert c.post("/api/notifications/report").status_code == 400  # Telegram belum diatur
    assert "Laporan portofolio" in c.get("/api/notifications/report/preview").json()["text"]
    st = c.put("/api/notifications/config", json={"bot_token": TOKEN, "chat_id": "42", "report_enabled": True,
                                                  "report_time": "16:30"}).json()
    assert st["report_schedule"] == "16:30 WIB (Senin–Jumat)" and st["report_last_sent"] is None
    st = c.post("/api/notifications/report").json()
    assert st["history"][0]["kind"] == "REPORT" and st["report_last_sent"] and len(tg.sent) == 1


FRI_1630 = datetime(2026, 10, 9, 16, 30, tzinfo=WIB).timestamp()


class History(Prices):
    """Seperti Prices, plus candle harian (penutupan seminggu lalu) untuk laporan mingguan."""

    def __init__(self, week_ago, **p):
        super().__init__(**p)
        self.week_ago = week_ago

    def candles(self, symbol, range_="1mo", interval="1d"):
        from app.market_data import Candle
        start = datetime(2026, 9, 28, 9, 0, tzinfo=WIB).timestamp()
        out = []
        for i in range(12):  # 28/09 … 09/10
            close = self.week_ago[symbol] if i <= 4 else self.p[symbol][0]
            out.append(Candle(int(start + i * 86400), close, close, close, close, 0))
        return out


def test_weekly_schedule_snapshots_and_content(env):
    rep, watcher, paper, _, _, tg, clock = env
    rep.provider = History({"BBCA": 8500, "TLKM": 4000, "IHSG": 7000.0},
                           BBCA=(9050, 9000), TLKM=(3800, 3850), IHSG=(7123.45, 7100.0))
    paper.place_order(Order("TLKM", Side.BUY, 5, OrderType.MARKET, created_at=clock["t"]), 3850)
    watcher.update_config({"report_enabled": False, "weekly_enabled": True, "weekly_day": 4, "weekly_time": "16:30"})
    assert rep.next_weekly() == "Jumat 16:30 WIB"
    # Snapshot otomatis harian setelah 16:00 walau laporan harian mati: Kamis 01/10 & Rabu 07/10.
    clock["t"] = datetime(2026, 10, 1, 16, 5, tzinfo=WIB).timestamp()
    assert rep.run_due() == [] and rep.maybe_snapshot() is False  # sudah dicatat hari ini
    base = rep._snapshots()["idx"]["2026-10-01"]
    clock["t"] = FRI_1630 - 3600  # Jumat 15:30, sebelum jadwal
    assert not rep.weekly_due()
    clock["t"] = FRI_1630
    assert [e["message"] for e in rep.run_due()] == ["Laporan mingguan portofolio terkirim"]
    text = tg.sent[-1]["text"]
    assert "Laporan mingguan portofolio — 03/10 s/d 09/10/2026" in text
    from app.daily_report import signed_rp
    change = paper.account({"BBCA": 9050, "TLKM": 3800})["equity"] - base  # pembanding: snapshot ±7 hari lalu
    assert f"({signed_rp(change)} / " in text and "sejak 01/10)" in text
    assert "perubahan harga 7 hari" in text and "• BBCA 10 lot · 9.050 (+6,47%)" in text
    assert "Terbaik: BBCA +6,47% · Terburuk: TLKM -5,00%" in text
    assert "Transaksi 7 hari: 2 beli, 0 jual" in text and "IHSG: 7.123,45 (+1,76% dalam 7 hari)" in text
    assert rep.run_due() == []  # sekali per minggu
    clock["t"] += 86400  # Sabtu: bukan hari laporan
    assert not rep.weekly_due()


def test_weekly_api_and_validation(tmp_path):
    tg = FakeTelegram()
    c = TestClient(create_app(Settings(data_dir=tmp_path), DemoProvider(), telegram_http=tg.client()))
    assert "Laporan mingguan portofolio" in c.get("/api/notifications/report/preview?period=weekly").json()["text"]
    assert c.get("/api/notifications/report/preview?period=yearly").status_code == 422
    st = c.put("/api/notifications/config", json={"bot_token": TOKEN, "chat_id": "42", "weekly_enabled": True,
                                                  "weekly_day": 0, "weekly_time": "8:0"}).json()
    assert st["weekly_schedule"] == "Senin 08:00 WIB"
    assert c.put("/api/notifications/config", json={"weekly_day": 7}).status_code == 400
    st = c.post("/api/notifications/report?period=weekly").json()
    assert st["history"][0]["message"] == "Laporan mingguan portofolio terkirim" and st["weekly_last_sent"]
