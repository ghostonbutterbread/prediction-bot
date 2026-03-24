"""Signal validation and audit logging for multi-source trading signals."""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _parse_timestamp(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class ValidationResult:
    accepted: bool
    adjusted_confidence: float
    adjusted_prob: float
    warnings: list[str] = field(default_factory=list)
    rejection_reason: Optional[str] = None


class SignalAuditLog:
    """Append-only signal validation audit log."""

    def __init__(self, path: str = "data/signal_audit.jsonl"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        market,
        signal_name: str,
        raw_signal: dict,
        raw_predictions: dict[str, float],
        validation: ValidationResult,
    ):
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "market_id": getattr(market, "id", ""),
            "signal_type": signal_name,
            "raw_signal": raw_signal,
            "raw_predictions": raw_predictions,
            "accepted": validation.accepted,
            "warnings": validation.warnings,
            "rejection_reason": validation.rejection_reason,
            "final_edge": round(abs(validation.adjusted_prob - getattr(market, "yes_price", 0.5)), 4)
            if validation.accepted else None,
            "final_confidence": round(validation.adjusted_confidence, 4)
            if validation.accepted else None,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")


class SignalValidator:
    """Systemic signal hardening layer applied before ensemble weighting."""

    DEFAULT_TTLS = {
        "ai": 1800,
        "crypto": 60,
        "forex": 300,
        "live": 600,
        "news": 86400,
        "price": None,
        "social": 600,
        "time": None,
        "volume": None,
        "weather": 600,
    }

    def validate(self, signal: dict, market, signal_name: str = "") -> ValidationResult:
        data = signal.get("data", {}) or {}
        signal_type = self._infer_signal_type(signal, market, signal_name)
        adjusted_prob = _clamp(float(signal.get("predicted_prob", 0.5) or 0.5), 0.01, 0.99)
        adjusted_confidence = _clamp(float(signal.get("confidence", 0.5) or 0.5), 0.01, 0.99)
        warnings: list[str] = list(signal.get("warnings", []) or [])

        coherent, reason = self.check_coherence(signal, getattr(market, "question", ""))
        if not coherent:
            return ValidationResult(
                accepted=False,
                adjusted_confidence=0.0,
                adjusted_prob=adjusted_prob,
                rejection_reason=reason,
            )

        accepted, adjusted_prob, adjusted_confidence = self._apply_plausibility_bounds(
            signal_type,
            data,
            market,
            adjusted_prob,
            adjusted_confidence,
            warnings,
        )
        if not accepted:
            return ValidationResult(
                accepted=False,
                adjusted_confidence=0.0,
                adjusted_prob=adjusted_prob,
                warnings=warnings,
                rejection_reason=warnings[-1] if warnings else "Signal failed plausibility bounds",
            )

        accepted, adjusted_confidence = self._apply_liquidity_floor(
            market,
            adjusted_confidence,
            warnings,
        )
        if not accepted:
            return ValidationResult(
                accepted=False,
                adjusted_confidence=0.0,
                adjusted_prob=adjusted_prob,
                warnings=warnings,
                rejection_reason="Market volume below 100; signal discarded",
            )

        accepted, adjusted_confidence = self._apply_staleness_penalty(
            signal,
            signal_type,
            adjusted_confidence,
            warnings,
        )
        if not accepted:
            return ValidationResult(
                accepted=False,
                adjusted_confidence=0.0,
                adjusted_prob=adjusted_prob,
                warnings=warnings,
                rejection_reason="Feed data is more than 2x TTL old",
            )

        return ValidationResult(
            accepted=True,
            adjusted_confidence=_clamp(adjusted_confidence, 0.01, 0.99),
            adjusted_prob=_clamp(adjusted_prob, 0.01, 0.99),
            warnings=warnings,
        )

    def validate_all(self, signals: dict[str, dict], market) -> dict[str, ValidationResult]:
        results = {
            name: self.validate(signal, market, name)
            for name, signal in signals.items()
        }
        self._apply_cross_source_disagreement(signals, market, results)
        return results

    def check_coherence(self, signal: dict, market_question: str) -> tuple[bool, str]:
        market_side = self._infer_question_side(market_question)
        signal_side = self._infer_signal_side(signal)
        if market_side and signal_side and market_side != signal_side:
            return False, f"Signal answered '{signal_side}' while market asks '{market_side}'"
        return True, ""

    def _infer_signal_type(self, signal: dict, market, signal_name: str = "") -> str:
        if signal.get("signal_type"):
            return str(signal["signal_type"]).lower()

        data = signal.get("data", {}) or {}
        if "forecast_high" in data or "forecast_low" in data:
            return "weather"
        if "current_price" in data and "daily_volatility" in data:
            return "crypto"
        if "current_rate" in data:
            return "forex"
        if signal_name:
            return signal_name.lower()

        category = str(getattr(market, "category", "") or "").lower()
        question = str(getattr(market, "question", "") or "").lower()
        if any(word in category or word in question for word in ["temp", "temperature", "degrees", "weather"]):
            return "weather"
        if any(word in category or word in question for word in ["crypto", "bitcoin", "ethereum", "shib", "forex", "eur/usd", "usd/jpy"]):
            return "live"
        return "price"

    def _infer_question_side(self, question: str) -> Optional[str]:
        q = (question or "").lower()
        if not q:
            return None
        if any(token in q for token in [" between ", " range ", "-"]) and any(char.isdigit() for char in q):
            if "will" in q or "price" in q or "temp" in q or "temperature" in q:
                return "range"
        if any(token in q for token in [" below ", " under ", "<", " less than ", " lower than "]):
            return "below"
        if any(token in q for token in [" above ", " over ", ">", " more than ", " higher than ", " hit ", " reach "]):
            return "above"
        return None

    def _infer_signal_side(self, signal: dict) -> Optional[str]:
        for key in ("question_side", "answered_question_side"):
            value = signal.get(key)
            if value:
                return str(value).lower()

        data = signal.get("data", {}) or {}
        for key in ("question_side", "answered_question_side", "resolved_question_side"):
            value = data.get(key)
            if value:
                return str(value).lower()

        strike_type = str(data.get("strike_type", "") or "").lower()
        if strike_type == "greater":
            return "above"
        if strike_type == "less":
            return "below"
        if strike_type == "between":
            return "range"
        return None

    def _apply_plausibility_bounds(
        self,
        signal_type: str,
        data: dict,
        market,
        adjusted_prob: float,
        adjusted_confidence: float,
        warnings: list[str],
    ) -> tuple[bool, float, float]:
        if signal_type == "crypto":
            required_move_pct = self._float_or_none(data.get("required_move_pct"))
            daily_volatility = self._float_or_none(data.get("daily_volatility"))
            days_to_expiry = self._float_or_none(data.get("days_to_expiry"))
            if days_to_expiry is None:
                days_to_expiry = self._market_days_to_close(market)
            days_to_expiry = max(days_to_expiry or 1.0, 0.25)

            if required_move_pct is not None and daily_volatility is not None:
                daily_vol_fraction = daily_volatility / 100 if daily_volatility > 1 else daily_volatility
                max_move = 3 * daily_vol_fraction * math.sqrt(days_to_expiry) * 2.5
                if required_move_pct > max_move:
                    warnings.append(
                        f"Implausible crypto move: needs {required_move_pct:.1%} vs bound {max_move:.1%}"
                    )
                    adjusted_confidence = min(adjusted_confidence, 0.25)
                if days_to_expiry <= 1.05 and required_move_pct > 0.50 and adjusted_prob > 0.10:
                    warnings.append(
                        f"One-day crypto move exceeds 50% ({required_move_pct:.1%}); probability capped"
                    )
                    adjusted_prob = 0.10

        elif signal_type == "weather":
            temps = [
                self._float_or_none(data.get("predicted_temp")),
                self._float_or_none(data.get("actual_temp_used")),
                self._float_or_none(data.get("forecast_high")),
                self._float_or_none(data.get("forecast_low")),
                self._float_or_none(data.get("current_temp")),
            ]
            for temp in [value for value in temps if value is not None]:
                if temp < -60 or temp > 130:
                    warnings.append(f"Weather temperature {temp:.1f}F is outside plausible bounds")
                    return False, adjusted_prob, adjusted_confidence

        elif signal_type == "forex":
            implied_move_pct = self._float_or_none(data.get("implied_move_pct"))
            if implied_move_pct is None:
                current_rate = self._float_or_none(data.get("current_rate"))
                threshold = self._float_or_none(data.get("threshold"))
                if current_rate and threshold:
                    implied_move_pct = abs(threshold - current_rate) / current_rate
            days_to_expiry = self._float_or_none(data.get("days_to_expiry"))
            if days_to_expiry is None:
                days_to_expiry = self._market_days_to_close(market)
            days_to_expiry = max(days_to_expiry or 1.0, 0.25)

            if implied_move_pct is not None and implied_move_pct > 0.20 * days_to_expiry:
                warnings.append(
                    f"Implausible forex move: needs {implied_move_pct:.1%} over {days_to_expiry:.2f} day(s)"
                )
                return False, adjusted_prob, adjusted_confidence

        return True, adjusted_prob, adjusted_confidence

    def _apply_liquidity_floor(
        self,
        market,
        adjusted_confidence: float,
        warnings: list[str],
    ) -> tuple[bool, float]:
        volume = float(getattr(market, "volume", 0) or 0)
        if volume < 100:
            return False, adjusted_confidence
        if volume < 500 and adjusted_confidence > 0.40:
            warnings.append(f"Low liquidity (volume={volume:.0f}) capped confidence at 0.40")
            adjusted_confidence = 0.40
        return True, adjusted_confidence

    def _apply_staleness_penalty(
        self,
        signal: dict,
        signal_type: str,
        adjusted_confidence: float,
        warnings: list[str],
    ) -> tuple[bool, float]:
        data = signal.get("data", {}) or {}
        ttl_seconds = signal.get("ttl_seconds")
        if ttl_seconds is None:
            ttl_seconds = data.get("ttl_seconds")
        if ttl_seconds is None:
            ttl_seconds = self.DEFAULT_TTLS.get(signal_type)
        if ttl_seconds in (None, 0):
            return True, adjusted_confidence

        source_timestamp = (
            signal.get("source_timestamp")
            or data.get("source_timestamp")
            or data.get("fetched_at")
            or data.get("timestamp")
        )
        source_dt = _parse_timestamp(source_timestamp)
        if not source_dt:
            return True, adjusted_confidence

        age_seconds = (datetime.now(timezone.utc) - source_dt).total_seconds()
        if age_seconds > ttl_seconds * 2:
            warnings.append(f"Signal is stale ({age_seconds:.0f}s old vs TTL {ttl_seconds}s)")
            return False, adjusted_confidence
        if age_seconds > ttl_seconds:
            warnings.append(f"Signal is stale ({age_seconds:.0f}s old); reducing confidence")
            adjusted_confidence = max(0.01, adjusted_confidence - 0.15)
        return True, adjusted_confidence

    def _apply_cross_source_disagreement(
        self,
        signals: dict[str, dict],
        market,
        results: dict[str, ValidationResult],
    ):
        live_name = "live" if "live" in signals else "weather" if "weather" in signals else None
        if live_name and "news" in signals:
            live_signal = signals[live_name]
            if (
                results.get(live_name)
                and results[live_name].accepted
                and results.get("news")
                and results["news"].accepted
                and self._infer_signal_type(live_signal, market, live_name) == "weather"
                and self._opposite_direction(results[live_name], results["news"], market)
            ):
                for name in (live_name, "news"):
                    results[name].adjusted_confidence = max(0.01, results[name].adjusted_confidence - 0.10)
                    results[name].warnings.append("Weather and news disagree on market direction")

        if live_name and "social" in signals:
            if (
                results.get(live_name)
                and results[live_name].accepted
                and results.get("social")
                and results["social"].accepted
                and self._opposite_direction(results[live_name], results["social"], market)
            ):
                results[live_name].warnings.append("Social sentiment disagrees with live data")
                results["social"].warnings.append("Social sentiment disagrees with live data")

    def _opposite_direction(self, left: ValidationResult, right: ValidationResult, market) -> bool:
        market_price = float(getattr(market, "yes_price", 0.5) or 0.5)
        left_delta = left.adjusted_prob - market_price
        right_delta = right.adjusted_prob - market_price
        if abs(left_delta) < 0.02 or abs(right_delta) < 0.02:
            return False
        return left_delta * right_delta < 0

    def _market_days_to_close(self, market) -> float:
        closes_at = getattr(market, "closes_at", None)
        if not closes_at:
            return 1.0
        if closes_at.tzinfo is None:
            closes_at = closes_at.replace(tzinfo=timezone.utc)
        delta_seconds = (closes_at - datetime.now(timezone.utc)).total_seconds()
        return max(delta_seconds / 86400, 0.25)

    def _float_or_none(self, value) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
