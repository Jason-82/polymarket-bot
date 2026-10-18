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
            the prints (each run of prints against the counter just before and just after it) and the ratio is
            printed; once it is calibrated it gives the total for those markets too, since it covers all the
            observed time and prints cover only the streamed part. The counter has no side: the split between
            sellers and buyers comes from the market's prints (MIN_PRINTS bursts or more, pulled toward even
            when there are few), otherwise it is assumed even, which is not measured: the real split can make
            fill times shorter or longer. A counter that moved fewer than MIN_PRINTS times is "few", like a few
            prints. A reading below the highest value so far never lowers it (a drop is not counted; with
            MAX_COUNTER_DROPS or more the counter is not used); a restart from zero is accepted only when the
            next different reading rises from it; a stretch's first reading that the next one more than doubles
            within 2 x CALIB_LAG_S is taken as a bad reading, not as volume.
    book    no trade data: only size that left the best price between snapshots. That counts cancels and misses
            trades refilled between polls, so it is neither an upper nor a lower bound. Not ranked.
- fill hours for an order of `size`: joining the back of the queue needs queue + size to trade on that side;
  pricing one tick better needs only `size`, using only the flow seen WHILE the spread was 2+ ticks wide (from
  prints where there are enough; from the counter only for volume between two readings while every row was
  2+ ticks, a lower bound, so those times are at most what is shown). A maker needs both sides to fill, so
  markets are ranked by the slower side (the round trip).
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
CALIB_GAP_S = 900.0        # prints further apart than this start a new calibration run
CALIB_LAG_S = 90.0         # the counter shows a trade up to a bbo round plus a book poll after it
SPLIT_PRIOR_BURSTS = 5     # the buyer/seller split from prints is pulled toward even by this many bursts a side
GAME_DAYS = 3.0            # a sports market that ends within this many days of the tape's end is a game
WIDTH = 79


@dataclass
class MarketMeasure:
    token_id: str
    label: str
    slug: str
    tick: Decimal
    sports: bool = False
    game: bool = False             # sports, ending within GAME_DAYS of the tape's end (not a season future)
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
    side_source: str = ""          # venue rows: "prints" (measured split) or "even" (assumed, not measured)
    sell_share: float = 0.5        # share of printed volume that hit the bid
    split_share: float = 0.5       # the same, pulled toward even when there are few bursts (splits the counter)
    wide_split_share: float = 0.5  # the same for prints while the spread was 2+ ticks
    wide_sell_share: float = 0.0   # printed volume that hit the bid while the spread was 2+ ticks / all printed
    wide_buy_share: float = 0.0
    improve_bound: bool = False    # one-tick-better flow is a lower bound (counter only): times are at most this
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
    counter_drops: int = 0         # readings below the highest value so far that were not a confirmed restart
    counter_restarts: int = 0
    counter_rebased: int = 0       # a stretch's first reading was far too low and was replaced by the next
    counter_moves: int = 0         # readings that added volume
    calib_counter: float = 0.0
    calib_prints: float = 0.0
    calib_notional: float = 0.0    # dollars traded in those prints: tells a counter of dollars from one of contracts
    wide_secs: float = 0.0

    @property
    def can_improve(self) -> float:
        return self.spread_2 + self.spread_3plus

    @property
    def ranked(self) -> bool:
        return self.flow_source in ("trades", "venue")

    @property
    def calib_ratio(self) -> Optional[float]:
        return self.calib_counter / self.calib_prints if self.calib_prints > 0 and self.calib_counter > 0 else None

    def within_observed(self, attr: str) -> bool:
        """A fill time no longer than the market was watched (improve times are in hours of 2+-tick spread)."""
        seen = self.hours * (self.can_improve if attr == "improve_round_trip_h" else 1.0)
        return getattr(self, attr) <= seen

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


def _ticks(bid: Optional[Decimal], ask: Optional[Decimal], tick: Decimal) -> int:
    """Spread in ticks; 0 when the book is one-sided, empty, locked or crossed (not quotable)."""
    if bid is None or ask is None or ask <= bid:
        return 0
    return max(1, int(((ask - bid) / tick).to_integral_value(rounding=ROUND_HALF_UP)))


def measure_token(store: Store, market: Market, token_id: str, since_ts: float, size: float,
                  spans: list[tuple[float, float]], has_counter_col: bool = True,
                  game_cutoff_ts: Optional[float] = None) -> MarketMeasure:
    tick = market.tick_size if market.tick_size > 0 else Decimal("0.01")
    label = market.question if market.synthetic_no else f"{market.question} [{_outcome(market, token_id)}]"
    slug = token_id[:-2] if market.synthetic_no and token_id.endswith("|L") else token_id
    out = MarketMeasure(token_id=token_id, label=label, slug=slug, tick=tick, sports="sports" in market.tags)
    out.game = bool(out.sports and market.end_date is not None and game_cutoff_ts is not None
                    and market.end_date.timestamp() <= game_cutoff_ts)
    tt_col = "traded_total" if has_counter_col else "NULL"
    rows = [(r[0], _d(r[1]), _d(r[2]), _d(r[3]) or Decimal(0), _d(r[4]) or Decimal(0), _d(r[5]))
            for r in store.query(f"SELECT ts, best_bid, best_ask, bid_sz, ask_sz, {tt_col} FROM books "
                                 "WHERE token_id=? AND ts >= ? ORDER BY ts", (token_id, since_ts))]
    out.rows = len(rows)
    prints = store.query("SELECT ts, side, size, price FROM trades WHERE token_id=? AND ts >= ? ORDER BY ts",
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
    # (ts, cumulative counted volume, stretch) at each counter reading along observed stretches
    counter_pts: list[tuple[float, float, int]] = []
    peak: Optional[Decimal] = None                        # highest reading in this unbroken stretch: the baseline
    pending: Optional[Decimal] = None                     # a reading below half the peak: a restart, if confirmed
    below = False                                         # inside a dip below the peak (one drop per dip)
    first_ts: Optional[float] = None                      # the stretch's first reading, until the counter first moves
    hole_s = hole_wide_s = 0.0                            # observed time since the last reading
    bracket_min = 0                                       # narrowest spread since the counter last moved
    counted = 0.0
    stretch = 0
    for cur, nxt in zip(rows, rows[1:]):
        gap = nxt[0] - cur[0]
        observed = 0 < gap <= gap_limit and Store._within_one_span(spans, cur[0], nxt[0])
        if not observed:
            if peak is not None:
                stretch += 1
            peak = pending = first_ts = None              # a break: never diff the counter across it
            below = False
            hole_s = hole_wide_s = 0.0
            continue
        cb, ca, cbs, cas, ctt = cur[1:]
        nb, na, nbs, nas, ntt = nxt[1:]
        ct = _ticks(cb, ca, tick)
        # volume counter: diff against the highest value seen in this unbroken stretch (holes are skipped over)
        if peak is None and ctt is not None:
            peak, pending, bracket_min, below, first_ts = ctt, None, ct, False, cur[0]
        if peak is not None:
            bracket_min = min(bracket_min, ct)
            hole_s += gap                                 # counted only once a reading follows
            if ct >= 2:
                hole_wide_s += gap
            if ntt is not None:
                out.counter_secs += hole_s
                out.counter_wide_secs += hole_wide_s
                hole_s = hole_wide_s = 0.0
                delta = Decimal(0)
                if (first_ts is not None and ntt > 2 * peak and nxt[0] - first_ts <= 2 * CALIB_LAG_S):
                    # the stretch's first reading was far too low (a cold or lagging reply): start from this one
                    peak, pending, below, first_ts = ntt, None, False, None
                    out.counter_rebased += 1
                elif ntt >= peak:                         # back at or above the highest value: count from there
                    delta, peak, pending, below = ntt - peak, ntt, None, False
                elif pending is not None and pending < ntt <= peak / 2:   # confirmed: it restarted and rose
                    delta, peak, pending, below = ntt, ntt, None, False
                    out.counter_restarts += 1
                else:                                     # below the peak: not counted, never lowers the baseline
                    if not below:
                        out.counter_drops += 1
                        below = True
                    pending = ntt if ntt <= peak / 2 else None   # wait for the next reading to believe a restart
                if delta > 0:
                    first_ts = None
                    out.counter_moves += 1
                    out.counter_total += float(delta)
                    counted += float(delta)
                    if bracket_min >= 2:                  # wide the whole time it could have traded
                        out.counter_wide += float(delta)
                    bracket_min = ct                      # the next trades came after the row before this change
                counter_pts.append((nxt[0], counted, stretch))
        if ct == 0:
            continue                                      # one-sided, empty, locked or crossed: not quotable
        ticks = ct
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
    for ts, side, sz, _ in prints:
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
        if total > 0:
            out.wide_sell_share = sum(z for _, s, z, t in kept if s == "SELL" and t >= 2) / total
            out.wide_buy_share = sum(z for _, s, z, t in kept if s == "BUY" and t >= 2) / total
        # a split from a few bursts is noisy, and noise always makes the slower side look slower: pull it to even
        sizes: list[float] = []
        last_on = {}
        for ts, side, z, _ in kept:
            if ts - last_on.get(side, -math.inf) >= BURST_S:
                sizes.append(0.0)
            sizes[-1] += z
            last_on[side] = ts
        prior = SPLIT_PRIOR_BURSTS * sorted(sizes)[len(sizes) // 2]
        out.split_share = (sold + prior) / (total + 2 * prior) if total + prior > 0 else 0.5
        w_sold = sum(z for _, s, z, t in kept if s == "SELL" and t >= 2)
        w_tot = sum(z for _, _, z, t in kept if t >= 2)
        out.wide_split_share = (w_sold + prior) / (w_tot + 2 * prior) if w_tot + prior > 0 else 0.5
        out.flow_source = "trades" if n >= MIN_PRINTS else "few"
        if counter_pts and n >= MIN_PRINTS and out.counter_drops < MAX_COUNTER_DROPS and not out.counter_restarts:
            out.calib_counter, out.calib_prints, out.calib_notional = _calibration(prints, counter_pts)
    if out.flow_source == "book":
        out.flow_hours = out.hours
        out.sell_flow_h = dep_bid / out.hours
        out.buy_flow_h = dep_ask / out.hours
    return out


def _calibration(prints: list, counter_pts: list[tuple[float, float, int]]) -> tuple[float, float, float]:
    """(counter volume, printed volume, printed dollars) over the runs of prints that the counter brackets.

    Within each unbroken stretch of counter readings, a run is prints no more than CALIB_GAP_S apart. Its
    counter volume runs from the reading at or before its first print to the first reading at least CALIB_LAG_S
    after its last print (the counter shows a trade late), before the next run starts; if the stretch ends
    sooner, to its last reading, and then only prints at least CALIB_LAG_S before that reading count. Time
    without prints (the stream was down, the market was not streamed) is never counted against the prints."""
    c_sum = p_sum = n_sum = 0.0
    by_stretch: dict[int, list[tuple[float, float]]] = {}
    for t, c, k in counter_pts:
        by_stretch.setdefault(k, []).append((t, c))
    for pts in by_stretch.values():
        times = [t for t, _ in pts]
        inside = [p for p in prints if times[0] < p[0] <= times[-1]]
        runs: list[list] = []
        for p in inside:
            if runs and p[0] - runs[-1][-1][0] <= CALIB_GAP_S:
                runs[-1].append(p)
            else:
                runs.append([p])
        for k, run in enumerate(runs):
            p0, p1 = run[0][0], run[-1][0]
            limit = min(runs[k + 1][0][0] if k + 1 < len(runs) else math.inf, p1 + CALIB_GAP_S)
            i = bisect.bisect_right(times, p0) - 1
            j = bisect.bisect_left(times, p1 + CALIB_LAG_S)
            if j >= len(times) or times[j] >= limit:
                j = bisect.bisect_left(times, limit) - 1
            cut = min(p1, times[j] - CALIB_LAG_S) if j >= 0 else -math.inf
            counted = sum(float(z) for t, _, z, _ in run if t <= cut)
            if i < 0 or j <= i or counted <= 0:
                continue
            c_sum += pts[j][1] - pts[i][1]
            p_sum += counted
            n_sum += sum(float(z) * float(px) for t, _, z, px in run if t <= cut)
    return c_sum, p_sum, n_sum


def _uses_counter(r: MarketMeasure, counter_ratio: Optional[float], counter_live: bool = True) -> bool:
    """The counter gives the total unless it is too short, not a running total, or never moves anywhere on the
    tape; on a market with enough prints only once it has been calibrated (until then the prints are the
    better-known number)."""
    if not counter_live or r.counter_secs < MIN_COUNTER_S or r.counter_drops >= MAX_COUNTER_DROPS:
        return False
    return r.flow_source != "trades" or counter_ratio is not None


def _finalize(r: MarketMeasure, size: float, counter_ratio: Optional[float], counter_live: bool = True) -> None:
    if _uses_counter(r, counter_ratio, counter_live):
        ratio = counter_ratio or 1.0
        measured = r.bursts >= MIN_PRINTS
        share = r.split_share if measured else 0.5
        r.side_source = "prints" if measured else "even"
        r.flow_source = "venue" if r.counter_moves >= MIN_PRINTS or measured else "few"
        r.flow_hours = r.counter_secs / 3600.0
        total = r.counter_total / ratio / r.flow_hours
        r.sell_flow_h, r.buy_flow_h = total * share, total * (1 - share)
        r.wide_sell_flow_h = r.wide_buy_flow_h = 0.0
        wide_frac = r.wide_secs / (r.hours * 3600) if r.hours > 0 else 0.0
        if measured:                                      # prints know the spread at each trade
            if wide_frac > 0:
                w = (r.wide_sell_share + r.wide_buy_share) * total / wide_frac
                r.wide_sell_flow_h, r.wide_buy_flow_h = w * r.wide_split_share, w * (1 - r.wide_split_share)
        elif r.counter_wide_secs > 0:                     # the counter only for volume it can place: a lower bound
            wide = r.counter_wide / ratio / (r.counter_wide_secs / 3600)
            r.wide_sell_flow_h, r.wide_buy_flow_h = wide * share, wide * (1 - share)
            r.improve_bound = True

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
            mm = measure_token(store, m, tok, since_ts, size, spans, has_counter_col,
                               game_cutoff_ts=end + GAME_DAYS * 86400)
            if mm.hours > 0:
                mm.coverage = mm.hours / up_h if up_h > 0 else 0.0
                rows.append(mm)
            elif mm.rows:
                dropped += 1
    with_counter = [r for r in rows if r.counter_secs >= MIN_COUNTER_S]
    counter_live = any(r.counter_moves > 0 for r in with_counter)   # a counter that never moves is not live
    calib = [r for r in rows if r.calib_ratio is not None] if counter_live else []
    ratio = None
    if len(calib) >= 2:
        ratio = sum(r.calib_counter for r in calib) / sum(r.calib_prints for r in calib)
    for r in rows:
        _finalize(r, size, ratio, counter_live)
    rank = {"trades": 0, "venue": 0, "few": 1, "book": 2}
    rows.sort(key=lambda r: (rank[r.flow_source], r.join_round_trip_h, r.improve_round_trip_h))
    ratios = sorted(r.calib_ratio for r in calib)
    dollar_ratios = sorted(r.calib_counter / r.calib_notional for r in calib if r.calib_notional > 0)
    # contracts: counter/prints is the same on every market; dollars: counter/(prints x price) is, and the first
    # then moves with the price
    counter_dollars = (len(ratios) >= 2 and len(dollar_ratios) == len(ratios)
                       and dollar_ratios[-1] / dollar_ratios[0] < 1.1 and ratios[-1] / ratios[0] > 1.2)
    info = {"tape_hours": (end - since_ts) / 3600.0, "up_hours": up_h, "size": size, "dropped": dropped,
            "counter_ratio": ratio, "counter_markets": len(calib),
            "counter_ratios": (ratios[0], ratios[len(ratios) // 2], ratios[-1]) if len(ratios) >= 2 else None,
            "has_counter_col": has_counter_col, "counter_live": counter_live, "counter_dollars": counter_dollars,
            "rebased": sum(r.counter_rebased for r in rows),
            "with_counter": len(with_counter),
            "with_prints": sum(1 for r in rows if r.prints > 0),
            "drop_markets": sum(1 for r in rows if r.counter_drops > 0),
            "drops": sum(r.counter_drops for r in rows),
            "restarts": sum(r.counter_restarts for r in rows),
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


def _src(r: MarketMeasure, calibrated: bool) -> str:
    n_prints = f"{r.prints} print{'s' if r.prints != 1 else ''}"
    n_bursts = "" if r.bursts == r.prints else f" in {r.bursts} burst{'s' if r.bursts != 1 else ''}"
    sides = f"sides from {n_prints}" if r.side_source == "prints" else "sides assumed even"
    uncal = "" if calibrated else " (uncal.)"
    if r.flow_source == "trades":
        return f"{n_prints}{n_bursts} over {r.flow_hours:.1f}h"
    if r.flow_source == "venue":
        return f"counter {r.flow_hours:.1f}h, {sides}{uncal}"
    if r.side_source:                                     # "few" from the counter
        return f"counter moved only {r.counter_moves}x: too few to rely on"
    return f"only {n_prints}{n_bursts}: too few to rely on"


def _kinds(rows: list[MarketMeasure]) -> str:
    sp = sum(1 for r in rows if r.sports)
    gm = sum(1 for r in rows if r.game)
    return f" ({sp} sports, {gm} of them games)" if sp else ""


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
    print(_wrap(f"Coverage: {len(rows)} markets observed{_kinds(rows)}; {info.get('with_counter', 0)} with the "
                f"venue's volume counter, {info.get('with_prints', 0)} with trade prints. Games are sports markets "
                f"ending within {GAME_DAYS:g} days; the other sports markets are season futures."))
    for i, r in enumerate(rows[:top], 1):
        print()
        print(f"{i:>2}. {r.label[:WIDTH - 4]}")
        kind = "  game" if r.game else ("  sports" if r.sports else "")
        print(f"    {r.slug[:40]}  tick {r.tick}  observed {r.hours:.1f}h ({r.coverage:.0%}){kind}")
        if r.flow_source == "book":
            print(f"    fill time: no trade data (book churn {_fmt_n(r.sell_flow_h)}/{_fmt_n(r.buy_flow_h)} "
                  f"per h is not trades)")
        else:
            longer = ("  > observed" if any(getattr(r, a) < math.inf and not r.within_observed(a)
                                            for a in ("join_round_trip_h", "improve_round_trip_h")) else "")
            bound = "<=" if r.improve_bound and r.improve_round_trip_h < math.inf else ""
            print(f"    fill time  join bid/ask {_h(r.fill_join_bid_h)}/{_h(r.fill_join_ask_h)}   "
                  f"one tick better {bound}{_h(r.fill_improve_bid_h)}/{_h(r.fill_improve_ask_h)}{longer}"[:WIDTH])
            print(f"    flow/h sell/buy {_fmt_n(r.sell_flow_h)}/{_fmt_n(r.buy_flow_h)}  from {_src(r, calibrated)}"[:WIDTH])
        print(f"    queue bid/ask {_fmt_n(r.bid_depth)}/{_fmt_n(r.ask_depth)}  spread 1/2/3+t "
              f"{r.spread_1:.0%}/{r.spread_2:.0%}/{r.spread_3plus:.0%}  median {r.median_spread_c:.1f}c"[:WIDTH])
    if len(rows) > top:
        print(f"\n... {len(rows) - top} more (use --top N, or --csv to get every row)")

    ranked = [r for r in rows if r.ranked]
    used_counter = any(r.flow_source == "venue" for r in ranked)
    print()
    if ranked:
        def count(within: float, attr: str) -> str:
            # a fill time longer than the market was watched is an extrapolation (a game may settle first)
            hit = [r for r in ranked if getattr(r, attr) <= within and r.within_observed(attr)]
            return f"{len(hit)}{_kinds(hit) if n_sports else ''}"

        print(_wrap(f"Markets with measured flow: {len(ranked)}{_kinds(ranked)}. A {size:g}-lot fills on both sides "
                    f"when joining the queue within 1h in {count(1, 'join_round_trip_h')} and within 24h in "
                    f"{count(24, 'join_round_trip_h')}; when pricing one tick better (counting only time when the "
                    f"spread allows it) within 1h in {count(1, 'improve_round_trip_h')} and within 24h in "
                    f"{count(24, 'improve_round_trip_h')}. Only fill times no longer than the market was observed "
                    f"are counted."
                    + (" The volume counter is not calibrated, so its scale is unknown." if used_counter
                       and not calibrated else "")))
        even = sum(1 for r in ranked if r.side_source == "even")
        if even:
            print(_wrap(f"{even} of them {'has' if even == 1 else 'have'} too few trade prints to tell buyers from "
                        f"sellers, so trading there is assumed to be split evenly. That is not measured: the real "
                        f"split can make their fill times shorter or longer."))
        if any(r.improve_bound for r in ranked):
            print(_wrap("Without trade prints, 'one tick better' counts only volume that traded while the spread "
                        "stayed 2+ ticks wide between two counter readings: those times (marked <=) are at most "
                        "what is shown."))
    else:
        print(_wrap("No market has measured trading flow on this tape (no trade prints, no venue volume counter)."))
    few = sum(1 for r in rows if r.flow_source == "few")
    book = sum(1 for r in rows if r.flow_source == "book")
    if few:
        print(_wrap(f"{few} markets have fewer than {MIN_PRINTS} bursts of trade prints (prints on one side less "
                    f"than {BURST_S:g}s apart are one taker order) or a volume counter that moved fewer than "
                    f"{MIN_PRINTS} times: shown, not counted above."))
    if book:
        why = ("this tape has no volume counter (tapes recorded before 2026-10-18)" if not info.get("has_counter_col")
               else "the venue's volume counter was missing or unusable for them")
        print(_wrap(f"{book} markets have no trade data: their book churn is not trading, so they are not ranked "
                    f"({why})."))
    if info.get("with_counter") and not info.get("counter_live", True):
        print(_wrap(f"WARNING: the venue's volume counter never changed on any of {info['with_counter']} markets, "
                    f"so it is not a live count of trading and was not used."))
    if calibrated:
        rng = info.get("counter_ratios")
        spread = (f" (per market {rng[0]:.2f} to {rng[2]:.2f}, median {rng[1]:.2f})" if rng else "")
        print(_wrap(f"Venue volume counter calibrated against trade prints on {info['counter_markets']} streamed "
                    f"markets: counter = {info['counter_ratio']:.2f} x prints{spread}."
                    + (" The markets disagree by more than 1.5x, so one ratio may not fit all markets."
                       if rng and rng[0] > 0 and rng[2] / rng[0] > 1.5 else "")
                    + (" WARNING: the counter follows dollars traded (contracts x price) more closely than "
                       "contracts, so one ratio does not fit markets at other prices; treat counter flows as rough."
                       if info.get("counter_dollars") else "")))
    elif info.get("with_counter") and info.get("counter_live", True):
        print(_wrap("The venue volume counter is not calibrated yet: that needs 2 streamed markets with both the "
                    f"counter and {MIN_PRINTS}+ bursts of prints. Until then 1 on the counter is taken as 1 "
                    "contract (marked uncal.)."))
    if info.get("drops") or info.get("restarts"):
        print(_wrap(f"The venue volume counter went down {info.get('drops', 0)} times on {info.get('drop_markets', 0)} "
                    f"markets and restarted from zero {info.get('restarts', 0)} times. Volume below its highest "
                    f"value is not counted, and {info.get('unusable_counters', 0)} markets where it went down "
                    f"{MAX_COUNTER_DROPS}+ times are measured without it. Frequent drops would mean it is not a "
                    f"running total."))
    if info.get("dropped"):
        print(_wrap(f"{info['dropped']} markets were never quotable while observed (one-sided or crossed) and are "
                    f"not shown."))


def write_csv(rows: list[MarketMeasure], path: str) -> None:
    cols = ["label", "slug", "token_id", "tick", "sports", "game", "hours", "coverage", "flow_source",
            "side_source", "sell_share", "split_share", "counter_moves", "counter_drops", "counter_restarts",
            "counter_rebased", "calib_ratio", "prints", "bursts", "flow_hours",
            "sell_flow_h", "buy_flow_h", "bid_depth", "ask_depth", "spread_1", "spread_2", "spread_3plus",
            "median_spread_c", "can_improve", "wide_sell_flow_h", "wide_buy_flow_h", "fill_join_bid_h",
            "fill_join_ask_h", "join_round_trip_h", "fill_improve_bid_h", "fill_improve_ask_h",
            "improve_round_trip_h", "improve_bound", "mid_moves_h", "rows"]
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:      # BOM: Excel on Windows reads UTF-8
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            vals = []
            for c in cols:
                v = getattr(r, c)
                vals.append("" if v is None or (isinstance(v, float) and math.isinf(v)) else v)
            w.writerow(vals)
