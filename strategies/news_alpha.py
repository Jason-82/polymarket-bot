"""
News Alpha Strategy

Event-driven strategy that monitors news sources, uses AI reasoning to
analyze impact on prediction markets, and generates trade signals faster
than human traders.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set

from strategies.base import StrategyBase, StrategyContext
from strategies.loader import register_strategy
from connectors.news_sources import (
    NewsAggregator,
    NewsEvent,
    NewsCategory,
    TwitterNewsSource,
    RSSNewsSource,
)
from connectors.news_sources.twitter_client import TwitterConfig, MockTwitterSource
from connectors.news_sources.rss_client import RSSConfig
from reasoning import LLMAnalyzer, TradeSignal, SignalDirection, MarketMapper
from reasoning.llm_analyzer import LLMConfig
from reasoning.cross_market_analyzer import CrossMarketAnalyzer, RelatedMarket
from execution.order_intent import OrderIntent, OrderSide
from monitoring.logger import get_logger

logger = get_logger(__name__)


@dataclass
class NewsAlphaConfig:
    """Configuration for news alpha strategy."""
    # API Keys (loaded from env)
    twitter_bearer_token: Optional[str] = None
    claude_api_key: Optional[str] = None

    # News Sources
    enable_twitter: bool = True
    enable_rss: bool = True
    twitter_poll_interval: int = 15      # seconds
    rss_poll_interval: int = 60          # seconds

    # LLM Settings
    llm_model: str = "claude-sonnet-4-20250514"
    llm_temperature: float = 0.3
    min_confidence: float = 75.0         # Minimum confidence to trade
    min_edge: float = 0.03               # Minimum 3 cent edge

    # Cross-Market Analysis
    enable_cross_market: bool = True     # Find related markets on news
    cross_market_min_confidence: float = 60.0  # Lower threshold for related markets
    max_related_markets: int = 5         # Max additional markets to trade per signal
    min_lag_seconds: int = 30            # Only trade lagging markets with this much lag

    # Risk / Position Sizing
    base_order_size: Decimal = Decimal("500")   # $500 base size
    max_order_size: Decimal = Decimal("2000")   # $2000 max per signal
    max_position_per_market: Decimal = Decimal("5000")  # $5k max per market
    max_daily_exposure: Decimal = Decimal("10000")      # $10k daily cap
    max_signals_per_day: int = 20        # Rate limit signals

    # Timing
    max_news_age_seconds: int = 300      # Ignore news older than 5 min
    signal_cooldown_seconds: int = 60    # Min time between signals on same market
    analysis_timeout_seconds: float = 30.0

    # Categories (can be changed to switch focus)
    enabled_categories: List[str] = field(default_factory=lambda: [
        "politics",
        "world_events",
        "economics",
    ])

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "NewsAlphaConfig":
        """Create config from dictionary."""
        import os
        return cls(
            # Load API keys from config or fall back to environment variables
            twitter_bearer_token=data.get("twitter_bearer_token") or os.environ.get("TWITTER_BEARER_TOKEN"),
            claude_api_key=data.get("claude_api_key") or os.environ.get("ANTHROPIC_API_KEY"),
            enable_twitter=data.get("enable_twitter", True),
            enable_rss=data.get("enable_rss", True),
            twitter_poll_interval=data.get("twitter_poll_interval", 15),
            rss_poll_interval=data.get("rss_poll_interval", 60),
            llm_model=data.get("llm_model", "claude-sonnet-4-20250514"),
            llm_temperature=data.get("llm_temperature", 0.3),
            min_confidence=data.get("min_confidence", 75.0),
            min_edge=data.get("min_edge", 0.03),
            enable_cross_market=data.get("enable_cross_market", True),
            cross_market_min_confidence=data.get("cross_market_min_confidence", 60.0),
            max_related_markets=data.get("max_related_markets", 5),
            min_lag_seconds=data.get("min_lag_seconds", 30),
            base_order_size=Decimal(str(data.get("base_order_size", 500))),
            max_order_size=Decimal(str(data.get("max_order_size", 2000))),
            max_position_per_market=Decimal(str(data.get("max_position_per_market", 5000))),
            max_daily_exposure=Decimal(str(data.get("max_daily_exposure", 10000))),
            max_signals_per_day=data.get("max_signals_per_day", 20),
            max_news_age_seconds=data.get("max_news_age_seconds", 300),
            signal_cooldown_seconds=data.get("signal_cooldown_seconds", 60),
            analysis_timeout_seconds=data.get("analysis_timeout_seconds", 30.0),
            enabled_categories=data.get("enabled_categories", ["politics", "world_events", "economics"]),
        )


@dataclass
class DailyStats:
    """Track daily trading statistics."""
    date: str = ""
    signals_generated: int = 0
    orders_placed: int = 0
    total_exposure: Decimal = Decimal("0")
    pnl: Decimal = Decimal("0")

    def reset_if_new_day(self) -> None:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        if self.date != today:
            self.date = today
            self.signals_generated = 0
            self.orders_placed = 0
            self.total_exposure = Decimal("0")
            self.pnl = Decimal("0")


@register_strategy("news_alpha")
class NewsAlphaStrategy(StrategyBase):
    """
    News Alpha Strategy.

    Monitors news in real-time, uses Claude to reason about market impact,
    and places trades before slower human traders react.

    Edge comes from:
    1. Speed: Reading and processing news faster
    2. Reasoning: Finding non-obvious second-order effects
    3. Discipline: Systematic execution without emotion
    """

    def __init__(self, name: str, params: Dict[str, Any], tokens: List[str]):
        super().__init__(name, params, tokens)
        self.config = NewsAlphaConfig.from_dict(params)

        # Components (initialized in start())
        self._aggregator: Optional[NewsAggregator] = None
        self._analyzer: Optional[LLMAnalyzer] = None
        self._mapper: Optional[MarketMapper] = None
        self._cross_market_analyzer: Optional[CrossMarketAnalyzer] = None

        # State
        self._running = False
        self._pending_events: asyncio.Queue = asyncio.Queue()
        self._recent_signals: Dict[str, datetime] = {}  # market_id -> last signal time
        self._processed_news: Set[str] = set()  # news IDs already processed
        self._daily_stats = DailyStats()

        # Cache all markets for cross-market analysis
        self._all_markets: List[Dict[str, Any]] = []

        # Background task
        self._processing_task: Optional[asyncio.Task] = None

    async def start(self, context: StrategyContext) -> None:
        """Initialize and start the strategy."""
        logger.info("news_alpha_starting", config=self.config.__dict__)

        # Initialize market mapper
        self._mapper = MarketMapper()
        self._mapper.update_markets(list(context.market_metadata.values()))

        # Initialize LLM analyzer
        if self.config.claude_api_key:
            llm_config = LLMConfig(
                api_key=self.config.claude_api_key,
                model=self.config.llm_model,
                temperature=self.config.llm_temperature,
                min_confidence_to_signal=self.config.min_confidence,
                min_edge_to_signal=self.config.min_edge,
                timeout_seconds=self.config.analysis_timeout_seconds,
            )
            self._analyzer = LLMAnalyzer(llm_config)
            await self._analyzer.connect()

            # Initialize cross-market analyzer if enabled
            if self.config.enable_cross_market:
                self._cross_market_analyzer = CrossMarketAnalyzer(
                    api_key=self.config.claude_api_key,
                    model=self.config.llm_model,
                    timeout_seconds=self.config.analysis_timeout_seconds + 15,  # Extra time for larger analysis
                )
                await self._cross_market_analyzer.connect()
                logger.info("news_alpha_cross_market_enabled")
        else:
            logger.warning("news_alpha_no_claude_key", msg="LLM analysis disabled")

        # Initialize news aggregator
        self._aggregator = NewsAggregator()

        # Add Twitter source
        if self.config.enable_twitter and self.config.twitter_bearer_token:
            twitter_config = TwitterConfig(
                bearer_token=self.config.twitter_bearer_token,
                poll_interval_seconds=self.config.twitter_poll_interval,
            )
            twitter = TwitterNewsSource(twitter_config)
            self._aggregator.add_source(twitter, priority=8, latency_bonus=1.5)
            logger.info("news_alpha_twitter_enabled")
        else:
            logger.info("news_alpha_twitter_disabled")

        # Add RSS source
        if self.config.enable_rss:
            rss_config = RSSConfig(
                poll_interval_seconds=self.config.rss_poll_interval,
            )
            rss = RSSNewsSource(rss_config)
            self._aggregator.add_source(rss, priority=6)
            logger.info("news_alpha_rss_enabled")

        # Register news callback
        self._aggregator.on_news(self._on_news_event)

        # Start news aggregation
        await self._aggregator.start()

        # Start background processing
        self._running = True
        self._processing_task = asyncio.create_task(self._process_news_loop())

        logger.info(
            "news_alpha_started",
            sources=self._aggregator.source_count,
            markets=self._mapper.market_count,
        )

    async def stop(self) -> None:
        """Stop the strategy."""
        logger.info("news_alpha_stopping")
        self._running = False

        if self._processing_task:
            self._processing_task.cancel()
            try:
                await self._processing_task
            except asyncio.CancelledError:
                pass

        if self._aggregator:
            await self._aggregator.stop()

        if self._analyzer:
            await self._analyzer.disconnect()

        if self._cross_market_analyzer:
            await self._cross_market_analyzer.disconnect()

        logger.info("news_alpha_stopped")

    def _on_news_event(self, event: NewsEvent) -> None:
        """Callback when news arrives."""
        # Quick filter before queuing
        if event.id in self._processed_news:
            return

        if event.is_stale(self.config.max_news_age_seconds):
            return

        # Check category filter
        enabled_cats = {NewsCategory(c) for c in self.config.enabled_categories}
        if not any(cat in enabled_cats for cat in event.categories):
            return

        # Queue for processing
        try:
            self._pending_events.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("news_alpha_queue_full", dropped_event=event.id)

    async def _process_news_loop(self) -> None:
        """Background loop to process news events."""
        while self._running:
            try:
                # Wait for news event
                event = await asyncio.wait_for(
                    self._pending_events.get(),
                    timeout=1.0,
                )

                # Process it
                await self._process_news_event(event)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("news_alpha_process_error", error=str(e))

    async def _process_news_event(self, event: NewsEvent) -> None:
        """Process a single news event."""
        if event.id in self._processed_news:
            return

        self._processed_news.add(event.id)

        # Cap processed news set size
        if len(self._processed_news) > 1000:
            self._processed_news = set(list(self._processed_news)[500:])

        logger.info(
            "news_alpha_processing",
            news_id=event.id,
            source=event.source,
            headline=event.headline[:100],
        )

        # Check daily limits
        self._daily_stats.reset_if_new_day()
        if self._daily_stats.signals_generated >= self.config.max_signals_per_day:
            logger.info("news_alpha_daily_limit_reached")
            return

        if self._daily_stats.total_exposure >= self.config.max_daily_exposure:
            logger.info("news_alpha_exposure_limit_reached")
            return

        # Find relevant markets
        matches = self._mapper.find_relevant_markets(event)
        if not matches:
            logger.debug("news_alpha_no_matching_markets", news_id=event.id)
            return

        logger.info(
            "news_alpha_markets_matched",
            news_id=event.id,
            market_count=len(matches),
        )

        # Get current prices for matched markets
        # Note: This would come from context in on_tick, using dummy here
        current_prices: Dict[str, Decimal] = {}

        # Analyze with LLM
        if self._analyzer:
            active_markets = [m.market for m in matches]
            result = await self._analyzer.analyze(event, active_markets, current_prices)

            if result.has_actionable_signals:
                for signal in result.signals:
                    await self._handle_signal(signal)

                # Cross-market analysis: find related markets that may lag
                if self._cross_market_analyzer and self.config.enable_cross_market:
                    await self._analyze_cross_markets(event, result.signals, current_prices)

    async def _handle_signal(self, signal: TradeSignal) -> None:
        """Handle a trade signal from analysis."""
        # Check cooldown
        last_signal_time = self._recent_signals.get(signal.market_id)
        if last_signal_time:
            elapsed = (datetime.utcnow() - last_signal_time).total_seconds()
            if elapsed < self.config.signal_cooldown_seconds:
                logger.info(
                    "news_alpha_signal_cooldown",
                    market_id=signal.market_id,
                    seconds_remaining=self.config.signal_cooldown_seconds - elapsed,
                )
                return

        # Check position limits
        # Note: Would check actual position from context in production

        # Calculate order size based on confidence
        size = self._calculate_size(signal)

        logger.info(
            "news_alpha_signal",
            market_id=signal.market_id,
            token_id=signal.token_id,
            direction=signal.direction.value,
            confidence=signal.confidence,
            edge=signal.edge,
            size=str(size),
            reasoning=signal.reasoning,
        )

        # Update state
        self._recent_signals[signal.market_id] = datetime.utcnow()
        self._daily_stats.signals_generated += 1
        self._daily_stats.total_exposure += size

    async def _analyze_cross_markets(
        self,
        event: NewsEvent,
        primary_signals: List[TradeSignal],
        current_prices: Dict[str, Decimal],
    ) -> None:
        """
        Analyze related markets for additional trading opportunities.

        When we get a signal on a primary market, check for:
        1. Logically related markets that should move together
        2. Markets where price updates may lag (trading opportunities)
        3. Second-order effects that take time to play out
        """
        if not primary_signals or not self._cross_market_analyzer:
            return

        # Use the highest confidence primary signal
        primary_signal = max(primary_signals, key=lambda s: s.confidence)

        # Find the primary market in our cached markets
        primary_market = None
        for market in self._all_markets:
            mid = market.get("condition_id", market.get("id", ""))
            if mid == primary_signal.market_id:
                primary_market = market
                break

        if not primary_market:
            logger.debug("cross_market_primary_not_found", market_id=primary_signal.market_id)
            return

        try:
            # Run cross-market analysis
            analysis = await self._cross_market_analyzer.find_related_markets(
                news_event=event,
                primary_market=primary_market,
                all_markets=self._all_markets,
                current_prices=current_prices,
            )

            if not analysis.trading_opportunities:
                logger.debug("cross_market_no_opportunities", primary=primary_signal.market_id)
                return

            logger.info(
                "cross_market_opportunities_found",
                primary=primary_signal.market_id,
                related_count=len(analysis.related_markets),
                opportunities=len(analysis.trading_opportunities),
            )

            # Generate signals for promising related markets
            signals_generated = 0
            for opp in analysis.trading_opportunities[:self.config.max_related_markets]:
                # Skip if confidence too low
                if opp["confidence"] < self.config.cross_market_min_confidence:
                    continue

                # Skip if no lag (already priced in)
                if opp["lag_seconds"] < self.config.min_lag_seconds:
                    continue

                # Skip if already traded recently
                if opp["market_id"] in self._recent_signals:
                    last = self._recent_signals[opp["market_id"]]
                    if (datetime.utcnow() - last).total_seconds() < self.config.signal_cooldown_seconds:
                        continue

                # Determine direction based on primary signal and relationship
                if opp["expected_direction"] == "same":
                    direction = primary_signal.direction
                elif opp["expected_direction"] == "opposite":
                    direction = (
                        SignalDirection.SELL if primary_signal.direction == SignalDirection.BUY
                        else SignalDirection.BUY
                    )
                else:
                    # Uncertain direction - skip
                    continue

                # Create signal for related market
                related_signal = TradeSignal(
                    market_id=opp["market_id"],
                    token_id=opp["token_id"],
                    direction=direction,
                    confidence=opp["confidence"] * 0.9,  # Slight discount for indirect signal
                    reasoning=f"Cross-market ({opp['relationship']}): {opp['reasoning']}",
                    news_event_id=event.id,
                    suggested_size_pct=50.0,  # Half size for related markets
                    urgency="normal",
                )

                await self._handle_signal(related_signal)
                signals_generated += 1

            if signals_generated > 0:
                logger.info(
                    "cross_market_signals_generated",
                    count=signals_generated,
                    primary=primary_signal.market_id,
                )

        except Exception as e:
            logger.error("cross_market_analysis_error", error=str(e))

    def _calculate_size(self, signal: TradeSignal) -> Decimal:
        """Calculate order size based on signal strength."""
        # Base size scaled by confidence
        confidence_multiplier = Decimal(str(signal.suggested_size_pct / 100))
        size = self.config.base_order_size * confidence_multiplier

        # Scale by edge if available
        if signal.edge and signal.edge > self.config.min_edge:
            edge_multiplier = min(Decimal(str(signal.edge / 0.10)), Decimal("2.0"))
            size *= edge_multiplier

        # Apply limits
        size = min(size, self.config.max_order_size)
        size = max(size, Decimal("10"))  # Minimum $10

        return size.quantize(Decimal("1"))

    async def on_tick(self, context: StrategyContext) -> List[OrderIntent]:
        """
        Called on each tick of the trading engine.

        For news alpha, most work happens in background processing.
        on_tick is used to:
        1. Update market mapper with fresh market data
        2. Cache all markets for cross-market analysis
        3. Convert pending signals to order intents
        """
        # Update market metadata
        if self._mapper:
            self._mapper.update_markets(list(context.market_metadata.values()))

        # Cache all markets for cross-market analysis
        # Convert Market objects to dicts for the analyzer
        self._all_markets = [
            {
                "condition_id": m.condition_id,
                "id": m.market_id,
                "question": m.question or m.title,
                "title": m.title,
                "description": m.description,
                "category": m.category,
                "tokens": [
                    {"token_id": t.token_id, "outcome": t.outcome}
                    for t in m.tokens
                ],
            }
            for m in context.market_metadata.values()
        ]

        # For now, signals are logged but not converted to intents
        # Full implementation would maintain a signal queue and convert here
        return []

    def get_risk_budget(self) -> Dict[str, Any]:
        """Return risk budget for this strategy."""
        self._daily_stats.reset_if_new_day()

        return {
            "max_order_size": float(self.config.max_order_size),
            "max_position": float(self.config.max_position_per_market),
            "max_daily_exposure": float(self.config.max_daily_exposure),
            "current_daily_exposure": float(self._daily_stats.total_exposure),
            "signals_today": self._daily_stats.signals_generated,
            "signals_limit": self.config.max_signals_per_day,
        }

    def get_status(self) -> Dict[str, Any]:
        """Get strategy status for monitoring."""
        self._daily_stats.reset_if_new_day()

        return {
            "name": self.name,
            "running": self._running,
            "sources": self._aggregator.source_count if self._aggregator else 0,
            "markets_indexed": self._mapper.market_count if self._mapper else 0,
            "pending_events": self._pending_events.qsize(),
            "processed_today": len(self._processed_news),
            "signals_today": self._daily_stats.signals_generated,
            "exposure_today": float(self._daily_stats.total_exposure),
            "daily_limit_reached": (
                self._daily_stats.signals_generated >= self.config.max_signals_per_day or
                self._daily_stats.total_exposure >= self.config.max_daily_exposure
            ),
        }
