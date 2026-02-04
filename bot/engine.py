"""Main trading engine with event loop."""

import asyncio
import signal
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from bot.config import BotMode, Config
from bot.state import BotState
from bot.universe import UniverseSelector, UniverseFilters
from connectors.geoblock import GeoblockChecker, GeoblockStatus
from connectors.gamma_client import GammaClient
from connectors.clob_rest_client import ClobRestClient
from connectors.clob_ws_client import ClobWebSocketClient
from storage.database import Database
from storage.dao import MarketDAO, OrderBookDAO, PositionDAO, FillDAO
from storage.models import OrderBook, Market
from strategies.base import StrategyBase, StrategyContext
from strategies.loader import load_strategies_from_config
from risk.manager import RiskManager
from risk.kill_switch import KillSwitch, KillSwitchReason
from execution.oms import OrderManagementSystem
from monitoring.logger import get_logger, setup_logging
from monitoring.alerts import AlertManager

logger = get_logger(__name__)


@dataclass
class EngineMetrics:
    """Runtime metrics for the engine."""
    started_at: Optional[datetime] = None
    ticks: int = 0
    orderbook_updates: int = 0
    orders_placed: int = 0
    fills: int = 0
    errors: int = 0
    last_tick_at: Optional[datetime] = None
    ws_connected: bool = False
    ws_disconnected_since: Optional[datetime] = None


class TradingEngine:
    """
    Main trading engine.

    Orchestrates:
    - Market data ingestion (Gamma, CLOB REST/WS)
    - Strategy execution
    - Risk management
    - Order management
    - Storage persistence
    """

    def __init__(self, config: Config):
        self.config = config
        self.state = BotState()
        self.metrics = EngineMetrics()

        # Initialize components
        self.db = Database(config.database_path)
        self.market_dao = MarketDAO(self.db)
        self.orderbook_dao = OrderBookDAO(self.db)
        self.position_dao = PositionDAO(self.db)
        self.fill_dao = FillDAO(self.db)

        self.geoblock = GeoblockChecker(url=config.api.geoblock_url)
        self.gamma = GammaClient()
        self.clob_rest = ClobRestClient()
        self.clob_ws = ClobWebSocketClient(
            on_orderbook_update=self._on_orderbook_update,
        )

        self.universe = UniverseSelector(
            UniverseFilters.from_dict(config.universe_filters)
            if config.universe_filters else None
        )

        self.risk = RiskManager(config.risk)
        self.kill_switch = KillSwitch(base_path=Path("."))
        self.oms = OrderManagementSystem(
            mode=config.mode.value,
            min_update_interval_ms=config.risk.min_order_update_interval_ms,
            price_epsilon=config.risk.price_epsilon,
        )
        self.alerts = AlertManager(
            telegram_bot_token=config.telegram_bot_token,
            telegram_chat_id=config.telegram_chat_id,
            slack_webhook_url=config.slack_webhook_url,
        )

        # Strategies
        self.strategies: List[StrategyBase] = []

        # Control
        self._running = False
        self._shutdown_event = asyncio.Event()

    async def start(self) -> None:
        """Start the trading engine."""
        logger.info(
            "engine_starting",
            mode=self.config.mode.value,
            database=self.config.database_path,
        )

        # Initialize database
        self.db.initialize()

        # Check geoblock
        await self._check_geoblock()

        # Load market universe
        await self._refresh_market_universe()

        # Load strategies
        self.strategies = load_strategies_from_config(
            [s.__dict__ for s in self.config.strategies]
        )
        logger.info("engine_strategies_loaded", count=len(self.strategies))

        # Start strategies
        context = self._build_strategy_context()
        for strategy in self.strategies:
            try:
                await strategy.start(context)
                logger.info("engine_strategy_started", strategy=strategy.name)
            except Exception as e:
                logger.error("engine_strategy_start_failed", strategy=strategy.name, error=str(e))

        # Connect to WebSocket
        if self.state.active_token_ids:
            await self._connect_websocket()

        # Setup signal handlers
        self._setup_signal_handlers()

        # Register kill switch callback
        self.kill_switch.on_trigger(self._on_kill_switch)

        self.metrics.started_at = datetime.utcnow()
        self._running = True

        logger.info(
            "engine_started",
            mode=self.config.mode.value,
            active_tokens=len(self.state.active_token_ids),
            strategies=len(self.strategies),
        )

        # Start main loop
        await self._main_loop()

    async def stop(self) -> None:
        """Stop the trading engine gracefully."""
        logger.info("engine_stopping")
        self._running = False
        self._shutdown_event.set()

        # Stop strategies
        for strategy in self.strategies:
            try:
                await strategy.stop()
                logger.info("engine_strategy_stopped", strategy=strategy.name)
            except Exception as e:
                logger.error("engine_strategy_stop_failed", strategy=strategy.name, error=str(e))

        # Cancel all orders if in trading mode
        if self.config.mode in (BotMode.PAPER, BotMode.LIVE):
            await self.oms.cancel_all()

        # Disconnect WebSocket
        await self.clob_ws.disconnect()

        logger.info("engine_stopped")

    async def _main_loop(self) -> None:
        """Main event loop."""
        tick_interval = self.config.tick_interval
        geoblock_interval = self.config.geoblock_check_interval
        universe_interval = self.config.universe_refresh_interval
        position_interval = self.config.position_reconcile_interval

        last_geoblock = datetime.utcnow()
        last_universe = datetime.utcnow()
        last_position = datetime.utcnow()

        while self._running:
            try:
                tick_start = datetime.utcnow()

                # Check kill switch
                self.kill_switch.check()
                if self.kill_switch.is_active:
                    await self._handle_kill_switch()
                    await asyncio.sleep(tick_interval)
                    continue

                # Periodic geoblock check
                if (tick_start - last_geoblock).total_seconds() > geoblock_interval:
                    await self._check_geoblock()
                    last_geoblock = tick_start

                # Periodic universe refresh
                if (tick_start - last_universe).total_seconds() > universe_interval:
                    await self._refresh_market_universe()
                    last_universe = tick_start

                # Periodic position reconciliation
                if (tick_start - last_position).total_seconds() > position_interval:
                    await self._reconcile_positions()
                    last_position = tick_start

                # Run strategy tick
                await self._tick()

                # Update metrics
                self.metrics.ticks += 1
                self.metrics.last_tick_at = tick_start

                # Sleep until next tick
                elapsed = (datetime.utcnow() - tick_start).total_seconds()
                sleep_time = max(0, tick_interval - elapsed)
                await asyncio.sleep(sleep_time)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("engine_tick_error", error=str(e))
                self.metrics.errors += 1
                self.risk.record_error()
                await asyncio.sleep(tick_interval)

    async def _tick(self) -> None:
        """Execute one tick of the trading loop."""
        if self.config.mode == BotMode.READ_ONLY:
            # Just persist orderbook snapshots
            await self._persist_orderbook_snapshots()
            return

        # Build strategy context
        context = StrategyContext(
            timestamp=datetime.utcnow(),
            orderbooks=self.state.orderbooks.copy(),
            market_metadata=self.state.markets.copy(),
            token_to_market=self.state.token_to_market.copy(),
            positions=self.state.positions.copy(),
            open_orders=self.oms.get_open_orders(),
            account_balance=self.state.balance,
            mode=self.config.mode.value,
            config={},
        )

        # Run strategies
        all_intents = []
        for strategy in self.strategies:
            try:
                intents = await strategy.on_tick(context)
                all_intents.extend(intents)
            except Exception as e:
                logger.error(
                    "engine_strategy_error",
                    strategy=strategy.name,
                    error=str(e),
                )

        # Apply risk limits
        safe_intents, adjustments = self.risk.clamp_intents(
            intents=all_intents,
            positions=self.state.positions,
            open_orders=self.oms.get_open_orders(),
            geoblock_allowed=self.state.geoblock_allowed,
            mode=self.config.mode.value,
        )

        # Sync orders
        if safe_intents:
            # Build orderbook dict for paper fill simulation
            orderbook_dict = {
                tid: {"best_bid": ob.best_bid, "best_ask": ob.best_ask}
                for tid, ob in self.state.orderbooks.items()
            }
            await self.oms.sync_orders(safe_intents, orderbook_dict)

        # Persist data
        await self._persist_orderbook_snapshots()

    async def _check_geoblock(self) -> None:
        """Check geoblock status."""
        try:
            result = await self.geoblock.check(force=True)
            self.state.geoblock_allowed = not result.blocked
            self.state.last_geoblock_check = datetime.utcnow()

            if result.blocked:
                logger.warning(
                    "engine_geoblock_blocked",
                    country=result.country,
                )
                if self.config.mode == BotMode.LIVE:
                    logger.error("engine_live_mode_blocked")
                    self.risk.trigger_kill_switch("Geoblocked - LIVE mode not allowed")
            else:
                logger.info("engine_geoblock_allowed", country=result.country)

        except Exception as e:
            logger.error("engine_geoblock_check_failed", error=str(e))
            # Fail safe - treat as blocked
            self.state.geoblock_allowed = False

    async def _refresh_market_universe(self) -> None:
        """Refresh market universe from Gamma API."""
        try:
            async with GammaClient() as gamma:
                all_markets = await gamma.get_all_active_markets()

            # Select markets
            selected = self.universe.select_markets(all_markets)
            token_ids = self.universe.select_tokens(selected)

            # Update state
            self.state.update_markets(selected)
            self.state.set_active_tokens(token_ids)

            # Persist to database
            self.market_dao.save_markets(selected)

            logger.info(
                "engine_universe_refreshed",
                total_markets=len(all_markets),
                selected_markets=len(selected),
                active_tokens=len(token_ids),
            )

            # Update WebSocket subscriptions
            if self.clob_ws.is_connected:
                await self.clob_ws.subscribe_markets(token_ids)

        except Exception as e:
            logger.error("engine_universe_refresh_failed", error=str(e))

    async def _connect_websocket(self) -> None:
        """Connect to CLOB WebSocket."""
        try:
            await self.clob_ws.connect()
            await self.clob_ws.subscribe_markets(list(self.state.active_token_ids))
            self.metrics.ws_connected = True
            self.metrics.ws_disconnected_since = None
            logger.info("engine_ws_connected")
        except Exception as e:
            logger.error("engine_ws_connect_failed", error=str(e))
            self.metrics.ws_connected = False
            self.metrics.ws_disconnected_since = datetime.utcnow()

    def _on_orderbook_update(self, token_id: str, orderbook: OrderBook) -> None:
        """Handle orderbook update from WebSocket."""
        self.state.update_orderbook(token_id, orderbook)
        self.metrics.orderbook_updates += 1

    async def _persist_orderbook_snapshots(self) -> None:
        """Persist current orderbook snapshots to database."""
        for token_id, orderbook in self.state.orderbooks.items():
            try:
                self.orderbook_dao.save_snapshot(orderbook)
            except Exception as e:
                logger.warning(
                    "engine_persist_orderbook_failed",
                    token_id=token_id,
                    error=str(e),
                )

    async def _reconcile_positions(self) -> None:
        """Reconcile positions from Data API."""
        # TODO: Implement position reconciliation
        pass

    def _build_strategy_context(self) -> StrategyContext:
        """Build context object to pass to strategies."""
        return StrategyContext(
            timestamp=datetime.utcnow(),
            orderbooks=self.state.orderbooks.copy(),
            market_metadata=self.state.markets.copy(),
            token_to_market=self.state.token_to_market.copy(),
            positions=self.state.positions.copy(),
            open_orders=self.oms.get_open_orders(),
            account_balance=self.state.balance,
            mode=self.config.mode.value,
            config={},
        )

    def _on_kill_switch(self, state) -> None:
        """Handle kill switch activation."""
        asyncio.create_task(self._handle_kill_switch())

    async def _handle_kill_switch(self) -> None:
        """Handle kill switch - cancel all orders and alert."""
        logger.critical(
            "engine_kill_switch_handling",
            reason=self.kill_switch.state.reason.value if self.kill_switch.state.reason else None,
        )

        # Cancel all orders
        await self.oms.cancel_all()

        # Send alert
        async with AlertManager(
            telegram_bot_token=self.config.telegram_bot_token,
            telegram_chat_id=self.config.telegram_chat_id,
            slack_webhook_url=self.config.slack_webhook_url,
        ) as alerts:
            await alerts.alert_kill_switch(
                self.kill_switch.state.reason.value if self.kill_switch.state.reason else "Unknown"
            )

    def _setup_signal_handlers(self) -> None:
        """Setup OS signal handlers for graceful shutdown."""
        # Signal handlers only work on Unix systems, not Windows
        if sys.platform == "win32":
            # On Windows, Ctrl+C will raise KeyboardInterrupt which is caught in main
            logger.debug("signal_handlers_skipped", reason="Windows platform")
            return

        loop = asyncio.get_event_loop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))

    def get_status(self) -> Dict[str, Any]:
        """Get engine status for monitoring."""
        return {
            "mode": self.config.mode.value,
            "running": self._running,
            "started_at": self.metrics.started_at.isoformat() if self.metrics.started_at else None,
            "uptime_seconds": (
                (datetime.utcnow() - self.metrics.started_at).total_seconds()
                if self.metrics.started_at else 0
            ),
            "ticks": self.metrics.ticks,
            "orderbook_updates": self.metrics.orderbook_updates,
            "errors": self.metrics.errors,
            "ws_connected": self.metrics.ws_connected,
            "geoblock_allowed": self.state.geoblock_allowed,
            "state": self.state.get_summary(),
            "risk": self.risk.get_status(),
            "oms": self.oms.get_status(),
            "db_stats": self.db.get_stats(),
        }
