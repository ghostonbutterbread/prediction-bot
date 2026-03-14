"""News feed aggregator — scrapes and analyzes news for trading signals."""

import httpx
import logging
import re
from typing import Optional
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class NewsItem:
    title: str
    source: str
    url: str
    published: datetime
    summary: str
    relevance: float  # 0-1, how relevant to prediction markets
    sentiment: float  # -1 (bearish) to 1 (bullish)


class NewsFeed:
    """Aggregates news from multiple sources for sentiment analysis."""

    def __init__(self):
        self.http = httpx.Client(timeout=15)
        self.cache = {}
        self.cache_ttl = 300  # 5 min cache

    def get_news_for_market(self, market_question: str, keywords: list = None) -> list[NewsItem]:
        """Get relevant news for a specific market question."""
        search_terms = self._extract_keywords(market_question)
        if keywords:
            search_terms.extend(keywords)

        articles = []

        # Google News RSS (free, no API key)
        articles.extend(self._fetch_google_news(search_terms))

        # Filter and score relevance
        scored = []
        for article in articles:
            relevance = self._score_relevance(article.title, market_question)
            if relevance > 0.3:
                article.relevance = relevance
                article.sentiment = self._simple_sentiment(article.title)
                scored.append(article)

        scored.sort(key=lambda a: a.relevance, reverse=True)
        return scored[:10]

    def _extract_keywords(self, question: str) -> list[str]:
        """Extract searchable keywords from a market question."""
        # Remove common prediction market words
        stop_words = {
            "will", "be", "the", "a", "an", "is", "are", "was", "were",
            "have", "has", "had", "do", "does", "did", "in", "on", "at",
            "to", "for", "of", "with", "by", "from", "this", "that",
            "than", "over", "under", "above", "below", "before", "after",
            "more", "less", "higher", "lower", "above", "below",
        }

        words = re.findall(r'\b[a-zA-Z]{3,}\b', question.lower())
        keywords = [w for w in words if w not in stop_words]

        return keywords[:5]  # Top 5 keywords

    def _fetch_google_news(self, keywords: list[str]) -> list[NewsItem]:
        """Fetch from Google News RSS."""
        query = "+".join(keywords[:3])
        url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

        try:
            resp = self.http.get(url)
            resp.raise_for_status()

            # Simple XML parsing
            items = []
            entries = re.findall(r'<item>(.*?)</item>', resp.text, re.DOTALL)

            for entry in entries[:20]:
                title = self._xml_extract(entry, 'title')
                link = self._xml_extract(entry, 'link')
                pub_date = self._xml_extract(entry, 'pubDate')
                source = self._xml_extract(entry, 'source')

                if title and title != "Google News":
                    items.append(NewsItem(
                        title=title,
                        source=source or "Google News",
                        url=link or "",
                        published=self._parse_date(pub_date),
                        summary="",
                        relevance=0.5,
                        sentiment=0,
                    ))

            return items

        except Exception as e:
            logger.debug(f"Google News fetch error: {e}")
            return []

    def _score_relevance(self, title: str, question: str) -> float:
        """Score how relevant a news title is to a market question."""
        title_lower = title.lower()
        question_lower = question.lower()

        # Count keyword overlap
        question_words = set(re.findall(r'\b[a-zA-Z]{3,}\b', question_lower))
        title_words = set(re.findall(r'\b[a-zA-Z]{3,}\b', title_lower))

        if not question_words:
            return 0

        overlap = len(question_words & title_words)
        return min(overlap / len(question_words) * 2, 1.0)

    def _simple_sentiment(self, title: str) -> float:
        """Simple keyword-based sentiment analysis. Returns -1 to 1."""
        positive = {
            "surge", "soar", "rise", "gain", "bull", "rally", "breakthrough",
            "success", "record", "high", "boom", "growth", "positive", "up",
            "beat", "exceed", "strong", "win", "approval", "agree", "deal",
        }
        negative = {
            "crash", "fall", "drop", "bear", "decline", "loss", "fail",
            "low", "risk", "threat", "warn", "crisis", "negative", "down",
            "miss", "weak", "lose", "reject", "delay", "ban", "block",
        }

        words = set(title.lower().split())
        pos = len(words & positive)
        neg = len(words & negative)
        total = pos + neg

        if total == 0:
            return 0
        return (pos - neg) / total

    def _xml_extract(self, text: str, tag: str) -> Optional[str]:
        """Extract content from XML tag."""
        match = re.search(f'<{tag}[^>]*>(.*?)</{tag}>', text, re.DOTALL)
        return match.group(1).strip() if match else None

    def _parse_date(self, date_str: str) -> datetime:
        """Parse various date formats."""
        if not date_str:
            return datetime.now(timezone.utc)

        try:
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(date_str)
        except Exception:
            return datetime.now(timezone.utc)

    def close(self):
        self.http.close()
