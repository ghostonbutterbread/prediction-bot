"""Read-time weather identity reconciliation; never changes retained evidence.

Contract identity comes from the original question/ticker and retained exchange
rules, not collector-enriched city labels or a guessed station. Source geography
is a separate assertion: a forecast for another city is never reassigned.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Mapping
from math import isfinite
from urllib.parse import parse_qs, urlsplit
import re

from bot.weather.market_mapping import WeatherMarketCityMapper, WeatherMarketContext


@lru_cache(maxsize=1)
def _mapper() -> WeatherMarketCityMapper:
    return WeatherMarketCityMapper()


def intended_weather_market(*, market_id: str, question: str,
                            market_metadata: Mapping[str, Any]) -> WeatherMarketContext | None:
    """Ignore old guessed city fields when resolving original contract identity."""
    original = {key: value for key, value in market_metadata.items() if key in
                {'rules_primary', 'rules_secondary', 'custom_strike', 'series_ticker', 'event_ticker'}}
    mapper = _mapper()
    question_city = mapper.resolve(question)
    if question_city:
        # The immutable original question is itself retained identity evidence.
        # Let it establish a location for legacy/nonstandard contract IDs; a
        # recognized contradictory ticker or rule still fails the shared mapper.
        # Never trim or rewrite the ID, or use a snapshot-enriched question.
        original['city'] = question_city.city
    return mapper.resolve(question, market_id=market_id, market_metadata=original)


@dataclass(frozen=True)
class WeatherIdentityReconciliation:
    market: WeatherMarketContext | None
    source_record: Mapping[str, Any]
    reasons: tuple[str, ...]
    status: str


def reconcile_weather_identity(*, market_id: str, question: str,
                               market_metadata: Mapping[str, Any],
                               source_record: Mapping[str, Any]) -> WeatherIdentityReconciliation:
    """Return an in-memory attribution projection, keeping source values/clocks."""
    intended = intended_weather_market(market_id=market_id, question=question,
                                       market_metadata=market_metadata)
    if intended is None:
        return WeatherIdentityReconciliation(None, source_record, ('unproven_original_market_city',),
                                             'unproven_market_identity')
    metadata = source_record.get('source_metadata')
    metadata = metadata if isinstance(metadata, Mapping) else {}
    reasons = []
    location = metadata.get('forecast_location')
    location = location if isinstance(location, Mapping) else {}
    query_city_value = location.get('city')
    query_city = _city(query_city_value)
    unknown_query_city = bool(query_city_value) and query_city is None
    query_pairs, invalid_query_coordinates = _query_coordinates(metadata)
    if invalid_query_coordinates:
        reasons.append('invalid_retained_source_query_coordinates')
    query_cities = {query_city.city_id} if query_city else set()
    for parameters in query_pairs:
        matched = _coordinate_cities().get(parameters)
        if matched:
            query_cities.add(matched)
    if query_cities - {intended.city_id}:
        reasons.append('source_query_city_conflicts_with_original_market')
    if len(set(query_pairs)) > 1:
        reasons.append('conflicting_retained_source_query_coordinates')
    provider = metadata.get('source_reported_location')
    provider = provider if isinstance(provider, Mapping) else {}
    provider_values = [provider.get('city')]
    selected = provider.get('selected_period_location')
    if isinstance(selected, Mapping):
        provider_values.append(selected.get('city'))
    provider_cities = {city.city_id for value in provider_values if (city := _city(value)) is not None}
    response = {'latitude': metadata.get('response_latitude'), 'longitude': metadata.get('response_longitude')}
    for coordinates in (provider, response):
        if any(coordinates.get(key) is not None for key in ('latitude', 'longitude')):
            pair = _pair(coordinates)
            if pair is None:
                reasons.append('invalid_retained_provider_coordinates')
            elif (provider_coordinate_city := _coordinate_cities().get(pair)):
                provider_cities.add(provider_coordinate_city)
    unknown_provider_city = any(isinstance(value, str) and value.strip() and _city(value) is None
                                for value in provider_values)
    if provider_cities - {intended.city_id}:
        reasons.append('source_provider_city_conflicts_with_original_market')
    label = _city(source_record.get('source_location_city'))
    evidence_proves_city = (provider_cities == {intended.city_id} or
                            any(_coordinate_cities().get(pair) == intended.city_id for pair in query_pairs))
    unknown_query_coordinates = any(
        _coordinate_cities().get(pair) != intended.city_id
        and not (provider_cities == {intended.city_id} and _pair(provider) == pair)
        for pair in query_pairs)
    projection = source_record
    status = 'unchanged_source_location'
    if label is None or label.city_id != intended.city_id:
        if (evidence_proves_city and not reasons and not unknown_provider_city
                and not unknown_query_city and not unknown_query_coordinates):
            projection = {**source_record, 'source_location_city': intended.city}
            status = 'repaired_source_location_label'
        else:
            reasons.append('unproven_source_location_label')
    elif source_record.get('source_location_city') != intended.city:
        # Equivalent registry aliases are normalized only in the derived view.
        projection = {**source_record, 'source_location_city': intended.city}
    return WeatherIdentityReconciliation(intended, projection, tuple(reasons),
                                         'conflicting_source_geography' if reasons else status)


def classify_city_label(value: Any, intended: WeatherMarketContext | None, *, canonical: str) -> str:
    """Separate semantic wrong-city labels from harmless case/registry aliases."""
    if value is None or value == '':
        return 'missing'
    resolved = _city(value)
    if intended is None or resolved is None:
        return 'unproven'
    if resolved.city_id != intended.city_id:
        return 'conflicting'
    return 'canonical' if value == canonical else 'normalized'


def _city(value: Any) -> WeatherMarketContext | None:
    return _named_city(value) if isinstance(value, str) and value.strip() else None


@lru_cache(maxsize=256)
def _named_city(value: str) -> WeatherMarketContext | None:
    return _mapper().resolve(value)


@lru_cache(maxsize=1)
def _coordinate_cities() -> dict[tuple[float, float], str]:
    # Exact configured query coordinates only, not a radius/geocoder or a
    # settlement-station assertion. Unknown station/grid coordinates stay unknown.
    from bot.feeds.weather_pro import CITY_COORDS
    result = {}
    for name, pair in CITY_COORDS.items():
        city = _city(name)
        if city:
            result[tuple(pair)] = city.city_id
    return result


def _query_coordinates(metadata: Mapping[str, Any]) -> tuple[list[tuple[float, float]], bool]:
    location = metadata.get('forecast_location')
    location_pair = _pair(location) if isinstance(location, Mapping) else None
    pairs = [location_pair] if location_pair else []
    invalid = bool(isinstance(location, Mapping) and any(location.get(key) is not None
                   for key in ('latitude', 'longitude')) and location_pair is None)
    references = [metadata.get('capture')]
    attempts = metadata.get('collection_attempts')
    if isinstance(attempts, list):
        references.extend(attempts)
    urls = [metadata.get('forecast_query_url')]
    for reference in references:
        if isinstance(reference, Mapping):
            params = reference.get('request_parameters')
            pair = _pair(params) if isinstance(params, Mapping) else None
            if pair:
                pairs.append(pair)
            elif isinstance(params, Mapping) and any(params.get(key) is not None for key in ('latitude', 'longitude')):
                invalid = True
            urls.extend((reference.get('retrieval_url'), reference.get('url')))
    for url in urls:
        if isinstance(url, str):
            params = parse_qs(urlsplit(url).query)
            pair = _pair({key: values[0] for key, values in params.items() if len(values) == 1})
            if pair:
                pairs.append(pair)
            match = re.search(r'/points/(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)', urlsplit(url).path)
            if match:
                pair = _pair(dict(latitude=match[1], longitude=match[2]))
                if pair:
                    pairs.append(pair)
    return pairs, invalid


def _pair(parameters: Mapping[str, Any]) -> tuple[float, float] | None:
    try:
        if any(isinstance(parameters.get(key), bool) for key in ('latitude', 'longitude')):
            return None
        latitude, longitude = (float(parameters[key]) for key in ('latitude', 'longitude'))
        return (latitude, longitude) if (isfinite(latitude) and isfinite(longitude)
                                         and -90 <= latitude <= 90 and -180 <= longitude <= 180) else None
    except (KeyError, ValueError, TypeError):
        return None
