"""Tests for the scale features: markouts, reward-aware quoting, quote holding, user feed, alerts."""

import json
import time
from decimal import Decimal

import pytest

from pm.alerts import Alerts
from pm.clob import reward_from_row
from pm.config import ExecutionConfig, UniverseConfig
from pm.execution.paper import PaperExchange
from pm.feed import UserFeed
from pm.models import Fill, Order, Side
from pm.oms import OMS
from pm.store import Store
from pm.strategy.maker import Maker
from pm.universe import Universe
from tests.conftest import D, mk_book, mk_ctx, mk_market

MAKER = {
    "quote_size_shares": 10, "half_spread": 0.02, "min_book_spread": 0.02, "max_book_spread": 0.15,
    "min_depth_shares_at_touch": 50, "price_band": [0.10, 0.90], "max_inventory_shares": 40,
    "inventory_skew_per_share": 0.0005, "sell_inventory": True, "stale_after_seconds": 15,
    "hold_within": 0.01, "rewards_first": True, "reward_spread_buffer": 0.005,
}


# ------------------------------------------------------------------ markouts

def test_markouts_measure_capture_and_drift():
    s = Store(":memory:")
    t0 = 1_000_000.0
    b = mk_book("Y1", "0.40", "0.44")               # mid 0.42
    b.ts = t0
    s._q("INSERT INTO books(ts, token_id, best_bid, best_ask, bid_sz, ask_sz, top) VALUES (?,?,?,?,?,?,?)",
         (t0, "Y1", "0.40", "0.44", "100", "100", "{}"))
    s._q("INSERT INTO books(ts, token_id, best_bid, best_ask, bid_sz, ask_sz, top) VALUES (?,?,?,?,?,?,?)",
         (t0 + 30, "Y1", "0.38", "0.42", "100", "100", "{}"))   # mid 0.40: moved against our buy
    s._q("INSERT INTO books(ts, token_id, best_bid, best_ask, bid_sz, ask_sz, top) VALUES (?,?,?,?,?,?,?)",
         (t0 + 300, "Y1", "0.42", "0.46", "100", "100", "{}"))  # mid 0.44: recovered
    s.fill(Fill("o1", "maker:Y1:BUY", "maker", "Y1", Side.BUY, D("0.40"), D(10), ts=t0))
    mk = s.markouts(horizons=(30, 300))
    row = mk["by_strategy"][0]
    assert row["fills"] == 1 and row["capture_c"] == pytest.approx(2.0)       # bought 2c below mid
    assert row["drift_30s_c"] == pytest.approx(-2.0) and row["net_30s_c"] == pytest.approx(0.0)
    assert row["drift_300s_c"] == pytest.approx(2.0) and row["net_300s_c"] == pytest.approx(4.0)
    assert "maker" in json.dumps(s.summary())
    s.close()


def test_book_snapshots_only_on_change_or_heartbeat():
    s = Store(":memory:", book_snapshot_interval=0.0, book_heartbeat=1000.0)
    b = mk_book("Y1", "0.40", "0.44")
    s.book(b); s.book(b); s.book(b)
    assert s.query("SELECT COUNT(*) FROM books")[0][0] == 1
    s.book(mk_book("Y1", "0.41", "0.44"))
    assert s.query("SELECT COUNT(*) FROM books")[0][0] == 2
    s.close()


# ------------------------------------------------------------------ rewards

def test_reward_row_parsing_handles_cents_and_configs():
    r = reward_from_row({"condition_id": "0xc", "rewards_max_spread": 3.5, "rewards_min_size": 50,
                         "market_competitiveness": 1.2, "rewards_config": [{"rate_per_day": 40}, {"rate_per_day": 10}]})
    assert r.rate_per_day == D(50) and r.max_spread == D("0.035") and r.min_size == D(50)
    assert reward_from_row({"question": "no id"}) is None


def test_maker_quotes_inside_reward_band_at_min_size():
    m = mk_market()
    m.reward_rate_per_day, m.reward_max_spread, m.reward_min_size = D(50), D("0.03"), D(25)
    ctx = mk_ctx([m], [mk_book("Y1", "0.40", "0.46"), mk_book("N1", "0.54", "0.60")])  # fair 0.43
    out = Maker({**MAKER, "half_spread": 0.05}).on_tick(ctx)
    yes = next(i for i in out if i.token_id == "Y1")
    # half spread clamped to 0.03 - 0.005 = 0.025 -> 0.405 rounds down to 0.40 ; size raised to 25
    assert yes.price == D("0.40") and yes.size == D(25)
    assert abs(D("0.43") - yes.price) <= D("0.03")


class _Gamma:
    def __init__(self, m): self._m = m
    async def active_markets(self, limit=500): return self._m


class _Clob:
    def __init__(self, rewards): self._r = rewards
    async def rewards_by_condition(self): return self._r
    async def enrich_market(self, m): return m


async def test_universe_ranks_rewarded_markets_first():
    quiet = mk_market("q", "Qy", "Qn"); quiet.volume_24h_usd = D(1000)
    loud = mk_market("l", "Ly", "Ln"); loud.volume_24h_usd = D(900000)
    rewards = {"q": reward_from_row({"condition_id": "q", "rewards_max_spread": 3, "rewards_min_size": 20,
                                     "rewards_config": [{"rate_per_day": 30}]})}
    cfg = UniverseConfig(max_markets=1, min_liquidity_usd=D(0), min_volume_24h_usd=D(0), prefer_rewards=True)
    out = await Universe(_Gamma([loud, quiet]), _Clob(rewards), cfg).select()
    assert [m.condition_id for m in out] == ["q"] and out[0].has_rewards and out[0].reward_min_size == D(20)
    cfg.prefer_rewards = False
    out = await Universe(_Gamma([loud, quiet]), _Clob({}), cfg).select()
    assert [m.condition_id for m in out] == ["l"]


# ------------------------------------------------------------------ quote holding

def test_maker_holds_existing_quote_within_band():
    m = mk_market()
    books = [mk_book("Y1", "0.40", "0.46"), mk_book("N1", "0.54", "0.60")]
    existing = Order("o1", "maker:Y1:BUY", "maker", "Y1", Side.BUY, D("0.40"), D(10))  # target would be 0.41
    out = Maker(MAKER).on_tick(mk_ctx([m], books, open_orders=[existing]))
    assert next(i for i in out if i.token_id == "Y1").price == D("0.40")            # held
    far = Order("o1", "maker:Y1:BUY", "maker", "Y1", Side.BUY, D("0.36"), D(10))
    out = Maker(MAKER).on_tick(mk_ctx([m], books, open_orders=[far]))
    assert next(i for i in out if i.token_id == "Y1").price == D("0.41")            # too far: requote


async def test_oms_ignores_partial_fill_and_small_size_changes():
    m = mk_market()
    px = PaperExchange(lambda t: m, D(300))
    px.on_book(mk_book("Y1", "0.40", "0.46"))
    store = Store(":memory:")
    oms = OMS(px, ExecutionConfig(min_seconds_between_requotes=0.0, size_change_band=D("0.25")), store)
    from pm.models import Intent
    it = Intent(strategy="maker", token_id="Y1", side=Side.BUY, price=D("0.41"), size=D(10))
    await oms.sync([it])
    o = (await px.open_orders())[0]
    o.filled = D(4)                                    # partial fill must not trigger a replace
    await oms.sync([it])
    assert oms.placed == 1 and oms.cancelled == 0
    await oms.sync([Intent(strategy="maker", token_id="Y1", side=Side.BUY, price=D("0.41"), size=D(12))])
    assert oms.placed == 1                             # +20% is inside the band
    await oms.sync([Intent(strategy="maker", token_id="Y1", side=Side.BUY, price=D("0.41"), size=D(20))])
    assert oms.placed == 2 and oms.cancelled == 1      # +100% replaces
    store.close()


# ------------------------------------------------------------------ user feed + alerts

def test_user_feed_routes_order_and_trade_events():
    orders, trades = [], []
    f = UserFeed("wss://x", "k", "s", "p", orders.append, trades.append)
    f._handle_raw(json.dumps({"event_type": "order", "id": "abc", "size_matched": "5", "type": "UPDATE"}))
    f._handle_raw(json.dumps([{"event_type": "trade", "id": "t1", "status": "MATCHED"}]))
    f._handle_raw("PONG")
    assert orders[0]["id"] == "abc" and trades[0]["id"] == "t1" and f.messages == 2


def test_alerts_disabled_without_credentials_and_dedupes():
    a = Alerts()
    assert not a.enabled
    a.fire("x")                        # no loop, no creds: must not raise
    a = Alerts("t", "c", dedupe_seconds=60)
    a._last["k"] = time.time()
    a.fire("again", key="k")           # suppressed by dedupe; no running loop -> no task created
    assert a.sent == 0 and a.failed == 0
