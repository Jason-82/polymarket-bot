"""Geoblock checking for Polymarket compliance."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional
import httpx

from monitoring.logger import get_logger

logger = get_logger(__name__)


class GeoblockStatus(str, Enum):
    """Geoblock check result status."""
    ALLOWED = "ALLOWED"      # Trading permitted
    BLOCKED = "BLOCKED"      # Trading not permitted (geoblocked)
    ERROR = "ERROR"          # Could not determine (treat as blocked)
    UNKNOWN = "UNKNOWN"      # Not yet checked


@dataclass
class GeoblockResult:
    """Result of a geoblock check."""
    status: GeoblockStatus
    blocked: bool
    country: Optional[str] = None
    message: Optional[str] = None
    checked_at: Optional[datetime] = None
    error: Optional[str] = None


class GeoblockChecker:
    """
    Check Polymarket geoblock status.

    IMPORTANT: This bot respects geoblock restrictions.
    If blocked, only READ_ONLY and PAPER modes are available.
    """

    DEFAULT_URL = "https://polymarket.com/api/geoblock"
    CHECK_INTERVAL = timedelta(hours=6)  # Recheck every 6 hours

    def __init__(
        self,
        url: str = DEFAULT_URL,
        timeout: float = 10.0,
    ):
        self.url = url
        self.timeout = timeout
        self._last_result: Optional[GeoblockResult] = None
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "GeoblockChecker":
        """Async context manager entry."""
        self._client = httpx.AsyncClient(timeout=self.timeout)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def check(self, force: bool = False) -> GeoblockResult:
        """
        Check geoblock status.

        Args:
            force: Force a fresh check even if cached result is available

        Returns:
            GeoblockResult with status and details
        """
        # Return cached result if still valid
        if not force and self._is_cache_valid():
            return self._last_result

        try:
            result = await self._perform_check()
            self._last_result = result
            return result

        except Exception as e:
            logger.error("geoblock_check_failed", error=str(e))
            result = GeoblockResult(
                status=GeoblockStatus.ERROR,
                blocked=True,  # Fail safe - treat errors as blocked
                message="Could not determine geoblock status",
                error=str(e),
                checked_at=datetime.utcnow(),
            )
            self._last_result = result
            return result

    async def _perform_check(self) -> GeoblockResult:
        """Perform the actual geoblock check."""
        client = self._client or httpx.AsyncClient(timeout=self.timeout)
        should_close = self._client is None

        try:
            logger.debug("geoblock_checking", url=self.url)

            response = await client.get(self.url)
            response.raise_for_status()

            data = response.json()

            # Parse response - expected format: {"blocked": true/false, ...}
            blocked = data.get("blocked", True)  # Default to blocked if missing
            country = data.get("country")

            if blocked:
                logger.warning(
                    "geoblock_blocked",
                    country=country,
                    message="Trading disabled due to geographic restrictions"
                )
                return GeoblockResult(
                    status=GeoblockStatus.BLOCKED,
                    blocked=True,
                    country=country,
                    message="Trading is not available in your region",
                    checked_at=datetime.utcnow(),
                )
            else:
                logger.info("geoblock_allowed", country=country)
                return GeoblockResult(
                    status=GeoblockStatus.ALLOWED,
                    blocked=False,
                    country=country,
                    message="Trading is allowed",
                    checked_at=datetime.utcnow(),
                )

        finally:
            if should_close:
                await client.aclose()

    def _is_cache_valid(self) -> bool:
        """Check if cached result is still valid."""
        if self._last_result is None:
            return False
        if self._last_result.checked_at is None:
            return False

        age = datetime.utcnow() - self._last_result.checked_at
        return age < self.CHECK_INTERVAL

    def get_cached_result(self) -> Optional[GeoblockResult]:
        """Get the last cached result without making a new request."""
        return self._last_result

    def is_trading_allowed(self) -> bool:
        """
        Quick check if trading is allowed based on cached result.

        Returns False if:
        - No check has been performed
        - Last check was blocked
        - Last check was an error
        """
        if self._last_result is None:
            return False
        return self._last_result.status == GeoblockStatus.ALLOWED

    @staticmethod
    def get_status_message(result: GeoblockResult) -> str:
        """Get a human-readable status message."""
        if result.status == GeoblockStatus.ALLOWED:
            return f"Trading allowed (country: {result.country or 'unknown'})"
        elif result.status == GeoblockStatus.BLOCKED:
            return f"Trading BLOCKED (country: {result.country or 'unknown'}). Only READ_ONLY and PAPER modes available."
        elif result.status == GeoblockStatus.ERROR:
            return f"Geoblock check ERROR: {result.error}. Treating as blocked for safety."
        else:
            return "Geoblock status unknown. Run check() first."


async def check_geoblock(url: str = GeoblockChecker.DEFAULT_URL) -> GeoblockResult:
    """Convenience function to perform a one-off geoblock check."""
    async with GeoblockChecker(url=url) as checker:
        return await checker.check()
