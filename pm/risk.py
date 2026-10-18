"""Risk gate. Every intent passes through here; anything that fails is dropped with a reason.

All limits are in dollars. The gate is conservative: an intent that replaces an
existing order of the same tag is charged only for its delta, but everything else
is counted at full notional.
"""

from __future__ import annotations

import dataclasses
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

        # Grouped intents (the legs of a complete set) are all-or-nothing: one leg alone is a directional
        # bet the strategy never asked for. A failing group is removed and the pass re-run, so budget its
        # other legs would have used is released to the remaining intents instead of leaking. Removing
        # intents only frees budget, so this converges in at most one extra pass per newly failed group.
        excluded: dict[str, str] = {}
        own_reason: dict[int, str] = {}
        accepted: list[Intent] = []
        rejected: list[tuple[Intent, str]] = []
        for _ in range(1 + len({i.group for i in intents if i.group})):
            candidates = [i for i in intents if not (i.group and i.group in excluded)]
            accepted, rejected = self._pass(candidates, ctx)
            newly = {i.group: why for i, why in rejected if i.group and i.group not in excluded}
            if not newly:
                break
            for i, why in rejected:
                if i.group in newly:
                    own_reason[id(i)] = why
            excluded.update(newly)
        rep.accepted = accepted
        rep.rejected = [(i, why) for i, why in rejected if not (i.group and i.group in excluded)]
        for i in intents:
            if i.group and i.group in excluded:
                rep.rejected.append((i, own_reason.get(id(i)) or f"group leg rejected: {excluded[i.group]}"))
        return rep

    def _pass(self, intents: list[Intent], ctx: Context) -> tuple[list[Intent], list[tuple[Intent, str]]]:
        """One sequential pass of the per-intent checks with running budget totals."""
        rep = RiskReport()
        day_pnl = ctx.portfolio.day_pnl(ctx.books)
        loss_limited = day_pnl <= -self.cfg.daily_loss_limit_usd

        open_by_tag = {o.tag: o for o in ctx.open_orders}
        total_position = sum((p.cost for p in ctx.portfolio.positions.values()), ZERO)
        mirrors = ctx.portfolio.mirrors
        held_avail: dict[str, Decimal] = {}          # held shares not yet claimed by a closing bid

        def avail(tok: str) -> Decimal:
            return held_avail.setdefault(tok, ctx.shares(tok))

        # On a mirrored venue (Polymarket US) buying one side while holding the other CLOSES the held side: that part
        # opens no exposure and needs no cash. Resting orders and new intents must be charged by the SAME rule; when
        # resting closers were charged in full but new ones were not, every order accepted on one tick put the book
        # over the cap on the next, the gate rejected them all, and quotes flapped on and off each tick.
        rest_charge: dict[str, Decimal] = {}
        rest_claim: dict[str, tuple[str, Decimal]] = {}
        rest_by_cid: dict[str, Decimal] = {}
        total_resting = ZERO
        resting_bids = ZERO                          # cash committed to resting bids
        for o in sorted(ctx.open_orders, key=lambda o: o.created_ts):   # older orders claim held shares first
            if o.side is Side.BUY:
                mir = mirrors.get(o.token_id)
                c = ZERO
                if mir:
                    c = min(o.remaining, avail(mir))
                    held_avail[mir] -= c
                    rest_claim[o.tag] = (mir, c)
                charge = (o.remaining - c) * o.price
                resting_bids += charge
            else:
                charge = o.remaining_notional        # resting sells: counted, conservatively
            rest_charge[o.tag] = charge
            total_resting += charge
            cid_o = ctx.token_to_market.get(o.token_id)
            if cid_o:
                rest_by_cid[cid_o] = rest_by_cid.get(cid_o, ZERO) + charge

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
            if existing is not None and it.tag in rest_claim:
                mir, c = rest_claim.pop(it.tag)      # this intent replaces that order: its claim is released
                held_avail[mir] += c
            mirror = mirrors.get(it.token_id) if it.side is Side.BUY else None
            closes = min(it.size, avail(mirror)) if mirror else ZERO
            opens = it.size - closes
            delta = opens * it.price - rest_charge.get(it.tag, ZERO)
            is_new = existing is None
            reduces_risk = ((it.side is Side.SELL and ctx.shares(it.token_id) >= it.size)
                            or (it.side is Side.BUY and closes > ZERO and opens <= ZERO))

            if loss_limited and not reduces_risk:
                rep.rejected.append((it, f"daily loss limit hit (day_pnl={day_pnl:.2f})"))
                continue

            if it.side is Side.SELL and ctx.shares(it.token_id) < it.size:
                rep.rejected.append((it, "cannot sell more than held (no shorting; buy the other outcome)"))
                continue

            if it.side is Side.BUY:
                if it.post_only and opens > ZERO:
                    secs = m.seconds_to_resolution(ctx.now)
                    if secs is not None and secs < self.cfg.min_hours_to_resolution * 3600:
                        rep.rejected.append((it, "too close to resolution for resting orders"))
                        continue

                cid = m.condition_id

                def cap_reason(d: Decimal) -> str:
                    mkt_exposure = (ctx.position_cost(cid) + rest_by_cid.get(cid, ZERO)
                                    + per_market_delta.get(cid, ZERO) + d)
                    if mkt_exposure > self.cfg.max_notional_per_market_usd:
                        return f"per-market cap {self.cfg.max_notional_per_market_usd} (would be {mkt_exposure:.2f})"
                    total = total_position + total_resting + accepted_delta_total + d
                    if total > self.cfg.max_total_notional_usd:
                        return f"total cap {self.cfg.max_total_notional_usd} (would be {total:.2f})"
                    if self.mode is not Mode.READ_ONLY:
                        free = ctx.portfolio.cash - resting_bids - accepted_delta_total - d
                        if free < self.cfg.min_cash_reserve_usd:
                            return f"cash reserve {self.cfg.min_cash_reserve_usd} (free would be {free:.2f})"
                    return ""

                why = cap_reason(delta)
                if why:
                    # A requote that no longer fits used to cancel the resting order outright, and the next tick
                    # often re-placed it: flapping. If the resting order is no riskier than the requote (same or
                    # lower bid, same or smaller size) and still fits as it stands, keep it unchanged instead.
                    if (existing is not None and existing.side is Side.BUY and existing.price <= it.price
                            and existing.size <= it.size and not cap_reason(ZERO)):
                        rep.accepted.append(dataclasses.replace(it, price=existing.price, size=existing.size,
                                                                reason=f"held: {why}"))
                        if mirror:
                            held_avail[mirror] = avail(mirror) - min(existing.remaining, avail(mirror))
                        continue
                    rep.rejected.append((it, why))
                    continue

            if is_new and len(ctx.open_orders) + accepted_new_orders >= self.cfg.max_open_orders:
                rep.rejected.append((it, f"max open orders {self.cfg.max_open_orders}"))
                continue

            # accept
            rep.accepted.append(it)
            if it.side is Side.BUY:
                accepted_delta_total += delta
                per_market_delta[m.condition_id] = per_market_delta.get(m.condition_id, ZERO) + delta
                if mirror and closes > ZERO:
                    held_avail[mirror] = avail(mirror) - closes
            if is_new:
                accepted_new_orders += 1

        return rep.accepted, rep.rejected

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
