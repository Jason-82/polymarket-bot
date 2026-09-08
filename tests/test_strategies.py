from decimal import Decimal

from pm.models import Side, TimeInForce
from pm.state import Portfolio
from pm.strategy.complete_set import CompleteSet
from pm.strategy.favorite_yield import FavoriteYield
from pm.strategy.maker import Maker
from tests.conftest import D, mk_book, mk_ctx, mk_market


# ------------------------------------------------------------------ complete_set

def test_complete_set_fires_when_asks_sum_below_one_after_fees():
    m = mk_market(fee_rate="0")
    ctx = mk_ctx([m], [mk_book("Y1", "0.40", "0.45"), mk_book("N1", "0.50", "0.52")])  # 0.97
    out = CompleteSet({"margin": 0.01, "max_legs_notional_usd": 30}).on_tick(ctx)
    assert len(out) == 2
    assert all(i.tif is TimeInForce.FAK and not i.post_only and i.side is Side.BUY for i in out)
    assert {i.token_id for i in out} == {"Y1", "N1"}
    # size capped by dollars: 30 / 0.97 -> 30 shares
    assert out[0].size == D(30)


def test_complete_set_respects_fees():
    m = mk_market(fee_rate="0.07")  # fees at ~0.45/0.52 ≈ 0.017+0.017 push cost over 0.99
    ctx = mk_ctx([m], [mk_book("Y1", "0.40", "0.45"), mk_book("N1", "0.50", "0.52")])
    assert CompleteSet({"margin": 0.01}).on_tick(ctx) == []


def test_complete_set_ignores_fair_books():
    ctx = mk_ctx([mk_market(fee_rate="0")], [mk_book("Y1", "0.48", "0.50"), mk_book("N1", "0.49", "0.51")])
    assert CompleteSet({"margin": 0.0}).on_tick(ctx) == []


def test_neg_risk_requires_complete_event():
    ms = [mk_market(f"c{i}", f"Y{i}", f"N{i}", fee_rate="0", neg_risk=True, event_id="E", event_count=3) for i in range(3)]
    books = [mk_book("Y0", "0.30", "0.32"), mk_book("Y1", "0.30", "0.31"), mk_book("Y2", "0.28", "0.30")]  # 0.93
    out = CompleteSet({"margin": 0.01}).on_tick(mk_ctx(ms, books))
    assert len(out) == 3 and all(i.side is Side.BUY for i in out)

    # missing one market -> no trade
    assert CompleteSet({"margin": 0.01}).on_tick(mk_ctx(ms[:2], books[:2])) == []

    # augmented event -> no trade
    for m in ms:
        m.neg_risk_augmented = True
    assert CompleteSet({"margin": 0.01}).on_tick(mk_ctx(ms, books)) == []


# ------------------------------------------------------------------ maker

MAKER = {
    "quote_size_shares": 10, "half_spread": 0.02, "min_book_spread": 0.02, "max_book_spread": 0.15,
    "min_depth_shares_at_touch": 50, "price_band": [0.10, 0.90], "max_inventory_shares": 40,
    "inventory_skew_per_share": 0.0005, "sell_inventory": True, "stale_after_seconds": 15,
}


def test_maker_quotes_both_bids_without_crossing():
    m = mk_market()
    ctx = mk_ctx([m], [mk_book("Y1", "0.40", "0.46"), mk_book("N1", "0.54", "0.60")])
    out = Maker(MAKER).on_tick(ctx)
    assert {i.token_id for i in out} == {"Y1", "N1"}
    for i in out:
        assert i.side is Side.BUY and i.post_only and i.tif is TimeInForce.GTC
        assert (i.price / m.tick_size) % 1 == 0
    yes = next(i for i in out if i.token_id == "Y1")
    no = next(i for i in out if i.token_id == "N1")
    # fair = (0.43 + (1-0.57))/2 = 0.43 ; yes bid = 0.41 ; no bid = 0.57-0.02 = 0.55
    assert yes.price == D("0.41") and no.price == D("0.55")
    assert yes.price < D("0.46") and no.price < D("0.60")


def test_maker_skips_tight_wide_thin_stale_and_extreme_books():
    m = mk_market()
    mk = Maker(MAKER)
    assert mk.on_tick(mk_ctx([m], [mk_book("Y1", "0.44", "0.45"), mk_book("N1", "0.55", "0.56")])) == []   # tight
    assert mk.on_tick(mk_ctx([m], [mk_book("Y1", "0.30", "0.60"), mk_book("N1", "0.40", "0.70")])) == []   # wide
    assert mk.on_tick(mk_ctx([m], [mk_book("Y1", "0.40", "0.46", bid_sz="5"), mk_book("N1", "0.54", "0.60")])) == []  # thin
    assert mk.on_tick(mk_ctx([m], [mk_book("Y1", "0.40", "0.46", ts=1.0), mk_book("N1", "0.54", "0.60")])) == []  # stale
    assert mk.on_tick(mk_ctx([m], [mk_book("Y1", "0.93", "0.96"), mk_book("N1", "0.04", "0.07")])) == []   # band


def test_maker_inventory_shrinks_and_unwinds():
    m = mk_market()
    pf = Portfolio(cash=D(300))
    pf.position("Y1").shares = D(40)   # at max inventory on YES
    pf.position("Y1").cost = D(16)
    ctx = mk_ctx([m], [mk_book("Y1", "0.40", "0.46"), mk_book("N1", "0.54", "0.60")], pf=pf)
    out = Maker(MAKER).on_tick(ctx)
    sides = {(i.token_id, i.side) for i in out}
    assert ("Y1", Side.BUY) not in sides           # no more YES bids
    assert ("N1", Side.BUY) in sides               # keep bidding NO (hedges)
    assert ("Y1", Side.SELL) in sides              # and offer the inventory
    ask = next(i for i in out if i.side is Side.SELL)
    assert ask.size == D(10) and ask.price > D("0.40")


# ------------------------------------------------------------------ favorite_yield

def test_favorite_yield_posts_bid_when_annualised_yield_clears():
    m = mk_market(days=5)
    ctx = mk_ctx([m], [mk_book("Y1", "0.96", "0.97"), mk_book("N1", "0.03", "0.04")])
    out = FavoriteYield({"min_price": 0.95, "max_days_to_resolution": 14, "min_annualized_yield": 0.25, "size_shares": 10}).on_tick(ctx)
    assert len(out) == 1 and out[0].token_id == "Y1" and out[0].price == D("0.96") and out[0].post_only
    # too far out -> nothing
    assert FavoriteYield({"max_days_to_resolution": 1}).on_tick(ctx) == []
