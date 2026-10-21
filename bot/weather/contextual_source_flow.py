"""Opt-in bridge from captured weather signals to prior empirical source choice.

The current projection is self-sealed, derived, and pre-persistence. Its hashes
identify captured collector inputs; they are NOT raw archive locators or proof
that raw bytes have already been durably archived. No outcomes enter this input.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any

from bot.weather.registry import WeatherRegistry
from bot.replay_decision_input import build_replay_decision_input_v1
from bot.weather.predictive_source_history import (
    load_predictive_source_history, select_contextual_weather_source,
)


def recorded_forecast_type(source: dict[str, Any]) -> Any:
    """Preserve an adapter's explicit semantics, never infer a full-day type."""
    metadata = source.get('source_metadata')
    return source.get('forecast_type') or (metadata.get('forecast_type') if isinstance(metadata, dict) else None)


def recorded_source_fields(source: dict[str, Any]) -> dict[str, Any]:
    """Lift recorded adapter aliases once for current and collector projections."""
    return dict(forecast_type=recorded_forecast_type(source),
                source_as_of=source.get('source_as_of') or source.get('as_of'),
                source_fetched_at=source.get('source_fetched_at') or source.get('fetched_at'))


def recorded_city_context(city: str | None) -> dict[str, Any]:
    """Bind a captured city label to the collector's existing registry identity."""
    sources = WeatherRegistry.from_file().collection_sources(city) if city else []
    return {'city':city, **({'city_id':sources[0]['city_id']} if sources else {})}


def select_captured_weather_source(
    signal: dict[str, Any], *, question: str, market_id: str,
    market_metadata: dict[str, Any] | None, observed_at: str, config: dict[str, Any],
) -> dict[str, Any]:
    """Load the materialized history and return only a bounded selection receipt."""
    minimum = config.get('min_sample_count', 30)
    cap = config.get('max_history_events', 100)
    audit = dict(routeable=False, chosen_source_id=None, selected_forecast_temp_f=None,
                 min_sample_count=minimum, max_history_events=cap,
                 selection_mode='contextual_empirical_predictor',
                 confidence_type='rank_score_not_calibrated_probability',
                 history_availability_field='known_after', source_scores=[],
                 current_input_kind='derived_captured_current_projection',
                 current_observed_at=observed_at)
    if signal.get('collection_only') is True or signal.get('prediction_available') is False:
        return {**audit, 'blockers':[signal.get('availability_reason') or 'no_current_weather_prediction']}
    data = signal['data']
    # Already-canonical collector source rows go through the one replay
    # allowlist/normalizer. Only the adapter's recorded type needs lifting from
    # metadata (also used by PredictionLab's existing source projection).
    sources = [{**source, **recorded_source_fields(source)}
               for source in data.get('source_details', []) if isinstance(source, dict)]
    row = dict(observed_at=observed_at, market_id=market_id, question=question,
               decision_artifact={'source_context':{'source':'captured_weather', 'data':{
                   'market_metadata':market_metadata or {},
                   'weather_source_snapshot':dict(market_id=market_id, question=question,
                       market_date=data.get('market_target_date'),
                       station_resolution=recorded_city_context(data.get('city')), sources=sources)}}})
    current = build_replay_decision_input_v1(row, source_history_only=True)
    if not current.ok or current.record is None:
        return {**audit, 'blockers':['invalid_current_decision_input']}
    audit.update(current_input_sha256=current.record['canonical_input_sha256'],
                 current_projection_sha256=current.record['decision_key']['raw_row_sha256'])
    path = config.get('history_metadata_path')
    if not isinstance(path, (str, Path)) or not str(path):
        return {**audit, 'blockers':['missing_predictive_history_metadata_path']}
    try:
        metadata_bytes = Path(path).read_bytes()
        history = load_predictive_source_history(path)
        if Path(path).read_bytes() != metadata_bytes:
            return {**audit, 'blockers':['predictive_history_generation_changed']}
        generation = hashlib.sha256(metadata_bytes).hexdigest()
    except FileNotFoundError:
        return {**audit, 'blockers':['missing_predictive_history']}
    except OSError:
        return {**audit, 'blockers':['unreadable_predictive_history']}
    except (ValueError, TypeError, AttributeError):
        return {**audit, 'blockers':['corrupt_predictive_history']}
    audit['history_generation_sha256'] = generation
    try:
        choice = select_contextual_weather_source(current.record, history,
            min_sample_count=minimum, max_history_events=cap)
    except ValueError:
        return {**audit, 'blockers':['invalid_contextual_selection_config']}
    for key in ('routeable', 'chosen_source_id', 'selected_forecast_temp_f', 'prior_sample_count',
                'rank_score', 'slice_key', 'blockers'):
        if key in choice:
            audit[key] = choice[key]
    audit['source_scores'] = [
        {**{key: score[key] for key in ('source_id','sample_count','wins','losses','win_rate','shortfall') if key in score},
         'source_observation_ids':score.get('source_observation_ids', [])[:3]}
        for score in choice.get('source_scores', [])]
    # Staleness is explicit and opt-in, separate from the 30/100 event policy.
    # Only observations actually used for this contextual comparison can refresh
    # it; unrelated cities and outcomes known at/after decision cannot do so.
    max_age = config.get('max_history_age_seconds')
    if max_age is not None:
        if type(max_age) not in (int, float) or not 0 < max_age < float('inf'):
            return {**audit, 'routeable':False, 'blockers':['invalid_contextual_selection_config']}
        used = {ref for score in choice.get('source_scores', []) for ref in score.get('source_observation_ids', [])}
        known = [datetime.fromisoformat(row['known_after'].replace('Z','+00:00')) for row in history
                 if row.get('source_observation_id') in used and row.get('known_after')]
        if known and (datetime.fromisoformat(observed_at.replace('Z','+00:00')) - max(known)).total_seconds() > max_age:
            audit.update(routeable=False, blockers=['stale_predictive_history'])
    return audit
