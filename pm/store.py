"""SQLite tape: books, trades, intents, orders, fills, equity. Everything the backtester needs."""

from __future__ import annotations

import json
import sqlite3
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from .models import Book, Fill, Market, Side

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
  condition_id TEXT PRIMARY KEY, question TEXT, slug TEXT, json TEXT, updated_ts REAL);
CREATE TABLE IF NOT EXISTS books (
  ts REAL, token_id TEXT, best_bid TEXT, best_ask TEXT, bid_sz TEXT, ask_sz TEXT, top TEXT);
CREATE INDEX IF NOT EXISTS ix_books_ts ON books(ts);
CREATE INDEX IF NOT EXISTS ix_books_tok ON books(token_id, ts);
CREATE TABLE IF NOT EXISTS trades (ts REAL, token_id TEXT, price TEXT, size TEXT, side TEXT);
CREATE INDEX IF NOT EXISTS ix_trades_ts ON trades(ts);
CREATE TABLE IF NOT EXISTS intents (
  ts REAL, strategy TEXT, tag TEXT, token_id TEXT, side TEXT, price TEXT, size TEXT,
  tif TEXT, post_only INTEGER, reason TEXT, accepted INTEGER, reject_reason TEXT);
CREATE TABLE IF NOT EXISTS orders (
  ts REAL, event TEXT, order_id TEXT, tag TEXT, strategy TEXT, token_id TEXT, side TEXT,
  price TEXT, size TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS fills (
  ts REAL, order_id TEXT, tag TEXT, strategy TEXT, token_id TEXT, side TEXT,
  price TEXT, size TEXT, fee TEXT, maker INTEGER);
CREATE TABLE IF NOT EXISTS equity (
  ts REAL, cash TEXT, equity TEXT, realised TEXT, unrealised TEXT, fees TEXT, day_pnl TEXT,
  open_orders INTEGER, positions TEXT);
CREATE TABLE IF NOT EXISTS events (ts REAL, kind TEXT, detail TEXT);
"""


class Store:
    def __init__(self, path: str | Path, book_snapshot_interval: float = 0.5, book_heartbeat: float = 10.0):
        p = Path(path)
        if str(p) != ":memory:":
            p.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(p), isolation_level=None, check_same_thread=False, timeout=10)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        self._buf: list[tuple[str, tuple]] = []
        self._last_book_ts: dict[str, float] = {}
        self._last_book_key: dict[str, tuple] = {}
        self._snap_interval = book_snapshot_interval
        self._heartbeat = book_heartbeat
        self._last_flush = time.time()

    # ------------------------------------------------------------------ writes (buffered)
    def _q(self, sql: str, params: tuple) -> None:
        self._buf.append((sql, params))
        if len(self._buf) >= 500 or time.time() - self._last_flush > 1.0:
            self.flush()

    def flush(self) -> None:
        if not self._buf:
            return
        buf, self._buf = self._buf, []
        self._db.execute("BEGIN")
        try:
            for sql, params in buf:
                self._db.execute(sql, params)
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise
        self._last_flush = time.time()

    def close(self) -> None:
        self.flush()
        self._db.close()

    def market(self, m: Market) -> None:
        payload = {
            "condition_id": m.condition_id, "question": m.question, "slug": m.slug,
            "tokens": [{"token_id": t.token_id, "outcome": t.outcome} for t in m.tokens],
            "neg_risk": m.neg_risk, "neg_risk_augmented": m.neg_risk_augmented,
            "event_id": m.event_id, "event_market_count": m.event_market_count,
            "tick_size": str(m.tick_size), "min_order_size": str(m.min_order_size),
            "fee_rate": str(m.fee_rate), "fee_exponent": str(m.fee_exponent), "fee_known": m.fee_known,
            "end_date": m.end_date.isoformat() if m.end_date else None,
            "liquidity_usd": str(m.liquidity_usd), "volume_24h_usd": str(m.volume_24h_usd), "tags": m.tags,
            "reward_rate_per_day": str(m.reward_rate_per_day), "reward_max_spread": str(m.reward_max_spread),
            "reward_min_size": str(m.reward_min_size),
        }
        self._q(
            "INSERT OR REPLACE INTO markets(condition_id, question, slug, json, updated_ts) VALUES (?,?,?,?,?)",
            (m.condition_id, m.question, m.slug, json.dumps(payload), time.time()),
        )

    def book(self, b: Book, force: bool = False) -> None:
        """Snapshot on top-of-book change (rate-limited), plus a heartbeat row every 10s."""
        now = time.time()
        key = (b.best_bid, b.best_ask, b.bid_size_at_touch, b.ask_size_at_touch)
        last_ts = self._last_book_ts.get(b.token_id, 0.0)
        if not force:
            unchanged = self._last_book_key.get(b.token_id) == key
            if unchanged and now - last_ts < self._heartbeat:
                return
            if not unchanged and now - last_ts < self._snap_interval:
                return
        self._last_book_ts[b.token_id] = now
        self._last_book_key[b.token_id] = key
        self._q(
            "INSERT INTO books(ts, token_id, best_bid, best_ask, bid_sz, ask_sz, top) VALUES (?,?,?,?,?,?,?)",
            (now, b.token_id, _s(b.best_bid), _s(b.best_ask), str(b.bid_size_at_touch), str(b.ask_size_at_touch),
             json.dumps(b.top(5))),
        )

    def trade(self, token_id: str, price: Decimal, size: Decimal, side: Side, ts: float) -> None:
        self._q("INSERT INTO trades(ts, token_id, price, size, side) VALUES (?,?,?,?,?)",
                (ts, token_id, str(price), str(size), side.value))

    def intent(self, it, accepted: bool, reject_reason: str = "") -> None:
        self._q(
            "INSERT INTO intents(ts, strategy, tag, token_id, side, price, size, tif, post_only, reason, accepted, reject_reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), it.strategy, it.tag, it.token_id, it.side.value, str(it.price), str(it.size),
             it.tif.value, int(it.post_only), it.reason, int(accepted), reject_reason),
        )

    def order_event(self, event: str, tag: str, strategy: str, token_id: str, side: Side,
                    price: Decimal, size: Decimal, order_id: str = "", note: str = "") -> None:
        self._q(
            "INSERT INTO orders(ts, event, order_id, tag, strategy, token_id, side, price, size, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), event, order_id, tag, strategy, token_id, side.value, str(price), str(size), note),
        )

    def fill(self, f: Fill) -> None:
        self._q(
            "INSERT INTO fills(ts, order_id, tag, strategy, token_id, side, price, size, fee, maker) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f.ts, f.order_id, f.tag, f.strategy, f.token_id, f.side.value, str(f.price), str(f.size), str(f.fee), int(f.maker)),
        )

    def equity(self, cash, equity, realised, unrealised, fees, day_pnl, open_orders: int, positions: dict[str, Any]) -> None:
        self._q(
            "INSERT INTO equity(ts, cash, equity, realised, unrealised, fees, day_pnl, open_orders, positions) VALUES (?,?,?,?,?,?,?,?,?)",
            (time.time(), str(cash), str(equity), str(realised), str(unrealised), str(fees), str(day_pnl), open_orders,
             json.dumps(positions)),
        )

    def event(self, kind: str, detail: str) -> None:
        self._q("INSERT INTO events(ts, kind, detail) VALUES (?,?,?)", (time.time(), kind, detail))

    # ------------------------------------------------------------------ reads
    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        self.flush()
        return self._db.execute(sql, params).fetchall()

    def mid_at(self, token_id: str, ts: float, before: float = 5.0, after: float = 15.0) -> Optional[Decimal]:
        """Midpoint from the book snapshot nearest to ts within [ts-before, ts+after]."""
        row = self._db.execute(
            "SELECT best_bid, best_ask FROM books WHERE token_id=? AND ts BETWEEN ? AND ? "
            "AND best_bid IS NOT NULL AND best_ask IS NOT NULL ORDER BY ABS(ts-?) LIMIT 1",
            (token_id, ts - before, ts + after, ts),
        ).fetchone()
        if not row:
            return None
        return (Decimal(row[0]) + Decimal(row[1])) / 2

    def markouts(self, horizons: tuple[int, ...] = (30, 300), since_ts: float = 0.0) -> dict[str, Any]:
        """Post-fill price drift: the adverse-selection measurement.

        For each fill: capture = sign * (mid_at_fill - price)      (what the spread paid us)
                       drift_h = sign * (mid_at(t+h) - mid_at_fill) (what the market did next)
                       net_h   = capture + drift_h
        sign is +1 for buys, -1 for sells. All in cents per share, share-weighted.
        A persistently negative drift means we are being picked off in that market.
        """
        self.flush()
        rows = self._db.execute(
            "SELECT ts, strategy, token_id, side, price, size, maker FROM fills WHERE ts >= ? ORDER BY ts",
            (since_ts,),
        ).fetchall()
        by_strat: dict[str, dict[str, Any]] = {}
        by_token: dict[str, dict[str, Any]] = {}

        def bucket(d: dict, key: str) -> dict[str, Any]:
            return d.setdefault(key, {"fills": 0, "shares": Decimal(0), "capture": Decimal(0),
                                      **{f"drift_{h}": Decimal(0) for h in horizons},
                                      **{f"n_{h}": Decimal(0) for h in horizons}})

        for ts, strat, tok, side, price, size, maker in rows:
            price, size = Decimal(price), Decimal(size)
            sign = Decimal(1) if side == "BUY" else Decimal(-1)
            mid0 = self.mid_at(tok, ts)
            if mid0 is None:
                continue
            capture = sign * (mid0 - price) * size
            for b in (bucket(by_strat, strat), bucket(by_token, tok)):
                b["fills"] += 1
                b["shares"] += size
                b["capture"] += capture
            for h in horizons:
                midh = self.mid_at(tok, ts + h, before=2.0, after=max(15.0, h * 0.5))
                if midh is None:
                    continue
                drift = sign * (midh - mid0) * size
                for b in (bucket(by_strat, strat), bucket(by_token, tok)):
                    b[f"drift_{h}"] += drift
                    b[f"n_{h}"] += size

        def finish(key: str, b: dict[str, Any]) -> dict[str, Any]:
            sh = b["shares"] or Decimal(1)
            out = {"key": key, "fills": b["fills"], "shares": str(b["shares"]),
                   "capture_c": float(b["capture"] / sh * 100)}
            for h in horizons:
                n = b[f"n_{h}"] or Decimal(1)
                d = b[f"drift_{h}"] / n * 100
                out[f"drift_{h}s_c"] = float(d)
                out[f"net_{h}s_c"] = float(b["capture"] / sh * 100 + d)
                out[f"covered_{h}s"] = str(b[f"n_{h}"])
            return out

        strat_rows = [finish(k, b) for k, b in by_strat.items()]
        tok_rows = [finish(k, b) for k, b in by_token.items()]
        last_h = f"net_{horizons[-1]}s_c"
        tok_rows.sort(key=lambda r: r[last_h])
        return {
            "horizons_s": list(horizons),
            "fills_measured": sum(b["fills"] for b in by_strat.values()),
            "by_strategy": strat_rows,
            "worst_tokens": tok_rows[:5],
            "best_tokens": list(reversed(tok_rows[-5:])),
            "units": "cents per share, share-weighted; capture = spread earned at fill, drift = mid move after",
        }

    def summary(self, since_hours: float = 0.0) -> dict[str, Any]:
        self.flush()
        since_ts = time.time() - since_hours * 3600 if since_hours > 0 else 0.0
        q = lambda sql, p=(): self._db.execute(sql, p).fetchone()  # noqa: E731
        out: dict[str, Any] = {}
        out["books_rows"] = q("SELECT COUNT(*) FROM books")[0]
        out["trades_rows"] = q("SELECT COUNT(*) FROM trades")[0]
        out["markets"] = q("SELECT COUNT(*) FROM markets")[0]
        out["intents"] = q("SELECT COUNT(*) FROM intents")[0]
        out["intents_rejected"] = q("SELECT COUNT(*) FROM intents WHERE accepted=0")[0]
        out["orders_placed"] = q("SELECT COUNT(*) FROM orders WHERE event='placed'")[0]
        out["fills"] = q("SELECT COUNT(*) FROM fills")[0]
        span = q("SELECT MIN(ts), MAX(ts) FROM books")
        out["tape_from"], out["tape_to"] = span[0], span[1]
        last = q("SELECT ts, cash, equity, realised, unrealised, fees, day_pnl, open_orders FROM equity ORDER BY ts DESC LIMIT 1")
        if last:
            out["last_equity"] = dict(zip(["ts", "cash", "equity", "realised", "unrealised", "fees", "day_pnl", "open_orders"], last))
        rej = self._db.execute(
            "SELECT reject_reason, COUNT(*) FROM intents WHERE accepted=0 GROUP BY reject_reason ORDER BY 2 DESC LIMIT 8").fetchall()
        out["top_reject_reasons"] = rej
        by_strat = self._db.execute(
            "SELECT strategy, COUNT(*), SUM(CAST(size AS REAL)*CAST(price AS REAL)), SUM(maker) FROM fills GROUP BY strategy").fetchall()
        out["fills_by_strategy"] = [{"strategy": s, "fills": n, "notional": v, "maker": mk} for s, n, v, mk in by_strat]
        out["markouts"] = self.markouts(since_ts=since_ts)
        return out


def _s(v: Optional[Decimal]) -> Optional[str]:
    return None if v is None else str(v)
