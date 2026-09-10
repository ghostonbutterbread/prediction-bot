"""Bounded, read-only Source Router history from committed collector evidence."""
from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from bot.collector_replay_index import load_indexed_collector_rows_reverse
from bot.replay_decision_input import build_replay_decision_input_v1, verify_replay_decision_input_record_v1
from bot.replay_outcome_binding import (
    _bound_outcome_row, _decision_identity as _binding_identity, _index_strict_resolutions,
)
from bot.weather.source_history_manifest import collapse_strict_source_history_rows
from bot.weather.source_observation_ledger import (
    _decision_identity, _identity_key, _normalized_outcome, _pending_rows_for_input,
    _source_implied_outcome, is_eligible_for_future_history,
)


@dataclass(frozen=True, slots=True)
class DirectSourceHistoryResult:
    """In-memory, provenance-bound router history for one committed checkpoint."""

    scorecard_rows: list[dict[str, Any]]
    counts: dict[str, int]
    index_manifest_sha256: str
    resolution_sha256: str
    bucket_coverage: list[dict[str, Any]]


def load_direct_strict_source_history(
    *,
    index_path: str | Path,
    manifest_path: str | Path,
    strict_resolutions_path: str | Path,
    accepted_limit: int = 100,
    as_of_decision_time: str | None = None,
) -> DirectSourceHistoryResult:
    """Select up to accepted_limit distinct events per source/city/kind/shape.

    ``accepted_limit`` is a compatibility spelling for an event budget, not a
    raw-row budget. Repeated polls and correlated contracts share one unit.
    Selection uses newest committed archive order; each selected event keeps
    its earliest eligible source observation, never its best outcome.
    No derived history, replay input, outcome, or scorecard artifact is written.
    The whole committed index is validated and scanned: a global early exit
    would starve sparse buckets. Memory scales with buckets times the budget.
    """
    if type(accepted_limit) is not int or accepted_limit <= 0:
        raise ValueError("accepted_limit must be a positive integer")
    index_file, manifest_file = Path(index_path).resolve(), Path(manifest_path).resolve()
    if not index_file.is_file() or not manifest_file.is_file():
        raise ValueError("committed collector replay index and manifest must be readable files")
    resolution_path = Path(strict_resolutions_path).resolve()
    if not resolution_path.is_file():
        raise ValueError("strict resolutions must be a readable file")

    if not isinstance(as_of_decision_time, str) or not as_of_decision_time:
        raise ValueError("direct Source Router history requires an immutable decision timestamp")
    resolution_index, resolution_sha256 = _load_stable_resolution_index(resolution_path)
    buckets: dict[tuple[str, ...], dict[tuple[str, ...], dict[str, Any]]] = {}
    qualified_observations = repeat_observations = outside_budget = 0
    rejections: Counter[str] = Counter()
    settlement_counts: Counter[str] = Counter()
    inspected = 0
    bound_count = 0
    for raw in load_indexed_collector_rows_reverse(Path(index_path), Path(manifest_path)):
        inspected += 1
        built = build_replay_decision_input_v1(raw)
        if not built.ok or built.record is None:
            for error in built.errors or ():
                rejections[error.code] += 1
            if not built.errors:
                rejections["unknown_replay_input_rejection"] += 1
            continue
        identity = _binding_identity(built.record)
        if identity is None:
            raise ValueError("sanitized replay input failed its own identity contract")
        resolution = resolution_index.accepted.get(identity["market_id"])
        outcomes = [] if resolution is None else [
            _bound_outcome_row(identity, resolution, strict_resolution_source_sha256=resolution_sha256)
        ]
        candidate_settled, candidate_counts = _settled_strict_observations([built.record], outcomes)
        settlement_counts.update(candidate_counts)
        candidate_settled = [
            row for row in candidate_settled
            if is_eligible_for_future_history(row, as_of_decision_time)
        ]
        qualified_observations += len(candidate_settled)
        for observation in candidate_settled:
            bucket = tuple(str(observation.get(key) or '').strip().casefold() for key in
                           ('source_id', 'city_id', 'market_kind', 'contract_shape'))
            event = _history_event_key(observation)
            representatives = buckets.setdefault(bucket, {})
            previous = representatives.get(event)
            if previous is not None:
                repeat_observations += 1
                if _history_representative_key(observation) < _history_representative_key(previous):
                    representatives[event] = observation
            elif len(representatives) < accepted_limit:
                representatives[event] = observation
            else:
                outside_budget += 1
    settled = [row for events in buckets.values() for row in events.values()]
    settled.sort(key=_history_representative_key)
    accepted = {row['canonical_input_sha256'] for row in settled}
    bound_count = len(accepted)
    collapsed, collapse_counts = collapse_strict_source_history_rows(settled)
    collapse_counts['collapsed_repeat_polls'] += repeat_observations
    # Keep aggregation behavior centralized with the legacy generation writer.
    from bot.auto_source_router_promotion import strict_scorecard_rows_from_history
    source_sha256 = hashlib.sha256(_canonical_jsonl(collapsed)).hexdigest()
    scorecards = strict_scorecard_rows_from_history(collapsed, source_sha256=source_sha256)
    manifest_bytes = Path(manifest_path).read_bytes()
    return DirectSourceHistoryResult(
        scorecard_rows=scorecards,
        counts={
            "indexed_rows_inspected": inspected,
            "accepted_replay_inputs": len(accepted),
            "rejected_replay_inputs": inspected - len(accepted),
            "bound_outcomes": bound_count,
            "settled_strict_observations": len(settled),
            "collapsed_observations": len(collapsed),
            "qualified_source_observations": qualified_observations,
            "repeat_observations_in_selected_events": repeat_observations,
            "observations_outside_bucket_budget": outside_budget,
            "history_events_per_bucket": accepted_limit,
            **{f"rejection_{key}": int(value) for key, value in sorted(rejections.items())},
            **{f"settlement_{key}": int(value) for key, value in sorted(settlement_counts.items())},
            **{f"collapse_{key}": int(value) for key, value in sorted(collapse_counts.items())},
        },
        index_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        resolution_sha256=resolution_sha256,
        bucket_coverage=[
            {
                **dict(zip(('source_id', 'city_id', 'market_kind', 'contract_shape'), bucket)),
                'requested_units': accepted_limit,
                'selected_units': len(events),
                'shortfall': max(0, accepted_limit - len(events)),
                'selected_market_count': len({row['market_id'] for row in events.values()}),
                'event_units': sum(key[0] != 'contract_only' for key in events),
                'contract_only_units': sum(key[0] == 'contract_only' for key in events),
                'earliest_source_as_of': min(events.values(), key=_history_representative_key)['source_as_of'],
                'latest_source_as_of': max(events.values(), key=_history_representative_key)['source_as_of'],
            }
            for bucket, events in sorted(buckets.items())
        ],
    )


def _history_event_key(row: Mapping[str, Any]) -> tuple[str, ...]:
    # Strict city/target-date/kind proof identifies the physical weather event.
    # Optional exchange event metadata must not split the same event on repolls.
    if row.get('city_id') and row.get('market_date') and row.get('market_kind'):
        return ('city_date_kind', str(row['city_id']).casefold(), str(row['market_date']),
                str(row['market_kind']).casefold())
    event = row.get('event_ticker') or row.get('event_id')
    if event:
        return ('event', str(event).strip().casefold())
    return ('contract_only', str(row['market_id']))


def _history_representative_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    from datetime import datetime
    return (
        datetime.fromisoformat(str(row['source_as_of']).replace('Z', '+00:00')),
        datetime.fromisoformat(str(row['input_observed_at']).replace('Z', '+00:00')),
        str(row.get('source_provenance', {}).get('source_record_sha256') or ''),
        str(row.get('market_id') or ''), str(row['canonical_input_sha256']),
    )


def _settled_strict_observations(
    inputs: list[Mapping[str, Any]], outcomes: list[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Apply the existing source-proof/outcome rules without artifact I/O."""
    counts: Counter[str] = Counter()
    pending: dict[str, dict[str, Any]] = {}
    for row in inputs:
        if not verify_replay_decision_input_record_v1(row):
            counts["invalid_input"] += 1
            continue
        identity = _decision_identity(row)
        if identity is None:
            counts["invalid_input"] += 1
            continue
        for observation in _pending_rows_for_input(row, identity):
            existing = pending.get(observation["source_observation_id"])
            if existing is None:
                pending[observation["source_observation_id"]] = observation
            elif existing != observation:
                raise ValueError("conflicting duplicate source observation")

    outcome_by_identity: dict[tuple[str, ...], dict[str, Any]] = {}
    conflicts: set[tuple[str, ...]] = set()
    for raw in outcomes:
        normalized = _normalized_outcome(raw)
        if normalized is None:
            counts["invalid_bound_outcome"] += 1
            continue
        key = _identity_key(normalized["canonical_input_sha256"], normalized["decision_key"])
        previous = outcome_by_identity.get(key)
        if previous is None:
            outcome_by_identity[key] = normalized
        elif previous != normalized:
            conflicts.add(key)
    settled: list[dict[str, Any]] = []
    for observation in pending.values():
        if observation.get("source_correctness_eligibility") != "eligible_strict_source_proof":
            counts["unusable_strict_proof"] += 1
            continue
        key = _identity_key(observation["canonical_input_sha256"], observation["decision_key"])
        if key in conflicts:
            counts["conflicting_outcome"] += 1
            continue
        outcome = outcome_by_identity.get(key)
        if outcome is None:
            counts["unresolved"] += 1
            continue
        if outcome["official_outcome"] == "VOID":
            counts["void"] += 1
            continue
        implied = _source_implied_outcome(observation)
        if implied is None:
            counts["unusable_implied_side"] += 1
            continue
        settled.append({
            **observation,
            "schema_name": "settled_source_correctness", "schema_version": 1,
            "source_implied_outcome": implied, "official_outcome": outcome["official_outcome"],
            "direction_correct": implied == outcome["official_outcome"],
            "eligible_for_reliability": True, "eligible_for_source_history": True,
            "availability_field": "settlement_ts", "settlement_ts": outcome["settlement_ts"],
            "known_after": outcome["settlement_ts"], "resolution_id": outcome["resolution_id"],
            "resolution_provenance": outcome.get("provenance"),
        })
    return settled, counts


def _load_stable_resolution_index(path: Path):
    """Index one immutable resolution-file receipt and attest its exact bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())

        def records():
            for line_number, raw in enumerate(stream, start=1):
                digest.update(raw)
                if stream.tell() > opened.st_size:
                    raise ValueError("strict resolutions changed while being read")
                raw_line = raw.rstrip(b"\r\n")
                if not raw_line.strip():
                    continue
                try:
                    parsed = json.loads(raw_line)
                except json.JSONDecodeError:
                    parsed = None
                yield line_number, parsed if isinstance(parsed, Mapping) else None, raw_line

        indexed, _ = _index_strict_resolutions(records())
        closed = os.fstat(stream.fileno())
    current = path.stat()
    if (
        closed.st_size != opened.st_size
        or (closed.st_dev, closed.st_ino) != (opened.st_dev, opened.st_ino)
        or (closed.st_mtime_ns, closed.st_ctime_ns) != (opened.st_mtime_ns, opened.st_ctime_ns)
        or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns)
        != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
    ):
        raise ValueError("strict resolutions changed while being read")
    return indexed, digest.hexdigest()


def _canonical_jsonl(rows: list[Mapping[str, Any]]) -> bytes:
    import json
    return b"".join((json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8") for row in rows)
