"""Enhanced strategy engine — combines multiple signals with news + social sentiment."""

import logging
import math
from typing import Optional
from datetime import datetime, timezone, timedelta

from bot.feeds.news import NewsFeed
from bot.feeds.twitter import SocialFeed
from bot.feeds.ai_signal import AISignalFeed
from bot.feeds.live_data import LiveFeedAggregator
from bot.strategies.signal_validator import SignalAuditLog, SignalValidator

logger = logging.getLogger(__name__)


class EnhancedStrategyEngine:
    """
    Multi-signal strategy engine for prediction markets.

    Signals:
    1. Price mispricing (market price vs model probability)
    2. Live data (weather forecasts, crypto prices, forex rates)
    3. News sentiment (reactive trading on breaking news)
    4. Social media sentiment (Twitter/X via web search)
    5. Volume analysis (unusual volume = informed trading)
    6. Time decay (markets resolving soon have clearer signals)
    """

    def __init__(self, config: dict = None):
        config = config or {}
        self.min_edge = config.get("min_edge", 0.05)
        self.min_confidence = config.get("min_confidence", 0.50)
        self.max_position_pct = config.get("max_position_pct", 0.10)
        self.news_weight = config.get("news_weight", 0.20)
        self.social_weight = config.get("social_weight", 0.15)
        self.enable_news = config.get("enable_news", True)
        self.enable_social = config.get("enable_social", True)

        if self.enable_news:
            self.news = NewsFeed()
        if self.enable_social:
            self.social = SocialFeed(config)

        self.enable_ai = config.get("enable_ai", True)
        self.ai_weight = config.get("ai_weight", 0.20)
        if self.enable_ai:
            self.ai_feed = AISignalFeed(config)

        # Live data feeds (weather, crypto, forex)
        self.live_feeds = LiveFeedAggregator()
        self.validator = SignalValidator()
        self.signal_audit = SignalAuditLog()

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

        # 3. Social media signal
        if self.enable_social:
            social_signal = self._social_signal(market)
            if social_signal:
                signals["social"] = social_signal
                weights["social"] = self.social_weight

        # 3.5 Live data signal (weather forecasts, crypto prices, forex)
        live_signal = self._live_data_signal(market)
        if live_signal:
            signals["live"] = live_signal
            weights["live"] = 0.50  # Heavy weight — real data beats generic signals

        # 4. Volume signal
        volume_signal = self._volume_signal(market)
        if volume_signal:
            signals["volume"] = volume_signal
            weights["volume"] = 0.15

        # 5. Time decay signal
        time_signal = self._time_signal(market)
        if time_signal:
            signals["time"] = time_signal
            weights["time"] = 0.10

        # 6. AI signal (Ghost's analysis)
        if self.enable_ai:
            ai_signal = self._ai_signal(market)
            if ai_signal:
                signals["ai"] = ai_signal
                weights["ai"] = self.ai_weight

        if not signals:
            return None

        validation_results = self.validator.validate_all(signals, market)
        raw_predictions = {
            name: round(float(signal.get("predicted_prob", 0.5) or 0.5), 4)
            for name, signal in signals.items()
        }
        validated_signals = {}
        validated_weights = {}

        for name, sig in signals.items():
            validation = validation_results[name]
            self.signal_audit.write(market, name, sig, raw_predictions, validation)
            if validation.accepted:
                adjusted = dict(sig)
                adjusted["predicted_prob"] = validation.adjusted_prob
                adjusted["confidence"] = validation.adjusted_confidence
                if validation.warnings:
                    adjusted.setdefault("warnings", []).extend(validation.warnings)
                    for warning in validation.warnings:
                        logger.warning(f"Signal warning [{name}]: {warning}")
                validated_signals[name] = adjusted
                validated_weights[name] = weights[name]
            else:
                logger.warning(f"Signal REJECTED [{name}]: {validation.rejection_reason}")

        if not validated_signals:
            return None

        # Weighted ensemble
        total_weight = sum(validated_weights.values())
        if total_weight == 0:
            return None

        weighted_prob = sum(
            s["predicted_prob"] * validated_weights[k]
            for k, s in validated_signals.items()
        ) / total_weight

        weighted_confidence = sum(
            s["confidence"] * validated_weights[k]
            for k, s in validated_signals.items()
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
            "no_market_price": market.no_price,
            "edge": round(edge, 4),
            "confidence": round(weighted_confidence, 4),
            "signals": {k: s["predicted_prob"] for k, s in validated_signals.items()},
            "question": market.question,
        }

    def _price_signal(self, market, order_book: dict = None) -> Optional[dict]:
        """Detect mispricing using market microstructure + known biases."""
        yes_price = market.yes_price
        if yes_price <= 0 or yes_price >= 1:
            return None

        predicted = yes_price
        confidence = 0.3

        # === Bias 1: Longshot bias ===
        # Markets < $0.10 tend to be OVERPRICED (people overbet longshots)
        # Markets > $0.90 tend to be UNDERPRICED (people underbet near-certainties)
        if yes_price < 0.10:
            predicted -= 0.03  # Longshot likely overpriced → short it
            confidence = 0.55
        elif yes_price > 0.90:
            predicted += 0.02  # Near-certainty likely underpriced → buy it
            confidence = 0.60
        elif 0.40 < yes_price < 0.60:
            # Near coin-flip markets are most efficient
            confidence = 0.35

        # === Bias 2: Volume efficiency ===
        volume = market.volume
        if volume > 50000:
            # High volume = more efficient pricing
            confidence += 0.1
        elif volume < 1000:
            # Low volume = less efficient = more potential edge
            confidence -= 0.1
            predicted += 0.01 if yes_price < 0.5 else -0.01

        # === Bias 3: Spread-based confidence ===
        if order_book:
            spread_pct = order_book.get("spread_pct", 10)
            if spread_pct < 3:
                confidence += 0.1  # Tight spread = confident market
            elif spread_pct > 10:
                confidence -= 0.1  # Wide spread = uncertain

        # === Bias 4: Category-based signals ===
        category = getattr(market, 'category', '').lower()
        if 'sports' in category:
            # Sports markets are less efficient (emotional betting)
            confidence -= 0.05
        elif 'politics' in category or 'election' in category:
            # Political markets have polling data → more predictable
            confidence += 0.05

        return {
            "signal_type": "price",
            "predicted_prob": max(0.01, min(0.99, predicted)),
            "confidence": max(0.1, min(0.95, confidence)),
        }

    def _news_signal(self, market) -> Optional[dict]:
        """Analyze news sentiment for the market."""
        try:
            news_items = self.news.get_news_for_market(market.question)

            if not news_items:
                return None

            # Average sentiment weighted by relevance
            total_weight = sum(n.relevance * getattr(n, "recency_weight", 1.0) for n in news_items)
            if total_weight == 0:
                return None

            avg_sentiment = sum(
                n.sentiment * n.relevance * getattr(n, "recency_weight", 1.0)
                for n in news_items
            ) / total_weight

            quality = self.news.assess_signal_quality(news_items)

            predicted = market.yes_price + avg_sentiment * 0.15

            confidence = min(len(news_items) / 5, 1.0) * 0.8
            confidence = max(0.01, confidence - quality["confidence_penalty"])
            latest_published = max((n.published for n in news_items), default=datetime.now(timezone.utc))

            return {
                "signal_type": "news",
                "predicted_prob": max(0.01, min(0.99, predicted)),
                "confidence": confidence,
                "source_timestamp": latest_published.isoformat(),
                "ttl_seconds": 86400,
                "data": {
                    "sources": [n.source for n in news_items],
                    "source_count": len({n.source for n in news_items}),
                    "quality_warnings": quality["warnings"],
                },
                "warnings": quality["warnings"],
            }
        except Exception as e:
            logger.debug(f"News signal error: {e}")
            return None

    def _live_data_signal(self, market) -> Optional[dict]:
        """Get market-specific live data signal (weather, crypto, forex)."""
        try:
            result = self.live_feeds.get_signal(
                market.question,
                market.yes_price,
                getattr(market, 'category', '')
            )
            if result:
                return result
            return None
        except Exception as e:
            logger.debug(f"Live data signal error: {e}")
            return None

    def _social_signal(self, market) -> Optional[dict]:
        """Analyze social media sentiment for the market."""
        try:
            signal = self.social.get_market_sentiment(market.question)
            if not signal or signal.mention_count == 0:
                return None

            # Convert social sentiment to probability shift
            predicted = market.yes_price + signal.predicted_prob_adjustment

            return {
                "signal_type": "social",
                "predicted_prob": max(0.01, min(0.99, predicted)),
                "confidence": signal.confidence,
                "source_timestamp": signal.timestamp,
                "ttl_seconds": self.social.cache_ttl,
                "data": {
                    "warnings": list(signal.warnings),
                    "manipulation_flag": signal.manipulation_flag,
                    "confidence_cap": signal.confidence_cap,
                },
                "warnings": list(signal.warnings),
            }
        except Exception as e:
            logger.debug(f"Social signal error: {e}")
            return None

    def _ai_signal(self, market) -> Optional[dict]:
        """Get AI signal from Ghost's analysis."""
        try:
            signal = self.ai_feed.get_signal(market.id)
            return signal
        except Exception as e:
            logger.debug(f"AI signal error: {e}")
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
            "signal_type": "volume",
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
            "signal_type": "time",
            "predicted_prob": market.yes_price,
            "confidence": confidence,
        }


class KellySizer:
    """
    Kelly Criterion position sizing with automatic mode-aware defaults.

    Optimal bet size = (p * b_net - q) / b_net
    where:
        p     = probability of winning
        q     = 1 - p
        b_net = net odds after Kalshi fee = (1 - market_price) / market_price * (1 - fee_rate)

    The fee is deducted from expected value before computing the fraction so
    that Kelly sizing is never over-aggressive on low-edge trades.

    Mode-aware:
    - Paper (KALSHI_USE_DEMO=true): Half-Kelly, 10% max bet
    - Live (KALSHI_USE_DEMO=false): Quarter-Kelly, 5% max bet

    Fee rate configurable via KALSHI_FEE_RATE env var (default 0.07 = 7%).
    """

    # Presets by mode
    PAPER = {"fraction": 0.50, "max_bet_pct": 0.10}
    LIVE = {"fraction": 0.25, "max_bet_pct": 0.05}

    DEFAULT_FEE_RATE = 0.07  # 7% on winnings — Kalshi standard

    def __init__(self, fraction: float = None, max_bet_pct: float = None,
                 kelly_fraction: float = None, fee_rate: float = None):
        import os
        is_live = os.getenv("KALSHI_USE_DEMO", "true").lower() == "false"
        preset = self.LIVE if is_live else self.PAPER

        # Explicit params override preset
        self.fraction = (
            kelly_fraction if kelly_fraction is not None
            else (fraction if fraction is not None else preset["fraction"])
        )
        self.max_bet_pct = max_bet_pct if max_bet_pct is not None else preset["max_bet_pct"]

        # Fee rate: env var → explicit param → default
        env_fee = os.getenv("KALSHI_FEE_RATE")
        if fee_rate is not None:
            self.fee_rate = float(fee_rate)
        elif env_fee is not None:
            self.fee_rate = float(env_fee)
        else:
            self.fee_rate = self.DEFAULT_FEE_RATE

    def calculate(self, model_prob: float, market_price: float,
                  bankroll: float) -> float:
        """Calculate optimal bet size in dollars, accounting for Kalshi fees."""
        if market_price <= 0 or market_price >= 1:
            return 0

        p = model_prob
        q = 1 - p

        # Net odds after fee: winning a contract pays (1 - market_price) per dollar staked,
        # but Kalshi takes fee_rate of that gross profit.
        gross_odds = (1 - market_price) / market_price  # decimal odds pre-fee
        b_net = gross_odds * (1 - self.fee_rate)        # net odds after fee

        if b_net <= 0:
            return 0

        # Kelly formula using fee-adjusted odds
        kelly = (p * (b_net + 1) - 1) / b_net

        if kelly <= 0:
            return 0  # No bet (negative expected value after fees)

        # Apply fractional Kelly
        size = kelly * self.fraction * bankroll

        # Cap at max bet percentage (of current bankroll)
        max_size = bankroll * self.max_bet_pct
        size = min(size, max_size)

        # Minimum $1
        return max(1.0, round(size, 2))
