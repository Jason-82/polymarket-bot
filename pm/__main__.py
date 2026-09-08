"""CLI: python -m pm <command>

  probe     connectivity, geoblock, fee info and a live book for one market
  scan      one-shot structural scan across the universe (no trading)
  run       run the engine (mode from config.yaml or --mode)
  status    summarise the SQLite tape
  backtest  replay the recorded tape through the strategies
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from decimal import Decimal

from . import __version__
from .config import Config
from .log import get_logger, setup
from .models import ONE, Mode

log = get_logger("pm")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pm", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--venue", choices=["polymarket", "us"], help="override venue from config.yaml")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("probe")
    sp.add_argument("--condition-id", help="market to inspect (default: top-volume market); US: market slug")
    sp.add_argument("--ws-seconds", type=float, default=6.0)

    sub.add_parser("scan")

    sr = sub.add_parser("run")
    sr.add_argument("--mode", choices=[m.value for m in Mode])

    st = sub.add_parser("status")
    st.add_argument("--since-hours", type=float, default=0.0, help="restrict markouts/equity to this window")

    sb = sub.add_parser("backtest")
    sb.add_argument("--since-hours", type=float, default=24.0)
    sb.add_argument("--tick-seconds", type=float, default=5.0)

    args = p.parse_args(argv)
    cfg = Config.load(args.config)
    if args.venue:
        cfg.venue = "polymarket_us" if args.venue == "us" else "polymarket"
    if args.cmd == "run" and args.mode:
        cfg.mode = Mode(args.mode)
    setup(cfg.log_level)
    is_us = cfg.venue.replace("-", "_") in ("us", "polymarket_us", "polymarketus")

    try:
        if args.cmd == "probe":
            if is_us:
                return asyncio.run(_probe_us(cfg, args.condition_id, args.ws_seconds))
            return asyncio.run(_probe(cfg, args.condition_id, args.ws_seconds))
        if args.cmd == "scan":
            if is_us:
                return asyncio.run(_scan_us(cfg))
            return asyncio.run(_scan(cfg))
        if args.cmd == "run":
            return _run(cfg)
        if args.cmd == "status":
            return _status(cfg, args.since_hours)
        if args.cmd == "backtest":
            from .backtest import run_backtest
            return run_backtest(cfg, since_hours=args.since_hours, tick_seconds=args.tick_seconds)
    except KeyboardInterrupt:
        return 130
    return 0


# ---------------------------------------------------------------------- commands

async def _probe(cfg: Config, condition_id: str | None, ws_seconds: float) -> int:
    from .clob import Clob, Geoblock
    from .feed import MarketFeed
    from .gamma import Gamma

    clob, gamma = Clob(cfg.clob_url), Gamma(cfg.gamma_url)
    try:
        print(f"CLOB /ok ............ {'OK' if await clob.ok() else 'FAILED'}")
        try:
            ok, country = await Geoblock(cfg.geoblock_url).check()
            print(f"Geoblock ............ {'allowed' if ok else 'BLOCKED'} (country={country})")
        except Exception as e:
            print(f"Geoblock ............ check failed: {e}")

        if condition_id:
            markets = await gamma.markets_by_condition([condition_id])
        else:
            markets = await gamma.active_markets(limit=5)
        if not markets:
            print("Gamma ............... no markets returned")
            return 1
        m = await clob.enrich_market(markets[0])
        print(f"Gamma ............... OK ({len(markets)} market(s) fetched)")
        print(f"\nMarket: {m.question}\n  condition_id={m.condition_id}\n  slug={m.slug}")
        print(f"  neg_risk={m.neg_risk} tick={m.tick_size} min_size={m.min_order_size} end={m.end_date}")
        print(f"  fee_rate={m.fee_rate} exponent={m.fee_exponent} known={m.fee_known} tags={m.tags}")
        print(f"  liquidity=${m.liquidity_usd:,.0f} volume24h=${m.volume_24h_usd:,.0f}")
        for t in m.tokens:
            b = await clob.book(t.token_id)
            print(f"\n  [{t.outcome}] token={t.token_id[:16]}…  bid={b.best_bid} ask={b.best_ask} spread={b.spread}")
            for lvl in reversed(b.asks[:5]):
                print(f"      ask {lvl.price}  x {lvl.size}")
            for lvl in b.bids[:5]:
                print(f"      bid {lvl.price}  x {lvl.size}")

        counts = {"book": 0, "trade": 0}
        feed = MarketFeed(cfg.ws_url, lambda b: counts.__setitem__("book", counts["book"] + 1),
                          lambda t: counts.__setitem__("trade", counts["trade"] + 1))
        feed.start([t.token_id for t in m.tokens])
        await asyncio.sleep(ws_seconds)
        await feed.stop()
        print(f"\nWebSocket ........... connected={feed.connected or feed.messages > 0} "
              f"messages={feed.messages} book_updates={counts['book']} trades={counts['trade']} in {ws_seconds:.0f}s")
        return 0
    finally:
        await clob.aclose()
        await gamma.aclose()


async def _scan(cfg: Config) -> int:
    from .clob import Clob
    from .fees import taker_fee_per_share, WORST_CASE_RATE
    from .gamma import Gamma
    from .state import MarketState, Portfolio, build_context
    from .strategy.complete_set import CompleteSet
    from .universe import Universe

    clob, gamma = Clob(cfg.clob_url), Gamma(cfg.gamma_url)
    try:
        markets = await Universe(gamma, clob, cfg.universe).select()
        ms = MarketState()
        ms.set_markets(markets)
        books = await clob.books(ms.token_ids)
        for b in books.values():
            ms.update_book(b)

        print(f"\n{'market':60} {'yes_ask':>7} {'no_ask':>7} {'sum':>6} {'fee':>6} {'net':>6} {'spr':>5}")
        rows = []
        for m in markets:
            by, bn = ms.books.get(m.yes.token_id), ms.books.get(m.no.token_id)
            if not by or not bn or by.best_ask is None or bn.best_ask is None:
                continue
            rate = m.fee_rate if m.fee_known else WORST_CASE_RATE
            fee = taker_fee_per_share(by.best_ask, rate, m.fee_exponent) + taker_fee_per_share(bn.best_ask, rate, m.fee_exponent)
            s = by.best_ask + bn.best_ask
            rows.append((s + fee, m.question[:60], by.best_ask, bn.best_ask, s, fee, by.spread))
        for net, q, ay, an, s, fee, spr in sorted(rows, key=lambda r: r[0]):
            flag = "  <-- ARB" if net < ONE else ""
            print(f"{q:60} {ay:>7} {an:>7} {s:>6.3f} {fee:>6.3f} {net:>6.3f} {spr!s:>5}{flag}")

        params = cfg.strategy("complete_set") or {"margin": 0.0}
        params["margin"] = params.get("margin", 0.0)
        ctx = build_context(ms, Portfolio(), [], {})
        intents = CompleteSet(params).on_tick(ctx)
        print(f"\n{len(intents)} actionable leg(s) at margin {params['margin']} across {len(rows)} markets.")
        for it in intents:
            print(f"  {it.tag}: BUY {it.size} @ {it.price}  ({it.reason})")
        return 0
    finally:
        await clob.aclose()
        await gamma.aclose()


async def _probe_us(cfg: Config, slug: str | None, ws_seconds: float) -> int:
    """Read-only look at the Polymarket US venue. Prints raw field names so we can verify the schema."""
    from .venues.polymarket_us import PolymarketUSVenue, books_from_us, market_from_us

    v = PolymarketUSVenue(cfg)
    try:
        ok, detail = await v.check_access()
        print(f"Access .............. {'OK' if ok else 'FAILED'} ({detail})")

        events = await v.list_events_raw(limit=3)
        if not events:
            print("Events .............. none returned")
            return 1
        ev = events[0]
        print(f"Events .............. OK (top event: {ev.get('title')!r}, keys={sorted(ev.keys())})")
        mk_rows = ev.get("markets") or []
        print(f"  nested market keys: {sorted(mk_rows[0].keys()) if mk_rows else 'none'}")
        print(f"  endTime={ev.get('endTime')} tags={[t.get('label') for t in ev.get('tags') or []]} "
              f"series={(ev.get('series') or {}).get('slug')}")

        if slug:
            resp = await v.public.markets.retrieve_by_slug(slug)
            row = (resp or {}).get("market") or {}
        else:
            row = mk_rows[0] if mk_rows else {}
            slug = row.get("slug")
        if not slug:
            print("Market .............. none")
            return 1
        full = (await v.public.markets.retrieve_by_slug(slug) or {}).get("market") or row
        print(f"\nMarket: {full.get('title')}  slug={slug}")
        keep = {k: full.get(k) for k in ("outcomes", "outcomePrices", "bestBidQuote", "bestAskQuote", "status",
                                         "marketType", "category", "tags", "feeCoefficient", "minimumTradeQty",
                                         "orderPriceMinTickSize", "endDate", "sportsMarketType", "gameStartTime",
                                         "marketSides", "subject", "question", "titleShort")}
        print(f"  raw values: {json.dumps(keep, default=str)[:1500]}")
        ev_keep = {k: ev.get(k) for k in ("category", "tags", "endDate", "seriesSlug", "teams", "marketCounts", "period")}
        print(f"  event values: {json.dumps(ev_keep, default=str)[:600]}")
        m = market_from_us(full, ev)
        print(f"  mapped: question={m.question!r} outcome={m.yes.outcome} end={m.end_date} tags={m.tags} "
              f"tick={m.tick_size} min_qty={m.min_order_size} accepting={m.accepting_orders} "
              f"fee_rate={m.fee_rate} rebate={m.maker_rebate_rate}")

        data = await v.public.markets.book(slug)
        inner = (data or {}).get("marketData") if isinstance((data or {}).get("marketData"), dict) else (data or {})
        print(f"  book keys: {sorted((data or {}).keys())} inner={sorted(inner.keys())} state={inner.get('state')}")
        long_b, short_b = books_from_us(slug, data or {})
        for name, b in (("LONG " + str(m.yes.outcome), long_b), ("SHORT (mirrored)", short_b)):
            print(f"\n  [{name}] bid={b.best_bid} ask={b.best_ask} spread={b.spread}")
            for lvl in reversed(b.asks[:5]):
                print(f"      ask {lvl.price}  x {lvl.size}")
            for lvl in b.bids[:5]:
                print(f"      bid {lvl.price}  x {lvl.size}")

        s = cfg.secrets
        if s.us_key_id and s.us_secret_key:
            try:
                bal = await v.public.account.balances()
                print(f"\nBalances ............ {bal}")
            except Exception as e:
                print(f"\nBalances ............ failed: {str(e)[:200]}")
            # Order preview is read-only: nothing is placed. It tells us how the venue interprets prices.
            for intent in ("ORDER_INTENT_BUY_LONG", "ORDER_INTENT_BUY_SHORT"):
                req = {"marketSlug": slug, "intent": intent, "type": "ORDER_TYPE_LIMIT",
                       "price": {"value": "0.02", "currency": "USD"}, "quantity": 1,
                       "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL", "participateDontInitiate": True}
                try:
                    pv = await v.public.orders.preview({"request": req})
                    print(f"Preview {intent[13:]:10} -> {pv}")
                except Exception as e:
                    print(f"Preview {intent[13:]:10} -> failed: {str(e)[:200]}")

            counts = {"book": 0, "trade": 0}
            v.start_feed([m.yes.token_id], lambda b: counts.__setitem__("book", counts["book"] + 1),
                         lambda t: counts.__setitem__("trade", counts["trade"] + 1))
            await asyncio.sleep(ws_seconds)
            print(f"\nMarket data ......... ws_connected={v.data.connected} messages={v.data.messages} "
                  f"book_updates={counts['book']} trades={counts['trade']} in {ws_seconds:.0f}s")
        else:
            print("\nNo PM_US_KEY_ID / PM_US_SECRET_KEY in .env: skipped balances, preview and WebSocket.")
        return 0
    finally:
        await v.stop()


async def _scan_us(cfg: Config) -> int:
    from .venues.polymarket_us import PolymarketUSVenue

    v = PolymarketUSVenue(cfg)
    try:
        markets = await v.select_universe()
        books = await v.seed_books([t.token_id for m in markets for t in m.tokens])
        print(f"\n{'market':64} {'bid':>5} {'ask':>5} {'spr':>5} {'bidsz':>7} {'asksz':>7} {'liq$':>9} {'end':>10} tags")
        for m in markets:
            b = books.get(m.yes.token_id)
            if not b:
                continue
            end = m.end_date.strftime("%Y-%m-%d") if m.end_date else "?"
            print(f"{m.question[:64]:64} {b.best_bid!s:>5} {b.best_ask!s:>5} {b.spread!s:>5} "
                  f"{b.bid_size_at_touch!s:>7} {b.ask_size_at_touch!s:>7} {m.liquidity_usd:>9,.0f} {end:>10} {','.join(m.tags)[:30]}")
        quotable = sum(1 for m in markets if (b := books.get(m.yes.token_id)) and b.spread and b.spread >= Decimal("0.02"))
        print(f"\n{len(markets)} markets selected; {quotable} with spread >= 0.02; "
              f"{sum(1 for m in markets if m.end_date)} with a known end date.")
        return 0
    finally:
        await v.stop()


def _run(cfg: Config) -> int:
    from .engine import Engine

    if cfg.mode is Mode.LIVE and not cfg.secrets.live_trading_ack:
        print("Refusing to start LIVE: set LIVE_TRADING=yes in .env after reading README 'Going live'.")
        return 2
    print(f"venue={cfg.venue} mode={cfg.mode.value}  (Ctrl+C to stop; create a file named "
          f"{cfg.risk.kill_switch_file} to halt without stopping)")
    engine = Engine(cfg)

    async def _main():
        task = asyncio.create_task(engine.run())
        try:
            await task
        except asyncio.CancelledError:
            engine.request_stop()
            await task

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        # Windows: no signal handlers; asyncio.run has already cancelled the task and awaited shutdown.
        print("\nstopped.")
    return 0


def _status(cfg: Config, since_hours: float = 0.0) -> int:
    from .store import Store

    s = Store(cfg.db_path)
    try:
        summary = s.summary(since_hours=since_hours)
    finally:
        s.close()
    mk = summary.pop("markouts", None)
    print(json.dumps(summary, indent=2, default=str))
    if mk and mk.get("fills_measured"):
        hz = mk["horizons_s"]
        print(f"\nMarkouts ({mk['units']})")
        print(f"{'strategy/token':40} {'fills':>5} {'shares':>8} {'capture':>8} " +
              " ".join(f"{'drift'+str(h)+'s':>9} {'net'+str(h)+'s':>8}" for h in hz))
        for section, rows in (("by strategy", mk["by_strategy"]), ("worst tokens", mk["worst_tokens"]),
                              ("best tokens", mk["best_tokens"])):
            print(f"  -- {section}")
            for r in rows:
                cells = " ".join(f"{r[f'drift_{h}s_c']:>9.2f} {r[f'net_{h}s_c']:>8.2f}" for h in hz)
                print(f"  {r['key'][:38]:38} {r['fills']:>5} {r['shares']:>8} {r['capture_c']:>8.2f} {cells}")
    elif mk is not None:
        print("\nMarkouts: no fills measured yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
