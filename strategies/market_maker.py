"""Passive two-sided quoter (market-making) strategy."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional

from strategies.base import PassiveStrategy, StrategyContext, RiskBudget
from strategies.loader import register_strategy
from storage.models import OrderIntent, OrderSide, OrderType
from execution.order_intent import round_to_tick, clamp_price


@dataclass
class MarketMakerConfig:
    """Configuration for the market maker strategy."""
    # Spread settings
    min_spread: Decimal = Decimal("0.02")  # 2 cents minimum spread
    target_spread: Decimal = Decimal("0.04")  # 4 cents target spread

    # Size settings
    quote_size: Decimal = Decimal("10")  # Size per side
    max_position: Decimal = Decimal("100")  # Max position before stopping

    # Inventory management
    inventory_skew: Decimal = Decimal("0.001")  # Price skew per share of inventory
    reduce_size_on_inventory: bool = True  # Reduce quote size when inventory high

    # Quote management
    requote_threshold: Decimal = Decimal("0.005")  # Minimum price change to requote

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MarketMakerConfig":
        """Create config from dictionary."""
        return cls(
            min_spread=Decimal(str(data.get("min_spread", cls.min_spread))),
            target_spread=Decimal(str(data.get("target_spread", cls.target_spread))),
            quote_size=Decimal(str(data.get("quote_size", cls.quote_size))),
            max_position=Decimal(str(data.get("max_position", cls.max_position))),
            inventory_skew=Decimal(str(data.get("inventory_skew", cls.inventory_skew))),
            reduce_size_on_inventory=data.get("reduce_size_on_inventory", cls.reduce_size_on_inventory),
            requote_threshold=Decimal(str(data.get("requote_threshold", cls.requote_threshold))),
        )


@register_strategy("market_maker")
class MarketMakerStrategy(PassiveStrategy):
    """
    Passive two-sided quoter strategy.

    This strategy:
    - Quotes bid/ask around the midpoint with configurable spread
    - Skews quotes based on inventory (reduces exposure on heavy side)
    - Respects tick size rounding
    - Does NOT guarantee profit - this is a scaffold for experimentation
    """

    name = "market_maker"

    def __init__(self, name: Optional[str] = None, config: Optional[Dict[str, Any]] = None):
        super().__init__(name, config)
        self.mm_config = MarketMakerConfig.from_dict(config or {})
        self._last_quotes: Dict[str, tuple[Decimal, Decimal]] = {}  # token_id -> (bid, ask)

    def get_risk_budget(self) -> RiskBudget:
        """Declare risk budget for this strategy."""
        return RiskBudget(
            max_position_size=self.mm_config.max_position,
            max_order_size=self.mm_config.quote_size,
            max_open_orders=20,  # 2 orders per token, up to 10 tokens
            max_exposure=self.mm_config.max_position * Decimal("2"),
            allow_scaling=True,
        )

    async def on_tick(self, context: StrategyContext) -> List[OrderIntent]:
        """Generate two-sided quotes for configured tokens."""
        intents: List[OrderIntent] = []

        if context.mode == "READ_ONLY":
            return intents

        # Get tokens to quote from config
        token_ids = self.config.get("tokens", [])

        for token_id in token_ids:
            orderbook = context.get_orderbook(token_id)
            if not orderbook or orderbook.midpoint is None:
                continue

            # Get current position
            position = context.get_position(token_id)
            inventory = position.shares if position else Decimal("0")

            # Check if we're at max position
            if abs(inventory) >= self.mm_config.max_position:
                # Only quote on the reducing side
                new_intents = self._generate_reducing_quotes(
                    token_id, orderbook.midpoint, inventory
                )
            else:
                # Generate two-sided quotes
                new_intents = self._generate_two_sided_quotes(
                    token_id, orderbook.midpoint, inventory
                )

            # Check if we need to requote (avoid churning)
            if self._should_requote(token_id, new_intents):
                intents.extend(new_intents)
                self._update_last_quotes(token_id, new_intents)

        return intents

    def _generate_two_sided_quotes(
        self,
        token_id: str,
        midpoint: Decimal,
        inventory: Decimal,
    ) -> List[OrderIntent]:
        """Generate bid and ask quotes."""
        intents = []

        # Calculate skewed prices
        bid_price, bid_size, ask_price, ask_size = self.calculate_quotes(
            midpoint=midpoint,
            spread=self.mm_config.target_spread,
            size=self.mm_config.quote_size,
            inventory=inventory,
            inventory_skew=self.mm_config.inventory_skew,
        )

        # Reduce size based on inventory if configured
        if self.mm_config.reduce_size_on_inventory:
            inventory_ratio = abs(inventory) / self.mm_config.max_position
            size_factor = max(Decimal("0.2"), 1 - inventory_ratio)
            if inventory > 0:
                bid_size *= size_factor  # Reduce bids when long
            else:
                ask_size *= size_factor  # Reduce asks when short

        # Round to tick and clamp
        bid_price = clamp_price(round_to_tick(bid_price))
        ask_price = clamp_price(round_to_tick(ask_price))

        # Ensure minimum spread
        if ask_price - bid_price < self.mm_config.min_spread:
            half_min = self.mm_config.min_spread / 2
            bid_price = clamp_price(round_to_tick(midpoint - half_min))
            ask_price = clamp_price(round_to_tick(midpoint + half_min))

        # Create order intents
        if bid_size > 0:
            intents.append(OrderIntent(
                token_id=token_id,
                side=OrderSide.BUY,
                price=bid_price,
                size=round(bid_size, 2),
                order_type=OrderType.GTC,
                strategy_name=self.name,
            ))

        if ask_size > 0:
            intents.append(OrderIntent(
                token_id=token_id,
                side=OrderSide.SELL,
                price=ask_price,
                size=round(ask_size, 2),
                order_type=OrderType.GTC,
                strategy_name=self.name,
            ))

        return intents

    def _generate_reducing_quotes(
        self,
        token_id: str,
        midpoint: Decimal,
        inventory: Decimal,
    ) -> List[OrderIntent]:
        """Generate quotes only on the side that reduces position."""
        intents = []

        if inventory > 0:
            # Long, only sell
            ask_price = clamp_price(round_to_tick(
                midpoint + self.mm_config.target_spread / 2
            ))
            intents.append(OrderIntent(
                token_id=token_id,
                side=OrderSide.SELL,
                price=ask_price,
                size=min(self.mm_config.quote_size, inventory),
                order_type=OrderType.GTC,
                strategy_name=self.name,
            ))
        elif inventory < 0:
            # Short (if allowed), only buy
            bid_price = clamp_price(round_to_tick(
                midpoint - self.mm_config.target_spread / 2
            ))
            intents.append(OrderIntent(
                token_id=token_id,
                side=OrderSide.BUY,
                price=bid_price,
                size=min(self.mm_config.quote_size, abs(inventory)),
                order_type=OrderType.GTC,
                strategy_name=self.name,
            ))

        return intents

    def _should_requote(self, token_id: str, new_intents: List[OrderIntent]) -> bool:
        """Check if quotes have changed enough to warrant an update."""
        if token_id not in self._last_quotes:
            return True

        last_bid, last_ask = self._last_quotes[token_id]

        # Find new bid and ask
        new_bid = new_ask = None
        for intent in new_intents:
            if intent.side == OrderSide.BUY:
                new_bid = intent.price
            elif intent.side == OrderSide.SELL:
                new_ask = intent.price

        # Check if change exceeds threshold
        threshold = self.mm_config.requote_threshold

        if new_bid is not None and abs(new_bid - last_bid) >= threshold:
            return True
        if new_ask is not None and abs(new_ask - last_ask) >= threshold:
            return True

        return False

    def _update_last_quotes(self, token_id: str, intents: List[OrderIntent]) -> None:
        """Update last quote cache."""
        bid = ask = Decimal("0")
        for intent in intents:
            if intent.side == OrderSide.BUY:
                bid = intent.price
            elif intent.side == OrderSide.SELL:
                ask = intent.price
        self._last_quotes[token_id] = (bid, ask)

    def get_status(self) -> Dict[str, Any]:
        """Return strategy status."""
        return {
            "name": self.name,
            "initialized": self._initialized,
            "config": {
                "target_spread": str(self.mm_config.target_spread),
                "quote_size": str(self.mm_config.quote_size),
                "max_position": str(self.mm_config.max_position),
            },
            "active_quotes": len(self._last_quotes),
        }
