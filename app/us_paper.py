"""Akun simulasi saham AS (USD) yang berjalan lokal — tidak butuh broker.

Harga dari penyedia data (Finnhub / Alpaca). Jumlah boleh pecahan, tanpa komisi. Order market langsung terisi
pada harga terakhir (di luar jam bursa = harga penutupan terakhir); order limit menunggu harga menyentuh limit
dan dicocokkan setiap akun/riwayat dibuka. Saldo, posisi & riwayat tersimpan di SQLite.
"""
import threading
import time
from datetime import datetime

from .alpaca import NY, UsOrder
from .brokers.base import BrokerError, OrderStatus, OrderType, Side
from .market_data import MarketDataError

MAX_ORDERS = 500
MIN_NOTIONAL = 1.0
DUST = 1e-9


class UsPaperBroker:
    name = "sim"
    display_name = "Simulasi Saham AS (USD)"
    is_live = False

    def __init__(self, db, provider, starting_cash: float, clock=time.time):
        self.db, self.provider, self.clock = db, provider, clock
        self.configured_cash = starting_cash
        self.on_fill: list = []
        self._lock = threading.RLock()
        saved = db.get_setting("us_paper_account") if db is not None else None
        if saved:
            self.cash, self.starting_cash, self.positions = saved["cash"], saved["starting_cash"], saved["positions"]
            self._orders = [UsOrder.from_dict(o) for o in saved["orders"]]
            self.day_start = saved.get("day_start")
        else:
            self.reset()

    def available(self) -> bool:
        return True

    def status(self) -> dict:
        return {"name": self.name, "display_name": self.display_name, "is_live": False, "available": True,
                "note": "Simulasi lokal: saldo & order tidak dikirim ke broker mana pun."}

    def _save(self) -> None:
        if self.db is not None:
            self.db.set_setting("us_paper_account", {
                "cash": self.cash, "starting_cash": self.starting_cash, "positions": self.positions,
                "orders": [o.to_dict() for o in self._orders[-MAX_ORDERS:]], "day_start": self.day_start})

    def reset(self) -> None:
        with self._lock:
            self.cash = self.starting_cash = self.configured_cash
            self.positions: dict[str, dict] = {}  # simbol -> {"quantity", "avg_price"}
            self._orders: list[UsOrder] = []
            self.day_start = None
            self._save()

    # ---- helper -----------------------------------------------------------
    def _reserved_cash(self) -> float:
        return sum(o.quantity * o.limit_price for o in self._orders
                   if o.status == OrderStatus.OPEN and o.side == Side.BUY)

    def _free_qty(self, symbol: str) -> float:
        held = self.positions.get(symbol, {}).get("quantity", 0.0)
        return held - sum(o.quantity for o in self._orders
                          if o.status == OrderStatus.OPEN and o.side == Side.SELL and o.symbol == symbol)

    def _fill(self, o: UsOrder, price: float) -> None:
        value = o.quantity * price
        if o.side == Side.BUY:
            pos = self.positions.setdefault(o.symbol, {"quantity": 0.0, "avg_price": 0.0})
            cost = pos["quantity"] * pos["avg_price"] + value
            pos["quantity"] += o.quantity
            pos["avg_price"] = cost / pos["quantity"]
            self.cash -= value
        else:
            pos = self.positions[o.symbol]
            pos["quantity"] -= o.quantity
            if pos["quantity"] <= DUST:
                del self.positions[o.symbol]
            self.cash += value
        o.fill_price, o.filled_qty, o.status, o.filled_at = price, o.quantity, OrderStatus.FILLED, self.clock()

    def _reject(self, o: UsOrder, msg: str):
        o.status, o.message = OrderStatus.REJECTED, msg
        self._orders.append(o)
        self._save()
        raise BrokerError(msg)

    # ---- order --------------------------------------------------------------
    def place_order(self, o: UsOrder) -> UsOrder:
        with self._lock:
            o.broker = self.name
            try:
                market = self.provider.quote(o.symbol).price
            except MarketDataError as exc:
                return self._reject(o, f"Harga {o.symbol} tidak tersedia: {exc}")
            limit = o.order_type == OrderType.LIMIT
            price = round(o.limit_price, 2) if limit else market
            if limit:
                o.limit_price = price
            qty = o.quantity or (round(o.notional / price, 6) if o.notional else 0)
            if qty <= 0 or qty * price < MIN_NOTIONAL:
                return self._reject(o, f"Nilai order minimal ${MIN_NOTIONAL:g}")
            o.quantity, o.notional = qty, None
            if o.side == Side.BUY:
                free = self.cash - self._reserved_cash()
                if qty * price > free + 1e-6:
                    return self._reject(o, f"Saldo tidak cukup (butuh ${qty * price:,.2f}, tersedia ${free:,.2f})")
            elif qty > self._free_qty(o.symbol) + DUST:
                return self._reject(o, f"Saham {o.symbol} tidak cukup untuk dijual")
            marketable = not limit or (o.side == Side.BUY and market <= price) or (o.side == Side.SELL and market >= price)
            if marketable:
                self._fill(o, market)
            self._orders.append(o)
            self._save()
            return o

    def match_open(self) -> list[UsOrder]:
        filled = []
        with self._lock:
            quotes: dict[str, float | None] = {}
            for o in self._orders:
                if o.status != OrderStatus.OPEN:
                    continue
                if o.symbol not in quotes:
                    try:
                        quotes[o.symbol] = self.provider.quote(o.symbol).price
                    except MarketDataError:
                        quotes[o.symbol] = None
                p = quotes[o.symbol]
                if p is not None and ((o.side == Side.BUY and p <= o.limit_price) or
                                      (o.side == Side.SELL and p >= o.limit_price)):
                    self._fill(o, o.limit_price)
                    filled.append(o)
            if filled:
                self._save()
        for o in filled:
            for listener in self.on_fill:
                try:
                    listener(o)
                except Exception:
                    pass
        return filled

    def orders(self, limit: int = 100) -> list[UsOrder]:
        self.match_open()
        return list(reversed(self._orders))[:limit]

    def cancel_order(self, order_id: str) -> UsOrder:
        with self._lock:
            for o in self._orders:
                if order_id in (o.id, o.exchange_order_id):
                    if o.status != OrderStatus.OPEN:
                        raise BrokerError("Hanya order OPEN yang bisa dibatalkan")
                    o.status = OrderStatus.CANCELLED
                    self._save()
                    return o
        raise BrokerError("Order tidak ditemukan")

    def account(self) -> dict:
        self.match_open()
        with self._lock:
            positions, value_total = [], 0.0
            for sym, pos in sorted(self.positions.items()):
                try:
                    last = self.provider.quote(sym).price
                except MarketDataError:
                    last = pos["avg_price"]
                value, cost = pos["quantity"] * last, pos["quantity"] * pos["avg_price"]
                value_total += value
                positions.append({"symbol": sym, "quantity": pos["quantity"], "avg_price": pos["avg_price"],
                                  "last_price": last, "market_value": round(value, 2),
                                  "unrealized_pl": round(value - cost, 2),
                                  "unrealized_pl_pct": round((value - cost) / cost * 100, 2) if cost else 0.0})
            equity = self.cash + value_total
            today = datetime.fromtimestamp(self.clock(), NY).strftime("%Y-%m-%d")
            if not self.day_start or self.day_start["date"] != today:  # ekuitas awal hari (ET) untuk P/L hari ini
                self.day_start = {"date": today, "equity": equity}
                self._save()
            day_pl = equity - self.day_start["equity"]
            return {"broker": self.name, "currency": "USD", "cash": round(self.cash, 2),
                    "buying_power": round(self.cash - self._reserved_cash(), 2), "equity": round(equity, 2),
                    "market_value": round(value_total, 2), "day_pl": round(day_pl, 2),
                    "day_pl_pct": round(day_pl / self.day_start["equity"] * 100, 2) if self.day_start["equity"] else 0.0,
                    "total_pl": round(equity - self.starting_cash, 2),
                    "total_pl_pct": round((equity - self.starting_cash) / self.starting_cash * 100, 2),
                    "status": "ACTIVE", "trading_blocked": False, "positions": positions}
