"""`pm measure`, checked against hand-computed values. Each test names the finding it pins (independent oracle and
review of f10f5d8)."""

import math
import os
import sqlite3
import subprocess
import sys
from decimal import Decimal

import pytest

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
            counter = 900                                       # restarted from zero, 900 traded since
            true_volume += 900
        elif k > 0:
            counter += 5
            true_volume += 5
        _row(s, t, "x|L", "0.50", "0.52", 100, 100, None if k % 2 else str(counter))   # every other row lacks it
    rows, _ = measure_tape(s)
    r = _one(rows)
    assert r.flow_source == "venue"
    assert r.sell_flow_h + r.buy_flow_h == pytest.approx(true_volume, rel=0.02)
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
    md = USMarketData.__new__(USMarketData)
    md._on_book = got.append
    md._last_traded = {}
    book = {"bids": [{"px": {"value": "0.5"}, "qty": "10"}], "offers": [{"px": {"value": "0.51"}, "qty": "10"}]}
    md._emit_books("x", {**book, "stats": {"sharesTraded": {"value": "123"}}})   # an Amount-shaped value
    md._emit_books("x", book)                                                     # no stats this time
    assert [b.traded_total for b in got] == [D(123), D(123), D(123), D(123)]
    long_b, _ = books_from_us("x", {"marketData": {**book, "stats": {"sharesTraded": "77"}}})
    assert long_b.traded_total == D(77)
