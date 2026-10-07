import json

import pytest
from fastapi.testclient import TestClient

from app.db import Database
from app.brokers import PaperBroker
from app.config import Settings
from app.main import create_app
from app.market_data import DemoProvider, MarketDataError, Quote
from app.notifier import SignalWatcher
from app.webhooks import TradingViewWebhook, WebhookError, parse_alert
from tests.test_notifier import TOKEN, FakeTelegram


class PriceProvider:
    name = "fixed"

    def __init__(self, prices):
        self.prices = prices

    def quote(self, symbol):
        if symbol not in self.prices:
            raise MarketDataError("tidak ada")
        return Quote(symbol, self.prices[symbol], self.prices[symbol], 0, 0)


@pytest.fixture
def env(tmp_path):
    tg = FakeTelegram()
    provider = PriceProvider({"BBCA": 9000, "TLKM": 3000, "IHSG": 7000})
    watcher = SignalWatcher(provider, None, http=tg.client())
    watcher.update_config({"bot_token": TOKEN, "chat_id": "42"})
    broker = PaperBroker(None, 100_000_000, 0.15, 0.25)
    clock = {"t": 1_800_000_000.0}
    hook = TradingViewWebhook(broker, provider, Database(tmp_path / "app.db"), 20, notifier=watcher, clock=lambda: clock["t"])
    hook.update_config({"enabled": True})
    return hook, broker, tg, clock


def send(hook, **payload):
    body = json.dumps({"secret": hook.config.secret, **payload}).encode()
    alert, _ = hook.accept(body)
    return hook.process(alert)


def kinds(entries):
    return [e["kind"] for e in entries]


def test_auth_and_validation(env):
    hook, *_ = env
    ok = json.dumps({"secret": hook.config.secret, "symbol": "BBCA", "action": "buy"}).encode()
    with pytest.raises(WebhookError, match="rahasia") as e:
        hook.accept(json.dumps({"secret": "salah", "symbol": "BBCA"}).encode())
    assert e.value.status == 401
    with pytest.raises(WebhookError, match="JSON"):
        hook.accept(b"BBCA buy")
    with pytest.raises(WebhookError, match="tidak dikenali"):
        hook.accept(json.dumps({"secret": hook.config.secret, "symbol": "BBCA", "action": "hold"}).encode())
    alert, _ = hook.accept(json.dumps({"symbol": "IDX:BBCA", "action": "BUY"}).encode(), query_secret=hook.config.secret)
    assert alert.symbol == "BBCA" and alert.action == "BUY"
    hook.update_config({"enabled": False})
    with pytest.raises(WebhookError, match="nonaktif") as e:
        hook.accept(ok)
    assert e.value.status == 403


def test_parse_alert_fields():
    a = parse_alert({"ticker": "bbca", "side": "long", "price": "9025.0", "lots": 3, "comment": "x" * 900})
    assert (a.symbol, a.action, a.price, a.lots) == ("BBCA", "BUY", 9025.0, 3) and len(a.message) == 500
    assert parse_alert({"symbol": "TLKM"}).action is None  # alert informasi saja
    for bad in ({"symbol": "X", "lots": 1.5}, {"symbol": "X", "price": "abc"}, {"symbol": "X", "price": -1},
                {"symbol": "X", "price": "nan"}, {"symbol": "X", "mode": "trade"}, {"action": "buy"}):
        with pytest.raises(WebhookError):
            parse_alert(bad)


def test_duplicate_within_a_minute(env):
    hook, _, _, clock = env
    body = json.dumps({"secret": hook.config.secret, "symbol": "BBCA", "action": "buy"}).encode()
    hook.accept(body)
    with pytest.raises(WebhookError, match="duplikat") as e:
        hook.accept(body)
    assert e.value.status == 409
    clock["t"] += 61
    hook.accept(body)


def test_notify_mode_sends_telegram_without_order(env):
    hook, broker, tg, _ = env
    out = send(hook, symbol="BBCA", action="buy", price=9025, message="Golden cross <EMA>")
    assert kinds(out) == ["NOTIFY"] and broker.positions == {}
    assert "Alert TradingView — BBCA" in tg.sent[0]["text"] and "&lt;EMA&gt;" in tg.sent[0]["text"]


def test_order_mode_buy_sizing_and_sell(env):
    hook, broker, tg, _ = env
    hook.update_config({"mode": "order"})
    out = send(hook, symbol="BBCA", action="buy")
    assert kinds(out) == ["ORDER", "NOTIFY"]
    # 10% dari Rp100 jt = Rp10 jt; 1 lot @9000 + fee = Rp901.350 -> 11 lot
    assert broker.positions["BBCA"]["shares"] == 1100
    assert broker.orders()[0].source == "webhook"
    assert "Order simulasi: BUY 11 lot" in tg.sent[0]["text"]
    out = send(hook, symbol="BBCA", action="sell", lots=5)
    assert broker.positions["BBCA"]["shares"] == 600
    send(hook, symbol="BBCA", action="exit")  # tanpa lots -> jual semua
    assert broker.positions == {}
    out = send(hook, symbol="BBCA", action="sell", message="lagi")
    assert kinds(out)[0] == "SKIP" and "tidak punya posisi" in out[0]["message"]


def test_order_limits_and_indices(env):
    hook, broker, _, _ = env
    hook.update_config({"mode": "order", "max_lots": 50})
    send(hook, symbol="TLKM", action="buy", lots=500)
    # max_lots 50 -> 50 lot @3000 = Rp15 jt (< batas risiko 20% ekuitas)
    assert broker.positions["TLKM"]["shares"] == 5000
    hook.update_config({"max_lots": 1000})
    send(hook, symbol="BBCA", action="buy", lots=1000)
    # batas risiko 20% ekuitas ≈ Rp20 jt -> maks. 22 lot @9000
    assert broker.positions["BBCA"]["shares"] == 2200
    out = send(hook, symbol="^JKSE", action="buy")
    assert out[0]["kind"] == "SKIP" and "Indeks" in out[0]["message"] and out[1]["kind"] == "NOTIFY"


def test_price_fallback_and_allowed_symbols(env):
    hook, broker, _, _ = env
    hook.update_config({"mode": "order", "allowed_symbols": ["ASII", "bbca"]})
    out = send(hook, symbol="ASII", action="buy", price=5000, mode="log")
    assert out == []  # mode per alert: hanya dicatat
    out = send(hook, symbol="ASII", action="buy", price=5000, lots=2)
    assert out[0]["kind"] == "ORDER" and broker.positions["ASII"]["shares"] == 200  # harga dari alert
    with pytest.raises(WebhookError, match="diizinkan") as e:
        hook.accept(json.dumps({"secret": hook.config.secret, "symbol": "TLKM", "action": "buy"}).encode())
    assert e.value.status == 422


def test_telegram_not_configured(tmp_path):
    hook = TradingViewWebhook(PaperBroker(None, 1e8, 0.15, 0.25), PriceProvider({}), None, 20,
                              notifier=SignalWatcher(PriceProvider({}), None))
    hook.update_config({"enabled": True})
    assert send(hook, symbol="BBCA", action="buy")[0]["kind"] == "SKIP"


def test_secret_management_and_template(env, tmp_path):
    hook, broker, *_ = env
    old = hook.config.secret
    hook.update_config({"secret": "x" * 20})  # tidak bisa diganti lewat config
    assert hook.config.secret == old and len(old) >= 24
    assert hook.regenerate_secret() != old
    reloaded = TradingViewWebhook(broker, None, Database(tmp_path / "app.db"), 20)
    assert reloaded.config.secret == hook.config.secret and reloaded.config.enabled
    tpl = hook.message_template().replace("{{ticker}}", "BBCA").replace("{{strategy.order.action}}", "buy") \
        .replace("{{close}}", "9025").replace("{{strategy.order.comment}}", "x")
    assert json.loads(tpl) == {"secret": hook.config.secret, "symbol": "BBCA", "action": "buy", "price": 9025, "message": "x"}
    with pytest.raises(ValueError, match="Ukuran posisi"):
        hook.update_config({"position_pct": 50})


def test_api_and_tunnel_guard(tmp_path):
    client = TestClient(create_app(Settings(data_dir=tmp_path), DemoProvider()))
    st = client.get("/api/webhooks").json()
    secret = st["config"]["secret"]
    url = "/api/webhooks/tradingview"
    assert client.post(url, json={"secret": secret, "symbol": "BBCA"}).status_code == 403  # belum aktif
    assert client.put("/api/webhooks/config", json={"enabled": True, "mode": "order"}).status_code == 200
    assert client.post(url, json={"secret": "x", "symbol": "BBCA"}).status_code == 401
    tunnel = {"X-Forwarded-For": "203.0.113.9"}
    r = client.post(url, json={"secret": secret, "symbol": "AAMB", "action": "buy"}, headers=tunnel)
    assert r.status_code == 200 and r.json()["ok"]
    assert client.get("/api/account").json()["positions"][0]["symbol"] == "AAMB"  # diproses di background
    assert client.get("/api/orders").json()[0]["source"] == "webhook"
    # Lewat tunnel, selain webhook semuanya ditolak
    for path in ("/", "/api/webhooks", "/api/account", "/api/notifications"):
        assert client.get(path, headers=tunnel).status_code == 403
    assert client.post("/api/webhooks/regenerate-secret", headers={"Cf-Connecting-Ip": "1.2.3.4"}).status_code == 403
    assert client.post("/api/webhooks/regenerate-secret").json()["config"]["secret"] != secret


def test_guard_can_be_disabled(tmp_path):
    client = TestClient(create_app(Settings(data_dir=tmp_path, local_only_guard=False), DemoProvider()))
    assert client.get("/api/config", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 200


# ---- pesan Telegram yang lebih lengkap & peringatan keamanan ------------------------------
class PrevProvider(PriceProvider):
    """Seperti PriceProvider, tapi penutupan kemarin bisa diatur (untuk ARA/ARB & % harian)."""

    def __init__(self, prices, prevs):
        super().__init__(prices)
        self.prevs = prevs

    def quote(self, symbol):
        if symbol not in self.prices:
            raise MarketDataError("tidak ada")
        p, prev = self.prices[symbol], self.prevs.get(symbol, self.prices[symbol])
        return Quote(symbol, p, prev, p - prev, (p / prev - 1) * 100)


@pytest.fixture
def rich(tmp_path):
    tg = FakeTelegram()
    provider = PrevProvider({"BBCA": 9050, "GOTO": 125, "IHSG": 7123.45}, {"BBCA": 9000, "GOTO": 100, "IHSG": 7100})
    watcher = SignalWatcher(provider, None, http=tg.client())
    watcher.update_config({"bot_token": TOKEN, "chat_id": "42"})
    clock = {"t": 1_800_000_000.0}
    hook = TradingViewWebhook(PaperBroker(None, 100_000_000, 0.15, 0.25), provider, None, 20,
                              notifier=watcher, clock=lambda: clock["t"])
    hook.update_config({"enabled": True})
    return hook, tg, clock


def test_rich_message_notify_only(rich):
    hook, tg, _ = rich
    send(hook, symbol="BBCA", action="buy", price=9025, message="EMA cross")
    text = tg.sent[-1]["text"]
    assert "🟢 <b>Alert TradingView — BBCA</b>" in text and "Sinyal: BELI @ 9.025 — EMA cross" in text
    assert "Harga terkini: <b>9.050</b> (+0,56% hari ini) · +0,28% dari harga alert" in text
    assert "ARB 7.650 · ARA 10.800" in text and "sedang" not in text
    assert "Order" not in text and "Posisi" not in text  # mode notify: tanpa order
    assert "symbols/IDX-BBCA/" in text and "stockbit.com/symbol/BBCA" in text


def test_rich_message_order_success_and_position(rich):
    hook, tg, _ = rich
    hook.update_config({"mode": "order"})
    send(hook, symbol="BBCA", action="buy", lots=3)
    text = tg.sent[-1]["text"]
    assert "✅ Order simulasi: BUY 3 lot @ 9.050 (simulasi)" in text
    assert "Posisi: 3 lot · avg 9.050 · P/L +Rp0 (+0,00%)" in text
    send(hook, symbol="BBCA", action="sell", lots=1, message="profit")
    assert "Posisi: 2 lot" in tg.sent[-1]["text"]


def test_rich_message_rejected_at_ara_and_skipped(rich):
    hook, tg, _ = rich
    hook.update_config({"mode": "order"})
    send(hook, symbol="GOTO", action="buy", lots=1)  # acuan 100 (rentang <=200): ARA 35% = 135, harga 125
    text = tg.sent[-1]["text"]
    assert "ARB 85 · ARA 135" in text and "❌" not in text  # 125 belum ARA
    hook.provider.prices["GOTO"] = 135
    send(hook, symbol="GOTO", action="buy", lots=1, message="lagi")
    text = tg.sent[-1]["text"]
    assert "⚠️ <b>sedang ARA</b>" in text and "❌ Order ditolak:" in text and "tidak ada penjual" in text
    send(hook, symbol="BBCA", action="sell")
    text = tg.sent[-1]["text"]
    assert "⏭ Order dilewati: Alert JUAL diabaikan: tidak punya posisi" in text and "Posisi: tidak ada" in text


def test_rich_message_index_and_log_mode(rich):
    hook, tg, _ = rich
    hook.update_config({"mode": "order"})
    send(hook, symbol="^JKSE", action="sell")
    text = tg.sent[-1]["text"]
    assert "Harga terkini: <b>7.123,45</b>" in text and "ARA" not in text and "Posisi" not in text
    assert "⏭ Order dilewati: Indeks" in text
    n = len(tg.sent)
    send(hook, symbol="BBCA", action="buy", mode="log")
    assert len(tg.sent) == n


def test_security_alert_rate_limited(rich):
    hook, tg, clock = rich
    hook.report_auth_failure("203.0.113.9")
    assert "kode rahasia salah" in tg.sent[-1]["text"] and "1 percobaan" in tg.sent[-1]["text"]
    assert "203.0.113.9" in tg.sent[-1]["text"]
    clock["t"] += 60
    hook.report_auth_failure("198.51.100.7")
    hook.report_auth_failure("198.51.100.7")
    assert len(tg.sent) == 1  # dalam 10 menit: dikumpulkan
    clock["t"] += 600
    hook.report_auth_failure("203.0.113.9")
    text = tg.sent[-1]["text"]
    assert len(tg.sent) == 2 and "3 percobaan" in text and "198.51.100.7, 203.0.113.9" in text
    assert [e["kind"] for e in list(hook.log)[:2]] == ["SECURITY", "AUTH"]
    hook.update_config({"notify_security": False})
    clock["t"] += 3600
    hook.report_auth_failure("1.2.3.4")
    assert len(tg.sent) == 2 and hook.log[0]["kind"] == "AUTH"


def test_api_wrong_secret_logs_ip(tmp_path):
    client = TestClient(create_app(Settings(data_dir=tmp_path), DemoProvider()))
    client.put("/api/webhooks/config", json={"enabled": True})
    r = client.post("/api/webhooks/tradingview", json={"secret": "salah", "symbol": "BBCA"},
                    headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1"})
    assert r.status_code == 401
    log = client.get("/api/webhooks").json()["log"]
    assert log[0]["kind"] == "AUTH" and "203.0.113.9" in log[0]["message"]
    assert client.get("/api/webhooks").json()["config"]["notify_security"] is True
