"""Canonical data models for the trading bot."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Literal, Optional


class OrderSide(str, Enum):
    """Order side."""
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    """Order type."""
    GTC = "GTC"  # Good til cancelled
    GTD = "GTD"  # Good til date
    FOK = "FOK"  # Fill or kill


class TradeMode(str, Enum):
    """Trading mode."""
    PAPER = "PAPER"
    LIVE = "LIVE"


@dataclass
class Token:
    """Outcome token for a prediction market."""
    token_id: str
    market_id: str
    outcome: str  # "Yes" or "No"
    winner: Optional[bool] = None

    def __hash__(self) -> int:
        return hash(self.token_id)


@dataclass
class Market:
    """Prediction market from Gamma API."""
    market_id: str
    event_id: str
    condition_id: str
    title: str
    slug: str
    description: str
    active: bool
    closed: bool
    start_date: Optional[datetime]
    end_date: Optional[datetime]
    tokens: List[Token]
    category: str
    liquidity: Decimal
    volume: Decimal
    last_updated: datetime

    # Additional metadata
    question: Optional[str] = None
    outcomes: Optional[str] = None  # JSON string of outcome labels

    def __hash__(self) -> int:
        return hash(self.market_id)

    @property
    def yes_token(self) -> Optional[Token]:
        """Get the YES outcome token."""
        for token in self.tokens:
            if token.outcome.lower() == "yes":
                return token
        return None

    @property
    def no_token(self) -> Optional[Token]:
        """Get the NO outcome token."""
        for token in self.tokens:
            if token.outcome.lower() == "no":
                return token
        return None


@dataclass
class OrderBookLevel:
    """Single level in an order book."""
    price: Decimal
    size: Decimal


@dataclass
class OrderBook:
    """Order book snapshot for a token."""
    token_id: str
    timestamp: datetime
    bids: List[OrderBookLevel] = field(default_factory=list)
    asks: List[OrderBookLevel] = field(default_factory=list)

    @property
    def best_bid(self) -> Optional[Decimal]:
        """Get best bid price."""
        if self.bids:
            return max(level.price for level in self.bids)
        return None

    @property
    def best_bid_size(self) -> Optional[Decimal]:
        """Get size at best bid."""
        if not self.bids:
            return None
        best_price = self.best_bid
        for level in self.bids:
            if level.price == best_price:
                return level.size
        return None

    @property
    def best_ask(self) -> Optional[Decimal]:
        """Get best ask price."""
        if self.asks:
            return min(level.price for level in self.asks)
        return None

    @property
    def best_ask_size(self) -> Optional[Decimal]:
        """Get size at best ask."""
        if not self.asks:
            return None
        best_price = self.best_ask
        for level in self.asks:
            if level.price == best_price:
                return level.size
        return None

    @property
    def midpoint(self) -> Optional[Decimal]:
        """Get midpoint price."""
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None

    @property
    def spread(self) -> Optional[Decimal]:
        """Get bid-ask spread."""
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None

    @property
    def spread_pct(self) -> Optional[Decimal]:
        """Get spread as percentage of midpoint."""
        mid = self.midpoint
        spread = self.spread
        if mid and spread and mid > 0:
            return spread / mid
        return None


@dataclass
class Position:
    """Position in a token."""
    token_id: str
    shares: Decimal
    avg_cost: Decimal
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    timestamp: datetime = field(default_factory=datetime.utcnow)

    @property
    def cost_basis(self) -> Decimal:
        """Total cost basis of position."""
        return self.shares * self.avg_cost

    @property
    def market_value(self) -> Decimal:
        """Current market value (requires price update)."""
        return self.shares * self.avg_cost + self.unrealized_pnl

    def update_unrealized_pnl(self, current_price: Decimal) -> None:
        """Update unrealized PnL based on current price."""
        if self.shares > 0:
            self.unrealized_pnl = (current_price - self.avg_cost) * self.shares
        else:
            self.unrealized_pnl = Decimal("0")


@dataclass
class OrderIntent:
    """Exchange-agnostic order intent from strategy."""
    token_id: str
    side: OrderSide
    price: Decimal
    size: Decimal
    order_type: OrderType = OrderType.GTC
    expiration_ts: Optional[int] = None
    client_order_id: str = ""
    strategy_name: str = ""

    def __post_init__(self):
        if not self.client_order_id:
            # Generate deterministic client order ID
            import hashlib
            data = f"{self.token_id}:{self.side.value}:{self.price}:{self.size}:{self.strategy_name}"
            self.client_order_id = hashlib.sha256(data.encode()).hexdigest()[:16]


@dataclass
class Order:
    """An order on the exchange."""
    order_id: str
    client_order_id: str
    token_id: str
    side: OrderSide
    price: Decimal
    original_size: Decimal
    remaining_size: Decimal
    status: str  # "open", "filled", "cancelled", "expired"
    created_at: datetime
    updated_at: datetime

    @property
    def filled_size(self) -> Decimal:
        return self.original_size - self.remaining_size

    @property
    def is_open(self) -> bool:
        return self.status == "open"


@dataclass
class Fill:
    """A trade fill."""
    fill_id: str
    order_id: str
    client_order_id: str
    token_id: str
    side: OrderSide
    price: Decimal
    size: Decimal
    fee: Decimal
    timestamp: datetime
    mode: TradeMode

    @property
    def notional(self) -> Decimal:
        """Notional value of fill."""
        return self.price * self.size


@dataclass
class PnLSnapshot:
    """Point-in-time PnL snapshot."""
    timestamp: datetime
    mode: TradeMode
    total_value: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    max_drawdown: Decimal
    positions: List[Position] = field(default_factory=list)

    @property
    def total_pnl(self) -> Decimal:
        return self.realized_pnl + self.unrealized_pnl


@dataclass
class StrategyDecision:
    """Audit log entry for a strategy decision."""
    timestamp: datetime
    strategy_name: str
    inputs_hash: str  # Hash of input state for reproducibility
    intents: List[OrderIntent]
    risk_adjustments: Dict[str, str]  # What risk manager changed
    final_intents: List[OrderIntent]  # After risk clamping


@dataclass
class AccountState:
    """Unified account state from Data API."""
    address: str
    balance: Decimal  # Available balance
    positions: Dict[str, Position]  # token_id -> Position
    open_orders: List[Order]
    timestamp: datetime

    @property
    def total_exposure(self) -> Decimal:
        """Total exposure across all positions."""
        return sum(pos.cost_basis for pos in self.positions.values())

    @property
    def total_open_notional(self) -> Decimal:
        """Total notional of open orders."""
        return sum(o.remaining_size * o.price for o in self.open_orders)
