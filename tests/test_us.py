"""Polymarket US venue mapping: markets, books, trades, orders, positions, fees."""

from decimal import Decimal

from pm.config import Config, ExecutionConfig, RiskConfig
from pm.execution.paper import PaperExchange
from pm.fees import maker_rebate, taker_fee
from pm.models import Intent, Mode, Order, Side, TimeInForce
from pm.risk import RiskGate
from pm.state import Portfolio
from pm.strategy.maker import Maker
from pm.venues.polymarket_us import (
    books_from_us, market_from_us, order_params, positions_from_us, quote_hints, split_token, token_ids,
    trades_from_us,
)
from tests.conftest import D, mk_ctx

ROW = {
    "id": 7, "slug": "fed-cut-sep", "title": "Fed cuts rates in September?", "outcome": "Yes",
    "active": True, "closed": False, "liquidity": 120000.0, "volume": 550000.0, "eventSlug": "fomc-sep",
}
# Shape observed from the real venue (probe 2026-09-08): no outcome/liquidity/volume, but quotes and fees.
REAL_ROW = {
    "slug": "paccc-usse-midterms-2026-11-03-rep", "title": "Republican Party", "outcomes": ["Yes", "No"],
    "outcomePrices": ["0.62", "0.38"], "bestBidQuote": {"px": {"value": "0.61"}, "qty": "200"},
    "bestAskQuote": {"px": {"value": "0.63"}, "qty": "150"}, "feeCoefficient": 0.06, "minimumTradeQty": 5,
    "orderPriceMinTickSize": 0.01, "endDate": "2027-02-01T23:59:00Z", "status": "active", "active": True,
    "closed": False, "hidden": False, "category": "Politics", "tags": [{"label": "Midterms"}],
    "sportsMarketType": None, "gameStartTime": None, "subject": {"name": "Republican Party"},
}
REAL_EVENT = {"slug": "usse-midterms-2026-11-03", "title": "U.S Senate Midterm Winner", "category": "Politics",
              "tags": [{"label": "Politics"}, {"label": "Midterms"}], "endDate": None, "teams": [], "seriesSlug": None}
BOOK = {
    "marketSlug": "fed-cut-sep", "state": "MARKET_STATE_OPEN",
    "bids": [{"px": {"value": "0.61", "currency": "USD"}, "qty": "500"}, {"px": {"value": "0.60", "currency": "USD"}, "qty": "900"}],
    "offers": [{"px": {"value": "0.64", "currency": "USD"}, "qty": "300"}, {"px": {"value": "0.65", "currency": "USD"}, "qty": "1000"}],
}


def test_market_mapping_and_fees():
    m = market_from_us(ROW)
    assert m.condition_id == "fed-cut-sep" and m.is_binary
    assert m.yes.token_id == "fed-cut-sep|L" and m.no.token_id == "fed-cut-sep|S"
    assert m.fee_known and m.fee_rate == D("0.06") and m.maker_rebate_rate == D("0.0125")
    assert "sports" not in m.tags
    # 100 contracts at 0.50: taker $1.50, maker rebate $0.3125
    assert taker_fee(D("0.5"), D(100), m) == D("1.50")
    assert maker_rebate(D("0.5"), D(100), m) == D("0.3125")
    sport = market_from_us({**ROW, "slug": "nyk-bos", "title": "Knicks vs Celtics", "team": {"id": 1}})
    assert "sports" in sport.tags
    assert market_from_us({**ROW, "status": "MARKET_STATE_HALTED"}).accepting_orders is False


def test_real_venue_row_shape():
    m = market_from_us(REAL_ROW, REAL_EVENT)
    assert m.question == "U.S Senate Midterm Winner: Republican Party" and m.yes.outcome == "Yes"
    assert m.end_date.year == 2027 and m.tick_size == D("0.01") and m.min_order_size == D(5)
    assert m.fee_rate == D("0.06") and m.maker_rebate_rate == D("0.0125")
    assert "politics" in m.tags and "midterms" in m.tags and "sports" not in m.tags
    assert m.accepting_orders
    assert quote_hints(REAL_ROW) == (D("0.61"), D("0.63"))
    # sports by sportsMarketType, zero-fee market gets zero rebate, hidden markets excluded
    assert "sports" in market_from_us({**REAL_ROW, "sportsMarketType": "moneyline"}, REAL_EVENT).tags
    assert market_from_us({**REAL_ROW, "feeCoefficient": 0}, REAL_EVENT).maker_rebate_rate == 0
    assert market_from_us({**REAL_ROW, "hidden": True}, REAL_EVENT).accepting_orders is False


def test_rest_book_unwraps_marketdata():
    long_b, _ = books_from_us("fed-cut-sep", {"marketData": BOOK})
    assert long_b.best_bid == D("0.61") and long_b.best_ask == D("0.64")


class _Public:
    """Fake AsyncPolymarketUS exposing just what select_universe uses."""

    def __init__(self, events, books):
        self._events, self._books = events, books
        self.events = self
        self.markets = self

    async def list(self, params):                       # events.list
        return {"events": self._events if params.get("offset", 0) == 0 else []}

    async def book(self, slug):                          # markets.book
        return {"marketData": self._books[slug]}

    async def close(self):
        return None


async def test_us_universe_prefilters_and_ranks_by_depth():
    from pm.venues.polymarket_us import PolymarketUSVenue
    cfg = Config()
    cfg.universe.max_markets = 2
    cfg.universe.min_liquidity_usd = D(0)
    cfg.universe.max_days_to_resolution = 365      # the fixture market ends 2027-02-01
    deep = {**REAL_ROW, "slug": "deep"}
    thin = {**REAL_ROW, "slug": "thin"}
    wide = {**REAL_ROW, "slug": "wide", "bestBidQuote": {"px": {"value": "0.30"}}, "bestAskQuote": {"px": {"value": "0.70"}}}
    sport = {**REAL_ROW, "slug": "sport", "sportsMarketType": "spread"}
    books = {
        "deep": {"bids": [{"px": {"value": "0.61"}, "qty": "5000"}], "offers": [{"px": {"value": "0.63"}, "qty": "5000"}]},
        "thin": {"bids": [{"px": {"value": "0.61"}, "qty": "10"}], "offers": [{"px": {"value": "0.63"}, "qty": "10"}]},
    }
    v = PolymarketUSVenue.__new__(PolymarketUSVenue)
    v.cfg, v.data, v._exchange = cfg, None, None
    v.public = _Public([{**REAL_EVENT, "markets": [thin, sport, wide, deep]}], books)
    out = await v.select_universe()
    assert [m.condition_id for m in out] == ["deep", "thin"]        # wide (prefilter) and sport (tag) dropped
    assert out[0].liquidity_usd > out[1].liquidity_usd


def test_books_mirror_and_split():
    long_b, short_b = books_from_us("fed-cut-sep", BOOK)
    assert long_b.best_bid == D("0.61") and long_b.best_ask == D("0.64")
    assert short_b.best_bid == D("0.36") and short_b.best_ask == D("0.39")   # 1 - 0.64, 1 - 0.61
    assert short_b.bid_size_at_touch == D(300) and short_b.ask_size_at_touch == D(500)
    assert split_token("fed-cut-sep|S") == ("fed-cut-sep", True)
    assert token_ids("x") == ("x|L", "x|S")


def test_trades_mirror():
    t_long, t_short = trades_from_us("fed-cut-sep", {
        "price": {"value": "0.63"}, "quantity": {"value": "40"}, "tradeTime": "2026-09-08T12:00:00Z",
        "taker": {"side": "ORDER_SIDE_BUY", "intent": "ORDER_INTENT_BUY_LONG"},
    })
    assert t_long.token_id == "fed-cut-sep|L" and t_long.price == D("0.63") and t_long.side is Side.BUY
    assert t_short.token_id == "fed-cut-sep|S" and t_short.price == D("0.37") and t_short.side is Side.SELL
    assert t_long.size == D(40)


def test_order_params_for_all_four_intents():
    p = order_params(Intent("maker", "fed-cut-sep|L", Side.BUY, D("0.60"), D(25)))
    assert p["intent"] == "ORDER_INTENT_BUY_LONG" and p["price"] == {"value": "0.60", "currency": "USD"}
    assert p["quantity"] == 25 and p["tif"] == "TIME_IN_FORCE_GOOD_TILL_CANCEL" and p["participateDontInitiate"]
    assert order_params(Intent("maker", "fed-cut-sep|S", Side.BUY, D("0.37"), D(25)))["intent"] == "ORDER_INTENT_BUY_SHORT"
    assert order_params(Intent("maker", "fed-cut-sep|L", Side.SELL, D("0.70"), D(5)))["intent"] == "ORDER_INTENT_SELL_LONG"
    fak = order_params(Intent("cset", "fed-cut-sep|S", Side.SELL, D("0.40"), D(5), tif=TimeInForce.FAK, post_only=False))
    assert fak["intent"] == "ORDER_INTENT_SELL_SHORT" and fak["tif"] == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"
    assert "participateDontInitiate" not in fak


def test_positions_mapping_signed_net():
    out = positions_from_us({"positions": {
        "a": {"netPosition": "30", "cost": {"value": "18.00"}},
        "b": {"netPosition": "-10", "cost": {"value": "-4.00"}, "marketMetadata": {"slug": "b"}},
        "c": {"netPosition": "0", "cost": {"value": "0"}},
    }})
    assert out == {"a|L": (D(30), D("18.00")), "b|S": (D(10), D("4.00"))}


async def test_paper_maker_fill_pays_rebate_on_us_market():
    m = market_from_us(ROW)
    px = PaperExchange(lambda t: m, D(300))
    long_b, short_b = books_from_us("fed-cut-sep", BOOK)
    px.on_book(long_b)
    o = await px.place(Intent("maker", "fed-cut-sep|L", Side.BUY, D("0.60"), D(100)))
    px.on_book(books_from_us("fed-cut-sep", {**BOOK, "offers": [{"px": {"value": "0.59"}, "qty": "100"}]})[0])
    fills = await px.drain_fills()
    assert fills[0].size == D(100) and fills[0].maker
    assert fills[0].fee == -maker_rebate(D("0.60"), D(100), m)      # negative fee = rebate
    pf = Portfolio(cash=D(300))
    pf.apply_fill(fills[0])
    assert pf.cash > D(300) - D("60.00")                              # rebate landed in cash


def test_maker_join_best_on_tight_us_book():
    m = market_from_us(ROW)
    long_b, short_b = books_from_us("fed-cut-sep", BOOK)       # spread 0.03 on long, fair ≈ 0.625
    params = {"quote_size_shares": 10, "half_spread": 0.02, "min_book_spread": 0.02, "max_book_spread": 0.15,
              "min_depth_shares_at_touch": 50, "price_band": [0.10, 0.90], "max_inventory_shares": 40,
              "sell_inventory": False, "join_best": True, "min_half_spread": 0.01, "rewards_first": False}
    out = Maker(params).on_tick(mk_ctx([m], [long_b, short_b]))
    yes = next(i for i in out if i.token_id.endswith("|L"))
    # target 0.625-0.02=0.605 -> 0.60; best bid 0.61 is better and fair-0.61=0.015 >= 0.01 -> join at 0.61
    assert yes.price == D("0.61")
    assert RiskGate(RiskConfig(), Mode.PAPER).evaluate(out, mk_ctx([m], [long_b, short_b]), True).accepted


def test_config_venue_and_us_secrets(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("venue: polymarket_us\nmode: paper\nvenue_us:\n  ws_max_markets: 4\n")
    (tmp_path / ".env").write_text("PM_US_KEY_ID=abc\nPM_US_SECRET_KEY=c2VjcmV0\n")
    for k in ("PM_US_KEY_ID", "PM_US_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    cfg = Config.load(tmp_path / "config.yaml", tmp_path / ".env")
    assert cfg.venue == "polymarket_us" and cfg.venue_us.ws_max_markets == 4
    assert cfg.secrets.us_key_id == "abc" and cfg.secrets.us_secret_key == "c2VjcmV0"
