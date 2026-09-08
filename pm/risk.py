"""Risk gate. Every intent passes through here; anything that fails is dropped with a reason.

All limits are in dollars. The gate is conservative: an intent that replaces an
existing order of the same tag is charged only for its delta, but everything else
is counted at full notional.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .config import RiskConfig
from .log import get_logger
from .models import ONE, ZERO, Intent, Mode, Side
from .state import Context

log = get_logger(__name__)


@dataclass
class RiskReport:
    accepted: list[Intent] = field(default_factory=list)
    rejected: list[tuple[Intent, str]] = field(default_factory=list)
    halted: bool = False
    halt_reason: str = ""


class RiskGate:
    def __init__(self, cfg: RiskConfig, mode: Mode):
        self.cfg = cfg
        self.mode = mode

    def kill_switch_active(self) -> bool:
        return Path(self.cfg.kill_switch_file).exists()

    def evaluate(self, intents: list[Intent], ctx: Context, geoblock_ok: bool) -> RiskReport:
        rep = RiskReport()

        # ---- global halts
        if self.kill_switch_active():
            rep.halted, rep.halt_reason = True, f"kill switch file '{self.cfg.kill_switch_file}' present"
        elif self.mode is Mode.LIVE and self.cfg.require_geoblock_ok_for_live and not geoblock_ok:
            rep.halted, rep.halt_reason = True, "geoblocked"
        if rep.halted:
            rep.rejected = [(i, rep.halt_reason) for i in intents]
            return rep

        day_pnl = ctx.portfolio.day_pnl(ctx.books)
        loss_limited = day_pnl <= -self.cfg.daily_loss_limit_usd

        open_by_tag = {o.tag: o for o in ctx.open_orders}
        total_resting = sum((o.remaining_notional for o in ctx.open_orders), ZERO)
        total_position = sum((p.cost for p in ctx.portfolio.positions.values()), ZERO)
        # Cash committed to resting bids (paper/live)
        resting_bids = sum((o.remaining_notional for o in ctx.open_orders if o.side is Side.BUY), ZERO)

        accepted_delta_total = ZERO
        accepted_new_orders = 0
        per_market_delta: dict[str, Decimal] = {}

        for it in intents:
            m = ctx.market_for(it.token_id)
            reason = self._static_checks(it, m)
            if reason:
                rep.rejected.append((it, reason))
                continue

            existing = open_by_tag.get(it.tag)
            delta = it.notional - (existing.remaining_notional if existing else ZERO)
            is_new = existing is None
            reduces_risk = it.side is Side.SELL and ctx.shares(it.token_id) >= it.size

            if loss_limited and not reduces_risk:
                rep.rejected.append((it, f"daily loss limit hit (day_pnl={day_pnl:.2f})"))
                continue

            if it.side is Side.SELL and ctx.shares(it.token_id) < it.size:
                rep.rejected.append((it, "cannot sell more than held (no shorting; buy the other outcome)"))
                continue

            if it.side is Side.BUY:
                if it.post_only:
                    secs = m.seconds_to_resolution(ctx.now)
                    if secs is not None and secs < self.cfg.min_hours_to_resolution * 3600:
                        rep.rejected.append((it, "too close to resolution for resting orders"))
                        continue

                cid = m.condition_id
                mkt_exposure = ctx.position_cost(cid) + ctx.resting_notional(cid) + per_market_delta.get(cid, ZERO) + delta
                if mkt_exposure > self.cfg.max_notional_per_market_usd:
                    rep.rejected.append((it, f"per-market cap {self.cfg.max_notional_per_market_usd} (would be {mkt_exposure:.2f})"))
                    continue

                total = total_position + total_resting + accepted_delta_total + delta
                if total > self.cfg.max_total_notional_usd:
                    rep.rejected.append((it, f"total cap {self.cfg.max_total_notional_usd} (would be {total:.2f})"))
                    continue

                if self.mode is not Mode.READ_ONLY:
                    free = ctx.portfolio.cash - resting_bids - accepted_delta_total - delta
                    if free < self.cfg.min_cash_reserve_usd:
                        rep.rejected.append((it, f"cash reserve {self.cfg.min_cash_reserve_usd} (free would be {free:.2f})"))
                        continue

            if is_new and len(ctx.open_orders) + accepted_new_orders >= self.cfg.max_open_orders:
                rep.rejected.append((it, f"max open orders {self.cfg.max_open_orders}"))
                continue

            # accept
            rep.accepted.append(it)
            if it.side is Side.BUY:
                accepted_delta_total += delta
                per_market_delta[m.condition_id] = per_market_delta.get(m.condition_id, ZERO) + delta
            if is_new:
                accepted_new_orders += 1

        return rep

    @staticmethod
    def _static_checks(it: Intent, m) -> str:
        if m is None:
            return "unknown market"
        if not (ZERO < it.price < ONE):
            return "price outside (0,1)"
        if it.size <= ZERO:
            return "non-positive size"
        if it.size < m.min_order_size:
            return f"size below market minimum {m.min_order_size}"
        if (it.price / m.tick_size) % 1 != 0:
            return f"price not on tick {m.tick_size}"
        if not m.accepting_orders:
            return "market not accepting orders"
        return ""
