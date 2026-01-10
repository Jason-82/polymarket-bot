"""RSS feed client for news monitoring."""

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncIterator, Dict, List, Optional, Set
from xml.etree import ElementTree

import httpx

from .base import NewsSource, NewsEvent, NewsCategory


# High-quality RSS feeds for politics/world events
DEFAULT_RSS_FEEDS = {
    # Wire Services (fastest)
    "reuters_world": "https://feeds.reuters.com/Reuters/worldNews",
    "reuters_politics": "https://feeds.reuters.com/Reuters/PoliticsNews",
    "ap_topnews": "https://rsshub.app/apnews/topics/apf-topnews",

    # Major Papers
    "nyt_world": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "nyt_politics": "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",
    "wapo_politics": "https://feeds.washingtonpost.com/rss/politics",
    "wsj_world": "https://feeds.a]wsj.com/wsj/xml/rss/3_7085.xml",

    # International
    "bbc_world": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "guardian_world": "https://www.theguardian.com/world/rss",
    "ft_world": "https://www.ft.com/world?format=rss",

    # Financial/Economic
    "bloomberg_politics": "https://feeds.bloomberg.com/politics/news.rss",
    "cnbc_world": "https://www.cnbc.com/id/100727362/device/rss/rss.html",
}


@dataclass
class RSSConfig:
    """Configuration for RSS news source."""
    feeds: Dict[str, str] = field(default_factory=lambda: DEFAULT_RSS_FEEDS.copy())
    poll_interval_seconds: int = 60   # RSS updates less frequently
    max_age_hours: int = 1            # Ignore articles older than this
    enabled_feeds: Optional[List[str]] = None  # If set, only use these feeds


class RSSNewsSource(NewsSource):
    """
    RSS feed aggregator for news monitoring.

    Polls multiple RSS feeds and emits new articles as NewsEvents.
    """

    def __init__(self, config: Optional[RSSConfig] = None):
        super().__init__(
            name="rss",
            categories=[NewsCategory.POLITICS, NewsCategory.WORLD_EVENTS, NewsCategory.ECONOMICS],
        )
        self.config = config or RSSConfig()
        self._client: Optional[httpx.AsyncClient] = None
        self._seen_guids: Set[str] = set()

    async def connect(self) -> None:
        """Initialize HTTP client."""
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "User-Agent": "PolymarketBot/1.0 (RSS Reader)",
                "Accept": "application/rss+xml, application/xml, text/xml",
            },
            follow_redirects=True,
        )
        self._running = True

    async def disconnect(self) -> None:
        """Close HTTP client."""
        self._running = False
        if self._client:
            await self._client.aclose()
            self._client = None

    async def stream(self) -> AsyncIterator[NewsEvent]:
        """Poll RSS feeds and yield new articles."""
        if not self._client:
            raise RuntimeError("Not connected. Call connect() first.")

        while self._running:
            try:
                # Check all feeds
                feeds_to_check = self.config.enabled_feeds or list(self.config.feeds.keys())

                for feed_name in feeds_to_check:
                    if feed_name not in self.config.feeds:
                        continue

                    feed_url = self.config.feeds[feed_name]
                    events = await self._fetch_feed(feed_name, feed_url)

                    for event in events:
                        yield event

                    # Small delay between feeds
                    await asyncio.sleep(1)

            except Exception as e:
                print(f"RSS polling error: {e}")

            await asyncio.sleep(self.config.poll_interval_seconds)

    async def _fetch_feed(self, feed_name: str, feed_url: str) -> List[NewsEvent]:
        """Fetch and parse a single RSS feed."""
        if not self._client:
            return []

        try:
            response = await self._client.get(feed_url)
            response.raise_for_status()

            return self._parse_rss(feed_name, response.text)

        except httpx.HTTPStatusError as e:
            print(f"RSS feed error ({feed_name}): {e.response.status_code}")
            return []
        except Exception as e:
            print(f"RSS feed error ({feed_name}): {e}")
            return []

    def _parse_rss(self, feed_name: str, xml_content: str) -> List[NewsEvent]:
        """Parse RSS XML into NewsEvents."""
        events = []

        try:
            root = ElementTree.fromstring(xml_content)
        except ElementTree.ParseError as e:
            print(f"RSS parse error ({feed_name}): {e}")
            return []

        # Handle both RSS 2.0 and Atom formats
        items = root.findall(".//item")  # RSS 2.0
        if not items:
            items = root.findall(".//{http://www.w3.org/2005/Atom}entry")  # Atom

        max_age = self.config.max_age_hours * 3600
        now = datetime.utcnow()

        for item in items:
            try:
                event = self._parse_item(feed_name, item)

                if event is None:
                    continue

                # Skip if too old
                if event.age_seconds() > max_age:
                    continue

                # Skip if already seen
                if event.source_id in self._seen_guids:
                    continue

                self._seen_guids.add(event.source_id)
                events.append(event)

                # Cap seen GUIDs
                if len(self._seen_guids) > 5000:
                    self._seen_guids = set(list(self._seen_guids)[2500:])

            except Exception as e:
                print(f"RSS item parse error: {e}")

        return events

    def _parse_item(self, feed_name: str, item: ElementTree.Element) -> Optional[NewsEvent]:
        """Parse a single RSS item."""
        # Try RSS 2.0 format
        title = self._get_text(item, "title")
        link = self._get_text(item, "link")
        description = self._get_text(item, "description")
        guid = self._get_text(item, "guid") or link
        pub_date = self._get_text(item, "pubDate")

        # Try Atom format if RSS failed
        if not title:
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            title = self._get_text(item, "atom:title", ns)
            link = item.find("atom:link", ns)
            link = link.get("href") if link is not None else None
            description = self._get_text(item, "atom:summary", ns)
            guid = self._get_text(item, "atom:id", ns) or link
            pub_date = self._get_text(item, "atom:published", ns)

        if not title:
            return None

        # Parse timestamp
        timestamp = self._parse_date(pub_date) if pub_date else datetime.utcnow()

        # Generate ID
        event_id = hashlib.sha256(f"rss:{feed_name}:{guid}".encode()).hexdigest()[:16]

        # Categorize based on feed name
        categories = [NewsCategory.POLITICS, NewsCategory.WORLD_EVENTS]
        if "econom" in feed_name or "business" in feed_name or "bloomberg" in feed_name:
            categories.append(NewsCategory.ECONOMICS)

        return NewsEvent(
            id=event_id,
            headline=self._clean_text(title),
            body=self._clean_text(description) if description else None,
            source=f"rss:{feed_name}",
            source_id=guid or "",
            url=link,
            timestamp=timestamp,
            categories=categories,
            entities=[],
            keywords=[],
            is_verified=True,  # RSS from major outlets is verified
        )

    def _get_text(self, element: ElementTree.Element, path: str, ns: dict = None) -> Optional[str]:
        """Get text content of a child element."""
        child = element.find(path, ns) if ns else element.find(path)
        if child is not None and child.text:
            return child.text.strip()
        return None

    def _clean_text(self, text: str) -> str:
        """Clean HTML and whitespace from text."""
        import re
        # Remove HTML tags
        text = re.sub(r"<[^>]+>", "", text)
        # Normalize whitespace
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _parse_date(self, date_str: str) -> datetime:
        """Parse various date formats."""
        import email.utils

        # Try RFC 2822 (common in RSS)
        try:
            parsed = email.utils.parsedate_to_datetime(date_str)
            return parsed.replace(tzinfo=None)
        except:
            pass

        # Try ISO format
        try:
            return datetime.fromisoformat(date_str.replace("Z", "+00:00")).replace(tzinfo=None)
        except:
            pass

        # Fallback to now
        return datetime.utcnow()
