"""News feed aggregator — multi-source RSS for trading signals.

Sources (all free, no API key required):
- Yahoo Finance Search RSS  — keyword-specific news (best for market queries)
- Reuters Top News RSS      — general breaking news
- BBC News RSS              — international perspective
- ESPN RSS                  — sports markets
- CoinDesk RSS              — crypto markets
- NPR News RSS              — US politics / policy markets

Each source has a per-session circuit breaker: after 3 consecutive failures it
is skipped for 15 minutes, then retried automatically.
"""

import httpx
import logging
import re
import xml.etree.ElementTree as ET
from typing import Optional
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)


@dataclass
class NewsItem:
    title: str
    source: str
    url: str
    published: datetime
    summary: str
    relevance: float  # 0-1
    sentiment: float  # -1 (bearish) to +1 (bullish)
    recency_weight: float = 1.0


# ── Feed definitions ──────────────────────────────────────────────────────────

RSS_FEEDS = {
    "reuters":   "https://feeds.reuters.com/reuters/topNews",
    "bbc":       "http://feeds.bbci.co.uk/news/rss.xml",
    "espn":      "https://www.espn.com/espn/rss/news",
    "coindesk":  "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "npr":       "https://feeds.npr.org/1001/rss.xml",
    "ap":        "https://rsshub.app/apnews/topics/apf-topnews",
}

# Topic routing — which feeds to try for a given market category
TOPIC_FEEDS = {
    "sports":   ["espn", "reuters"],
    "crypto":   ["coindesk", "reuters"],
    "politics": ["npr", "reuters", "bbc"],
    "finance":  ["reuters", "bbc"],
    "default":  ["reuters", "bbc", "npr"],
}


class NewsFeed:
    """Aggregates news from multiple RSS sources for sentiment analysis."""

    def __init__(self):
        self.http = httpx.Client(
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (compatible; PredictionBot/1.0)"},
            follow_redirects=True,
        )
        self.cache: dict = {}          # query → (items, fetched_at)
        self.cache_ttl = 300           # 5 min

        # Per-feed circuit breakers: feed_name → (fail_count, disabled_until)
        self._breakers: dict[str, tuple[int, Optional[datetime]]] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def get_news_for_market(self, market_question: str,
                             keywords: list = None) -> list[NewsItem]:
        """
        Return scored, relevance-filtered news items for a market question.
        Tries Yahoo Finance search RSS first (keyword-specific), then
        falls back to topic-routed static feeds.
        """
        cache_key = market_question[:80]
        if cache_key in self.cache:
            items, fetched_at = self.cache[cache_key]
            if (datetime.now(timezone.utc) - fetched_at).total_seconds() < self.cache_ttl:
                return items

        search_terms = self._extract_keywords(market_question)
        if keywords:
            search_terms.extend(keywords)

        articles: list[NewsItem] = []

        # 1. Yahoo Finance search RSS — most relevant for specific queries
        if search_terms:
            articles.extend(self._fetch_yahoo_search(search_terms))

        # 2. Topic-routed static feeds as fallback / supplement
        topic = self._detect_topic(market_question)
        feed_names = TOPIC_FEEDS.get(topic, TOPIC_FEEDS["default"])
        for feed_name in feed_names:
            if len(articles) >= 20:
                break
            articles.extend(self._fetch_feed(feed_name))

        # Score, filter, deduplicate
        seen_urls: set = set()
        scored: list[NewsItem] = []
        for article in articles:
            if article.url in seen_urls:
                continue
            seen_urls.add(article.url)

            relevance = self._score_relevance(article.title, market_question)
            if relevance < 0.25:
                continue

            article.relevance = relevance
            article.sentiment = self._simple_sentiment(article.title + " " + article.summary)
            article.recency_weight = self._recency_weight(article.published)
            scored.append(article)

        scored.sort(key=lambda a: a.relevance * a.recency_weight, reverse=True)
        result = scored[:10]

        self.cache[cache_key] = (result, datetime.now(timezone.utc))
        return result

    def assess_signal_quality(self, items: list[NewsItem]) -> dict:
        """Return confidence penalties and warnings for news signal quality."""
        warnings = []
        confidence_penalty = 0.0

        sources = {item.source for item in items if item.source}
        if items and len(sources) == 1:
            confidence_penalty += 0.10
            warnings.append("All news items came from a single source")

        return {
            "confidence_penalty": confidence_penalty,
            "warnings": warnings,
        }

    # ── Fetchers ──────────────────────────────────────────────────────────────

    def _fetch_yahoo_search(self, keywords: list[str]) -> list[NewsItem]:
        """Fetch Yahoo Finance News search RSS for specific keywords."""
        query = quote_plus(" ".join(keywords[:4]))
        url = f"https://news.search.yahoo.com/search?p={query}&output=rss"
        return self._fetch_rss(url, source_name="yahoo")

    def _fetch_feed(self, feed_name: str) -> list[NewsItem]:
        """Fetch a named static RSS feed, respecting its circuit breaker."""
        if self._is_broken(feed_name):
            return []
        url = RSS_FEEDS.get(feed_name)
        if not url:
            return []
        items = self._fetch_rss(url, source_name=feed_name)
        if items:
            self._reset_breaker(feed_name)
        return items

    def _fetch_rss(self, url: str, source_name: str) -> list[NewsItem]:
        """Fetch and parse an RSS/Atom feed URL. Returns NewsItems."""
        try:
            resp = self.http.get(url)
            if resp.status_code != 200:
                self._record_failure(source_name)
                logger.debug(f"RSS {source_name}: HTTP {resp.status_code}")
                return []

            return self._parse_rss(resp.text, source_name)

        except httpx.TimeoutException:
            self._record_failure(source_name)
            logger.debug(f"RSS {source_name}: timeout")
            return []
        except Exception as e:
            self._record_failure(source_name)
            logger.debug(f"RSS {source_name}: {e}")
            return []

    def _parse_rss(self, xml_text: str, source_name: str) -> list[NewsItem]:
        """Parse RSS 2.0 or Atom XML into NewsItem list."""
        items: list[NewsItem] = []
        try:
            # Strip namespace prefixes that confuse ElementTree
            xml_clean = re.sub(r' xmlns[^"]*"[^"]*"', '', xml_text)
            xml_clean = re.sub(r'<[a-zA-Z]+:', '<', xml_clean)
            xml_clean = re.sub(r'</[a-zA-Z]+:', '</', xml_clean)

            root = ET.fromstring(xml_clean)
        except ET.ParseError as e:
            logger.debug(f"RSS parse error ({source_name}): {e}")
            return []

        # RSS 2.0: <channel><item>...</item></channel>
        # Atom: <feed><entry>...</entry></feed>
        entries = root.findall(".//item") or root.findall(".//entry")

        for entry in entries[:15]:  # Cap per feed
            title = self._xml_text(entry, ["title"]) or ""
            url   = (self._xml_text(entry, ["link"])
                     or self._xml_attr(entry, "link", "href") or "")
            pub   = (self._xml_text(entry, ["pubDate", "published", "updated"]) or "")
            desc  = (self._xml_text(entry, ["description", "summary", "content"]) or "")

            if not title:
                continue

            # Clean HTML tags from description
            desc_clean = re.sub(r'<[^>]+>', ' ', desc).strip()
            desc_clean = re.sub(r'\s+', ' ', desc_clean)[:300]

            items.append(NewsItem(
                title=title.strip(),
                source=source_name,
                url=url.strip(),
                published=self._parse_date(pub),
                summary=desc_clean,
                relevance=0.0,   # Filled in by caller
                sentiment=0.0,   # Filled in by caller
            ))

        return items

    # ── Circuit breakers ──────────────────────────────────────────────────────

    def _is_broken(self, feed_name: str) -> bool:
        fail_count, disabled_until = self._breakers.get(feed_name, (0, None))
        if disabled_until and datetime.now(timezone.utc) < disabled_until:
            return True
        return False

    def _record_failure(self, feed_name: str):
        fail_count, _ = self._breakers.get(feed_name, (0, None))
        fail_count += 1
        if fail_count >= 3:
            disabled_until = datetime.now(timezone.utc) + timedelta(minutes=15)
            self._breakers[feed_name] = (fail_count, disabled_until)
            logger.warning(f"News feed '{feed_name}' disabled for 15 min after {fail_count} failures")
        else:
            self._breakers[feed_name] = (fail_count, None)

    def _reset_breaker(self, feed_name: str):
        self._breakers[feed_name] = (0, None)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _detect_topic(self, question: str) -> str:
        """Route a market question to the most relevant feed group."""
        q = question.lower()
        if any(w in q for w in ["bitcoin", "btc", "ethereum", "eth", "crypto",
                                  "solana", "shiba", "coin", "blockchain"]):
            return "crypto"
        if any(w in q for w in ["nfl", "nba", "mlb", "nhl", "soccer", "football",
                                  "basketball", "baseball", "hockey", "sport",
                                  "player", "team", "game", "match", "score"]):
            return "sports"
        if any(w in q for w in ["president", "congress", "senate", "election",
                                  "vote", "republican", "democrat", "policy",
                                  "legislation", "bill", "governor"]):
            return "politics"
        if any(w in q for w in ["fed", "interest rate", "inflation", "gdp",
                                  "market", "stock", "nasdaq", "s&p", "dow"]):
            return "finance"
        return "default"

    def _extract_keywords(self, question: str) -> list[str]:
        """Extract meaningful keywords from a market question."""
        stop_words = {
            "will", "be", "the", "a", "an", "is", "are", "was", "were",
            "have", "has", "had", "do", "does", "did", "in", "on", "at",
            "to", "for", "of", "with", "by", "from", "this", "that",
            "than", "over", "under", "above", "below", "before", "after",
            "more", "less", "higher", "lower", "between", "during",
        }
        words = re.findall(r'\b[a-zA-Z]{3,}\b', question.lower())
        return [w for w in words if w not in stop_words][:5]

    def _score_relevance(self, title: str, question: str) -> float:
        """Word-overlap relevance score between a title and a market question."""
        title_words  = set(re.findall(r'\b[a-zA-Z]{3,}\b', title.lower()))
        quest_words  = set(re.findall(r'\b[a-zA-Z]{3,}\b', question.lower()))
        if not quest_words:
            return 0.0
        overlap = len(quest_words & title_words)
        return min(overlap / len(quest_words) * 2, 1.0)

    def _simple_sentiment(self, text: str) -> float:
        """Keyword-based sentiment. Returns -1 to 1."""
        positive = {
            "surge", "soar", "rise", "gain", "bull", "rally", "breakthrough",
            "success", "record", "high", "boom", "growth", "positive",
            "beat", "exceed", "strong", "win", "approval", "agree", "deal",
            "up", "increase", "jump", "climb", "recover", "rebound",
        }
        negative = {
            "crash", "fall", "drop", "bear", "decline", "loss", "fail",
            "low", "risk", "threat", "warn", "crisis", "negative",
            "miss", "weak", "lose", "reject", "delay", "ban", "block",
            "down", "decrease", "slump", "plunge", "concern", "fears",
        }
        words = set(text.lower().split())
        pos = len(words & positive)
        neg = len(words & negative)
        total = pos + neg
        if total == 0:
            return 0.0
        return (pos - neg) / total

    def _recency_weight(self, published: datetime) -> float:
        """Progressively discount news older than 24 hours."""
        pub_dt = published
        if pub_dt.tzinfo is None:
            pub_dt = pub_dt.replace(tzinfo=timezone.utc)

        age_hours = max((datetime.now(timezone.utc) - pub_dt).total_seconds() / 3600, 0)
        if age_hours <= 24:
            return 1.0

        extra_age = age_hours - 24
        return max(0.20, 1.0 - extra_age / 72)

    def _xml_text(self, element, tags: list[str]) -> Optional[str]:
        """Find the first matching child tag and return its text."""
        for tag in tags:
            child = element.find(tag)
            if child is not None and child.text:
                return child.text.strip()
        return None

    def _xml_attr(self, element, tag: str, attr: str) -> Optional[str]:
        """Find a child tag and return an attribute value."""
        child = element.find(tag)
        if child is not None:
            return child.get(attr)
        return None

    def _parse_date(self, date_str: str) -> datetime:
        """Parse RFC 2822 or ISO 8601 date strings."""
        if not date_str:
            return datetime.now(timezone.utc)
        try:
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(date_str)
        except Exception:
            pass
        try:
            return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except Exception:
            return datetime.now(timezone.utc)

    def close(self):
        self.http.close()
