"""Replay the recorded tape through the strategies with the paper exchange.

This is not a separate simulator: it is the same PaperExchange, RiskGate and OMS
that run in paper mode, driven by recorded books and trades instead of the
WebSocket. Only the top 5 levels per side were recorded, which is enough for
maker strategies and for small taker legs.
"""

from __future__ import annotations

import asyncio
import bisect
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .config import Config
from .diagnostics import Withdrawals
from .execution.paper import PaperExchange
from .feed import Trade
from .models import ZERO, Book, D, Level, Market, Mode, Side, Token
from .oms import OMS
from .risk import RiskGate
from .state import Context, MarketState, Portfolio
from .store import Store
from .strategy import build_strategies


# Kept here for backwards compatibility; the implementation lives with the tape format.
from .store import market_from_json  # noqa: E402,F401


def run_backtest(cfg: Config, since_hours: float = 24.0, tick_seconds: float = 5.0, db: str | None = None) -> int:
    path = db or cfg.db_file
    if not Path(path).exists():   # Store() would create an empty tape, which then fools `status`
        print(f"No tape at {path}. Run the bot on this venue first, or pass --db <file>.")
        return 1
    print(f"tape: {path}")
    return asyncio.run(_run(cfg, since_hours, tick_seconds, path))


class ReplayClock:
    """The backtest's notion of 'now': the timestamp of the tape event being replayed."""

    def __init__(self, ts: float = 0.0):
        self.ts = ts

    def __call__(self) -> float:
        return self.ts


async def _run(cfg: Config, since_hours: float, tick_seconds: float, path: str) -> int:
    tape = Store(path, readonly=True)
    scratch = Store(":memory:")
    try:
        markets = tape.load_markets()
        if not markets:
            print("no markets recorded; run `pm run --mode read_only` for a while first")
            return 1
        ms = MarketState()
        ms.set_markets(markets)

        tape_end = tape.query("SELECT MAX(ts) FROM books")[0][0]
        if tape_end is None:
            print("no book snapshots in tape")
            return 1
        since = tape_end - since_hours * 3600          # window is relative to the END of the tape, not to today
        books = tape.query("SELECT ts, token_id, top FROM books WHERE ts >= ? ORDER BY ts", (since,))
        trades = tape.query("SELECT ts, token_id, price, size, side FROM trades WHERE ts >= ? ORDER BY ts", (since,))
        if not books:
            print("no book snapshots in window")
            return 1

        # Liveness from the tape: a streamed market only writes rows when its book changes, so a book counts as
        # current while the token's NEXT row comes within LIVE_GAP_S (it was still being watched). A token whose rows
        # stop for longer (feed down, market left the universe) goes stale as it did live.
        rows_by_token: dict[str, list[float]] = {}
        for r in books:
            rows_by_token.setdefault(r[1], []).append(r[0])
        # Streamed books (those with trade prints) write rows only when they change, so a quiet spell is not a
        # dead feed: they stay current up to STREAM_LIVE_GAP_S while the recorder was running (same rule as
        # `pm measure`). REST-polled books write every ~10 s, so for them a 5-minute silence does mean stale.
        streamed = {r[1] for r in trades}
        alive = [(a, b) for a, b in tape._alive_spans() if b >= since]
        clock = ReplayClock(books[0][0])
        withdrawals = Withdrawals()
        pf = Portfolio(cash=cfg.execution.paper_starting_cash_usd)
        pf.register_mirrors(markets)
        px = PaperExchange(ms.market_for, pf.cash, clock=clock)
        oms = OMS(px, cfg.execution, scratch, clock=clock)
        risk = RiskGate(cfg.risk, Mode.PAPER)
        strategies = build_strategies(cfg.strategies, cfg.venue)

        events: list[tuple[float, int, tuple]] = [(r[0], 0, r) for r in books] + [(r[0], 1, r) for r in trades]
        events.sort(key=lambda e: (e[0], -e[1]))     # at equal ts, the print before the book that shows it

        next_tick = events[0][0]
        n_ticks = 0
        for ts, kind, row in events:
            while ts >= next_tick:
                clock.ts = next_tick
                _touch_live_books(ms, rows_by_token, next_tick, streamed, alive)
                await _tick(next_tick, ms, pf, px, oms, risk, strategies, withdrawals)
                n_ticks += 1
                next_tick += tick_seconds
            clock.ts = ts
            if kind == 0:
                b = _book_from_row(row)
                ms.update_book(b)
                px.on_book(b)
            else:
                px.on_trade(Trade(token_id=row[1], price=D(row[2]), size=D(row[3]),
                                  side=Side(row[4]), ts=row[0]))
        clock.ts = next_tick
        await _tick(next_tick, ms, pf, px, oms, risk, strategies, withdrawals)

        eq = pf.equity(ms.books)
        hours = (books[-1][0] - books[0][0]) / 3600
        fills = scratch.query("SELECT strategy, COUNT(*), SUM(CAST(size AS REAL)*CAST(price AS REAL)), SUM(maker) FROM fills GROUP BY strategy")
        n_fills = sum(r[1] for r in fills)
        print(f"\nBacktest over {hours:.1f}h of tape  ({len(books)} book rows, {len(trades)} trades, {n_ticks} ticks)")
        print(f"  start cash ....... {cfg.execution.paper_starting_cash_usd}")
        print(f"  end equity ....... {eq:.2f}  (cash {pf.cash:.2f}, realised {pf.realised_pnl:.2f}, "
              f"unrealised {pf.unrealised(ms.books):.2f}, fees {pf.fees_paid:.2f})")
        print(f"  orders placed .... {oms.placed}  cancelled {oms.cancelled}  rejected {oms.rejected}"
              + (f"  ({oms.placed / n_fills:.0f} orders per fill)" if n_fills else ""))
        for strat, n, notional, makers in fills:
            print(f"  fills[{strat}] .... {n} fills, ${notional:.2f} notional, {makers} maker")
        held = {t: str(p.shares) for t, p in pf.positions.items() if p.shares > ZERO}
        if held:
            print(f"  open inventory ... {held}")
        print(f"  quotes withdrawn . {withdrawals.total}" + (f"  (each costs queue position)" if withdrawals.total else ""))
        for why, k in withdrawals.top(6):
            print(f"      {k:>7}  {why}")
        if withdrawals.by_tag:
            print("    most withdrawn: " + ", ".join(f"{t.split(':', 1)[1][:44]} x{k}" for t, k in withdrawals.by_tag.most_common(3)))
        mk = tape.markouts(fills=scratch.fills_rows())
        print_markouts(mk)
        print("\nPaper fills model queue position (we join behind the displayed size) and assume the adverse case")
        print("when a level vanishes. They are an approximation of what a real order would have received.")
        return 0
    finally:
        tape.close()
        scratch.close()


def print_markouts(mk: dict[str, Any]) -> None:
    if not mk or not mk.get("fills_measured"):
        skipped = (mk or {}).get("fills_unmeasured") or 0
        print("\nMarkouts: no fills measured." + (f" ({skipped} fills had no book snapshot before them.)" if skipped else ""))
        return
    hz = mk["horizons_s"]
    fmt = lambda v, w: f"{v:>{w}.2f}" if v is not None else f"{'n/a':>{w}}"  # noqa: E731
    print(f"\nMarkouts ({mk['units']})")
    print(f"{'strategy/token':40} {'fills':>5} {'shares':>9} {'capture':>8} "
          + " ".join(f"{'drift' + str(h) + 's':>9} {'net' + str(h) + 's':>8} {'cov%':>5}" for h in hz))
    for section, rows in (("by strategy", mk["by_strategy"]), ("worst tokens", mk["worst_tokens"]),
                          ("best tokens", mk["best_tokens"])):
        print(f"  -- {section}")
        for r in rows:
            cells = " ".join(f"{fmt(r[f'drift_{h}s_c'], 9)} {fmt(r[f'net_{h}s_c'], 8)} {r[f'coverage_{h}s_pct']:>5.0f}"
                             for h in hz)
            print(f"  {r['key'][:38]:38} {r['fills']:>5} {float(r['shares']):>9.1f} {r['capture_c']:>8.2f} {cells}")
    if mk.get("fills_unmeasured"):
        print(f"  ({mk['fills_unmeasured']} fills had no book snapshot before them and are not measured)")


LIVE_GAP_S = 300.0
STREAM_LIVE_GAP_S = 3600.0


def _touch_live_books(ms: MarketState, rows_by_token: dict[str, list[float]], ts: float,
                      streamed: set[str] | None = None, alive: list[tuple[float, float]] | None = None) -> None:
    for tok, b in ms.books.items():
        rows = rows_by_token.get(tok)
        if not rows:
            continue
        i = bisect.bisect_right(rows, ts)
        if i == 0 or i >= len(rows):
            continue                                   # before its first row, or after its last: no evidence
        gap = rows[i] - rows[i - 1]
        if gap <= LIVE_GAP_S:
            b.ts = ts
        elif (streamed and tok in streamed and gap <= STREAM_LIVE_GAP_S and alive is not None
              and Store._within_one_span(alive, rows[i - 1], rows[i])):
            b.ts = ts


async def _tick(ts: float, ms, pf, px, oms, risk, strategies, withdrawals: Withdrawals | None = None) -> None:
    now = datetime.fromtimestamp(ts, tz=timezone.utc)
    pf.roll_day(ms.books, now=now)
    for f in await oms.drain_fills():
        pf.apply_fill(f)
    ctx = Context(
        now=now, now_ts=ts,
        markets=ms.markets, books=ms.books, token_to_market=ms.token_to_market,
        portfolio=pf, open_orders=await px.open_orders(), params={},
    )
    intents = []
    for s in strategies:
        intents.extend(s.on_tick(ctx))
    rep = risk.evaluate(intents, ctx, geoblock_ok=True)
    if withdrawals is not None:
        withdrawals.record(ctx.open_orders, rep.accepted, rep.rejected, strategies, ms.market_for)
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
