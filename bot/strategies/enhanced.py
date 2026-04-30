"""Enhanced strategy engine — combines multiple signals with news + social sentiment."""

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from bot.feeds.news import NewsFeed
from bot.feeds.twitter import SocialFeed
from bot.feeds.ai_signal import AISignalFeed
from bot.feeds.live_data import LiveFeedAggregator
from bot.strategies.signal_validator import SignalAuditLog, SignalValidator
from bot.weather import ObservationLog, WeatherMarketCityMapper

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class StrategyTrace:
    """Inspectable strategy decision trace for research/evaluator modes."""

    raw_signals: dict[str, dict] = field(default_factory=dict)
    validation_results: dict[str, dict] = field(default_factory=dict)
    accepted_signals: dict[str, dict] = field(default_factory=dict)
    rejected_signals: dict[str, dict] = field(default_factory=dict)
    ensemble_signal: dict | None = None
    skip_reason_code: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_signals": dict(self.raw_signals),
            "validation_results": dict(self.validation_results),
            "accepted_signals": dict(self.accepted_signals),
            "rejected_signals": dict(self.rejected_signals),
            "ensemble_signal": dict(self.ensemble_signal) if isinstance(self.ensemble_signal, dict) else None,
            "skip_reason_code": self.skip_reason_code,
            "warnings": list(self.warnings),
        }


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
        self.fail_closed_on_news_source_failure = bool(
            config.get("fail_closed_on_news_source_failure", False)
        )
        self._last_news_lookup_sources_failed = False
        self.enable_weather_hidden_gem_safety_guard = bool(
            config.get("enable_weather_hidden_gem_safety_guard", False)
        )
        self.weather_hidden_gem_max_entry_price = self._config_float(
            config.get("weather_hidden_gem_max_entry_price"),
            0.05,
        )
        self.weather_hidden_gem_probability_threshold = self._config_float(
            config.get("weather_hidden_gem_probability_threshold"),
            0.20,
        )
        self.weather_hidden_gem_min_live_confidence = self._config_float(
            config.get("weather_hidden_gem_min_live_confidence"),
            0.60,
        )
        self.weather_hidden_gem_min_source_agreement = self._config_float(
            config.get("weather_hidden_gem_min_source_agreement"),
            0.75,
        )

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
        self.enable_weather_observation_log = bool(config.get("enable_weather_observation_log", False))
        self.weather_market_mapper = None
        self.weather_observation_log = None
        if self.enable_weather_observation_log:
            try:
                self.weather_market_mapper = WeatherMarketCityMapper()
                self.weather_observation_log = ObservationLog(
                    config.get("weather_observation_log_path", "data/weather_observations.jsonl"),
                    identical_cooldown_seconds=int(config.get("weather_observation_cooldown_seconds", 6 * 60 * 60)),
                    max_bytes=int(config.get("weather_observation_log_max_bytes", 10 * 1024 * 1024)),
                )
            except Exception as exc:
                logger.warning("Weather observation logging disabled: %s", exc)
                self.enable_weather_observation_log = False

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

        if self._handle_news_source_failure(signals, weights):
            return None

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
                self._record_weather_observation(market, name, sig)
                adjusted = dict(sig)
                adjusted["predicted_prob"] = validation.adjusted_prob
                adjusted["confidence"] = validation.adjusted_confidence
                if validation.warnings:
                    adjusted.setdefault("warnings", []).extend(validation.warnings)
                validated_signals[name] = adjusted
                validated_weights[name] = weights[name]
            else:
                logger.debug(f"Signal REJECTED [{name}]: {validation.rejection_reason}")

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

        yes_price = market.yes_price
        no_price = getattr(market, "no_price", None)
        no_prob = 1 - weighted_prob
        yes_edge = weighted_prob - yes_price
        no_edge = no_prob - no_price if no_price is not None else -yes_edge

        if yes_edge >= no_edge:
            direction = "BUY_YES"
            edge = yes_edge
            entry_price = yes_price
        else:
            direction = "BUY_NO"
            edge = no_edge
            entry_price = no_price if no_price is not None else yes_price

        if edge < self.min_edge:
            return None
        if weighted_confidence < self.min_confidence:
            return None

        live_signal_for_gate = validated_signals.get("live")
        if self._weather_hidden_gem_safety_rejects(live_signal_for_gate, direction, entry_price):
            logger.debug(
                "Weather hidden-gem safety guard rejected %s for %s",
                direction,
                getattr(market, "id", ""),
            )
            return None
        if self._weather_live_signal_vetoes_direction(live_signal_for_gate, direction):
            logger.debug("Weather live signal vetoed opposite-side tail trade for %s", getattr(market, "id", ""))
            return None

        signal_details = {k: dict(s) for k, s in validated_signals.items()}
        result = {
            "market_id": market.id,
            "exchange": market.exchange,
            "direction": direction,
            "model_probability": round(weighted_prob, 4),
            "market_price": round(entry_price, 4),
            "yes_market_price": yes_price,
            "no_market_price": no_price,
            "edge": round(edge, 4),
            "confidence": round(weighted_confidence, 4),
            "signals": {k: s["predicted_prob"] for k, s in validated_signals.items()},
            "signal_details": signal_details,
            "question": market.question,
        }
        live_details = signal_details.get("live", {})
        live_data = live_details.get("data") if isinstance(live_details.get("data"), dict) else {}
        if live_data:
            result["weather_confidence"] = live_details.get("confidence")
            result["agreement"] = live_data.get("agreement")
            result["station_id"] = live_data.get("station_id")
            result["station_cli"] = live_data.get("station_cli")
            result["weather_station_mapping"] = "exact" if live_data.get("station_id") else "inferred"
            result["source_agreement_score"] = live_data.get("agreement")
        return result

    def analyze_market_with_trace(self, market, order_book: dict = None) -> tuple[Optional[dict], StrategyTrace]:
        """Analyze a market and return strategy-level trace metadata.

        This intentionally mirrors ``analyze_market`` for Phase 1 consumers while
        keeping the existing live/paper call path unchanged.
        """
        trace = StrategyTrace()
        signals = {}
        weights = {}

        price_signal = self._price_signal(market, order_book)
        if price_signal:
            signals["price"] = price_signal
            weights["price"] = 0.40

        if self.enable_news:
            news_signal = self._news_signal(market)
            if news_signal:
                signals["news"] = news_signal
                weights["news"] = self.news_weight

        if self.enable_social:
            social_signal = self._social_signal(market)
            if social_signal:
                signals["social"] = social_signal
                weights["social"] = self.social_weight

        live_signal = self._live_data_signal(market)
        if live_signal:
            signals["live"] = live_signal
            weights["live"] = 0.50

        volume_signal = self._volume_signal(market)
        if volume_signal:
            signals["volume"] = volume_signal
            weights["volume"] = 0.15

        time_signal = self._time_signal(market)
        if time_signal:
            signals["time"] = time_signal
            weights["time"] = 0.10

        if self.enable_ai:
            ai_signal = self._ai_signal(market)
            if ai_signal:
                signals["ai"] = ai_signal
                weights["ai"] = self.ai_weight

        trace.raw_signals = {name: dict(signal) for name, signal in signals.items()}
        if not signals:
            trace.skip_reason_code = "no_raw_signals"
            return None, trace

        if "news" in signals and self.enable_news:
            news_feed_obj = getattr(self, "news", None)
            if news_feed_obj is not None and getattr(news_feed_obj, "all_sources_failed", False):
                redistributed = weights.pop("news", 0)
                signals.pop("news", None)
                if weights and redistributed > 0:
                    remaining_total = sum(weights.values())
                    if remaining_total > 0:
                        for k in list(weights.keys()):
                            weights[k] += redistributed * (weights[k] / remaining_total)
                trace.warnings.append("news_sources_failed_weight_redistributed")
                logger.debug(
                    f"News unavailable — redistributed {redistributed:.0%} weight "
                    f"to remaining signals: {list(weights.keys())}"
                )

        if not signals:
            trace.skip_reason_code = "no_signals_after_news_redistribution"
            return None, trace

        validation_results = self.validator.validate_all(signals, market)
        raw_predictions = {
            name: round(float(signal.get("predicted_prob", 0.5) or 0.5), 4)
            for name, signal in signals.items()
        }
        validated_signals = {}
        validated_weights = {}

        for name, sig in signals.items():
            validation = validation_results[name]
            trace.validation_results[name] = self._validation_result_to_trace(validation)
            self.signal_audit.write(market, name, sig, raw_predictions, validation)
            if validation.accepted:
                self._record_weather_observation(market, name, sig)
                adjusted = dict(sig)
                adjusted["predicted_prob"] = validation.adjusted_prob
                adjusted["confidence"] = validation.adjusted_confidence
                if validation.warnings:
                    adjusted.setdefault("warnings", []).extend(validation.warnings)
                validated_signals[name] = adjusted
                validated_weights[name] = weights[name]
                trace.accepted_signals[name] = dict(adjusted)
            else:
                rejected = dict(sig)
                rejected["rejection_reason"] = validation.rejection_reason
                rejected["warnings"] = list(validation.warnings or [])
                trace.rejected_signals[name] = rejected
                logger.debug(f"Signal REJECTED [{name}]: {validation.rejection_reason}")

        if not validated_signals:
            trace.skip_reason_code = "no_validated_signals"
            return None, trace

        total_weight = sum(validated_weights.values())
        if total_weight == 0:
            trace.skip_reason_code = "zero_validated_weight"
            return None, trace

        weighted_prob = sum(
            s["predicted_prob"] * validated_weights[k]
            for k, s in validated_signals.items()
        ) / total_weight

        weighted_confidence = sum(
            s["confidence"] * validated_weights[k]
            for k, s in validated_signals.items()
        ) / total_weight

        yes_price = market.yes_price
        no_price = getattr(market, "no_price", None)
        no_prob = 1 - weighted_prob
        yes_edge = weighted_prob - yes_price
        no_edge = no_prob - no_price if no_price is not None else -yes_edge

        if yes_edge >= no_edge:
            direction = "BUY_YES"
            edge = yes_edge
            entry_price = yes_price
        else:
            direction = "BUY_NO"
            edge = no_edge
            entry_price = no_price if no_price is not None else yes_price

        signal_details = {k: dict(s) for k, s in validated_signals.items()}
        result = {
            "market_id": market.id,
            "exchange": market.exchange,
            "direction": direction,
            "model_probability": round(weighted_prob, 4),
            "market_price": round(entry_price, 4),
            "yes_market_price": yes_price,
            "no_market_price": no_price,
            "edge": round(edge, 4),
            "confidence": round(weighted_confidence, 4),
            "signals": {k: s["predicted_prob"] for k, s in validated_signals.items()},
            "signal_details": signal_details,
            "question": market.question,
        }
        trace.ensemble_signal = dict(result)

        if edge < self.min_edge:
            trace.skip_reason_code = "edge_below_threshold"
            return None, trace
        if weighted_confidence < self.min_confidence:
            trace.skip_reason_code = "confidence_below_threshold"
            return None, trace

        live_signal_for_gate = validated_signals.get("live")
        if self._weather_live_signal_vetoes_direction(live_signal_for_gate, direction):
            trace.skip_reason_code = "weather_live_signal_veto"
            logger.debug("Weather live signal vetoed opposite-side tail trade for %s", getattr(market, "id", ""))
            return None, trace

        live_details = signal_details.get("live", {})
        live_data = live_details.get("data") if isinstance(live_details.get("data"), dict) else {}
        if live_data:
            result["weather_confidence"] = live_details.get("confidence")
            result["agreement"] = live_data.get("agreement")
            result["station_id"] = live_data.get("station_id")
            result["station_cli"] = live_data.get("station_cli")
            result["weather_station_mapping"] = "exact" if live_data.get("station_id") else "inferred"
            result["source_agreement_score"] = live_data.get("agreement")
            trace.ensemble_signal = dict(result)
        return result, trace

    @staticmethod
    def _validation_result_to_trace(validation) -> dict[str, Any]:
        return {
            "accepted": bool(validation.accepted),
            "adjusted_confidence": validation.adjusted_confidence,
            "adjusted_prob": validation.adjusted_prob,
            "warnings": list(validation.warnings or []),
            "rejection_reason": validation.rejection_reason,
        }

    @staticmethod
    def _weather_live_signal_vetoes_direction(live_signal: dict | None, direction: str) -> bool:
        if not isinstance(live_signal, dict):
            return False
        if live_signal.get("signal_type") != "weather":
            return False
        try:
            probability = float(live_signal.get("predicted_prob"))
            confidence = float(live_signal.get("confidence") or 0.0)
        except (TypeError, ValueError):
            return False
        data = live_signal.get("data") if isinstance(live_signal.get("data"), dict) else {}
        source_quality = str(data.get("source_quality") or "")
        is_official_daily = source_quality == "settlement_station_official_daily"
        has_strong_source = source_quality.startswith("settlement_station") or bool(data.get("station_id"))
        if not has_strong_source:
            return False
        if confidence < 0.85 and not is_official_daily:
            return False
        normalized_direction = str(direction or "").upper()
        if is_official_daily and probability < 0.50 and normalized_direction == "BUY_YES":
            return True
        if is_official_daily and probability > 0.50 and normalized_direction == "BUY_NO":
            return True
        if probability >= 0.80 and normalized_direction == "BUY_NO":
            return True
        if probability <= 0.20 and normalized_direction == "BUY_YES":
            return True
        return False

    def _record_weather_observation(self, market, signal_name: str, signal: dict) -> None:
        if not self.enable_weather_observation_log or self.weather_observation_log is None:
            return
        if not self._is_weather_signal(signal_name, signal):
            return
        if self.weather_market_mapper is None:
            return

        context = self.weather_market_mapper.resolve(
            getattr(market, "question", ""),
            getattr(market, "category", ""),
        )
        if context is None:
            return

        record = self._build_weather_observation_record(market, context, signal)
        if record is None:
            return

        try:
            self.weather_observation_log.append(record)
        except Exception as exc:
            logger.debug("Weather observation logging error for %s: %s", getattr(market, "id", ""), exc)

    def _build_weather_observation_record(self, market, context, signal: dict) -> Optional[dict]:
        data = signal.get("data", {}) or {}
        if not isinstance(data, dict):
            return None

        value = {}
        for key in ("forecast_high", "forecast_low", "current_temp", "actual_temp_used", "predicted_temp", "threshold"):
            metric = self._compact_number(data.get(key))
            if metric is not None:
                value[key] = metric

        agreement = self._compact_number(data.get("agreement"), digits=4)
        if agreement is not None:
            value["agreement"] = agreement

        question_side = signal.get("question_side") or data.get("question_side")
        if question_side:
            value["question_side"] = str(question_side)

        sources = data.get("sources") or []
        if not isinstance(sources, list):
            sources = [sources]
        normalized_sources = sorted({str(source).lower() for source in sources if source})
        if normalized_sources:
            value["sources"] = normalized_sources

        if not value:
            return None

        timestamp = signal.get("source_timestamp") or datetime.now(timezone.utc).isoformat()
        return {
            "ts": self._normalize_timestamp(timestamp),
            "kind": "forecast_update",
            "market_id": getattr(market, "id", ""),
            "city_id": context.city_id,
            "market_type": self._weather_market_type(getattr(market, "question", "")),
            "source_id": context.primary_source_id or "weather_registry_unassigned",
            "value": value,
        }

    def _is_weather_signal(self, signal_name: str, signal: dict) -> bool:
        signal_type = str(signal.get("signal_type") or signal_name or "").lower()
        if signal_type == "weather":
            return True
        if signal_type != "live":
            return False
        data = signal.get("data", {}) or {}
        return any(field in data for field in ("forecast_high", "forecast_low", "current_temp", "actual_temp_used"))

    def _weather_market_type(self, question: str) -> str:
        q = str(question or "").lower()
        if " high " in f" {q} ":
            return "high_temp"
        if " low " in f" {q} ":
            return "low_temp"
        return "temperature"

    def _normalize_timestamp(self, value: Any) -> str:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.isoformat()
        return str(value)

    def _compact_number(self, value: Any, *, digits: int = 2) -> float | int | None:
        if value is None or isinstance(value, bool):
            return None
        if not isinstance(value, (int, float)):
            return None
        if isinstance(value, int):
            return value
        return round(float(value), digits)

    def _handle_news_source_failure(self, signals: dict[str, dict], weights: dict[str, float]) -> bool:
        if not self.enable_news or not self._last_news_lookup_sources_failed:
            return False
        if self.fail_closed_on_news_source_failure:
            logger.debug("News unavailable — fail-closed strategy policy skipped trade")
            return True

        if "news" not in signals:
            return False

        redistributed = weights.pop("news", 0)
        signals.pop("news", None)
        if weights and redistributed > 0:
            remaining_total = sum(weights.values())
            if remaining_total > 0:
                for k in list(weights.keys()):
                    weights[k] += redistributed * (weights[k] / remaining_total)
        logger.debug(
            f"News unavailable — redistributed {redistributed:.0%} weight "
            f"to remaining signals: {list(weights.keys())}"
        )
        return False

    def _weather_hidden_gem_safety_rejects(
        self,
        live_signal: dict | None,
        direction: str,
        entry_price: float | None,
    ) -> bool:
        if not self.enable_weather_hidden_gem_safety_guard:
            return False
        try:
            entry = float(entry_price)
        except (TypeError, ValueError):
            return False
        if entry > self.weather_hidden_gem_max_entry_price:
            return False
        if not isinstance(live_signal, dict) or live_signal.get("signal_type") != "weather":
            return False

        data = live_signal.get("data") if isinstance(live_signal.get("data"), dict) else {}
        question_side = str(live_signal.get("question_side") or data.get("question_side") or "").lower()

        # Exact weather buckets at 1–5¢ need distribution support. A point forecast
        # near the bucket is not enough to call a 10x–25x hidden gem.
        if question_side == "range" and data.get("distribution_probability") is None:
            return True

        # Tail hidden gems are skipped only when validated live weather strongly
        # disagrees with the candidate side. This is direction-based, not YES-biased.
        if question_side not in {"above", "below"}:
            return False
        try:
            probability = float(live_signal.get("predicted_prob"))
            confidence = float(live_signal.get("confidence") or 0.0)
            agreement = float(data.get("agreement") or 0.0)
        except (TypeError, ValueError):
            return False
        if confidence < self.weather_hidden_gem_min_live_confidence:
            return False
        if agreement < self.weather_hidden_gem_min_source_agreement:
            return False

        normalized_direction = str(direction or "").upper()
        threshold = max(0.0, min(0.5, self.weather_hidden_gem_probability_threshold))
        candidate_probability = probability if normalized_direction == "BUY_YES" else 1.0 - probability
        return candidate_probability <= threshold

    @staticmethod
    def _config_float(value: Any, default: float) -> float:
        try:
            if value is None:
                return float(default)
            return float(value)
        except (TypeError, ValueError):
            return float(default)

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
        self._last_news_lookup_sources_failed = False
        try:
            news_items = self.news.get_news_for_market(market.question)

            if not news_items:
                self._last_news_lookup_sources_failed = bool(getattr(self.news, "all_sources_failed", False))
                return None

            self._last_news_lookup_sources_failed = False

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
            self._last_news_lookup_sources_failed = True
            logger.debug(f"News signal error: {e}")
            return None

    def _live_data_signal(self, market) -> Optional[dict]:
        """Get market-specific live data signal (weather, crypto, forex)."""
        try:
            result = self.live_feeds.get_signal(
                market.question,
                market.yes_price,
                getattr(market, 'category', ''),
                market_id=getattr(market, 'id', ''),
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
                 kelly_fraction: float = None, fee_rate: float = None,
                 min_position_size_usd: float = 1.0,
                 min_expected_net_profit_usd: float = 0.0):
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
        self.min_position_size_usd = float(min_position_size_usd)
        self.min_expected_net_profit_usd = float(min_expected_net_profit_usd)

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
        size = round(size, 2)
        if size <= 0:
            return 0

        gross_profit_if_win = size * ((1 - market_price) / market_price)
        net_profit_if_win = gross_profit_if_win * (1 - self.fee_rate)
        expected_net_profit = (p * net_profit_if_win) - ((1 - p) * size)
        if expected_net_profit < self.min_expected_net_profit_usd:
            return 0
        if size < self.min_position_size_usd:
            return 0
        return size
