"""Measure every market on a tape: is there enough trading for a small resting order to ever fill?

One block per book (the mirrored short side of a Polymarket US market is the same book and is skipped):

- observed time. The recorder must have been running (book rows on the tape, from any market, never more than a
  minute apart). A REST-polled book writes a row at least every ~10 s, so a silence of more than MAX_GAP_S in its
  own rows means it left the universe. A STREAMED book (one with trade prints) only writes a row when it changes,
  so its quiet spells count as observed while the recorder ran, up to STREAM_MAX_GAP_S.
- spread: share of observed time at 1, 2 and 3+ ticks, and the median spread in cents (the tick size matters: a
  1-cent spread is 10 ticks on a 0.001-tick market).
- queue: time-weighted median size at the best bid and the best ask.
- flow: contracts per hour hitting the bid (sellers) and lifting the ask (buyers), from
    trades  trade prints. One taker order that sweeps several resting orders prints several trades at once, so
            prints on one side less than BURST_S apart count as one burst, and bursts are what is counted.
            Streaming can cover only part of a tape (the streamed set changes with the universe), so the rate is
            taken over the stretch the bursts cover: first-to-last burst, scaled by (n+1)/(n-1), the unbiased
            estimate for n evenly spread bursts, never less than MIN_FLOW_WINDOW_S (bursts minutes apart say
            nothing about an hourly rate) and never more than the observed time.
            Fewer than MIN_PRINTS bursts: shown, flagged "few", not ranked.
    venue   the venue's traded-volume counter (Polymarket US tapes from 2026-10-18), over the time it was
            recorded. Where streamed markets have both prints and the counter, the counter is calibrated against
            the prints and the ratio is printed; once it is calibrated it gives the total for those markets too,
            since it covers all the observed time and prints cover only the streamed part. The counter has no
            side: the split between sellers and buyers comes from the market's prints (MIN_PRINTS bursts or
            more), otherwise it is taken as even, which is the best case for a maker who needs both sides.
            A counter that goes down is not counted (MAX_COUNTER_DROPS or more drops: not used at all), unless
            it fell below half its last value, which reads as a restart from zero.
    book    no trade data: only size that left the best price between snapshots. That counts cancels and misses
            trades refilled between polls, so it is neither an upper nor a lower bound. Not ranked.
- fill hours for an order of `size`: joining the back of the queue needs queue + size to trade on that side;
  pricing one tick better needs only `size`, using only the flow seen WHILE the spread was 2+ ticks wide. A
  maker needs both sides to fill, so markets are ranked by the slower side (the round trip).
"""

from __future__ import annotations

import bisect
import csv
import math
import textwrap
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

from .models import Market
from .store import Store

MAX_GAP_S = 300.0          # REST-polled books: a longer silence means the market was not being watched
STREAM_MAX_GAP_S = 3600.0  # streamed books: a quiet hour still counts; longer probably means it left the universe
MIN_PRINTS = 5             # fewer bursts of prints than this: too few to rely on
BURST_S = 1.0              # prints on one side closer than this are one taker order sweeping several makers
MIN_FLOW_WINDOW_S = 3600.0 # a print-based rate is never taken over less than an hour (or the observed time)
MIN_COUNTER_S = 600.0      # at least 10 minutes of volume-counter coverage
MIN_IMPROVE_SHARE = 0.05   # pricing inside the spread must be possible at least 5% of the time to be reported
MAX_COUNTER_DROPS = 3      # a counter that went down this often is not a running total: do not use it
WIDTH = 79


@dataclass
class MarketMeasure:
    token_id: str
    label: str
    slug: str
    tick: Decimal
    sports: bool = False
    hours: float = 0.0
    coverage: float = 0.0          # observed hours / hours the recorder ran
    rows: int = 0
    spread_1: float = 0.0
    spread_2: float = 0.0
    spread_3plus: float = 0.0
    median_spread_c: float = 0.0
    bid_depth: float = 0.0
    ask_depth: float = 0.0
    sell_flow_h: float = 0.0       # contracts per hour hitting the bid
    buy_flow_h: float = 0.0        # contracts per hour lifting the ask
    wide_sell_flow_h: float = 0.0  # the same, only while the spread was 2+ ticks
    wide_buy_flow_h: float = 0.0
    flow_source: str = "book"      # trades | venue | few | book
    side_source: str = ""          # venue rows: "prints" (measured split) or "even" (assumed: the best case)
    sell_share: float = 0.5        # share of printed volume that hit the bid
    prints: int = 0
    bursts: int = 0                # prints grouped into taker orders (see BURST_S)
    flow_hours: float = 0.0        # hours the flow figure is based on
    mid_moves_h: float = 0.0
    fill_join_bid_h: float = math.inf
    fill_join_ask_h: float = math.inf
    fill_improve_bid_h: float = math.inf
    fill_improve_ask_h: float = math.inf
    # raw material for the tape-level counter calibration
    counter_total: float = 0.0
    counter_secs: float = 0.0
    counter_wide: float = 0.0
    counter_wide_secs: float = 0.0
    counter_drops: int = 0         # times the counter went down without looking like a restart
    calib_counter: float = 0.0
    calib_prints: float = 0.0
    wide_secs: float = 0.0

    @property
    def can_improve(self) -> float:
        return self.spread_2 + self.spread_3plus

    @property
    def ranked(self) -> bool:
        return self.flow_source in ("trades", "venue")

    @property
    def join_round_trip_h(self) -> float:
        return max(self.fill_join_bid_h, self.fill_join_ask_h)

    @property
    def improve_round_trip_h(self) -> float:
        return max(self.fill_improve_bid_h, self.fill_improve_ask_h)


def _d(x: Any) -> Optional[Decimal]:
    if x is None or x == "":
        return None
    try:
        return Decimal(str(x))
    except Exception:
        return None


def _weighted_median(pairs: list[tuple[float, float]]) -> float:
    pairs = sorted((v, w) for v, w in pairs if w > 0)
    if not pairs:
        return 0.0
    half = sum(w for _, w in pairs) / 2
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= half:
            return v
    return pairs[-1][0]


def _outcome(m: Market, token_id: str) -> str:
    for tk in m.tokens:
        if tk.token_id == token_id:
            return tk.outcome
    return "?"


def measure_token(store: Store, market: Market, token_id: str, since_ts: float, size: float,
                  spans: list[tuple[float, float]], has_counter_col: bool = True) -> MarketMeasure:
    tick = market.tick_size if market.tick_size > 0 else Decimal("0.01")
    label = market.question if market.synthetic_no else f"{market.question} [{_outcome(market, token_id)}]"
    slug = token_id[:-2] if market.synthetic_no and token_id.endswith("|L") else token_id
    out = MarketMeasure(token_id=token_id, label=label, slug=slug, tick=tick, sports="sports" in market.tags)
    tt_col = "traded_total" if has_counter_col else "NULL"
    rows = [(r[0], _d(r[1]), _d(r[2]), _d(r[3]) or Decimal(0), _d(r[4]) or Decimal(0), _d(r[5]))
            for r in store.query(f"SELECT ts, best_bid, best_ask, bid_sz, ask_sz, {tt_col} FROM books "
                                 "WHERE token_id=? AND ts >= ? ORDER BY ts", (token_id, since_ts))]
    out.rows = len(rows)
    prints = store.query("SELECT ts, side, size FROM trades WHERE token_id=? AND ts >= ? ORDER BY ts",
                         (token_id, since_ts))
    streamed = bool(prints)
    gap_limit = STREAM_MAX_GAP_S if streamed else MAX_GAP_S

    intervals: list[tuple[float, float, int]] = []      # observed, quotable: (start, end, spread ticks)
    spread_w = {1: 0.0, 2: 0.0, 3: 0.0}
    spread_c: list[tuple[float, float]] = []
    bid_depths: list[tuple[float, float]] = []
    ask_depths: list[tuple[float, float]] = []
    moves = 0
    dep_bid = dep_ask = 0.0
    counter_pts: list[tuple[float, float]] = []         # (ts, cumulative counted volume) along observed stretches
    last_tt: Optional[tuple[float, Decimal]] = None
    counted = 0.0
    for cur, nxt in zip(rows, rows[1:]):
        gap = nxt[0] - cur[0]
        observed = 0 < gap <= gap_limit and Store._within_one_span(spans, cur[0], nxt[0])
        if not observed:
            last_tt = None                                # a break: never diff the counter across it
            continue
        cb, ca, cbs, cas, ctt = cur[1:]
        nb, na, nbs, nas, ntt = nxt[1:]
        # volume counter: diff against the last value seen in this unbroken stretch (holes are skipped over)
        if last_tt is None and ctt is not None:
            last_tt = (cur[0], ctt)
        if ntt is not None and last_tt is not None:
            delta = ntt - last_tt[1]
            if delta < 0:
                if ntt <= last_tt[1] / 2:
                    delta = ntt                           # the counter restarted from zero
                else:
                    delta = Decimal(0)                    # a running total never goes down: count it, skip it
                    out.counter_drops += 1
            out.counter_total += float(delta)
            out.counter_secs += nxt[0] - last_tt[0]
            counted += float(delta)
            counter_pts.append((nxt[0], counted))
            if (last_tt[0] == cur[0] and cb is not None and ca is not None and ca > cb
                    and (ca - cb) / tick >= Decimal("1.5")):
                out.counter_wide += float(delta)
                out.counter_wide_secs += gap
            last_tt = (nxt[0], ntt)
        elif ntt is not None:
            last_tt = (nxt[0], ntt)
        if cb is None or ca is None or ca <= cb:
            continue                                      # one-sided, empty, locked or crossed: not quotable
        ticks = max(1, int(((ca - cb) / tick).to_integral_value(rounding=ROUND_HALF_UP)))
        intervals.append((cur[0], nxt[0], ticks))
        spread_w[min(ticks, 3)] += gap
        if ticks >= 2:
            out.wide_secs += gap
        spread_c.append((float((ca - cb) * 100), gap))
        bid_depths.append((float(cbs), gap))
        ask_depths.append((float(cas), gap))
        if nb is not None and na is not None and na > nb and (nb + na) != (cb + ca):
            moves += 1
        if nb is not None:
            dep_bid += float(max(Decimal(0), cbs - nbs) if nb == cb else (cbs if nb < cb else Decimal(0)))
        if na is not None:
            dep_ask += float(max(Decimal(0), cas - nas) if na == ca else (cas if na > ca else Decimal(0)))

    observed_s = sum(b - a for a, b, _ in intervals)
    out.hours = observed_s / 3600.0
    if observed_s <= 0:
        return out
    out.spread_1, out.spread_2, out.spread_3plus = (spread_w[k] / observed_s for k in (1, 2, 3))
    out.median_spread_c = _weighted_median(spread_c)
    out.bid_depth = _weighted_median(bid_depths)
    out.ask_depth = _weighted_median(ask_depths)
    out.mid_moves_h = moves / out.hours

    # prints inside observed time, each tagged with the spread at that moment
    starts = [a for a, _, _ in intervals]
    kept: list[tuple[float, str, float, int]] = []
    for ts, side, sz in prints:
        i = bisect.bisect_right(starts, ts) - 1
        if i >= 0 and ts <= intervals[i][1]:
            kept.append((ts, side, float(sz), intervals[i][2]))
    out.prints = len(kept)
    if kept:
        bursts: list[float] = []
        last_on: dict[str, float] = {}
        for ts, side, _, _ in kept:
            if ts - last_on.get(side, -math.inf) >= BURST_S:
                bursts.append(ts)
            last_on[side] = ts
        n = out.bursts = len(bursts)
        span = bursts[-1] - bursts[0]
        window = min(observed_s, max(MIN_FLOW_WINDOW_S, span * (n + 1) / (n - 1))) if n >= 2 else observed_s
        frac = window / observed_s
        out.flow_hours = window / 3600.0
        out.sell_flow_h = sum(z for _, s, z, _ in kept if s == "SELL") / out.flow_hours
        out.buy_flow_h = sum(z for _, s, z, _ in kept if s == "BUY") / out.flow_hours
        wide_h = out.wide_secs * frac / 3600.0
        if wide_h > 0:
            out.wide_sell_flow_h = sum(z for _, s, z, t in kept if s == "SELL" and t >= 2) / wide_h
            out.wide_buy_flow_h = sum(z for _, s, z, t in kept if s == "BUY" and t >= 2) / wide_h
        sold = sum(z for _, s, z, _ in kept if s == "SELL")
        total = sum(z for _, _, z, _ in kept)
        out.sell_share = sold / total if total > 0 else 0.5
        out.flow_source = "trades" if n >= MIN_PRINTS else "few"
        # calibration material: counter volume over the stretch the prints cover
        if counter_pts and n >= MIN_PRINTS and out.counter_drops < MAX_COUNTER_DROPS:
            lo, hi = kept[0][0], kept[-1][0]
            inside = [c for t, c in counter_pts if lo <= t <= hi]
            if len(inside) >= 2:
                out.calib_counter = inside[-1] - inside[0]
                out.calib_prints = sum(z for t, _, z, _ in kept if lo <= t <= hi)
    if out.flow_source == "book":
        out.flow_hours = out.hours
        out.sell_flow_h = dep_bid / out.hours
        out.buy_flow_h = dep_ask / out.hours
    return out


def _uses_counter(r: MarketMeasure, counter_ratio: Optional[float]) -> bool:
    """The counter gives the total unless it is too short or not a running total; on a market with enough prints
    only once it has been calibrated (until then the prints are the better-known number)."""
    if r.counter_secs < MIN_COUNTER_S or r.counter_drops >= MAX_COUNTER_DROPS:
        return False
    return r.flow_source != "trades" or counter_ratio is not None


def _finalize(r: MarketMeasure, size: float, counter_ratio: Optional[float]) -> None:
    if _uses_counter(r, counter_ratio):
        ratio = counter_ratio or 1.0
        measured = r.bursts >= MIN_PRINTS
        share = r.sell_share if measured else 0.5
        r.flow_source, r.side_source = "venue", ("prints" if measured else "even")
        r.flow_hours = r.counter_secs / 3600.0
        total = r.counter_total / ratio / r.flow_hours
        r.sell_flow_h, r.buy_flow_h = total * share, total * (1 - share)
        r.wide_sell_flow_h = r.wide_buy_flow_h = 0.0
        if r.counter_wide_secs > 0:
            wide = r.counter_wide / ratio / (r.counter_wide_secs / 3600)
            r.wide_sell_flow_h, r.wide_buy_flow_h = wide * share, wide * (1 - share)

    def hours(queue: float, flow: float) -> float:
        return (queue + size) / flow if flow > 0 else math.inf

    if r.flow_source in ("trades", "venue", "few"):
        r.fill_join_bid_h = hours(r.bid_depth, r.sell_flow_h)
        r.fill_join_ask_h = hours(r.ask_depth, r.buy_flow_h)
        if r.can_improve >= MIN_IMPROVE_SHARE:
            r.fill_improve_bid_h = hours(0.0, r.wide_sell_flow_h)
            r.fill_improve_ask_h = hours(0.0, r.wide_buy_flow_h)


def measure_tape(store: Store, since_hours: float = 0.0, size: float = 20.0) -> tuple[list[MarketMeasure], dict]:
    end = store.query("SELECT MAX(ts) FROM books")[0][0]
    start = store.query("SELECT MIN(ts) FROM books")[0][0]
    if end is None:
        return [], {}
    since_ts = max(start, end - since_hours * 3600) if since_hours > 0 else start
    spans = [(max(a, since_ts), b) for a, b in store._alive_spans() if b >= since_ts]
    up_h = sum(b - a for a, b in spans) / 3600.0
    has_counter_col = "traded_total" in store.book_columns()
    rows: list[MarketMeasure] = []
    dropped = 0
    for m in store.load_markets():
        tokens = [m.yes.token_id] if m.synthetic_no else [t.token_id for t in m.tokens]
        for tok in tokens:
            mm = measure_token(store, m, tok, since_ts, size, spans, has_counter_col)
            if mm.hours > 0:
                mm.coverage = mm.hours / up_h if up_h > 0 else 0.0
                rows.append(mm)
            elif mm.rows:
                dropped += 1
    calib = [r for r in rows if r.calib_prints > 0 and r.calib_counter > 0]
    ratio = None
    if len(calib) >= 2:
        ratio = sum(r.calib_counter for r in calib) / sum(r.calib_prints for r in calib)
    for r in rows:
        _finalize(r, size, ratio)
    rank = {"trades": 0, "venue": 0, "few": 1, "book": 2}
    rows.sort(key=lambda r: (rank[r.flow_source], r.join_round_trip_h, r.improve_round_trip_h))
    info = {"tape_hours": (end - since_ts) / 3600.0, "up_hours": up_h, "size": size, "dropped": dropped,
            "counter_ratio": ratio, "counter_markets": len(calib),
            "with_counter": sum(1 for r in rows if r.counter_secs >= MIN_COUNTER_S),
            "with_prints": sum(1 for r in rows if r.prints > 0),
            "drop_markets": sum(1 for r in rows if r.counter_drops > 0),
            "drops": sum(r.counter_drops for r in rows),
            "unusable_counters": sum(1 for r in rows if r.counter_drops >= MAX_COUNTER_DROPS)}
    return rows, info


# ---------------------------------------------------------------------------------------------- report

def _h(x: float) -> str:
    if math.isinf(x):
        return "never"
    if x < 1:
        return f"{max(1, round(x * 60))}m"
    if x >= 72:
        return f"{x / 24:.0f}d"
    return f"{x:.1f}h" if x < 10 else f"{x:.0f}h"


def _fmt_n(x: float) -> str:
    return f"{x:,.0f}" if x >= 10 else f"{x:.1f}"


def _wrap(text: str, indent: str = "") -> str:
    return textwrap.fill(text, width=WIDTH, initial_indent=indent, subsequent_indent=indent)


def print_report(rows: list[MarketMeasure], info: dict, top: int = 30) -> None:
    if not rows:
        print("No markets with quotable books in this tape.")
        return
    size = info["size"]
    calibrated = bool(info.get("counter_ratio"))
    print()
    print(_wrap(f"Market measurement: {info['tape_hours']:.1f}h of tape (recorder running {info['up_hours']:.1f}h), "
                f"for a {size:g}-contract order. Ranked by the slower side when joining the queue: a maker needs "
                f"both sides to fill."))
    n_sports = sum(1 for r in rows if r.sports)
    print(_wrap(f"Coverage: {len(rows)} markets observed ({n_sports} sports); {info.get('with_counter', 0)} with the "
                f"venue's volume counter, {info.get('with_prints', 0)} with trade prints."))
    for i, r in enumerate(rows[:top], 1):
        print()
        print(f"{i:>2}. {r.label[:WIDTH - 4]}")
        print(f"    {r.slug[:40]}  tick {r.tick}  observed {r.hours:.1f}h ({r.coverage:.0%})"
              f"{'  sports' if r.sports else ''}")
        if r.flow_source == "book":
            print(f"    fill time: no trade data (book churn {_fmt_n(r.sell_flow_h)}/{_fmt_n(r.buy_flow_h)} "
                  f"per h is not trades)")
        else:
            print(f"    fill time  join bid/ask {_h(r.fill_join_bid_h)}/{_h(r.fill_join_ask_h)}   "
                  f"one tick better {_h(r.fill_improve_bid_h)}/{_h(r.fill_improve_ask_h)}")
            n_prints = f"{r.prints} print{'s' if r.prints != 1 else ''}"
            n_bursts = "" if r.bursts == r.prints else f" in {r.bursts} burst{'s' if r.bursts != 1 else ''}"
            sides = f"sides from {n_prints}" if r.side_source == "prints" else "sides assumed even"
            src = {"trades": f"{n_prints}{n_bursts} over {r.flow_hours:.1f}h",
                   "few": f"only {n_prints}{n_bursts}: too few to rely on",
                   "venue": f"counter {r.flow_hours:.1f}h, {sides}"
                            + ("" if calibrated else " (uncal.)")}[r.flow_source]
            print(f"    flow/h sell/buy {_fmt_n(r.sell_flow_h)}/{_fmt_n(r.buy_flow_h)}  from {src}"[:WIDTH])
        print(f"    queue bid/ask {_fmt_n(r.bid_depth)}/{_fmt_n(r.ask_depth)}  spread 1/2/3+t "
              f"{r.spread_1:.0%}/{r.spread_2:.0%}/{r.spread_3plus:.0%}  median {r.median_spread_c:.1f}c"[:WIDTH])
    if len(rows) > top:
        print(f"\n... {len(rows) - top} more (use --top N, or --csv to get every row)")

    ranked = [r for r in rows if r.ranked]
    print()
    if ranked:
        def count(within: float, attr: str) -> str:
            hit = [r for r in ranked if getattr(r, attr) <= within]
            sp = sum(1 for r in hit if r.sports)
            return f"{len(hit)}" + (f" ({sp} sports)" if n_sports else "")

        n_sp = sum(1 for r in ranked if r.sports)
        print(_wrap(f"Markets with measured flow: {len(ranked)}" + (f" ({n_sp} sports)" if n_sports else "") +
                    f". A {size:g}-lot fills on both sides when joining the queue within 1h in "
                    f"{count(1, 'join_round_trip_h')} and within 24h in {count(24, 'join_round_trip_h')}; when "
                    f"pricing one tick better (only counting time when the spread allows it) within 1h in "
                    f"{count(1, 'improve_round_trip_h')} and within 24h in {count(24, 'improve_round_trip_h')}."))
        even = sum(1 for r in ranked if r.side_source == "even")
        if even:
            print(_wrap(f"{even} of them {'has' if even == 1 else 'have'} no trade prints to tell buyers from "
                        f"sellers, so trading there is split evenly: the best case for a maker, whose real fill "
                        f"times are longer when trading is one-sided."))
    else:
        print(_wrap("No market has measured trading flow on this tape (no trade prints, no venue volume counter)."))
    few = sum(1 for r in rows if r.flow_source == "few")
    book = sum(1 for r in rows if r.flow_source == "book")
    if few:
        print(_wrap(f"{few} markets have fewer than {MIN_PRINTS} bursts of trade prints (prints on one side less "
                    f"than {BURST_S:g}s apart are one taker order): shown, not counted above."))
    if book:
        print(_wrap(f"{book} markets have no trade data at all: their book churn is not trading, so they are not "
                    f"ranked. Tapes recorded from 2026-10-18 carry the venue's volume counter for every market."))
    if calibrated:
        print(_wrap(f"Venue volume counter calibrated against trade prints on {info['counter_markets']} streamed "
                    f"markets: counter = {info['counter_ratio']:.2f} x prints."))
    elif info.get("with_counter"):
        print(_wrap("The venue volume counter is not calibrated yet: that needs 2 streamed markets with both the "
                    f"counter and {MIN_PRINTS}+ bursts of prints. Until then 1 on the counter is taken as 1 "
                    "contract (marked uncal.)."))
    if info.get("drops"):
        print(_wrap(f"The venue volume counter went down {info['drops']} times on {info['drop_markets']} markets; "
                    f"those drops are not counted, and {info.get('unusable_counters', 0)} markets where it went down "
                    f"{MAX_COUNTER_DROPS}+ times are measured without it. Frequent drops would mean it is not a "
                    f"running total."))
    if info.get("dropped"):
        print(_wrap(f"{info['dropped']} markets were never quotable while observed (one-sided or crossed) and are "
                    f"not shown."))


def write_csv(rows: list[MarketMeasure], path: str) -> None:
    cols = ["label", "slug", "token_id", "tick", "sports", "hours", "coverage", "flow_source", "side_source",
            "sell_share", "counter_drops", "prints", "bursts", "flow_hours",
            "sell_flow_h", "buy_flow_h", "bid_depth", "ask_depth", "spread_1", "spread_2", "spread_3plus",
            "median_spread_c", "can_improve", "wide_sell_flow_h", "wide_buy_flow_h", "fill_join_bid_h",
            "fill_join_ask_h", "join_round_trip_h", "fill_improve_bid_h", "fill_improve_ask_h",
            "improve_round_trip_h", "mid_moves_h", "rows"]
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:      # BOM: Excel on Windows reads UTF-8
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            vals = []
            for c in cols:
                v = getattr(r, c)
                vals.append("" if isinstance(v, float) and math.isinf(v) else v)
            w.writerow(vals)
