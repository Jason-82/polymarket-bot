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
    books_from_us, market_from_us, order_params, positions_from_us, split_token, token_ids, trades_from_us,
)
from tests.conftest import D, mk_ctx

ROW = {
    "id": 7, "slug": "fed-cut-sep", "title": "Fed cuts rates in September?", "outcome": "Yes",
    "active": True, "closed": False, "liquidity": 120000.0, "volume": 550000.0, "eventSlug": "fomc-sep",
}
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
    assert market_from_us({**ROW, "state": "MARKET_STATE_HALTED"}).accepting_orders is False


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
