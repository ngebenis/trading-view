"""Antarmuka broker. Semua broker (paper, Stockbit, Pluang, dll) mengikuti kontrak ini."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from enum import Enum
import time
import uuid


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, Enum):
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass
class Order:
    symbol: str
    side: Side
    lots: int
    order_type: OrderType
    limit_price: float | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: OrderStatus = OrderStatus.OPEN
    fill_price: float | None = None
    fee: float = 0.0
    created_at: float = field(default_factory=time.time)
    filled_at: float | None = None
    message: str = ""
    source: str = "manual"  # "manual" atau "auto"

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("side", "order_type", "status"):
            d[k] = getattr(self, k).value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Order":
        d = dict(d)
        d["side"], d["order_type"], d["status"] = Side(d["side"]), OrderType(d["order_type"]), OrderStatus(d["status"])
        return cls(**d)


class BrokerError(Exception):
    pass


class BrokerNotAvailable(BrokerError):
    """Broker belum bisa dipakai (tidak ada API resmi / belum dikonfigurasi)."""


class Broker(ABC):
    name: str = ""
    display_name: str = ""
    is_live: bool = False  # True = uang sungguhan

    def status(self) -> dict:
        return {"name": self.name, "display_name": self.display_name, "is_live": self.is_live,
                "available": True, "note": ""}

    @abstractmethod
    def account(self, prices: dict[str, float]) -> dict: ...

    @abstractmethod
    def place_order(self, order: Order, market_price: float) -> Order: ...

    @abstractmethod
    def orders(self) -> list[Order]: ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> Order: ...
