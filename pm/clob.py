"""CLOB REST, read-only and unauthenticated: books, prices, fee/tick/neg-risk info, rewards."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

import httpx

from .log import get_logger
from .models import D, Book, Level, Market

log = get_logger(__name__)


def book_from_json(token_id: str, data: dict[str, Any]) -> Book:
    b = Book(token_id=token_id, hash=str(data.get("hash") or ""))
    b.replace(
        bids=[Level(D(l["price"]), D(l["size"])) for l in data.get("bids") or []],
        asks=[Level(D(l["price"]), D(l["size"])) for l in data.get("asks") or []],
        hash=b.hash,
    )
    return b


class Clob:
    def __init__(self, base_url: str = "https://clob.polymarket.com", timeout: float = 15.0):
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout, headers={"User-Agent": "pm/2"})

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, **params: Any) -> Any:
        r = await self._client.get(path, params=params or None)
        r.raise_for_status()
        return r.json()

    async def _post(self, path: str, body: Any) -> Any:
        r = await self._client.post(path, json=body)
        r.raise_for_status()
        return r.json()

    async def ok(self) -> bool:
        try:
            r = await self._client.get("/ok")
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def book(self, token_id: str) -> Book:
        return book_from_json(token_id, await self._get("/book", token_id=token_id))

    async def books(self, token_ids: list[str]) -> dict[str, Book]:
        out: dict[str, Book] = {}
        for i in range(0, len(token_ids), 50):
            chunk = token_ids[i:i + 50]
            rows = await self._post("/books", [{"token_id": t} for t in chunk])
            for row in rows or []:
                tid = str(row.get("asset_id") or "")
                if tid:
                    out[tid] = book_from_json(tid, row)
        return out

    async def midpoint(self, token_id: str) -> Optional[Decimal]:
        data = await self._get("/midpoint", token_id=token_id)
        return D(data.get("mid")) if data and data.get("mid") is not None else None

    async def enrich_market(self, market: Market) -> Market:
        """Fill tick size, neg-risk and taker fee info from /clob-markets/{condition_id}."""
        try:
            info = await self._get(f"/clob-markets/{market.condition_id}")
        except httpx.HTTPError as e:
            log.warning("clob_market_info_failed", condition_id=market.condition_id, error=str(e))
            return market
        if not info:
            return market
        if info.get("mts") is not None:
            market.tick_size = D(info["mts"])
        if "nr" in info:
            market.neg_risk = bool(info["nr"])
        fd = info.get("fd") or {}
        if "r" in fd:
            market.fee_rate = D(fd.get("r") or 0)
            market.fee_exponent = D(fd.get("e") or 1) or Decimal("1")
            market.fee_known = True
        return market

    async def rewards_markets(self) -> list[dict[str, Any]]:
        """Markets currently in the liquidity-rewards program (best effort)."""
        try:
            data = await self._get("/rewards/markets/current")
        except httpx.HTTPError as e:
            log.warning("rewards_fetch_failed", error=str(e))
            return []
        if isinstance(data, dict):
            return data.get("data") or []
        return data or []


class Geoblock:
    def __init__(self, url: str = "https://polymarket.com/api/geoblock", timeout: float = 10.0):
        self._url = url
        self._timeout = timeout

    async def check(self) -> tuple[bool, str]:
        """Returns (allowed, country)."""
        async with httpx.AsyncClient(timeout=self._timeout, headers={"User-Agent": "pm/2"}) as c:
            r = await c.get(self._url)
            r.raise_for_status()
            data = r.json()
        return (not bool(data.get("blocked", False)), str(data.get("country") or "?"))
