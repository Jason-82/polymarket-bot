"""Replay the recorded tape through the strategies with the paper exchange.

This is not a separate simulator: it is the same PaperExchange, RiskGate and OMS
that run in paper mode, driven by recorded books and trades instead of the
WebSocket. Only the top 5 levels per side were recorded, which is enough for
maker strategies and for small taker legs.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .config import Config
from .execution.paper import PaperExchange
from .feed import Trade
from .models import ZERO, Book, D, Level, Market, Mode, Side, Token
from .oms import OMS
from .risk import RiskGate
from .state import Context, MarketState, Portfolio
from .store import Store
from .strategy import build_strategies


def market_from_json(row: dict[str, Any]) -> Market:
    return Market(
        condition_id=row["condition_id"], question=row.get("question", ""), slug=row.get("slug", ""),
        tokens=[Token(t["token_id"], t["outcome"]) for t in row["tokens"]],
        neg_risk=row.get("neg_risk", False), neg_risk_augmented=row.get("neg_risk_augmented", False),
        event_id=row.get("event_id", ""), event_market_count=row.get("event_market_count", 0),
        tick_size=D(row.get("tick_size", "0.01")), min_order_size=D(row.get("min_order_size", "5")),
        fee_rate=D(row.get("fee_rate", 0)), fee_exponent=D(row.get("fee_exponent", 1)), fee_known=row.get("fee_known", False),
        end_date=datetime.fromisoformat(row["end_date"]) if row.get("end_date") else None,
        liquidity_usd=D(row.get("liquidity_usd", 0)), volume_24h_usd=D(row.get("volume_24h_usd", 0)),
        tags=row.get("tags", []),
    )


def run_backtest(cfg: Config, since_hours: float = 24.0, tick_seconds: float = 5.0) -> int:
    return asyncio.run(_run(cfg, since_hours, tick_seconds))


async def _run(cfg: Config, since_hours: float, tick_seconds: float) -> int:
    tape = Store(cfg.db_path)
    scratch = Store(":memory:")
    try:
        markets = [market_from_json(json.loads(r[0])) for r in tape.query("SELECT json FROM markets")]
        if not markets:
            print("no markets recorded; run `pm run --mode read_only` for a while first")
            return 1
        ms = MarketState()
        ms.set_markets(markets)

        since = time.time() - since_hours * 3600
        books = tape.query("SELECT ts, token_id, top FROM books WHERE ts >= ? ORDER BY ts", (since,))
        trades = tape.query("SELECT ts, token_id, price, size, side FROM trades WHERE ts >= ? ORDER BY ts", (since,))
        if not books:
            print("no book snapshots in window")
            return 1

        pf = Portfolio(cash=cfg.execution.paper_starting_cash_usd)
        px = PaperExchange(ms.market_for, pf.cash)
        oms = OMS(px, cfg.execution, scratch)
        risk = RiskGate(cfg.risk, Mode.PAPER)
        strategies = build_strategies(cfg.strategies, cfg.venue)

        events: list[tuple[float, int, tuple]] = [(r[0], 0, r) for r in books] + [(r[0], 1, r) for r in trades]
        events.sort(key=lambda e: (e[0], e[1]))

        next_tick = events[0][0]
        n_ticks = 0
        for ts, kind, row in events:
            while ts >= next_tick:
                await _tick(next_tick, ms, pf, px, oms, risk, strategies)
                n_ticks += 1
                next_tick += tick_seconds
            if kind == 0:
                b = _book_from_row(row)
                ms.update_book(b)
                px.on_book(b)
            else:
                px.on_trade(Trade(token_id=row[1], price=D(row[2]), size=D(row[3]),
                                  side=Side(row[4]), ts=row[0]))
        await _tick(next_tick, ms, pf, px, oms, risk, strategies)

        eq = pf.equity(ms.books)
        fills = scratch.query("SELECT strategy, COUNT(*), SUM(CAST(size AS REAL)*CAST(price AS REAL)), SUM(maker) FROM fills GROUP BY strategy")
        print(f"\nBacktest over {since_hours:.1f}h  ({len(books)} book rows, {len(trades)} trades, {n_ticks} ticks)")
        print(f"  start cash ....... {cfg.execution.paper_starting_cash_usd}")
        print(f"  end equity ....... {eq:.2f}  (cash {pf.cash:.2f}, realised {pf.realised_pnl:.2f}, "
              f"unrealised {pf.unrealised(ms.books):.2f}, fees {pf.fees_paid:.2f})")
        print(f"  orders placed .... {oms.placed}  cancelled {oms.cancelled}  rejected {oms.rejected}")
        for strat, n, notional, makers in fills:
            print(f"  fills[{strat}] .... {n} fills, ${notional:.2f} notional, {makers} maker")
        held = {t: str(p.shares) for t, p in pf.positions.items() if p.shares > ZERO}
        if held:
            print(f"  open inventory ... {held}")
        print("\nNote: resting fills ignore queue position; treat this as an upper bound.")
        return 0
    finally:
        tape.close()
        scratch.close()


async def _tick(ts: float, ms, pf, px, oms, risk, strategies) -> None:
    pf.roll_day(ms.books)
    for f in await oms.drain_fills():
        pf.apply_fill(f)
    ctx = Context(
        now=datetime.fromtimestamp(ts, tz=timezone.utc), now_ts=ts,
        markets=ms.markets, books=ms.books, token_to_market=ms.token_to_market,
        portfolio=pf, open_orders=await px.open_orders(), params={},
    )
    intents = []
    for s in strategies:
        intents.extend(s.on_tick(ctx))
    rep = risk.evaluate(intents, ctx, geoblock_ok=True)
    await oms.sync(rep.accepted)


def _book_from_row(row: tuple) -> Book:
    ts, token_id, top = row
    d = json.loads(top)
    b = Book(token_id=token_id)
    b.replace(
        bids=[Level(D(p), D(s)) for p, s in d.get("bids", [])],
        asks=[Level(D(p), D(s)) for p, s in d.get("asks", [])],
        ts=ts,
    )
    return b
