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


def build_strategies(strategy_cfg: dict[str, dict[str, Any]]) -> list[Strategy]:
    # Import for side effects (registration)
    from . import complete_set, favorite_yield, maker  # noqa: F401

    out: list[Strategy] = []
    for name, params in strategy_cfg.items():
        if not params or not params.get("enabled", False):
            continue
        cls = REGISTRY.get(name)
        if cls is None:
            raise ValueError(f"unknown strategy '{name}' (known: {sorted(REGISTRY)})")
        out.append(cls(params))
    return out
