"""Why did a resting quote get withdrawn? Every withdrawal costs queue position, which on deep books is the
difference between being filled and never being filled, so the engine and the backtest both count them."""

from __future__ import annotations

from collections import Counter
from typing import Iterable, Optional

from .models import Intent, Order, TimeInForce


class Withdrawals:
    def __init__(self) -> None:
        self.reasons: Counter = Counter()
        self.by_tag: Counter = Counter()

    def record(self, open_orders: Iterable[Order], accepted: Iterable[Intent],
               rejected: Iterable[tuple[Intent, str]], strategies: Iterable, market_for) -> None:
        desired = {i.tag for i in accepted if i.tif is TimeInForce.GTC}
        risk = {i.tag: why.split(" (")[0] for i, why in rejected}
        skips: dict[str, str] = {}
        for s in strategies:
            skips.update(getattr(s, "last_skip", {}) or {})
        for o in open_orders:
            if o.tag in desired:
                continue
            if o.tag in risk:
                why = f"risk: {risk[o.tag]}"
            else:
                m = market_for(o.token_id)
                why = f"strategy: {skips.get(m.condition_id, 'not quoted')}" if m else "market left universe"
            self.reasons[why] += 1
            self.by_tag[o.tag] += 1

    @property
    def total(self) -> int:
        return sum(self.reasons.values())

    def top(self, n: int = 6) -> list[tuple[str, int]]:
        return self.reasons.most_common(n)

    def summary(self, n: int = 3) -> str:
        return "; ".join(f"{why}={k}" for why, k in self.top(n)) or "none"
