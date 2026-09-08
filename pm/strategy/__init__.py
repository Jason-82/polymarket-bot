"""Strategy protocol and registry.

A strategy is a pure function of the Context: on_tick(ctx) -> list[Intent].
No I/O, no clocks (use ctx.now / ctx.now_ts), no hidden state that the
backtester could not reproduce.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..models import Intent
from ..state import Context


@runtime_checkable
class Strategy(Protocol):
    name: str

    def on_tick(self, ctx: Context) -> list[Intent]: ...


REGISTRY: dict[str, type] = {}


def register(name: str):
    def deco(cls):
        cls.name = name
        REGISTRY[name] = cls
        return cls
    return deco


def resolve_params(params: dict[str, Any], venue: str = "") -> dict[str, Any]:
    """Apply `venue_overrides.<venue>` on top of the base params (venues differ in tick, spread, fees)."""
    base = {k: v for k, v in (params or {}).items() if k != "venue_overrides"}
    overrides = (params or {}).get("venue_overrides") or {}
    key = (venue or "").lower().replace("-", "_")
    for k, v in overrides.items():
        if str(k).lower().replace("-", "_") == key and isinstance(v, dict):
            base.update(v)
    return base


def build_strategies(strategy_cfg: dict[str, dict[str, Any]], venue: str = "") -> list[Strategy]:
    # Import for side effects (registration)
    from . import complete_set, favorite_yield, maker  # noqa: F401

    out: list[Strategy] = []
    for name, params in strategy_cfg.items():
        if not params or not params.get("enabled", False):
            continue
        cls = REGISTRY.get(name)
        if cls is None:
            raise ValueError(f"unknown strategy '{name}' (known: {sorted(REGISTRY)})")
        out.append(cls(resolve_params(params, venue)))
    return out
