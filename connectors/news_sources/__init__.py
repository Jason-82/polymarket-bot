"""News source connectors for real-time news monitoring."""

from .base import NewsSource, NewsEvent, NewsCategory
from .twitter_client import TwitterNewsSource
from .rss_client import RSSNewsSource
from .aggregator import NewsAggregator

__all__ = [
    "NewsSource",
    "NewsEvent",
    "NewsCategory",
    "TwitterNewsSource",
    "RSSNewsSource",
    "NewsAggregator",
]
