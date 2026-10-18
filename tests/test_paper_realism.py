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
    s.fill(Fill("paper-a", "t", "maker", "x|L", Side.BUY, D("0.53"), D(20), ts=1.0))
    s.fill(Fill("paper-b", "t", "maker", "x|S", Side.BUY, D("0.40"), D(5), ts=2.0))
    s.fill(Fill("0xLIVE", "t", "maker", "x|L", Side.BUY, D("0.50"), D(100), ts=3.0))   # a real fill: not replayed
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


def _heartbeat(s, tok, bid, ask, start, end, every=10.0):
    """What the engine's timer writes for a quiet market while the recorder runs."""
    t = start
    while t <= end:
        _book_row(s, t, tok, bid, ask)
        t += every


def test_markout_uses_book_before_fill_and_forward_fills_quiet_periods():
    s = Store(":memory:")
    t0 = 1_000_000.0
    _book_row(s, t0 - 3, "Y1", "0.40", "0.42")          # book we were quoting against (mid 0.41)
    _book_row(s, t0 - 0.002, "Y1", "0.39", "0.41")      # the update that triggered the fill: the engine stores
                                                        # it a moment BEFORE the paper fill is stamped (mid 0.40)
    _heartbeat(s, "Y1", "0.39", "0.41", t0 + 10, t0 + 600)   # quiet afterwards, recorder running
    s.fill(Fill("o", "t", "maker", "Y1", Side.BUY, D("0.40"), D(10), ts=t0))
    r = s.markouts(horizons=(30, 300))["by_strategy"][0]
    assert r["capture_c"] == pytest.approx(1.0)          # vs the PRE-fill mid
    assert r["drift_30s_c"] == pytest.approx(-1.0)
    assert r["drift_300s_c"] == pytest.approx(-1.0) and r["coverage_300s_pct"] == pytest.approx(100.0)
    s.close()


def test_markout_reports_uncovered_horizons_as_none_not_zero():
    s = Store(":memory:")
    t0 = 1_000_000.0
    _book_row(s, t0 - 3, "Y1", "0.40", "0.42")
    _heartbeat(s, "Y1", "0.40", "0.42", t0 + 5, t0 + 60)     # then the token stops being recorded...
    _heartbeat(s, "Z9", "0.20", "0.22", t0 - 3, t0 + 900)    # ...although the recorder keeps running
    s.fill(Fill("o", "t", "maker", "Y1", Side.BUY, D("0.40"), D(10), ts=t0))
    r = s.markouts(horizons=(30, 300))["by_strategy"][0]
    assert r["drift_30s_c"] == pytest.approx(0.0) and r["coverage_30s_pct"] == pytest.approx(100.0)
    assert r["drift_300s_c"] is None and r["net_300s_c"] is None and r["coverage_300s_pct"] == 0
    s.close()


def test_markout_treats_a_recorder_restart_as_a_gap():
    """Stopped right after the fill and restarted 10 minutes later: the old forward-fill must not count."""
    s = Store(":memory:")
    t0 = 1_000_000.0
    _heartbeat(s, "Y1", "0.40", "0.42", t0 - 30, t0 + 5)
    _heartbeat(s, "Y1", "0.30", "0.32", t0 + 600, t0 + 900)  # after the restart
    s.fill(Fill("o", "t", "maker", "Y1", Side.BUY, D("0.40"), D(10), ts=t0))
    r = s.markouts(horizons=(30, 300))["by_strategy"][0]
    assert r["drift_30s_c"] is None and r["drift_300s_c"] is None
    s.close()


def test_book_flicker_that_reverts_is_not_written():
    s = Store(":memory:", book_snapshot_interval=60.0)
    s.book(mk_book("Y1", "0.40", "0.43"))
    s.book(mk_book("Y1", "0.41", "0.43"))               # pending inside the rate window...
    s.book(mk_book("Y1", "0.40", "0.43"))               # ...and back to what the tape already shows
    assert [r[0] for r in s.query("SELECT best_bid FROM books ORDER BY ts")] == ["0.40"]
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
    # The bid steps up 9 times; requotes must follow replay time (17 orders). On wall-clock cooldowns the whole
    # replay fits inside one 10 s requote interval and only 7 orders are placed.
    assert placed >= 10


# ------------------------------------------------------------------ review round 2 (adversarial review of 709e963)

def _us_market():
    from pm.venues.polymarket_us import market_from_us
    return market_from_us({"slug": "s", "title": "S", "outcomes": "[\"Yes\",\"No\"]", "feeCoefficient": 0.06,
                           "minimumTradeQty": 1, "orderPriceMinTickSize": 0.01, "endDate": "2099-01-01T00:00:00Z",
                           "active": True}, {"slug": "e", "title": "E", "category": "politics"})


def _us_books(bid, ask, bid_sz="100", ask_sz="100"):
    from pm.venues.polymarket_us import books_from_us
    return books_from_us("s", {"bids": [{"px": {"value": bid}, "qty": bid_sz}],
                               "offers": [{"px": {"value": ask}, "qty": ask_sz}]})


def _us_print(px_, price, qty, taker_buy):
    from pm.venues.polymarket_us import trades_from_us
    for t in trades_from_us("s", {"price": {"value": price}, "quantity": {"value": qty},
                                  "taker": {"side": "ORDER_SIDE_BUY" if taker_buy else "ORDER_SIDE_SELL"}}):
        px_.on_trade(t)


async def test_mirrored_orders_share_one_real_queue_and_one_print():
    """SELL |L at 0.51 and BUY |S at 0.49 are both offers at 0.51 on the one US book."""
    m = _us_market()
    px = PaperExchange(lambda t: m, D(300))
    for b in _us_books("0.50", "0.51"):
        px.on_book(b)
    await px.place(Intent("maker", "s|L", Side.SELL, D("0.51"), D(10)))
    await px.place(Intent("maker", "s|S", Side.BUY, D("0.49"), D(20)))
    _us_print(px, "0.51", "110", taker_buy=True)       # 100 ahead of us, then 10 for us
    fills = await px.drain_fills()
    assert sum(f.size for f in fills) == 10            # not 10 + 10 from the same print
    assert [(f.token_id, f.size) for f in fills] == [("s|L", D(10))]   # time priority: the earlier order
    _us_print(px, "0.51", "15", taker_buy=True)        # the next 15 go to the |S bid behind it
    assert [(f.token_id, f.size) for f in await px.drain_fills()] == [("s|S", D(15))]


async def test_book_before_trade_is_not_counted_twice():
    m = mk_market()
    px = PaperExchange(lambda t: m, D(300))
    px.on_book(mk_book("Y1", "0.40", "0.45", bid_sz="1000"))
    o = await px.place(Intent("maker", "Y1", Side.BUY, D("0.40"), D(20)))
    px.on_book(mk_book("Y1", "0.40", "0.45", bid_sz="400"))         # the book shows a 600 print first...
    px.on_trade(Trade("Y1", D("0.40"), D(600), Side.SELL, 0.0))     # ...then the print itself arrives
    assert await px.drain_fills() == [] and px.queue_ahead(o.order_id) == D(400)
    # same result in the natural order
    px2 = PaperExchange(lambda t: m, D(300))
    px2.on_book(mk_book("Y1", "0.40", "0.45", bid_sz="1000"))
    o2 = await px2.place(Intent("maker", "Y1", Side.BUY, D("0.40"), D(20)))
    px2.on_trade(Trade("Y1", D("0.40"), D(600), Side.SELL, 0.0))
    px2.on_book(mk_book("Y1", "0.40", "0.45", bid_sz="400"))
    assert await px2.drain_fills() == [] and px2.queue_ahead(o2.order_id) == D(400)


async def test_empty_and_crossed_snapshots_never_fill():
    m = mk_market()
    px = PaperExchange(lambda t: m, D(300))
    px.on_book(mk_book("Y1", "0.40", "0.45", bid_sz="100", ask_sz="100"))
    await px.place(Intent("maker", "Y1", Side.BUY, D("0.40"), D(20)))
    await px.place(Intent("maker", "Y1", Side.SELL, D("0.45"), D(20)))
    px.on_book(mk_book("Y1", None, None))                            # failed poll / halted
    px.on_book(mk_book("Y1", "0.46", "0.44"))                        # crossed mid-update snapshot
    px.on_book(mk_book("Y1", None, "0.45", ask_sz="100"))            # one side missing
    assert await px.drain_fills() == []


async def test_lone_order_is_filled_when_the_levels_behind_it_are_swept():
    m = mk_market()
    px = PaperExchange(lambda t: m, D(300))
    px.on_book(mk_book("Y1", "0.50", "0.54", bid_sz="500"))
    await px.place(Intent("maker", "Y1", Side.BUY, D("0.51"), D(20)))   # improves: alone at 0.51
    px.on_book(mk_book("Y1", "0.48", "0.54", bid_sz="500"))              # 0.50 taken out: a seller passed us
    assert [f.size for f in await px.drain_fills()] == [D(20)]


async def test_gtc_taker_remainder_is_not_refilled_from_the_same_liquidity():
    m = mk_market(fee_rate="0.04")
    px = PaperExchange(lambda t: m, D(300))
    px.on_book(mk_book("Y1", "0.49", "0.50", bid_sz="100", ask_sz="10"))
    o = await px.place(Intent("x", "Y1", Side.BUY, D("0.50"), D(100), post_only=False))
    px.on_book(mk_book("Y1", "0.49", "0.50", bid_sz="100", ask_sz="10"))
    fills = await px.drain_fills()
    assert sum(f.size for f in fills) == D(10) and await px.open_orders() == []


async def test_us_paper_fills_are_whole_contracts():
    m = _us_market()
    now = [1000.0]
    px = PaperExchange(lambda t: m, D(300), clock=lambda: now[0])
    for b in _us_books("0.50", "0.51", bid_sz="7"):
        px.on_book(b)
    await px.place(Intent("maker", "s|L", Side.BUY, D("0.50"), D(20)))        # 7 ahead
    for sz in ("10", "4"):                                               # 3 join behind, then 6 cancel pro rata
        for b in _us_books("0.50", "0.51", bid_sz=sz):
            px.on_book(b)                                                # ahead = 7 - 6*7/10 = 2.8
    now[0] += 30                                                         # a later, separate print
    _us_print(px, "0.50", "5", taker_buy=False)                          # 2.2 beyond the queue
    assert [f.size for f in await px.drain_fills()] == [D(2)]            # whole contracts only


def test_maker_posts_no_separate_inventory_ask_on_mirrored_venues():
    from pm.strategy.maker import Maker
    m = _us_market()
    pf = Portfolio(cash=D(300))
    pf.register_mirrors([m])
    pf.position("s|L").shares, pf.position("s|L").cost = D(20), D(10)
    params = {"quote_size_shares": 20, "half_spread": 0.01, "min_book_spread": 0.0, "max_book_spread": 0.15,
              "min_depth_shares_at_touch": 20, "price_band": [0.10, 0.90], "max_inventory_shares": 40,
              "sell_inventory": True, "join_best": True, "min_half_spread": 0.0, "rewards_first": False}
    out = Maker(params).on_tick(mk_ctx([m], list(_us_books("0.50", "0.51")), pf=pf))
    assert all(i.side is Side.BUY for i in out)                         # the |S bid is the unwind
    assert any(i.token_id == "s|S" for i in out)


def test_closing_bid_on_mirror_is_not_charged_as_new_exposure():
    m = _us_market()
    pf = Portfolio(cash=D(300))
    pf.register_mirrors([m])
    pf.position("s|L").shares, pf.position("s|L").cost = D(80), D(40)  # at the per-market cap
    close = Intent("maker", "s|S", Side.BUY, D("0.49"), D(20))
    ctx = mk_ctx([m], list(_us_books("0.50", "0.51")), pf=pf)
    cfg = RiskConfig(max_notional_per_market_usd=D(40), min_cash_reserve_usd=D(0))
    assert RiskGate(cfg, Mode.PAPER).evaluate([close], ctx, True).accepted == [close]
    opener = Intent("maker", "s|L", Side.BUY, D("0.50"), D(20))
    assert RiskGate(cfg, Mode.PAPER).evaluate([opener], ctx, True).accepted == []
    pf.day_start_equity = D(1000)                                      # deep in the daily loss limit
    rep = RiskGate(cfg, Mode.PAPER).evaluate([close, opener], ctx, True)
    assert rep.accepted == [close]


def test_live_excess_sell_does_not_invent_a_position():
    m = _us_market()
    pf = Portfolio(cash=D(300), open_mirror_on_excess_sell=False)
    pf.register_mirrors([m])
    pf.apply_fill(Fill("v1", "t", "maker", "s|L", Side.SELL, D("0.52"), D(20)))
    assert pf.positions.get("s|S") is None or pf.shares("s|S") == 0
    assert pf.unbacked_sells == D(20) and pf.cash == D(300)


# ------------------------------------------------------------------ round 3: quote flapping found in the real re-score

def _us_mkts(n):
    from pm.venues.polymarket_us import market_from_us
    ev = {"slug": "e", "title": "E", "category": "politics", "endDate": "2099-01-01T00:00:00Z"}
    return [market_from_us({"slug": f"m{i}", "title": f"m{i}", "outcomes": "[\"Yes\",\"No\"]", "feeCoefficient": 0.06,
                            "minimumTradeQty": 1, "orderPriceMinTickSize": 0.01, "endDate": "2099-01-01T00:00:00Z",
                            "active": True}, ev) for i in range(n)]


async def test_resting_closers_are_charged_like_new_ones_so_quotes_do_not_flap():
    """Holding inventory makes the maker post closing bids. When new closers were charged only for the part that
    opens exposure but resting ones in full, every order accepted on one tick put the book over the total cap on the
    next: all quotes were cancelled and re-placed on alternate ticks (22,352 orders for 3 fills on the user's tape)."""
    from datetime import datetime, timezone
    from pm.state import Context, MarketState
    from pm.strategy import build_strategies
    from pm.venues.polymarket_us import books_from_us
    cfg = Config.load("config.yaml", env_path="/nonexistent/.env")
    markets = _us_mkts(12)
    ms = MarketState(); ms.set_markets(markets)
    pf = Portfolio(cash=D(300)); pf.register_mirrors(markets)
    for m in markets[:4]:                                              # inventory -> closing bids on the other side
        pf.position(m.yes.token_id).shares, pf.position(m.yes.token_id).cost = D(20), D(10)
    now = [1_000_000.0]
    px = PaperExchange(ms.market_for, pf.cash, clock=lambda: now[0])
    store = Store(":memory:")
    oms = OMS(px, cfg.execution, store, clock=lambda: now[0])
    risk = RiskGate(cfg.risk, Mode.PAPER)
    strategies = build_strategies(cfg.strategies, "polymarket_us")
    placed = []
    for k in range(6):
        for i, m in enumerate(markets):
            bid = D("0.30") + D("0.04") * i
            for b in books_from_us(m.slug, {"bids": [{"px": {"value": str(bid)}, "qty": "5000"}],
                                            "offers": [{"px": {"value": str(bid + D("0.01"))}, "qty": "5000"}]}, ts=now[0]):
                ms.update_book(b); px.on_book(b)
        ctx = Context(now=datetime.fromtimestamp(now[0], tz=timezone.utc), now_ts=now[0], markets=ms.markets,
                      books=ms.books, token_to_market=ms.token_to_market, portfolio=pf,
                      open_orders=await px.open_orders(), params={})
        rep = risk.evaluate([i for s in strategies for i in s.on_tick(ctx)], ctx, True)
        before = oms.placed
        await oms.sync(rep.accepted)
        placed.append(oms.placed - before)
        now[0] += 5
    assert placed[0] > 0 and placed[1:] == [0] * 5, placed            # placed once, then left alone
    assert oms.cancelled == 0
    store.close()


def test_withdrawal_diagnostics_attribute_the_reason():
    from pm.diagnostics import Withdrawals
    from pm.strategy.maker import Maker
    m = mk_market()
    resting = [Order("o1", "maker:Y1:BUY", "maker", "Y1", Side.BUY, D("0.40"), D(10)),
               Order("o2", "maker:N1:BUY", "maker", "N1", Side.BUY, D("0.50"), D(10))]
    mk = Maker({"stale_after_seconds": 15})
    mk.on_tick(mk_ctx([m], [mk_book("Y1", "0.40", "0.46", ts=1.0), mk_book("N1", "0.54", "0.60")]))
    w = Withdrawals()
    w.record(resting, [], [], [mk], lambda t: m)
    assert w.reasons == {"strategy: book stale": 2}
    w2 = Withdrawals()
    w2.record(resting, [], [(Intent("maker", "Y1", Side.BUY, D("0.41"), D(10)), "total cap 200 (would be 201.00)")],
              [mk], lambda t: m)
    assert w2.reasons["risk: total cap 200"] == 1


def test_a_live_stream_keeps_unchanged_books_current():
    from pm.feed import MarketFeed
    f = MarketFeed("wss://x", lambda b: None, lambda t: None)
    f._tokens = {"T1", "T2"}
    f.connected = True
    assert f.streaming_tokens() == set()                               # nothing heard yet
    f._handle_raw("PONG")                                              # a pong proves the stream is current
    assert f.streaming_tokens() == {"T1", "T2"}
    f.connected = False
    assert f.streaming_tokens() == set()


def test_backtest_treats_a_quiet_streamed_book_as_current_until_its_rows_stop():
    from pm.backtest import _touch_live_books
    from pm.state import MarketState
    ms = MarketState()
    ms.set_markets([mk_market()])
    b = mk_book("Y1", "0.40", "0.42", ts=100.0)
    ms.update_book(b)
    rows = {"Y1": [100.0, 160.0, 1000.0]}                              # quiet 60 s, then a 14-minute gap
    _touch_live_books(ms, rows, 130.0)
    assert b.ts == 130.0                                               # unchanged, still watched: current
    b.ts = 160.0
    _touch_live_books(ms, rows, 700.0)
    assert b.ts == 160.0                                               # inside the gap: stale, as it was live


def test_requoted_closer_releases_its_old_claim_on_held_shares():
    m = _us_mkts(1)[0]
    pf = Portfolio(cash=D(300)); pf.register_mirrors([m])
    pf.position(m.yes.token_id).shares, pf.position(m.yes.token_id).cost = D(20), D(10)
    resting = Order("o1", f"maker:{m.no.token_id}:BUY", "maker", m.no.token_id, Side.BUY, D("0.49"), D(20))
    ctx = mk_ctx([m], list(_us_books("0.50", "0.51")), pf=pf, open_orders=[resting])
    requote = Intent("maker", m.no.token_id, Side.BUY, D("0.50"), D(20))
    rep = RiskGate(RiskConfig(max_notional_per_market_usd=D(15), min_cash_reserve_usd=D(0)), Mode.PAPER).evaluate(
        [requote], ctx, True)
    assert [(i.price, i.reason) for i in rep.accepted] == [(D("0.50"), "")]   # still a closer: charged nothing
