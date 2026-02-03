"""Order book analysis for execution quality estimation.

This module provides:
1. VWAP (Volume-Weighted Average Price) calculation
2. Slippage estimation before trading
3. Liquidity analysis for position sizing
4. Execution feasibility checks

Based on the research paper showing that execution quality matters:
- VWAP captures actual achievable prices vs quoted best bid/ask
- Order book depth limits maximum extractable profit
- Slippage eats larger percentage of smaller positions
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

from storage.models import OrderBook, OrderBookLevel, OrderSide
from monitoring.logger import get_logger

logger = get_logger(__name__)


@dataclass
class VWAPResult:
    """Result of VWAP calculation."""
    vwap: Decimal                     # Volume-weighted average price
    total_size_available: Decimal     # Total size at or better than VWAP
    levels_consumed: int              # Number of price levels used
    worst_price: Decimal              # Worst price in the fill
    price_impact: Decimal             # VWAP - best price (slippage)
    price_impact_pct: Decimal         # Price impact as percentage of best price
    fully_filled: bool                # Whether full size could be filled
    remaining_size: Decimal           # Size that couldn't be filled (if any)


@dataclass
class SlippageEstimate:
    """Slippage estimation for a trade."""
    estimated_fill_price: Decimal     # Expected average fill price
    slippage_cents: Decimal           # Slippage in cents from best price
    slippage_pct: Decimal             # Slippage as percentage
    max_fill_size: Decimal            # Maximum size fillable at this slippage
    execution_feasible: bool          # Whether trade is feasible
    warning: Optional[str] = None     # Any warnings about execution


@dataclass
class LiquiditySnapshot:
    """Snapshot of market liquidity."""
    token_id: str
    timestamp: datetime
    best_bid: Optional[Decimal]
    best_ask: Optional[Decimal]
    spread: Optional[Decimal]
    spread_pct: Optional[Decimal]
    bid_depth_10bps: Decimal          # Volume within 10bps of best bid
    ask_depth_10bps: Decimal          # Volume within 10bps of best ask
    bid_depth_50bps: Decimal          # Volume within 50bps of best bid
    ask_depth_50bps: Decimal          # Volume within 50bps of best ask
    total_bid_volume: Decimal
    total_ask_volume: Decimal
    imbalance: Decimal                # (bid - ask) / (bid + ask), positive = buy pressure


class OrderBookAnalyzer:
    """
    Analyzes order book depth for execution quality estimation.

    Key metrics:
    - VWAP: What price will you actually get for a given size?
    - Slippage: How much worse than the quoted price?
    - Liquidity: How much can you trade before moving the market?
    """

    def __init__(
        self,
        max_slippage_pct: Decimal = Decimal("0.05"),  # 5% max slippage
        min_liquidity_multiple: Decimal = Decimal("3"),  # 3x order size in book
    ):
        self.max_slippage_pct = max_slippage_pct
        self.min_liquidity_multiple = min_liquidity_multiple

    def calculate_vwap(
        self,
        orderbook: OrderBook,
        side: OrderSide,
        size: Decimal,
    ) -> VWAPResult:
        """
        Calculate the volume-weighted average price for a given order size.

        Args:
            orderbook: The order book to analyze
            side: BUY (use asks) or SELL (use bids)
            size: The order size to fill

        Returns:
            VWAPResult with execution price estimate
        """
        # Get the relevant side of the book
        if side == OrderSide.BUY:
            # Buying: walk through asks (ascending price)
            levels = sorted(orderbook.asks, key=lambda x: x.price)
            best_price = orderbook.best_ask
        else:
            # Selling: walk through bids (descending price)
            levels = sorted(orderbook.bids, key=lambda x: x.price, reverse=True)
            best_price = orderbook.best_bid

        if not levels or best_price is None:
            return VWAPResult(
                vwap=Decimal("0"),
                total_size_available=Decimal("0"),
                levels_consumed=0,
                worst_price=Decimal("0"),
                price_impact=Decimal("0"),
                price_impact_pct=Decimal("0"),
                fully_filled=False,
                remaining_size=size,
            )

        # Walk through levels accumulating fill
        total_cost = Decimal("0")
        total_filled = Decimal("0")
        levels_used = 0
        worst_price = best_price

        for level in levels:
            if total_filled >= size:
                break

            fill_at_level = min(level.size, size - total_filled)
            total_cost += fill_at_level * level.price
            total_filled += fill_at_level
            levels_used += 1
            worst_price = level.price

        # Calculate VWAP
        if total_filled > 0:
            vwap = total_cost / total_filled
        else:
            vwap = best_price

        # Calculate price impact
        if side == OrderSide.BUY:
            price_impact = vwap - best_price  # Positive = slippage
        else:
            price_impact = best_price - vwap  # Positive = slippage

        price_impact_pct = (
            price_impact / best_price if best_price > 0 else Decimal("0")
        )

        return VWAPResult(
            vwap=vwap.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
            total_size_available=total_filled,
            levels_consumed=levels_used,
            worst_price=worst_price,
            price_impact=price_impact.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
            price_impact_pct=price_impact_pct.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
            fully_filled=total_filled >= size,
            remaining_size=max(Decimal("0"), size - total_filled),
        )

    def estimate_slippage(
        self,
        orderbook: OrderBook,
        side: OrderSide,
        size: Decimal,
    ) -> SlippageEstimate:
        """
        Estimate slippage for a trade.

        Args:
            orderbook: The order book to analyze
            side: BUY or SELL
            size: The order size

        Returns:
            SlippageEstimate with execution cost analysis
        """
        vwap_result = self.calculate_vwap(orderbook, side, size)

        best_price = orderbook.best_ask if side == OrderSide.BUY else orderbook.best_bid

        if best_price is None or best_price == 0:
            return SlippageEstimate(
                estimated_fill_price=Decimal("0"),
                slippage_cents=Decimal("0"),
                slippage_pct=Decimal("0"),
                max_fill_size=Decimal("0"),
                execution_feasible=False,
                warning="No liquidity on this side of the book",
            )

        slippage_pct = vwap_result.price_impact_pct

        # Check feasibility
        warning = None
        feasible = True

        if not vwap_result.fully_filled:
            feasible = False
            warning = f"Insufficient liquidity: only {vwap_result.total_size_available} available"
        elif slippage_pct > self.max_slippage_pct:
            feasible = False
            warning = f"Slippage too high: {slippage_pct:.2%} > {self.max_slippage_pct:.2%}"
        elif vwap_result.levels_consumed > 5:
            warning = f"Order consumes {vwap_result.levels_consumed} price levels"

        return SlippageEstimate(
            estimated_fill_price=vwap_result.vwap,
            slippage_cents=vwap_result.price_impact * 100,  # Convert to cents
            slippage_pct=slippage_pct,
            max_fill_size=vwap_result.total_size_available,
            execution_feasible=feasible,
            warning=warning,
        )

    def get_max_size_for_slippage(
        self,
        orderbook: OrderBook,
        side: OrderSide,
        max_slippage_pct: Decimal,
    ) -> Decimal:
        """
        Find the maximum order size achievable within a slippage limit.

        Binary search for the largest size where slippage <= max_slippage_pct.
        """
        # Get total available
        if side == OrderSide.BUY:
            total_available = sum(level.size for level in orderbook.asks)
        else:
            total_available = sum(level.size for level in orderbook.bids)

        if total_available == 0:
            return Decimal("0")

        # Binary search
        low = Decimal("0")
        high = total_available
        best_size = Decimal("0")

        for _ in range(20):  # Max 20 iterations
            mid = (low + high) / 2
            vwap = self.calculate_vwap(orderbook, side, mid)

            if vwap.price_impact_pct <= max_slippage_pct:
                best_size = mid
                low = mid
            else:
                high = mid

            if high - low < Decimal("0.01"):
                break

        return best_size.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def analyze_liquidity(
        self,
        orderbook: OrderBook,
    ) -> LiquiditySnapshot:
        """
        Analyze overall liquidity in the order book.

        Returns a snapshot of liquidity metrics useful for:
        - Position sizing
        - Execution timing
        - Market health monitoring
        """
        best_bid = orderbook.best_bid
        best_ask = orderbook.best_ask

        spread = None
        spread_pct = None
        if best_bid and best_ask:
            spread = best_ask - best_bid
            midpoint = (best_bid + best_ask) / 2
            spread_pct = spread / midpoint if midpoint > 0 else None

        # Calculate depth at different price levels
        bid_depth_10bps = Decimal("0")
        bid_depth_50bps = Decimal("0")
        total_bid = Decimal("0")

        if best_bid:
            threshold_10bps = best_bid * Decimal("0.999")  # 10bps = 0.1%
            threshold_50bps = best_bid * Decimal("0.995")  # 50bps = 0.5%

            for level in orderbook.bids:
                total_bid += level.size
                if level.price >= threshold_10bps:
                    bid_depth_10bps += level.size
                if level.price >= threshold_50bps:
                    bid_depth_50bps += level.size

        ask_depth_10bps = Decimal("0")
        ask_depth_50bps = Decimal("0")
        total_ask = Decimal("0")

        if best_ask:
            threshold_10bps = best_ask * Decimal("1.001")  # 10bps = 0.1%
            threshold_50bps = best_ask * Decimal("1.005")  # 50bps = 0.5%

            for level in orderbook.asks:
                total_ask += level.size
                if level.price <= threshold_10bps:
                    ask_depth_10bps += level.size
                if level.price <= threshold_50bps:
                    ask_depth_50bps += level.size

        # Calculate order book imbalance
        total_volume = total_bid + total_ask
        imbalance = (
            (total_bid - total_ask) / total_volume
            if total_volume > 0 else Decimal("0")
        )

        return LiquiditySnapshot(
            token_id=orderbook.token_id,
            timestamp=orderbook.timestamp,
            best_bid=best_bid,
            best_ask=best_ask,
            spread=spread,
            spread_pct=spread_pct,
            bid_depth_10bps=bid_depth_10bps,
            ask_depth_10bps=ask_depth_10bps,
            bid_depth_50bps=bid_depth_50bps,
            ask_depth_50bps=ask_depth_50bps,
            total_bid_volume=total_bid,
            total_ask_volume=total_ask,
            imbalance=imbalance,
        )

    def check_execution_quality(
        self,
        orderbook: OrderBook,
        side: OrderSide,
        size: Decimal,
        price: Decimal,
    ) -> Dict[str, Any]:
        """
        Comprehensive check of execution quality for a proposed trade.

        Returns a dict with:
        - vwap: Expected fill price
        - slippage: Expected slippage
        - feasible: Whether trade is recommended
        - liquidity_ratio: How much of the book this order consumes
        - recommendation: Suggested action
        """
        vwap_result = self.calculate_vwap(orderbook, side, size)
        liquidity = self.analyze_liquidity(orderbook)

        best_price = orderbook.best_ask if side == OrderSide.BUY else orderbook.best_bid
        total_liquidity = (
            liquidity.total_ask_volume if side == OrderSide.BUY
            else liquidity.total_bid_volume
        )

        # Calculate liquidity consumption ratio
        liquidity_ratio = (
            size / total_liquidity if total_liquidity > 0 else Decimal("1")
        )

        # Determine recommendation
        feasible = True
        recommendation = "proceed"
        warnings = []

        if not vwap_result.fully_filled:
            feasible = False
            recommendation = "reduce_size"
            warnings.append(f"Only {vwap_result.total_size_available} available")

        if vwap_result.price_impact_pct > self.max_slippage_pct:
            feasible = False
            recommendation = "reduce_size_or_wait"
            warnings.append(f"Slippage {vwap_result.price_impact_pct:.2%} exceeds limit")

        if liquidity_ratio > Decimal("0.5"):
            warnings.append(f"Order consumes {liquidity_ratio:.0%} of available liquidity")
            if feasible:
                recommendation = "proceed_with_caution"

        if liquidity.spread_pct and liquidity.spread_pct > Decimal("0.05"):
            warnings.append(f"Wide spread: {liquidity.spread_pct:.2%}")

        # Check if limit price is reasonable
        if best_price:
            if side == OrderSide.BUY and price < best_price:
                warnings.append(f"Limit price ${price:.2f} below best ask ${best_price:.2f}")
            elif side == OrderSide.SELL and price > best_price:
                warnings.append(f"Limit price ${price:.2f} above best bid ${best_price:.2f}")

        return {
            "vwap": vwap_result.vwap,
            "best_price": best_price,
            "slippage_cents": vwap_result.price_impact * 100,
            "slippage_pct": vwap_result.price_impact_pct,
            "levels_consumed": vwap_result.levels_consumed,
            "liquidity_ratio": liquidity_ratio,
            "spread": liquidity.spread,
            "spread_pct": liquidity.spread_pct,
            "feasible": feasible,
            "recommendation": recommendation,
            "warnings": warnings,
            "max_size_at_1pct_slippage": self.get_max_size_for_slippage(
                orderbook, side, Decimal("0.01")
            ),
        }


def estimate_execution_cost(
    orderbook: OrderBook,
    side: OrderSide,
    size: Decimal,
    gas_cost: Decimal = Decimal("0.02"),  # Polygon gas ~$0.02
) -> Dict[str, Decimal]:
    """
    Estimate total execution cost including gas.

    Returns breakdown of:
    - notional: Base trade value
    - slippage_cost: Cost due to slippage
    - gas_cost: Network fees
    - total_cost: All-in cost
    - cost_pct: Total cost as percentage of notional
    """
    analyzer = OrderBookAnalyzer()
    vwap = analyzer.calculate_vwap(orderbook, side, size)

    best_price = orderbook.best_ask if side == OrderSide.BUY else orderbook.best_bid
    best_price = best_price or Decimal("0")

    notional = size * best_price
    slippage_cost = size * vwap.price_impact
    total_cost = slippage_cost + gas_cost

    cost_pct = total_cost / notional if notional > 0 else Decimal("0")

    return {
        "notional": notional,
        "slippage_cost": slippage_cost,
        "gas_cost": gas_cost,
        "total_cost": total_cost,
        "cost_pct": cost_pct,
        "vwap": vwap.vwap,
    }
