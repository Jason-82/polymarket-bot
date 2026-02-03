"""Strategy loading and management."""

import importlib
from typing import Any, Dict, List, Optional, Type

from strategies.base import StrategyBase


# Registry of available strategies
_STRATEGY_REGISTRY: Dict[str, Type[StrategyBase]] = {}


def register_strategy(name: str):
    """Decorator to register a strategy class."""
    def decorator(cls: Type[StrategyBase]):
        _STRATEGY_REGISTRY[name] = cls
        cls.name = name
        return cls
    return decorator


def get_available_strategies() -> List[str]:
    """Get list of available strategy names."""
    # Ensure built-in strategies are imported
    _ensure_builtins_loaded()
    return list(_STRATEGY_REGISTRY.keys())


def load_strategy(
    name: str,
    config: Optional[Dict[str, Any]] = None
) -> StrategyBase:
    """
    Load a strategy by name.

    Args:
        name: Strategy name (must be registered)
        config: Strategy-specific configuration

    Returns:
        Instantiated strategy object

    Raises:
        ValueError: If strategy name not found
    """
    _ensure_builtins_loaded()

    if name not in _STRATEGY_REGISTRY:
        available = ", ".join(get_available_strategies())
        raise ValueError(
            f"Unknown strategy: {name}. Available: {available}"
        )

    strategy_cls = _STRATEGY_REGISTRY[name]
    return strategy_cls(name=name, params=config or {}, tokens=[])


def load_strategies_from_config(
    strategy_configs: List[Dict[str, Any]]
) -> List[StrategyBase]:
    """
    Load multiple strategies from configuration.

    Args:
        strategy_configs: List of strategy config dicts with 'name' and 'params'

    Returns:
        List of instantiated strategy objects
    """
    _ensure_builtins_loaded()
    strategies = []

    for cfg in strategy_configs:
        if not cfg.get("enabled", True):
            continue

        name = cfg["name"]
        params = cfg.get("params", {})
        tokens = cfg.get("tokens", [])

        strategy_cls = _STRATEGY_REGISTRY[name]
        strategy = strategy_cls(name=name, params=params, tokens=tokens)
        strategies.append(strategy)

    return strategies


def _ensure_builtins_loaded() -> None:
    """Ensure built-in strategies are imported and registered."""
    if not _STRATEGY_REGISTRY:
        # Import built-in strategies to trigger registration
        try:
            from strategies import market_maker  # noqa: F401
            from strategies import value_threshold  # noqa: F401
            from strategies import news_alpha  # noqa: F401
        except ImportError:
            pass  # Strategies may not be implemented yet
