"""Server Binance Spot tiruan untuk tes (format respons mengikuti dokumentasi API resmi)."""
import hashlib
import hmac
import json
import math
from urllib.parse import parse_qsl

import httpx

KEY, SECRET = "test-key", "test-secret"
NOW_MS = 1_791_340_800_000

EXCHANGE_INFO = {
    "BTCUSDT": {"symbol": "BTCUSDT", "status": "TRADING", "baseAsset": "BTC", "quoteAsset": "USDT", "filters": [
        {"filterType": "PRICE_FILTER", "minPrice": "0.01000000", "maxPrice": "1000000.00000000", "tickSize": "0.01000000"},
        {"filterType": "LOT_SIZE", "minQty": "0.00001000", "maxQty": "9000.00000000", "stepSize": "0.00001000"},
        {"filterType": "NOTIONAL", "minNotional": "5.00000000", "applyMinToMarket": True}]},
    "ETHUSDT": {"symbol": "ETHUSDT", "status": "TRADING", "baseAsset": "ETH", "quoteAsset": "USDT", "filters": [
        {"filterType": "PRICE_FILTER", "minPrice": "0.01000000", "maxPrice": "1000000.00000000", "tickSize": "0.01000000"},
        {"filterType": "LOT_SIZE", "minQty": "0.00010000", "maxQty": "9000.00000000", "stepSize": "0.00010000"},
        {"filterType": "NOTIONAL", "minNotional": "5.00000000"}]},
    "ETHBTC": {"symbol": "ETHBTC", "status": "TRADING", "baseAsset": "ETH", "quoteAsset": "BTC", "filters": [
        {"filterType": "PRICE_FILTER", "tickSize": "0.00001000"},
        {"filterType": "LOT_SIZE", "minQty": "0.00010000", "stepSize": "0.00010000"},
        {"filterType": "NOTIONAL", "minNotional": "0.00010000"}]},
}


class FakeBinance:
    def __init__(self):
        self.prices = {"BTCUSDT": 65000.0, "ETHUSDT": 3200.0, "ETHBTC": 0.05}
        self.series = {}  # simbol -> deret penutupan candle (untuk memancing sinyal BELI/JUAL)
        self.balances = {"USDT": 1000.0, "BTC": 0.5, "BNB": 2.0}
        self.orders = {}
        self.next_id = 1
        self.requests = []
        self.reject_next = None  # (code, msg)

    # ---- helpers -----------------------------------------------------
    def set_wave(self, symbol, n, base):
        """Candle penutupan base*(1+0,2*sin(i/8)) untuk i<n: n=42 -> sinyal BELI, n=52 -> sinyal JUAL."""
        self.series[symbol] = [base * (1 + 0.2 * math.sin(i / 8)) for i in range(n)]
        self.prices[symbol] = round(self.series[symbol][-1], 2)

    def klines(self, symbol, interval, limit):
        closes = self.series.get(symbol) or [self.prices[symbol]] * limit
        rows = []
        for i, c in enumerate(closes):
            o = closes[i - 1] if i else c
            t = NOW_MS - (len(closes) - i) * 3_600_000
            rows.append([t, f"{o:.2f}", f"{max(o, c) * 1.001:.2f}", f"{min(o, c) * 0.999:.2f}", f"{c:.2f}", "12.5",
                         t + 3_599_999, "0", 10, "0", "0", "0"])
        return rows

    def _err(self, code, msg, status=400):
        return httpx.Response(status, json={"code": code, "msg": msg})

    def _order_resp(self, o, full=False):
        r = {k: o[k] for k in ("symbol", "orderId", "clientOrderId", "price", "origQty", "executedQty",
                               "cummulativeQuoteQty", "status", "timeInForce", "type", "side")}
        r["transactTime"] = NOW_MS
        if full:
            r["fills"] = o["fills"]
        return r

    def _fill(self, o, price):
        info = EXCHANGE_INFO[o["symbol"]]
        base, quote = info["baseAsset"], info["quoteAsset"]
        qty = float(o["origQty"])
        value = qty * price
        if o["side"] == "BUY":
            if self.balances.get(quote, 0) < value - 1e-9:
                return False
            fee = qty * 0.001
            self.balances[quote] -= value
            self.balances[base] = self.balances.get(base, 0) + qty - fee
            fills = [{"price": f"{price:.2f}", "qty": f"{qty:.8f}", "commission": f"{fee:.8f}", "commissionAsset": base}]
        else:
            if self.balances.get(base, 0) < qty - 1e-12:
                return False
            fee = value * 0.001
            self.balances[base] -= qty
            self.balances[quote] = self.balances.get(quote, 0) + value - fee
            fills = [{"price": f"{price:.2f}", "qty": f"{qty:.8f}", "commission": f"{fee:.8f}", "commissionAsset": quote}]
        o.update(status="FILLED", executedQty=f"{qty:.8f}", cummulativeQuoteQty=f"{value:.8f}", fills=fills)
        return True

    # ---- router ------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        q = dict(parse_qsl(request.url.query.decode()))
        self.requests.append((request.method, path, q))
        if path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": NOW_MS})
        if path == "/api/v3/exchangeInfo":
            if q.get("symbol") not in EXCHANGE_INFO:
                return self._err(-1121, "Invalid symbol.")
            return httpx.Response(200, json={"symbols": [EXCHANGE_INFO[q["symbol"]]]})
        if path == "/api/v3/ticker/24hr":
            if q.get("symbol") not in self.prices:
                return self._err(-1121, "Invalid symbol.")
            p = self.prices[q["symbol"]]
            return httpx.Response(200, json={"symbol": q["symbol"], "lastPrice": f"{p:.8f}", "openPrice": f"{p / 1.02:.8f}",
                                             "priceChangePercent": "2.000", "closeTime": NOW_MS})
        if path == "/api/v3/ticker/price":
            return httpx.Response(200, json=[{"symbol": s, "price": f"{p:.8f}"} for s, p in self.prices.items()])
        if path == "/api/v3/klines":
            return httpx.Response(200, json=self.klines(q["symbol"], q["interval"], int(q["limit"])))
        return self.signed(request, path, q)

    def signed(self, request, path, q):
        if request.headers.get("X-MBX-APIKEY") != KEY:
            return self._err(-2015, "Invalid API-key, IP, or permissions for action.", 401)
        query = request.url.query.decode()
        payload, _, sig = query.rpartition("&signature=")
        if hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest() != sig:
            return self._err(-1022, "Signature for this request is not valid.")
        if path == "/api/v3/account":
            return httpx.Response(200, json={"canTrade": True, "balances": [
                {"asset": a, "free": f"{v:.8f}", "locked": "0.00000000"} for a, v in self.balances.items() if v > 0]})
        if path == "/api/v3/order" and request.method == "POST":
            if self.reject_next:
                code, msg = self.reject_next
                self.reject_next = None
                return self._err(code, msg)
            sym = q["symbol"]
            info = EXCHANGE_INFO[sym]
            step = float(next(f for f in info["filters"] if f["filterType"] == "LOT_SIZE")["stepSize"])
            qty = float(q["quantity"])
            if abs(round(qty / step) * step - qty) > 1e-12:
                return self._err(-1013, "Filter failure: LOT_SIZE")
            o = {"symbol": sym, "orderId": self.next_id, "clientOrderId": q["newClientOrderId"], "price": q.get("price", "0"),
                 "origQty": q["quantity"], "executedQty": "0", "cummulativeQuoteQty": "0", "status": "NEW",
                 "timeInForce": q.get("timeInForce", "GTC"), "type": q["type"], "side": q["side"], "fills": []}
            self.next_id += 1
            market = self.prices[sym]
            if q["type"] == "MARKET":
                if not self._fill(o, market):
                    return self._err(-2010, "Account has insufficient balance for requested action.")
            elif (q["side"] == "BUY" and market <= float(q["price"])) or (q["side"] == "SELL" and market >= float(q["price"])):
                self._fill(o, float(q["price"]))
            self.orders[o["clientOrderId"]] = o
            return httpx.Response(200, json=self._order_resp(o, full=True))
        if path == "/api/v3/order" and request.method in ("GET", "DELETE"):
            o = self.orders.get(q.get("origClientOrderId"))
            if o is None:
                return self._err(-2013, "Order does not exist.")
            if request.method == "DELETE":
                o["status"] = "CANCELED"
            return httpx.Response(200, json=self._order_resp(o))
        return self._err(-1000, f"unknown {path}", 404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def fill_open(self, client_id, price):
        """Simulasikan order limit yang tersentuh di bursa."""
        o = self.orders[client_id]
        self._fill(o, price)


def as_json(resp):
    return json.loads(resp.content)
