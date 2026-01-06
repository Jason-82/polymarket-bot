"""Risk management and pre-trade checks."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from bot.config import RiskLimits
from storage.models import OrderIntent, OrderSide, OrderType, Position, Order
from execution.order_intent import TICK_SIZE, MIN_PRICE, MAX_PRICE, round_to_tick, clamp_price
from monitoring.logger import get_logger

logger = get_logger(__name__)


@dataclass
class RiskCheckResult:
    """Result of a risk check."""
    passed: bool
    reason: Optional[str] = None
    adjustments: Dict[str, str] = None

    def __post_init__(self):
        if self.adjustments is None:
            self.adjustments = {}


class RiskManager:
    """
    Risk management layer between strategies and OMS.

    Responsibilities:
    - Pre-trade validation
    - Position/exposure limits
    - Order size clamping
    - Circuit breaker monitoring
    - Kill switch management
    """

    def __init__(self, limits: RiskLimits):
        self.limits = limits

        # Tracking
        self._daily_pnl = Decimal("0")
        self._peak_value = Decimal("0")
        self._current_value = Decimal("0")
        self._error_count = 0
        self._last_error_reset = datetime.utcnow()

        # Kill switch state
        self._kill_switch_active = False
        self._kill_switch_reason: Optional[str] = None

    @property
    def is_halted(self) -> bool:
        """Check if trading is halted."""
        return self._kill_switch_active

    @property
    def kill_switch_reason(self) -> Optional[str]:
        """Get kill switch reason if active."""
        return self._kill_switch_reason

    def clamp_intents(
        self,
        intents: List[OrderIntent],
        positions: Dict[str, Position],
        open_orders: List[Order],
        geoblock_allowed: bool,
        mode: str,
    ) -> Tuple[List[OrderIntent], Dict[str, str]]:
        """
        Apply risk limits to order intents.

        Args:
            intents: Raw order intents from strategy
            positions: Current positions
            open_orders: Current open orders
            geoblock_allowed: Whether trading is allowed
            mode: Current bot mode

        Returns:
            Tuple of (safe_intents, adjustments_made)
        """
        if self._kill_switch_active:
            logger.warning("risk_kill_switch_active", reason=self._kill_switch_reason)
            return [], {"all": f"Kill switch active: {self._kill_switch_reason}"}

        if mode == "LIVE" and not geoblock_allowed:
            logger.warning("risk_geoblock_blocked")
            return [], {"all": "Geoblock: trading not allowed"}

        if mode == "READ_ONLY":
            return [], {"all": "READ_ONLY mode: no orders allowed"}

        safe_intents = []
        adjustments = {}

        # Calculate current exposure
        current_exposure = sum(pos.cost_basis for pos in positions.values())
        current_open_notional = sum(o.remaining_size * o.price for o in open_orders)

        for intent in intents:
            result, adjusted_intent = self._check_intent(
                intent, positions, current_exposure, current_open_notional
            )

            if result.passed and adjusted_intent:
                safe_intents.append(adjusted_intent)
                if result.adjustments:
                    adjustments[intent.client_order_id] = str(result.adjustments)
            else:
                adjustments[intent.client_order_id] = result.reason or "rejected"
                logger.debug(
                    "risk_intent_rejected",
                    token_id=intent.token_id,
                    reason=result.reason,
                )

        return safe_intents, adjustments

    def _check_intent(
        self,
        intent: OrderIntent,
        positions: Dict[str, Position],
        current_exposure: Decimal,
        current_open_notional: Decimal,
    ) -> Tuple[RiskCheckResult, Optional[OrderIntent]]:
        """Check and potentially adjust a single order intent."""
        adjustments = {}

        # 1. Price bounds check
        if intent.price < MIN_PRICE or intent.price > MAX_PRICE:
            return RiskCheckResult(
                passed=False,
                reason=f"Price {intent.price} out of bounds [{MIN_PRICE}, {MAX_PRICE}]"
            ), None

        # 2. Tick size compliance
        adjusted_price = round_to_tick(intent.price)
        if adjusted_price != intent.price:
            adjustments["price"] = f"{intent.price} -> {adjusted_price}"
            intent = OrderIntent(
                token_id=intent.token_id,
                side=intent.side,
                price=adjusted_price,
                size=intent.size,
                order_type=intent.order_type,
                expiration_ts=intent.expiration_ts,
                client_order_id=intent.client_order_id,
                strategy_name=intent.strategy_name,
            )

        # 3. Size validation
        if intent.size <= 0:
            return RiskCheckResult(passed=False, reason="Size must be positive"), None

        # 4. Max order size per token
        if intent.size > self.limits.max_order_size_per_token:
            adjusted_size = self.limits.max_order_size_per_token
            adjustments["size"] = f"{intent.size} -> {adjusted_size}"
            intent = OrderIntent(
                token_id=intent.token_id,
                side=intent.side,
                price=intent.price,
                size=adjusted_size,
                order_type=intent.order_type,
                expiration_ts=intent.expiration_ts,
                client_order_id=intent.client_order_id,
                strategy_name=intent.strategy_name,
            )

        # 5. Max total open notional
        intent_notional = intent.price * intent.size
        if current_open_notional + intent_notional > self.limits.max_total_open_notional:
            remaining_budget = self.limits.max_total_open_notional - current_open_notional
            if remaining_budget <= 0:
                return RiskCheckResult(
                    passed=False,
                    reason="Max open notional exceeded"
                ), None

            adjusted_size = remaining_budget / intent.price
            if adjusted_size < Decimal("0.01"):
                return RiskCheckResult(
                    passed=False,
                    reason="Remaining notional budget too small"
                ), None

            adjustments["size_notional"] = f"{intent.size} -> {adjusted_size:.2f}"
            intent = OrderIntent(
                token_id=intent.token_id,
                side=intent.side,
                price=intent.price,
                size=adjusted_size,
                order_type=intent.order_type,
                expiration_ts=intent.expiration_ts,
                client_order_id=intent.client_order_id,
                strategy_name=intent.strategy_name,
            )

        # 6. Max exposure check
        position = positions.get(intent.token_id)
        position_size = position.shares if position else Decimal("0")

        if intent.side == OrderSide.BUY:
            new_exposure = current_exposure + intent_notional
        else:
            # Selling reduces exposure
            new_exposure = current_exposure - min(intent_notional, position_size * intent.price)

        if new_exposure > self.limits.max_total_exposure:
            return RiskCheckResult(
                passed=False,
                reason=f"Would exceed max exposure ({new_exposure} > {self.limits.max_total_exposure})"
            ), None

        return RiskCheckResult(passed=True, adjustments=adjustments), intent

    def should_halt(
        self,
        daily_pnl: Decimal,
        current_value: Decimal,
        error_count: int,
        ws_disconnected_seconds: int,
    ) -> Tuple[bool, Optional[str]]:
        """
        Check if trading should be halted.

        Returns:
            Tuple of (should_halt, reason)
        """
        # Update tracking
        self._daily_pnl = daily_pnl
        self._current_value = current_value
        if current_value > self._peak_value:
            self._peak_value = current_value

        # 1. Max daily loss
        if daily_pnl < -self.limits.max_daily_loss:
            return True, f"Max daily loss exceeded: {daily_pnl}"

        # 2. Max drawdown
        if self._peak_value > 0:
            drawdown = (self._peak_value - current_value) / self._peak_value
            if drawdown > self.limits.max_drawdown_pct:
                return True, f"Max drawdown exceeded: {drawdown:.2%}"

        # 3. Error rate
        if error_count > 10:
            return True, f"Too many errors: {error_count}"

        # 4. WebSocket disconnection
        if ws_disconnected_seconds > 300:  # 5 minutes
            return True, f"WebSocket disconnected too long: {ws_disconnected_seconds}s"

        return False, None

    def trigger_kill_switch(self, reason: str) -> None:
        """Manually trigger the kill switch."""
        self._kill_switch_active = True
        self._kill_switch_reason = reason
        logger.critical("risk_kill_switch_triggered", reason=reason)

    def reset_kill_switch(self) -> None:
        """Reset the kill switch (requires manual intervention)."""
        self._kill_switch_active = False
        self._kill_switch_reason = None
        logger.info("risk_kill_switch_reset")

    def reset_daily_tracking(self) -> None:
        """Reset daily tracking (call at start of new day)."""
        self._daily_pnl = Decimal("0")
        self._peak_value = self._current_value
        self._error_count = 0
        self._last_error_reset = datetime.utcnow()

    def record_error(self) -> None:
        """Record an error occurrence."""
        self._error_count += 1

    def get_status(self) -> Dict[str, Any]:
        """Get risk manager status for monitoring."""
        drawdown = Decimal("0")
        if self._peak_value > 0:
            drawdown = (self._peak_value - self._current_value) / self._peak_value

        return {
            "kill_switch_active": self._kill_switch_active,
            "kill_switch_reason": self._kill_switch_reason,
            "daily_pnl": str(self._daily_pnl),
            "current_value": str(self._current_value),
            "peak_value": str(self._peak_value),
            "drawdown_pct": f"{drawdown:.2%}",
            "error_count": self._error_count,
            "limits": {
                "max_daily_loss": str(self.limits.max_daily_loss),
                "max_drawdown_pct": str(self.limits.max_drawdown_pct),
                "max_order_size": str(self.limits.max_order_size_per_token),
                "max_exposure": str(self.limits.max_total_exposure),
            }
        }
