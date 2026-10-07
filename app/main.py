"""Server API + UI. Jalankan: uvicorn app.main:app --reload"""
import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from datetime import date, datetime, timezone

from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .autotrader import AutoTrader, is_idx_market_open
from .chart_data import build_chart
from .db import Database
from .backtest import PERIOD_DAYS, BacktestRequest, run_backtest
from .brokers import (Broker, BrokerError, BrokerNotAvailable, Order, OrderType, PaperBroker,
                      PluangBroker, Side, StockbitBroker)
from .config import Settings, settings as default_settings
from .idx_rules import (ARA_PCTS, ARB_PCTS, LOT_SIZE, is_index, limit_status, normalize_symbol, price_limits,
                        round_to_tick, stockbit_url, tick_size,
                        tradingview_symbol, tradingview_url)
from .fundamentals import FundamentalsError, FundamentalsStore, compute_ratios, idx_report_url, recent_periods
from .crypto_api import register_crypto
from .idx_vendors import build_provider
from .market_data import MarketDataError
from .daily_report import DailyReporter
from .price_alerts import AlertError, PriceAlertWatcher
from .price_feed import FeedError, FeedProvider, PriceFeed, pine_script
from .notifier import NotifierError, SignalWatcher
from .webhooks import TradingViewWebhook, WebhookError
from .strategy import analyze

STATIC = Path(__file__).parent / "static"


class BacktestBody(BaseModel):
    symbols: list[str] | None = None  # default: simbol auto-trader
    period: str = "1y"
    initial_cash: float | None = None  # default: PAPER_STARTING_CASH
    execution: str = "next_open"
    strategy: dict = Field(default_factory=dict)  # override pengaturan auto-trader


class OrderRequest(BaseModel):
    broker: str = "paper"
    symbol: str
    side: Side
    lots: int = Field(gt=0)
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    confirm_live: bool = False


def create_app(settings: Settings = default_settings, provider=None, telegram_http=None,
               binance_http=None, vendor_http=None) -> FastAPI:
    db = Database(settings.database_path or settings.data_dir / "app.db")
    db.migrate_json(settings.data_dir, settings.paper_starting_cash)  # sekali, dari versi berbasis JSON
    db.prune_logs(keep_days=180)
    # Harga dari price feed webhook TradingView (bila aktif & segar), selebihnya dari provider biasa.
    feed = PriceFeed(db)
    provider = FeedProvider(provider or build_provider(settings, vendor_http), feed)
    paper = PaperBroker(db, settings.paper_starting_cash,
                        settings.buy_fee_pct, settings.sell_fee_pct)
    autotrader = AutoTrader(paper, provider, db, settings.max_position_pct)
    watcher = SignalWatcher(provider, db, settings.telegram_bot_token,
                            settings.telegram_chat_id, http=telegram_http)
    autotrader.listeners.append(watcher.on_autotrader_log)
    webhook = TradingViewWebhook(paper, provider, db, settings.max_position_pct,
                                 notifier=watcher)
    fundamentals = FundamentalsStore(Path(settings.fundamentals_xbrl_dir) if settings.fundamentals_xbrl_dir
                                     else settings.data_dir / "fundamentals" / "XBRL")

    @asynccontextmanager
    async def lifespan(_app):
        if autotrader.config.enabled:  # lanjutkan bila sebelumnya aktif
            autotrader.start()
        if watcher.config.enabled:
            try:
                watcher.start()
            except NotifierError:
                pass
        if crypto["bot"].config.enabled and crypto["bot"].target.available():
            crypto["bot"].start()
        price_alerts.start()
        reporter.start()
        yield
        if autotrader.running:
            autotrader._stop.set()
        if crypto["bot"].running:
            crypto["bot"]._stop.set()
        watcher.shutdown()
        price_alerts.shutdown()
        reporter.shutdown()

    app = FastAPI(title="IDX Trading View", version="0.2.0", lifespan=lifespan)
    app.state.autotrader = autotrader
    app.state.watcher = watcher
    app.state.webhook = webhook
    app.state.feed = feed
    crypto = register_crypto(app, settings, db, watcher, binance_http)
    app.state.crypto = crypto
    price_alerts = PriceAlertWatcher(provider, watcher, db, settings.price_alert_seconds)
    app.state.price_alerts = price_alerts
    reporter = DailyReporter(watcher, paper, provider, db, crypto)
    watcher.reporter = reporter  # jadwal laporan ikut tampil di status notifikasi
    app.state.reporter = reporter
    app.state.db = db

    # Pengaman tunnel: request yang lewat proxy/tunnel (ngrok, Cloudflare Tunnel, dll) membawa header
    # penerusan. Request seperti itu hanya boleh ke endpoint webhook; UI & API lain hanya untuk lokal.
    PUBLIC_PATHS = {"/api/webhooks/tradingview"}
    FORWARD_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip", "cf-connecting-ip", "x-forwarded-host")

    @app.middleware("http")
    async def local_only_guard(request: Request, call_next):
        if settings.local_only_guard and request.url.path not in PUBLIC_PATHS and \
                any(h in request.headers for h in FORWARD_HEADERS):
            return JSONResponse({"detail": "Hanya endpoint webhook yang bisa diakses dari luar"}, status_code=403)
        return await call_next(request)
    brokers: dict[str, Broker] = {b.name: b for b in (paper, StockbitBroker(), PluangBroker())}

    def notify_order(b, order: Order, event: str) -> None:
        """Telegram untuk order manual saham (diatur di tab Notifikasi)."""
        pos = b.positions.get(order.symbol) if isinstance(b, PaperBroker) else None
        watcher.notify_order(order.to_dict(), event, "idx", b.display_name, pos)

    paper.on_fill.append(lambda o: notify_order(paper, o, "filled"))

    def get_broker(name: str) -> Broker:
        if name not in brokers:
            raise HTTPException(404, f"Broker '{name}' tidak dikenal")
        return brokers[name]

    def prices_for(symbols) -> dict[str, float]:
        out = {}
        for s in symbols:
            try:
                out[s] = provider.quote(s).price
            except MarketDataError:
                pass
        return out

    @app.get("/api/config")
    def config():
        return {"market_data_provider": provider.name, "live_trading_enabled": settings.enable_live_trading,
                "lot_size": LOT_SIZE, "buy_fee_pct": settings.buy_fee_pct, "sell_fee_pct": settings.sell_fee_pct,
                "max_position_pct": settings.max_position_pct,
                "ara_pcts": ARA_PCTS, "arb_pcts": ARB_PCTS,
                "live_focus_seconds": settings.live_focus_seconds, "live_watch_seconds": settings.live_watch_seconds}

    def quote_payload(symbol: str) -> dict:
        q = provider.quote(symbol).to_dict()
        q["tradingview_symbol"] = tradingview_symbol(symbol)
        q["tradingview_url"] = tradingview_url(symbol)
        q["stockbit_url"] = stockbit_url(symbol)
        q["is_index"] = is_index(symbol)
        q["tick_size"] = None if q["is_index"] else tick_size(q["price"])
        limits = None if q["is_index"] else price_limits(q["prev_close"])
        q["reference_price"] = q["prev_close"]
        q["arb"], q["ara"] = limits if limits else (None, None)
        q["limit_status"] = limit_status(q["price"], limits)  # "ARA" / "ARB" / None
        q["server_time"] = time.time()
        q["market_open"] = is_idx_market_open(datetime.now(timezone.utc))
        # Seberapa jauh data tertinggal dari waktu sekarang (detik), bila sumber memberi waktu transaksi.
        q["data_age_seconds"] = round(q["server_time"] - q["market_time"]) if q.get("market_time") else None
        return q

    @app.get("/api/quote/{symbol}")
    def quote(symbol: str):
        try:
            return quote_payload(symbol)
        except MarketDataError as exc:
            raise HTTPException(502, str(exc))

    # ---- mode live: Server-Sent Events -----------------------------------
    @app.get("/api/stream")
    async def stream(request: Request, symbols: str = Query(..., max_length=400), focus: str | None = None):
        """Kirim event `quote` setiap kali harga berubah. Saham `focus` diperiksa tiap LIVE_FOCUS_SECONDS,
        sisanya tiap LIVE_WATCH_SECONDS. Data diambil lewat cache provider, jadi banyak tab tidak
        melipatgandakan permintaan ke sumber data."""
        syms = list(dict.fromkeys(normalize_symbol(s) for s in symbols.split(",") if s.strip()))[:30]
        if not syms:
            raise HTTPException(400, "Isi minimal satu simbol")
        focus_sym = normalize_symbol(focus) if focus else None

        async def events():
            due = {s: 0.0 for s in syms}
            last_sent: dict[str, tuple] = {}
            last_beat = started = time.monotonic()
            yield f"retry: 5000\nevent: hello\ndata: {json.dumps({'symbols': syms, 'focus': focus_sym})}\n\n"
            while not await request.is_disconnected():
                now = time.monotonic()
                if now - started > settings.live_stream_max_seconds:
                    break  # akhiri koneksi lama; browser menyambung ulang sendiri (retry 5 dtk)
                for s in [s for s, t in due.items() if t <= now]:
                    due[s] = now + (settings.live_focus_seconds if s == focus_sym else settings.live_watch_seconds)
                    try:
                        q = await run_in_threadpool(quote_payload, s)
                    except MarketDataError as exc:
                        yield f"event: stream_error\ndata: {json.dumps({'symbol': s, 'detail': str(exc)})}\n\n"
                        continue
                    key = (q["price"], q.get("market_time"), q["limit_status"])
                    if last_sent.get(s) != key:
                        last_sent[s] = key
                        yield f"event: quote\ndata: {json.dumps(q)}\n\n"
                if now - last_beat > 15:  # jaga koneksi tetap hidup lewat proxy/tunnel
                    last_beat = now
                    yield ": ping\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/candles/{symbol}")
    def candles(symbol: str, range: str = Query("6mo"), interval: str = Query("1d")):
        try:
            return [c.to_dict() for c in provider.candles(symbol, range, interval)]
        except MarketDataError as exc:
            raise HTTPException(502, str(exc))

    @app.get("/api/chart/{symbol}")
    def chart(symbol: str, range: str = Query("1y"), interval: str = Query("1d")):
        if range not in ("3mo", "6mo", "1y", "2y", "5y"):
            raise HTTPException(400, "Rentang harus salah satu dari 3mo, 6mo, 1y, 2y, 5y")
        if interval not in ("1d", "1m", "5m", "15m"):
            raise HTTPException(400, "Interval harus salah satu dari 1d, 1m, 5m, 15m")
        try:
            return build_chart(provider, paper, symbol, range, interval)
        except MarketDataError as exc:
            raise HTTPException(502, str(exc))

    @app.get("/api/analysis/{symbol}")
    def analysis(symbol: str):
        try:
            closes = [c.close for c in provider.candles(symbol, "1y", "1d")]
        except MarketDataError as exc:
            raise HTTPException(502, str(exc))
        return {"symbol": normalize_symbol(symbol), **analyze(closes)}

    @app.get("/api/brokers")
    def list_brokers():
        return [b.status() for b in brokers.values()]

    @app.get("/api/account")
    def account(broker: str = "paper"):
        b = get_broker(broker)
        try:
            if isinstance(b, PaperBroker):
                prices = prices_for(set(b.positions) | b.open_symbols())
                b.match_open_orders(prices)
                return b.account(prices)
            return b.account({})
        except BrokerNotAvailable as exc:
            raise HTTPException(503, str(exc))

    @app.get("/api/orders")
    def orders(broker: str = "paper", limit: int = Query(500, ge=1, le=10000)):
        try:
            return [o.to_dict() for o in get_broker(broker).orders()[:limit]]
        except BrokerNotAvailable as exc:
            raise HTTPException(503, str(exc))

    @app.post("/api/orders")
    def place_order(req: OrderRequest):
        b = get_broker(req.broker)
        if is_index(req.symbol):
            raise HTTPException(400, f"{normalize_symbol(req.symbol)} adalah indeks dan tidak bisa dibeli/dijual")
        if b.is_live:
            if not settings.enable_live_trading:
                raise HTTPException(403, "Live trading dinonaktifkan (set ENABLE_LIVE_TRADING=true).")
            if not req.confirm_live:
                raise HTTPException(400, "Order live butuh konfirmasi eksplisit (confirm_live=true).")
        try:
            quote = provider.quote(req.symbol)
        except MarketDataError as exc:
            raise HTTPException(502, str(exc))
        market, limits = quote.price, price_limits(quote.prev_close)

        # Batas risiko: nilai order beli maksimal MAX_POSITION_PCT dari ekuitas.
        if req.side == Side.BUY and isinstance(b, PaperBroker):
            equity = b.account(prices_for(b.positions))["equity"]
            value = req.lots * LOT_SIZE * (req.limit_price or market)
            limit = equity * settings.max_position_pct / 100
            if value > limit:
                raise HTTPException(400, f"Nilai order Rp{value:,.0f} melebihi batas "
                                         f"{settings.max_position_pct:g}% ekuitas (Rp{limit:,.0f}).")
        order = Order(req.symbol, req.side, req.lots, req.order_type, req.limit_price)
        try:
            if isinstance(b, PaperBroker):
                b.place_order(order, market, limits)
            else:
                b.place_order(order, market)
            notify_order(b, order, "placed")
            return order.to_dict()
        except BrokerNotAvailable as exc:
            raise HTTPException(503, str(exc))
        except BrokerError as exc:
            notify_order(b, order, "placed")  # ditolak, beserta alasannya
            raise HTTPException(400, str(exc))

    @app.delete("/api/orders/{order_id}")
    def cancel(order_id: str, broker: str = "paper"):
        try:
            b = get_broker(broker)
            order = b.cancel_order(order_id)
            notify_order(b, order, "cancelled")
            return order.to_dict()
        except BrokerNotAvailable as exc:
            raise HTTPException(503, str(exc))
        except BrokerError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/paper/reset")
    def reset_paper():
        paper.reset()
        autotrader.last_trade_at.clear()
        return paper.account({})

    # ---- auto-trading (khusus akun simulasi) ---------------------------
    @app.get("/api/autotrader")
    def autotrader_status():
        return autotrader.status()

    @app.put("/api/autotrader/config")
    def autotrader_config(data: dict = Body(...)):
        try:
            autotrader.update_config(data)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc))
        return autotrader.status()

    @app.post("/api/autotrader/start")
    def autotrader_start():
        autotrader.start()
        return autotrader.status()

    @app.post("/api/autotrader/stop")
    def autotrader_stop():
        autotrader.stop()
        return autotrader.status()

    @app.post("/api/autotrader/run-once")
    def autotrader_run_once():
        autotrader.run_cycle()
        return autotrader.status()

    # ---- notifikasi Telegram -------------------------------------------
    def notifier_call(fn):
        try:
            fn()
        except NotifierError as exc:
            raise HTTPException(400, str(exc))
        return watcher.status()

    @app.get("/api/notifications")
    def notifications_status():
        return watcher.status()

    @app.put("/api/notifications/config")
    def notifications_config(data: dict = Body(...)):
        try:
            watcher.update_config(data)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc))
        return watcher.status()

    @app.post("/api/notifications/test")
    def notifications_test():
        return notifier_call(watcher.send_test)

    @app.get("/api/notifications/chats")
    def notifications_chats():
        try:
            return watcher.telegram().recent_chats()
        except NotifierError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/notifications/start")
    def notifications_start():
        return notifier_call(watcher.start)

    @app.post("/api/notifications/stop")
    def notifications_stop():
        return notifier_call(watcher.stop)

    @app.post("/api/notifications/report")
    def notifications_report():
        if not watcher.status()["configured"]:
            raise HTTPException(400, "Isi bot token dan chat ID Telegram terlebih dahulu")
        entry = reporter.send_now()
        if entry["kind"] == "ERROR":
            raise HTTPException(502, entry["message"])
        return watcher.status()

    @app.get("/api/notifications/report/preview")
    def notifications_report_preview():
        return {"text": reporter.build(save=False)}

    @app.post("/api/notifications/run-once")
    def notifications_run_once():
        if not watcher.status()["configured"]:
            raise HTTPException(400, "Isi bot token dan chat ID Telegram terlebih dahulu")
        return notifier_call(watcher.scan)

    # ---- alert harga (Telegram) -----------------------------------------
    class AlertBody(BaseModel):
        symbol: str
        target: float = Field(gt=0)
        direction: str | None = None  # above / below / kosong = otomatis dari harga sekarang
        note: str = ""
        repeat: bool = False
        cooldown_minutes: int = 30

    def alert_call(fn):
        try:
            return fn()
        except AlertError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/alerts")
    def alerts_status():
        return price_alerts.status()

    @app.post("/api/alerts")
    def alerts_add(body: AlertBody):
        alert, notes = alert_call(lambda: price_alerts.add(body.symbol, body.target, body.direction, body.note,
                                                           body.repeat, body.cooldown_minutes))
        return {**price_alerts.status(), "created": alert.id, "notes": notes}

    @app.delete("/api/alerts/{alert_id}")
    def alerts_remove(alert_id: str):
        alert_call(lambda: price_alerts.remove(alert_id))
        return price_alerts.status()

    @app.post("/api/alerts/{alert_id}/rearm")
    def alerts_rearm(alert_id: str):
        alert_call(lambda: price_alerts.rearm(alert_id))
        return price_alerts.status()

    @app.post("/api/alerts/check")
    def alerts_check():
        return {**price_alerts.status(), "fired": price_alerts.check()}

    # ---- fundamental (laporan keuangan XBRL IDX) -----------------------
    MAX_UPLOAD = 30 * 1024 * 1024

    @app.get("/api/fundamentals/{symbol}")
    def fundamentals_get(symbol: str):
        sym = normalize_symbol(symbol)
        if is_index(sym):
            raise HTTPException(400, f"{sym} adalah indeks dan tidak memiliki laporan keuangan")
        reports, errors = fundamentals.reports(sym)
        price = None
        try:
            price = provider.quote(sym).price
        except MarketDataError as exc:
            errors.append(f"Harga {sym} tidak tersedia, rasio valuasi (PER/PBV) dilewati: {exc}")
        stored = {(r.year, r.period) for r in reports}
        return {
            "symbol": sym,
            "xbrl_dir": str(fundamentals.xbrl_dir),
            "reports": [{**r.to_dict(), "ratios": compute_ratios(r, price, settings.usd_idr_rate)} for r in reports],
            "download_links": [{"year": y, "period": p, "url": idx_report_url(sym, y, p), "stored": (y, p) in stored}
                               for y, p in recent_periods(date.today())],
            "errors": errors,
        }

    @app.post("/api/fundamentals/upload")
    async def fundamentals_upload(request: Request, ticker: str | None = None):
        content = await request.body()
        if not content:
            raise HTTPException(400, "File kosong")
        if len(content) > MAX_UPLOAD:
            raise HTTPException(413, "File terlalu besar (maks. 30 MB)")
        try:
            return fundamentals.save_upload(content, ticker).to_dict()
        except FundamentalsError as exc:
            raise HTTPException(400, str(exc))

    # ---- webhook alert TradingView --------------------------------------
    @app.post("/api/webhooks/tradingview")
    async def tradingview_webhook(request: Request, background: BackgroundTasks, secret: str | None = None):
        try:
            body = await request.body()
            payload = webhook.parse_body(body)
            if payload.get("type") == "bars":  # price feed dari skrip Pine, bukan alert sinyal
                webhook.check_secret(payload, secret)
                try:
                    result = await run_in_threadpool(feed.ingest, payload)
                except FeedError as exc:
                    return JSONResponse({"ok": False, "detail": str(exc)}, status_code=exc.status)
                return {"ok": True, **result}
            alert, entry = webhook.accept(body, secret, payload)
        except WebhookError as exc:
            if exc.status == 401:  # kode rahasia salah: catat & (bila perlu) kabari Telegram
                ip = (request.headers.get("cf-connecting-ip")
                      or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                      or (request.client.host if request.client else ""))
                background.add_task(webhook.report_auth_failure, ip)
                return JSONResponse({"ok": False, "detail": str(exc)}, status_code=exc.status,
                                    background=background)
            return JSONResponse({"ok": False, "detail": str(exc)}, status_code=exc.status)
        # Balas TradingView secepatnya (batas waktunya ~3 detik); aksi dijalankan setelahnya.
        background.add_task(webhook.process, alert)
        return {"ok": True, "received": entry["message"], "symbol": alert.symbol}

    @app.get("/api/webhooks")
    def webhook_status():
        return webhook.status()

    @app.put("/api/webhooks/config")
    def webhook_config(data: dict = Body(...)):
        try:
            webhook.update_config(data)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc))
        return webhook.status()

    @app.post("/api/webhooks/regenerate-secret")
    def webhook_regenerate():
        webhook.regenerate_secret()
        return webhook.status()

    # ---- price feed TradingView ----------------------------------------
    @app.get("/api/feed")
    def feed_status():
        return feed.status()

    @app.put("/api/feed/config")
    def feed_config(data: dict = Body(...)):
        try:
            feed.update_config(data)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc))
        return feed.status()

    @app.get("/api/feed/pine", response_class=PlainTextResponse)
    def feed_pine():
        return pine_script(webhook.config.secret, feed.config.symbols)

    # ---- backtest ------------------------------------------------------
    @app.post("/api/backtest")
    def backtest(body: BacktestBody):
        req = BacktestRequest(
            symbols=body.symbols or autotrader.config.symbols,
            period=body.period,
            initial_cash=body.initial_cash or settings.paper_starting_cash,
            execution=body.execution,
            strategy=body.strategy,
        )
        try:
            result = run_backtest(req, provider, autotrader.config, settings.buy_fee_pct,
                                  settings.sell_fee_pct, settings.max_position_pct)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc))
        result["id"] = db.save_backtest(result)  # simpan agar bisa dibuka & dibandingkan lagi
        return result

    @app.get("/api/backtests")
    def backtests_list(limit: int = Query(50, ge=1, le=500)):
        return db.list_backtests(limit)

    @app.get("/api/backtests/{backtest_id}")
    def backtests_get(backtest_id: int):
        result = db.get_backtest(backtest_id)
        if result is None:
            raise HTTPException(404, "Backtest tidak ditemukan")
        return result

    @app.delete("/api/backtests/{backtest_id}")
    def backtests_delete(backtest_id: int):
        if not db.delete_backtest(backtest_id):
            raise HTTPException(404, "Backtest tidak ditemukan")
        return {"deleted": backtest_id}

    @app.get("/api/backtest/periods")
    def backtest_periods():
        return list(PERIOD_DAYS)

    @app.get("/api/tick")
    def tick(price: float):
        return {"price": price, "tick_size": tick_size(price), "rounded": round_to_tick(price)}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    return app


app = create_app()
