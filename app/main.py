"""Server API + UI. Jalankan: uvicorn app.main:app --reload"""
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .brokers import (Broker, BrokerError, BrokerNotAvailable, Order, OrderType, PaperBroker,
                      PluangBroker, Side, StockbitBroker)
from .config import Settings, settings as default_settings
from .idx_rules import LOT_SIZE, normalize_symbol, round_to_tick, tick_size, tradingview_symbol
from .market_data import MarketDataError, get_provider
from .strategy import analyze

STATIC = Path(__file__).parent / "static"


class OrderRequest(BaseModel):
    broker: str = "paper"
    symbol: str
    side: Side
    lots: int = Field(gt=0)
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    confirm_live: bool = False


def create_app(settings: Settings = default_settings, provider=None) -> FastAPI:
    app = FastAPI(title="IDX Trading View", version="0.1.0")
    provider = provider or get_provider(settings.market_data_provider)
    paper = PaperBroker(settings.data_dir / "paper_account.json", settings.paper_starting_cash,
                        settings.buy_fee_pct, settings.sell_fee_pct)
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
        q["tick_size"] = tick_size(q["price"])
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
        return paper.account({})

    @app.get("/api/tick")
    def tick(price: float):
        return {"price": price, "tick_size": tick_size(price), "rounded": round_to_tick(price)}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    return app


app = create_app()
