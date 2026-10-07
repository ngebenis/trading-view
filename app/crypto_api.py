"""Endpoint /api/crypto/*: harga, grafik, akun, order & auto-trading crypto lewat Binance."""
import time
from bisect import bisect_right

from fastapi import Body, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from .binance import KLINE_INTERVALS, BinanceAPI, BinanceProvider, normalize_pair, split_pair
from .brokers.base import BrokerError, BrokerNotAvailable, OrderStatus, OrderType, Side
from .config import Settings
from .crypto import BinanceBroker, CryptoAutoTrader, CryptoOrder, CryptoPaperBroker
from .indicators import ema
from .market_data import MarketDataError
from .strategy import analyze

WIB_OFFSET = 7 * 3600  # waktu bar digeser +7 jam agar sumbu grafik menampilkan WIB


class CryptoOrderRequest(BaseModel):
    broker: str = "paper"
    symbol: str
    side: Side
    quantity: float | None = Field(None, gt=0)       # jumlah aset dasar (mis. BTC)
    quote_amount: float | None = Field(None, gt=0)   # atau: nilai dalam aset kutipan (mis. 50 USDT)
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = Field(None, gt=0)
    confirm_live: bool = False


def register_crypto(app: FastAPI, settings: Settings, db, watcher=None, http=None) -> dict:
    data_api = BinanceAPI(settings.binance_data_url, client=http)
    provider = BinanceProvider(data_api)
    paper = CryptoPaperBroker(db, settings.crypto_paper_starting_cash, settings.crypto_fee_pct)
    testnet = BinanceBroker(BinanceAPI(settings.binance_testnet_url, settings.binance_testnet_api_key,
                                       settings.binance_testnet_api_secret, client=http), db, live=False)
    live = BinanceBroker(BinanceAPI(settings.binance_api_url, settings.binance_api_key,
                                    settings.binance_api_secret, client=http), db, live=True)
    brokers = {"paper": paper, "testnet": testnet, "binance": live}
    bot = CryptoAutoTrader(brokers, provider, db, settings.crypto_max_position_pct, settings.enable_live_trading)
    if watcher is not None:
        watcher.crypto_provider = provider  # sinyal crypto di notifikasi Telegram
        bot.listeners.append(watcher.on_autotrader_log)  # notifikasi transaksi lewat Telegram yang sama

    def notify_order(b, order: CryptoOrder, event: str) -> None:
        """Telegram untuk order manual crypto (diatur di tab Notifikasi)."""
        if watcher is None:
            return
        pos = b.positions.get(order.symbol) if isinstance(b, CryptoPaperBroker) else None
        watcher.notify_order(order.to_dict(), event, "crypto", b.display_name, pos)

    for _b in brokers.values():
        _b.on_fill.append(lambda o, b=_b: notify_order(b, o, "filled"))

    def get_broker(name: str):
        if name not in brokers:
            raise HTTPException(404, f"Akun crypto '{name}' tidak dikenal (paper / testnet / binance)")
        return brokers[name]

    def market(fn):
        try:
            return fn()
        except MarketDataError as exc:
            raise HTTPException(502, str(exc))

    def prices_for(broker, extra=()) -> dict[str, float]:
        """Harga untuk menilai akun: semua pasangan sekaligus (satu permintaan), cadangan per simbol."""
        try:
            return provider.all_prices()
        except MarketDataError:
            out = {}
            syms = set(extra) | set(getattr(broker, "positions", {})) | broker.open_symbols()
            for s in syms:
                try:
                    out[s] = provider.quote(s).price
                except MarketDataError:
                    pass
            return out

    @app.get("/api/crypto/config")
    def crypto_config():
        return {"brokers": {k: b.status() for k, b in brokers.items()},
                "live_trading_enabled": settings.enable_live_trading, "fee_pct": settings.crypto_fee_pct,
                "max_position_pct": settings.crypto_max_position_pct, "intervals": list(KLINE_INTERVALS),
                "data_url": settings.binance_data_url}

    @app.get("/api/crypto/quote/{symbol}")
    def crypto_quote(symbol: str):
        sym = normalize_pair(symbol)
        if not split_pair(sym):
            raise HTTPException(400, f"'{symbol}' bukan pasangan crypto (contoh: BTCUSDT, ETH/USDT)")
        q = market(lambda: provider.quote(sym)).to_dict()
        try:
            q["rules"] = provider.rules(sym).to_dict()
        except MarketDataError:
            q["rules"] = None
        base, quote_asset = split_pair(sym)
        q.update(base=base, quote_asset=quote_asset, server_time=time.time(),
                 tradingview_url=f"https://www.tradingview.com/symbols/{sym}/?exchange=BINANCE",
                 binance_url=f"https://www.binance.com/en/trade/{base}_{quote_asset}?type=spot")
        q["data_age_seconds"] = round(q["server_time"] - q["market_time"]) if q.get("market_time") else None
        return q

    @app.get("/api/crypto/chart/{symbol}")
    def crypto_chart(symbol: str, interval: str = Query("1h"), limit: int = Query(300, ge=50, le=1000),
                     broker: str = "paper"):
        sym = normalize_pair(symbol)
        if interval not in KLINE_INTERVALS:
            raise HTTPException(400, f"Interval harus salah satu dari {', '.join(KLINE_INTERVALS)}")
        b = get_broker(broker)
        candles = market(lambda: provider.candles(sym, None, interval, limit))
        bars = [{"time": c.time + WIB_OFFSET, "open": c.open, "high": c.high, "low": c.low, "close": c.close,
                 "volume": c.volume} for c in candles]
        times, closes = [x["time"] for x in bars], [x["close"] for x in bars]
        seconds = KLINE_INTERVALS[interval]
        groups: dict[tuple, dict] = {}
        for o in b.orders():
            if o.symbol != sym or o.status != OrderStatus.FILLED or not o.filled_qty:
                continue
            t = int((o.filled_at or o.created_at) + WIB_OFFSET)
            i = bisect_right(times, t) - 1
            if i < 0 or t - times[i] >= seconds * 2:
                continue
            g = groups.setdefault((times[i], o.side.value), {"time": times[i], "side": o.side.value, "quantity": 0.0,
                                                           "value": 0.0, "count": 0, "sources": set()})
            g["quantity"] += o.filled_qty
            g["value"] += o.filled_qty * o.fill_price
            g["count"] += 1
            g["sources"].add(o.source)
        markers = sorted(({"time": g["time"], "side": g["side"], "quantity": g["quantity"], "count": g["count"],
                           "avg_price": g["value"] / g["quantity"], "sources": sorted(g["sources"])}
                          for g in groups.values()), key=lambda m: (m["time"], m["side"]))

        def line(values):
            return [{"time": t, "value": v} for t, v in zip(times, values) if v is not None]

        return {"symbol": sym, "interval": interval, "bar_seconds": seconds, "bars": bars,
                "ema12": line(ema(closes, 12)), "ema26": line(ema(closes, 26)), "markers": markers,
                "analysis": analyze(closes)}

    @app.get("/api/crypto/account")
    def crypto_account(broker: str = "paper"):
        b = get_broker(broker)
        prices = prices_for(b)
        try:
            b.match_open_orders(prices)
            return b.account(prices)
        except BrokerNotAvailable as exc:
            raise HTTPException(503, str(exc))
        except BrokerError as exc:
            raise HTTPException(502, str(exc))

    @app.get("/api/crypto/orders")
    def crypto_orders(broker: str = "paper", limit: int = Query(200, ge=1, le=5000)):
        return [o.to_dict() for o in get_broker(broker).orders()[:limit]]

    @app.post("/api/crypto/orders")
    def crypto_place(req: CryptoOrderRequest):
        b = get_broker(req.broker)
        if b.is_live:
            if not settings.enable_live_trading:
                raise HTTPException(403, "Order Binance asli dinonaktifkan (set ENABLE_LIVE_TRADING=true).")
            if not req.confirm_live:
                raise HTTPException(400, "Order Binance asli butuh konfirmasi eksplisit (confirm_live=true).")
        if not b.available():
            raise HTTPException(503, b.status()["note"])
        if req.order_type == OrderType.LIMIT and not req.limit_price:
            raise HTTPException(400, "Order limit butuh harga limit (limit_price)")
        sym = normalize_pair(req.symbol)
        quote = market(lambda: provider.quote(sym))
        rules = market(lambda: provider.rules(sym))
        price = req.limit_price if req.order_type == OrderType.LIMIT and req.limit_price else quote.price
        qty = req.quantity or (req.quote_amount / price if req.quote_amount else None)
        if not qty:
            raise HTTPException(400, "Isi jumlah (quantity) atau nilai order (quote_amount)")
        if req.side == Side.BUY and isinstance(b, CryptoPaperBroker):  # batas risiko seperti akun saham
            equity = b.account(prices_for(b, [sym]))["equity"]
            cap = equity * settings.crypto_max_position_pct / 100
            if qty * price > cap:
                raise HTTPException(400, f"Nilai order {qty * price:,.2f} {rules.quote} melebihi batas "
                                         f"{settings.crypto_max_position_pct:g}% ekuitas ({cap:,.2f})")
        order = CryptoOrder(sym, req.side, qty, req.order_type, req.limit_price)
        try:
            b.place_order(order, quote.price, rules)
        except BrokerNotAvailable as exc:
            raise HTTPException(503, str(exc))
        except BrokerError as exc:
            notify_order(b, order, "placed")  # ditolak, beserta alasannya
            raise HTTPException(400, str(exc))
        notify_order(b, order, "placed")
        return order.to_dict()

    @app.delete("/api/crypto/orders/{order_id}")
    def crypto_cancel(order_id: str, broker: str = "paper"):
        try:
            b = get_broker(broker)
            order = b.cancel_order(order_id)
            notify_order(b, order, "cancelled")
            return order.to_dict()
        except BrokerNotAvailable as exc:
            raise HTTPException(503, str(exc))
        except BrokerError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/crypto/paper/reset")
    def crypto_reset():
        paper.reset()
        bot.last_trade_at.clear()
        return paper.account({})

    # ---- auto-trading crypto ---------------------------------------------
    @app.get("/api/crypto/autotrader")
    def crypto_bot_status():
        return bot.status()

    @app.put("/api/crypto/autotrader/config")
    def crypto_bot_config(data: dict = Body(...)):
        try:
            bot.update_config(data)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc))
        return bot.status()

    @app.post("/api/crypto/autotrader/start")
    def crypto_bot_start(data: dict = Body(default={})):
        if bot.config.broker == "binance" and not data.get("confirm_live"):
            raise HTTPException(400, "Bot di akun Binance asli butuh konfirmasi eksplisit (confirm_live=true).")
        if not bot.target.available():
            raise HTTPException(503, bot.target.status()["note"])
        bot.start()
        return bot.status()

    @app.post("/api/crypto/autotrader/stop")
    def crypto_bot_stop():
        bot.stop()
        return bot.status()

    @app.post("/api/crypto/autotrader/run-once")
    def crypto_bot_run_once():
        if bot.config.broker == "binance":
            raise HTTPException(400, "Siklus manual hanya untuk simulasi / testnet")
        bot.run_cycle()
        return bot.status()

    return {"provider": provider, "brokers": brokers, "bot": bot}
