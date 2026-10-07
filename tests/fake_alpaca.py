"""Server Alpaca tiruan untuk tes (bentuk respons mengikuti dokumentasi API resmi)."""
import json
import re
from datetime import datetime, timezone

import httpx

KEY, SECRET = "pk-test", "sk-test"
URL = "https://fake.alpaca"
NOW = datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")


class FakeAlpaca:
    def __init__(self):
        self.prices = {"AAPL": 230.0, "MSFT": 420.0, "BRK.B": 480.0}
        self.prev = {"AAPL": 225.0, "MSFT": 425.0, "BRK.B": 480.0}
        self.cash = 100000.0
        self.positions = {}   # simbol -> {"qty", "avg"}
        self.orders = []      # dict Alpaca
        self.requests = []
        self.is_open = True
        self.reject_next = None  # (status, code, message)
        self.next = 1

    # ---- bantuan untuk tes ------------------------------------------------
    def fill(self, client_id, price):
        o = next(o for o in self.orders if o["client_order_id"] == client_id)
        self._fill(o, price)

    def _fill(self, o, price):
        qty = float(o["qty"])
        o.update(status="filled", filled_qty=o["qty"], filled_avg_price=str(price), filled_at=iso(NOW.timestamp()))
        pos = self.positions.setdefault(o["symbol"], {"qty": 0.0, "avg": 0.0})
        if o["side"] == "buy":
            pos["avg"] = (pos["qty"] * pos["avg"] + qty * price) / (pos["qty"] + qty)
            pos["qty"] += qty
            self.cash -= qty * price
        else:
            pos["qty"] -= qty
            self.cash += qty * price
            if pos["qty"] <= 1e-9:
                del self.positions[o["symbol"]]

    def bars(self, symbol, timeframe, limit):
        step = {"1Min": 60, "5Min": 300, "15Min": 900, "1Hour": 3600, "1Day": 86400}[timeframe]
        base = self.prices[symbol]
        out = []
        for i in range(limit):  # urutan desc: yang terbaru dulu
            t = NOW.timestamp() - i * step
            c = base * (1 + 0.001 * ((-1) ** i))
            out.append({"t": iso(t), "o": c, "h": c * 1.002, "l": c * 0.998, "c": c, "v": 1000 + i, "n": 10, "vw": c})
        return out

    # ---- transport ----------------------------------------------------------
    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def _err(self, status, code, msg):
        return httpx.Response(status, json={"code": code, "message": msg})

    def handle(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        self.requests.append((req.method, req.url.path, dict(req.url.params), body))
        if req.headers.get("APCA-API-KEY-ID") != KEY or req.headers.get("APCA-API-SECRET-KEY") != SECRET:
            return self._err(401, 40110000, "request is not authorized")
        path, p = req.url.path, req.url.params
        if m := re.fullmatch(r"/v2/stocks/([A-Z.]+)/snapshot", path):
            s = m.group(1)
            if s not in self.prices:
                return self._err(404, 40410000, f"asset not found for {s}")
            return httpx.Response(200, json={
                "latestTrade": {"t": iso(NOW.timestamp()), "p": self.prices[s], "s": 100},
                "dailyBar": {"o": self.prev[s], "c": self.prices[s]}, "prevDailyBar": {"c": self.prev[s]}})
        if m := re.fullmatch(r"/v2/stocks/([A-Z.]+)/bars", path):
            s = m.group(1)
            if s not in self.prices:
                return self._err(400, 40010000, f"invalid symbol: {s}")
            return httpx.Response(200, json={"bars": self.bars(s, p["timeframe"], int(p["limit"])), "symbol": s,
                                             "next_page_token": None})
        if path == "/v2/clock":
            return httpx.Response(200, json={"is_open": self.is_open, "timestamp": iso(NOW.timestamp()),
                                             "next_open": "2026-10-07T13:30:00Z", "next_close": "2026-10-06T20:00:00Z"})
        if path == "/v2/account":
            mv = sum(self.prices[s] * v["qty"] for s, v in self.positions.items())
            return httpx.Response(200, json={"status": "ACTIVE", "currency": "USD", "cash": str(self.cash),
                                             "buying_power": str(self.cash * 2), "equity": str(self.cash + mv),
                                             "last_equity": "100000", "long_market_value": str(mv),
                                             "trading_blocked": False})
        if path == "/v2/positions":
            rows = []
            for s, v in self.positions.items():
                mv, cost = self.prices[s] * v["qty"], v["avg"] * v["qty"]
                rows.append({"symbol": s, "qty": str(v["qty"]), "avg_entry_price": str(v["avg"]),
                             "current_price": str(self.prices[s]), "market_value": str(mv),
                             "unrealized_pl": str(mv - cost), "unrealized_plpc": str((mv - cost) / cost),
                             "unrealized_intraday_pl": "0"})
            return httpx.Response(200, json=rows)
        if path == "/v2/orders" and req.method == "POST":
            if self.reject_next:
                status, code, msg = self.reject_next
                self.reject_next = None
                return self._err(status, code, msg)
            price = self.prices.get(body["symbol"])
            if price is None:
                return self._err(404, 40410000, f"asset \"{body['symbol']}\" not found")
            qty = body.get("qty") or str(round(float(body["notional"]) / price, 9))
            o = {"id": f"alp-{self.next}", "client_order_id": body["client_order_id"], "symbol": body["symbol"],
                 "side": body["side"], "type": body["type"], "time_in_force": body["time_in_force"], "qty": qty,
                 "notional": body.get("notional"), "limit_price": body.get("limit_price"), "status": "accepted",
                 "filled_qty": "0", "filled_avg_price": None, "submitted_at": iso(NOW.timestamp()),
                 "created_at": iso(NOW.timestamp()), "filled_at": None}
            self.next += 1
            self.orders.append(o)
            if body["side"] == "sell" and self.positions.get(body["symbol"], {"qty": 0})["qty"] + 1e-9 < float(qty):
                self.orders.pop()
                return self._err(403, 40310000, "insufficient qty available for order")
            marketable = body["type"] == "market" or (
                body["side"] == "buy" and price <= float(body["limit_price"])) or (
                body["side"] == "sell" and price >= float(body["limit_price"]))
            if marketable and self.is_open:
                self._fill(o, price)
            return httpx.Response(200, json=o)
        if path == "/v2/orders" and req.method == "GET":
            rows = [o for o in self.orders if p.get("status") != "open" or o["status"] in ("new", "accepted")]
            return httpx.Response(200, json=list(reversed(rows)))
        if m := re.fullmatch(r"/v2/orders/([\w-]+)", path) and req.method == "DELETE":
            oid = path.rsplit("/", 1)[1]
            o = next((o for o in self.orders if o["id"] == oid), None)
            if o is None or o["status"] == "filled":
                return self._err(422, 42210000, "order is not cancelable")
            o["status"] = "canceled"
            return httpx.Response(204)
        return self._err(404, 40410000, "not found")
