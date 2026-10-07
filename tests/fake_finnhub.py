"""Server Finnhub (+ Yahoo chart) tiruan untuk tes; bentuk respons mengikuti dokumentasi resmi."""
import httpx

KEY = "fh-test-key"
BASE = "https://fake.finnhub/api/v1"
NOW = 1_791_300_000


class FakeFinnhub:
    def __init__(self):
        self.prices = {"AAPL": 230.0, "MSFT": 420.0, "BRK.B": 480.0}
        self.prev = {"AAPL": 225.0, "MSFT": 425.0, "BRK.B": 480.0}
        self.candles_allowed = False  # paket gratis: /stock/candle -> 403
        self.yahoo_ok = True
        self.is_open = True
        self.requests = []

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def _bars(self, n, step, base):
        return [(NOW - (n - i) * step, base * (1 + 0.001 * (-1) ** i)) for i in range(n)]

    def handle(self, req: httpx.Request) -> httpx.Response:
        p = dict(req.url.params)
        self.requests.append((req.url.host, req.url.path, p, req.headers.get("X-Finnhub-Token")))
        if req.url.host == "query1.finance.yahoo.com":
            if not self.yahoo_ok:
                return httpx.Response(503)
            sym = req.url.path.rsplit("/", 1)[1].replace("-", ".")
            if sym not in self.prices:
                return httpx.Response(404)
            rows = self._bars(120, 300, self.prices[sym])
            return httpx.Response(200, json={"chart": {"result": [{"timestamp": [t for t, _ in rows], "indicators": {
                "quote": [{"open": [c for _, c in rows], "high": [c * 1.001 for _, c in rows],
                           "low": [c * 0.999 for _, c in rows], "close": [c for _, c in rows],
                           "volume": [1000] * len(rows)}]}}]}})
        if req.headers.get("X-Finnhub-Token") != KEY:
            return httpx.Response(401, json={"error": "Invalid API key"})
        if req.url.path.endswith("/quote"):
            s = p["symbol"]
            if s not in self.prices:
                return httpx.Response(200, json={"c": 0, "d": None, "dp": None, "h": 0, "l": 0, "o": 0, "pc": 0, "t": 0})
            c, pc = self.prices[s], self.prev[s]
            return httpx.Response(200, json={"c": c, "d": c - pc, "dp": (c / pc - 1) * 100, "h": c, "l": pc, "o": pc,
                                             "pc": pc, "t": NOW})
        if req.url.path.endswith("/stock/candle"):
            if not self.candles_allowed:
                return httpx.Response(403, json={"error": "You don't have access to this resource."})
            rows = self._bars(100, 300, self.prices[p["symbol"]])
            return httpx.Response(200, json={"s": "ok", "t": [t for t, _ in rows], "o": [c for _, c in rows],
                                             "h": [c * 1.001 for _, c in rows], "l": [c * 0.999 for _, c in rows],
                                             "c": [c for _, c in rows], "v": [500] * len(rows)})
        if req.url.path.endswith("/stock/market-status"):
            return httpx.Response(200, json={"exchange": "US", "isOpen": self.is_open, "holiday": None, "t": NOW})
        return httpx.Response(404, json={"error": "not found"})
