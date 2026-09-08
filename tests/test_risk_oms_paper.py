from decimal import Decimal
from pathlib import Path

import pytest

from pm.config import ExecutionConfig, RiskConfig
from pm.execution import ExchangeError
from pm.execution.paper import PaperExchange
from pm.feed import Trade
from pm.models import Intent, Mode, Order, Side, TimeInForce
from pm.oms import OMS
from pm.risk import RiskGate
from pm.state import Portfolio
from pm.store import Store
from tests.conftest import D, mk_book, mk_ctx, mk_market


def intent(token="Y1", side=Side.BUY, price="0.41", size=10, tif=TimeInForce.GTC, post_only=True, strategy="s"):
    return Intent(strategy=strategy, token_id=token, side=side, price=D(price), size=D(size), tif=tif, post_only=post_only)


# ------------------------------------------------------------------ risk

def test_risk_static_checks(market):
    ctx = mk_ctx([market], [mk_book("Y1", "0.40", "0.46")])
    rep = RiskGate(RiskConfig(), Mode.PAPER).evaluate([
        intent(size=1),                 # below min size
        intent(price="0.405"),          # off tick
        intent(token="ZZZ"),            # unknown market
        intent(side=Side.SELL),         # nothing held
    ], ctx, True)
    assert rep.accepted == [] and len(rep.rejected) == 4


def test_risk_caps_per_market_total_and_orders(market):
    m2 = mk_market("0xc2", "Y2", "N2")
    ctx = mk_ctx([market, m2], [mk_book("Y1", "0.40", "0.46"), mk_book("Y2", "0.40", "0.46")])
    cfg = RiskConfig(max_notional_per_market_usd=D(5), max_total_notional_usd=D(8), max_open_orders=5,
                     min_cash_reserve_usd=D(0))
    rep = RiskGate(cfg, Mode.PAPER).evaluate([
        intent(token="Y1", price="0.40", size=10),   # $4 ok
        intent(token="N1", price="0.40", size=10),   # $4 more on same market -> per-market cap
        intent(token="Y2", price="0.40", size=10),   # $4, total 8 ok
        intent(token="N2", price="0.10", size=10),   # $1 -> total cap
    ], ctx, True)
    assert [i.token_id for i in rep.accepted] == ["Y1", "Y2"]
    reasons = " | ".join(r for _, r in rep.rejected)
    assert "per-market cap" in reasons and "total cap" in reasons


def test_risk_replacement_only_charges_delta(market):
    existing = Order("o1", "s:Y1:BUY", "s", "Y1", Side.BUY, D("0.40"), D(10))
    ctx = mk_ctx([market], [mk_book("Y1", "0.40", "0.46")], open_orders=[existing])
    cfg = RiskConfig(max_notional_per_market_usd=D("4.5"), max_total_notional_usd=D("4.5"), min_cash_reserve_usd=D(0))
    rep = RiskGate(cfg, Mode.PAPER).evaluate([intent(price="0.41", size=10)], ctx, True)  # same tag, $4.1 total
    assert len(rep.accepted) == 1


def test_risk_kill_switch_and_geoblock(market, tmp_path):
    ctx = mk_ctx([market], [mk_book("Y1", "0.40", "0.46")])
    ks = tmp_path / "KILL"
    ks.write_text("x")
    rep = RiskGate(RiskConfig(kill_switch_file=str(ks)), Mode.PAPER).evaluate([intent()], ctx, True)
    assert rep.halted and rep.accepted == []
    rep = RiskGate(RiskConfig(kill_switch_file=str(tmp_path / "nope")), Mode.LIVE).evaluate([intent()], ctx, False)
    assert rep.halted and "geoblocked" in rep.halt_reason


def test_risk_daily_loss_limit_allows_risk_reduction(market):
    pf = Portfolio(cash=D(100))
    pf.position("Y1").shares = D(20)
    pf.position("Y1").cost = D(10)
    books = [mk_book("Y1", "0.40", "0.46")]
    pf.day_start_equity = D(200)  # equity now ≈ 100 + 10 + unrealised -> big loss
    ctx = mk_ctx([market], books, pf=pf)
    rep = RiskGate(RiskConfig(daily_loss_limit_usd=D(20)), Mode.PAPER).evaluate(
        [intent(), intent(side=Side.SELL, price="0.47", size=10)], ctx, True)
    assert [i.side for i in rep.accepted] == [Side.SELL]


# ------------------------------------------------------------------ paper exchange

@pytest.fixture
def paper(market):
    px = PaperExchange(lambda t: market, D(300))
    px.on_book(mk_book("Y1", "0.40", "0.46", ask_sz="20", extra_asks=[("0.47", "20")]))
    return px


async def test_paper_post_only_cross_rejected(paper):
    with pytest.raises(ExchangeError):
        await paper.place(intent(price="0.46"))


async def test_paper_taker_fak_walks_book_with_fee(paper, market):
    o = await paper.place(intent(price="0.47", size=30, tif=TimeInForce.FAK, post_only=False))
    fills = await paper.drain_fills()
    assert o.filled == D(30) and len(fills) == 1
    f = fills[0]
    assert f.size == D(30) and f.price == (D("0.46") * 20 + D("0.47") * 10) / 30
    assert f.fee > 0 and not f.maker
    # FOK beyond depth fails
    with pytest.raises(ExchangeError):
        await paper.place(intent(price="0.47", size=100, tif=TimeInForce.FOK, post_only=False))


async def test_paper_resting_fill_on_trade_and_book_cross(paper):
    o = await paper.place(intent(price="0.41", size=10))
    assert (await paper.open_orders()) == [o]
    paper.on_trade(Trade("Y1", D("0.41"), D(4), Side.SELL, 0.0))       # partial
    fills = await paper.drain_fills()
    assert fills[0].size == D(4) and fills[0].maker and fills[0].fee == 0
    paper.on_book(mk_book("Y1", "0.38", "0.40", ask_sz="100"))         # ask drops through our bid
    fills = await paper.drain_fills()
    assert fills[0].size == D(6) and await paper.open_orders() == []


# ------------------------------------------------------------------ OMS

async def test_oms_places_replaces_and_cancels(paper):
    store = Store(":memory:")
    oms = OMS(paper, ExecutionConfig(requote_epsilon=D("0.005"), min_seconds_between_requotes=0.0), store)
    await oms.sync([intent(price="0.41")])
    assert len(await paper.open_orders()) == 1 and oms.placed == 1
    await oms.sync([intent(price="0.412")])          # within epsilon: keep
    assert oms.placed == 1 and oms.cancelled == 0
    await oms.sync([intent(price="0.42")])           # replace
    assert oms.placed == 2 and oms.cancelled == 1
    assert (await paper.open_orders())[0].price == D("0.42")
    await oms.sync([])                               # not desired: cancel
    assert await paper.open_orders() == [] and oms.cancelled == 2
    assert store.summary()["orders_placed"] == 2
    store.close()


async def test_oms_taker_cooldown(paper):
    store = Store(":memory:")
    oms = OMS(paper, ExecutionConfig(taker_retry_seconds=60.0), store)
    it = intent(price="0.46", size=5, tif=TimeInForce.FAK, post_only=False)
    await oms.sync([it])
    await oms.sync([it])
    assert oms.placed == 1
    store.close()
