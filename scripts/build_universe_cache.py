#!/usr/bin/env python3
"""Script to build and cache the market universe."""

import asyncio
import sys

sys.path.insert(0, ".")

from connectors.gamma_client import GammaClient
from storage.database import Database
from storage.dao import MarketDAO


async def main():
    """Fetch and cache all active markets."""
    print("Building market universe cache...")
    print()

    # Initialize database
    db = Database("data/polymarket.db")
    db.initialize()
    market_dao = MarketDAO(db)

    # Fetch markets
    async with GammaClient() as gamma:
        print("Fetching markets from Gamma API...")
        markets = await gamma.get_all_active_markets()

    print(f"Found {len(markets)} active markets")
    print()

    # Save to database
    print("Saving to database...")
    market_dao.save_markets(markets)

    # Show summary
    print()
    print("Summary by category:")
    categories = {}
    for market in markets:
        cat = market.category or "unknown"
        categories[cat] = categories.get(cat, 0) + 1

    for cat, count in sorted(categories.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {count}")

    print()
    print(f"Database: data/polymarket.db")
    print(f"Total markets cached: {len(markets)}")

    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
