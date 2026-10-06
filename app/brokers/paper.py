"""Paper trading: simulasi beli/jual dengan aturan IDX (lot, fraksi harga, fee)."""
import json
import threading
import time
from pathlib import Path

from ..idx_rules import LOT_SIZE, is_index, is_valid_price, normalize_symbol, round_to_tick
from .base import Broker, BrokerError, Order, OrderStatus, OrderType, Side


class PaperBroker(Broker):
    name = "paper"
    display_name = "Paper Trading (Simulasi)"
    is_live = False

    def __init__(self, path: Path | None, starting_cash: float, buy_fee_pct: float, sell_fee_pct: float,
                 clock=time.time):
        self.path = path
        self.clock = clock
        self.starting_cash = starting_cash
        self.buy_fee = buy_fee_pct / 100
        self.sell_fee = sell_fee_pct / 100
        self._lock = threading.Lock()
        self._load()

    # ---- persistence -------------------------------------------------
    def _load(self) -> None:
        if self.path is not None and self.path.exists():
            raw = json.loads(self.path.read_text())
            self.cash = raw["cash"]
            self.positions = raw["positions"]
            self._orders = [Order.from_dict(o) for o in raw["orders"]]
        else:
            self.reset()

    def _save(self) -> None:
        if self.path is None:  # mode in-memory (dipakai backtest)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "cash": self.cash,
            "positions": self.positions,
            "orders": [o.to_dict() for o in self._orders],
        }, indent=2))

    def reset(self) -> None:
        self.cash = self.starting_cash
        self.positions: dict[str, dict] = {}  # symbol -> {"shares", "avg_price"}
        self._orders: list[Order] = []
        self._save()

    # ---- helpers -----------------------------------------------------
    def _reserved_shares(self, symbol: str) -> int:
        return sum(o.lots * LOT_SIZE for o in self._orders
                   if o.status == OrderStatus.OPEN and o.side == Side.SELL and o.symbol == symbol)

    def _reserved_cash(self) -> float:
        return sum(o.lots * LOT_SIZE * o.limit_price * (1 + self.buy_fee) for o in self._orders
                   if o.status == OrderStatus.OPEN and o.side == Side.BUY)

    def _fill(self, order: Order, price: float) -> None:
        shares = order.lots * LOT_SIZE
        value = shares * price
        if order.side == Side.BUY:
            fee = value * self.buy_fee
            pos = self.positions.setdefault(order.symbol, {"shares": 0, "avg_price": 0.0})
            total_cost = pos["shares"] * pos["avg_price"] + value
            pos["shares"] += shares
            pos["avg_price"] = total_cost / pos["shares"]
            self.cash -= value + fee
        else:
            fee = value * self.sell_fee
            pos = self.positions[order.symbol]
            pos["shares"] -= shares
            if pos["shares"] == 0:
                del self.positions[order.symbol]
            self.cash += value - fee
        order.fill_price, order.fee = price, round(fee, 2)
        order.status = OrderStatus.FILLED
        order.filled_at = self.clock()

    def _reject(self, order: Order, msg: str) -> Order:
        order.status, order.message = OrderStatus.REJECTED, msg
        self._orders.append(order)
        self._save()
        raise BrokerError(msg)

    # ---- Broker API --------------------------------------------------
    def place_order(self, order: Order, market_price: float) -> Order:
        with self._lock:
            order.symbol = normalize_symbol(order.symbol)
            if is_index(order.symbol):
                return self._reject(order, f"{order.symbol} adalah indeks dan tidak bisa dibeli/dijual")
            if order.lots <= 0:
                return self._reject(order, "Jumlah lot harus > 0")
            if order.order_type == OrderType.LIMIT:
                if order.limit_price is None or not is_valid_price(order.limit_price):
                    return self._reject(order, f"Harga limit tidak sesuai fraksi harga IDX "
                                               f"(contoh valid: {round_to_tick(order.limit_price or market_price)})")
                exec_price = order.limit_price
            else:
                exec_price = round_to_tick(market_price)

            shares = order.lots * LOT_SIZE
            if order.side == Side.BUY:
                need = shares * exec_price * (1 + self.buy_fee)
                if need > self.cash - self._reserved_cash():
                    return self._reject(order, f"Saldo tidak cukup (butuh Rp{need:,.0f})")
            else:
                held = self.positions.get(order.symbol, {}).get("shares", 0)
                if shares > held - self._reserved_shares(order.symbol):
                    return self._reject(order, f"Saham {order.symbol} tidak cukup untuk dijual")

            marketable = order.order_type == OrderType.MARKET or (
                order.side == Side.BUY and market_price <= order.limit_price
            ) or (order.side == Side.SELL and market_price >= order.limit_price)
            if marketable:
                self._fill(order, exec_price)
            self._orders.append(order)
            self._save()
            return order

    def match_open_orders(self, prices: dict[str, float]) -> list[Order]:
        """Eksekusi order limit yang harganya sudah tersentuh."""
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
                self._save()
        return filled

    def cancel_order(self, order_id: str) -> Order:
        with self._lock:
            for o in self._orders:
                if o.id == order_id:
                    if o.status != OrderStatus.OPEN:
                        raise BrokerError("Hanya order OPEN yang bisa dibatalkan")
                    o.status = OrderStatus.CANCELLED
                    self._save()
                    return o
        raise BrokerError("Order tidak ditemukan")

    def orders(self) -> list[Order]:
        return list(reversed(self._orders))

    def open_symbols(self) -> set[str]:
        return {o.symbol for o in self._orders if o.status == OrderStatus.OPEN}

    def account(self, prices: dict[str, float]) -> dict:
        positions = []
        market_value = 0.0
        for sym, pos in sorted(self.positions.items()):
            last = prices.get(sym, pos["avg_price"])
            value = pos["shares"] * last
            market_value += value
            cost = pos["shares"] * pos["avg_price"]
            positions.append({
                "symbol": sym, "lots": pos["shares"] // LOT_SIZE, "shares": pos["shares"],
                "avg_price": round(pos["avg_price"], 2), "last_price": last,
                "market_value": round(value, 2), "unrealized_pl": round(value - cost, 2),
                "unrealized_pl_pct": round((value - cost) / cost * 100, 2) if cost else 0.0,
            })
        equity = self.cash + market_value
        return {
            "broker": self.name, "cash": round(self.cash, 2),
            "buying_power": round(self.cash - self._reserved_cash(), 2),
            "market_value": round(market_value, 2), "equity": round(equity, 2),
            "total_pl": round(equity - self.starting_cash, 2),
            "total_pl_pct": round((equity - self.starting_cash) / self.starting_cash * 100, 2),
            "positions": positions,
        }
