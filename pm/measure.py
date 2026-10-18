"""Measure every market on a tape: is there enough trading for a small resting order to ever fill?

For each market (one row per book; the mirrored short book of a Polymarket US market is the same book and is
skipped) the tape gives:

- spread, in ticks, time-weighted: how often there is room to price one tick better than the best quote;
- queue depth: the time-weighted median size waiting at the best bid and at the best ask;
- flow: contracts traded per hour against each side (sellers hitting bids, buyers lifting asks), from the best
  source the tape has:
    trades   the venue's trade prints (streamed markets only)
    venue    the venue's running traded-volume counter (Polymarket US tapes from 2026-10-18 on), split evenly
             between the two sides because the counter has no side
    <=book   size that disappeared from the best price between snapshots: trades AND cancellations, so an
             UPPER bound on flow; the true flow can be far lower
- fill time: hours until an order of `size` would fill, if it joins the back of the queue (queue + size must
  trade) or, when the spread allows, prices one tick better (only `size` must trade). Lower is better.

Periods where the tape has no row for a market for more than MAX_GAP_S are not counted as observed time.
"""

from __future__ import annotations

import bisect
import csv
import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from .models import Market
from .store import Store

MAX_GAP_S = 300.0      # same liveness rule as the backtest: a longer silence means the market was not watched


@dataclass
class MarketMeasure:
    token_id: str
    label: str
    tick: Decimal
    hours: float = 0.0
    rows: int = 0
    spread_1: float = 0.0          # share of observed time with a 1-tick spread
    spread_2: float = 0.0
    spread_3plus: float = 0.0
    bid_depth: float = 0.0         # time-weighted median size at the best bid
    ask_depth: float = 0.0
    sell_flow_h: float = 0.0       # contracts per hour hitting the bid
    buy_flow_h: float = 0.0        # contracts per hour lifting the ask
    flow_source: str = "none"
    mid_moves_h: float = 0.0
    trades: int = 0
    fill_join_bid_h: float = math.inf
    fill_join_ask_h: float = math.inf
    fill_improve_bid_h: float = math.inf
    fill_improve_ask_h: float = math.inf

    @property
    def can_improve(self) -> float:
        """Share of observed time with room to price inside the spread (2+ ticks)."""
        return self.spread_2 + self.spread_3plus

    @property
    def best_join_h(self) -> float:
        return min(self.fill_join_bid_h, self.fill_join_ask_h)

    @property
    def best_improve_h(self) -> float:
        return min(self.fill_improve_bid_h, self.fill_improve_ask_h)


def _weighted_median(pairs: list[tuple[float, float]]) -> float:
    pairs = [(v, w) for v, w in pairs if w > 0]
    if not pairs:
        return 0.0
    pairs.sort()
    half = sum(w for _, w in pairs) / 2
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= half:
            return v
    return pairs[-1][0]


def _f(x: Any) -> Optional[float]:
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def measure_token(store: Store, market: Market, token_id: str, since_ts: float, size: float) -> MarketMeasure:
    tick = market.tick_size if market.tick_size > 0 else Decimal("0.01")
    label = market.question if market.synthetic_no else f"{market.question} [{_outcome(market, token_id)}]"
    out = MarketMeasure(token_id=token_id, label=label, tick=tick)
    rows = store.query(
        "SELECT ts, best_bid, best_ask, bid_sz, ask_sz, traded_total FROM books WHERE token_id=? AND ts >= ? "
        "ORDER BY ts", (token_id, since_ts))
    out.rows = len(rows)
    t = float(tick)
    observed = 0.0
    spread_w = {1: 0.0, 2: 0.0, 3: 0.0}
    bid_depths: list[tuple[float, float]] = []
    ask_depths: list[tuple[float, float]] = []
    moves = 0
    dep_bid = dep_ask = 0.0
    counter = 0.0
    counter_seen = 0
    intervals: list[tuple[float, float]] = []      # observed, quotable time
    for cur, nxt in zip(rows, rows[1:]):
        gap = nxt[0] - cur[0]
        if gap <= 0 or gap > MAX_GAP_S:
            continue
        cb, ca, cbs, cas, ctt = _f(cur[1]), _f(cur[2]), _f(cur[3]) or 0.0, _f(cur[4]) or 0.0, _f(cur[5])
        nb, na, nbs, nas, ntt = _f(nxt[1]), _f(nxt[2]), _f(nxt[3]) or 0.0, _f(nxt[4]) or 0.0, _f(nxt[5])
        if cb is None or ca is None or ca <= cb:
            continue                                   # one-sided, empty, locked or crossed: not quotable
        observed += gap
        intervals.append((cur[0], nxt[0]))
        ticks = max(1, round((ca - cb) / t))
        spread_w[min(ticks, 3)] += gap
        bid_depths.append((cbs, gap))
        ask_depths.append((cas, gap))
        if nb is not None and na is not None and (nb + na) != (cb + ca):
            moves += 1
        if nb is not None:                             # size that left the best bid: traded or cancelled
            dep_bid += max(0.0, cbs - nbs) if nb == cb else (cbs if nb < cb else 0.0)
        if na is not None:
            dep_ask += max(0.0, cas - nas) if na == ca else (cas if na > ca else 0.0)
        if ctt is not None and ntt is not None:
            counter_seen += 1
            if ntt >= ctt:                             # a drop is the counter resetting, not negative volume
                counter += ntt - ctt
    out.hours = observed / 3600.0
    if observed <= 0:
        return out
    out.spread_1, out.spread_2, out.spread_3plus = (spread_w[k] / observed for k in (1, 2, 3))
    out.bid_depth = _weighted_median(bid_depths)
    out.ask_depth = _weighted_median(ask_depths)
    out.mid_moves_h = moves / out.hours

    starts = [a for a, _ in intervals]
    trades = []
    for ts, side, sz in store.query("SELECT ts, side, size FROM trades WHERE token_id=? AND ts >= ? ORDER BY ts",
                                    (token_id, since_ts)):
        i = bisect.bisect_right(starts, ts) - 1        # only prints inside observed time count against it
        if i >= 0 and ts <= intervals[i][1]:
            trades.append((side, sz))
    out.trades = len(trades)
    if trades:
        out.flow_source = "trades"
        out.sell_flow_h = sum(float(s) for side, s in trades if side == "SELL") / out.hours
        out.buy_flow_h = sum(float(s) for side, s in trades if side == "BUY") / out.hours
    elif counter_seen:
        out.flow_source = "venue"
        out.sell_flow_h = out.buy_flow_h = counter / 2 / out.hours
    else:
        out.flow_source = "<=book"
        out.sell_flow_h = dep_bid / out.hours
        out.buy_flow_h = dep_ask / out.hours

    def hours(queue: float, flow: float) -> float:
        return (queue + size) / flow if flow > 0 else math.inf

    out.fill_join_bid_h = hours(out.bid_depth, out.sell_flow_h)
    out.fill_join_ask_h = hours(out.ask_depth, out.buy_flow_h)
    if out.can_improve > 0:
        out.fill_improve_bid_h = hours(0.0, out.sell_flow_h)
        out.fill_improve_ask_h = hours(0.0, out.buy_flow_h)
    return out


def _outcome(m: Market, token_id: str) -> str:
    for tk in m.tokens:
        if tk.token_id == token_id:
            return tk.outcome
    return "?"


def measure_tape(store: Store, since_hours: float = 0.0, size: float = 20.0) -> tuple[list[MarketMeasure], dict]:
    end = store.query("SELECT MAX(ts) FROM books")[0][0]
    start = store.query("SELECT MIN(ts) FROM books")[0][0]
    if end is None:
        return [], {}
    since_ts = end - since_hours * 3600 if since_hours > 0 else start
    out: list[MarketMeasure] = []
    for m in store.load_markets():
        tokens = [m.yes.token_id] if m.synthetic_no else [t.token_id for t in m.tokens]
        for tok in tokens:
            mm = measure_token(store, m, tok, since_ts, size)
            if mm.hours > 0:
                out.append(mm)
    out.sort(key=lambda r: (r.flow_source == "<=book", r.best_join_h, r.best_improve_h))
    return out, {"tape_hours": (end - since_ts) / 3600.0, "size": size}


def _h(x: float, lower_bound: bool = False) -> str:
    if math.isinf(x):
        return "never"
    v = f"{x / 24:.0f}d" if x >= 1000 else (f"{x:.1f}" if x < 10 else f"{x:.0f}")
    return (">=" + v) if lower_bound else v


def print_report(rows: list[MarketMeasure], info: dict, top: int = 30) -> None:
    if not rows:
        print("No markets with quotable books in this tape.")
        return
    size = info["size"]
    print(f"\nMarket measurement over {info['tape_hours']:.1f}h of tape, for a {size:g}-contract order "
          f"({len(rows)} books; measured flow first, best first)")
    print(f"{'market':44} {'hrs':>5} {'spread 1t/2t/3t+ %':>18} {'depth bid/ask':>15} {'flow/h sell/buy':>16} "
          f"{'src':>6} {'fill h: join':>12} {'improve':>8} {'moves/h':>7}")
    for r in rows[:top]:
        lb = r.flow_source == "<=book"
        spread = f"{r.spread_1 * 100:.0f}/{r.spread_2 * 100:.0f}/{r.spread_3plus * 100:.0f}"
        depth = f"{r.bid_depth:,.0f}/{r.ask_depth:,.0f}"
        flow = f"{r.sell_flow_h:,.0f}/{r.buy_flow_h:,.0f}"
        print(f"{r.label[:44]:44} {r.hours:>5.1f} {spread:>18} {depth:>15} {flow:>16} {r.flow_source:>6} "
              f"{_h(r.best_join_h, lb):>12} {_h(r.best_improve_h, lb):>8} {r.mid_moves_h:>7.1f}")
    if len(rows) > top:
        print(f"... {len(rows) - top} more (use --top or --csv to see all)")
    measured = [r for r in rows if r.flow_source in ("trades", "venue")]
    book_only = [r for r in rows if r.flow_source == "<=book"]
    print()
    if measured:
        fast = [r for r in measured if r.best_join_h <= 24]
        print(f"Measured flow ({len(measured)} books): {len(fast)} would fill a {size:g}-lot joining the queue within "
              f"24h; {sum(1 for r in measured if r.can_improve >= 0.2)} have room to improve the price at least 20% "
              f"of the time.")
    if book_only:
        slow = [r for r in book_only if r.best_join_h > 24]
        print(f"Book-only estimate ({len(book_only)} books, no trade data): fill times are lower bounds, because "
              f"size that left the book counts as traded even when it was cancelled. {len(slow)} of them take "
              f"more than 24h even so.")
    none = [r for r in rows if r.flow_source == "none"]
    if none:
        print(f"{len(none)} books had no flow information at all.")


def write_csv(rows: list[MarketMeasure], path: str) -> None:
    cols = ["label", "token_id", "tick", "hours", "rows", "spread_1", "spread_2", "spread_3plus", "bid_depth",
            "ask_depth", "sell_flow_h", "buy_flow_h", "flow_source", "trades", "mid_moves_h", "fill_join_bid_h",
            "fill_join_ask_h", "fill_improve_bid_h", "fill_improve_ask_h"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            w.writerow([getattr(r, c) for c in cols])
