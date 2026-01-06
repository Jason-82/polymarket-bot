"""Strategy plugin interface - base classes for all strategies."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional

from storage.models import (
    Market, OrderBook, Position, Order, OrderIntent, Fill,
    OrderSide, OrderType
)


@dataclass
class RiskBudget:
    """Risk budget that a strategy requests."""
    max_position_size: Decimal = Decimal("100")
    max_order_size: Decimal = Decimal("50")
    max_open_orders: int = 10
    max_exposure: Decimal = Decimal("500")
    # Allow the risk manager to scale down if needed
    allow_scaling: bool = True


@dataclass
class StrategyContext:
    """
    Immutable context provided to strategies each tick.

    Strategies receive all the information they need to make decisions
    but cannot call exchange APIs directly.
    """
    timestamp: datetime

    # Market data
    orderbooks: Dict[str, OrderBook]      # token_id -> OrderBook
    market_metadata: Dict[str, Market]    # market_id -> Market
    token_to_market: Dict[str, str]       # token_id -> market_id

    # Account state
    positions: Dict[str, Position]        # token_id -> Position
    open_orders: List[Order]
    account_balance: Decimal

    # Operating mode
    mode: Literal["READ_ONLY", "PAPER", "LIVE"]

    # Strategy-specific config
    config: Dict[str, Any] = field(default_factory=dict)

    def get_orderbook(self, token_id: str) -> Optional[OrderBook]:
        """Get orderbook for a token."""
        return self.orderbooks.get(token_id)

    def get_position(self, token_id: str) -> Optional[Position]:
        """Get position for a token."""
        return self.positions.get(token_id)

    def get_market_for_token(self, token_id: str) -> Optional[Market]:
        """Get market metadata for a token."""
        market_id = self.token_to_market.get(token_id)
        if market_id:
            return self.market_metadata.get(market_id)
        return None

    def get_open_orders_for_token(self, token_id: str) -> List[Order]:
        """Get open orders for a specific token."""
        return [o for o in self.open_orders if o.token_id == token_id]

    def get_net_position(self, token_id: str) -> Decimal:
        """Get net position size for a token."""
        pos = self.get_position(token_id)
        return pos.shares if pos else Decimal("0")

    def get_midpoint(self, token_id: str) -> Optional[Decimal]:
        """Get midpoint price for a token."""
        ob = self.get_orderbook(token_id)
        return ob.midpoint if ob else None


class StrategyBase(ABC):
    """
    Base class for all trading strategies.

    Strategies are plugins that:
    - Receive market state each tick
    - Output a list of desired OrderIntents
    - Must be deterministic for the same inputs (to aid testing)
    - Must not call exchange APIs directly

    The Risk Manager is the final authority on which orders actually execute.
    """

    # Strategy name for identification
    name: str = "unnamed_strategy"

    def __init__(self, name: Optional[str] = None, config: Optional[Dict[str, Any]] = None):
        """Initialize strategy with optional name and config."""
        if name:
            self.name = name
        self.config = config or {}
        self._initialized = False

    async def initialize(self, context: StrategyContext) -> None:
        """
        Optional initialization hook called once at startup.

        Override to set up strategy state based on initial context.
        """
        self._initialized = True

    @abstractmethod
    async def on_tick(self, context: StrategyContext) -> List[OrderIntent]:
        """
        Called on each tick/event.

        Must return a list of desired OrderIntents. These represent
        the strategy's ideal orders, which the Risk Manager may modify
        or reject.

        Args:
            context: Current market state and account info

        Returns:
            List of OrderIntent objects
        """
        pass

    @abstractmethod
    def get_risk_budget(self) -> RiskBudget:
        """
        Declare the risk budget this strategy requests.

        The Risk Manager may scale down or reject if limits are exceeded.
        """
        pass

    def on_fill(self, fill: Fill) -> None:
        """
        Optional callback when a fill occurs for this strategy's orders.

        Override to update strategy state on fills.
        """
        pass

    def on_cancel(self, order_id: str, reason: str) -> None:
        """
        Optional callback when an order is cancelled.

        Override to handle cancellation events.
        """
        pass

    def on_reject(self, intent: OrderIntent, reason: str) -> None:
        """
        Optional callback when Risk Manager rejects an order intent.

        Override to handle rejection events.
        """
        pass

    def get_status(self) -> Dict[str, Any]:
        """
        Return strategy status for monitoring/debugging.

        Override to provide strategy-specific status info.
        """
        return {
            "name": self.name,
            "initialized": self._initialized,
        }

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r})"


class PassiveStrategy(StrategyBase):
    """
    Base class for passive (market-making) strategies.

    Provides helper methods for two-sided quoting.
    """

    def calculate_quotes(
        self,
        midpoint: Decimal,
        spread: Decimal,
        size: Decimal,
        inventory: Decimal = Decimal("0"),
        inventory_skew: Decimal = Decimal("0.001"),
    ) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        """
        Calculate bid and ask quotes with optional inventory skew.

        Args:
            midpoint: Current midpoint price
            spread: Total spread (bid to ask)
            size: Quote size
            inventory: Current inventory (positive = long)
            inventory_skew: How much to skew per unit of inventory

        Returns:
            Tuple of (bid_price, bid_size, ask_price, ask_size)
        """
        half_spread = spread / 2

        # Skew quotes based on inventory (reduce quote on side we're heavy)
        skew = inventory * inventory_skew

        bid_price = midpoint - half_spread - skew
        ask_price = midpoint + half_spread - skew

        # Could also adjust sizes based on inventory
        bid_size = size
        ask_size = size

        return bid_price, bid_size, ask_price, ask_size


class DirectionalStrategy(StrategyBase):
    """
    Base class for directional (signal-based) strategies.

    Provides helper methods for threshold-based trading.
    """

    def should_trade(
        self,
        fair_value: Decimal,
        current_price: Decimal,
        threshold: Decimal,
    ) -> Optional[OrderSide]:
        """
        Determine if we should trade based on price deviation.

        Args:
            fair_value: Our estimate of fair value
            current_price: Current market price
            threshold: Minimum deviation to trigger trade

        Returns:
            OrderSide.BUY if underpriced, OrderSide.SELL if overpriced, None otherwise
        """
        deviation = current_price - fair_value

        if deviation < -threshold:
            return OrderSide.BUY  # Underpriced, buy
        elif deviation > threshold:
            return OrderSide.SELL  # Overpriced, sell

        return None
