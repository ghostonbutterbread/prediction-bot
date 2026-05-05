from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping


MONTHS = {
    "JAN": 1,
    "JANUARY": 1,
    "FEB": 2,
    "FEBRUARY": 2,
    "MAR": 3,
    "MARCH": 3,
    "APR": 4,
    "APRIL": 4,
    "MAY": 5,
    "JUN": 6,
    "JUNE": 6,
    "JUL": 7,
    "JULY": 7,
    "AUG": 8,
    "AUGUST": 8,
    "SEP": 9,
    "SEPT": 9,
    "SEPTEMBER": 9,
    "OCT": 10,
    "OCTOBER": 10,
    "NOV": 11,
    "NOVEMBER": 11,
    "DEC": 12,
    "DECEMBER": 12,
}

MARKET_DATE_FIELDS = (
    "market_date",
    "event_date",
    "target_date",
    "weather_date",
)
TICKER_FIELDS = (
    "market_ticker",
    "MARKET_TICKER",
    "ticker",
    "id",
    "market_id",
)
QUESTION_FIELDS = (
    "question",
    "title",
    "market_title",
    "MARKET_TITLE",
)
WEATHER_DATE_FIELDS = (
    "weather_date",
    "observation_date",
    "observed_date",
    "forecast_date",
    "forecast_start",
    "forecast_period_start",
    "period_start",
    "date",
    "target_date",
)


@dataclass(frozen=True)
class DateMatchValidationResult:
    ok: bool
    reason: str
    market_date: str | None
    weather_date: str | None
    source: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "market_date": self.market_date,
            "weather_date": self.weather_date,
            "source": self.source,
        }


@dataclass(frozen=True)
class DateDerivation:
    value: date | None
    source: str | None

    @property
    def isoformat(self) -> str | None:
        return self.value.isoformat() if self.value is not None else None


def validate_weather_date_match(
    market: Any,
    weather_data: Any | None = None,
    *,
    weather_date: Any | None = None,
) -> DateMatchValidationResult:
    """Fail-closed validation that market target date matches weather data date."""

    market_derivation = derive_market_date(market)
    weather_derivation = derive_weather_date(weather_data, weather_date=weather_date)

    if market_derivation.value is None and weather_derivation.value is None:
        return DateMatchValidationResult(
            ok=False,
            reason="missing_market_date_and_weather_date",
            market_date=None,
            weather_date=None,
            source=market_derivation.source,
        )
    if market_derivation.value is None:
        return DateMatchValidationResult(
            ok=False,
            reason="missing_market_date",
            market_date=None,
            weather_date=weather_derivation.isoformat,
            source=market_derivation.source,
        )
    if weather_derivation.value is None:
        return DateMatchValidationResult(
            ok=False,
            reason="missing_weather_date",
            market_date=market_derivation.isoformat,
            weather_date=None,
            source=market_derivation.source,
        )
    if market_derivation.value != weather_derivation.value:
        return DateMatchValidationResult(
            ok=False,
            reason="date_mismatch",
            market_date=market_derivation.isoformat,
            weather_date=weather_derivation.isoformat,
            source=market_derivation.source,
        )
    return DateMatchValidationResult(
        ok=True,
        reason="dates_match",
        market_date=market_derivation.isoformat,
        weather_date=weather_derivation.isoformat,
        source=market_derivation.source,
    )


def derive_market_date(market: Any) -> DateDerivation:
    for field in MARKET_DATE_FIELDS:
        value, matched_field = _lookup_with_source(market, field)
        parsed = _parse_date_value(value)
        if parsed is not None:
            return DateDerivation(parsed, f"field:{matched_field}")

    for field in TICKER_FIELDS:
        value, matched_field = _lookup_with_source(market, field)
        parsed = _parse_kalshi_weather_ticker_date(value)
        if parsed is not None:
            return DateDerivation(parsed, f"ticker:{matched_field}")

    for field in QUESTION_FIELDS:
        value, matched_field = _lookup_with_source(market, field)
        parsed = _parse_question_date(value)
        if parsed is not None:
            return DateDerivation(parsed, f"question:{matched_field}")

    return DateDerivation(None, None)


def derive_weather_date(weather_data: Any | None = None, *, weather_date: Any | None = None) -> DateDerivation:
    parsed = _parse_date_value(weather_date)
    if parsed is not None:
        return DateDerivation(parsed, "weather_date")

    for field in WEATHER_DATE_FIELDS:
        value, matched_field = _lookup_with_source(weather_data, field)
        parsed = _parse_date_value(value)
        if parsed is not None:
            return DateDerivation(parsed, f"weather:{matched_field}")

    return DateDerivation(None, None)


def _lookup_with_source(item: Any, field: str) -> tuple[Any | None, str]:
    if item is None:
        return None, field
    if isinstance(item, Mapping):
        if field in item:
            return item[field], field
        lowered = field.lower()
        for key, value in item.items():
            if str(key).lower() == lowered:
                return value, str(key)
        metadata = item.get("metadata")
        if isinstance(metadata, Mapping):
            return _lookup_with_source(metadata, field)
        return None, field
    return getattr(item, field, None), field


def _parse_date_value(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    rendered = str(value).strip()
    if not rendered:
        return None

    iso_match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", rendered)
    if iso_match:
        return _safe_date(int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3)))

    try:
        return datetime.fromisoformat(rendered.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _parse_kalshi_weather_ticker_date(value: Any) -> date | None:
    if value is None:
        return None
    rendered = str(value).upper()
    match = re.search(r"-(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|SEPT|OCT|NOV|DEC)(\d{1,2})(?:-|$)", rendered)
    if not match:
        return None

    year = 2000 + int(match.group(1))
    month = MONTHS[match.group(2)]
    day = int(match.group(3))
    return _safe_date(year, month, day)


def _parse_question_date(value: Any) -> date | None:
    if value is None:
        return None
    rendered = str(value)

    parsed = _parse_date_value(rendered)
    if parsed is not None:
        return parsed

    month_names = "|".join(sorted(MONTHS, key=len, reverse=True))
    month_first = re.search(
        rf"\b({month_names})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,)?\s+(\d{{4}})\b",
        rendered,
        flags=re.IGNORECASE,
    )
    if month_first:
        return _safe_date(
            int(month_first.group(3)),
            MONTHS[month_first.group(1).upper().rstrip(".")],
            int(month_first.group(2)),
        )

    day_first = re.search(
        rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_names})\.?(?:,)?\s+(\d{{4}})\b",
        rendered,
        flags=re.IGNORECASE,
    )
    if day_first:
        return _safe_date(
            int(day_first.group(3)),
            MONTHS[day_first.group(2).upper().rstrip(".")],
            int(day_first.group(1)),
        )

    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None
