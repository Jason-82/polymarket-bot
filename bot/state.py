"""In-memory state store for the trading bot."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Set

from storage.models import Market, Token, OrderBook, Position, Order


@dataclass
class BotState:
    """
    Central state store for the trading bot.

    Holds in-memory state that is updated from various sources:
    - Orderbooks from WebSocket/REST
    - Positions from Data API
    - Orders from OMS
    - Markets from Gamma API
    """

    # Market universe
    markets: Dict[str, Market] = field(default_factory=dict)  # market_id -> Market
    tokens: Dict[str, Token] = field(default_factory=dict)    # token_id -> Token
    token_to_market: Dict[str, str] = field(default_factory=dict)  # token_id -> market_id

    # Active token IDs we're tracking
    active_token_ids: Set[str] = field(default_factory=set)

    # Real-time data
    orderbooks: Dict[str, OrderBook] = field(default_factory=dict)  # token_id -> OrderBook

    # Account state
    positions: Dict[str, Position] = field(default_factory=dict)  # token_id -> Position
    open_orders: List[Order] = field(default_factory=list)
    balance: Decimal = Decimal("0")

    # Timestamps
    last_market_refresh: Optional[datetime] = None
    last_position_refresh: Optional[datetime] = None
    last_geoblock_check: Optional[datetime] = None

    # Geoblock status
    geoblock_allowed: bool = False

    def update_markets(self, markets: List[Market]) -> None:
        """Update market universe."""
        for market in markets:
            self.markets[market.market_id] = market
            for token in market.tokens:
                self.tokens[token.token_id] = token
                self.token_to_market[token.token_id] = market.market_id
        self.last_market_refresh = datetime.utcnow()

    def update_orderbook(self, token_id: str, orderbook: OrderBook) -> None:
        """Update orderbook for a token."""
        self.orderbooks[token_id] = orderbook

    def update_position(self, position: Position) -> None:
        """Update position for a token."""
        self.positions[position.token_id] = position

    def update_positions(self, positions: List[Position]) -> None:
        """Update multiple positions."""
        for pos in positions:
            self.update_position(pos)
        self.last_position_refresh = datetime.utcnow()

    def set_active_tokens(self, token_ids: List[str]) -> None:
        """Set the active token IDs to track."""
        self.active_token_ids = set(token_ids)

    def get_market_for_token(self, token_id: str) -> Optional[Market]:
        """Get market for a token."""
        market_id = self.token_to_market.get(token_id)
        return self.markets.get(market_id) if market_id else None

    def get_orderbook(self, token_id: str) -> Optional[OrderBook]:
        """Get orderbook for a token."""
        return self.orderbooks.get(token_id)

    def get_position(self, token_id: str) -> Optional[Position]:
        """Get position for a token."""
        return self.positions.get(token_id)

    def get_open_orders_for_token(self, token_id: str) -> List[Order]:
        """Get open orders for a token."""
        return [o for o in self.open_orders if o.token_id == token_id]

    def get_total_exposure(self) -> Decimal:
        """Get total exposure across all positions."""
        return sum(pos.cost_basis for pos in self.positions.values())

    def get_total_open_notional(self) -> Decimal:
        """Get total notional of open orders."""
        return sum(o.remaining_size * o.price for o in self.open_orders)

    def clear_orders(self) -> None:
        """Clear all open orders (after cancel all)."""
        self.open_orders = []

    def get_summary(self) -> Dict:
        """Get state summary for monitoring."""
        return {
            "markets_count": len(self.markets),
            "tokens_count": len(self.tokens),
            "active_tokens": len(self.active_token_ids),
            "orderbooks_count": len(self.orderbooks),
            "positions_count": len(self.positions),
            "open_orders_count": len(self.open_orders),
            "balance": str(self.balance),
            "total_exposure": str(self.get_total_exposure()),
            "geoblock_allowed": self.geoblock_allowed,
            "last_market_refresh": self.last_market_refresh.isoformat() if self.last_market_refresh else None,
        }
