"""Order intent model - exchange-agnostic order representation."""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional
import hashlib
import time


class OrderSide(str, Enum):
    """Order side."""
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    """Order type."""
    GTC = "GTC"  # Good til cancelled
    GTD = "GTD"  # Good til date
    FOK = "FOK"  # Fill or kill


# Polymarket tick size
TICK_SIZE = Decimal("0.01")
MIN_PRICE = Decimal("0.01")
MAX_PRICE = Decimal("0.99")


@dataclass
class OrderIntent:
    """
    Exchange-agnostic order intent from strategy.

    This represents what the strategy wants to do, before risk checks
    and exchange-specific formatting.
    """
    token_id: str
    side: OrderSide
    price: Decimal
    size: Decimal
    order_type: OrderType = OrderType.GTC
    expiration_ts: Optional[int] = None
    client_order_id: str = ""
    strategy_name: str = ""

    def __post_init__(self) -> None:
        """Generate client order ID if not provided."""
        if not self.client_order_id:
            self.client_order_id = self._generate_client_order_id()

    def _generate_client_order_id(self) -> str:
        """Generate a deterministic client order ID."""
        # Use timestamp + intent details for uniqueness
        ts = int(time.time() * 1000)
        data = f"{ts}:{self.token_id}:{self.side.value}:{self.price}:{self.size}:{self.strategy_name}"
        return hashlib.sha256(data.encode()).hexdigest()[:16]

    def round_price_to_tick(self) -> "OrderIntent":
        """Round price to valid tick size."""
        rounded_price = round_to_tick(self.price)
        return OrderIntent(
            token_id=self.token_id,
            side=self.side,
            price=rounded_price,
            size=self.size,
            order_type=self.order_type,
            expiration_ts=self.expiration_ts,
            client_order_id=self.client_order_id,
            strategy_name=self.strategy_name,
        )

    def validate(self) -> list[str]:
        """Validate the order intent, return list of errors."""
        errors = []

        if self.price < MIN_PRICE or self.price > MAX_PRICE:
            errors.append(f"Price {self.price} out of bounds [{MIN_PRICE}, {MAX_PRICE}]")

        if self.size <= 0:
            errors.append(f"Size {self.size} must be positive")

        if not self.token_id:
            errors.append("token_id is required")

        # Check tick size
        if self.price % TICK_SIZE != 0:
            errors.append(f"Price {self.price} not aligned to tick size {TICK_SIZE}")

        return errors

    def is_valid(self) -> bool:
        """Check if order intent is valid."""
        return len(self.validate()) == 0

    @property
    def notional(self) -> Decimal:
        """Notional value of the order."""
        return self.price * self.size


def round_to_tick(price: Decimal, tick_size: Decimal = TICK_SIZE) -> Decimal:
    """Round price to nearest tick."""
    return (price / tick_size).quantize(Decimal("1")) * tick_size


def clamp_price(price: Decimal) -> Decimal:
    """Clamp price to valid range."""
    return max(MIN_PRICE, min(MAX_PRICE, price))


def format_price(price: Decimal) -> str:
    """Format price for display."""
    return f"{price:.2f}"
