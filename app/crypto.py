"""Trading crypto lewat Binance: akun simulasi (USDT), Binance Spot Testnet, dan akun Binance asli.

Berbeda dengan saham IDX: jumlah boleh pecahan (mengikuti stepSize Binance), pasar buka 24 jam,
tanpa ARA/ARB, fee bawaan 0,1% per transaksi, dan nilai dihitung dalam aset kutipan (USDT).

Bot auto-trading crypto hanya mengelola posisi yang dibukanya sendiri (dihitung dari order "auto"
yang terisi), sehingga saldo lain di akun Binance Anda tidak pernah ikut dijual oleh bot.
"""
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from decimal import Decimal

from .autotrader import AutoTrader
from .binance import (KLINE_INTERVALS, BinanceAPI, BinanceError, SymbolRules, fmt_decimal, normalize_pair,
                      split_pair)
from .brokers.base import BrokerError, BrokerNotAvailable, OrderStatus, OrderType, Side
from .market_data import MarketDataError
from .strategy import analyze

PAPER_QUOTE = "USDT"
DUST = 1e-12


def usdt(x: float | None) -> str:
    return "—" if x is None else f"{x:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def qty_str(x: float) -> str:
    return fmt_decimal(Decimal(str(x)))


def price_str(x: float) -> str:
    """Harga crypto bisa sangat kecil (mis. 0,00001234) atau besar (mis. 65.432,10)."""
    if x >= 1:
        return usdt(x)
    return qty_str(float(f"{x:.8g}")).replace(".", ",")


@dataclass
class CryptoOrder:
    symbol: str
    side: Side
    quantity: float
    order_type: OrderType
    limit_price: float | None = None
    broker: str = "paper"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:20])
    status: OrderStatus = OrderStatus.OPEN
    fill_price: float | None = None
    filled_qty: float = 0.0      # jumlah aset dasar yang benar-benar diterima/dijual (setelah fee aset dasar)
    fee: float = 0.0
    fee_asset: str = ""
    created_at: float = field(default_factory=time.time)
    filled_at: float | None = None
    message: str = ""
    source: str = "manual"       # manual / auto
    exchange_order_id: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("side", "order_type", "status"):
            d[k] = getattr(self, k).value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "CryptoOrder":
        d = dict(d)
        d["side"], d["order_type"], d["status"] = Side(d["side"]), OrderType(d["order_type"]), OrderStatus(d["status"])
        return cls(**d)


def bot_positions(orders: list[CryptoOrder]) -> dict[str, dict]:
    """Posisi yang dibuka bot sendiri: {simbol: {"quantity", "avg_price", "opened_at"}} dari order auto terisi."""
    out: dict[str, dict] = {}
    for o in sorted(orders, key=lambda o: o.filled_at or o.created_at):
        if o.source != "auto" or o.status != OrderStatus.FILLED or not o.filled_qty:
            continue
        pos = out.setdefault(o.symbol, {"quantity": 0.0, "cost": 0.0, "opened_at": o.filled_at})
        if o.side == Side.BUY:
            pos["cost"] += o.filled_qty * o.fill_price
            pos["quantity"] += o.filled_qty
        else:
            avg = pos["cost"] / pos["quantity"] if pos["quantity"] > DUST else 0.0
            pos["quantity"] = max(0.0, pos["quantity"] - o.filled_qty)
            pos["cost"] = avg * pos["quantity"]
        if pos["quantity"] <= DUST:
            del out[o.symbol]
    return {s: {"quantity": p["quantity"], "avg_price": p["cost"] / p["quantity"], "opened_at": p["opened_at"]}
            for s, p in out.items()}


class CryptoBroker:
    name = ""
    display_name = ""
    is_live = False

    def __init__(self, db, clock=time.time):
        self.db = db
        self.clock = clock
        self._lock = threading.Lock()
        self._orders: list[CryptoOrder] = [CryptoOrder.from_dict(o) for o in db.load_crypto_orders(self.name)] \
            if db is not None else []

    def available(self) -> bool:
        return True

    def status(self) -> dict:
        return {"name": self.name, "display_name": self.display_name, "is_live": self.is_live,
                "available": self.available(), "note": ""}

    def _save_orders(self, orders) -> None:
        if self.db is not None:
            self.db.save_crypto_orders(self.name, [o.to_dict() for o in orders])

    def orders(self) -> list[CryptoOrder]:
        return list(reversed(self._orders))

    def open_symbols(self) -> set[str]:
        return {o.symbol for o in self._orders if o.status == OrderStatus.OPEN}

    def bot_positions(self) -> dict[str, dict]:
        return bot_positions(self._orders)


class CryptoPaperBroker(CryptoBroker):
    """Simulasi spot crypto dengan saldo USDT; mengikuti filter Binance (stepSize, tickSize, minNotional)."""

    name = "paper"
    display_name = "Simulasi Crypto (USDT)"

    def __init__(self, db, starting_cash: float, fee_pct: float, clock=time.time):
        super().__init__(db, clock)
        self.configured_cash = starting_cash
        self.fee = fee_pct / 100
        saved = db.get_setting("crypto_paper_account") if db is not None else None
        if saved:
            self.cash, self.starting_cash, self.positions = saved["cash"], saved["starting_cash"], saved["positions"]
        else:
            self.reset()

    def _save(self, changed=()) -> None:
        if self.db is not None:
            self.db.set_setting("crypto_paper_account", {"cash": self.cash, "starting_cash": self.starting_cash,
                                                         "positions": self.positions})
            self._save_orders(changed)

    def reset(self) -> None:
        self.cash = self.starting_cash = self.configured_cash
        self.positions: dict[str, dict] = {}  # simbol -> {"quantity", "avg_price"}
        self._orders = []
        if self.db is not None:
            self.db.delete_crypto_orders(self.name)
        self._save()

    def _reserved_cash(self) -> float:
        return sum(o.quantity * o.limit_price * (1 + self.fee) for o in self._orders
                   if o.status == OrderStatus.OPEN and o.side == Side.BUY)

    def _reserved_qty(self, symbol: str) -> float:
        return sum(o.quantity for o in self._orders
                   if o.status == OrderStatus.OPEN and o.side == Side.SELL and o.symbol == symbol)

    def free_quantity(self, symbol: str) -> float:
        return self.positions.get(symbol, {}).get("quantity", 0.0) - self._reserved_qty(symbol)

    def _fill(self, o: CryptoOrder, price: float) -> None:
        value = o.quantity * price
        fee = value * self.fee
        if o.side == Side.BUY:
            pos = self.positions.setdefault(o.symbol, {"quantity": 0.0, "avg_price": 0.0})
            cost = pos["quantity"] * pos["avg_price"] + value
            pos["quantity"] += o.quantity
            pos["avg_price"] = cost / pos["quantity"]
            self.cash -= value + fee
        else:
            pos = self.positions[o.symbol]
            pos["quantity"] -= o.quantity
            if pos["quantity"] <= DUST:
                del self.positions[o.symbol]
            self.cash += value - fee
        o.fill_price, o.filled_qty, o.fee, o.fee_asset = price, o.quantity, round(fee, 8), PAPER_QUOTE
        o.status, o.filled_at = OrderStatus.FILLED, self.clock()

    def _reject(self, o: CryptoOrder, msg: str):
        o.status, o.message = OrderStatus.REJECTED, msg
        self._orders.append(o)
        self._save([o])
        raise BrokerError(msg)

    def place_order(self, o: CryptoOrder, market_price: float, rules: SymbolRules) -> CryptoOrder:
        with self._lock:
            o.symbol, o.broker = normalize_pair(o.symbol), self.name
            if rules.quote != PAPER_QUOTE:
                return self._reject(o, f"Akun simulasi memakai saldo {PAPER_QUOTE}; pilih pasangan …{PAPER_QUOTE}")
            qty = rules.floor_qty(o.quantity)
            if o.order_type == OrderType.LIMIT:
                if not o.limit_price or o.limit_price <= 0:
                    return self._reject(o, "Isi harga limit")
                price = float(rules.round_price(o.limit_price))
                o.limit_price = price
            else:
                price = market_price
            reason = rules.check(qty, price)
            if reason:
                return self._reject(o, reason)
            o.quantity = float(qty)
            if o.side == Side.BUY:
                need = o.quantity * price * (1 + self.fee)
                if need > self.cash - self._reserved_cash() + 1e-9:
                    return self._reject(o, f"Saldo {PAPER_QUOTE} tidak cukup (butuh {usdt(need)})")
            elif o.quantity > self.free_quantity(o.symbol) + DUST:
                return self._reject(o, f"Saldo {rules.base} tidak cukup untuk dijual")
            marketable = o.order_type == OrderType.MARKET or (
                o.side == Side.BUY and market_price <= price) or (o.side == Side.SELL and market_price >= price)
            if marketable:
                self._fill(o, price)
            self._orders.append(o)
            self._save([o])
            return o

    def match_open_orders(self, prices: dict[str, float]) -> list[CryptoOrder]:
        filled = []
        with self._lock:
            for o in self._orders:
                p = prices.get(o.symbol)
                if o.status != OrderStatus.OPEN or p is None:
                    continue
                if (o.side == Side.BUY and p <= o.limit_price) or (o.side == Side.SELL and p >= o.limit_price):
                    self._fill(o, o.limit_price)
                    filled.append(o)
            if filled:
                self._save(filled)
        return filled

    def cancel_order(self, order_id: str) -> CryptoOrder:
        with self._lock:
            for o in self._orders:
                if o.id == order_id:
                    if o.status != OrderStatus.OPEN:
                        raise BrokerError("Hanya order OPEN yang bisa dibatalkan")
                    o.status = OrderStatus.CANCELLED
                    self._save([o])
                    return o
        raise BrokerError("Order tidak ditemukan")

    def account(self, prices: dict[str, float]) -> dict:
        positions, market_value = [], 0.0
        for sym, pos in sorted(self.positions.items()):
            last = prices.get(sym, pos["avg_price"])
            value, cost = pos["quantity"] * last, pos["quantity"] * pos["avg_price"]
            market_value += value
            positions.append({"symbol": sym, "asset": (split_pair(sym) or (sym, ""))[0], "quantity": pos["quantity"],
                              "avg_price": pos["avg_price"], "last_price": last, "market_value": round(value, 8),
                              "unrealized_pl": round(value - cost, 8),
                              "unrealized_pl_pct": round((value - cost) / cost * 100, 2) if cost else 0.0})
        equity = self.cash + market_value
        return {"broker": self.name, "quote_asset": PAPER_QUOTE, "cash": round(self.cash, 8),
                "buying_power": round(self.cash - self._reserved_cash(), 8), "market_value": round(market_value, 8),
                "equity": round(equity, 8), "total_pl": round(equity - self.starting_cash, 8),
                "total_pl_pct": round((equity - self.starting_cash) / self.starting_cash * 100, 2),
                "positions": positions}


STATUS_MAP = {"NEW": OrderStatus.OPEN, "PARTIALLY_FILLED": OrderStatus.OPEN, "PENDING_NEW": OrderStatus.OPEN,
              "FILLED": OrderStatus.FILLED, "CANCELED": OrderStatus.CANCELLED, "PENDING_CANCEL": OrderStatus.OPEN,
              "EXPIRED": OrderStatus.CANCELLED, "EXPIRED_IN_MATCH": OrderStatus.CANCELLED,
              "REJECTED": OrderStatus.REJECTED}


class BinanceBroker(CryptoBroker):
    """Order sungguhan lewat API Binance Spot (Testnet atau akun asli)."""

    def __init__(self, api: BinanceAPI, db, live: bool, clock=time.time):
        self.name = "binance" if live else "testnet"
        self.display_name = "Binance (akun asli)" if live else "Binance Spot Testnet"
        self.is_live = live
        super().__init__(db, clock)
        self.api = api

    def available(self) -> bool:
        return self.api.configured

    def status(self) -> dict:
        s = super().status()
        if not self.available():
            s["note"] = ("Isi BINANCE_API_KEY & BINANCE_API_SECRET di .env (akun asli; juga butuh ENABLE_LIVE_TRADING=true)"
                         if self.is_live else
                         "Isi BINANCE_TESTNET_API_KEY & BINANCE_TESTNET_API_SECRET di .env (buat di testnet.binance.vision)")
        return s

    def _call(self, method: str, path: str, params: dict):
        if not self.available():
            raise BrokerNotAvailable(self.status()["note"])
        try:
            return self.api.signed(method, path, params)
        except BinanceError as exc:
            raise BrokerError(str(exc)) from exc

    def balances(self) -> dict[str, dict]:
        data = self._call("GET", "/api/v3/account", {"omitZeroBalances": "true"})
        return {b["asset"]: {"free": float(b["free"]), "locked": float(b["locked"])} for b in data.get("balances", [])
                if float(b["free"]) + float(b["locked"]) > 0}

    def free_quantity(self, symbol: str) -> float:
        base = (split_pair(symbol) or (symbol, ""))[0]
        return self.balances().get(base, {}).get("free", 0.0)

    def _apply(self, o: CryptoOrder, r: dict, rules: SymbolRules | None = None) -> None:
        o.exchange_order_id = str(r.get("orderId", o.exchange_order_id or ""))
        o.status = STATUS_MAP.get(r.get("status", ""), o.status)
        executed = float(r.get("executedQty") or 0)
        quote_qty = float(r.get("cummulativeQuoteQty") or 0)
        if executed > 0:
            o.fill_price = quote_qty / executed
            o.filled_qty = executed
            fills = r.get("fills") or []
            if fills:
                assets = {f["commissionAsset"] for f in fills}
                o.fee = sum(float(f["commission"]) for f in fills)
                o.fee_asset = assets.pop() if len(assets) == 1 else "beragam"
                base = rules.base if rules else (split_pair(o.symbol) or ("", ""))[0]
                if o.side == Side.BUY and o.fee_asset == base:
                    o.filled_qty = executed - o.fee  # fee dipotong dari aset yang dibeli
            if o.status == OrderStatus.FILLED and not o.filled_at:
                o.filled_at = (r.get("transactTime") or r.get("updateTime") or self.clock() * 1000) / 1000

    def place_order(self, o: CryptoOrder, market_price: float, rules: SymbolRules) -> CryptoOrder:
        o.symbol, o.broker = normalize_pair(o.symbol), self.name
        qty = rules.floor_qty(o.quantity)
        price = float(rules.round_price(o.limit_price)) if o.order_type == OrderType.LIMIT and o.limit_price else market_price
        reason = rules.check(qty, price) if price else "Isi harga limit"
        if not reason and not self.available():
            reason = self.status()["note"]
        params = {"symbol": o.symbol, "side": o.side.value, "type": o.order_type.value, "quantity": fmt_decimal(qty),
                  "newClientOrderId": o.id, "newOrderRespType": "FULL"}
        if o.order_type == OrderType.LIMIT:
            o.limit_price = price
            params.update(timeInForce="GTC", price=fmt_decimal(rules.round_price(price)))
        o.quantity = float(qty)
        if not reason:
            try:
                self._apply(o, self._call("POST", "/api/v3/order", params), rules)
            except BrokerError as exc:
                reason = str(exc)
        with self._lock:
            if reason:
                o.status, o.message = OrderStatus.REJECTED, reason
            self._orders.append(o)
            self._save_orders([o])
        if reason:
            raise BrokerError(reason)
        return o

    def refresh_open(self) -> list[CryptoOrder]:
        """Perbarui status order OPEN dari Binance; kembalikan yang baru terisi."""
        filled = []
        for o in [o for o in self._orders if o.status == OrderStatus.OPEN]:
            try:
                r = self._call("GET", "/api/v3/order", {"symbol": o.symbol, "origClientOrderId": o.id})
            except BrokerError:
                continue
            self._apply(o, r)
            self._save_orders([o])
            if o.status == OrderStatus.FILLED:
                filled.append(o)
        return filled

    def match_open_orders(self, prices: dict[str, float]) -> list[CryptoOrder]:
        return self.refresh_open()

    def cancel_order(self, order_id: str) -> CryptoOrder:
        o = next((o for o in self._orders if o.id == order_id), None)
        if o is None:
            raise BrokerError("Order tidak ditemukan")
        if o.status != OrderStatus.OPEN:
            raise BrokerError("Hanya order OPEN yang bisa dibatalkan")
        self._apply(o, self._call("DELETE", "/api/v3/order", {"symbol": o.symbol, "origClientOrderId": o.id}))
        self._save_orders([o])
        return o

    def account(self, prices: dict[str, float]) -> dict:
        """Saldo Binance dinilai dalam USDT. Harga rata-rata hanya diketahui untuk posisi yang dibuka bot."""
        balances = self.balances()
        bot = self.bot_positions()
        positions, market_value = [], 0.0
        for asset, b in sorted(balances.items()):
            if asset == PAPER_QUOTE:
                continue
            sym = asset + PAPER_QUOTE
            qty = b["free"] + b["locked"]
            last = prices.get(sym)
            value = qty * last if last is not None else None
            market_value += value or 0.0
            mine = bot.get(sym)
            row = {"symbol": sym, "asset": asset, "quantity": qty, "free": b["free"], "locked": b["locked"],
                   "last_price": last, "market_value": value, "avg_price": None, "unrealized_pl": None,
                   "unrealized_pl_pct": None, "bot_quantity": mine["quantity"] if mine else 0.0}
            if mine and last is not None:
                row["avg_price"] = mine["avg_price"]
                row["unrealized_pl"] = mine["quantity"] * (last - mine["avg_price"])
                row["unrealized_pl_pct"] = round((last / mine["avg_price"] - 1) * 100, 2)
            positions.append(row)
        cash = balances.get(PAPER_QUOTE, {"free": 0.0, "locked": 0.0})
        return {"broker": self.name, "quote_asset": PAPER_QUOTE, "cash": cash["free"] + cash["locked"],
                "buying_power": cash["free"], "market_value": market_value,
                "equity": cash["free"] + cash["locked"] + market_value, "total_pl": None, "total_pl_pct": None,
                "positions": positions}


# ---- auto-trading crypto ------------------------------------------------------
TARGETS = ("paper", "testnet", "binance")


@dataclass
class CryptoAutoTraderConfig:
    enabled: bool = False
    broker: str = "paper"            # paper / testnet / binance (akun asli)
    symbols: list[str] = field(default_factory=lambda: ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"])
    candle_interval: str = "1h"      # candle untuk sinyal
    interval_seconds: int = 300
    position_pct: float = 10.0       # % ekuitas per posisi baru
    max_order_usdt: float = 100.0    # batas nilai satu order beli (0 = tanpa batas, khusus simulasi/testnet)
    max_positions: int = 3
    min_buy_score: int = 2
    max_sell_score: int = -2
    stop_loss_pct: float = 5.0
    take_profit_pct: float = 10.0
    cooldown_minutes: int = 60

    def validate(self, max_position_pct: float, live_allowed: bool = False) -> None:
        self.symbols = sorted({normalize_pair(s) for s in self.symbols if str(s).strip()})
        errors = []
        if not self.symbols:
            errors.append("Daftar pasangan tidak boleh kosong")
        bad = [s for s in self.symbols if (split_pair(s) or ("", ""))[1] != PAPER_QUOTE]
        if bad:
            errors.append(f"Bot crypto hanya untuk pasangan …{PAPER_QUOTE}: {', '.join(bad)}")
        if self.broker not in TARGETS:
            errors.append(f"Akun harus salah satu dari {', '.join(TARGETS)}")
        if self.broker == "binance":
            if not live_allowed:
                errors.append("Bot di akun Binance asli butuh ENABLE_LIVE_TRADING=true")
            if self.max_order_usdt <= 0:
                errors.append("Untuk akun asli, isi batas nilai per order (USDT) > 0")
        if self.candle_interval not in KLINE_INTERVALS:
            errors.append(f"Candle harus salah satu dari {', '.join(KLINE_INTERVALS)}")
        if self.interval_seconds < 30:
            errors.append("Interval minimal 30 detik")
        if not 0 < self.position_pct <= max_position_pct:
            errors.append(f"Ukuran posisi harus 0 < x <= {max_position_pct:g}%")
        if self.max_order_usdt < 0 or self.max_positions < 1:
            errors.append("Batas order tidak boleh negatif dan maks. posisi minimal 1")
        if self.min_buy_score < 1 or self.max_sell_score > -1:
            errors.append("Skor beli minimal 1 dan skor jual maksimal -1")
        if self.stop_loss_pct < 0 or self.take_profit_pct < 0 or self.cooldown_minutes < 0:
            errors.append("Stop-loss, take-profit, dan cooldown tidak boleh negatif")
        if errors:
            raise ValueError("; ".join(errors))


class CryptoAutoTrader(AutoTrader):
    """Bot sinyal teknikal untuk crypto. Memakai loop, log & cooldown yang sama dengan bot saham."""

    SETTING_KEY = "crypto_autotrader"
    LOG_STREAM = "crypto_autotrader"
    CONFIG_CLS = CryptoAutoTraderConfig

    def __init__(self, brokers: dict, provider, db, max_position_pct: float, live_allowed: bool, clock=time.time):
        self.brokers = brokers
        self.live_allowed = live_allowed
        super().__init__(brokers["paper"], provider, db, max_position_pct, clock)
        if self.config.broker == "binance" and not live_allowed:  # izin live dicabut sejak terakhir disimpan
            self.config.broker, self.config.enabled = "paper", False

    def _validate(self, cfg) -> None:
        cfg.validate(self.max_position_pct, self.live_allowed)

    def update_config(self, data: dict):
        if self.running and data.get("broker", self.config.broker) != self.config.broker:
            raise ValueError("Hentikan bot sebelum mengganti akun")
        return super().update_config(data)

    @property
    def target(self) -> CryptoBroker:
        return self.brokers[self.config.broker]

    def _title(self) -> str:
        return f"Auto-trading crypto ({self.target.display_name})"

    def _submit_crypto(self, sym: str, side: Side, qty: float, price: float, reason: str, now: float,
                       rules: SymbolRules) -> None:
        order = CryptoOrder(sym, side, qty, OrderType.MARKET, source="auto", created_at=now)
        try:
            self.target.place_order(order, price, rules)
        except BrokerError as exc:
            self._log("WARN", sym, f"Order {side.value} ditolak: {exc}")
            return
        self.last_trade_at[sym] = now
        status = "" if order.status == OrderStatus.FILLED else f" ({order.status.value})"
        self._log("TRADE", sym, f"{side.value} {qty_str(order.filled_qty or order.quantity)} {rules.base} @ "
                  f"{price_str(order.fill_price or price)} {rules.quote}{status} — {reason}",
                  side=side.value, quantity=order.quantity, price=order.fill_price, order_id=order.id,
                  broker=self.target.name, title=self._title())

    def run_cycle(self) -> list[dict]:
        with self._cycle_lock:
            now = self.clock()
            start_id = self._seq

            def created() -> list[dict]:
                return [e for e in self.log if e["id"] > start_id]

            self.last_run = now
            cfg, broker = self.config, self.target
            if not broker.available():
                self._log("ERROR", "-", f"{broker.display_name} belum bisa dipakai: {broker.status()['note']}")
                return created()
            held = broker.bot_positions()
            prices, signals, rules = {}, {}, {}
            for sym in sorted(set(cfg.symbols) | set(held)):
                try:
                    prices[sym] = self.provider.quote(sym).price
                    rules[sym] = self.provider.rules(sym)
                    signals[sym] = analyze([c.close for c in self.provider.candles(sym, None, cfg.candle_interval, 300)])
                except MarketDataError as exc:
                    self._log("WARN", sym, f"Data tidak tersedia: {exc}")
            try:
                for o in broker.match_open_orders(prices):
                    self._log("TRADE", o.symbol, f"Order limit {o.side.value} {qty_str(o.filled_qty)} @ "
                              f"{price_str(o.fill_price)} tereksekusi", side=o.side.value, title=self._title())
                held = broker.bot_positions()

                # 1) Kelola posisi milik bot.
                for sym, pos in sorted(held.items()):
                    if sym not in prices or sym in broker.open_symbols():
                        continue
                    price, avg = prices[sym], pos["avg_price"]
                    qty = float(rules[sym].floor_qty(min(pos["quantity"], broker.free_quantity(sym))))
                    if qty <= 0:
                        continue
                    change = (price - avg) / avg * 100
                    sig = signals.get(sym)
                    reason = None
                    if cfg.stop_loss_pct and change <= -cfg.stop_loss_pct:
                        reason = f"stop-loss ({change:.1f}%)"
                    elif cfg.take_profit_pct and change >= cfg.take_profit_pct:
                        reason = f"take-profit (+{change:.1f}%)"
                    elif sig and sig["score"] <= cfg.max_sell_score and not self._in_cooldown(sym, now):
                        reason = f"sinyal JUAL skor {sig['score']}"
                    if reason:
                        self._submit_crypto(sym, Side.SELL, qty, price, reason, now, rules[sym])

                # 2) Buka posisi baru. Ekuitas dinilai dengan harga semua pasangan (saldo Binance bisa berisi aset lain).
                held = broker.bot_positions()
                try:
                    valuation = {**self.provider.all_prices(), **prices}
                except MarketDataError:
                    valuation = prices
                for sym in cfg.symbols:
                    sig = signals.get(sym)
                    if not sig or sym in held or sym in broker.open_symbols() or sig["score"] < cfg.min_buy_score:
                        continue
                    if self._in_cooldown(sym, now):
                        self._log("SKIP", sym, f"Sinyal BELI skor {sig['score']} diabaikan (cooldown)")
                        continue
                    if len(held) >= cfg.max_positions:
                        self._log("SKIP", sym, f"Sinyal BELI diabaikan: maks. {cfg.max_positions} posisi bot tercapai")
                        continue
                    acct = broker.account(valuation)
                    budget = min(acct["equity"] * cfg.position_pct / 100, acct["buying_power"])
                    if cfg.max_order_usdt > 0:
                        budget = min(budget, cfg.max_order_usdt)
                    fee = getattr(broker, "fee", 0.001)
                    qty = float(rules[sym].floor_qty(budget / (prices[sym] * (1 + fee))))
                    problem = rules[sym].check(Decimal(str(qty)), prices[sym])
                    if problem:
                        self._log("SKIP", sym, f"Sinyal BELI diabaikan: anggaran {usdt(budget)} USDT terlalu kecil ({problem})")
                        continue
                    self._submit_crypto(sym, Side.BUY, qty, prices[sym], f"sinyal BELI skor {sig['score']}", now, rules[sym])
                    held = broker.bot_positions()
            except (BrokerError, MarketDataError) as exc:
                self._log("ERROR", "-", f"{broker.display_name}: {exc}")

            if not created():
                self._log("INFO", "-", f"Tidak ada aksi ({len(signals)} pasangan dipindai, akun {broker.display_name})")
            return created()

    def status(self) -> dict:
        st = super().status()
        st["targets"] = {name: b.status() for name, b in self.brokers.items()}
        st["bot_positions"] = self.target.bot_positions()
        return st
