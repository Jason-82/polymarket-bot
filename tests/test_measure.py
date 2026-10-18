"""`pm measure`, checked against hand-computed values. Each test names the finding it pins (independent oracle and
review of f10f5d8)."""

import math
import os
import sqlite3
import subprocess
import sys
from decimal import Decimal

import pytest

from pm.config import Config
from pm.measure import measure_tape, print_report
from pm.models import Side
from pm.store import Store
from pm.venues.polymarket_us import USMarketData, books_from_us, market_from_us
from tests.conftest import D, mk_market

H = 3600.0


def _us(slug="x", tick=0.01, title=None):
    return market_from_us({"slug": slug, "title": title or slug.upper(), "outcomes": "[\"Yes\",\"No\"]",
                           "feeCoefficient": 0.06, "minimumTradeQty": 1, "orderPriceMinTickSize": tick,
                           "endDate": "2099-01-01T00:00:00Z", "active": True},
                          {"slug": "e", "title": "E", "category": "politics"})


def _row(s, ts, tok, bid, ask, bsz=1000, asz=1000, traded=None):
    s._q(s._BOOK_SQL_VOL, (ts, tok, bid, ask, str(bsz), str(asz), "{}", traded))


def _recorder(s, start, end, every=10.0):
    """A REST-polled market the recorder writes every 10 s: proves the recorder was running."""
    t = start
    while t <= end:
        _row(s, t, "HB|L", "0.10", "0.11")
        t += every


def _tape(markets, hours=1.0):
    s = Store(":memory:")
    for m in markets:
        s.market(m)
    _recorder(s, 0, hours * H)
    return s


def _one(rows, token="x|L"):
    return next(r for r in rows if r.token_id == token)


# ------------------------------------------------------------------ basics

def test_spread_depth_moves_and_cents():
    m = _us(tick=0.01)
    s = _tape([m], hours=1)
    for k in range(0, 360):                       # one hour, a row every 10 s
        t = 10 * k
        if t < 720:
            _row(s, t, "x|L", "0.41", "0.42", 1000, 2000)      # 1 tick, 20% of the time
        elif t < 1440:
            _row(s, t, "x|L", "0.40", "0.43", 3000, 2000)      # symmetric widen: 3 ticks, mid unchanged
        else:                                                  # (0.41+0.42 != 0.40+0.43 in float arithmetic)
            _row(s, t, "x|L", "0.40", "0.44", 5000, 2000)      # 4 ticks, mid moved
    _row(s, 3600, "x|L", "0.40", "0.44", 5000, 2000)
    rows, _ = measure_tape(s)
    r = _one(rows)
    assert r.hours == pytest.approx(1.0)
    assert (r.spread_1, r.spread_2, r.spread_3plus) == pytest.approx((0.2, 0.0, 0.8))
    assert r.bid_depth == 5000 and r.ask_depth == 2000
    assert r.median_spread_c == pytest.approx(4.0)
    assert r.mid_moves_h == pytest.approx(1.0)                  # the symmetric widen is not a move (Decimal math)
    s.close()


# ------------------------------------------------------------------ F1: streamed books' quiet time is observed

def test_quiet_time_on_a_streamed_book_counts_as_observed():
    m = _us()
    s = _tape([m], hours=10)
    for k in range(11):                                         # a change every hour only
        _row(s, k * H, "x|L", "0.50", "0.51", 93, 93)
    for k in range(50):                                         # 50 sells of 20 spread over the 10 h, each just
        s.trade("x|L", D("0.50"), D(20), Side.SELL, 300 + k * 711.0)   # before a book row would follow
    rows, _ = measure_tape(s)
    r = _one(rows)
    assert r.hours == pytest.approx(10.0)
    assert r.flow_source == "trades" and r.prints == 50
    assert r.sell_flow_h == pytest.approx(1000 / 10.0, rel=0.05)   # ~100/h, not inflated
    s.close()


# ------------------------------------------------------------------ F3/F8: streamed for part of the tape

def test_flow_uses_the_stretch_the_prints_cover():
    m = _us()
    s = _tape([m], hours=10)
    for k in range(0, 3601):
        _row(s, 10 * k, "x|L", "0.50", "0.51", 100, 100)        # 10 h, polled throughout
    for k in range(120):                                        # streamed only in the first hour: 1200/h each way
        s.trade("x|L", D("0.50"), D(10), Side.SELL, 15 + k * 30.0)
        s.trade("x|L", D("0.51"), D(10), Side.BUY, 16 + k * 30.0)
    rows, _ = measure_tape(s)
    r = _one(rows)
    assert r.flow_source == "trades"
    assert r.flow_hours == pytest.approx(1.0, rel=0.05)
    assert r.sell_flow_h == pytest.approx(1200, rel=0.05)       # not diluted 10x by the polled hours
    s.close()


# ------------------------------------------------------------------ one sweep is one order, not a rate

def _quiet_streamed(slug="x", hours=10):
    m = _us(slug)
    s = _tape([m], hours=hours)
    for k in range(hours + 1):
        _row(s, k * H, f"{slug}|L", "0.50", "0.51", 5000, 5000)
    return s


def test_a_sweep_of_many_prints_counts_as_one_burst():
    s = _quiet_streamed()
    for k in range(13):                                         # one seller sweeps 13 bids within 1.2 s
        s.trade("x|L", D("0.50"), D(500), Side.SELL, 4 * H + k * 0.1)
    rows, _ = measure_tape(s)
    r = _one(rows)
    assert r.prints == 13 and r.bursts == 1 and r.flow_source == "few" and not r.ranked
    assert r.flow_hours == pytest.approx(10.0)
    assert r.sell_flow_h == pytest.approx(6500 / 10.0)          # 650/h; the span estimate gave ~65 million/h
    s.close()


def test_bursts_minutes_apart_are_rated_over_at_least_an_hour():
    s = _quiet_streamed()
    for k in range(6):                                          # six separate orders within one minute
        s.trade("x|L", D("0.50"), D(100), Side.SELL, 4 * H + k * 10.0)
    rows, _ = measure_tape(s)
    r = _one(rows)
    assert r.bursts == 6 and r.flow_source == "trades"
    assert r.flow_hours == pytest.approx(1.0)                   # the floor, not 70 s
    assert r.sell_flow_h == pytest.approx(600)
    s.close()


def test_the_flow_window_never_exceeds_the_observed_time():
    m = _us()
    s = _tape([m], hours=1)
    for k in range(181):                                        # observed for 30 minutes only
        _row(s, 10 * k, "x|L", "0.50", "0.51", 100, 100)
    for k in range(6):
        s.trade("x|L", D("0.50"), D(10), Side.SELL, 600 + k * 10.0)
    rows, _ = measure_tape(s)
    r = _one(rows)
    assert r.hours == pytest.approx(0.5) and r.flow_hours == pytest.approx(0.5)   # not the 1 h floor
    s.close()


def test_bursts_are_counted_per_side():
    s = _quiet_streamed()
    for k in range(5):                                          # a sell and a buy at the same instant: two orders
        s.trade("x|L", D("0.50"), D(10), Side.SELL, (2 + k) * H)
        s.trade("x|L", D("0.51"), D(10), Side.BUY, (2 + k) * H)
        s.trade("x|L", D("0.51"), D(10), Side.BUY, (2 + k) * H + 0.5)   # same buyer, next maker
    rows, _ = measure_tape(s)
    r = _one(rows)
    assert r.prints == 15 and r.bursts == 10 and r.flow_source == "trades"
    assert r.flow_hours == pytest.approx(4 * 11 / 9)            # first-to-last burst 4 h, scaled (n+1)/(n-1)
    s.close()


# ------------------------------------------------------------------ F2: counter only over the time it covers

def test_venue_counter_is_divided_by_the_time_it_was_recorded():
    m = _us()
    s = _tape([m], hours=11)
    counter = 5000
    for k in range(0, 3961):
        t = 10 * k
        if t < 10 * H:
            _row(s, t, "x|L", "0.50", "0.52", 100, 100, None)   # rows from before the counter was recorded
        else:
            counter += 10                                       # +10 per 10 s = 3600/h total
            _row(s, t, "x|L", "0.50", "0.52", 100, 100, str(counter))
    rows, info = measure_tape(s)
    r = _one(rows)
    assert r.flow_source == "venue" and info["counter_ratio"] is None
    assert r.flow_hours == pytest.approx(1.0, rel=0.01)
    assert r.sell_flow_h == pytest.approx(1800, rel=0.02)       # not 11x too low
    s.close()


def test_counter_holes_and_resets_keep_the_volume():
    m = _us()
    s = _tape([m], hours=1)
    counter, true_volume = 100, 0
    for k in range(361):
        t = 10 * k
        if k == 180:
            counter = 400                                       # restarted from zero (below half of 995), 400 since
            true_volume += 400
        elif k > 0:
            counter += 5
            true_volume += 5
        _row(s, t, "x|L", "0.50", "0.52", 100, 100, None if k % 2 else str(counter))   # every other row lacks it
    rows, _ = measure_tape(s)
    r = _one(rows)
    assert r.flow_source == "venue"
    assert r.sell_flow_h + r.buy_flow_h == pytest.approx(true_volume, rel=0.02)
    s.close()


def test_a_counter_that_goes_down_is_not_counted_and_three_drops_retire_it():
    ms = [_us("a"), _us("b")]
    s = _tape(ms, hours=1)
    for slug, dips in (("a", (100,)), ("b", (100, 150, 200))):
        counter = 10000
        for k in range(361):
            counter += 5
            if k in dips:
                counter -= 40                                   # a rolling window dropping old trades, not a restart
            _row(s, 10 * k, f"{slug}|L", "0.50", "0.52", 100, 100, str(counter))
    rows, info = measure_tape(s)
    a, b = _one(rows, "a|L"), _one(rows, "b|L")
    assert a.counter_drops == 1 and a.flow_source == "venue"
    # below the highest value nothing is counted, so the dip is never counted twice: last - first reading
    assert a.sell_flow_h + a.buy_flow_h == pytest.approx(360 * 5 - 40, rel=0.01)
    assert b.counter_drops == 3 and b.flow_source == "book" and math.isinf(b.join_round_trip_h)
    assert info["drop_markets"] == 2 and info["drops"] == 4 and info["unusable_counters"] == 1
    s.close()


def test_counter_is_calibrated_against_prints_on_streamed_markets():
    ms = [_us("a"), _us("b"), _us("c")]
    s = _tape(ms, hours=2)
    for slug in ("a", "b"):
        counter = 0
        for k in range(721):
            t = 10 * k
            if k % 6 == 0 and 0 < k < 720:
                s.trade(f"{slug}|L", D("0.50"), D(10), Side.SELL, t - 1)
                counter += 20                                   # this venue's counter counts 2x the prints
            _row(s, t, f"{slug}|L", "0.50", "0.52", 100, 100, str(counter))
    counter = 0
    for k in range(721):                                        # c: counter only, 200 per 10 s
        counter += 200
        _row(s, 10 * k, "c|L", "0.50", "0.52", 100, 100, str(counter))
    rows, info = measure_tape(s)
    assert info["counter_ratio"] == pytest.approx(2.0, rel=0.05)
    c = _one(rows, "c|L")
    assert c.flow_source == "venue"
    assert c.sell_flow_h == pytest.approx(72000 / 2 / 2, rel=0.03)    # counter/h, de-doubled, split by side
    s.close()


# ------------------------------------------------------------------ the counter gives the total, prints the sides

def _sport(slug, end="1970-01-02T00:00:00Z"):
    """A game: it ends within 3 days of these tapes' end (they start at ts 0); end=None makes it a season future."""
    return market_from_us({"slug": slug, "title": slug.upper(), "outcomes": "[\"Yes\",\"No\"]",
                           "feeCoefficient": 0.06, "minimumTradeQty": 1, "orderPriceMinTickSize": 0.01,
                           "marketType": "moneyline", "active": True, **({"endDate": end} if end else {})},
                          {"slug": "g", "title": "Knicks vs Celtics", "category": "sports"})


def _calibrated_tape(extra, hours=2):
    """Two streamed markets whose counter equals their prints (ratio 1), plus `extra`."""
    ms = [_us("a"), _us("b")] + list(extra)
    s = _tape(ms, hours=hours)
    for slug in ("a", "b"):
        counter = 0
        for k in range(int(hours * 360) + 1):
            t = 10 * k
            if k % 6 == 0 and 0 < k < hours * 360:
                s.trade(f"{slug}|L", D("0.50"), D(30), Side.SELL, t - 1)
                s.trade(f"{slug}|L", D("0.51"), D(10), Side.BUY, t - 2)
                counter += 40
            _row(s, t, f"{slug}|L", "0.50", "0.51", 100, 100, str(counter))
    return s


def test_calibrated_counter_gives_the_total_and_prints_the_split():
    c = _us("c")
    s = _calibrated_tape([c])
    counter = 0
    for k in range(721):                                        # 2 h of counter, 720/h in all
        counter += 2
        _row(s, 10 * k, "c|L", "0.50", "0.51", 100, 100, str(counter))
    for k in range(36):                                         # streamed only in the first half hour: buyers only
        s.trade("c|L", D("0.51"), D(10), Side.BUY, 45 + k * 50.0)    # (the counter's 360 in that half hour)
    rows, info = measure_tape(s)
    assert info["counter_ratio"] == pytest.approx(1.0, rel=0.02)
    lo, mid, hi = info["counter_ratios"]
    assert lo <= mid <= hi and hi / lo < 1.1
    a, r = _one(rows, "a|L"), _one(rows, "c|L")
    assert a.flow_source == "venue" and a.side_source == "prints" and a.sell_share == pytest.approx(0.75)
    assert a.split_share == pytest.approx((3570 + 150) / (4760 + 300))   # 119 bursts a side: pulled a little
    assert a.sell_flow_h / a.buy_flow_h == pytest.approx(a.split_share / (1 - a.split_share))
    assert r.flow_source == "venue" and r.side_source == "prints"
    assert r.flow_hours == pytest.approx(2.0, rel=0.01)         # the counter's time, not the half hour of prints
    assert r.sell_flow_h + r.buy_flow_h == pytest.approx(720, rel=0.03)
    # 36 bursts, all buyers: nearly one-sided, pulled toward even by 5 median bursts a side
    assert r.split_share == pytest.approx(50 / 460) and r.sell_flow_h > 0
    s.close()


def test_uncalibrated_counter_leaves_printed_markets_on_their_prints():
    m = _us()
    s = _tape([m], hours=2)
    counter = 0
    for k in range(721):
        counter += 50                                           # unknown scale: much bigger than the prints
        _row(s, 10 * k, "x|L", "0.50", "0.51", 100, 100, str(counter))
    for k in range(20):
        s.trade("x|L", D("0.50"), D(10), Side.SELL, 100 + k * 360.0)
    rows, info = measure_tape(s)
    r = _one(rows)
    assert info["counter_ratio"] is None and r.flow_source == "trades"
    assert r.sell_flow_h == pytest.approx(200 / 2.0, rel=0.1)
    s.close()


def test_report_counts_sports_and_marks_even_splits_as_the_best_case(capsys):
    game, future = _sport("nyk-bos-ml"), _sport("nba-champion", end=None)
    s = _calibrated_tape([game, future])
    counter = 0
    for k in range(721):
        counter += 200                                          # 72,000/h, no prints: split evenly
        _row(s, 10 * k, "nyk-bos-ml|L", "0.50", "0.51", 100, 100, str(counter))
        _row(s, 10 * k, "nba-champion|L", "0.50", "0.51", 100, 100, str(counter))
    rows, info = measure_tape(s)
    g, f = _one(rows, "nyk-bos-ml|L"), _one(rows, "nba-champion|L")
    assert g.sports and g.game and f.sports and not f.game
    assert g.side_source == "even" and g.sell_flow_h == pytest.approx(36000, rel=0.02)
    assert g.join_round_trip_h < 1
    print_report(rows, info)
    out = capsys.readouterr().out
    text = " ".join(out.split())                                # the summary is wrapped at 79 columns
    assert ("4 markets observed (2 sports, 1 of them games); 4 with the venue's volume counter, 2 with trade "
            "prints") in text
    assert "sides assumed even" in text and "not measured" in text and "nyk-bos-ml tick 0.01" in text
    assert "within 1h in 4 (2 sports, 1 of them games)" in text and "2 of them have too few trade prints" in text
    assert all(len(line) <= 79 for line in out.splitlines())
    s.close()


# ------------------------------------------------------------------ F5: improving only counts while it is possible

def test_improve_time_uses_only_flow_while_the_spread_is_wide():
    m = _us()
    s = _tape([m], hours=10)
    for k in range(3601):
        t = 10 * k
        wide = 1000 <= t < 1300                                 # 2 ticks for 5 minutes only (0.8% of the time)
        _row(s, t, "x|L", "0.50", "0.52" if wide else "0.51", 500, 500)
    for k in range(200):
        s.trade("x|L", D("0.50"), D(1), Side.SELL, 5 + k * 180.0)
        s.trade("x|L", D("0.51"), D(1), Side.BUY, 6 + k * 180.0)
    rows, _ = measure_tape(s)
    r = _one(rows)
    assert r.can_improve < 0.05
    assert math.isinf(r.fill_improve_bid_h) and math.isinf(r.improve_round_trip_h)   # not "1.0h"
    s.close()


def test_improve_flow_counts_only_prints_while_the_spread_is_wide():
    m = _us()
    s = _tape([m], hours=2)
    for k in range(721):
        t = 10 * k
        wide = (t // 600) % 2 == 1                              # wide every other 10 minutes: 50% of the time
        _row(s, t, "x|L", "0.50", "0.52" if wide else "0.51", 500, 500)
    for k in range(12):                                         # every print falls in a narrow stretch
        s.trade("x|L", D("0.50"), D(5), Side.SELL, 100 + k * 1200.0)
        s.trade("x|L", D("0.51"), D(5), Side.BUY, 101 + k * 1200.0)
    rows, _ = measure_tape(s)
    r = _one(rows)
    assert r.can_improve == pytest.approx(0.5, abs=0.02) and r.sell_flow_h > 0
    assert r.wide_sell_flow_h == 0 and math.isinf(r.fill_improve_bid_h)
    s.close()


# ------------------------------------------------------------------ F6/F7: ranking and reliability

def test_ranking_uses_the_slower_side_and_few_prints_are_not_ranked():
    one_sided, two_sided, thin = _us("one"), _us("two"), _us("thin")
    s = _tape([one_sided, two_sided, thin], hours=10)
    for k in range(3601):
        for slug in ("one", "two", "thin"):
            _row(s, 10 * k, f"{slug}|L", "0.50", "0.51", 100, 100)
    for k in range(100):
        s.trade("one|L", D("0.51"), D(50), Side.BUY, 50 + k * 355.0)       # buyers only
        s.trade("two|L", D("0.50"), D(10), Side.SELL, 50 + k * 355.0)
        s.trade("two|L", D("0.51"), D(10), Side.BUY, 51 + k * 355.0)
    s.trade("thin|L", D("0.50"), D(400), Side.SELL, 500.0)                  # one big print
    rows, _ = measure_tape(s)
    order = [r.slug for r in rows]
    assert order.index("two") < order.index("one")
    assert math.isinf(_one(rows, "one|L").join_round_trip_h)
    assert _one(rows, "thin|L").flow_source == "few" and order.index("thin") > order.index("one")
    s.close()


def test_book_only_markets_get_no_fill_time(capsys):
    m = _us("quiet", title="Quiet")
    s = _tape([m], hours=1)
    for k in range(361):
        _row(s, 10 * k, "quiet|L", "0.50", "0.51", 300 + 50 * (k % 2), 300 + 50 * (k % 2))   # churn, both sides
    rows, info = measure_tape(s)
    r = _one(rows, "quiet|L")
    assert r.flow_source == "book" and not r.ranked and math.isinf(r.join_round_trip_h)
    print_report(rows, info)
    out = capsys.readouterr().out
    assert "no trade data" in out and "not trades" in out
    s.close()


# ------------------------------------------------------------------ F9/F10/width: the report reads correctly

def test_report_shows_tick_slug_and_fits_79_columns(capsys):
    a = _us("highest-temp-nyc-oct20-60-61", tick=0.001, title="Highest temperature in NYC on October 20?: 60-61F")
    b = _us("highest-temp-nyc-oct20-62-63", tick=0.01, title="Highest temperature in NYC on October 20?: 62-63F")
    s = _tape([a, b], hours=1)
    for k in range(361):
        for m in (a, b):
            _row(s, 10 * k, m.yes.token_id, "0.50", "0.51", 100, 100)
    for k in range(20):
        for m in (a, b):
            s.trade(m.yes.token_id, D("0.50"), D(5), Side.SELL, 100 + k * 170.0)
            s.trade(m.yes.token_id, D("0.51"), D(5), Side.BUY, 101 + k * 170.0)
    rows, info = measure_tape(s)
    print_report(rows, info)
    out = capsys.readouterr().out
    assert all(len(line) <= 79 for line in out.splitlines()), [l for l in out.splitlines() if len(l) > 79]
    assert "highest-temp-nyc-oct20-60-61  tick 0.001" in out and "highest-temp-nyc-oct20-62-63  tick 0.01" in out
    assert "median 1.0c" in out
    s.close()


def test_non_cp1252_market_names_do_not_crash_a_redirected_windows_stdout(tmp_path):
    path = tmp_path / "t.sqlite"
    s = Store(str(path))
    m = _us("swiatek", title="Swiatek Świątek ≥ 2 sets")
    s.market(m)
    for k in range(361):
        _row(s, 10 * k, "swiatek|L", "0.50", "0.51")
    for k in range(10):
        s.trade("swiatek|L", D("0.50"), D(5), Side.SELL, 50 + k * 300.0)
    s.close()
    env = dict(os.environ, PYTHONIOENCODING="cp1252", PYTHONUTF8="0")
    csv_path = tmp_path / "m.csv"
    r = subprocess.run([sys.executable, "-m", "pm", "--venue", "us", "measure", "--db", str(path), "--csv",
                        str(csv_path)], capture_output=True, env=env, cwd=os.getcwd())
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert csv_path.exists()


# ------------------------------------------------------------------ tapes: read-only, old schema, recording

def test_reports_never_modify_the_tape(tmp_path):
    path = tmp_path / "old.sqlite"
    con = sqlite3.connect(path)                                 # written before traded_total existed
    con.execute("CREATE TABLE books (ts REAL, token_id TEXT, best_bid TEXT, best_ask TEXT, bid_sz TEXT, "
                "ask_sz TEXT, top TEXT)")
    con.execute("CREATE TABLE trades (ts REAL, token_id TEXT, price TEXT, size TEXT, side TEXT)")
    con.execute("CREATE TABLE markets (condition_id TEXT PRIMARY KEY, question TEXT, slug TEXT, json TEXT, "
                "updated_ts REAL)")
    con.commit()
    con.close()
    w = Store(":memory:")
    m = _us()
    w.market(m)
    con = sqlite3.connect(path)
    con.executemany("INSERT INTO markets VALUES (?,?,?,?,?)", w.query("SELECT * FROM markets"))
    for k in range(61):
        con.execute("INSERT INTO books VALUES (?,?,?,?,?,?,?)", (60 * k, "x|L", "0.5", "0.51", "10", "10", "{}"))
    con.commit()
    con.close()
    w.close()
    before = path.read_bytes()
    s = Store(str(path), readonly=True)
    rows, _ = measure_tape(s)
    s.close()
    assert [r.token_id for r in rows] == ["x|L"]
    assert path.read_bytes() == before                          # no ALTER TABLE, no schema writes


def test_us_stream_carries_the_volume_counter_forward():
    got = []
    md = USMarketData(Config(), None, got.append, lambda t: None)
    book = {"bids": [{"px": {"value": "0.5"}, "qty": "10"}], "offers": [{"px": {"value": "0.51"}, "qty": "10"}]}
    md._emit_books("x", {**book, "stats": {"sharesTraded": {"value": "123"}}})   # an Amount-shaped value
    md._emit_books("x", book)                                                     # no stats this time
    assert [b.traded_total for b in got] == [D(123), D(123), D(123), D(123)]
    long_b, _ = books_from_us("x", {"marketData": {**book, "stats": {"sharesTraded": "77"}}})
    assert long_b.traded_total == D(77)


# ------------------------------------------------------------------ review findings: the counter

def _counter_market(s, slug, values, every=10.0, start=0.0, bid="0.50", ask="0.51", qty=100):
    for k, v in enumerate(values):
        _row(s, start + every * k, f"{slug}|L", bid, ask, qty, qty, None if v is None else str(v))


def test_one_glitch_reading_is_not_counted_as_a_restart():
    s = _tape([_us()], hours=1)
    vals = [1_000_000 + 5 * k for k in range(361)]
    vals[100] = 0                                                       # one bad reading
    vals[200] = 400_000                                                 # and one low-but-not-zero
    _counter_market(s, "x", vals)
    rows, info = measure_tape(s)
    r = _one(rows)
    assert r.counter_restarts == 0 and r.counter_drops == 2
    assert r.counter_total == pytest.approx(360 * 5)                    # not +1,000,000
    s.close()


def test_a_restart_needs_the_next_reading_to_confirm_it():
    s = _tape([_us()], hours=1)
    vals = [10_000 + 5 * k for k in range(181)] + [100 + 5 * k for k in range(180)]
    _counter_market(s, "x", vals)
    rows, info = measure_tape(s)
    r = _one(rows)
    assert r.counter_restarts == 1 and r.counter_drops == 1 and info["restarts"] == 1
    # the reading that restarts is held until the next confirms it, then counted from zero
    assert r.counter_total == pytest.approx(180 * 5 + 100 + 179 * 5)
    s.close()


def test_one_trade_on_the_counter_is_too_few_to_rank():
    s = _tape([_us()], hours=10)
    vals = [5000] * 1800 + [5600] * 1801                                # one 600-lot in 10 hours
    _counter_market(s, "x", vals)
    rows, _ = measure_tape(s)
    r = _one(rows)
    assert r.counter_moves == 1 and r.flow_source == "few" and not r.ranked
    s.close()


def test_a_counter_that_never_moves_is_not_used_and_is_warned_about(capsys):
    s = _tape([_us("a"), _us("b")], hours=1)
    for slug in ("a", "b"):
        _counter_market(s, slug, [0] * 361)
    rows, info = measure_tape(s)
    assert not info["counter_live"] and all(r.flow_source == "book" for r in rows)
    print_report(rows, info)
    assert "never changed on any of 2 markets" in " ".join(capsys.readouterr().out.split())
    s.close()


def test_calibration_ignores_hours_when_no_prints_were_recorded():
    ms = [_us("a"), _us("b"), _us("c")]
    s = _tape(ms, hours=10)
    for slug in ("a", "b"):
        counter = 0
        for k in range(3601):
            t = 10 * k
            if k % 3 == 0 and k:
                counter += 10                                           # 1200/h all day on the counter...
                if not (2 * H <= t < 8 * H):                            # ...but the stream was down from 2 h to 8 h
                    s.trade(f"{slug}|L", D("0.50"), D(10), Side.SELL, t - 4)
            _row(s, t, f"{slug}|L", "0.50", "0.51", 100, 100, str(counter))
    _counter_market(s, "c", [10 * k for k in range(3601)])              # 3600/h
    rows, info = measure_tape(s)
    assert info["counter_ratio"] == pytest.approx(1.0, rel=0.03)       # not 2.5
    c = _one(rows, "c|L")
    assert c.sell_flow_h + c.buy_flow_h == pytest.approx(3600, rel=0.04)
    s.close()


def test_calibration_counts_prints_and_counter_over_the_same_window():
    """Prints off the row grid, a counter that shows each trade at the next row: 5 bursts must still give ~1."""
    ms = [_us("a"), _us("b")]
    s = _tape(ms, hours=2)
    for slug in ("a", "b"):
        trades = [600 + 900 * k + 3.7 for k in range(4)] + [3333.7]    # the last two 30 s apart
        counter = 0
        for k in range(721):
            t = 10 * k
            counter += sum(50 for x in trades if t - 10 < x <= t)
            _row(s, t, f"{slug}|L", "0.50", "0.51", 100, 100, str(counter))
        for x in trades:
            s.trade(f"{slug}|L", D("0.50"), D(50), Side.SELL, x)
    rows, info = measure_tape(s)
    assert info["counter_ratio"] == pytest.approx(1.0, rel=0.01)       # not (n-2)/n = 0.6
    s.close()


def test_counter_volume_is_wide_only_if_the_spread_was_wide_the_whole_time():
    """Takers trade only at a 1-tick spread; each trade leaves the spread 2 ticks wide for 20 s, and the counter
    shows it on the next row. None of that volume could have filled an order priced inside the spread."""
    s = _tape([_us()], hours=2)
    counter = 0
    for k in range(721):
        t = 10 * k
        wide = k % 9 in (4, 5)                                          # the trade at row 3 widened the spread
        if k % 9 == 5:
            counter += 50                                               # the counter catches up two rows later
        _row(s, t, "x|L", "0.50", "0.52" if wide else "0.51", 100, 100, str(counter))
    rows, _ = measure_tape(s)
    r = _one(rows)
    assert r.can_improve > 0.05 and r.counter_wide == 0
    assert math.isinf(r.improve_round_trip_h)
    s.close()


def test_streamed_markets_take_their_wide_flow_from_the_prints():
    ms = [_us("a"), _us("b")]
    s = _tape(ms, hours=2)
    for slug in ("a", "b"):
        counter = 0
        for k in range(721):
            t = 10 * k
            wide = (t // 600) % 2 == 1                                  # wide half the time, no trades then
            if not wide and k % 6 == 3:
                s.trade(f"{slug}|L", D("0.50"), D(10), Side.SELL, t - 3)
                s.trade(f"{slug}|L", D("0.51"), D(10), Side.BUY, t - 2)
                counter += 20
            _row(s, t + 1, f"{slug}|L", "0.50", "0.52" if wide else "0.51", 100, 100, str(counter))
    rows, info = measure_tape(s)
    a = _one(rows, "a|L")
    assert info["counter_ratio"] is not None and a.flow_source == "venue" and a.side_source == "prints"
    assert a.wide_sell_flow_h == 0 and math.isinf(a.improve_round_trip_h)
    s.close()


def test_streamed_markets_with_a_sparse_counter_still_see_wide_trading_in_their_prints():
    """Takers trade only while the spread is 2 ticks; the counter is read once every 20 minutes, across narrow and
    wide stretches, so it cannot place that volume. The prints can."""
    ms = [_us("a"), _us("b")]
    s = _tape(ms, hours=4)
    for slug in ("a", "b"):
        counter, pending = 0, 0
        for k in range(1441):
            t = 10 * k
            wide = (t // 600) % 2 == 1
            if wide and k % 6 == 3:
                s.trade(f"{slug}|L", D("0.50"), D(10), Side.SELL, t - 3)
                s.trade(f"{slug}|L", D("0.52"), D(10), Side.BUY, t - 2)
                pending += 20
            if t % 1200 == 0:
                counter, pending = counter + pending, 0
            _row(s, t, f"{slug}|L", "0.50", "0.52" if wide else "0.51", 100, 100, str(counter))
    rows, info = measure_tape(s)
    a = _one(rows, "a|L")
    assert info["counter_ratio"] is not None and a.side_source == "prints" and a.counter_wide == 0
    total = a.sell_flow_h + a.buy_flow_h
    assert a.wide_sell_flow_h + a.wide_buy_flow_h == pytest.approx(2 * total, rel=0.1)   # all of it, in half the time
    assert a.improve_round_trip_h < 1 and not a.improve_bound
    s.close()


def test_calibration_starts_at_the_first_counter_reading_when_prints_start_earlier():
    ms = [_us("a"), _us("b")]
    s = _tape(ms, hours=3)
    for slug in ("a", "b"):
        counter = 0
        for k in range(1081):
            t = 10 * k
            if k % 6 == 3:
                s.trade(f"{slug}|L", D("0.50"), D(10), Side.SELL, t - 3)
                counter += 10
            _row(s, t, f"{slug}|L", "0.50", "0.51", 100, 100, str(counter) if t >= H else None)   # counter from 1 h
    rows, info = measure_tape(s)
    assert info["counter_ratio"] == pytest.approx(1.0, rel=0.02)
    s.close()


def test_fill_times_longer_than_the_market_was_watched_are_not_counted(capsys):
    s = _tape([_us("a"), _us("b"), _us("short")], hours=24)
    for slug in ("a", "b"):
        for k in range(721):
            t = 10 * k
            if k % 6 == 0 and k:
                s.trade(f"{slug}|L", D("0.50"), D(1), Side.SELL, t - 3)
                s.trade(f"{slug}|L", D("0.51"), D(1), Side.BUY, t - 2)
            _row(s, t, f"{slug}|L", "0.50", "0.51", 100, 100, str(2 * (k // 6)))
    _counter_market(s, "short", [k for k in range(721)], start=0.0, qty=2000)   # 2 h only, 360/h, deep queue
    rows, info = measure_tape(s)
    r = _one(rows, "short|L")
    assert r.ranked and r.hours == pytest.approx(2.0) and 2 < r.join_round_trip_h < 24
    print_report(rows, info)
    out = capsys.readouterr().out
    assert "> observed" in out
    text = " ".join(out.split())
    assert "when joining the queue within 1h in 0 and within 24h in 0;" in text   # a, b and short all exceed 2 h
    s.close()


# ------------------------------------------------------------------ second review: measure

def test_a_glitch_carried_onto_several_rows_is_not_a_restart():
    s = _tape([_us()], hours=1)
    vals = [1_000_000 + 30 * (k // 6) for k in range(361)]
    for k in range(180, 186):
        vals[k] = 0                                                     # one bad bbo reply rides on 6 rows
    _counter_market(s, "x", vals)
    rows, _ = measure_tape(s)
    r = _one(rows)
    assert r.counter_restarts == 0 and r.counter_total == pytest.approx(30 * 60)
    s.close()


def test_a_counter_that_stops_arriving_does_not_dilute_the_flow():
    s = _tape([_us()], hours=10)
    vals = [6 * k if k <= 360 else None for k in range(3601)]           # 2160/h for the first hour, then nothing
    _counter_market(s, "x", vals)
    rows, _ = measure_tape(s)
    r = _one(rows)
    assert r.counter_secs == pytest.approx(3600) and r.sell_flow_h + r.buy_flow_h == pytest.approx(2160, rel=0.01)
    s.close()


def test_a_cold_first_reading_is_not_counted_as_volume():
    s = _tape([_us()], hours=1)
    vals = [0] + [1_000_000 + 5 * k for k in range(360)]
    _counter_market(s, "x", vals)
    rows, info = measure_tape(s)
    r = _one(rows)
    assert r.counter_rebased == 1 and r.counter_total == pytest.approx(359 * 5) and info["rebased"] == 1
    s.close()


def test_a_print_rich_market_stays_ranked_when_its_counter_is_coarse():
    s = _calibrated_tape([_us("c")])
    counter = 0
    for k in range(721):
        t = 10 * k
        if 300 <= t < 480 and k % 2 == 0:
            s.trade("c|L", D("0.50"), D(5), Side.SELL, t - 3)
            s.trade("c|L", D("0.51"), D(5), Side.BUY, t - 2)
        if t in (360, 420, 480):
            counter += 30                                               # read once a minute: 3 moves
        _row(s, t, "c|L", "0.50", "0.51", 100, 100, str(counter))
    rows, info = measure_tape(s)
    c = _one(rows, "c|L")
    assert info["counter_ratio"] is not None and c.bursts >= 5 and c.counter_moves == 3
    assert c.flow_source == "venue" and c.ranked
    s.close()


def test_improve_times_are_compared_with_the_wide_time_observed(capsys):
    s = _calibrated_tape([_us("w")], hours=2)
    counter = 0
    for k in range(721):
        t = 10 * k
        wide = 3600 <= t < 3600 + 720                                   # wide for 12 minutes (10%)
        if wide and k % 6 == 3:
            counter += 2
        _row(s, t, "w|L", "0.50", "0.52" if wide else "0.51", 10000, 10000, str(counter))
    rows, info = measure_tape(s)
    w = _one(rows, "w|L")
    seen_wide = w.hours * w.can_improve
    assert w.improve_round_trip_h < math.inf
    assert w.within_observed("improve_round_trip_h") == (w.improve_round_trip_h <= seen_wide)
    s.close()


def test_a_counter_of_dollars_is_flagged(capsys):
    ms = [_us("a"), _us("b"), _us("c")]
    s = _tape(ms, hours=2)
    for slug, px in (("a", "0.20"), ("b", "0.50"), ("c", "0.80")):
        counter = Decimal(0)
        for k in range(721):
            t = 10 * k
            if k % 6 == 3:
                s.trade(f"{slug}|L", D(px), D(10), Side.SELL, t - 3)
                counter += 10 * D(px)                                   # the counter counts dollars
            bid = str(D(px) - D("0.01"))
            _row(s, t, f"{slug}|L", bid, px, 100, 100, str(counter))
    rows, info = measure_tape(s)
    assert info["counter_dollars"]
    print_report(rows, info)
    assert "follows dollars traded" in " ".join(capsys.readouterr().out.split())
    s.close()


def test_a_counter_of_contracts_is_not_flagged():
    ms = [_us("a"), _us("b")]
    s = _tape(ms, hours=2)
    for slug, px in (("a", "0.20"), ("b", "0.80")):
        counter = 0
        for k in range(721):
            t = 10 * k
            if k % 6 == 3:
                s.trade(f"{slug}|L", D(px), D(10), Side.SELL, t - 3)
                counter += 10
            _row(s, t, f"{slug}|L", str(D(px) - D("0.01")), px, 100, 100, str(counter))
    rows, info = measure_tape(s)
    assert info["counter_ratio"] == pytest.approx(1.0, rel=0.02) and not info["counter_dollars"]
    s.close()


def test_the_wide_spread_split_is_pulled_toward_even_too():
    """6 bursts, 3 a side; the market is wide half the time and only sellers happened to trade while it was."""
    s = _calibrated_tape([_us("c")])
    counter = 0
    plan = {65: "SELL", 125: "SELL", 185: "BUY", 245: "BUY", 305: "BUY", 365: "SELL"}   # row index -> taker
    for k in range(721):
        t = 10 * k
        wide = k >= 360                                                 # wide in the second hour
        if k in plan:
            side = Side.SELL if plan[k] == "SELL" else Side.BUY
            s.trade("c|L", D("0.50"), D(10), side, t - 3)
            counter += 10
        if wide and k in (420, 480):                                    # two more sellers while wide
            s.trade("c|L", D("0.50"), D(10), Side.SELL, t - 3)
            counter += 10
        _row(s, t, "c|L", "0.50", "0.52" if wide else "0.51", 100, 100, str(counter))
    rows, info = measure_tape(s)
    c = _one(rows, "c|L")
    assert info["counter_ratio"] is not None and c.side_source == "prints" and c.wide_buy_share == 0
    assert c.wide_buy_flow_h > 0 and c.improve_round_trip_h < math.inf   # not 'never' from 3 wide bursts
    s.close()
