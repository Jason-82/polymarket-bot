"""Universe selection: which markets to watch and quote."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from .clob import Clob
from .config import UniverseConfig
from .gamma import Gamma
from .log import get_logger
from .models import Market, utcnow

log = get_logger(__name__)


def passes_filters(m: Market, c: UniverseConfig, require_end_date: bool = True) -> bool:
    """Venue-independent universe filters."""
    if not m.is_binary or not m.accepting_orders:
        return False
    if m.neg_risk and not c.include_neg_risk:
        return False
    if m.liquidity_usd < c.min_liquidity_usd or m.volume_24h_usd < c.min_volume_24h_usd:
        return False
    if c.prefer_rewards and c.min_reward_rate_per_day > 0 and m.reward_rate_per_day < c.min_reward_rate_per_day:
        return False
    excluded = {x.lower() for x in c.exclude_tags}
    if any(t in excluded for t in m.tags):
        return False
    if m.end_date is None:
        return not require_end_date
    now = utcnow()
    if m.end_date < now + timedelta(days=c.min_days_to_resolution):
        return False
    if m.end_date > now + timedelta(days=c.max_days_to_resolution):
        return False
    return True


class Universe:
    def __init__(self, gamma: Gamma, clob: Clob, cfg: UniverseConfig):
        self.gamma = gamma
        self.clob = clob
        self.cfg = cfg
        self._fee_cache: dict[str, Market] = {}

    async def select(self) -> list[Market]:
        rewards = await self.clob.rewards_by_condition() if self.cfg.prefer_rewards else {}
        if self.cfg.condition_ids:
            markets = await self.gamma.markets_by_condition(self.cfg.condition_ids)
            markets = await self._complete_neg_risk_events(markets)
            self._attach_rewards(markets, rewards)
        else:
            all_markets = await self.gamma.active_markets(limit=max(600, self.cfg.max_markets * 10))
            self._attach_rewards(all_markets, rewards)
            markets = self._pick(all_markets)
        markets = await self._enrich(markets)
        groups = len({m.event_id for m in markets if m.neg_risk and m.event_id})
        incentivised = sum(1 for m in markets if m.has_rewards)
        pool = sum((m.reward_rate_per_day for m in markets), Decimal("0"))
        log.info("universe_selected", markets=len(markets), tokens=sum(len(m.tokens) for m in markets),
                 neg_risk_events=groups, incentivised=incentivised, reward_pool_per_day=str(pool),
                 rewards_known=len(rewards))
        return markets

    @staticmethod
    def _attach_rewards(markets: list[Market], rewards: dict) -> None:
        for m in markets:
            r = rewards.get(m.condition_id)
            if r is None:
                continue
            m.reward_rate_per_day = r.rate_per_day
            m.reward_max_spread = r.max_spread
            m.reward_min_size = r.min_size
            m.reward_competitiveness = r.competitiveness

    def _rank_key(self, m: Market):
        # Incentivised markets first, biggest daily pool first; volume breaks ties.
        if self.cfg.prefer_rewards:
            return (m.reward_rate_per_day, m.volume_24h_usd)
        return (Decimal("0"), m.volume_24h_usd)

    # ------------------------------------------------------------------ selection
    def _pick(self, all_markets: list[Market]) -> list[Market]:
        """Walk markets by rank; neg-risk markets bring their whole event if it is small enough."""
        all_markets = sorted(all_markets, key=self._rank_key, reverse=True)
        by_event: dict[str, list[Market]] = {}
        for m in all_markets:
            if m.event_id and m.accepting_orders and m.is_binary:
                by_event.setdefault(m.event_id, []).append(m)

        selected: list[Market] = []
        have: set[str] = set()
        singleton_events: set[str] = set()
        for m in all_markets:
            if len(selected) >= self.cfg.max_markets:
                break
            if m.condition_id in have or not self._passes(m):
                continue
            group = [m]
            if m.neg_risk and m.event_id:
                ev = by_event.get(m.event_id, [])
                if 2 <= len(ev) <= self.cfg.max_event_markets and len(selected) + len(ev) <= self.cfg.max_markets:
                    group = ev
                else:
                    # Event too large to complete: take at most ONE market from it as a plain
                    # binary (complete_set stays out of incomplete groups; the maker may quote it).
                    # Without this cap a 30-candidate field would fill the whole universe.
                    if m.event_id in singleton_events:
                        continue
                    singleton_events.add(m.event_id)
            for x in group:
                if x.condition_id not in have:
                    selected.append(x)
                    have.add(x.condition_id)
        return selected

    def _passes(self, m: Market) -> bool:
        return passes_filters(m, self.cfg, require_end_date=True)

    async def _complete_neg_risk_events(self, markets: list[Market]) -> list[Market]:
        """Explicit condition_ids path: pull the rest of any neg-risk event we were given."""
        have = {m.condition_id for m in markets}
        out = list(markets)
        for eid in sorted({m.event_id for m in markets if m.neg_risk and m.event_id}):
            try:
                ev_markets = [m for m in await self.gamma.event_markets(eid) if m.accepting_orders and m.is_binary]
            except Exception as e:
                log.warning("event_fetch_failed", event=eid, error=str(e)[:200])
                continue
            for m in ev_markets:
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
