"""Market universe selection and filtering."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set

from storage.models import Market, Token


@dataclass
class UniverseFilters:
    """Filters for market universe selection."""

    # Explicit allowlists (if set, only these are included)
    market_ids: Optional[Set[str]] = None
    token_ids: Optional[Set[str]] = None

    # Category filter
    categories: Optional[Set[str]] = None
    exclude_categories: Optional[Set[str]] = None

    # Liquidity/volume filters
    min_liquidity: Decimal = Decimal("0")
    min_volume: Decimal = Decimal("0")

    # Time filters
    min_days_to_expiry: int = 0
    max_days_to_expiry: Optional[int] = None

    # Status filters
    active_only: bool = True
    exclude_closed: bool = True

    # Limit
    max_markets: int = 100

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UniverseFilters":
        """Create filters from dictionary."""
        return cls(
            market_ids=set(data["market_ids"]) if data.get("market_ids") else None,
            token_ids=set(data["token_ids"]) if data.get("token_ids") else None,
            categories=set(data["categories"]) if data.get("categories") else None,
            exclude_categories=set(data["exclude_categories"]) if data.get("exclude_categories") else None,
            min_liquidity=Decimal(str(data.get("min_liquidity", 0))),
            min_volume=Decimal(str(data.get("min_volume", 0))),
            min_days_to_expiry=data.get("min_days_to_expiry", 0),
            max_days_to_expiry=data.get("max_days_to_expiry"),
            active_only=data.get("active_only", True),
            exclude_closed=data.get("exclude_closed", True),
            max_markets=data.get("max_markets", 100),
        )


class UniverseSelector:
    """
    Selects which markets and tokens to track/trade.

    Supports:
    - Explicit allowlists
    - Category filtering
    - Liquidity/volume thresholds
    - Time-to-expiry filtering
    """

    def __init__(self, filters: Optional[UniverseFilters] = None):
        self.filters = filters or UniverseFilters()

    def select_markets(self, all_markets: List[Market]) -> List[Market]:
        """
        Select markets based on configured filters.

        Args:
            all_markets: List of all available markets

        Returns:
            Filtered list of markets
        """
        selected = []

        for market in all_markets:
            if self._passes_filters(market):
                selected.append(market)

        # Sort by liquidity (highest first)
        selected.sort(key=lambda m: m.liquidity, reverse=True)

        # Apply limit
        return selected[:self.filters.max_markets]

    def select_tokens(self, markets: List[Market]) -> List[str]:
        """
        Get token IDs for selected markets.

        Args:
            markets: List of selected markets

        Returns:
            List of token IDs
        """
        token_ids = []

        for market in markets:
            for token in market.tokens:
                # Check explicit token allowlist
                if self.filters.token_ids is not None:
                    if token.token_id in self.filters.token_ids:
                        token_ids.append(token.token_id)
                else:
                    token_ids.append(token.token_id)

        return token_ids

    def _passes_filters(self, market: Market) -> bool:
        """Check if a market passes all filters."""
        f = self.filters

        # Explicit market allowlist
        if f.market_ids is not None:
            if market.market_id not in f.market_ids:
                return False

        # Status filters
        if f.active_only and not market.active:
            return False
        if f.exclude_closed and market.closed:
            return False

        # Category filters
        if f.categories is not None:
            if market.category not in f.categories:
                return False
        if f.exclude_categories is not None:
            if market.category in f.exclude_categories:
                return False

        # Liquidity/volume filters
        if market.liquidity < f.min_liquidity:
            return False
        if market.volume < f.min_volume:
            return False

        # Time-to-expiry filters
        if market.end_date:
            # Handle timezone-aware vs naive datetime comparison
            end_date = market.end_date
            now = datetime.utcnow()
            # Strip timezone if present to make comparison work
            if end_date.tzinfo is not None:
                end_date = end_date.replace(tzinfo=None)
            days_to_expiry = (end_date - now).days
            if days_to_expiry < f.min_days_to_expiry:
                return False
            if f.max_days_to_expiry is not None:
                if days_to_expiry > f.max_days_to_expiry:
                    return False

        return True

    def update_filters(self, filters: UniverseFilters) -> None:
        """Update filter configuration."""
        self.filters = filters
