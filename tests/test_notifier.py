import json
import math
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from app.autotrader import WIB, AutoTrader
from app.brokers import PaperBroker
from app.config import Settings
from app.main import create_app
from app.market_data import Candle, DemoProvider, Quote
from app.notifier import NotifierError, SignalWatcher, TelegramClient, format_signal

WAVE = [1000 + 200 * math.sin(i / 8) for i in range(60)]
BUY, SELL, FLAT = WAVE[:42], WAVE[:52], [1000.0] * 60
TOKEN = "123456:ABCDEFGHIJKLMNOP"


class FakeTelegram:
    """Server Telegram tiruan lewat httpx.MockTransport."""

    def __init__(self, fail=False):
        self.sent, self.fail = [], fail
        self.updates = []

    def handler(self, request: httpx.Request):
        method = request.url.path.rsplit("/", 1)[-1]
        assert request.url.path.startswith(f"/bot{TOKEN}/")
        body = json.loads(request.content or b"{}")
        if self.fail:
            return httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})
        if method == "sendMessage":
            self.sent.append(body)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.sent)}})
        if method == "getUpdates":
            return httpx.Response(200, json={"ok": True, "result": self.updates})
        return httpx.Response(404, json={"ok": False, "description": "Not Found"})

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))


class FakeProvider:
    name = "fake"

    def __init__(self):
        self.series = {}

    def set(self, sym, closes):
        self.series[sym] = closes

    def candles(self, symbol, range_="1y", interval="1d"):
        return [Candle(i, c, c, c, c, 0) for i, c in enumerate(self.series[symbol])]

    def quote(self, symbol):
        p = round(self.series[symbol][-1])
        return Quote(symbol, p, p - 10, 10, 1.25)


@pytest.fixture
def env(tmp_path):
    tg, provider = FakeTelegram(), FakeProvider()
    watcher = SignalWatcher(provider, tmp_path / "n.json", http=tg.client(), clock=lambda: 1_800_000_000.0)
    watcher.update_config({"bot_token": TOKEN, "chat_id": "42", "symbols": ["AAAA"]})
    return watcher, provider, tg


def test_notifies_only_on_signal_change(env):
    watcher, provider, tg = env
    provider.set("AAAA", BUY)
    out = watcher.scan()
    assert out[0]["kind"] == "BUY" and out[0]["sent"]
    assert len(tg.sent) == 1 and tg.sent[0]["chat_id"] == "42" and tg.sent[0]["parse_mode"] == "HTML"
    assert "SINYAL BELI — AAAA" in tg.sent[0]["text"]

    watcher.scan()  # sinyal sama -> tidak dikirim ulang
    assert len(tg.sent) == 1

    provider.set("AAAA", FLAT)  # mereda ke TAHAN -> diam
    watcher.scan()
    assert len(tg.sent) == 1 and watcher.last_action["AAAA"] == "HOLD"

    provider.set("AAAA", BUY)  # BELI muncul lagi -> kirim
    watcher.scan()
    provider.set("AAAA", SELL)  # berbalik JUAL -> kirim
    watcher.scan()
    assert len(tg.sent) == 3 and "SINYAL JUAL" in tg.sent[2]["text"]


def test_custom_thresholds(env):
    watcher, provider, tg = env
    watcher.update_config({"min_buy_score": 4})
    provider.set("AAAA", BUY)  # skor 2 < 4
    watcher.scan()
    assert tg.sent == []


def test_failed_send_is_retried(env, tmp_path):
    watcher, provider, tg = env
    tg.fail = True
    provider.set("AAAA", BUY)
    out = watcher.scan()
    assert out[0]["kind"] == "ERROR" and "chat not found" in out[0]["message"]
    assert "AAAA" not in watcher.last_action
    tg.fail = False
    watcher.scan()
    assert len(tg.sent) == 1


def test_state_persists_across_restart(env, tmp_path):
    watcher, provider, tg = env
    provider.set("AAAA", BUY)
    watcher.scan()
    again = SignalWatcher(provider, tmp_path / "n.json", http=tg.client())
    again.scan()  # sinyal yang sama setelah restart tidak dikirim ulang
    assert len(tg.sent) == 1


def test_message_escapes_html():
    analysis = {"score": 3, "reasons": ["RSI 25.0 < 30 (oversold)", "<script>"]}
    text = format_signal("BBCA", "BUY", analysis, Quote("BBCA", 9025, 8900, 125, 1.4))
    assert "RSI 25.0 &lt; 30" in text and "&lt;script&gt;" in text
    assert "Harga: <b>9.025</b> (+1,40%)" in text
    assert "stockbit.com/symbol/BBCA" in text


def test_market_hours_only(env):
    watcher, provider, tg = env
    watcher.clock = lambda: datetime(2026, 10, 10, 10, 0, tzinfo=WIB).timestamp()  # Sabtu
    watcher.update_config({"market_hours_only": True})
    provider.set("AAAA", BUY)
    assert "Bursa tutup" in watcher.scan()[0]["message"] and tg.sent == []


def test_token_masking_and_env_priority(tmp_path):
    w = SignalWatcher(FakeProvider(), tmp_path / "n.json", env_token="999:ENVTOKENENVTOKEN", env_chat_id="7")
    w.update_config({"bot_token": TOKEN})
    st = w.status()
    assert TOKEN not in json.dumps(st) and st["config"]["bot_token"] == "1234…MNOP"
    assert w.token == "999:ENVTOKENENVTOKEN" and w.chat_id == "7" and st["token_from_env"]
    w.update_config({"bot_token": "1234…MNOP"})  # UI mengirim balik nilai tersamar
    assert w.config.bot_token == TOKEN


def test_start_requires_credentials(tmp_path):
    with pytest.raises(NotifierError, match="token"):
        SignalWatcher(FakeProvider(), None).start()


def test_recent_chats():
    tg = FakeTelegram()
    tg.updates = [{"update_id": 1, "message": {"chat": {"id": 42, "type": "private", "first_name": "Budi"}}}]
    assert TelegramClient(TOKEN, tg.client()).recent_chats() == [{"id": "42", "name": "Budi", "type": "private"}]


def test_autotrader_trades_forwarded(env, tmp_path):
    watcher, provider, tg = env
    broker = PaperBroker(None, 100_000_000, 0.15, 0.25)
    trader = AutoTrader(broker, provider, None, 20)
    trader.update_config({"symbols": ["AAAA"]})
    trader.listeners.append(watcher.on_autotrader_log)
    provider.set("AAAA", BUY)
    trader.run_cycle()
    assert len(tg.sent) == 1 and "Auto-trading (simulasi)" in tg.sent[0]["text"] and "BUY" in tg.sent[0]["text"]
    watcher.update_config({"notify_trades": False})
    broker.reset()
    trader.last_trade_at.clear()
    trader.run_cycle()
    assert len(tg.sent) == 1


def demo_buy_symbol(client) -> str:
    """Kode demo pertama yang sinyalnya BELI (data demo deterministik, tapi tidak dihafal di test)."""
    import itertools
    for a, b, c, d in itertools.product("ABCDEMNPT", "ABDIKLMNORT", "ACEIKMNRST", "ABDIMNOPRT"):
        sym = a + b + c + d
        if client.get(f"/api/analysis/{sym}").json()["action"] == "BUY":
            return sym
    raise AssertionError("tidak ada simbol demo bersinyal BELI")


def test_api(tmp_path):
    tg = FakeTelegram()
    client = TestClient(create_app(Settings(data_dir=tmp_path), DemoProvider(), telegram_http=tg.client()))
    st = client.get("/api/notifications").json()
    assert st["configured"] is False and st["running"] is False
    assert client.post("/api/notifications/start").status_code == 400
    assert client.post("/api/notifications/run-once").status_code == 400
    buy_sym = demo_buy_symbol(client)
    r = client.put("/api/notifications/config", json={"bot_token": TOKEN, "chat_id": "42", "symbols": [buy_sym, "BBCA"]})
    assert r.status_code == 200 and r.json()["configured"] and TOKEN not in r.text
    assert client.put("/api/notifications/config", json={"interval_seconds": 5}).status_code == 400
    assert client.post("/api/notifications/test").status_code == 200 and "aktif" in tg.sent[-1]["text"]
    out = client.post("/api/notifications/run-once").json()
    assert out["last_action"][buy_sym] == "BUY"
    assert any(f"SINYAL BELI — {buy_sym}" in m["text"] for m in tg.sent)
    assert client.get("/api/notifications/chats").status_code == 200
    assert client.post("/api/notifications/start").json()["running"] is True
    assert client.post("/api/notifications/stop").json()["running"] is False
