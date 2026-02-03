"""Arbitrage opportunity monitor for Polymarket.

This module provides continuous monitoring of markets for:
1. Single-market arbitrage (YES + NO != $1)
2. Market health metrics
3. Historical tracking of opportunities
4. Alerts for significant mispricing

Based on research showing $40M extracted via arbitrage - we monitor
these opportunities even though we can't capture them, to:
- Understand market efficiency
- Avoid trading into mispriced markets
- Track market health over time
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set
from collections import deque

from storage.models import Market, OrderBook
from risk.arbitrage_checker import ArbitrageChecker, ArbitrageOpportunity
from monitoring.logger import get_logger

logger = get_logger(__name__)


@dataclass
class MarketHealthMetrics:
    """Health metrics for a single market."""
    market_id: str
    title: str
    timestamp: datetime
    yes_price: Optional[Decimal]
    no_price: Optional[Decimal]
    price_sum: Optional[Decimal]
    mispricing: Optional[Decimal]
    yes_spread: Optional[Decimal]
    no_spread: Optional[Decimal]
    yes_liquidity: Optional[Decimal]
    no_liquidity: Optional[Decimal]
    is_healthy: bool
    issues: List[str] = field(default_factory=list)


@dataclass
class ArbitrageStats:
    """Statistics about arbitrage opportunities."""
    period_start: datetime
    period_end: datetime
    total_opportunities: int
    buy_both_count: int      # Sum < $1
    sell_both_count: int     # Sum > $1
    total_theoretical_profit: Decimal
    avg_mispricing: Decimal
    max_mispricing: Decimal
    affected_markets: int
    most_mispriced_market: Optional[str]
    opportunities_per_hour: float


class ArbitrageMonitor:
    """
    Monitors markets for arbitrage opportunities and market health.

    This is for observability and alerting, not execution.
    We track opportunities to:
    1. Monitor market efficiency over time
    2. Alert on significant mispricing events
    3. Identify markets to avoid (being exit liquidity)
    4. Generate metrics for analysis
    """

    def __init__(
        self,
        check_interval_seconds: int = 60,
        history_window_hours: int = 24,
        alert_threshold: Decimal = Decimal("0.10"),  # Alert if mispricing > 10 cents
        on_alert: Optional[callable] = None,
    ):
        self.check_interval_seconds = check_interval_seconds
        self.history_window_hours = history_window_hours
        self.alert_threshold = alert_threshold
        self._on_alert = on_alert

        # Core checker
        self._checker = ArbitrageChecker()

        # State
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Data sources (set by bot engine)
        self._markets: Dict[str, Market] = {}
        self._orderbooks: Dict[str, OrderBook] = {}

        # History tracking
        self._opportunity_history: deque = deque(maxlen=10000)
        self._health_history: deque = deque(maxlen=10000)

        # Current state
        self._current_opportunities: List[ArbitrageOpportunity] = []
        self._current_health: Dict[str, MarketHealthMetrics] = {}

        # Stats tracking
        self._last_stats_time = datetime.utcnow()
        self._hourly_stats: List[ArbitrageStats] = []

    def set_data_sources(
        self,
        markets: Dict[str, Market],
        orderbooks: Dict[str, OrderBook],
    ) -> None:
        """Set the data sources for monitoring."""
        self._markets = markets
        self._orderbooks = orderbooks

    async def start(self) -> None:
        """Start the monitoring loop."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("arbitrage_monitor_started", interval=self.check_interval_seconds)

    async def stop(self) -> None:
        """Stop the monitoring loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("arbitrage_monitor_stopped")

    async def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        while self._running:
            try:
                await self._run_check()
                await asyncio.sleep(self.check_interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("arbitrage_monitor_error", error=str(e))
                await asyncio.sleep(self.check_interval_seconds)

    async def _run_check(self) -> None:
        """Run a single monitoring check."""
        if not self._markets or not self._orderbooks:
            return

        now = datetime.utcnow()

        # Scan for opportunities
        markets_list = list(self._markets.values())
        opportunities = self._checker.scan_all_markets(markets_list, self._orderbooks)
        self._current_opportunities = opportunities

        # Record opportunities
        for opp in opportunities:
            self._opportunity_history.append(opp)

            # Check for alerts
            if opp.potential_profit >= self.alert_threshold:
                self._send_alert(opp)

        # Check market health
        health_metrics = self._check_all_markets_health()
        self._current_health = {m.market_id: m for m in health_metrics}

        for metric in health_metrics:
            self._health_history.append(metric)

        # Calculate hourly stats if needed
        if (now - self._last_stats_time).total_seconds() >= 3600:
            stats = self._calculate_stats(
                self._last_stats_time,
                now,
            )
            self._hourly_stats.append(stats)
            self._last_stats_time = now

            logger.info(
                "arbitrage_hourly_stats",
                opportunities=stats.total_opportunities,
                total_profit=str(stats.total_theoretical_profit),
                avg_mispricing=str(stats.avg_mispricing),
            )

        # Log summary
        if opportunities:
            logger.info(
                "arbitrage_scan_complete",
                opportunities=len(opportunities),
                total_profit=sum(o.potential_profit for o in opportunities),
            )

    def _check_all_markets_health(self) -> List[MarketHealthMetrics]:
        """Check health of all markets."""
        metrics = []

        for market in self._markets.values():
            health = self._check_market_health(market)
            if health:
                metrics.append(health)

        return metrics

    def _check_market_health(self, market: Market) -> Optional[MarketHealthMetrics]:
        """Check health of a single market."""
        yes_token = market.yes_token
        no_token = market.no_token

        if not yes_token or not no_token:
            return None

        yes_ob = self._orderbooks.get(yes_token.token_id)
        no_ob = self._orderbooks.get(no_token.token_id)

        issues = []
        is_healthy = True

        # Get prices
        yes_price = yes_ob.best_ask if yes_ob else None
        no_price = no_ob.best_ask if no_ob else None

        price_sum = None
        mispricing = None

        if yes_price and no_price:
            price_sum = yes_price + no_price
            mispricing = abs(price_sum - Decimal("1"))

            if mispricing > Decimal("0.05"):
                is_healthy = False
                issues.append(f"Significant mispricing: {mispricing:.2%}")
            elif mispricing > Decimal("0.02"):
                issues.append(f"Minor mispricing: {mispricing:.2%}")
        else:
            issues.append("Missing price data")

        # Get spreads
        yes_spread = yes_ob.spread if yes_ob else None
        no_spread = no_ob.spread if no_ob else None

        if yes_spread and yes_spread > Decimal("0.10"):
            issues.append(f"Wide YES spread: {yes_spread:.2%}")
        if no_spread and no_spread > Decimal("0.10"):
            issues.append(f"Wide NO spread: {no_spread:.2%}")

        # Get liquidity
        yes_liquidity = None
        no_liquidity = None

        if yes_ob and yes_ob.asks:
            yes_liquidity = sum(level.size for level in yes_ob.asks)
        if no_ob and no_ob.bids:
            no_liquidity = sum(level.size for level in no_ob.bids)

        if yes_liquidity and yes_liquidity < Decimal("100"):
            issues.append("Low YES liquidity")
        if no_liquidity and no_liquidity < Decimal("100"):
            issues.append("Low NO liquidity")

        return MarketHealthMetrics(
            market_id=market.market_id,
            title=market.title,
            timestamp=datetime.utcnow(),
            yes_price=yes_price,
            no_price=no_price,
            price_sum=price_sum,
            mispricing=mispricing,
            yes_spread=yes_spread,
            no_spread=no_spread,
            yes_liquidity=yes_liquidity,
            no_liquidity=no_liquidity,
            is_healthy=is_healthy,
            issues=issues,
        )

    def _calculate_stats(
        self,
        start: datetime,
        end: datetime,
    ) -> ArbitrageStats:
        """Calculate statistics for a time period."""
        # Filter opportunities in the time range
        opps = [
            o for o in self._opportunity_history
            if start <= o.detected_at <= end
        ]

        if not opps:
            return ArbitrageStats(
                period_start=start,
                period_end=end,
                total_opportunities=0,
                buy_both_count=0,
                sell_both_count=0,
                total_theoretical_profit=Decimal("0"),
                avg_mispricing=Decimal("0"),
                max_mispricing=Decimal("0"),
                affected_markets=0,
                most_mispriced_market=None,
                opportunities_per_hour=0.0,
            )

        buy_both = [o for o in opps if o.opportunity_type == "buy_both"]
        sell_both = [o for o in opps if o.opportunity_type == "sell_both"]

        total_profit = sum(o.potential_profit for o in opps)
        mispricings = [o.potential_profit for o in opps]
        avg_mispricing = sum(mispricings) / len(mispricings)
        max_mispricing = max(mispricings)

        # Find most mispriced market
        max_opp = max(opps, key=lambda o: o.potential_profit)

        # Count unique markets
        affected_markets = len(set(o.market_id for o in opps))

        # Calculate rate
        hours = (end - start).total_seconds() / 3600
        rate = len(opps) / hours if hours > 0 else 0

        return ArbitrageStats(
            period_start=start,
            period_end=end,
            total_opportunities=len(opps),
            buy_both_count=len(buy_both),
            sell_both_count=len(sell_both),
            total_theoretical_profit=total_profit,
            avg_mispricing=avg_mispricing,
            max_mispricing=max_mispricing,
            affected_markets=affected_markets,
            most_mispriced_market=max_opp.market_title,
            opportunities_per_hour=rate,
        )

    def _send_alert(self, opportunity: ArbitrageOpportunity) -> None:
        """Send an alert for a significant arbitrage opportunity."""
        logger.warning(
            "arbitrage_alert",
            market_id=opportunity.market_id,
            title=opportunity.market_title,
            type=opportunity.opportunity_type,
            profit=str(opportunity.potential_profit),
            yes_price=str(opportunity.yes_price),
            no_price=str(opportunity.no_price),
        )

        if self._on_alert:
            try:
                self._on_alert(opportunity)
            except Exception as e:
                logger.error("arbitrage_alert_callback_error", error=str(e))

    def get_current_opportunities(self) -> List[ArbitrageOpportunity]:
        """Get currently detected arbitrage opportunities."""
        return self._current_opportunities.copy()

    def get_market_health(self, market_id: str) -> Optional[MarketHealthMetrics]:
        """Get health metrics for a specific market."""
        return self._current_health.get(market_id)

    def get_unhealthy_markets(self) -> List[MarketHealthMetrics]:
        """Get all markets currently flagged as unhealthy."""
        return [m for m in self._current_health.values() if not m.is_healthy]

    def get_recent_stats(self, hours: int = 24) -> ArbitrageStats:
        """Get aggregated stats for the last N hours."""
        end = datetime.utcnow()
        start = end - timedelta(hours=hours)
        return self._calculate_stats(start, end)

    def get_hourly_stats(self, hours: int = 24) -> List[ArbitrageStats]:
        """Get hourly stats for the last N hours."""
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        return [s for s in self._hourly_stats if s.period_end >= cutoff]

    def get_status(self) -> Dict[str, Any]:
        """Get monitor status for dashboard/API."""
        recent = self.get_recent_stats(1)

        return {
            "running": self._running,
            "check_interval_seconds": self.check_interval_seconds,
            "markets_monitored": len(self._markets),
            "current_opportunities": len(self._current_opportunities),
            "unhealthy_markets": len(self.get_unhealthy_markets()),
            "last_hour": {
                "opportunities": recent.total_opportunities,
                "total_profit": str(recent.total_theoretical_profit),
                "avg_mispricing": str(recent.avg_mispricing),
                "opportunities_per_hour": recent.opportunities_per_hour,
            },
            "history_size": len(self._opportunity_history),
        }

    def get_detailed_report(self) -> Dict[str, Any]:
        """Generate a detailed report for analysis."""
        recent_24h = self.get_recent_stats(24)
        recent_1h = self.get_recent_stats(1)

        unhealthy = self.get_unhealthy_markets()

        return {
            "generated_at": datetime.utcnow().isoformat(),
            "summary": {
                "markets_monitored": len(self._markets),
                "unhealthy_markets": len(unhealthy),
                "current_opportunities": len(self._current_opportunities),
            },
            "last_hour": {
                "opportunities": recent_1h.total_opportunities,
                "buy_both": recent_1h.buy_both_count,
                "sell_both": recent_1h.sell_both_count,
                "total_profit": str(recent_1h.total_theoretical_profit),
                "avg_mispricing": str(recent_1h.avg_mispricing),
                "max_mispricing": str(recent_1h.max_mispricing),
                "most_mispriced": recent_1h.most_mispriced_market,
            },
            "last_24_hours": {
                "opportunities": recent_24h.total_opportunities,
                "buy_both": recent_24h.buy_both_count,
                "sell_both": recent_24h.sell_both_count,
                "total_profit": str(recent_24h.total_theoretical_profit),
                "avg_mispricing": str(recent_24h.avg_mispricing),
                "max_mispricing": str(recent_24h.max_mispricing),
                "affected_markets": recent_24h.affected_markets,
                "opportunities_per_hour": recent_24h.opportunities_per_hour,
            },
            "unhealthy_markets": [
                {
                    "market_id": m.market_id,
                    "title": m.title,
                    "price_sum": str(m.price_sum) if m.price_sum else None,
                    "mispricing": str(m.mispricing) if m.mispricing else None,
                    "issues": m.issues,
                }
                for m in unhealthy
            ],
            "current_opportunities": [
                {
                    "market_id": o.market_id,
                    "title": o.market_title,
                    "type": o.opportunity_type,
                    "yes_price": str(o.yes_price),
                    "no_price": str(o.no_price),
                    "profit": str(o.potential_profit),
                }
                for o in self._current_opportunities[:10]  # Top 10
            ],
        }
