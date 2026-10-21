"""Read predictive weather history from the committed collector index in memory."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from bot.collector_replay_index import load_indexed_collector_rows_reverse
from bot.replay_decision_input import build_replay_decision_input_v1
from bot.replay_outcome_binding import _bound_outcome_row, _decision_identity as binding_identity
from bot.weather.source_observation_ledger import (
    _attach_resolver_knowledge, _decision_identity, _normalized_outcome,
    _parse_time, _pending_rows_for_input, _source_implied_outcome,
    is_eligible_for_predictive_history, predictive_context_supported,
)
from bot.weather.source_router_direct_history import (
    _history_event_key, _history_event_recency_key, _history_representative_key,
    _load_stable_resolution_index, _verify_selected_cross_archive_resolutions,
)


@dataclass(frozen=True, slots=True)
class DirectPredictiveHistoryResult:
    rows: list[dict[str, Any]]
    archive_receipts: list[dict[str, str]]
    generation_sha256: str


def load_direct_predictive_source_history(
    *, index_path: str | Path | None = None, manifest_path: str | Path | None = None,
    strict_resolutions_path: str | Path | None = None,
    archive_sources: Sequence[Mapping[str, str | Path]] | None = None,
    accepted_limit: int = 100, as_of_decision_time: str,
) -> DirectPredictiveHistoryResult:
    """Keep at most N independent events per source/location/kind/shape.

    Partial-period predictors can qualify here without becoming strict full-day
    measurements. The selector owns final slice, tie and minimum-support rules.
    """
    if type(accepted_limit) is not int or accepted_limit <= 0 or _parse_time(as_of_decision_time) is None:
        raise ValueError('direct predictive history requires a positive budget and immutable decision time')
    if archive_sources is not None:
        if any(value is not None for value in (index_path, manifest_path, strict_resolutions_path)):
            raise ValueError('direct predictive archives cannot be mixed with single-archive paths')
        if not isinstance(archive_sources, (list, tuple)) or not archive_sources:
            raise ValueError('direct predictive archives require a nonempty ordered list')
        sources = archive_sources
    else:
        sources = ({'index_path': index_path, 'manifest_path': manifest_path,
                    'strict_resolutions_path': strict_resolutions_path},)
    buckets: dict[tuple[str, ...], dict[tuple[str, ...], dict[str, Any]]] = {}
    receipts: list[dict[str, str]] = []
    for source in sources:
        if not isinstance(source, Mapping) or not all(source.get(key) for key in
                ('index_path', 'manifest_path', 'strict_resolutions_path')):
            raise ValueError('each direct archive requires index, manifest and strict resolutions paths')
        index_file = Path(source['index_path']).resolve()
        manifest_file = Path(source['manifest_path']).resolve()
        resolution_file = Path(source['strict_resolutions_path']).resolve()
        if not all(path.is_file() for path in (index_file, manifest_file, resolution_file)):
            raise ValueError('committed collector replay index, manifest and strict resolutions must be readable files')
        resolution_index, resolution_sha256 = _load_stable_resolution_index(resolution_file)
        # Reuse the ledger's exact resolver receipt/clock qualification, once
        # per resolution file rather than once per selected observation.
        outcomes = {
            market_id: {'market_id': market_id, **resolution,
                'provenance': {'strict_resolution_source_sha256': resolution_sha256,
                    'strict_resolution_source_line_number': resolution['source_line_number'],
                    'raw_resolution_row_sha256': resolution['raw_row_sha256']}}
            for market_id, resolution in resolution_index.accepted.items()
        }
        _attach_resolver_knowledge(outcomes.values(), resolution_file)
        manifest_sha256 = hashlib.sha256(manifest_file.read_bytes()).hexdigest()
        receipts.append({'index_path': str(index_file), 'manifest_path': str(manifest_file),
            'strict_resolutions_path': str(resolution_file),
            'index_manifest_sha256': manifest_sha256, 'resolution_sha256': resolution_sha256})
        for raw in load_indexed_collector_rows_reverse(
            index_file, manifest_file, expected_manifest_sha256=manifest_sha256,
        ):
            built = build_replay_decision_input_v1(raw, source_history_only=True)
            if not built.ok or built.record is None:
                continue
            identity = binding_identity(built.record)
            if identity is None:
                raise ValueError('sanitized replay input failed its identity contract')
            decision_identity = _decision_identity(built.record)
            if decision_identity is None:
                raise ValueError('sanitized replay input failed its source identity contract')
            resolution = outcomes.get(identity['market_id'])
            if resolution is None or resolution['official_outcome'] == 'VOID':
                continue
            bound = _bound_outcome_row(identity, resolution, strict_resolution_source_sha256=resolution_sha256)
            normalized = _normalized_outcome(bound)
            if normalized is None:
                continue
            for pending in _pending_rows_for_input(built.record, decision_identity):
                if (pending['predictive_source_proof']['status'] != 'eligible_predictive_source_proof'
                        or not predictive_context_supported(pending)):
                    continue
                implied = _source_implied_outcome(pending)
                if implied is None:
                    continue
                row = {**pending, 'schema_name': 'settled_source_correctness', 'schema_version': 1,
                    'source_implied_outcome': implied, 'official_outcome': normalized['official_outcome'],
                    'direction_correct': implied == normalized['official_outcome'],
                    'eligible_for_reliability': True,
                    'eligible_for_source_history': pending['source_correctness_eligibility'] == 'eligible_strict_source_proof',
                    'eligible_for_predictive_history': resolution['resolver_knowledge_verified'] is True,
                    'predictive_availability_field': 'known_after', 'availability_field': 'settlement_ts',
                    'settlement_ts': normalized['settlement_ts'], 'known_after': resolution.get('known_after'),
                    'resolver_knowledge_verified': resolution['resolver_knowledge_verified'],
                    'predictive_history_reason': resolution.get('predictive_history_reason'),
                    'resolution_id': normalized['resolution_id'],
                    'resolution_resolved_at': resolution.get('resolution_resolved_at'),
                    'resolution_retrieved_at': resolution.get('resolution_retrieved_at'),
                    'resolution_provenance': normalized['provenance']}
                if not is_eligible_for_predictive_history(row, as_of_decision_time):
                    continue
                bucket = tuple(str(row.get(key) or '').strip().casefold() for key in
                    ('source_id', 'city_id', 'market_kind', 'contract_shape'))
                event = _history_event_key(row)
                representatives = buckets.setdefault(bucket, {})
                previous = representatives.get(event)
                if previous is not None:
                    if _history_representative_key(row) < _history_representative_key(previous):
                        representatives[event] = row
                elif len(representatives) < accepted_limit:
                    representatives[event] = row
                elif len(sources) > 1:
                    oldest = min(representatives, key=lambda key:
                        (_history_event_recency_key(representatives[key]), key))
                    if (_history_event_recency_key(row), event) > (
                        _history_event_recency_key(representatives[oldest]), oldest):
                        del representatives[oldest]
                        representatives[event] = row
        with resolution_file.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != resolution_sha256:
                raise ValueError('strict resolutions changed across direct predictive reads')
    rows = sorted((row for events in buckets.values() for row in events.values()), key=_history_representative_key)
    if len(receipts) > 1:
        _verify_selected_cross_archive_resolutions(rows, receipts)
    generation = hashlib.sha256(json.dumps(receipts, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return DirectPredictiveHistoryResult(rows, receipts, generation)
