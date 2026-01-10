"""Twitter/X API client for real-time news monitoring."""

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncIterator, Dict, List, Optional, Set

import httpx

from .base import NewsSource, NewsEvent, NewsCategory

# Political/world events keywords for filtering
DEFAULT_POLITICS_KEYWORDS = [
    # US Politics
    "president", "congress", "senate", "white house", "supreme court",
    "biden", "trump", "election", "vote", "legislation", "bill signed",
    "executive order", "impeach", "inaugur",

    # World Events
    "war", "invasion", "military", "troops", "nato", "ukraine", "russia",
    "china", "taiwan", "iran", "israel", "gaza", "sanctions", "treaty",
    "united nations", "g7", "g20", "summit",

    # Economics/Policy
    "fed ", "federal reserve", "interest rate", "inflation", "tariff",
    "trade war", "recession", "gdp", "unemployment", "jobs report",

    # Major events
    "breaking:", "just in:", "developing:", "urgent:",
    "explosion", "earthquake", "hurricane", "assassination", "coup",
]

# High-value accounts for politics/world events
DEFAULT_POLITICS_ACCOUNTS = [
    # News Organizations
    "reuters", "ap", "afp", "baboringbnews", "caboringbnews",
    "nytimes", "washingtonpost", "wsj", "ft", "economist",
    "baboringbc", "baboringbcworld", "baboringbcbreaking",

    # Wire Services & Breaking
    "breaking911", "disclosetv", "spectatorindex",

    # Government/Official
    "potus", "whitehouse", "secdef", "statedept",
    "pentagonpresssec", "nsaboringcgov",

    # Journalists (notable for breaking news)
    "jimsaboringciutto", "jaaboringketapper", "maggienyt",
]


@dataclass
class TwitterConfig:
    """Configuration for Twitter news source."""
    bearer_token: str
    keywords: List[str] = field(default_factory=lambda: DEFAULT_POLITICS_KEYWORDS.copy())
    accounts: List[str] = field(default_factory=lambda: DEFAULT_POLITICS_ACCOUNTS.copy())
    include_retweets: bool = False
    min_followers: int = 10000          # Filter low-quality accounts
    poll_interval_seconds: int = 15     # How often to check for new tweets
    max_results_per_poll: int = 100


class TwitterNewsSource(NewsSource):
    """
    Twitter/X news source using the API v2.

    Monitors:
    - Specific accounts (journalists, news orgs, officials)
    - Keyword searches (filtered stream)
    """

    def __init__(self, config: TwitterConfig):
        super().__init__(name="twitter", categories=[NewsCategory.POLITICS, NewsCategory.WORLD_EVENTS])
        self.config = config
        self._client: Optional[httpx.AsyncClient] = None
        self._last_tweet_ids: Dict[str, str] = {}  # account -> last tweet id

    async def connect(self) -> None:
        """Initialize the HTTP client."""
        self._client = httpx.AsyncClient(
            base_url="https://api.twitter.com/2",
            headers={
                "Authorization": f"Bearer {self.config.bearer_token}",
                "User-Agent": "PolymarketBot/1.0",
            },
            timeout=30.0,
        )
        self._running = True

    async def disconnect(self) -> None:
        """Close the HTTP client."""
        self._running = False
        if self._client:
            await self._client.aclose()
            self._client = None

    async def stream(self) -> AsyncIterator[NewsEvent]:
        """
        Stream tweets by polling the search endpoint.

        Note: True streaming requires Twitter API v2 filtered stream,
        which has complex setup. Polling is simpler and sufficient
        for our latency needs (~15 second delay is acceptable).
        """
        if not self._client:
            raise RuntimeError("Not connected. Call connect() first.")

        while self._running:
            try:
                # Search for recent tweets matching our keywords
                events = await self._search_recent_tweets()
                for event in events:
                    yield event

                # Also check specific accounts
                account_events = await self._check_accounts()
                for event in account_events:
                    yield event

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    # Rate limited - back off
                    await asyncio.sleep(60)
                else:
                    raise
            except Exception as e:
                print(f"Twitter polling error: {e}")

            await asyncio.sleep(self.config.poll_interval_seconds)

    async def _search_recent_tweets(self) -> List[NewsEvent]:
        """Search for recent tweets matching keywords."""
        if not self._client:
            return []

        # Build query from keywords
        keyword_query = " OR ".join(f'"{kw}"' for kw in self.config.keywords[:20])  # API limit
        query = f"({keyword_query}) -is:retweet lang:en"

        try:
            response = await self._client.get(
                "/tweets/search/recent",
                params={
                    "query": query,
                    "max_results": min(self.config.max_results_per_poll, 100),
                    "tweet.fields": "created_at,author_id,public_metrics,entities",
                    "expansions": "author_id",
                    "user.fields": "name,username,verified,public_metrics",
                },
            )
            response.raise_for_status()
            data = response.json()

            return self._parse_tweets(data)

        except httpx.HTTPStatusError as e:
            if e.response.status_code != 429:  # Don't log rate limits
                print(f"Twitter search error: {e}")
            return []

    async def _check_accounts(self) -> List[NewsEvent]:
        """Check specific accounts for new tweets."""
        if not self._client:
            return []

        events = []

        # Batch accounts to reduce API calls
        for account in self.config.accounts[:10]:  # Limit to avoid rate limits
            try:
                # Get user ID first (could cache this)
                user_response = await self._client.get(
                    f"/users/by/username/{account}",
                )
                if user_response.status_code != 200:
                    continue

                user_data = user_response.json()
                user_id = user_data.get("data", {}).get("id")
                if not user_id:
                    continue

                # Get recent tweets
                params = {
                    "max_results": 5,
                    "tweet.fields": "created_at,public_metrics,entities",
                    "exclude": "retweets,replies",
                }

                # Only get tweets newer than last seen
                if account in self._last_tweet_ids:
                    params["since_id"] = self._last_tweet_ids[account]

                tweets_response = await self._client.get(
                    f"/users/{user_id}/tweets",
                    params=params,
                )

                if tweets_response.status_code == 200:
                    tweets_data = tweets_response.json()
                    tweets = tweets_data.get("data", [])

                    if tweets:
                        # Update last seen
                        self._last_tweet_ids[account] = tweets[0]["id"]

                        for tweet in tweets:
                            event = self._tweet_to_event(tweet, account)
                            events.append(event)

            except Exception as e:
                print(f"Error checking account @{account}: {e}")

            # Small delay between account checks
            await asyncio.sleep(0.5)

        return events

    def _parse_tweets(self, data: dict) -> List[NewsEvent]:
        """Parse Twitter API response into NewsEvents."""
        events = []
        tweets = data.get("data", [])
        users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}

        for tweet in tweets:
            author_id = tweet.get("author_id", "")
            author = users.get(author_id, {})

            # Filter by follower count
            followers = author.get("public_metrics", {}).get("followers_count", 0)
            if followers < self.config.min_followers:
                continue

            event = self._tweet_to_event(
                tweet,
                author.get("username", "unknown"),
                author.get("verified", False),
            )
            events.append(event)

        return events

    def _tweet_to_event(
        self,
        tweet: dict,
        username: str,
        verified: bool = False,
    ) -> NewsEvent:
        """Convert a tweet to a NewsEvent."""
        tweet_id = tweet.get("id", "")
        text = tweet.get("text", "")

        # Parse timestamp
        created_at = tweet.get("created_at", "")
        try:
            timestamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except:
            timestamp = datetime.utcnow()

        # Extract entities
        entities = []
        tweet_entities = tweet.get("entities", {})

        # Hashtags
        for ht in tweet_entities.get("hashtags", []):
            entities.append(f"#{ht.get('tag', '')}")

        # Mentions
        for mention in tweet_entities.get("mentions", []):
            entities.append(f"@{mention.get('username', '')}")

        # Engagement
        metrics = tweet.get("public_metrics", {})
        engagement = (
            metrics.get("like_count", 0) +
            metrics.get("retweet_count", 0) * 2 +  # Weight RTs higher
            metrics.get("reply_count", 0)
        )

        # Generate unique ID
        event_id = hashlib.sha256(f"twitter:{tweet_id}".encode()).hexdigest()[:16]

        return NewsEvent(
            id=event_id,
            headline=text,
            body=None,
            source="twitter",
            source_id=tweet_id,
            url=f"https://twitter.com/{username}/status/{tweet_id}",
            timestamp=timestamp,
            categories=[NewsCategory.POLITICS, NewsCategory.WORLD_EVENTS],
            entities=entities,
            keywords=self._extract_keywords(text),
            author=f"@{username}",
            is_verified=verified,
            engagement=engagement,
        )

    def _extract_keywords(self, text: str) -> List[str]:
        """Extract relevant keywords from tweet text."""
        text_lower = text.lower()
        found = []

        for keyword in self.config.keywords:
            if keyword.lower() in text_lower:
                found.append(keyword)

        return found[:10]  # Limit


class MockTwitterSource(NewsSource):
    """Mock Twitter source for testing without API access."""

    def __init__(self):
        super().__init__(name="mock_twitter", categories=[NewsCategory.POLITICS])
        self._events: List[NewsEvent] = []

    async def connect(self) -> None:
        self._running = True

    async def disconnect(self) -> None:
        self._running = False

    async def stream(self) -> AsyncIterator[NewsEvent]:
        while self._running:
            if self._events:
                event = self._events.pop(0)
                yield event
            await asyncio.sleep(0.1)

    def inject_event(self, headline: str, **kwargs) -> NewsEvent:
        """Inject a test event."""
        event = NewsEvent(
            id=hashlib.sha256(headline.encode()).hexdigest()[:16],
            headline=headline,
            source="mock_twitter",
            timestamp=datetime.utcnow(),
            categories=[NewsCategory.POLITICS],
            **kwargs,
        )
        self._events.append(event)
        return event
