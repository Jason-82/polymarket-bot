"""Gamma API: market discovery. Read-only, unauthenticated."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from .log import get_logger
from .models import D, Market, Token

log = get_logger(__name__)


def _parse_json_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    try:
        out = json.loads(v)
        return out if isinstance(out, list) else []
    except (TypeError, ValueError):
        return []


def _parse_dt(v: Any) -> Optional[datetime]:
    if not v:
        return None
    try:
        s = str(v).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def market_from_gamma(m: dict[str, Any]) -> Optional[Market]:
    token_ids = _parse_json_list(m.get("clobTokenIds"))
    outcomes = _parse_json_list(m.get("outcomes"))
    if not token_ids or len(token_ids) != len(outcomes):
        return None
    cid = m.get("conditionId") or m.get("condition_id")
    if not cid:
        return None
    events = m.get("events") or []
    ev = events[0] if events else {}
    tags = []
    for t in (m.get("tags") or []) + (ev.get("tags") or []):
        label = t.get("label") if isinstance(t, dict) else t
        if label:
            tags.append(str(label).lower())
    return Market(
        condition_id=cid,
        question=m.get("question") or "",
        slug=m.get("slug") or "",
        tokens=[Token(str(tid), str(o)) for tid, o in zip(token_ids, outcomes)],
        neg_risk=bool(m.get("negRisk", False)),
        neg_risk_augmented=bool(m.get("negRiskAugmented", False)),
        event_id=str(ev.get("id") or m.get("eventId") or ""),
        event_slug=str(ev.get("slug") or ""),
        tick_size=D(m.get("orderPriceMinTickSize") or "0.01"),
        min_order_size=D(m.get("orderMinSize") or "5"),
        end_date=_parse_dt(m.get("endDate") or m.get("endDateIso")),
        liquidity_usd=D(m.get("liquidityNum") or m.get("liquidity") or 0),
        volume_24h_usd=D(m.get("volume24hr") or 0),
        tags=sorted(set(tags)),
        accepting_orders=bool(m.get("acceptingOrders", True)),
    )


class Gamma:
    def __init__(self, base_url: str = "https://gamma-api.polymarket.com", timeout: float = 20.0):
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout, headers={"User-Agent": "pm/2"})

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, **params: Any) -> Any:
        r = await self._client.get(path, params=params)
        r.raise_for_status()
        return r.json()

    async def active_markets(self, limit: int = 500, order: str = "volume24hr") -> list[Market]:
        """All active, open markets ordered by 24h volume (paged)."""
        out: list[Market] = []
        offset = 0
        page = 100
        while len(out) < limit:
            rows = await self._get(
                "/markets",
                active="true", closed="false", archived="false",
                order=order, ascending="false", limit=page, offset=offset,
            )
            if not rows:
                break
            for row in rows:
                mk = market_from_gamma(row)
                if mk and mk.accepting_orders:
                    out.append(mk)
            if len(rows) < page:
                break
            offset += page
        return out[:limit]

    async def markets_by_condition(self, condition_ids: list[str]) -> list[Market]:
        out = []
        for cid in condition_ids:
            rows = await self._get("/markets", condition_ids=cid)
            for row in rows or []:
                mk = market_from_gamma(row)
                if mk:
                    out.append(mk)
        return out

    async def event_markets(self, event_id: str) -> list[Market]:
        ev = await self._get(f"/events/{event_id}")
        out = []
        for row in (ev or {}).get("markets") or []:
            row = dict(row)
            row.setdefault("events", [{"id": ev.get("id"), "slug": ev.get("slug"), "tags": ev.get("tags")}])
            mk = market_from_gamma(row)
            if mk:
                out.append(mk)
        return out
