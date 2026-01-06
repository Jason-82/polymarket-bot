#!/usr/bin/env python3
"""Script to backfill price history for backtesting."""

import argparse
import asyncio
import sys

sys.path.insert(0, ".")

from storage.database import Database
from storage.dao import MarketDAO
from backtest.data_loader import HistoricalDataLoader


async def main():
    """Backfill price history for selected tokens."""
    parser = argparse.ArgumentParser(
        description="Backfill price history for backtesting"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Number of days to backfill (default: 30)",
    )
    parser.add_argument(
        "--interval",
        type=str,
        default="1h",
        choices=["1m", "5m", "15m", "1h", "4h", "1d"],
        help="Candle interval (default: 1h)",
    )
    parser.add_argument(
        "--token",
        type=str,
        action="append",
        help="Specific token ID to backfill (can specify multiple)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Backfill all tokens in database",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Max tokens to backfill when using --all (default: 10)",
    )

    args = parser.parse_args()

    print("Price History Backfill")
    print("=" * 40)
    print()

    # Get token IDs
    if args.token:
        token_ids = args.token
    elif args.all:
        db = Database("data/polymarket.db")
        db.initialize()
        market_dao = MarketDAO(db)
        token_ids = market_dao.get_all_token_ids()[:args.limit]
        print(f"Found {len(token_ids)} tokens in database")
    else:
        print("Error: Specify --token <id> or --all")
        return 1

    if not token_ids:
        print("No tokens to backfill")
        return 1

    print(f"Backfilling {len(token_ids)} tokens...")
    print(f"Days: {args.days}")
    print(f"Interval: {args.interval}")
    print()

    loader = HistoricalDataLoader()
    saved = await loader.backfill_price_history(
        token_ids=token_ids,
        days=args.days,
        interval=args.interval,
    )

    print()
    print(f"Saved {saved} price history entries")

    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
