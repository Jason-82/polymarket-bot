#!/usr/bin/env python3
"""Script to check Polymarket geoblock status."""

import asyncio
import sys

sys.path.insert(0, ".")

from connectors.geoblock import GeoblockChecker


async def main():
    """Check and display geoblock status."""
    print("Checking Polymarket geoblock status...")
    print()

    async with GeoblockChecker() as checker:
        result = await checker.check()

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


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
