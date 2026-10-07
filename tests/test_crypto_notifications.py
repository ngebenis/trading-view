from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.autotrader import WIB
from app.binance import BinanceAPI, BinanceProvider
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.market_data import DemoProvider
from app.notifier import SignalWatcher
from tests.fake_binance import FakeBinance
from tests.test_notifier import BUY, TOKEN, FakeProvider, FakeTelegram

URL = "https://fake.binance"
SATURDAY = datetime(2026, 10, 10, 10, 0, tzinfo=WIB).timestamp()


@pytest.fixture
def env(tmp_path):
    tg, binance = FakeTelegram(), FakeBinance()
    watcher = SignalWatcher(FakeProvider(), Database(tmp_path / "app.db"), http=tg.client(), clock=lambda: SATURDAY)
    watcher.crypto_provider = BinanceProvider(BinanceAPI(URL, client=binance.client()), quote_ttl=0, kline_ttl=0)
    watcher.update_config({"bot_token": TOKEN, "chat_id": "42", "symbols": [], "crypto_symbols": ["btc/usdt"],
                           "crypto_move_pct": 0})
    return watcher, binance, tg


def test_crypto_signal_sent_once_and_on_change(env):
    watcher, binance, tg = env
    binance.set_wave("BTCUSDT", 42, 65000)  # pola sinyal BELI
    out = watcher.scan()
    assert [e["kind"] for e in out] == ["BUY"] and len(tg.sent) == 1
    text = tg.sent[0]["text"]
    assert "SINYAL BELI — BTCUSDT" in text and "candle 1 jam" in text and "USDT" in text and "Binance</a>" in text
    assert "@ " in out[0]["message"] and "USDT" in out[0]["message"]
    watcher.scan()
    assert len(tg.sent) == 1  # sinyal yang sama tidak dikirim ulang
    binance.set_wave("BTCUSDT", 52, 65000)  # berubah menjadi JUAL
    assert [e["kind"] for e in watcher.scan()] == ["SELL"] and "SINYAL JUAL" in tg.sent[-1]["text"]


def test_crypto_ignores_market_hours_and_uses_chosen_candle(env):
    watcher, binance, tg = env
    watcher.update_config({"market_hours_only": True, "symbols": ["AAAA"], "crypto_candle_interval": "4h"})
    watcher.provider.set("AAAA", BUY)
    binance.set_wave("BTCUSDT", 42, 65000)
    out = watcher.scan()  # Sabtu: saham dilewati, crypto tetap dipindai
    assert [e["symbol"] for e in out] == ["BTCUSDT"] and len(tg.sent) == 1
    assert ("GET", "/api/v3/klines") in [(m, p) for m, p, q in binance.requests if q.get("interval") == "4h"]


def test_big_24h_move_alert_once_per_day_per_direction(env):
    watcher, binance, tg = env
    watcher.update_config({"crypto_move_pct": 1.5})  # bursa tiruan selalu +2% dalam 24 jam
    out = watcher.scan()
    assert [e["kind"] for e in out] == ["MOVE"] and "naik +2,00% dalam 24 jam" in tg.sent[0]["text"]
    watcher.scan()
    assert len(tg.sent) == 1
    watcher.clock = lambda: SATURDAY + 86400  # hari berikutnya: boleh dikabarkan lagi
    watcher.scan()
    assert len(tg.sent) == 2
    watcher.update_config({"crypto_move_pct": 3})
    watcher.clock = lambda: SATURDAY + 2 * 86400
    watcher.scan()
    assert len(tg.sent) == 2  # di bawah ambang


def test_config_validation(env):
    watcher, *_ = env
    with pytest.raises(ValueError, match="minimal satu"):
        watcher.update_config({"symbols": [], "crypto_symbols": []})
    with pytest.raises(ValueError, match="Bukan pasangan crypto"):
        watcher.update_config({"crypto_symbols": ["BBCA"]})
    with pytest.raises(ValueError, match="Candle crypto"):
        watcher.update_config({"crypto_candle_interval": "2h"})
    assert watcher.update_config({"crypto_symbols": ["eth-usdt", "BTCUSDT"]}).crypto_symbols == ["BTCUSDT", "ETHUSDT"]


def test_signal_state_kept_when_crypto_list_changes(env):
    watcher, binance, tg = env
    binance.set_wave("BTCUSDT", 42, 65000)
    watcher.scan()
    watcher.update_config({"crypto_symbols": ["BTCUSDT", "ETHUSDT"]})
    assert watcher.last_action["BTCUSDT"] == "BUY"  # tidak terhapus -> tidak dikirim ulang
    watcher.update_config({"crypto_symbols": ["ETHUSDT"]})
    assert "BTCUSDT" not in watcher.last_action


def test_without_crypto_provider_warns(tmp_path):
    tg = FakeTelegram()
    w = SignalWatcher(FakeProvider(), None, http=tg.client())
    w.update_config({"bot_token": TOKEN, "chat_id": "42", "symbols": [], "crypto_symbols": ["BTCUSDT"]})
    assert "belum tersedia" in w.scan()[0]["message"]


def test_api_wires_binance_into_notifications(tmp_path):
    tg, binance = FakeTelegram(), FakeBinance()
    binance.set_wave("ETHUSDT", 42, 3200)
    app = create_app(Settings(data_dir=tmp_path, binance_data_url=URL), DemoProvider(), telegram_http=tg.client(),
                     binance_http=binance.client())
    c = TestClient(app)
    r = c.put("/api/notifications/config", json={"bot_token": TOKEN, "chat_id": "42", "symbols": [],
                                                  "crypto_symbols": ["ETHUSDT"], "crypto_move_pct": 0})
    assert r.status_code == 200 and r.json()["config"]["crypto_symbols"] == ["ETHUSDT"]
    st = c.post("/api/notifications/run-once").json()
    assert st["history"][0]["kind"] == "BUY" and "SINYAL BELI — ETHUSDT" in tg.sent[0]["text"]


def test_unknown_pair_is_a_warning_not_a_crash(env):
    watcher, binance, tg = env
    binance.set_wave("BTCUSDT", 42, 65000)
    watcher.update_config({"crypto_symbols": ["SOLUSDT", "BTCUSDT"]})
    out = watcher.scan()
    assert [(e["kind"], e["symbol"]) for e in out] == [("BUY", "BTCUSDT"), ("WARN", "SOLUSDT")]
    assert "tidak ada di Binance" in out[1]["message"]
