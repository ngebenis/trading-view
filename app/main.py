"""Server API + UI. Jalankan: uvicorn app.main:app --reload"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .autotrader import AutoTrader
from .backtest import PERIOD_DAYS, BacktestRequest, run_backtest
from .brokers import (Broker, BrokerError, BrokerNotAvailable, Order, OrderType, PaperBroker,
                      PluangBroker, Side, StockbitBroker)
from .config import Settings, settings as default_settings
from .idx_rules import (LOT_SIZE, is_index, normalize_symbol, round_to_tick, stockbit_url, tick_size,
                        tradingview_symbol, tradingview_url)
from .market_data import MarketDataError, get_provider
from .notifier import NotifierError, SignalWatcher
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


def create_app(settings: Settings = default_settings, provider=None, telegram_http=None) -> FastAPI:
    provider = provider or get_provider(settings.market_data_provider)
    paper = PaperBroker(settings.data_dir / "paper_account.json", settings.paper_starting_cash,
                        settings.buy_fee_pct, settings.sell_fee_pct)
    autotrader = AutoTrader(paper, provider, settings.data_dir / "autotrader.json", settings.max_position_pct)
    watcher = SignalWatcher(provider, settings.data_dir / "notifications.json", settings.telegram_bot_token,
                            settings.telegram_chat_id, http=telegram_http)
    autotrader.listeners.append(watcher.on_autotrader_log)

    @asynccontextmanager
    async def lifespan(_app):
        if autotrader.config.enabled:  # lanjutkan bila sebelumnya aktif
            autotrader.start()
        if watcher.config.enabled:
            try:
                watcher.start()
            except NotifierError:
                pass
        yield
        if autotrader.running:
            autotrader._stop.set()
        watcher.shutdown()

    app = FastAPI(title="IDX Trading View", version="0.2.0", lifespan=lifespan)
    app.state.autotrader = autotrader
    app.state.watcher = watcher
    brokers: dict[str, Broker] = {b.name: b for b in (paper, StockbitBroker(), PluangBroker())}

    def get_broker(name: str) -> Broker:
        if name not in brokers:
            raise HTTPException(404, f"Broker '{name}' tidak dikenal")
        return brokers[name]

    def price_of(symbol: str) -> float:
        try:
            return provider.quote(symbol).price
        except MarketDataError as exc:
            raise HTTPException(502, str(exc))

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
                "max_position_pct": settings.max_position_pct}

    @app.get("/api/quote/{symbol}")
    def quote(symbol: str):
        try:
            q = provider.quote(symbol).to_dict()
        except MarketDataError as exc:
            raise HTTPException(502, str(exc))
        q["tradingview_symbol"] = tradingview_symbol(symbol)
        q["tradingview_url"] = tradingview_url(symbol)
        q["stockbit_url"] = stockbit_url(symbol)
        q["is_index"] = is_index(symbol)
        q["tick_size"] = None if q["is_index"] else tick_size(q["price"])
        return q

    @app.get("/api/candles/{symbol}")
    def candles(symbol: str, range: str = Query("6mo"), interval: str = Query("1d")):
        try:
            return [c.to_dict() for c in provider.candles(symbol, range, interval)]
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
    def orders(broker: str = "paper"):
        try:
            return [o.to_dict() for o in get_broker(broker).orders()]
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
        market = price_of(req.symbol)

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
            return b.place_order(order, market).to_dict()
        except BrokerNotAvailable as exc:
            raise HTTPException(503, str(exc))
        except BrokerError as exc:
            raise HTTPException(400, str(exc))

    @app.delete("/api/orders/{order_id}")
    def cancel(order_id: str, broker: str = "paper"):
        try:
            return get_broker(broker).cancel_order(order_id).to_dict()
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

    @app.post("/api/notifications/run-once")
    def notifications_run_once():
        if not watcher.status()["configured"]:
            raise HTTPException(400, "Isi bot token dan chat ID Telegram terlebih dahulu")
        return notifier_call(watcher.scan)

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
            return run_backtest(req, provider, autotrader.config, settings.buy_fee_pct,
                                settings.sell_fee_pct, settings.max_position_pct)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc))

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
