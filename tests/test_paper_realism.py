"""Paper-trading realism, from the 2026-10-15..17 Polymarket US paper run.

That run reported 49 maker fills, every one capturing exactly half the spread with exactly zero price
drift afterwards, and equity 300 next to those fills. Causes, each pinned below:
- the paper exchange filled resting orders on ANY trade at their price, ignoring thousands of contracts
  queued in front of them;
- the paper portfolio held YES and NO of a single US contract as two positions (the venue nets them);
- a sell could credit cash for shares no longer held;
- the paper portfolio was not rebuilt from the tape on restart;
- markouts counted an unmeasured horizon as zero drift.
"""

import asyncio
import json
import time
from decimal import Decimal

import pytest

from pm.config import Config, ExecutionConfig, RiskConfig
from pm.execution.paper import PaperExchange
from pm.feed import Trade
from pm.models import Fill, Intent, Mode, Order, Side
from pm.oms import OMS
from pm.risk import RiskGate
from pm.state import Portfolio
from pm.store import Store, market_from_json
from tests.conftest import D, mk_book, mk_ctx, mk_market


def _px(token="ga|L", bid="0.07", ask="0.08", bid_sz="6373", ask_sz="26914"):
    m = mk_market("ga", "ga|L", "ga|S", fee_rate="0.06")
    px = PaperExchange(lambda t: m, D(300))
    px.on_book(mk_book(token, bid, ask, bid_sz=bid_sz, ask_sz=ask_sz))
    return m, px


def _bid(price="0.07", size=20, token="ga|L"):
    return Intent("maker", token, Side.BUY, D(price), D(size))


# ------------------------------------------------------------------ queue position

async def test_small_print_does_not_fill_an_order_at_the_back_of_a_deep_queue():
    _, px = _px()
    o = await px.place(_bid())
    assert px.queue_ahead(o.order_id) == D(6373)
    px.on_trade(Trade("ga|L", D("0.07"), D(5), Side.SELL, 0.0))
    assert await px.drain_fills() == []
    assert px.queue_ahead(o.order_id) == D(6368)


async def test_volume_beyond_the_queue_fills_us():
    _, px = _px(bid_sz="30")
    o = await px.place(_bid())
    px.on_trade(Trade("ga|L", D("0.07"), D(25), Side.SELL, 0.0))      # 5 still ahead
    assert await px.drain_fills() == []
    px.on_trade(Trade("ga|L", D("0.07"), D(12), Side.SELL, 0.0))      # 5 to the queue, 7 to us
    fills = await px.drain_fills()
    assert [f.size for f in fills] == [D(7)] and px.queue_ahead(o.order_id) == 0


async def test_print_through_our_price_fills_everything():
    _, px = _px()
    await px.place(_bid())
    px.on_trade(Trade("ga|L", D("0.06"), D(1), Side.SELL, 0.0))
    assert [f.size for f in await px.drain_fills()] == [D(20)]


async def test_unexplained_shrink_is_pro_rata_cancellation():
    _, px = _px(bid_sz="1000")
    o = await px.place(_bid())
    px.on_book(mk_book("ga|L", "0.07", "0.08", bid_sz="1500", ask_sz="100"))   # 500 joined behind us
    assert px.queue_ahead(o.order_id) == D(1000)
    px.on_book(mk_book("ga|L", "0.07", "0.08", bid_sz="900", ask_sz="100"))    # 600 cancelled, 2/3 of them ahead
    assert px.queue_ahead(o.order_id) == D(600)
    assert await px.drain_fills() == []


async def test_swept_level_with_price_moving_away_fills_us():
    _, px = _px(bid_sz="1000")
    await px.place(_bid())
    px.on_book(mk_book("ga|L", "0.06", "0.08", bid_sz="5000", ask_sz="100"))   # 0.07 gone, best bid fell
    assert [f.size for f in await px.drain_fills()] == [D(20)]


async def test_lone_improving_order_is_not_filled_when_followers_cancel():
    _, px = _px(bid="0.06", ask="0.09", bid_sz="1000")
    o = await px.place(_bid("0.07"))                                           # improves the best bid
    assert px.queue_ahead(o.order_id) == 0
    px.on_book(mk_book("ga|L", "0.07", "0.09", bid_sz="300", ask_sz="100"))    # someone joins behind us
    px.on_book(mk_book("ga|L", "0.06", "0.09", bid_sz="1000", ask_sz="100"))   # ...and leaves
    assert await px.drain_fills() == []
    px.on_trade(Trade("ga|L", D("0.06"), D(3), Side.SELL, 0.0))                # a seller would have hit us first
    assert [f.size for f in await px.drain_fills()] == [D(20)]


async def test_opposite_side_reaching_our_price_fills_everything():
    _, px = _px()
    await px.place(_bid())
    px.on_book(mk_book("ga|L", "0.06", "0.07", bid_sz="10", ask_sz="5"))
    assert [f.size for f in await px.drain_fills()] == [D(20)]


async def test_queue_is_per_order_and_cleared_on_cancel():
    _, px = _px()
    o = await px.place(_bid())
    await px.cancel(o.order_id)
    assert px.queue_ahead(o.order_id) is None


# ------------------------------------------------------------------ US netting and cash integrity

def _us_portfolio():
    m = mk_market("x", "x|L", "x|S")
    m.synthetic_no = True
    pf = Portfolio(cash=D(300))
    pf.register_mirrors([m])
    return pf


def _equity_at(pf, mark_l):
    """Equity with the long marked at mark_l and the short at 1 - mark_l."""
    val = pf.cash
    for tid, p in pf.positions.items():
        mark = mark_l if tid.endswith("|L") else 1 - mark_l
        val += p.shares * mark
    return val


def test_buying_the_short_side_closes_a_held_long():
    pf = _us_portfolio()
    pf.apply_fill(Fill("a", "t", "maker", "x|L", Side.BUY, D("0.53"), D(20)))
    pf.apply_fill(Fill("b", "t", "maker", "x|S", Side.BUY, D("0.46"), D(20)))   # == sell long at 0.54
    assert pf.shares("x|L") == 0 and pf.shares("x|S") == 0
    assert pf.realised_pnl == D("0.20") and pf.cash == D("300.20")
    pf.apply_fill(Fill("c", "t", "maker", "x|S", Side.BUY, D("0.46"), D(5)))    # nothing to close: opens a short
    assert pf.shares("x|S") == 5


def test_stale_sell_never_creates_cash():
    pf = _us_portfolio()
    pf.apply_fill(Fill("a", "t", "maker", "x|L", Side.BUY, D("0.50"), D(20)))
    pf.apply_fill(Fill("b", "t", "maker", "x|S", Side.BUY, D("0.49"), D(20)))
    before = _equity_at(pf, D("0.50"))
    pf.apply_fill(Fill("c", "t", "maker", "x|L", Side.SELL, D("0.52"), D(20)))  # its long is already gone
    assert pf.shares("x|L") == 0 and pf.shares("x|S") == 20                     # it opened a short instead
    # selling the long at 0.52 when the mid is 0.50 is worth +0.02/share, not +0.52/share
    assert _equity_at(pf, D("0.50")) - before == D("0.40")
    plain = Portfolio(cash=D(100))
    plain.apply_fill(Fill("d", "t", "maker", "Y1", Side.SELL, D("0.52"), D(20)))  # non-mirrored: dropped
    assert plain.cash == D(100) and plain.unbacked_sells == D(20)


def test_portfolio_resume_from_tape(tmp_path):
    from pm.engine import Engine
    cfg = Config()
    cfg.db_path = str(tmp_path / "pm_{venue}.sqlite")
    cfg.venue = "polymarket"
    s = Store(cfg.db_file)
    m = mk_market("x", "x|L", "x|S")
    m.synthetic_no = True
    s.market(m)
    s.fill(Fill("a", "t", "maker", "x|L", Side.BUY, D("0.53"), D(20), ts=1.0))
    s.fill(Fill("b", "t", "maker", "x|S", Side.BUY, D("0.40"), D(5), ts=2.0))
    s.close()
    eng = Engine(cfg)
    try:
        eng._resume_paper_portfolio()
        assert eng.pf.shares("x|L") == 15 and eng.pf.shares("x|S") == 0      # netted on replay too
        assert eng.pf.cash == D(300) - D("10.60") + D("3.00")
    finally:
        eng.store.close()


# ------------------------------------------------------------------ markouts measure, or say they cannot

def _book_row(s, ts, tok, bid, ask):
    s._q(s._BOOK_SQL, (ts, tok, bid, ask, "100", "100", "{}"))


def test_markout_uses_book_before_fill_and_forward_fills_quiet_periods():
    s = Store(":memory:")
    t0 = 1_000_000.0
    _book_row(s, t0 - 3, "Y1", "0.40", "0.42")          # book we were quoting against (mid 0.41)
    _book_row(s, t0 + 0.001, "Y1", "0.39", "0.41")      # the update that triggered the fill (mid 0.40)
    _book_row(s, t0 + 400, "Y1", "0.39", "0.41")        # nothing else changed until after both horizons
    s.fill(Fill("o", "t", "maker", "Y1", Side.BUY, D("0.40"), D(10), ts=t0))
    r = s.markouts(horizons=(30, 300))["by_strategy"][0]
    assert r["capture_c"] == pytest.approx(1.0)          # vs the PRE-fill mid
    assert r["drift_30s_c"] == pytest.approx(-1.0)       # forward-filled from the trigger row
    assert r["drift_300s_c"] == pytest.approx(-1.0) and r["coverage_300s_pct"] == pytest.approx(100.0)
    s.close()


def test_markout_reports_uncovered_horizons_as_none_not_zero():
    s = Store(":memory:")
    t0 = 1_000_000.0
    _book_row(s, t0 - 3, "Y1", "0.40", "0.42")
    _book_row(s, t0 + 5, "Y1", "0.40", "0.42")          # then the token stops being recorded
    s.fill(Fill("o", "t", "maker", "Y1", Side.BUY, D("0.40"), D(10), ts=t0))
    r = s.markouts(horizons=(30, 300))["by_strategy"][0]
    assert r["drift_300s_c"] is None and r["net_300s_c"] is None and r["coverage_300s_pct"] == 0
    s.close()


def test_rate_limited_book_change_is_written_not_dropped():
    s = Store(":memory:", book_snapshot_interval=60.0)
    s.book(mk_book("Y1", "0.40", "0.42"))
    s.book(mk_book("Y1", "0.41", "0.43"))               # inside the rate window
    s.book(mk_book("Y1", "0.39", "0.41"))               # the final state of the burst
    rows = s.query("SELECT best_bid FROM books ORDER BY ts")
    assert [r[0] for r in rows] == ["0.40", "0.39"]
    s.close()


def test_old_us_tapes_infer_synthetic_markets():
    old = {"condition_id": "x", "tokens": [{"token_id": "x|L", "outcome": "Yes"}, {"token_id": "x|S", "outcome": "NOT Yes"}]}
    assert market_from_json(old).synthetic_no
    intl = {"condition_id": "0xc", "tokens": [{"token_id": "111", "outcome": "Yes"}, {"token_id": "222", "outcome": "No"}]}
    assert not market_from_json(intl).synthetic_no


# ------------------------------------------------------------------ risk: hold instead of flapping

def test_cap_breaching_requote_keeps_the_resting_order():
    m = mk_market()
    existing = Order("o1", "maker:Y1:BUY", "maker", "Y1", Side.BUY, D("0.40"), D(20))   # $8 resting
    ctx = mk_ctx([m], [mk_book("Y1", "0.40", "0.46")], open_orders=[existing])
    cfg = RiskConfig(max_notional_per_market_usd=D("8.10"), min_cash_reserve_usd=D(0))
    up = Intent("maker", "Y1", Side.BUY, D("0.41"), D(20))                              # $8.20: over the cap
    rep = RiskGate(cfg, Mode.PAPER).evaluate([up], ctx, True)
    assert len(rep.accepted) == 1 and rep.accepted[0].price == D("0.40") and rep.accepted[0].size == D(20)
    smaller = Intent("maker", "Y1", Side.BUY, D("0.41"), D(10))                         # existing is riskier
    rep = RiskGate(RiskConfig(max_notional_per_market_usd=D("3"), min_cash_reserve_usd=D(0)), Mode.PAPER).evaluate(
        [smaller], ctx, True)
    assert rep.accepted == []
    tight = RiskConfig(max_notional_per_market_usd=D("7"), min_cash_reserve_usd=D(0))   # existing no longer fits
    assert RiskGate(tight, Mode.PAPER).evaluate([up], ctx, True).accepted == []


async def test_held_order_is_left_alone_by_the_oms():
    m = mk_market()
    px = PaperExchange(lambda t: m, D(300))
    px.on_book(mk_book("Y1", "0.40", "0.46"))
    store = Store(":memory:")
    oms = OMS(px, ExecutionConfig(min_seconds_between_requotes=0.0), store)
    await oms.sync([Intent("maker", "Y1", Side.BUY, D("0.40"), D(20))])
    ctx = mk_ctx([m], [mk_book("Y1", "0.40", "0.46")], open_orders=await px.open_orders())
    rep = RiskGate(RiskConfig(max_notional_per_market_usd=D("8.10"), min_cash_reserve_usd=D(0)), Mode.PAPER).evaluate(
        [Intent("maker", "Y1", Side.BUY, D("0.41"), D(20))], ctx, True)
    await oms.sync(rep.accepted)
    assert oms.placed == 1 and oms.cancelled == 0
    store.close()


# ------------------------------------------------------------------ backtest runs on tape time

def test_backtest_uses_tape_time_for_window_and_cooldowns(tmp_path, capsys):
    """A tape from days ago must still be replayed, and requotes must follow replay time, not wall time."""
    from pm.backtest import run_backtest
    from pm.venues.polymarket_us import books_from_us, market_from_us
    path = tmp_path / "old.sqlite"
    s = Store(str(path))
    row = {"slug": "x", "title": "X", "outcomes": "[\"Yes\",\"No\"]", "feeCoefficient": 0.06, "minimumTradeQty": 1,
           "orderPriceMinTickSize": 0.01, "endDate": "2099-01-01T00:00:00Z", "active": True}
    s.market(market_from_us(row, {"slug": "e", "title": "E", "category": "politics", "endDate": "2099-01-01T00:00:00Z"}))
    t0 = time.time() - 5 * 86400                                             # five days old
    for k in range(240):                                                      # 20 min, bid steps up a cent every 2 min
        bid = D("0.40") + D("0.01") * (k // 24)
        payload = {"bids": [{"px": {"value": str(bid)}, "qty": "500"}],
                   "offers": [{"px": {"value": str(bid + D("0.02"))}, "qty": "500"}]}
        for b in books_from_us("x", payload, ts=t0 + 5 * k):
            s._q(s._BOOK_SQL, (t0 + 5 * k, b.token_id, str(b.best_bid), str(b.best_ask), str(b.bid_size_at_touch),
                               str(b.ask_size_at_touch), json.dumps(b.top(5))))
    s.close()
    cfg = Config.load("config.yaml", env_path="/nonexistent/.env")
    cfg.venue = "polymarket_us"
    assert run_backtest(cfg, since_hours=1, tick_seconds=5, db=str(path)) == 0
    out = capsys.readouterr().out
    assert "Backtest over 0.3h of tape" in out
    placed = int(out.split("orders placed .... ")[1].split()[0])
    assert placed > 4                                                         # requotes happened as the bid moved
