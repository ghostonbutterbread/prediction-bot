"""Decision-time empirical predictors, separate from strict measurement history.

Consumer seam::

    ledger = materialize_source_observation_ledger(
        replay_inputs_path=sealed_inputs, finalized_outcomes_path=bound_outcomes,
        strict_resolutions_path=original_independent_resolutions, output_dir=new_dir)
    history = load_predictive_source_history(ledger.metadata_path)
    choice = select_contextual_weather_source(sealed_current_input, history)

``choice['selected_forecast_temp_f']`` is the selected provider's numeric
forecast, NOT a probability. Consumers own the forecast-to-probability model
and all quote, sizing and wallet gates. No automatic fallback earns source
confidence when fewer than 30 independent prior events are available.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from bot.replay_decision_input import verify_replay_decision_input_record_v1
from bot.weather.source_observation_ledger import (
    SETTLED_FILENAME, SourceObservationLedgerError, _decision_identity,
    _pending_rows_for_input, _source_implied_outcome,
    predictive_context_supported,
)
from bot.weather.source_router import _recorded_forecast_sort_key, select_source_for_candidate

DEFAULT_CONTEXTUAL_MIN_EVENTS = 30
DEFAULT_CONTEXTUAL_MAX_EVENTS = 100


def load_predictive_source_history(metadata_path: str | Path) -> list[dict[str, Any]]:
    """Load exact settled bytes pinned by a materializer's local metadata.

    Return all retained rows; chronology, contextual filtering, deduplication
    and the event budget are applied per decision, not at collection/load time.
    Legacy settled rows remain visible but cannot earn predictive eligibility.
    """
    metadata_file = Path(metadata_path)
    metadata = json.loads(metadata_file.read_bytes())
    if metadata.get('schema_name') != 'source_observation_ledger_materialization' or metadata.get('schema_version') != 1:
        raise SourceObservationLedgerError('unsupported predictive source history materialization')
    declared = metadata.get('output_artifacts', {}).get(SETTLED_FILENAME, {})
    raw = (metadata_file.parent / SETTLED_FILENAME).read_bytes()
    if hashlib.sha256(raw).hexdigest() != declared.get('sha256'):
        raise SourceObservationLedgerError('predictive source history hash mismatch')
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if len(rows) != declared.get('record_count') or any(not isinstance(row, dict) for row in rows):
        raise SourceObservationLedgerError('predictive source history row count/type mismatch')
    return rows


def select_contextual_weather_source(
    current_input: Mapping[str, Any], history_rows: Iterable[Mapping[str, Any]], *,
    min_sample_count: int = DEFAULT_CONTEXTUAL_MIN_EVENTS,
    max_history_events: int = DEFAULT_CONTEXTUAL_MAX_EVENTS,
) -> dict[str, Any]:
    """Choose among captured current predictors using prior independent events.

    ``current_input`` is an existing sealed replay-decision-input v1 record,
    built by ``build_replay_decision_input_v1(..., source_history_only=True)``
    if economic/execution fields are unavailable. Outcome rows never enter it.
    Insufficient history returns ``routeable=False`` with a concrete reason.
    """
    if type(min_sample_count) is not int or min_sample_count <= 0:
        raise ValueError('min_sample_count must be a positive integer')
    if type(max_history_events) is not int or max_history_events < min_sample_count:
        raise ValueError('max_history_events must be an integer >= min_sample_count')
    base = dict(routeable=False, chosen_source_id=None, selected_forecast_temp_f=None,
                selected_source_observation=None, min_sample_count=min_sample_count,
                max_history_events=max_history_events, selection_mode='contextual_empirical_predictor',
                confidence_type='rank_score_not_calibrated_probability',
                history_availability_field='known_after', source_scores=[])
    if not verify_replay_decision_input_record_v1(current_input) or (identity := _decision_identity(current_input)) is None:
        return {**base, 'blockers': ['invalid_current_decision_input']}
    current = _pending_rows_for_input(current_input, identity)
    usable = [row for row in current if row['predictive_source_proof']['status'] == 'eligible_predictive_source_proof'
              and predictive_context_supported(row)
              and _source_implied_outcome(row) is not None]
    if not usable:
        return {**base, 'blockers': ['no_usable_current_predictors'], 'current_source_observations': current}
    # Source ID is the stable provider identity; display-name changes must not
    # split score rows or replace one source's history with another label.
    by_source = {row['source_id']: row for row in sorted(usable, key=_recorded_forecast_sort_key)}
    selected = select_source_for_candidate(usable[0],
        (row for row in history_rows if row.get('source_id') in by_source),
        history_mode='predictive', min_sample_count=min_sample_count,
        max_history_events=max_history_events, history_cutoff=identity['decision_key']['observed_at_utc'])
    scores = {row['source_id']: row for row in selected['source_scores']}
    selected['source_scores'] = [
        {**scores.get(source_id, dict(source_id=source_id, source_name=current_row['source_name'],
                                     sample_count=0, win_rate=None, wins=0, losses=0,
                                     source_observation_ids=[], forecast_semantics=[])),
         'shortfall': max(0, min_sample_count - scores.get(source_id, {}).get('sample_count', 0))}
        for source_id, current_row in sorted(by_source.items())
    ]
    chosen = by_source.get(selected['chosen_source_id'])
    return {**base, **selected, 'selection_mode': base['selection_mode'],
            'selected_forecast_temp_f': chosen['forecast_temp_f'] if chosen else None,
            'selected_source_observation': chosen, 'current_source_observations': current}
