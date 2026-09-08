"""Universe selection: which markets to watch and quote."""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

from .clob import Clob
from .config import UniverseConfig
from .gamma import Gamma
from .log import get_logger
from .models import Market, utcnow

log = get_logger(__name__)


class Universe:
    def __init__(self, gamma: Gamma, clob: Clob, cfg: UniverseConfig):
        self.gamma = gamma
        self.clob = clob
        self.cfg = cfg
        self._fee_cache: dict[str, Market] = {}

    async def select(self) -> list[Market]:
        if self.cfg.condition_ids:
            markets = await self.gamma.markets_by_condition(self.cfg.condition_ids)
        else:
            candidates = await self.gamma.active_markets(limit=max(400, self.cfg.max_markets * 5))
            markets = [m for m in candidates if self._passes(m)]
            markets.sort(key=lambda m: m.volume_24h_usd, reverse=True)
            markets = markets[: self.cfg.max_markets]

        markets = await self._complete_neg_risk_events(markets)
        markets = await self._enrich(markets)
        log.info("universe_selected", markets=len(markets), tokens=sum(len(m.tokens) for m in markets))
        return markets

    # ------------------------------------------------------------------ filters
    def _passes(self, m: Market) -> bool:
        c = self.cfg
        if not m.is_binary or not m.accepting_orders:
            return False
        if m.neg_risk and not c.include_neg_risk:
            return False
        if m.liquidity_usd < c.min_liquidity_usd or m.volume_24h_usd < c.min_volume_24h_usd:
            return False
        if any(t in m.tags for t in (x.lower() for x in c.exclude_tags)):
            return False
        if m.end_date is None:
            return False
        now = utcnow()
        if m.end_date < now + timedelta(days=c.min_days_to_resolution):
            return False
        if m.end_date > now + timedelta(days=c.max_days_to_resolution):
            return False
        return True

    async def _complete_neg_risk_events(self, markets: list[Market]) -> list[Market]:
        """For every neg-risk event we touch, hold all of its active markets so Σ YES is meaningful."""
        have = {m.condition_id for m in markets}
        out = list(markets)
        for eid in sorted({m.event_id for m in markets if m.neg_risk and m.event_id}):
            try:
                ev_markets = [m for m in await self.gamma.event_markets(eid) if m.accepting_orders and m.is_binary]
            except Exception as e:
                log.warning("event_fetch_failed", event=eid, error=str(e)[:200])
                continue
            for m in ev_markets:
                m.event_market_count = len(ev_markets)
                if m.condition_id not in have:
                    out.append(m)
                    have.add(m.condition_id)
            for m in out:
                if m.event_id == eid:
                    m.event_market_count = len(ev_markets)
        return out

    async def _enrich(self, markets: list[Market]) -> list[Market]:
        out = []
        for m in markets:
            cached = self._fee_cache.get(m.condition_id)
            if cached is not None:
                m.tick_size, m.neg_risk = cached.tick_size, cached.neg_risk
                m.fee_rate, m.fee_exponent, m.fee_known = cached.fee_rate, cached.fee_exponent, cached.fee_known
            else:
                m = await self.clob.enrich_market(m)
                if m.fee_known:
                    self._fee_cache[m.condition_id] = m
            out.append(m)
        return out
