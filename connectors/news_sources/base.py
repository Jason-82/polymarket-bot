"""Base classes for news sources."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import AsyncIterator, Callable, List, Optional, Set


class NewsCategory(str, Enum):
    """Categories for news classification."""
    POLITICS = "politics"
    WORLD_EVENTS = "world_events"
    ECONOMICS = "economics"
    CRYPTO = "crypto"
    SPORTS = "sports"
    ENTERTAINMENT = "entertainment"
    SCIENCE = "science"
    TECHNOLOGY = "technology"
    WEATHER = "weather"
    LEGAL = "legal"
    UNKNOWN = "unknown"


@dataclass
class NewsEvent:
    """
    Represents a news event from any source.

    This is the canonical news format that all sources convert to.
    """
    id: str                          # Unique identifier
    headline: str                    # Main headline/title
    body: Optional[str] = None       # Full text if available
    source: str = ""                 # Source name (twitter, reuters, etc.)
    source_id: str = ""              # Original ID from source
    url: Optional[str] = None        # Link to original
    timestamp: datetime = field(default_factory=datetime.utcnow)

    # Classification
    categories: List[NewsCategory] = field(default_factory=list)
    entities: List[str] = field(default_factory=list)  # Named entities (people, places, orgs)
    keywords: List[str] = field(default_factory=list)  # Extracted keywords

    # Metadata
    author: Optional[str] = None
    is_verified: bool = False        # From verified/official source
    engagement: Optional[int] = None  # Likes, retweets, etc.

    # Processing state
    processed: bool = False
    signal_generated: bool = False

    def __hash__(self):
        return hash(self.id)

    def age_seconds(self) -> float:
        """Get age of news event in seconds."""
        return (datetime.utcnow() - self.timestamp).total_seconds()

    def is_stale(self, max_age_seconds: int = 300) -> bool:
        """Check if news is too old to act on (default 5 minutes)."""
        return self.age_seconds() > max_age_seconds

    def full_text(self) -> str:
        """Get full text combining headline and body."""
        if self.body:
            return f"{self.headline}\n\n{self.body}"
        return self.headline


class NewsSource(ABC):
    """
    Abstract base class for news sources.

    Implementations should:
    - Connect to a news source (Twitter, RSS, etc.)
    - Stream or poll for new events
    - Convert to NewsEvent format
    """

    def __init__(self, name: str, categories: Optional[List[NewsCategory]] = None):
        self.name = name
        self.categories = categories or list(NewsCategory)
        self._callbacks: List[Callable[[NewsEvent], None]] = []
        self._running = False
        self._seen_ids: Set[str] = set()

    @abstractmethod
    async def connect(self) -> None:
        """Connect to the news source."""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from the news source."""
        pass

    @abstractmethod
    async def stream(self) -> AsyncIterator[NewsEvent]:
        """Stream news events as they arrive."""
        pass

    def on_news(self, callback: Callable[[NewsEvent], None]) -> None:
        """Register a callback for new events."""
        self._callbacks.append(callback)

    def _emit(self, event: NewsEvent) -> None:
        """Emit event to all registered callbacks."""
        # Deduplicate
        if event.id in self._seen_ids:
            return
        self._seen_ids.add(event.id)

        # Cap seen IDs to prevent memory leak
        if len(self._seen_ids) > 10000:
            # Remove oldest half
            self._seen_ids = set(list(self._seen_ids)[5000:])

        for callback in self._callbacks:
            try:
                callback(event)
            except Exception as e:
                print(f"Error in news callback: {e}")

    @property
    def is_running(self) -> bool:
        return self._running
