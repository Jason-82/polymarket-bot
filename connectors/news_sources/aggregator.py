"""News aggregator that combines multiple news sources."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional, Set

from .base import NewsSource, NewsEvent, NewsCategory


@dataclass
class AggregatorConfig:
    """Configuration for news aggregator."""
    # Deduplication
    dedup_window_seconds: int = 300     # Consider events duplicates if within 5 min
    similarity_threshold: float = 0.7   # Jaccard similarity for dedup

    # Filtering
    min_source_priority: int = 0        # Minimum priority to emit
    categories: List[NewsCategory] = field(
        default_factory=lambda: [NewsCategory.POLITICS, NewsCategory.WORLD_EVENTS]
    )

    # Rate limiting
    max_events_per_minute: int = 60     # Prevent event storms


@dataclass
class SourcePriority:
    """Priority configuration for a news source."""
    source: NewsSource
    priority: int = 5                   # 1-10, higher = more trusted
    latency_bonus: float = 1.0          # Multiplier for breaking news


class NewsAggregator:
    """
    Aggregates news from multiple sources.

    Features:
    - Deduplication across sources
    - Priority-based filtering
    - Rate limiting
    - Unified event stream
    """

    def __init__(self, config: Optional[AggregatorConfig] = None):
        self.config = config or AggregatorConfig()
        self._sources: List[SourcePriority] = []
        self._callbacks: List[Callable[[NewsEvent], None]] = []
        self._running = False

        # Deduplication state
        self._recent_events: List[NewsEvent] = []
        self._event_hashes: Set[str] = set()

        # Rate limiting
        self._events_this_minute: int = 0
        self._minute_start: datetime = datetime.utcnow()

        # Tasks
        self._tasks: List[asyncio.Task] = []

    def add_source(
        self,
        source: NewsSource,
        priority: int = 5,
        latency_bonus: float = 1.0,
    ) -> None:
        """Add a news source to the aggregator."""
        self._sources.append(SourcePriority(
            source=source,
            priority=priority,
            latency_bonus=latency_bonus,
        ))

    def on_news(self, callback: Callable[[NewsEvent], None]) -> None:
        """Register callback for aggregated news events."""
        self._callbacks.append(callback)

    async def start(self) -> None:
        """Start all news sources and begin aggregation."""
        self._running = True

        # Connect all sources
        for sp in self._sources:
            await sp.source.connect()

        # Start streaming tasks for each source
        for sp in self._sources:
            task = asyncio.create_task(self._stream_source(sp))
            self._tasks.append(task)

    async def stop(self) -> None:
        """Stop all news sources."""
        self._running = False

        # Cancel streaming tasks
        for task in self._tasks:
            task.cancel()

        # Disconnect all sources
        for sp in self._sources:
            await sp.source.disconnect()

        self._tasks.clear()

    async def _stream_source(self, sp: SourcePriority) -> None:
        """Stream events from a single source."""
        try:
            async for event in sp.source.stream():
                if not self._running:
                    break

                # Apply priority
                event_with_priority = self._apply_priority(event, sp)

                # Check filters and dedup
                if self._should_emit(event_with_priority):
                    self._emit(event_with_priority)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"Error streaming from {sp.source.name}: {e}")

    def _apply_priority(self, event: NewsEvent, sp: SourcePriority) -> NewsEvent:
        """Apply source priority to event."""
        # Store priority in keywords for now (hacky but works)
        event.keywords = event.keywords + [f"priority:{sp.priority}"]
        return event

    def _should_emit(self, event: NewsEvent) -> bool:
        """Check if event should be emitted."""
        # Category filter
        if not any(cat in self.config.categories for cat in event.categories):
            return False

        # Deduplication check
        if self._is_duplicate(event):
            return False

        # Rate limiting
        if not self._check_rate_limit():
            return False

        return True

    def _is_duplicate(self, event: NewsEvent) -> bool:
        """Check if event is a duplicate of a recent event."""
        # Quick hash check
        event_hash = self._hash_event(event)
        if event_hash in self._event_hashes:
            return True

        # Similarity check against recent events
        event_words = self._tokenize(event.headline)
        cutoff_time = datetime.utcnow().timestamp() - self.config.dedup_window_seconds

        for recent in self._recent_events:
            if recent.timestamp.timestamp() < cutoff_time:
                continue

            recent_words = self._tokenize(recent.headline)
            similarity = self._jaccard_similarity(event_words, recent_words)

            if similarity >= self.config.similarity_threshold:
                return True

        # Not a duplicate - add to tracking
        self._event_hashes.add(event_hash)
        self._recent_events.append(event)

        # Cleanup old events
        self._cleanup_old_events()

        return False

    def _hash_event(self, event: NewsEvent) -> str:
        """Generate a simple hash for quick dedup."""
        # Use first 50 chars of headline, normalized
        text = event.headline.lower()[:50]
        text = "".join(c for c in text if c.isalnum() or c.isspace())
        return text

    def _tokenize(self, text: str) -> Set[str]:
        """Tokenize text into words for similarity comparison."""
        text = text.lower()
        words = set(text.split())
        # Remove common words
        stopwords = {"the", "a", "an", "is", "are", "was", "were", "in", "on", "at", "to", "for"}
        return words - stopwords

    def _jaccard_similarity(self, set1: Set[str], set2: Set[str]) -> float:
        """Calculate Jaccard similarity between two sets."""
        if not set1 or not set2:
            return 0.0
        intersection = len(set1 & set2)
        union = len(set1 | set2)
        return intersection / union if union > 0 else 0.0

    def _cleanup_old_events(self) -> None:
        """Remove old events from tracking."""
        cutoff_time = datetime.utcnow().timestamp() - self.config.dedup_window_seconds

        self._recent_events = [
            e for e in self._recent_events
            if e.timestamp.timestamp() >= cutoff_time
        ]

        # Cap hashes
        if len(self._event_hashes) > 1000:
            self._event_hashes = set(list(self._event_hashes)[500:])

    def _check_rate_limit(self) -> bool:
        """Check if we're within rate limit."""
        now = datetime.utcnow()

        # Reset counter if new minute
        if (now - self._minute_start).total_seconds() >= 60:
            self._events_this_minute = 0
            self._minute_start = now

        if self._events_this_minute >= self.config.max_events_per_minute:
            return False

        self._events_this_minute += 1
        return True

    def _emit(self, event: NewsEvent) -> None:
        """Emit event to all callbacks."""
        for callback in self._callbacks:
            try:
                callback(event)
            except Exception as e:
                print(f"Error in news callback: {e}")

    @property
    def source_count(self) -> int:
        """Number of configured sources."""
        return len(self._sources)

    @property
    def is_running(self) -> bool:
        return self._running
