from decimal import Decimal

from pm.fees import taker_fee, taker_fee_per_share
from pm.models import Book, Fill, Level, Position, Side, round_to_tick
from tests.conftest import D, mk_book, mk_market


def test_book_sorting_and_top():
    b = Book("T")
    b.replace(
        bids=[Level(D("0.40"), D(10)), Level(D("0.42"), D(5)), Level(D("0.41"), D(0))],
        asks=[Level(D("0.45"), D(7)), Level(D("0.44"), D(3))],
    )
    assert b.best_bid == D("0.42") and b.best_ask == D("0.44")
    assert b.mid == D("0.43") and b.spread == D("0.02")
    assert [l.price for l in b.bids] == [D("0.42"), D("0.40")]  # zero-size level dropped


def test_set_level_updates_and_removes():
    b = mk_book("T", "0.40", "0.45")
    b.set_level(Side.BUY, D("0.41"), D(20))
    assert b.best_bid == D("0.41")
    b.set_level(Side.BUY, D("0.41"), D(0))
    assert b.best_bid == D("0.40")
    b.set_level(Side.SELL, D("0.43"), D(1))
    assert b.best_ask == D("0.43")


def test_walk_and_vwap():
    b = mk_book("T", "0.40", "0.45", ask_sz="10", extra_asks=[("0.46", "10"), ("0.50", "100")])
    filled, notional = b.walk(Side.BUY, D(15))
    assert filled == D(15) and notional == D("0.45") * 10 + D("0.46") * 5
    assert b.vwap(Side.BUY, D(15)) == notional / 15
    assert b.vwap(Side.BUY, D(1000)) is None  # not enough depth


def test_round_to_tick_is_conservative():
    assert round_to_tick(D("0.4349"), D("0.01"), Side.BUY) == D("0.43")
    assert round_to_tick(D("0.4301"), D("0.01"), Side.SELL) == D("0.44")
    assert round_to_tick(D("0.435"), D("0.005"), Side.BUY) == D("0.435")


def test_taker_fee_formula():
    # 100 shares at 0.50 with rate 0.04 -> 100 * 0.04 * 0.25 = $1.00
    assert taker_fee_per_share(D("0.5"), D("0.04")) == D("0.01")
    m = mk_market(fee_rate="0.04")
    assert taker_fee(D("0.5"), D(100), m) == D("1.00")
    # geopolitics: zero
    assert taker_fee(D("0.5"), D(100), mk_market(fee_rate="0")) == 0
    # unknown fee -> worst case 0.07
    m.fee_known = False
    assert taker_fee(D("0.5"), D(100), m) == D("1.75")


def test_position_accounting():
    p = Position("T")
    p.apply(Fill("o1", "t", "s", "T", Side.BUY, D("0.40"), D(10), fee=D("0.1")))
    assert p.shares == 10 and p.cost == D("4.1")
    realised = p.apply(Fill("o2", "t", "s", "T", Side.SELL, D("0.50"), D(4)))
    assert realised == (D("0.50") - D("0.41")) * 4
    assert p.shares == 6
