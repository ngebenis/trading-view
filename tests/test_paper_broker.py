import pytest

from app.db import Database
from app.brokers import BrokerError, Order, OrderStatus, OrderType, PaperBroker, Side


@pytest.fixture
def broker(tmp_path):
    return PaperBroker(Database(tmp_path / "app.db"), 10_000_000, buy_fee_pct=0.15, sell_fee_pct=0.25)


def test_market_buy_then_sell(broker):
    o = broker.place_order(Order("bbca", Side.BUY, 2, OrderType.MARKET), 9012)
    assert o.status == OrderStatus.FILLED and o.fill_price == 9000 and o.symbol == "BBCA"
    assert broker.cash == pytest.approx(10_000_000 - 1_800_000 * 1.0015)
    acct = broker.account({"BBCA": 9500})
    assert acct["positions"][0]["lots"] == 2
    assert acct["positions"][0]["unrealized_pl"] == 100_000

    broker.place_order(Order("BBCA", Side.SELL, 2, OrderType.MARKET), 9500)
    assert broker.positions == {}
    assert broker.cash == pytest.approx(10_000_000 - 1_800_000 * 1.0015 + 1_900_000 * 0.9975)


def test_rejects_insufficient_cash_and_shares(broker):
    with pytest.raises(BrokerError, match="Saldo"):
        broker.place_order(Order("BBCA", Side.BUY, 100, OrderType.MARKET), 9000)
    with pytest.raises(BrokerError, match="tidak cukup"):
        broker.place_order(Order("BBCA", Side.SELL, 1, OrderType.MARKET), 9000)


def test_limit_price_must_follow_tick(broker):
    with pytest.raises(BrokerError, match="fraksi"):
        broker.place_order(Order("BBCA", Side.BUY, 1, OrderType.LIMIT, 9010), 9000)


def test_limit_order_rests_then_fills(broker):
    o = broker.place_order(Order("TLKM", Side.BUY, 5, OrderType.LIMIT, 2800), 2900)
    assert o.status == OrderStatus.OPEN
    assert broker.account({})["buying_power"] < broker.cash
    assert broker.match_open_orders({"TLKM": 2850}) == []
    filled = broker.match_open_orders({"TLKM": 2790})
    assert filled[0].status == OrderStatus.FILLED and filled[0].fill_price == 2800
    assert broker.positions["TLKM"]["shares"] == 500


def test_cancel_and_persistence(broker, tmp_path):
    o = broker.place_order(Order("TLKM", Side.BUY, 1, OrderType.LIMIT, 2800), 2900)
    broker.cancel_order(o.id)
    reloaded = PaperBroker(Database(tmp_path / "app.db"), 10_000_000, 0.15, 0.25)
    assert reloaded.orders()[0].status == OrderStatus.CANCELLED
