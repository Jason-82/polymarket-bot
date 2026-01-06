"""Historical data loading for backtesting."""

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

from connectors.clob_rest_client import ClobRestClient
from storage.database import Database
from storage.dao import MarketDAO, OrderBookDAO
from storage.models import Market
from monitoring.logger import get_logger

logger = get_logger(__name__)


class HistoricalDataLoader:
    """
    Load and manage historical data for backtesting.

    Supports:
    - Fetching price history from CLOB API
    - Loading from local database
    - Building datasets for specific tokens/periods
    """

    def __init__(self, database_path: str = "data/polymarket.db"):
        self.db = Database(database_path)
        self.db.initialize()
        self.market_dao = MarketDAO(self.db)
        self.orderbook_dao = OrderBookDAO(self.db)

    async def fetch_price_history(
        self,
        token_id: str,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        interval: str = "1h",
    ) -> List[Dict[str, Any]]:
        """
        Fetch price history from CLOB API.

        Args:
            token_id: Token to fetch history for
            start_ts: Start timestamp (seconds)
            end_ts: End timestamp (seconds)
            interval: Candle interval (1m, 5m, 15m, 1h, 4h, 1d)

        Returns:
            List of price history entries
        """
        async with ClobRestClient() as clob:
            history = await clob.get_prices_history(
                token_id=token_id,
                start_ts=start_ts,
                end_ts=end_ts,
                interval=interval,
            )
        return history

    async def backfill_price_history(
        self,
        token_ids: List[str],
        days: int = 30,
        interval: str = "1h",
    ) -> int:
        """
        Backfill price history to database.

        Args:
            token_ids: Tokens to backfill
            days: Number of days to backfill
            interval: Candle interval

        Returns:
            Number of records saved
        """
        end_ts = int(datetime.utcnow().timestamp())
        start_ts = end_ts - (days * 24 * 60 * 60)

        total_saved = 0

        for token_id in token_ids:
            try:
                logger.info(
                    "backfilling_price_history",
                    token_id=token_id[:16],
                    days=days,
                )

                history = await self.fetch_price_history(
                    token_id=token_id,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    interval=interval,
                )

                for entry in history:
                    self._save_price_entry(token_id, entry)
                    total_saved += 1

                # Rate limit
                await asyncio.sleep(0.5)

            except Exception as e:
                logger.warning(
                    "backfill_failed",
                    token_id=token_id[:16],
                    error=str(e),
                )

        logger.info("backfill_complete", total_saved=total_saved)
        return total_saved

    def _save_price_entry(
        self,
        token_id: str,
        entry: Dict[str, Any],
    ) -> None:
        """Save a price history entry to database."""
        data = {
            "token_id": token_id,
            "timestamp": datetime.fromtimestamp(entry.get("t", 0)).isoformat(),
            "open": entry.get("o"),
            "high": entry.get("h"),
            "low": entry.get("l"),
            "close": entry.get("c"),
            "volume": entry.get("v"),
        }

        try:
            self.db.upsert(
                "price_history",
                data,
                conflict_columns=["token_id", "timestamp"],
            )
        except Exception:
            # Ignore duplicate errors
            pass

    def get_price_history(
        self,
        token_id: str,
        start: datetime,
        end: datetime,
    ) -> List[Dict[str, Any]]:
        """
        Get price history from database.

        Args:
            token_id: Token ID
            start: Start datetime
            end: End datetime

        Returns:
            List of price entries
        """
        return self.db.query(
            """SELECT * FROM price_history
               WHERE token_id = ? AND timestamp >= ? AND timestamp <= ?
               ORDER BY timestamp ASC""",
            (token_id, start.isoformat(), end.isoformat()),
        )

    def get_available_tokens(self) -> List[str]:
        """Get list of tokens with price history data."""
        rows = self.db.query(
            "SELECT DISTINCT token_id FROM price_history"
        )
        return [row["token_id"] for row in rows]

    def get_data_range(self, token_id: str) -> Optional[tuple[datetime, datetime]]:
        """Get date range of available data for a token."""
        row = self.db.query_one(
            """SELECT MIN(timestamp) as min_ts, MAX(timestamp) as max_ts
               FROM price_history WHERE token_id = ?""",
            (token_id,),
        )
        if row and row["min_ts"] and row["max_ts"]:
            return (
                datetime.fromisoformat(row["min_ts"]),
                datetime.fromisoformat(row["max_ts"]),
            )
        return None
