"""Endpoint /api/us/*: harga, grafik, akun & order saham Amerika lewat Alpaca Markets."""
import time
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from .alpaca import (INTERVALS, AlpacaAPI, AlpacaBroker, AlpacaProvider, UsOrder, normalize_us, us_market_open)
from .brokers.base import BrokerError, BrokerNotAvailable, OrderStatus, OrderType, Side
from .config import Settings
from .indicators import ema
from .market_data import MarketDataError
from .strategy import analyze


class UsOrderRequest(BaseModel):
    broker: str = "paper"
    symbol: str
    side: Side
    quantity: float | None = Field(None, gt=0)   # jumlah saham (boleh pecahan)
    notional: float | None = Field(None, gt=0)   # atau nilai order dalam USD
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = Field(None, gt=0)
    time_in_force: str = Field("day", pattern="^(day|gtc)$")
    confirm_live: bool = False


def register_us(app: FastAPI, settings: Settings, watcher=None, http=None) -> dict:
    paper = AlpacaBroker(AlpacaAPI(settings.alpaca_paper_url, settings.alpaca_paper_api_key,
                                   settings.alpaca_paper_api_secret, client=http), live=False)
    live = AlpacaBroker(AlpacaAPI(settings.alpaca_live_url, settings.alpaca_live_api_key,
                                  settings.alpaca_live_api_secret, client=http), live=True)
    brokers = {"paper": paper, "live": live}
    # Data pasar memakai API key akun yang tersedia (paper dulu); endpoint datanya sama untuk keduanya.
    key = next(((b.api.key, b.api.secret) for b in (paper, live) if b.available()), ("", ""))
    provider = AlpacaProvider(AlpacaAPI(settings.alpaca_data_url, *key, client=http), settings.alpaca_data_feed)

    def notify_order(b: AlpacaBroker, order: UsOrder, event: str) -> None:
        if watcher is not None:
            watcher.notify_order(order.to_dict(), event, "us", b.display_name)

    for _b in brokers.values():
        _b.on_fill.append(lambda o, b=_b: notify_order(b, o, "filled"))

    def get_broker(name: str) -> AlpacaBroker:
        if name not in brokers:
            raise HTTPException(404, f"Akun '{name}' tidak dikenal (paper / live)")
        return brokers[name]

    def market(fn):
        try:
            return fn()
        except MarketDataError as exc:
            raise HTTPException(502, str(exc))

    def trading(fn):
        try:
            return fn()
        except BrokerNotAvailable as exc:
            raise HTTPException(503, str(exc))
        except BrokerError as exc:
            raise HTTPException(502, str(exc))

    def need_symbol(symbol: str) -> str:
        sym = normalize_us(symbol)
        if not sym:
            raise HTTPException(400, f"'{symbol}' bukan kode saham AS (contoh: AAPL, MSFT, BRK.B)")
        return sym

    @app.get("/api/us/config")
    def us_config():
        clock = next((c for c in (b.market_clock() for b in brokers.values()) if c), None)
        return {"brokers": {k: b.status() for k, b in brokers.items()}, "intervals": list(INTERVALS),
                "live_trading_enabled": settings.enable_live_trading, "data_feed": settings.alpaca_data_feed,
                "data_configured": provider.api.configured, "max_position_pct": settings.us_max_position_pct,
                "max_live_order_usd": settings.us_max_order_usd,
                "market_open": clock["is_open"] if clock else us_market_open(),
                "market_clock_source": "alpaca" if clock else "perkiraan lokal (tanpa hari libur)",
                "next_open": clock["next_open"] if clock else None,
                "next_close": clock["next_close"] if clock else None}

    @app.get("/api/us/quote/{symbol}")
    def us_quote(symbol: str):
        sym = need_symbol(symbol)
        q = market(lambda: provider.quote(sym)).to_dict()
        q["server_time"] = time.time()
        q["data_age_seconds"] = round(q["server_time"] - q["market_time"]) if q.get("market_time") else None
        q["tradingview_url"] = f"https://www.tradingview.com/symbols/{sym.replace('.', '-')}/"
        return q

    @app.get("/api/us/chart/{symbol}")
    def us_chart(symbol: str, interval: str = Query("5m"), limit: int = Query(300, ge=50, le=1000)):
        sym = need_symbol(symbol)
        if interval not in INTERVALS:
            raise HTTPException(400, f"Interval harus salah satu dari {', '.join(INTERVALS)}")
        candles = market(lambda: provider.candles(sym, None, interval, limit))
        bars = [{"time": c.time, "open": c.open, "high": c.high, "low": c.low, "close": c.close,
                 "volume": c.volume} for c in candles]
        times, closes = [x["time"] for x in bars], [x["close"] for x in bars]

        def line(values):
            return [{"time": t, "value": v} for t, v in zip(times, values) if v is not None]

        return {"symbol": sym, "interval": interval, "bar_seconds": INTERVALS[interval][1], "bars": bars,
                "ema12": line(ema(closes, 12)), "ema26": line(ema(closes, 26)), "analysis": analyze(closes)}

    @app.get("/api/us/account")
    def us_account(broker: str = "paper"):
        return trading(get_broker(broker).account)

    @app.get("/api/us/orders")
    def us_orders(broker: str = "paper", limit: int = Query(100, ge=1, le=500)):
        return [o.to_dict() for o in trading(lambda: get_broker(broker).orders(limit))]

    @app.post("/api/us/orders")
    def us_place(req: UsOrderRequest):
        b = get_broker(req.broker)
        if b.is_live:
            if not settings.enable_live_trading:
                raise HTTPException(403, "Order Alpaca live dinonaktifkan (set ENABLE_LIVE_TRADING=true).")
            if not req.confirm_live:
                raise HTTPException(400, "Order Alpaca live butuh konfirmasi eksplisit (confirm_live=true).")
        if not b.available():
            raise HTTPException(503, b.status()["note"])
        if req.order_type == OrderType.LIMIT and not req.limit_price:
            raise HTTPException(400, "Order limit butuh harga limit (limit_price)")
        if not (req.quantity or req.notional):
            raise HTTPException(400, "Isi jumlah saham (quantity) atau nilai order (notional)")
        sym = need_symbol(req.symbol)
        price = req.limit_price if req.order_type == OrderType.LIMIT else market(lambda: provider.quote(sym)).price
        qty, notional = req.quantity, req.notional
        if req.order_type == OrderType.LIMIT and notional:  # order limit tidak boleh berbasis nilai: ubah ke jumlah saham
            qty, notional = round(notional / price, 6), None
        value = notional if notional else qty * price
        if b.is_live and settings.us_max_order_usd > 0 and value > settings.us_max_order_usd:
            raise HTTPException(400, f"Nilai order ${value:,.2f} melebihi batas akun live ${settings.us_max_order_usd:,.2f} "
                                     "(US_MAX_ORDER_USD)")
        if req.side == Side.BUY:  # batas risiko per posisi seperti akun saham & crypto
            equity = trading(b.account)["equity"]
            cap = equity * settings.us_max_position_pct / 100
            if value > cap:
                raise HTTPException(400, f"Nilai order ${value:,.2f} melebihi batas {settings.us_max_position_pct:g}% "
                                         f"ekuitas (${cap:,.2f})")
        order = UsOrder(sym, req.side, req.order_type, qty, notional, req.limit_price, req.time_in_force)
        try:
            b.place_order(order)
        except BrokerNotAvailable as exc:
            raise HTTPException(503, str(exc))
        except BrokerError as exc:
            notify_order(b, order, "placed")  # ditolak, beserta alasannya
            raise HTTPException(400, str(exc))
        notify_order(b, order, "placed")
        return order.to_dict()

    @app.delete("/api/us/orders/{order_id}")
    def us_cancel(order_id: str, broker: str = "paper"):
        b = get_broker(broker)
        try:
            order = b.cancel_order(order_id)
        except BrokerNotAvailable as exc:
            raise HTTPException(503, str(exc))
        except BrokerError as exc:
            raise HTTPException(400, str(exc))
        notify_order(b, order, "cancelled")
        return order.to_dict()

    return {"provider": provider, "brokers": brokers}
