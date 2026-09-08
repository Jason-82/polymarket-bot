"""Gamma API: market discovery. Read-only, unauthenticated.

Discovery goes through /events rather than /markets: events carry the tags,
the neg-risk flags and the complete list of markets in a group, none of
which the flat /markets rows reliably include.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from .log import get_logger
from .models import D, Market, Token

log = get_logger(__name__)

SPORT_WORDS = {
    "sports", "esports", "tennis", "soccer", "football", "nfl", "nba", "mlb", "nhl", "mls", "ufc", "mma",
    "boxing", "cricket", "golf", "f1", "formula 1", "nascar", "baseball", "basketball", "hockey", "rugby",
    "lol", "league of legends", "cs2", "counter-strike", "csgo", "dota", "dota 2", "valorant", "olympics",
    "chess", "wnba", "ncaa", "college football", "college basketball", "premier league", "la liga",
    "serie a", "bundesliga", "ligue 1", "champions league", "atp", "wta", "us open", "wimbledon",
}
VS_RE = re.compile(r"\bvs\.?\b|\bv\.\b|\bversus\b", re.I)


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


def _labels(items: Any) -> set[str]:
    out: set[str] = set()
    for t in items or []:
        if isinstance(t, dict):
            for k in ("label", "slug", "name"):
                if t.get(k):
                    out.add(str(t[k]).lower())
        elif t:
            out.add(str(t).lower())
    return out


def derive_tags(m: dict[str, Any], ev: dict[str, Any]) -> list[str]:
    """Tags from the market and its event, plus a 'sports' tag by heuristic."""
    tags = _labels(m.get("tags")) | _labels(ev.get("tags"))
    if m.get("category"):
        tags.add(str(m["category"]).lower())
    text = " ".join(str(x) for x in (m.get("question"), ev.get("title"), m.get("groupItemTitle")) if x)
    is_sport = (
        bool(tags & SPORT_WORDS)
        or any(any(w in t for w in ("sport", "esport")) for t in tags)
        or bool(ev.get("series"))
        or bool(m.get("sportsMarketType") or m.get("gameStartTime") or ev.get("gameStartTime"))
        or bool(VS_RE.search(text))
    )
    if is_sport:
        tags.add("sports")
    return sorted(tags)


def market_from_gamma(m: dict[str, Any], ev: Optional[dict[str, Any]] = None) -> Optional[Market]:
    token_ids = _parse_json_list(m.get("clobTokenIds"))
    outcomes = _parse_json_list(m.get("outcomes"))
    if not token_ids or len(token_ids) != len(outcomes):
        return None
    cid = m.get("conditionId") or m.get("condition_id")
    if not cid:
        return None
    if ev is None:
        events = m.get("events") or []
        ev = events[0] if events else {}
    return Market(
        condition_id=cid,
        question=m.get("question") or "",
        slug=m.get("slug") or "",
        tokens=[Token(str(tid), str(o)) for tid, o in zip(token_ids, outcomes)],
        neg_risk=bool(m.get("negRisk", ev.get("negRisk", False))),
        neg_risk_augmented=bool(m.get("negRiskAugmented", ev.get("negRiskAugmented", False))),
        event_id=str(ev.get("id") or m.get("eventId") or ""),
        event_slug=str(ev.get("slug") or ""),
        tick_size=D(m.get("orderPriceMinTickSize") or "0.01"),
        min_order_size=D(m.get("orderMinSize") or "5"),
        end_date=_parse_dt(m.get("endDate") or m.get("endDateIso") or ev.get("endDate")),
        liquidity_usd=D(m.get("liquidityNum") or m.get("liquidity") or 0),
        volume_24h_usd=D(m.get("volume24hr") or 0),
        tags=derive_tags(m, ev),
        accepting_orders=bool(m.get("acceptingOrders", True)) and not bool(m.get("closed", False)),
    )


def markets_from_event(ev: dict[str, Any]) -> list[Market]:
    out = [mk for mk in (market_from_gamma(row, ev) for row in ev.get("markets") or []) if mk]
    active = sum(1 for m in out if m.accepting_orders and m.is_binary)
    for m in out:
        m.event_market_count = active
    return out


class Gamma:
    def __init__(self, base_url: str = "https://gamma-api.polymarket.com", timeout: float = 20.0):
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout, headers={"User-Agent": "pm/2"})

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, **params: Any) -> Any:
        r = await self._client.get(path, params=params)
        r.raise_for_status()
        return r.json()

    async def active_events(self, limit: int = 300) -> list[dict[str, Any]]:
        """Active, open events ordered by 24h volume (paged)."""
        out: list[dict[str, Any]] = []
        offset, page = 0, 100
        while len(out) < limit:
            rows = await self._get(
                "/events", active="true", closed="false", archived="false",
                order="volume24hr", ascending="false", limit=page, offset=offset,
            )
            if not rows:
                break
            out.extend(rows)
            if len(rows) < page:
                break
            offset += page
        return out[:limit]

    async def active_markets(self, limit: int = 500) -> list[Market]:
        """All accepting markets from the top-volume events, sorted by market 24h volume."""
        markets: list[Market] = []
        for ev in await self.active_events(limit=max(100, limit // 2)):
            markets.extend(m for m in markets_from_event(ev) if m.accepting_orders)
        markets.sort(key=lambda m: m.volume_24h_usd, reverse=True)
        return markets[:limit]

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
        return markets_from_event(ev or {})
