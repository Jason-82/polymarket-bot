"""Threshold value trader (directional) strategy."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

from strategies.base import DirectionalStrategy, StrategyContext, RiskBudget
from strategies.loader import register_strategy
from storage.models import OrderIntent, OrderSide, OrderType
from execution.order_intent import round_to_tick, clamp_price


@dataclass
class ValueThresholdConfig:
    """Configuration for the value threshold strategy."""
    # Entry threshold - minimum deviation to trigger trade
    entry_threshold: Decimal = Decimal("0.05")  # 5 cents

    # Exit threshold - deviation at which to close position
    exit_threshold: Decimal = Decimal("0.02")  # 2 cents

    # Position sizing
    order_size: Decimal = Decimal("20")
    max_position: Decimal = Decimal("100")

    # Fair value settings
    # If no external model, use midpoint as fair value estimate
    use_midpoint_as_fair_value: bool = True

    # Manual fair value overrides (token_id -> fair_value)
    fair_value_overrides: Dict[str, Decimal] = None

    def __post_init__(self):
        if self.fair_value_overrides is None:
            self.fair_value_overrides = {}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ValueThresholdConfig":
        """Create config from dictionary."""
        overrides = {}
        if "fair_value_overrides" in data:
            overrides = {
                k: Decimal(str(v))
                for k, v in data["fair_value_overrides"].items()
            }

        return cls(
            entry_threshold=Decimal(str(data.get("entry_threshold", cls.entry_threshold))),
            exit_threshold=Decimal(str(data.get("exit_threshold", cls.exit_threshold))),
            order_size=Decimal(str(data.get("order_size", cls.order_size))),
            max_position=Decimal(str(data.get("max_position", cls.max_position))),
            use_midpoint_as_fair_value=data.get("use_midpoint_as_fair_value", cls.use_midpoint_as_fair_value),
            fair_value_overrides=overrides,
        )


@register_strategy("value_threshold")
class ValueThresholdStrategy(DirectionalStrategy):
    """
    Threshold value trader strategy.

    This strategy:
    - Uses a pluggable "fair value" estimator (initially manual or simple model)
    - Places orders only if price deviates by X from fair value
    - Focuses on a small set of markets chosen by config

    This is a scaffold for experimentation - does NOT guarantee profit.
    """

    name = "value_threshold"

    def __init__(self, name: Optional[str] = None, config: Optional[Dict[str, Any]] = None):
        super().__init__(name, config)
        self.vt_config = ValueThresholdConfig.from_dict(config or {})

        # Pluggable fair value estimator
        self._fair_value_fn: Optional[Callable[[str, StrategyContext], Optional[Decimal]]] = None

    def get_risk_budget(self) -> RiskBudget:
        """Declare risk budget for this strategy."""
        return RiskBudget(
            max_position_size=self.vt_config.max_position,
            max_order_size=self.vt_config.order_size,
            max_open_orders=10,
            max_exposure=self.vt_config.max_position * Decimal("2"),
            allow_scaling=True,
        )

    def set_fair_value_estimator(
        self,
        fn: Callable[[str, StrategyContext], Optional[Decimal]]
    ) -> None:
        """
        Set a custom fair value estimation function.

        The function takes (token_id, context) and returns the estimated fair value
        or None if no estimate is available.
        """
        self._fair_value_fn = fn

    async def on_tick(self, context: StrategyContext) -> List[OrderIntent]:
        """Evaluate fair value and generate trades if threshold is met."""
        intents: List[OrderIntent] = []

        if context.mode == "READ_ONLY":
            return intents

        # Get tokens to trade from config
        token_ids = self.config.get("tokens", [])

        for token_id in token_ids:
            orderbook = context.get_orderbook(token_id)
            if not orderbook or orderbook.midpoint is None:
                continue

            # Get fair value
            fair_value = self._get_fair_value(token_id, context)
            if fair_value is None:
                continue

            # Get current position
            position = context.get_position(token_id)
            current_position = position.shares if position else Decimal("0")

            # Get current market price (midpoint)
            current_price = orderbook.midpoint

            # Determine action based on deviation
            new_intent = self._evaluate_trade(
                token_id=token_id,
                fair_value=fair_value,
                current_price=current_price,
                current_position=current_position,
                orderbook=orderbook,
            )

            if new_intent:
                intents.append(new_intent)

        return intents

    def _get_fair_value(
        self,
        token_id: str,
        context: StrategyContext
    ) -> Optional[Decimal]:
        """Get fair value for a token."""
        # Check manual overrides first
        if token_id in self.vt_config.fair_value_overrides:
            return self.vt_config.fair_value_overrides[token_id]

        # Check custom estimator
        if self._fair_value_fn:
            fv = self._fair_value_fn(token_id, context)
            if fv is not None:
                return fv

        # Fall back to midpoint if configured
        if self.vt_config.use_midpoint_as_fair_value:
            orderbook = context.get_orderbook(token_id)
            if orderbook:
                return orderbook.midpoint

        return None

    def _evaluate_trade(
        self,
        token_id: str,
        fair_value: Decimal,
        current_price: Decimal,
        current_position: Decimal,
        orderbook,
    ) -> Optional[OrderIntent]:
        """Evaluate whether to enter, exit, or do nothing."""
        deviation = current_price - fair_value

        # If we have a position, check for exit
        if current_position != 0:
            return self._evaluate_exit(
                token_id=token_id,
                deviation=deviation,
                current_position=current_position,
                orderbook=orderbook,
            )

        # No position - check for entry
        return self._evaluate_entry(
            token_id=token_id,
            deviation=deviation,
            orderbook=orderbook,
        )

    def _evaluate_entry(
        self,
        token_id: str,
        deviation: Decimal,
        orderbook,
    ) -> Optional[OrderIntent]:
        """Evaluate entry conditions."""
        threshold = self.vt_config.entry_threshold

        if deviation < -threshold:
            # Underpriced - buy
            # Use best ask + small buffer for limit price
            price = orderbook.best_ask
            if price is None:
                return None

            return OrderIntent(
                token_id=token_id,
                side=OrderSide.BUY,
                price=clamp_price(round_to_tick(price)),
                size=self.vt_config.order_size,
                order_type=OrderType.GTC,
                strategy_name=self.name,
            )

        elif deviation > threshold:
            # Overpriced - sell (go short if allowed, or skip)
            # For MVP, we don't short
            pass

        return None

    def _evaluate_exit(
        self,
        token_id: str,
        deviation: Decimal,
        current_position: Decimal,
        orderbook,
    ) -> Optional[OrderIntent]:
        """Evaluate exit conditions."""
        threshold = self.vt_config.exit_threshold

        if current_position > 0:
            # Long position - exit if overpriced (deviation > exit_threshold)
            # or if no longer underpriced (deviation > -exit_threshold)
            if deviation >= -threshold:
                # Close long position
                price = orderbook.best_bid
                if price is None:
                    return None

                return OrderIntent(
                    token_id=token_id,
                    side=OrderSide.SELL,
                    price=clamp_price(round_to_tick(price)),
                    size=min(self.vt_config.order_size, current_position),
                    order_type=OrderType.GTC,
                    strategy_name=self.name,
                )

        elif current_position < 0:
            # Short position - exit if underpriced
            if deviation <= threshold:
                price = orderbook.best_ask
                if price is None:
                    return None

                return OrderIntent(
                    token_id=token_id,
                    side=OrderSide.BUY,
                    price=clamp_price(round_to_tick(price)),
                    size=min(self.vt_config.order_size, abs(current_position)),
                    order_type=OrderType.GTC,
                    strategy_name=self.name,
                )

        return None

    def get_status(self) -> Dict[str, Any]:
        """Return strategy status."""
        return {
            "name": self.name,
            "initialized": self._initialized,
            "config": {
                "entry_threshold": str(self.vt_config.entry_threshold),
                "exit_threshold": str(self.vt_config.exit_threshold),
                "order_size": str(self.vt_config.order_size),
                "max_position": str(self.vt_config.max_position),
            },
            "fair_value_overrides": {
                k: str(v) for k, v in self.vt_config.fair_value_overrides.items()
            },
            "has_custom_estimator": self._fair_value_fn is not None,
        }
