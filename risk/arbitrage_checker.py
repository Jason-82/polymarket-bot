"""Arbitrage detection and defensive checks for Polymarket trading.

This module provides:
1. Single-market arbitrage detection (YES + NO != $1)
2. Defensive checks to avoid being exit liquidity for arbitrageurs
3. Simple cross-market dependency detection (heuristic-based, no LLM)

Based on research showing $40M extracted via arbitrage on Polymarket.
We use this defensively - not to capture arbitrage (we're too slow),
but to avoid trading into mispriced markets where faster traders
will take the other side.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from storage.models import OrderBook, Market, OrderSide
from monitoring.logger import get_logger

logger = get_logger(__name__)


# Thresholds for arbitrage detection
ARBITRAGE_SUM_LOW = Decimal("0.95")   # If YES+NO < 0.95, potential arb
ARBITRAGE_SUM_HIGH = Decimal("1.05")  # If YES+NO > 1.05, potential arb
WARNING_SUM_LOW = Decimal("0.98")     # Warning zone
WARNING_SUM_HIGH = Decimal("1.02")    # Warning zone


@dataclass
class ArbitrageOpportunity:
    """Detected arbitrage opportunity."""
    market_id: str
    market_title: str
    yes_price: Decimal
    no_price: Decimal
    price_sum: Decimal
    opportunity_type: str  # "buy_both" (sum < 1) or "sell_both" (sum > 1)
    potential_profit: Decimal  # Per $1 notional
    detected_at: datetime = field(default_factory=datetime.utcnow)
    yes_liquidity: Optional[Decimal] = None
    no_liquidity: Optional[Decimal] = None


@dataclass
class ArbitrageCheckResult:
    """Result of arbitrage check for a trade."""
    is_safe: bool
    warning: Optional[str] = None
    should_block: bool = False
    opportunity: Optional[ArbitrageOpportunity] = None
    recommendation: Optional[str] = None


class ArbitrageChecker:
    """
    Checks for arbitrage conditions before trading.

    Main purposes:
    1. DEFENSIVE: Don't buy YES at 0.65 if YES+NO = 1.08 (you're exit liquidity)
    2. MONITORING: Track arbitrage opportunities (even if we can't capture them)
    3. ALERTING: Log when markets are significantly mispriced
    """

    def __init__(
        self,
        block_threshold: Decimal = Decimal("0.05"),  # Block if mispricing > 5 cents
        warn_threshold: Decimal = Decimal("0.02"),   # Warn if mispricing > 2 cents
    ):
        self.block_threshold = block_threshold
        self.warn_threshold = warn_threshold

        # Track detected opportunities
        self._opportunities: List[ArbitrageOpportunity] = []
        self._last_cleanup = datetime.utcnow()

    def check_trade(
        self,
        market: Market,
        side: OrderSide,
        price: Decimal,
        yes_orderbook: Optional[OrderBook],
        no_orderbook: Optional[OrderBook],
    ) -> ArbitrageCheckResult:
        """
        Check if a trade is safe from an arbitrage perspective.

        Args:
            market: The market being traded
            side: BUY or SELL
            price: The price we're trading at
            yes_orderbook: Orderbook for YES token
            no_orderbook: Orderbook for NO token

        Returns:
            ArbitrageCheckResult with safety assessment
        """
        if not yes_orderbook or not no_orderbook:
            # Can't check without both orderbooks
            return ArbitrageCheckResult(
                is_safe=True,
                warning="Missing orderbook data for arbitrage check",
            )

        # Get best prices
        yes_price = self._get_effective_price(yes_orderbook, OrderSide.BUY)
        no_price = self._get_effective_price(no_orderbook, OrderSide.BUY)

        if yes_price is None or no_price is None:
            return ArbitrageCheckResult(
                is_safe=True,
                warning="No liquidity for arbitrage check",
            )

        price_sum = yes_price + no_price
        mispricing = abs(price_sum - Decimal("1"))

        # Check for arbitrage condition
        if price_sum < ARBITRAGE_SUM_LOW:
            # Buy both opportunity - someone could buy YES and NO for < $1
            opportunity = ArbitrageOpportunity(
                market_id=market.market_id,
                market_title=market.title,
                yes_price=yes_price,
                no_price=no_price,
                price_sum=price_sum,
                opportunity_type="buy_both",
                potential_profit=Decimal("1") - price_sum,
                yes_liquidity=yes_orderbook.best_ask_size,
                no_liquidity=no_orderbook.best_ask_size,
            )
            self._record_opportunity(opportunity)

            # If we're buying, we might be providing exit liquidity
            if side == OrderSide.BUY:
                if mispricing >= self.block_threshold:
                    return ArbitrageCheckResult(
                        is_safe=False,
                        warning=f"Market mispriced: YES+NO = ${price_sum:.2f}. You may be exit liquidity.",
                        should_block=True,
                        opportunity=opportunity,
                        recommendation="Wait for prices to normalize or trade the other side",
                    )
                elif mispricing >= self.warn_threshold:
                    return ArbitrageCheckResult(
                        is_safe=True,
                        warning=f"Market slightly mispriced: YES+NO = ${price_sum:.2f}",
                        opportunity=opportunity,
                    )

        elif price_sum > ARBITRAGE_SUM_HIGH:
            # Sell both opportunity - someone could sell YES and NO for > $1
            opportunity = ArbitrageOpportunity(
                market_id=market.market_id,
                market_title=market.title,
                yes_price=yes_price,
                no_price=no_price,
                price_sum=price_sum,
                opportunity_type="sell_both",
                potential_profit=price_sum - Decimal("1"),
                yes_liquidity=yes_orderbook.best_bid_size,
                no_liquidity=no_orderbook.best_bid_size,
            )
            self._record_opportunity(opportunity)

            # If we're selling, we might be providing exit liquidity
            if side == OrderSide.SELL:
                if mispricing >= self.block_threshold:
                    return ArbitrageCheckResult(
                        is_safe=False,
                        warning=f"Market mispriced: YES+NO = ${price_sum:.2f}. You may be exit liquidity.",
                        should_block=True,
                        opportunity=opportunity,
                        recommendation="Wait for prices to normalize or trade the other side",
                    )
                elif mispricing >= self.warn_threshold:
                    return ArbitrageCheckResult(
                        is_safe=True,
                        warning=f"Market slightly mispriced: YES+NO = ${price_sum:.2f}",
                        opportunity=opportunity,
                    )

        # Check price reasonableness
        if side == OrderSide.BUY and price > yes_price + self.warn_threshold:
            return ArbitrageCheckResult(
                is_safe=True,
                warning=f"Buying above market: your price ${price:.2f} > best ask ${yes_price:.2f}",
            )

        return ArbitrageCheckResult(is_safe=True)

    def check_market_health(
        self,
        market: Market,
        yes_orderbook: Optional[OrderBook],
        no_orderbook: Optional[OrderBook],
    ) -> Dict[str, Any]:
        """
        Check overall market health for monitoring.

        Returns a dict with:
        - is_healthy: bool
        - price_sum: Decimal
        - mispricing: Decimal
        - spread: Decimal (average of YES and NO spreads)
        - liquidity_warning: Optional[str]
        """
        result = {
            "market_id": market.market_id,
            "is_healthy": True,
            "price_sum": None,
            "mispricing": None,
            "spread": None,
            "liquidity_warning": None,
        }

        if not yes_orderbook or not no_orderbook:
            result["liquidity_warning"] = "Missing orderbook"
            return result

        # Get midpoints
        yes_mid = yes_orderbook.midpoint
        no_mid = no_orderbook.midpoint

        if yes_mid is None or no_mid is None:
            result["liquidity_warning"] = "No liquidity"
            return result

        price_sum = yes_mid + no_mid
        mispricing = abs(price_sum - Decimal("1"))

        result["price_sum"] = price_sum
        result["mispricing"] = mispricing

        # Calculate average spread
        yes_spread = yes_orderbook.spread or Decimal("0")
        no_spread = no_orderbook.spread or Decimal("0")
        result["spread"] = (yes_spread + no_spread) / 2

        # Health checks
        if mispricing > self.block_threshold:
            result["is_healthy"] = False
            result["liquidity_warning"] = f"Significant mispricing: {mispricing:.2%}"
        elif result["spread"] > Decimal("0.10"):
            result["liquidity_warning"] = f"Wide spread: {result['spread']:.2%}"

        return result

    def scan_all_markets(
        self,
        markets: List[Market],
        orderbooks: Dict[str, OrderBook],
    ) -> List[ArbitrageOpportunity]:
        """
        Scan all markets for arbitrage opportunities.

        This is for monitoring/alerting, not execution.
        """
        opportunities = []

        for market in markets:
            yes_token = market.yes_token
            no_token = market.no_token

            if not yes_token or not no_token:
                continue

            yes_ob = orderbooks.get(yes_token.token_id)
            no_ob = orderbooks.get(no_token.token_id)

            if not yes_ob or not no_ob:
                continue

            # Get best prices for buy_both check
            yes_ask = yes_ob.best_ask
            no_ask = no_ob.best_ask

            if yes_ask and no_ask:
                buy_sum = yes_ask + no_ask
                if buy_sum < ARBITRAGE_SUM_LOW:
                    opp = ArbitrageOpportunity(
                        market_id=market.market_id,
                        market_title=market.title,
                        yes_price=yes_ask,
                        no_price=no_ask,
                        price_sum=buy_sum,
                        opportunity_type="buy_both",
                        potential_profit=Decimal("1") - buy_sum,
                        yes_liquidity=yes_ob.best_ask_size,
                        no_liquidity=no_ob.best_ask_size,
                    )
                    opportunities.append(opp)

            # Get best prices for sell_both check
            yes_bid = yes_ob.best_bid
            no_bid = no_ob.best_bid

            if yes_bid and no_bid:
                sell_sum = yes_bid + no_bid
                if sell_sum > ARBITRAGE_SUM_HIGH:
                    opp = ArbitrageOpportunity(
                        market_id=market.market_id,
                        market_title=market.title,
                        yes_price=yes_bid,
                        no_price=no_bid,
                        price_sum=sell_sum,
                        opportunity_type="sell_both",
                        potential_profit=sell_sum - Decimal("1"),
                        yes_liquidity=yes_ob.best_bid_size,
                        no_liquidity=no_ob.best_bid_size,
                    )
                    opportunities.append(opp)

        # Log if we found opportunities
        if opportunities:
            logger.info(
                "arbitrage_opportunities_detected",
                count=len(opportunities),
                total_profit=sum(o.potential_profit for o in opportunities),
            )

        return opportunities

    def _get_effective_price(
        self,
        orderbook: OrderBook,
        side: OrderSide,
    ) -> Optional[Decimal]:
        """Get the effective price for a side (ask for buy, bid for sell)."""
        if side == OrderSide.BUY:
            return orderbook.best_ask
        return orderbook.best_bid

    def _record_opportunity(self, opportunity: ArbitrageOpportunity) -> None:
        """Record an arbitrage opportunity for tracking."""
        self._opportunities.append(opportunity)

        # Log it
        logger.info(
            "arbitrage_opportunity",
            market_id=opportunity.market_id,
            type=opportunity.opportunity_type,
            profit=str(opportunity.potential_profit),
            yes=str(opportunity.yes_price),
            no=str(opportunity.no_price),
        )

        # Cleanup old opportunities periodically
        self._cleanup_old_opportunities()

    def _cleanup_old_opportunities(self) -> None:
        """Remove opportunities older than 1 hour."""
        now = datetime.utcnow()
        if (now - self._last_cleanup).total_seconds() < 300:  # Every 5 minutes
            return

        cutoff = now.replace(hour=now.hour - 1)
        self._opportunities = [
            o for o in self._opportunities
            if o.detected_at > cutoff
        ]
        self._last_cleanup = now

    def get_recent_opportunities(self, minutes: int = 60) -> List[ArbitrageOpportunity]:
        """Get arbitrage opportunities detected in the last N minutes."""
        cutoff = datetime.utcnow().replace(
            minute=datetime.utcnow().minute - minutes
        )
        return [o for o in self._opportunities if o.detected_at > cutoff]

    def get_stats(self) -> Dict[str, Any]:
        """Get arbitrage detection statistics."""
        recent = self.get_recent_opportunities(60)

        buy_both = [o for o in recent if o.opportunity_type == "buy_both"]
        sell_both = [o for o in recent if o.opportunity_type == "sell_both"]

        return {
            "total_detected_1h": len(recent),
            "buy_both_count": len(buy_both),
            "sell_both_count": len(sell_both),
            "total_potential_profit": sum(o.potential_profit for o in recent),
            "avg_profit_per_opp": (
                sum(o.potential_profit for o in recent) / len(recent)
                if recent else Decimal("0")
            ),
        }
