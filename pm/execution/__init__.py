"""Exchange interface shared by read-only, paper and live backends."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional, Protocol

from ..models import Fill, Intent, Order


class ExchangeError(Exception):
    pass


class Exchange(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def place(self, intent: Intent) -> Order: ...
    async def cancel(self, order_id: str) -> None: ...
    async def cancel_all(self) -> None: ...
    async def open_orders(self) -> list[Order]: ...
    async def drain_fills(self) -> list[Fill]: ...
    async def balance(self) -> Optional[Decimal]: ...
    async def positions(self) -> Optional[dict[str, tuple[Decimal, Decimal]]]: ...
    """{token_id: (shares, cost)} as the venue sees them, or None if the backend cannot say."""


class Recorder:
    """READ_ONLY backend: accepts nothing, records nothing, never fills."""

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def place(self, intent: Intent) -> Order:
        raise ExchangeError("read_only mode does not place orders")

    async def cancel(self, order_id: str) -> None:
        return None

    async def cancel_all(self) -> None:
        return None

    async def open_orders(self) -> list[Order]:
        return []

    async def drain_fills(self) -> list[Fill]:
        return []

    async def balance(self) -> Optional[Decimal]:
        return None

    async def positions(self) -> Optional[dict[str, tuple[Decimal, Decimal]]]:
        return None
