"""The loop. One state, one tick, one execution path; the venue is pluggable."""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from .alerts import Alerts
from .config import Config
from .execution import Exchange
from .execution.paper import PaperExchange
from .feed import Trade
from .log import get_logger
from .models import ZERO, Book, Mode, Position
from .oms import OMS
from .risk import RiskGate
from .state import MarketState, Portfolio, build_context
from .store import Store
from .strategy import Strategy, build_strategies
from .venues import Venue, make_venue

log = get_logger(__name__)

ACCESS_INTERVAL = 300
EQUITY_INTERVAL = 10
STATS_INTERVAL = 60
BALANCE_INTERVAL = 30
POSITIONS_INTERVAL = 60


class Engine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.mode = cfg.mode
        self.ms = MarketState()
        self.pf = Portfolio(cash=cfg.execution.paper_starting_cash_usd if self.mode is Mode.PAPER else ZERO)
        self.store = Store(cfg.db_path)
        self.venue: Venue = make_venue(cfg)
        self.strategies: list[Strategy] = build_strategies(cfg.strategies)
        self.risk = RiskGate(cfg.risk, self.mode)
        self.exchange: Exchange = self.venue.make_exchange(self.mode, self.ms.market_for)
        self.oms = OMS(self.exchange, cfg.execution, self.store)
        self.alerts = Alerts(cfg.secrets.telegram_bot_token, cfg.secrets.telegram_chat_id)
        self._stop = asyncio.Event()
        self._halted = False
        self._feed_down_since: Optional[float] = None
        self._feed_down_alerted = False
        self._loss_halt_alerted = False
        self._last_heartbeat = time.time()
        self.ticks = 0

    # ------------------------------------------------------------------ feed callbacks
    def _on_book(self, book: Book) -> None:
        self.ms.update_book(book)
        self.store.book(book)
        if isinstance(self.exchange, PaperExchange):
            self.exchange.on_book(book)

    def _on_trade(self, t: Trade) -> None:
        self.ms.last_trade[t.token_id] = (t.price, t.ts)
        self.store.trade(t.token_id, t.price, t.size, t.side, t.ts)
        if isinstance(self.exchange, PaperExchange):
            self.exchange.on_trade(t)

    # ------------------------------------------------------------------ lifecycle
    async def run(self) -> None:
        log.info("engine_starting", venue=self.venue.name, mode=self.mode.value,
                 strategies=[s.name for s in self.strategies])
        try:
            await self._startup()
            await self._loop()
        finally:
            await self._shutdown()

    def request_stop(self) -> None:
        self._stop.set()

    async def _startup(self) -> None:
        await self._check_access()
        await self._refresh_universe()
        await self._seed_books()
        self.venue.start_feed(self.ms.token_ids, self._on_book, self._on_trade)
        await self.exchange.start()
        await self.venue.after_exchange_start(self.exchange)
        if self.mode is Mode.LIVE:
            bal = await self.exchange.balance()
            if bal is not None:
                self.pf.cash = bal
            await self._sync_positions()
        self.pf.roll_day(self.ms.books)
        self.store.event("start", f"venue={self.venue.name} mode={self.mode.value} markets={len(self.ms.markets)}")
        log.info("engine_started", venue=self.venue.name, mode=self.mode.value, markets=len(self.ms.markets),
                 tokens=len(self.ms.token_ids), cash=str(self.pf.cash), alerts=self.alerts.enabled)
        self.alerts.fire(f"pm started: venue={self.venue.name} mode={self.mode.value} "
                         f"markets={len(self.ms.markets)} cash={self.pf.cash:.2f}")

    async def _shutdown(self) -> None:
        log.info("engine_stopping")
        try:
            if self.mode is not Mode.READ_ONLY:
                await self.oms.cancel_all()
                await self.exchange.stop()
        finally:
            await self.venue.stop()
            self.store.event("stop", f"ticks={self.ticks}")
            self.store.close()
            await self.alerts.send(f"pm stopped: ticks={self.ticks} equity={self.pf.equity(self.ms.books):.2f}")
            await self.alerts.aclose()
        log.info("engine_stopped", ticks=self.ticks)

    # ------------------------------------------------------------------ health
    def _check_health(self, now: float) -> None:
        a = self.cfg.alerts
        pfc = self.venue.private_feed_connected
        if pfc is not None and hasattr(self.exchange, "user_feed_ok"):
            self.exchange.user_feed_ok = pfc
        if self.venue.feed_connected or not self.ms.token_ids:
            if self._feed_down_alerted:
                self.alerts.fire("pm: market feed reconnected", key="feed_up")
            self._feed_down_since, self._feed_down_alerted = None, False
        else:
            self._feed_down_since = self._feed_down_since or now
            if not self._feed_down_alerted and now - self._feed_down_since > a.feed_down_seconds:
                self._feed_down_alerted = True
                log.error("feed_down", seconds=int(now - self._feed_down_since))
                self.alerts.fire(f"pm: market feed down for {int(now - self._feed_down_since)}s", key="feed_down")
        if a.heartbeat_hours > 0 and now - self._last_heartbeat > a.heartbeat_hours * 3600:
            self._last_heartbeat = now
            self.alerts.fire(
                f"pm alive: ticks={self.ticks} equity={self.pf.equity(self.ms.books):.2f} "
                f"day_pnl={self.pf.day_pnl(self.ms.books):.2f} placed={self.oms.placed}",
                key="heartbeat",
            )

    # ------------------------------------------------------------------ loop
    async def _loop(self) -> None:
        now = time.time()
        last_access = last_universe = last_equity = last_stats = last_balance = last_positions = now
        while not self._stop.is_set():
            t0 = time.time()
            try:
                if t0 - last_access > ACCESS_INTERVAL:
                    await self._check_access()
                    last_access = t0
                if t0 - last_universe > self.cfg.universe.refresh_seconds:
                    await self._refresh_universe()
                    await self.venue.set_feed_tokens(self.ms.token_ids)
                    last_universe = t0
                if self.mode is Mode.LIVE and t0 - last_balance > BALANCE_INTERVAL:
                    bal = await self.exchange.balance()
                    if bal is not None:
                        self.pf.cash = bal
                    last_balance = t0
                if self.mode is Mode.LIVE and t0 - last_positions > POSITIONS_INTERVAL:
                    await self._sync_positions()
                    last_positions = t0

                await self._tick()
                self._check_health(t0)

                if t0 - last_equity > EQUITY_INTERVAL:
                    self._snapshot_equity()
                    last_equity = t0
                if t0 - last_stats > STATS_INTERVAL:
                    self._log_stats()
                    last_stats = t0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("tick_error", error=str(e)[:200])
            elapsed = time.time() - t0
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(0.05, self.cfg.tick_seconds - elapsed))
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        self.ticks += 1
        self.pf.roll_day(self.ms.books)

        # Kill switch: cancel everything once, then idle until the file is removed.
        if self.risk.kill_switch_active():
            if not self._halted:
                self._halted = True
                log.critical("kill_switch_engaged", file=self.cfg.risk.kill_switch_file)
                self.store.event("kill_switch", "engaged")
                self.alerts.fire("pm: KILL switch engaged, all orders cancelled", key="kill_on")
                if self.mode is not Mode.READ_ONLY:
                    await self.oms.cancel_all()
            return
        if self._halted:
            self._halted = False
            log.warning("kill_switch_released")
            self.store.event("kill_switch", "released")
            self.alerts.fire("pm: KILL switch released, resuming", key="kill_off")

        # Fills first, so strategies see current inventory.
        for f in await self.oms.drain_fills():
            self.pf.apply_fill(f)
            log.info("fill", strategy=f.strategy, side=f.side.value, token=f.token_id[:24], size=str(f.size),
                     price=str(f.price), fee=str(f.fee), maker=f.maker)
            if self.cfg.alerts.on_fills:
                m = self.ms.market_for(f.token_id)
                q = (m.question[:50] if m else f.token_id[:24])
                self.alerts.fire(f"fill {f.strategy} {f.side.value} {f.size}@{f.price} {q}", key=f"fill:{f.order_id}")

        day_pnl = self.pf.day_pnl(self.ms.books)
        if day_pnl <= -self.cfg.risk.daily_loss_limit_usd:
            if not self._loss_halt_alerted:
                self._loss_halt_alerted = True
                log.error("daily_loss_limit", day_pnl=str(day_pnl))
                self.alerts.fire(f"pm: daily loss limit hit (day_pnl={day_pnl:.2f}); new risk blocked", key="loss_halt")
        else:
            self._loss_halt_alerted = False

        open_orders = await self.exchange.open_orders()
        ctx = build_context(self.ms, self.pf, open_orders, {})

        intents = []
        for s in self.strategies:
            try:
                intents.extend(s.on_tick(ctx))
            except Exception as e:
                log.exception("strategy_error", strategy=s.name, error=str(e)[:200])

        rep = self.risk.evaluate(intents, ctx, self.ms.geoblock_ok)
        for it in rep.accepted:
            self.store.intent(it, True)
        for it, why in rep.rejected:
            self.store.intent(it, False, why)
            log.debug("intent_rejected", tag=it.tag, why=why)

        if self.mode is Mode.READ_ONLY:
            return  # strategies and risk run for the record only
        if rep.halted:
            if open_orders:
                log.warning("risk_halt", reason=rep.halt_reason)
                await self.oms.cancel_all()
            return
        await self.oms.sync(rep.accepted)

    # ------------------------------------------------------------------ helpers
    async def _check_access(self) -> None:
        try:
            ok, detail = await self.venue.check_access()
            self.ms.geoblock_ok, self.ms.geoblock_country = ok, detail
            log.info("access", allowed=ok, detail=detail)
        except Exception as e:
            self.ms.geoblock_ok = False
            log.warning("access_check_failed", error=str(e)[:200])

    async def _refresh_universe(self) -> None:
        try:
            markets = await self.venue.select_universe()
        except Exception as e:
            log.error("universe_refresh_failed", error=str(e)[:200])
            return
        if not markets:
            log.warning("universe_empty")
            return
        self.ms.set_markets(markets)
        for m in markets:
            self.store.market(m)

    async def _seed_books(self) -> None:
        """REST snapshot so strategies have books before the feed delivers."""
        try:
            books = await self.venue.seed_books(self.ms.token_ids)
        except Exception as e:
            log.warning("seed_books_failed", error=str(e)[:200])
            return
        for b in books.values():
            self._on_book(b)
        log.info("books_seeded", count=len(books))

    async def _sync_positions(self) -> None:
        """Replace local positions with the venue's view when the backend can provide it."""
        try:
            venue_pos = await self.exchange.positions()
        except Exception as e:
            log.warning("positions_sync_failed", error=str(e)[:200])
            return
        if venue_pos is None:
            return
        self.pf.positions = {
            tid: Position(tid, shares=shares, cost=cost) for tid, (shares, cost) in venue_pos.items() if shares > ZERO
        }
        log.info("positions_synced", count=len(self.pf.positions))

    def _snapshot_equity(self) -> None:
        eq = self.pf.equity(self.ms.books)
        self.store.equity(
            self.pf.cash, eq, self.pf.realised_pnl, self.pf.unrealised(self.ms.books), self.pf.fees_paid,
            self.pf.day_pnl(self.ms.books), len(self.oms._last_replace),
            {t: str(p.shares) for t, p in self.pf.positions.items() if p.shares > ZERO},
        )

    def _log_stats(self) -> None:
        fresh = sum(1 for b in self.ms.books.values() if b.age() < 30)
        log.info(
            "stats", venue=self.venue.name, ticks=self.ticks, feed=self.venue.feed_connected,
            msgs=self.venue.feed_messages, books=len(self.ms.books), fresh=fresh, cash=f"{self.pf.cash:.2f}",
            equity=f"{self.pf.equity(self.ms.books):.2f}", realised=f"{self.pf.realised_pnl:.2f}",
            fees=f"{self.pf.fees_paid:.2f}", placed=self.oms.placed, cancelled=self.oms.cancelled,
            rejected=self.oms.rejected, positions=sum(1 for p in self.pf.positions.values() if p.shares > ZERO),
            access=self.ms.geoblock_country[:40],
        )
