"""Social media sentiment feed for prediction markets.

Uses web search to find tweets and social media discussions about markets.
Integrates with Google News RSS for broader social signal coverage.
"""

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Keywords that indicate bullish/bearish sentiment
BULLISH_KEYWORDS = [
    "likely", "probably", "sure thing", "easy win", "free money",
    "lock", "guaranteed", "confident", "bet big", "loading up",
    "undervalued", "mispriced", "edge", "buying yes", "going yes",
    "bullish", "pumping", "moon", "calls", "long",
]

BEARISH_KEYWORDS = [
    "unlikely", "no chance", "overpriced", "fading", "selling",
    "avoid", "trap", "scam", "rigged", "buying no", "going no",
    "dump", "crash", "dead", "impossible", "never happening",
    "bearish", "puts", "short", "cap", "copium",
]


@dataclass
class SocialSignal:
    """Aggregated social media sentiment for a market."""
    query: str
    mention_count: int
    avg_sentiment: float
    bullish_count: int
    bearish_count: int
    neutral_count: int
    sources: list
    timestamp: str = ""

    @property
    def predicted_prob_adjustment(self) -> float:
        """Convert sentiment to probability adjustment (-0.1 to +0.1)."""
        return self.avg_sentiment * 0.1

    @property
    def confidence(self) -> float:
        """Confidence based on sample size."""
        if self.mention_count >= 20:
            return 0.7
        elif self.mention_count >= 10:
            return 0.6
        elif self.mention_count >= 5:
            return 0.5
        elif self.mention_count >= 2:
            return 0.4
        return 0.3


class SocialFeed:
    """Social media sentiment analysis for markets via web search."""

    def __init__(self, config: dict = None):
        config = config or {}
        self.enabled = config.get("enable_social", True)
        self.cache_ttl = config.get("social_cache_ttl", 600)  # 10 minutes
        self._cache: dict[str, tuple[float, SocialSignal]] = {}

        self.client = httpx.Client(
            timeout=10,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            },
            follow_redirects=True,
        )

    def analyze_text(self, text: str) -> float:
        """Analyze sentiment of text. Returns -1 to +1."""
        text_lower = text.lower()
        bull = sum(1 for kw in BULLISH_KEYWORDS if kw in text_lower)
        bear = sum(1 for kw in BEARISH_KEYWORDS if kw in text_lower)
        total = bull + bear
        if total == 0:
            return 0.0
        return (bull - bear) / total

    def get_market_sentiment(self, market_question: str, keywords: list[str] = None) -> Optional[SocialSignal]:
        """Get social media sentiment for a market via Google News RSS."""
        if not self.enabled:
            return None

        # Check cache
        cache_key = market_question.lower()
        if cache_key in self._cache:
            cached_time, cached = self._cache[cache_key]
            if time.time() - cached_time < self.cache_ttl:
                return cached

        # Build search query
        clean = re.sub(r'\b(will|the|be|a|an|before|after|by|in|on|at|of)\b', ' ', market_question.lower())
        clean = re.sub(r'\s+', ' ', clean).strip()
        words = clean.split()[:4]
        query = " ".join(words)

        # Search Google News RSS for social/media mentions
        mentions = []
        sentiments = []

        try:
            search_url = "https://news.google.com/rss/search"
            params = {
                "q": f"{query} betting odds prediction",
                "hl": "en-US",
                "gl": "US",
                "ceid": "US:en",
            }
            resp = self.client.get(search_url, params=params)
            resp.raise_for_status()

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "xml")

            for item in soup.find_all("item")[:20]:
                title = item.title.text if item.title else ""
                description = item.description.text if item.description else ""
                source = item.source.text if item.source else ""
                pub_date = item.pubDate.text if item.pubDate else ""

                text = f"{title} {description}"
                sentiment = self.analyze_text(text)

                if sentiment != 0:
                    mentions.append({
                        "title": title[:80],
                        "source": source,
                        "sentiment": round(sentiment, 2),
                        "date": pub_date[:16],
                    })
                    sentiments.append(sentiment)

        except Exception as e:
            logger.debug(f"Social feed search failed: {e}")
            return None

        if not sentiments:
            return None

        avg = sum(sentiments) / len(sentiments)
        bullish = sum(1 for s in sentiments if s > 0.2)
        bearish = sum(1 for s in sentiments if s < -0.2)
        neutral = len(sentiments) - bullish - bearish

        signal = SocialSignal(
            query=query,
            mention_count=len(mentions),
            avg_sentiment=round(avg, 3),
            bullish_count=bullish,
            bearish_count=bearish,
            neutral_count=neutral,
            sources=mentions[:5],
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        # Cache result
        self._cache[cache_key] = (time.time(), signal)

        logger.info(f"Social sentiment '{query}': {len(mentions)} mentions, sentiment={avg:.3f}")
        return signal

    def close(self):
        """Close the HTTP client."""
        self.client.close()


# Quick test
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    feed = TwitterFeed()

    # Test with a sample market
    signal = feed.get_market_sentiment("Who will be the next Pope?")
    if signal:
        print(f"Query: {signal.query}")
        print(f"Tweets: {signal.tweet_count}")
        print(f"Sentiment: {signal.avg_sentiment} (weighted: {signal.weighted_sentiment})")
        print(f"Bull/Bear/Neutral: {signal.bullish_count}/{signal.bearish_count}/{signal.neutral_count}")
        print(f"Prob adjustment: {signal.predicted_prob_adjustment:+.1%}")
        print(f"Top tweets:")
        for t in signal.top_tweets[:3]:
            print(f"  @{t['author']}: {t['text'][:60]}...")
    else:
        print("No tweets found")
    feed.close()
