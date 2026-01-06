"""CLI entry point for the Polymarket trading bot."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Optional

from bot.config import BotMode, Config
from bot.engine import TradingEngine
from monitoring.logger import setup_logging, get_logger


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Polymarket Trading Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run in READ_ONLY mode (default - safe, no trading)
  python -m bot.main

  # Run in PAPER mode (simulated trading)
  python -m bot.main --mode PAPER

  # Run with specific config file
  python -m bot.main --config config/config.yaml

  # Check geoblock status
  python -m bot.main --check-geoblock

  # Show current status
  python -m bot.main --status
        """,
    )

    parser.add_argument(
        "--mode",
        type=str,
        choices=["READ_ONLY", "PAPER", "LIVE"],
        default="READ_ONLY",
        help="Bot operating mode (default: READ_ONLY)",
    )

    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to YAML configuration file",
    )

    parser.add_argument(
        "--database",
        type=str,
        default="data/polymarket.db",
        help="Path to SQLite database",
    )

    parser.add_argument(
        "--log-level",
        type=str,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Log level",
    )

    parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        help="Path to log file (default: stdout only)",
    )

    parser.add_argument(
        "--check-geoblock",
        action="store_true",
        help="Check geoblock status and exit",
    )

    parser.add_argument(
        "--status",
        action="store_true",
        help="Show current bot status and exit",
    )

    parser.add_argument(
        "--list-markets",
        action="store_true",
        help="List available markets and exit",
    )

    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run demo (fetch markets, subscribe to WS for 30s)",
    )

    return parser.parse_args()


async def check_geoblock() -> int:
    """Check and display geoblock status."""
    from connectors.geoblock import GeoblockChecker

    print("Checking Polymarket geoblock status...")

    async with GeoblockChecker() as checker:
        result = await checker.check()

    print()
    print(f"Status: {result.status.value}")
    print(f"Blocked: {result.blocked}")
    if result.country:
        print(f"Country: {result.country}")
    if result.message:
        print(f"Message: {result.message}")
    if result.error:
        print(f"Error: {result.error}")
    print()

    if result.blocked:
        print("WARNING: Trading is NOT available in your region.")
        print("The bot will only run in READ_ONLY or PAPER mode.")
        return 1
    else:
        print("Trading is available. You can run in LIVE mode.")
        return 0


async def list_markets(limit: int = 20) -> int:
    """List available markets from Gamma API."""
    from connectors.gamma_client import GammaClient

    print(f"Fetching markets from Gamma API (limit: {limit})...")
    print()

    async with GammaClient() as client:
        markets = await client.get_markets(limit=limit)

    if not markets:
        print("No markets found.")
        return 1

    print(f"Found {len(markets)} markets:\n")
    print("-" * 80)

    for market in markets:
        print(f"ID: {market.market_id}")
        print(f"Title: {market.title}")
        print(f"Category: {market.category}")
        print(f"Liquidity: ${market.liquidity:.2f}")
        print(f"Volume: ${market.volume:.2f}")
        print(f"Active: {market.active}, Closed: {market.closed}")
        print(f"Tokens: {len(market.tokens)}")
        for token in market.tokens:
            print(f"  - {token.outcome}: {token.token_id[:16]}...")
        print("-" * 80)

    return 0


async def run_demo() -> int:
    """Run a demo showing market discovery and WebSocket subscription."""
    from connectors.geoblock import GeoblockChecker
    from connectors.gamma_client import GammaClient
    from connectors.clob_rest_client import ClobRestClient
    from connectors.clob_ws_client import ClobWebSocketClient
    from storage.database import Database
    from storage.dao import MarketDAO, OrderBookDAO
    from storage.models import OrderBook

    print("=" * 60)
    print("POLYMARKET TRADING BOT - DEMO")
    print("=" * 60)
    print()

    # 1. Check geoblock
    print("1. Checking geoblock status...")
    async with GeoblockChecker() as checker:
        geoblock = await checker.check()
    print(f"   Status: {geoblock.status.value}")
    print(f"   Trading allowed: {not geoblock.blocked}")
    print()

    # 2. Initialize database
    print("2. Initializing database...")
    db = Database("data/demo.db")
    db.initialize()
    market_dao = MarketDAO(db)
    orderbook_dao = OrderBookDAO(db)
    print("   Database ready.")
    print()

    # 3. Fetch markets
    print("3. Fetching markets from Gamma API...")
    async with GammaClient() as gamma:
        markets = await gamma.get_markets(limit=5)

    print(f"   Found {len(markets)} markets")
    for m in markets:
        print(f"   - {m.title[:50]}...")
    print()

    # 4. Save to database
    print("4. Saving markets to database...")
    market_dao.save_markets(markets)
    print(f"   Saved {len(markets)} markets")
    print()

    # 5. Get token IDs
    token_ids = []
    for market in markets[:3]:  # First 3 markets
        for token in market.tokens:
            token_ids.append(token.token_id)

    print(f"5. Selected {len(token_ids)} tokens to track")
    print()

    # 6. Fetch initial orderbooks via REST
    print("6. Fetching orderbooks via REST...")
    async with ClobRestClient() as clob:
        for token_id in token_ids[:3]:  # First 3 tokens
            try:
                orderbook = await clob.get_orderbook(token_id)
                print(f"   Token: {token_id[:16]}...")
                if orderbook.midpoint:
                    print(f"   Midpoint: {orderbook.midpoint:.4f}")
                    print(f"   Spread: {orderbook.spread:.4f}")
                else:
                    print("   No quotes available")
                orderbook_dao.save_snapshot(orderbook)
            except Exception as e:
                print(f"   Error: {e}")
    print()

    # 7. Subscribe to WebSocket
    print("7. Subscribing to WebSocket for 30 seconds...")
    print("   (Press Ctrl+C to stop early)")
    print()

    updates = {"count": 0}

    def on_update(token_id: str, orderbook: OrderBook):
        updates["count"] += 1
        if orderbook.midpoint:
            print(
                f"   [{updates['count']}] {token_id[:12]}... "
                f"mid={orderbook.midpoint:.4f} "
                f"spread={orderbook.spread:.4f if orderbook.spread else 'N/A'}"
            )
            orderbook_dao.save_snapshot(orderbook)

    try:
        ws = ClobWebSocketClient(on_orderbook_update=on_update)
        await ws.connect()
        await ws.subscribe_markets(token_ids[:3])

        await asyncio.sleep(30)

        await ws.disconnect()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"   WebSocket error: {e}")

    print()
    print(f"   Received {updates['count']} orderbook updates")
    print()

    # 8. Show database stats
    print("8. Database stats:")
    stats = db.get_stats()
    for table, count in stats.items():
        print(f"   {table}: {count} rows")
    print()

    print("=" * 60)
    print("DEMO COMPLETE")
    print("=" * 60)

    return 0


async def show_status(config: Config) -> int:
    """Show current bot status from database."""
    from storage.database import Database
    from storage.dao import MarketDAO, OrderBookDAO

    db = Database(config.database_path)

    print("=" * 60)
    print("POLYMARKET BOT STATUS")
    print("=" * 60)
    print()

    print(f"Mode: {config.mode.value}")
    print(f"Database: {config.database_path}")
    print()

    try:
        db.initialize()
        stats = db.get_stats()

        print("Database Statistics:")
        for table, count in stats.items():
            print(f"  {table}: {count} rows")

        print()

        # Show recent market activity
        market_dao = MarketDAO(db)
        markets = market_dao.get_active_markets()
        print(f"Active markets tracked: {len(markets)}")

        if markets:
            print("\nTop markets by liquidity:")
            sorted_markets = sorted(markets, key=lambda m: m.liquidity, reverse=True)
            for m in sorted_markets[:5]:
                print(f"  - {m.title[:40]}... (${m.liquidity:,.0f})")

    except Exception as e:
        print(f"Error reading database: {e}")
        return 1

    print()
    return 0


async def run_bot(config: Config) -> int:
    """Run the trading bot."""
    logger = get_logger(__name__)
    logger.info("Starting Polymarket Trading Bot", mode=config.mode.value)

    engine = TradingEngine(config)

    try:
        await engine.start()
    except KeyboardInterrupt:
        logger.info("Received shutdown signal")
    except Exception as e:
        logger.exception("Engine error", error=str(e))
        return 1
    finally:
        await engine.stop()

    return 0


def main() -> int:
    """Main entry point."""
    args = parse_args()

    # Setup logging
    setup_logging(
        level=args.log_level,
        log_file=args.log_file,
        json_format=False,  # Use human-readable for CLI
    )

    # Load config
    if args.config:
        config = Config.from_yaml(Path(args.config))
    else:
        config = Config.from_env()

    # Apply CLI overrides
    config.mode = BotMode(args.mode)
    config.database_path = args.database
    config.log_level = args.log_level

    # Validate config
    errors = config.validate()
    if errors:
        print("Configuration errors:")
        for error in errors:
            print(f"  - {error}")
        return 1

    # Handle special commands
    if args.check_geoblock:
        return asyncio.run(check_geoblock())

    if args.list_markets:
        return asyncio.run(list_markets())

    if args.demo:
        return asyncio.run(run_demo())

    if args.status:
        return asyncio.run(show_status(config))

    # Run the bot
    return asyncio.run(run_bot(config))


if __name__ == "__main__":
    sys.exit(main())
