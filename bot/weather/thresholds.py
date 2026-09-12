from __future__ import annotations

import math
import re
from typing import Any


# Same point-forecast predicate as weather_pro and historical_provider: no
# transformation of forecasts into hypothetical official station observations.
RANGE_FORECAST_CONVENTION = "recorded_interval_unrounded_forecast_v1"
_NUMBER = r"[+-]?\d+(?:\.\d+)?"
# An omitted unit is legacy Fahrenheit only at a grammatical boundary. An
# arbitrary word after a degree marker is a unit token, never a bare-degree
# prefix. These continuations are range/comparison syntax or question prose.
_BARE_DEGREE_END = r"(?=\s*(?:$|[?.,;:!)/–-]|\d|\b(?:on|in|at|for|to|and|or|inclusive|exclusiv\w*|less|greater)\b))"
_UNIT = rf"(?:(?:°\s*|degrees?\b\s*)(?:Fahrenheit\b|F\b|{_BARE_DEGREE_END})|Fahrenheit\b|F\b)"
_UNIT_MARKER = re.compile(r"°|degrees?\b", re.I)
_TEMPERATURE = re.compile(rf"{_NUMBER}\s*(?:°|degrees?\b|Fahrenheit\b|F\b)", re.I)
_RANGE = re.compile(
    rf"(?<![\w.])({_NUMBER})\s*(?:{_UNIT}\s*)?(?:-|–|to|and)\s*({_NUMBER})\s*(?:{_UNIT})?", re.I,
)
_COMPARISON = re.compile(
    rf"(greater than(?: or equal to)?|less than(?: or equal to)?)\s*({_NUMBER})\s*(?:{_UNIT})?", re.I,
)
# Dates are context, not temperature predicates. Remove only complete supported
# calendar forms, not everything following 'on' (which can hide another bound).
_MONTH = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
_DATE = re.compile(rf"\b(?:\d{{4}}-\d{{2}}-\d{{2}}|{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?)(?![\w°])", re.I)
# Lex numbers more broadly than the supported bound grammar: an unsupported
# leading-dot decimal must be rejected, not disappear from the clause envelope.
_CLAUSE_TOKEN = re.compile(rf"greater than(?: or equal to)?|less than(?: or equal to)?|[+-]?(?:\d+(?:\.\d+)?|\.\d+)\s*(?:{_UNIT})?", re.I)


def _temperature_clause(text: str, *, inherited_unit: bool = False):
    """Bounded numeric clause envelope, then full grammar consumption.

    Ordinary location/question prose surrounds the numeric clause. Complete
    calendar dates have already been removed. Every remaining numeric token,
    including a unitless point/comparison/range, must belong to ONE interval.
    Do not truncate at conjunctions, punctuation or context prepositions.
    """
    tokens = list(_CLAUSE_TOKEN.finditer(text))
    if not tokens:
        return None
    start, end = tokens[0].start(), tokens[-1].end()
    clause = text[start:end].strip()
    pair = _RANGE.fullmatch(clause)
    joined = re.fullmatch(rf"{_COMPARISON.pattern}\s+and\s+{_COMPARISON.pattern}", clause, re.I)
    # Inherit for temperature predicates and bounded subtitle grammar, not
    # incidental numbers in explanatory prose (e.g. report pages 2-3).
    bare = re.fullmatch(r"\s*(?:between\s+)?", text[:start], re.I) and re.fullmatch(
        r"\s*(?:inclusive\s*)?[?.!]?\s*", text[end:], re.I)
    # Recognize an outside numeric condition using the same contract context as
    # affirmative clauses. This is evidence recognition, NOT complement parsing:
    # retain 'outside' in the remainder and let the origin reconcile/reject it.
    # Nonnumeric 'Outside range' and report prose supply no such predicate.
    outside = re.fullmatch(r"\s*outside\s*", text[:start], re.I)
    if not _TEMPERATURE.search(clause) and not (
        inherited_unit and (re.search(r"\btemperature\b", text, re.I) or bare or outside)
    ):
        return None
    if not pair and not joined:
        return ([], [], text, False)
    pairs = [(float(pair[1]), float(pair[2]))] if pair else []
    found = _COMPARISON.findall(clause) if joined else []
    return pairs, found, text[:start] + ' ' + text[end:], True


def extract_forecast_interval(question: str, metadata: dict | None = None) -> dict:
    """Retain recorded bounds; never reconstruct a bucket from its ticker.

    Printed weather ranges and strike pairs use the repository closed/unrounded
    forecast convention. Recognized explicit comparison rules take precedence
    over default inclusivity, but conflicting bounds/rules fail closed. Unknown
    quantization stays unscored: a forecast is not an official observation.
    """
    metadata = metadata or {}
    evidence = []
    intervals = []
    comparisons = []
    measurement_kinds = {metadata['market_kind']} if metadata.get('market_kind') in {'high', 'low'} else set()

    def result(status):
        return {"status": status, "convention": RANGE_FORECAST_CONVENTION,
                "forecast_transform": "none", "unit": "F",
                "exchange_settlement_parity_verified": False,
                "observation_quantization": "not_established", "evidence": evidence}

    lower, upper = metadata.get("floor_strike"), metadata.get("cap_strike")
    if lower is not None or upper is not None:
        evidence.append({"origin": "market_strikes", "floor_strike": lower, "cap_strike": upper,
                         "strike_type": metadata.get("strike_type")})
        if lower is None or upper is None:
            return result("range_incomplete_strikes")
        try:
            pair = float(lower), float(upper)
        except (TypeError, ValueError):
            return result("range_invalid_bounds")
        if not all(math.isfinite(x) for x in pair):
            return result("range_nonfinite_bounds")
        if metadata.get("strike_type") not in (None, "", "between"):
            return result("range_conflicting_strike_type")
        intervals.append(pair)

    origins = ('question', 'subtitle', 'market_subtitle', 'yes_subtitle',
               'rules_primary', 'rules_secondary', 'no_subtitle')
    texts = {origin: str(question or '') if origin == 'question' else str(metadata.get(origin) or '')
             for origin in origins}
    # Resolve context before consuming any origin: the only unit marker may be
    # in a later subtitle/rule. Strikes retain their existing Fahrenheit contract.
    inherited_unit = bool(intervals) or any(_TEMPERATURE.search(_DATE.sub(' ', text)) for text in texts.values())
    incomplete_clause = False
    for origin, text in texts.items():
        if not text:
            continue
        evidence.append({"origin": origin, "text": text})
        # A calendar date is never a temperature interval.
        text = _DATE.sub(" ", text)
        if (any(not re.match(_UNIT, text[m.start():], re.I) for m in _UNIT_MARKER.finditer(text))
                or re.search(rf'{_NUMBER}\s*(?:Celsius|C|Kelvin|K|Rankine|R|Reaumur)\b', text, re.I)):
            return result("range_unsupported_unit")
        for kind in re.findall(r"\b(maximum|minimum|highest|lowest|high|low)\s+temperature\b", text, re.I):
            measurement_kinds.add('high' if kind.lower() in {'maximum', 'highest', 'high'} else 'low')
        if len(measurement_kinds) > 1:
            return result("range_conflicting_measurement")
        if re.search(r"\bround\w*\b|\bquantiz\w*\b|\btruncat\w*\b", text, re.I):
            return result("range_quantization_unproven")
        parsed = _temperature_clause(text, inherited_unit=inherited_unit)
        if origin == 'no_subtitle':
            # NO-side predicates are not affirmative intervals. Numeric claims
            # require complement grammar, which is not implemented; prose does
            # not supply bounds. Never silently add them as YES-side evidence.
            if parsed:
                return result('range_unsupported_rules')
            continue
        pairs, found, remainder, complete = parsed or ([], [], text, True)
        incomplete_clause |= not complete
        if origin.startswith("rules_"):
            if not parsed and not re.search(r'\b(?:temperature|interval|between|greater|less)\b', text, re.I):
                continue
            # Only direct comparison sentences are implemented. Do not turn a
            # matching substring in conditional/negated prose into a contract.
            prefix = r"(?:the\s+)?(?:(?:maximum|minimum|high|low)\s+)?temperature\s+(?:is\s+)?"
            direct_rule = rf"{prefix}(?:{_COMPARISON.pattern}\s+and\s+{_COMPARISON.pattern}|between\s+{_RANGE.pattern}\s*,?\s*inclusive)\s*[.]?"
            if not complete or not parsed or not re.fullmatch(direct_rule, text.strip(), re.I):
                return result("range_unsupported_rules")
        else:
            # A lone point has no range bounds. With other range evidence it
            # conflicts; multiple numeric predicates must fully parse together.
            if ((not complete and (intervals or _RANGE.search(text) or _COMPARISON.search(text)
                                   or len(re.findall(_NUMBER, text)) > 1))
                    or re.search(r"\b(?:exclusive|excluding|not|outside|except|unless|if|or)\b|[<>]", remainder, re.I)):
                return result("range_ambiguous_rules")
        if found:
            lo = [(float(v), "equal" in op.lower()) for op, v in found if op.lower().startswith("greater")]
            hi = [(float(v), "equal" in op.lower()) for op, v in found if op.lower().startswith("less")]
            if len(lo) != 1 or len(hi) != 1 or not re.search(
                rf"{_COMPARISON.pattern}\s+and\s+{_COMPARISON.pattern}", text, re.I,
            ):
                return result("range_ambiguous_rules")
            pairs.append((lo[0][0], hi[0][0]))
            comparisons.append((lo[0][1], hi[0][1]))
        elif pairs and re.search(r"\binclusive\b", text, re.I):
            comparisons.append((True, True))
        intervals.extend(pairs)
    if not intervals:
        return result("range_missing_bounds")
    if incomplete_clause:
        return result("range_ambiguous_rules")
    if any(lo > hi for lo, hi in intervals):
        return result("range_reversed_bounds")
    if len(set(intervals)) != 1:
        return result("range_conflicting_bounds")
    if len(set(comparisons)) > 1:
        return result("range_conflicting_rules")
    lower, upper = intervals[0]
    lower_inclusive, upper_inclusive = comparisons[0] if comparisons else (True, True)
    return {**result("supported"), "lower": lower, "upper": upper,
            "lower_inclusive": lower_inclusive, "upper_inclusive": upper_inclusive,
            "endpoint_basis": "recorded_comparison_rules" if comparisons else "repository_closed_forecast_convention"}


def infer_range_forecast_outcome(value: float | None, interval: dict | None) -> str | None:
    if value is None or not math.isfinite(value) or not interval or interval.get("status") != "supported":
        return None
    lower, upper = interval["lower"], interval["upper"]
    above_lower = value >= lower if interval["lower_inclusive"] else value > lower
    below_upper = value <= upper if interval["upper_inclusive"] else value < upper
    return "YES" if above_lower and below_upper else "NO"


QUESTION_SIDES = {"above", "below", "range", "binary_bucket"}
DIRECTION_LABELS = {"above", "below"}
RESOLVED_OUTCOMES = {"YES", "NO"}


def infer_question_side(question: str, metadata: dict[str, Any] | None = None) -> str:
    metadata = metadata or {}
    candidates = [question, str(metadata.get("market_subtitle") or "")]
    normalized = " ".join(candidate.lower() for candidate in candidates if candidate)
    normalized = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", normalized)
    if " above " in f" {normalized} " or ">" in normalized:
        return "above"
    if " below " in f" {normalized} " or "<" in normalized:
        return "below"
    if (_RANGE.search(normalized)
            or re.search(rf"{_COMPARISON.pattern}\s+and\s+{_COMPARISON.pattern}", normalized, re.I)
            or " between " in f" {normalized} " or re.search(r"\b\d+\s*(?:to|-)\s*\d+\b", normalized)):
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

