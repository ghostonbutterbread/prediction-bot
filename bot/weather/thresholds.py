from __future__ import annotations

import re
from typing import Any


QUESTION_SIDES = {"above", "below", "range", "binary_bucket"}
DIRECTION_LABELS = {"above", "below"}
RESOLVED_OUTCOMES = {"YES", "NO"}


def infer_question_side(question: str, metadata: dict[str, Any] | None = None) -> str:
    metadata = metadata or {}
    candidates = [question, str(metadata.get("market_subtitle") or "")]
    normalized = " ".join(candidate.lower() for candidate in candidates if candidate)
    if " above " in f" {normalized} " or ">" in normalized:
        return "above"
    if " below " in f" {normalized} " or "<" in normalized:
        return "below"
    if " between " in f" {normalized} " or re.search(r"\b\d+\s*(?:to|-)\s*\d+\b", normalized):
        return "range"
    return "binary_bucket"


def extract_threshold_value(question: str, metadata: dict[str, Any] | None = None) -> float | None:
    metadata = metadata or {}
    for candidate in (
        question,
        str(metadata.get("market_subtitle") or ""),
        str(metadata.get("yes_subtitle") or ""),
    ):
        if not candidate:
            continue
        match = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:°|degrees?\b)", candidate, flags=re.IGNORECASE)
        if match:
            return float(match.group(1))
        match = re.search(r"[<>]=?\s*(-?\d+(?:\.\d+)?)", candidate)
        if match:
            return float(match.group(1))
    return None


def infer_direction_from_value(value: float | None, threshold_value: float | None) -> str | None:
    if value is None or threshold_value is None:
        return None
    if value > threshold_value:
        return "above"
    if value < threshold_value:
        return "below"
    return None


def infer_predicted_outcome(question_side: str, predicted_direction: str | None) -> str | None:
    normalized_direction = str(predicted_direction or "").strip().lower()
    if normalized_direction not in DIRECTION_LABELS:
        return None
    normalized_side = str(question_side or "").strip().lower()
    if normalized_side == "above":
        return "YES" if normalized_direction == "above" else "NO"
    if normalized_side == "below":
        return "YES" if normalized_direction == "below" else "NO"
    return None

