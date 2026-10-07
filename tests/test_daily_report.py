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
    assert rep.run_due()["kind"] == "REPORT" and len(tg.sent) == 1
    assert rep.run_due() is None  # sudah terkirim hari ini
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
    assert "IHSG: 7.123,45 (+0,33%)" in first and "sejak laporan kemarin" not in first  # belum ada pembanding
    assert "Crypto" not in first  # akun crypto belum pernah dipakai
    clock["t"] += 86400
    prices.p["BBCA"] = (9200, 9050)
    rep.send_now()
    second = tg.sent[1]["text"]
    assert "(+Rp150.000 / +0,15% sejak laporan kemarin)" in second and "Transaksi hari ini: tidak ada" in second


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
