"""Enhanced strategy engine — combines multiple signals with news sentiment."""

import logging
import math
from typing import Optional
from datetime import datetime, timezone, timedelta

from bot.feeds.news import NewsFeed

logger = logging.getLogger(__name__)


class EnhancedStrategyEngine:
    """
    Multi-signal strategy engine for prediction markets.

    Signals:
    1. Price mispricing (market price vs model probability)
    2. News sentiment (reactive trading on breaking news)
    3. Volume analysis (unusual volume = informed trading)
    4. Time decay (markets resolving soon have clearer signals)
    5. Cross-market signals (same question on multiple exchanges)
    """

    def __init__(self, config: dict = None):
        config = config or {}
        self.min_edge = config.get("min_edge", 0.05)
        self.min_confidence = config.get("min_confidence", 0.50)
        self.max_position_pct = config.get("max_position_pct", 0.10)
        self.news_weight = config.get("news_weight", 0.30)
        self.enable_news = config.get("enable_news", True)

        if self.enable_news:
            self.news = NewsFeed()

    def analyze_market(self, market, order_book: dict = None) -> Optional[dict]:
        """
        Full analysis of a market. Returns signal dict or None.

        Combines:
        - Price-based mispricing detection
        - News sentiment analysis
        - Volume signals
        - Time-to-resolution factor
        """
        signals = {}
        weights = {}

        # 1. Price mispricing signal
        price_signal = self._price_signal(market, order_book)
        if price_signal:
            signals["price"] = price_signal
            weights["price"] = 0.40

        # 2. News sentiment signal
        if self.enable_news:
            news_signal = self._news_signal(market)
            if news_signal:
                signals["news"] = news_signal
                weights["news"] = self.news_weight

        # 3. Volume signal
        volume_signal = self._volume_signal(market)
        if volume_signal:
            signals["volume"] = volume_signal
            weights["volume"] = 0.15

        # 4. Time decay signal
        time_signal = self._time_signal(market)
        if time_signal:
            signals["time"] = time_signal
            weights["time"] = 0.15

        if not signals:
            return None

        # Weighted ensemble
        total_weight = sum(weights.values())
        if total_weight == 0:
            return None

        weighted_prob = sum(
            s["predicted_prob"] * weights[k]
            for k, s in signals.items()
        ) / total_weight

        weighted_confidence = sum(
            s["confidence"] * weights[k]
            for k, s in signals.items()
        ) / total_weight

        edge = abs(weighted_prob - market.yes_price)

        if edge < self.min_edge:
            return None
        if weighted_confidence < self.min_confidence:
            return None

        direction = "BUY_YES" if weighted_prob > market.yes_price else "BUY_NO"

        return {
            "market_id": market.id,
            "exchange": market.exchange,
            "direction": direction,
            "model_probability": round(weighted_prob, 4),
            "market_price": market.yes_price,
            "edge": round(edge, 4),
            "confidence": round(weighted_confidence, 4),
            "signals": {k: s["predicted_prob"] for k, s in signals.items()},
            "question": market.question,
        }

    def _price_signal(self, market, order_book: dict = None) -> Optional[dict]:
        """Detect mispricing based on order book analysis."""
        yes_price = market.yes_price
        if yes_price <= 0 or yes_price >= 1:
            return None

        # Use order book imbalance as proxy for true probability
        if order_book:
            yes_bids = order_book.get("yes_bids", [])
            no_bids = order_book.get("no_bids", [])

            yes_depth = sum(qty for _, qty in yes_bids[:5])
            no_depth = sum(qty for _, qty in no_bids[:5])
            total_depth = yes_depth + no_depth

            if total_depth > 0:
                imbalance = (yes_depth - no_depth) / total_depth
                predicted = yes_price + imbalance * 0.10
            else:
                predicted = yes_price
        else:
            predicted = yes_price

        # Confidence based on spread (tighter = more confident)
        spread = order_book.get("spread", 0.1) if order_book else 0.1
        confidence = max(0.1, 1.0 - spread * 10)

        return {
            "predicted_prob": max(0.01, min(0.99, predicted)),
            "confidence": confidence,
        }

    def _news_signal(self, market) -> Optional[dict]:
        """Analyze news sentiment for the market."""
        try:
            news_items = self.news.get_news_for_market(market.question)

            if not news_items:
                return None

            # Average sentiment weighted by relevance
            total_weight = sum(n.relevance for n in news_items)
            if total_weight == 0:
                return None

            avg_sentiment = sum(
                n.sentiment * n.relevance for n in news_items
            ) / total_weight

            # Convert sentiment (-1 to 1) to probability shift
            predicted = market.yes_price + avg_sentiment * 0.15

            # More news = higher confidence
            confidence = min(len(news_items) / 5, 1.0) * 0.8

            return {
                "predicted_prob": max(0.01, min(0.99, predicted)),
                "confidence": confidence,
            }
        except Exception as e:
            logger.debug(f"News signal error: {e}")
            return None

    def _volume_signal(self, market) -> Optional[dict]:
        """Detect unusual volume patterns."""
        volume = market.volume

        if volume < 1000:
            return None  # Too low to analyze

        # High volume markets are more efficient (less mispricing)
        # Low volume with non-zero = potential opportunity
        if volume > 10000:
            confidence = 0.7
        elif volume > 5000:
            confidence = 0.5
        else:
            confidence = 0.3

        # Volume doesn't predict direction, just reliability
        return {
            "predicted_prob": market.yes_price,  # Neutral
            "confidence": confidence,
        }

    def _time_signal(self, market) -> Optional[dict]:
        """Adjust signals based on time to resolution."""
        if not market.closes_at:
            return None

        now = datetime.now(timezone.utc)
        hours_left = (market.closes_at - now).total_seconds() / 3600

        if hours_left < 0:
            return None  # Already closed

        # Markets resolving soon have clearer signals
        # Markets far out have more uncertainty
        if hours_left < 24:
            confidence = 0.8
        elif hours_left < 72:
            confidence = 0.6
        elif hours_left < 168:  # 1 week
            confidence = 0.4
        else:
            confidence = 0.2

        return {
            "predicted_prob": market.yes_price,
            "confidence": confidence,
        }


class KellySizer:
    """
    Kelly Criterion position sizing.

    Optimal bet size = (p * b - q) / b
    where:
        p = probability of winning
        q = 1 - p
        b = odds (payout per $1 bet)

    We use fractional Kelly (0.5x) for safety.
    """

    def __init__(self, fraction: float = 0.5, max_bet_pct: float = 0.10):
        self.fraction = fraction
        self.max_bet_pct = max_bet_pct

    def calculate(self, model_prob: float, market_price: float,
                  bankroll: float) -> float:
        """Calculate optimal bet size in dollars."""
        if market_price <= 0 or market_price >= 1:
            return 0

        p = model_prob
        q = 1 - p
        b = (1 - market_price) / market_price  # Decimal odds

        # Kelly formula
        kelly = (p * (b + 1) - 1) / b if b > 0 else 0

        if kelly <= 0:
            return 0  # No bet (negative expected value)

        # Apply fractional Kelly
        size = kelly * self.fraction * bankroll

        # Cap at max bet percentage
        max_size = bankroll * self.max_bet_pct
        size = min(size, max_size)

        # Minimum $1
        return max(1.0, round(size, 2))
