"""Regressions from the first real paper run (2026-09-08 to 09-09, 15.7h of tape).

complete_set bought the SAME Polymarket US token twice (Georgia Senate Collins: 2 x 177 @ 0.08, exactly
cap 30 / (2 x 0.08 + fees)); the phantom positions filled the risk caps and tripped the daily loss limit.
"""

from decimal import Decimal

from pm.config import Config, ExecutionConfig, RiskConfig
from pm.execution.paper import PaperExchange
from pm.models import Intent, Market, Mode, Side, TimeInForce, Token
from pm.oms import OMS
from pm.risk import RiskGate
from pm.store import Store
from pm.strategy import build_strategies
from pm.strategy.complete_set import CompleteSet
from pm.strategy.maker import Maker
from tests.conftest import D, mk_book, mk_ctx, mk_market

MAKER = {"quote_size_shares": 10, "half_spread": 0.02, "min_book_spread": 0.0, "max_book_spread": 0.15,
         "min_depth_shares_at_touch": 20, "price_band": [0.05, 0.95], "max_inventory_shares": 40,
         "sell_inventory": False, "join_best": True, "min_half_spread": 0.0, "rewards_first": False}


def _us_like(first_outcome: str) -> Market:
    m = mk_market("ga-collins", "ga-collins|L", "ga-collins|S", fee_rate="0.06")
    m.tokens = [Token("ga-collins|L", first_outcome), Token("ga-collins|S", f"NOT {first_outcome}")]
    return m


def test_yes_and_no_are_never_the_same_token():
    for label in ("Yes", "No", "no ", "Mike Collins (R)", "Above"):
        m = _us_like(label)
        assert m.yes.token_id != m.no.token_id, label
    m = _us_like("No")
    assert m.yes.token_id == "ga-collins|S" and m.no.token_id == "ga-collins|L"   # long side IS "No"
    plain = mk_market()
    assert plain.yes.token_id == "Y1" and plain.no.token_id == "N1"


def test_complete_set_cannot_buy_one_token_twice_even_on_mislabelled_market():
    """The exact tape: long book 0.07 / 0.08. Before the fix this produced two legs of 177 on |L."""
    m = _us_like("No")
    long_b = mk_book("ga-collins|L", "0.07", "0.08", bid_sz="6373", ask_sz="26914")
    short_b = mk_book("ga-collins|S", "0.92", "0.93", bid_sz="26914", ask_sz="6373")
    out = CompleteSet({"margin": 0.01, "max_legs_notional_usd": 30}).on_tick(mk_ctx([m], [long_b, short_b]))
    assert out == []
    # and the venue flag alone also blocks it
    m2 = _us_like("Yes")
    m2.synthetic_no = True
    crossed_l = mk_book("ga-collins|L", "0.10", "0.08")
    assert CompleteSet({"margin": 0.01}).on_tick(mk_ctx([m2], [crossed_l, short_b])) == []


def test_complete_set_ignores_crossed_books():
    m = mk_market(fee_rate="0")
    ctx = mk_ctx([m], [mk_book("Y1", "0.50", "0.45"), mk_book("N1", "0.50", "0.52")])
    assert CompleteSet({"margin": 0.01}).on_tick(ctx) == []


def test_complete_set_legs_are_atomic_in_the_risk_gate():
    m = mk_market(fee_rate="0")
    ctx = mk_ctx([m], [mk_book("Y1", "0.40", "0.45"), mk_book("N1", "0.50", "0.52")])
    legs = CompleteSet({"margin": 0.01, "max_legs_notional_usd": 30}).on_tick(ctx)
    assert len(legs) == 2 and legs[0].group and legs[0].group == legs[1].group
    # per-market cap of 20 admits the first leg ($13.5) but not the second -> neither may go
    rep = RiskGate(RiskConfig(max_notional_per_market_usd=D(20), min_cash_reserve_usd=D(0)), Mode.PAPER).evaluate(
        legs, ctx, True)
    assert rep.accepted == [] and len(rep.rejected) == 2
    assert any("group leg rejected" in why for _, why in rep.rejected)
    # with room for both, both pass
    rep = RiskGate(RiskConfig(min_cash_reserve_usd=D(0)), Mode.PAPER).evaluate(legs, ctx, True)
    assert len(rep.accepted) == 2


async def test_oms_fires_grouped_legs_together_under_one_cooldown():
    m = mk_market(fee_rate="0")
    px = PaperExchange(lambda t: m, D(300))
    px.on_book(mk_book("Y1", "0.40", "0.45"))
    px.on_book(mk_book("N1", "0.50", "0.52"))
    store = Store(":memory:")
    oms = OMS(px, ExecutionConfig(taker_retry_seconds=60.0), store)
    legs = [Intent("complete_set", "Y1", Side.BUY, D("0.45"), D(10), tif=TimeInForce.FAK, post_only=False,
                   tag="cs:Y", group="cs:m"),
            Intent("complete_set", "N1", Side.BUY, D("0.52"), D(10), tif=TimeInForce.FAK, post_only=False,
                   tag="cs:N", group="cs:m")]
    await oms.sync(legs)
    assert oms.placed == 2
    await oms.sync(legs)            # cooldown applies to the group: neither leg fires again
    assert oms.placed == 2
    store.close()


def test_maker_refuses_crossed_and_disagreeing_books():
    m = mk_market()
    mk = Maker(MAKER)
    assert mk.on_tick(mk_ctx([m], [mk_book("Y1", "0.45", "0.45"), mk_book("N1", "0.55", "0.56")])) == []   # locked
    assert mk.on_tick(mk_ctx([m], [mk_book("Y1", "0.46", "0.45"), mk_book("N1", "0.54", "0.55")])) == []   # crossed
    # YES mid 0.43 but NO mid 0.30 implies YES 0.70: one book is stale
    assert mk.on_tick(mk_ctx([m], [mk_book("Y1", "0.42", "0.44"), mk_book("N1", "0.29", "0.31")])) == []
    assert mk.on_tick(mk_ctx([m], [mk_book("Y1", "0.42", "0.44"), mk_book("N1", "0.56", "0.58")]))         # sane


def test_intent_log_records_changes_not_ticks():
    s = Store(":memory:")
    it = Intent("maker", "Y1", Side.BUY, D("0.41"), D(10))
    for _ in range(50):
        s.intent(it, False, "total cap 200 (would be 221.35)")
    s.intent(it, False, "total cap 200 (would be 222.10)")     # same reason, different detail: not a change
    assert s.query("SELECT COUNT(*) FROM intents")[0][0] == 1
    s.intent(it, True)                                          # decision changed
    s.intent(Intent("maker", "Y1", Side.BUY, D("0.42"), D(10)), True)   # price changed
    assert s.query("SELECT COUNT(*) FROM intents")[0][0] == 3
    s.close()


def test_venue_override_can_disable_a_strategy():
    cfg = {"complete_set": {"enabled": True, "venue_overrides": {"polymarket_us": {"enabled": False}}},
           "maker": {"enabled": True}}
    assert [s.name for s in build_strategies(cfg, "polymarket_us")] == ["maker"]
    assert sorted(s.name for s in build_strategies(cfg, "polymarket")) == ["complete_set", "maker"]


def test_db_file_is_per_venue():
    c = Config()
    c.db_path = "data/pm_{venue}.sqlite"
    c.venue = "us"
    assert c.db_file == "data/pm_polymarket_us.sqlite"
    c.venue = "polymarket"
    assert c.db_file == "data/pm_polymarket.sqlite"
    c.db_path = "data/pm.sqlite"
    assert c.db_file == "data/pm.sqlite"


def test_shipped_config_disables_complete_set_on_us():
    cfg = Config.load("config.yaml", env_path="/nonexistent/.env")
    assert "complete_set" not in [s.name for s in build_strategies(cfg.strategies, "polymarket_us")]
    assert "complete_set" in [s.name for s in build_strategies(cfg.strategies, "polymarket")]
    assert "{venue}" in cfg.db_path
