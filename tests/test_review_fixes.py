"""Tests for findings from the adversarial review of the 2026-10-15 fixes (each upheld by >= 2 of 3 skeptics)."""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from pm.config import Config, ExecutionConfig, RiskConfig
from pm.execution import ExchangeError
from pm.execution.live import skip_settlement_wait
from pm.execution.paper import PaperExchange
from pm.models import Intent, Mode, Order, OrderStatus, Side, TimeInForce
from pm.oms import OMS
from pm.risk import RiskGate
from pm.store import Store
from pm.strategy import build_strategies
from pm.strategy.complete_set import CompleteSet
from pm.strategy.maker import Maker
from pm.venues.polymarket_us import books_from_us, market_from_us
from tests.conftest import D, mk_book, mk_ctx, mk_market


def _legs(y_size=10, n_size=10):
    return [Intent("complete_set", "Y1", Side.BUY, D("0.45"), D(y_size), tif=TimeInForce.FAK, post_only=False,
                   tag="cs:m:YES", group="cs:m"),
            Intent("complete_set", "N1", Side.BUY, D("0.52"), D(n_size), tif=TimeInForce.FAK, post_only=False,
                   tag="cs:m:NO", group="cs:m")]


class _FailingFirstLeg(PaperExchange):
    """The CLOB answer when the ask is taken between snapshot and send."""

    async def place(self, intent):
        if intent.tag.endswith(":YES"):
            raise ExchangeError("no orders found to match with FAK order")
        return await super().place(intent)


# ------------------------------------------------------------------ OMS: groups fire as a unit

async def test_failed_first_leg_aborts_the_rest_of_the_set():
    m = mk_market(fee_rate="0")
    px = _FailingFirstLeg(lambda t: m, D(300))
    px.on_book(mk_book("Y1", "0.40", "0.45"))
    px.on_book(mk_book("N1", "0.50", "0.52"))
    store = Store(":memory:")
    oms = OMS(px, ExecutionConfig(), store)
    await oms.sync(_legs())
    assert oms.placed == 0 and oms.rejected == 1
    assert await px.drain_fills() == []                                   # no naked NO leg
    assert store.query("SELECT COUNT(*) FROM orders WHERE event='aborted'")[0][0] == 1
    store.close()


async def test_partial_first_leg_resizes_the_second():
    m = mk_market(fee_rate="0")
    px = PaperExchange(lambda t: m, D(300))
    px.on_book(mk_book("Y1", "0.40", "0.45", ask_sz="7"))                # only 7 YES available
    px.on_book(mk_book("N1", "0.50", "0.52", ask_sz="100"))
    store = Store(":memory:")
    oms = OMS(px, ExecutionConfig(), store)
    await oms.sync(_legs(30, 30))
    fills = {f.token_id: f.size for f in await px.drain_fills()}
    assert fills == {"Y1": D(7), "N1": D(7)}                              # balanced set of 7, not 7 vs 30
    store.close()


async def test_shortfall_on_last_leg_is_recorded_as_imbalance():
    m = mk_market(fee_rate="0")
    px = PaperExchange(lambda t: m, D(300))
    px.on_book(mk_book("Y1", "0.40", "0.45", ask_sz="100"))
    px.on_book(mk_book("N1", "0.50", "0.52", ask_sz="4"))
    store = Store(":memory:")
    oms = OMS(px, ExecutionConfig(), store)
    await oms.sync(_legs(10, 10))
    assert store.query("SELECT COUNT(*) FROM events WHERE kind='group_imbalanced'")[0][0] == 1
    store.close()


# ------------------------------------------------------------------ Risk: rejected groups release budget

def test_rejected_group_releases_budget_to_later_intents():
    m = mk_market(fee_rate="0")
    ctx = mk_ctx([m], [mk_book("Y1", "0.40", "0.45"), mk_book("N1", "0.50", "0.52")])
    legs = CompleteSet({"margin": 0.01, "max_legs_notional_usd": 30}).on_tick(ctx)   # ~$15 + ~$15
    maker_bid = Intent("maker", "Y1", Side.BUY, D("0.40"), D(40))                     # $16
    # per-market cap 20: YES leg ($13.9) fits, NO leg does not -> group out; the maker bid must then fit.
    cfg = RiskConfig(max_notional_per_market_usd=D(20), min_cash_reserve_usd=D(0))
    rep = RiskGate(cfg, Mode.PAPER).evaluate(legs + [maker_bid], ctx, True)
    assert [i.tag for i in rep.accepted] == [maker_bid.tag]
    reasons = {i.tag: why for i, why in rep.rejected}
    assert reasons[legs[1].tag].startswith("per-market cap")             # the leg that failed keeps its reason
    assert reasons[legs[0].tag].startswith("group leg rejected")


# ------------------------------------------------------------------ CLOB SDK: no 30s settlement wait

def test_skip_settlement_wait_replaces_hash_resolution_on_the_instance_only():
    class FakeClient:
        def _resolve_transactions_hashes(self, response):
            raise AssertionError("would poll get_trades for up to 30s")

    c = FakeClient()
    skip_settlement_wait(c)
    resp = {"orderID": "x", "tradeIDs": ["t1"]}
    assert c._resolve_transactions_hashes(resp) is resp
    with pytest.raises(AssertionError):
        FakeClient()._resolve_transactions_hashes(resp)                   # class untouched


def test_skip_settlement_wait_against_installed_sdk():
    sdk = pytest.importorskip("py_clob_client_v2")
    c = sdk.ClobClient(host="https://clob.invalid", chain_id=137)
    skip_settlement_wait(c)
    r = {"orderID": "x", "tradeIDs": ["t1"], "status": "matched"}
    assert c._resolve_transactions_hashes(r) is r


# ------------------------------------------------------------------ US venue: orphans, adoption, expansion

class _Orders:
    def __init__(self, open_rows, create_exc=None):
        self.open_rows, self.create_exc, self.cancelled = list(open_rows), create_exc, []

    async def create(self, params):
        if self.create_exc:
            raise self.create_exc
        return {"id": "new", "executions": []}

    async def list(self, params=None):
        slugs = (params or {}).get("slugs")
        return {"orders": [r for r in self.open_rows if not slugs or r.get("marketSlug") in slugs]}

    async def cancel(self, oid, params):
        self.cancelled.append((oid, params.get("marketSlug")))

    async def retrieve(self, oid):
        return {"order": {}}


class _Client:
    def __init__(self, orders):
        self.orders = orders


def _us_exchange(orders):
    from pm.venues.polymarket_us import USExchange
    m = mk_market("fed", "fed|L", "fed|S", fee_rate="0.06")
    x = USExchange(Config(), lambda t: m)
    x._client = _Client(orders)
    return x


async def test_us_create_timeout_adopts_the_order_the_venue_accepted():
    accepted = {"id": "o-123", "marketSlug": "fed", "intent": "ORDER_INTENT_BUY_LONG",
                "price": {"value": "0.41", "currency": "USD"}, "quantity": 10, "cumQuantity": 0,
                "leavesQuantity": 10, "state": "ORDER_STATE_NEW"}
    x = _us_exchange(_Orders([accepted], create_exc=asyncio.TimeoutError("read timeout")))
    o = await x.place(Intent("maker", "fed|L", Side.BUY, D("0.41"), D(10)))
    assert o.order_id == "o-123" and (await x.open_orders())[0].order_id == "o-123"
    # nothing to adopt -> the error surfaces
    x2 = _us_exchange(_Orders([], create_exc=asyncio.TimeoutError("read timeout")))
    with pytest.raises(ExchangeError):
        await x2.place(Intent("maker", "fed|L", Side.BUY, D("0.41"), D(10)))


async def test_us_poll_cancels_orphans_even_with_nothing_tracked():
    orphan = {"id": "ghost", "marketSlug": "fed", "intent": "ORDER_INTENT_BUY_LONG", "price": {"value": "0.4"},
              "quantity": 5, "cumQuantity": 0, "leavesQuantity": 5, "state": "ORDER_STATE_NEW"}
    orders = _Orders([orphan])
    x = _us_exchange(orders)
    await x.drain_fills()
    assert orders.cancelled == [("ghost", "fed")]


class _Events:
    def __init__(self, full, delay=0.0, bad=()):
        self.full, self.delay, self.bad, self.calls = full, delay, set(bad), []

    async def retrieve_by_slug(self, slug):
        self.calls.append(slug)
        await asyncio.sleep(self.delay)
        if slug in self.bad:
            return "not a dict"
        return {"event": self.full[slug]}


def _venue(cfg, events_api):
    from pm.venues.polymarket_us import PolymarketUSVenue
    v = PolymarketUSVenue.__new__(PolymarketUSVenue)
    v.cfg, v.data, v._exchange = cfg, None, None

    class P:
        pass
    v.public = P()
    v.public.events = events_api
    return v


async def test_expand_events_is_bounded_and_keeps_listed_on_timeout_or_bad_payload():
    cfg = Config()
    cfg.venue_us.expand_timeout_seconds = 0.2
    listed = [{"slug": "slow", "title": "A", "markets": [{"slug": "a1"}]},
              {"slug": "bad", "title": "B", "markets": [{"slug": "b1"}]}]
    api = _Events({"slow": {"slug": "slow", "markets": [{"slug": "a1"}, {"slug": "a2"}]}}, delay=5.0, bad={"bad"})
    t0 = asyncio.get_running_loop().time()
    out = await _venue(cfg, api).expand_events(listed)
    assert asyncio.get_running_loop().time() - t0 < 2.0
    assert [len(e["markets"]) for e in out] == [1, 1]                     # both keep the listed version


async def test_sports_events_are_expanded_when_sports_are_not_excluded():
    cfg = Config()
    sports_ev = {"slug": "nyk-bos", "title": "Knicks vs Celtics", "category": "sports", "markets": [{"slug": "ml"}]}
    full = {"nyk-bos": {**sports_ev, "markets": [{"slug": "ml"}, {"slug": "spread"}]}}
    cfg.universe.exclude_tags = ["sports"]
    api = _Events(full)
    await _venue(cfg, api).expand_events([sports_ev])
    assert api.calls == []
    cfg.universe.exclude_tags = []
    api = _Events(full)
    out = await _venue(cfg, api).expand_events([sports_ev])
    assert api.calls == ["nyk-bos"] and len(out[0]["markets"]) == 2


async def test_configured_rebate_reaches_selected_markets(monkeypatch):
    import pm.universe
    from tests.test_us import REAL_EVENT, REAL_ROW, _Public
    monkeypatch.setattr(pm.universe, "utcnow", lambda: datetime(2026, 10, 1, tzinfo=timezone.utc))
    cfg = Config()
    cfg.universe.min_liquidity_usd = D(0)
    cfg.universe.max_days_to_resolution = 365
    cfg.venue_us.maker_rebate_coef = D("0.0035")                          # tier 2
    book = {"bids": [{"px": {"value": "0.53"}, "qty": "500"}], "offers": [{"px": {"value": "0.54"}, "qty": "500"}]}
    v = _venue(cfg, None)
    v.public = _Public([{**REAL_EVENT, "markets": [REAL_ROW]}], {REAL_ROW["slug"]: book},
                       full={REAL_EVENT["slug"]: {**REAL_EVENT, "markets": [REAL_ROW]}})
    out = await v.select_universe()
    assert out and out[0].maker_rebate_rate == D("0.0035")


# ------------------------------------------------------------------ Maker: no rebate -> no free join

def test_shipped_us_maker_joins_cent_books_but_not_tenth_cent_books():
    cfg = Config.load("config.yaml", env_path="/nonexistent/.env")
    maker = next(s for s in build_strategies(cfg.strategies, "polymarket_us") if s.name == "maker")
    from tests.test_us import REAL_EVENT, REAL_ROW
    m = market_from_us(REAL_ROW, REAL_EVENT)                               # rebate 0 by default
    m.end_date = datetime.now(timezone.utc).replace(year=2099)
    cent = {"bids": [{"px": {"value": "0.53"}, "qty": "500"}], "offers": [{"px": {"value": "0.54"}, "qty": "500"}]}
    tenth = {"bids": [{"px": {"value": "0.536"}, "qty": "500"}], "offers": [{"px": {"value": "0.537"}, "qty": "500"}]}
    out = maker.on_tick(mk_ctx([m], list(books_from_us(m.slug, cent))))
    assert next(i for i in out if i.token_id.endswith("|L")).price == D("0.53")      # joins: 0.5c of edge
    out = maker.on_tick(mk_ctx([m], list(books_from_us(m.slug, tenth))))
    assert next(i for i in out if i.token_id.endswith("|L")).price < D("0.536")      # stands back
    # a venue rebate counts as edge: a large enough coefficient makes the tenth-cent join worthwhile
    rich = market_from_us(REAL_ROW, REAL_EVENT, maker_rebate_coef=D("0.05"))
    out = maker.on_tick(mk_ctx([rich], list(books_from_us(rich.slug, tenth))))
    assert next(i for i in out if i.token_id.endswith("|L")).price == D("0.536")


# ------------------------------------------------------------------ neg-risk guards

def _neg_risk_event(prices, ids=None):
    ids = ids or [f"Y{i}" for i in range(len(prices))]
    ms = [mk_market(f"c{i}", ids[i], f"N{i}", fee_rate="0", neg_risk=True, event_id="E", event_count=len(prices))
          for i in range(len(prices))]
    books = [mk_book(ids[i], str(D(p) - D("0.02")), p) for i, p in enumerate(prices)]
    return ms, books


def test_neg_risk_legs_share_a_group_and_respect_guards():
    ms, books = _neg_risk_event(["0.32", "0.31", "0.30"])
    out = CompleteSet({"margin": 0.01}).on_tick(mk_ctx(ms, books))
    assert len(out) == 3 and len({i.group for i in out}) == 1 and out[0].group
    crossed = list(books)
    crossed[1] = mk_book("Y1", "0.33", "0.31")
    assert CompleteSet({"margin": 0.01}).on_tick(mk_ctx(ms, crossed)) == []
    dup_ms, dup_books = _neg_risk_event(["0.32", "0.31", "0.30"], ids=["Y0", "Y0", "Y2"])
    assert CompleteSet({"margin": 0.01}).on_tick(mk_ctx(dup_ms, dup_books)) == []


# ------------------------------------------------------------------ CLI: missing tapes are reported, not created

def test_backtest_reports_missing_tape_without_creating_it(tmp_path, capsys):
    from pm.backtest import run_backtest
    cfg = Config()
    cfg.db_path = str(tmp_path / "pm_{venue}.sqlite")
    assert run_backtest(cfg) == 1
    assert "No tape at" in capsys.readouterr().out
    assert not (tmp_path / "pm_polymarket.sqlite").exists()
