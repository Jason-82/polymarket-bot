from __future__ import annotations

import time
from datetime import timedelta
from decimal import Decimal

import pytest

from pm.models import Book, Level, Market, Order, Token, utcnow
from pm.state import Context, MarketState, Portfolio


def D(x) -> Decimal:
    return Decimal(str(x))


def mk_market(cid: str = "0xc1", yes: str = "Y1", no: str = "N1", *, fee_rate="0.04", neg_risk=False,
              event_id: str = "", event_count: int = 0, days: float = 30, tick="0.01", min_size="5",
              augmented=False) -> Market:
    return Market(
        condition_id=cid, question=f"Q {cid}", slug=f"q-{cid}",
        tokens=[Token(yes, "Yes"), Token(no, "No")],
        neg_risk=neg_risk, neg_risk_augmented=augmented, event_id=event_id, event_market_count=event_count,
        tick_size=D(tick), min_order_size=D(min_size),
        fee_rate=D(fee_rate), fee_exponent=Decimal("1"), fee_known=True,
        end_date=utcnow() + timedelta(days=days), liquidity_usd=D(10000), volume_24h_usd=D(5000),
    )


def mk_book(token_id: str, bid: str | None, ask: str | None, bid_sz="100", ask_sz="100", ts: float | None = None,
            extra_bids=(), extra_asks=()) -> Book:
    b = Book(token_id=token_id)
    bids = [Level(D(bid), D(bid_sz))] if bid is not None else []
    asks = [Level(D(ask), D(ask_sz))] if ask is not None else []
    bids += [Level(D(p), D(s)) for p, s in extra_bids]
    asks += [Level(D(p), D(s)) for p, s in extra_asks]
    b.replace(bids, asks, ts=ts or time.time())
    return b


def mk_ctx(markets: list[Market], books: list[Book], pf: Portfolio | None = None,
           open_orders: list[Order] | None = None) -> Context:
    ms = MarketState()
    ms.set_markets(markets)
    for b in books:
        ms.update_book(b)
    pf = pf or Portfolio(cash=D(300))
    return Context(
        now=utcnow(), now_ts=time.time(), markets=ms.markets, books=ms.books,
        token_to_market=ms.token_to_market, portfolio=pf, open_orders=list(open_orders or []), params={},
    )


@pytest.fixture
def market() -> Market:
    return mk_market()
