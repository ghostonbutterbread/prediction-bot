"""Materialize paper-only source observations from sealed replay inputs.

Pending rows contain only decision-time source evidence.  Finalized replay
outcomes are joined later by the binder's complete sealed decision identity;
they are never copied back into the pending artifact.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from bot.replay_decision_input import verify_replay_decision_input_record_v1
from bot.weather.source_scoreboard import extract_source_forecast_observations
from bot.weather.thresholds import infer_direction_from_value, infer_predicted_outcome


SCHEMA_VERSION = 1
PENDING_FILENAME = "pending_source_observations.jsonl"
SETTLED_FILENAME = "settled_source_correctness.jsonl"
UNSETTLED_FILENAME = "unsettled_or_unusable_source_observations.jsonl"
VOID_FILENAME = "void_source_observations.jsonl"
METADATA_FILENAME = "run_metadata.json"
_IDENTITY_FIELDS = (
    "shared_snapshot_id", "shared_candidate_id", "market_id", "observed_at_utc", "raw_row_sha256",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_ELIGIBLE_TARGET_PROOF = "eligible_exact_target_proof"
_TARGET_ALIASES = ("source_target_date", "target_forecast_date", "forecast_target", "forecast_date")


class SourceObservationLedgerError(ValueError):
    """The source-observation evidence cannot be materialized fail-closed."""


@dataclass(frozen=True, slots=True)
class SourceObservationLedgerResult:
    output_dir: Path
    pending_path: Path
    settled_path: Path
    unsettled_path: Path
    void_path: Path
    metadata_path: Path
    metadata: dict[str, Any]


def materialize_source_observation_ledger(
    *, replay_inputs_path: str | Path, finalized_outcomes_path: str | Path, output_dir: str | Path,
) -> SourceObservationLedgerResult:
    """Write versioned pending/settled/unusable source artifacts from derived inputs.

    This intentionally has no router, price, action, wallet, or service surface.
    Duplicate identical observations are collapsed; conflicting identities abort
    before output is written. Conflicting authoritative outcomes are retained as
    unusable observations rather than selecting an arbitrary result.
    """

    inputs_path, outcomes_path, target_dir = (Path(replay_inputs_path).resolve(), Path(finalized_outcomes_path).resolve(), Path(output_dir).resolve())
    if not inputs_path.is_file() or not outcomes_path.is_file():
        raise SourceObservationLedgerError("replay inputs and finalized outcomes must be readable files")
    _prepare_output_dir(target_dir)

    input_sha256, outcome_sha256 = _sha256_file(inputs_path), _sha256_file(outcomes_path)
    counters: Counter[str] = Counter()
    pending_by_id: dict[str, dict[str, Any]] = {}
    for _line_number, row in _read_jsonl(inputs_path):
        counters["input_records_seen"] += 1
        if not verify_replay_decision_input_record_v1(row):
            counters["invalid_input_records"] += 1
            continue
        identity = _decision_identity(row)
        if identity is None:
            counters["invalid_input_records"] += 1
            continue
        pending_rows = _pending_rows_for_input(row, identity)
        if not pending_rows:
            counters["inputs_without_source_observations"] += 1
        for pending in pending_rows:
            counters["source_observations_seen"] += 1
            observation_id = pending["source_observation_id"]
            existing = pending_by_id.get(observation_id)
            if existing is None:
                pending_by_id[observation_id] = pending
            elif _canonical_bytes(existing) == _canonical_bytes(pending):
                counters["duplicate_input_source_observations"] += 1
            else:
                raise SourceObservationLedgerError(f"conflicting duplicate source observation ID: {observation_id}")

    outcome_index, conflicting_outcomes = _outcome_index(outcomes_path, counters)
    pending_rows = list(pending_by_id.values())
    settled_rows: list[dict[str, Any]] = []
    unusable_rows: list[dict[str, Any]] = []
    void_rows: list[dict[str, Any]] = []
    for pending in pending_rows:
        eligibility = pending.get("source_correctness_eligibility")
        if eligibility != _ELIGIBLE_TARGET_PROOF:
            reason = str(eligibility or "unusable_legacy_target_unproven")
            counters[reason] += 1
            unusable_rows.append(_unusable_row(pending, reason))
            continue
        identity_key = _identity_key(pending["canonical_input_sha256"], pending["decision_key"])
        if identity_key in conflicting_outcomes:
            unusable_rows.append(_unusable_row(pending, "conflicting_exact_authoritative_outcome"))
            continue
        outcome = outcome_index.get(identity_key)
        if outcome is None:
            unusable_rows.append(_unusable_row(pending, "missing_exact_authoritative_outcome"))
            continue
        if outcome["official_outcome"] == "VOID":
            counters["void_resolution"] += 1
            void_rows.append(_void_row(pending, outcome))
            continue
        implied = _source_implied_outcome(pending)
        if implied is None:
            unusable_rows.append(_unusable_row(pending, "unavailable_source_implied_side", outcome=outcome))
            continue
        settled_rows.append({
            **pending,
            "schema_name": "settled_source_correctness",
            "schema_version": SCHEMA_VERSION,
            "source_implied_outcome": implied,
            "official_outcome": outcome["official_outcome"],
            "direction_correct": implied == outcome["official_outcome"],
            "eligible_for_reliability": True,
            "eligible_for_source_history": True,
            "availability_field": "settlement_ts",
            "settlement_ts": outcome["settlement_ts"],
            "known_after": outcome["settlement_ts"],
            "resolution_id": outcome["resolution_id"],
            "resolution_resolved_at": outcome.get("resolution_resolved_at"),
            "resolution_retrieved_at": outcome.get("resolution_retrieved_at"),
            "resolution_provenance": outcome.get("provenance"),
        })

    pending_bytes = _jsonl_bytes(pending_rows)
    settled_bytes = _jsonl_bytes(settled_rows)
    unusable_bytes = _jsonl_bytes(unusable_rows)
    void_bytes = _jsonl_bytes(void_rows)
    pending_path, settled_path, unusable_path, void_path = (
        target_dir / PENDING_FILENAME, target_dir / SETTLED_FILENAME, target_dir / UNSETTLED_FILENAME, target_dir / VOID_FILENAME,
    )
    pending_path.write_bytes(pending_bytes)
    settled_path.write_bytes(settled_bytes)
    unusable_path.write_bytes(unusable_bytes)
    void_path.write_bytes(void_bytes)
    metadata = {
        "schema_name": "source_observation_ledger_materialization",
        "schema_version": SCHEMA_VERSION,
        "mode": "offline_derived_source_observation_materialization",
        "non_mutating": True,
        "network_access": False,
        "router_selection_used": False,
        "settlement_join": "exact canonical_input_sha256 plus complete decision_key",
        "history_availability_field": "settlement_ts",
        "history_eligibility": "settlement_ts < future_decision_time",
        "inputs": {
            "replay_inputs_path": str(inputs_path), "replay_inputs_sha256": input_sha256,
            "finalized_outcomes_path": str(outcomes_path), "finalized_outcomes_sha256": outcome_sha256,
        },
        "counts": {
            **{key: int(counters[key]) for key in (
                "input_records_seen", "invalid_input_records", "inputs_without_source_observations",
                "source_observations_seen", "duplicate_input_source_observations", "outcome_records_seen",
                "invalid_outcomes", "duplicate_outcomes", "conflicting_outcomes",
                "unusable_legacy_target_mismatch", "unusable_legacy_target_unproven",
                "unusable_v1_target_mismatch", "unusable_v1_target_unproven",
                "unusable_v1_forecast_not_scoreable",
            )},
            "pending": len(pending_rows), "settled": len(settled_rows), "unsettled_or_unusable": len(unusable_rows),
            "void_resolution": len(void_rows),
        },
        "output_artifacts": {
            PENDING_FILENAME: {"sha256": hashlib.sha256(pending_bytes).hexdigest(), "record_count": len(pending_rows)},
            SETTLED_FILENAME: {"sha256": hashlib.sha256(settled_bytes).hexdigest(), "record_count": len(settled_rows)},
            UNSETTLED_FILENAME: {"sha256": hashlib.sha256(unusable_bytes).hexdigest(), "record_count": len(unusable_rows)},
            VOID_FILENAME: {"sha256": hashlib.sha256(void_bytes).hexdigest(), "record_count": len(void_rows)},
        },
    }
    metadata_path = target_dir / METADATA_FILENAME
    metadata_path.write_bytes(_canonical_bytes(metadata))
    return SourceObservationLedgerResult(target_dir, pending_path, settled_path, unusable_path, void_path, metadata_path, metadata)


def is_eligible_for_future_history(row: Mapping[str, Any], future_decision_time: str) -> bool:
    """Return whether a source-correctness row was authoritative before a decision.

    Retrieval/resolution timestamps are deliberately audit-only. A source row may
    contribute only after its authoritative settlement timestamp, strictly.
    """

    if row.get("eligible_for_source_history") is not True or row.get("source_correctness_eligibility") != _ELIGIBLE_TARGET_PROOF:
        return False
    settlement, future = _parse_time(row.get("settlement_ts")), _parse_time(future_decision_time)
    return settlement is not None and future is not None and settlement < future


def _pending_rows_for_input(row: Mapping[str, Any], identity: Mapping[str, Any]) -> list[dict[str, Any]]:
    snapshot = _weather_snapshot(row)
    if not snapshot:
        return []
    market = row.get("market") if isinstance(row.get("market"), Mapping) else {}
    metadata = market.get("market_metadata") if isinstance(market.get("market_metadata"), Mapping) else {}
    context_row = {
        "market_id": identity["market_id"],
        "question": market.get("question"),
        "observed_at": row.get("observed_at"),
        "market": dict(metadata),
        "weather_source_snapshot": snapshot,
    }
    source_records = _source_records(snapshot)
    rows: list[dict[str, Any]] = []
    for source_record in source_records:
        # Normalize each recorded source payload through the shared extractor,
        # one at a time. This preserves distinct target records that a
        # scoreboard aggregate would otherwise collapse for the same source.
        source_snapshot = _snapshot_with_only_source(snapshot, source_record)
        observations = extract_source_forecast_observations({**context_row, "weather_source_snapshot": source_snapshot})
        for observation in observations:
            target_proof = source_correctness_target_proof(
                source_record=source_record, snapshot=snapshot, market_date=observation.market.market_date,
            )
            source_as_of = _first_text(
                _mapping_text(source_record, "source_as_of", "as_of", "observed_at"),
                _mapping_text(snapshot, "source_as_of", "as_of", "source_timestamp", "fetched_at"),
                _nested_text(row, "source_inputs", "recorded_as_of"),
            )
            source_fetched_at = _first_text(
                _mapping_text(source_record, "source_fetched_at", "fetched_at"),
                _mapping_text(snapshot, "source_fetched_at", "fetched_at"),
            )
            target_identity = {
                "market_date": observation.market.market_date,
                "source_target": _first_text(
                    _nested_text(source_record, "target_mapping", "source_target_date"),
                    _mapping_text(source_record, *_TARGET_ALIASES),
                ),
                "forecast_start": _mapping_text(source_record, "forecast_start", "forecast_period_start", "period_start"),
                "forecast_end": _mapping_text(source_record, "forecast_end", "forecast_period_end", "period_end"),
                "forecast_temp_f": observation.forecast_temp_f,
            }
            source_payload = {
                "source_id": observation.source_id,
                "source_name": observation.source_name,
                "source_as_of": source_as_of,
                "source_fetched_at": source_fetched_at,
                "target_identity": target_identity,
            }
            source_payload_sha256 = hashlib.sha256(_canonical_bytes(source_payload)).hexdigest()
            source_observation_id = "sha256:" + hashlib.sha256(_canonical_bytes({
                "canonical_input_sha256": identity["canonical_input_sha256"], "decision_key": identity["decision_key"],
                "source_id": observation.source_id, "target_identity": target_identity,
                "canonical_source_payload_sha256": source_payload_sha256,
            })).hexdigest()
            rows.append({
                "schema_name": "pending_source_observation", "schema_version": SCHEMA_VERSION,
                "source_observation_id": source_observation_id,
                "canonical_input_sha256": identity["canonical_input_sha256"], "decision_key": identity["decision_key"],
                "market_id": identity["market_id"], "input_observed_at": identity["decision_key"]["observed_at_utc"],
                "observed_at": identity["decision_key"]["observed_at_utc"], "market_date": observation.market.market_date,
                "source_id": observation.source_id, "source_name": observation.source_name,
                "source_as_of": source_as_of, "source_fetched_at": source_fetched_at,
                "canonical_source_payload_sha256": source_payload_sha256, "target_identity": target_identity,
                "source_correctness_eligibility": target_proof["status"],
                "source_target_proof": target_proof,
                "forecast_temp_f": observation.forecast_temp_f, "threshold": observation.market.threshold,
                "question_side": observation.market.question_side, "city_id": observation.market.city_id or "unknown",
                "market_kind": observation.market.market_kind or "unknown", "contract_shape": observation.market.contract_shape or "unknown",
                "event_ticker": _first_text(_mapping_text(metadata, "event_ticker"), _nested_text(metadata, "market_route", "evidence", "event_ticker")),
                "source_missing_reasons": list(observation.missing_reasons) or None,
            })
    return rows


def source_correctness_target_proof(
    *, source_record: Mapping[str, Any], snapshot: Mapping[str, Any], market_date: Any,
) -> dict[str, Any]:
    """Classify recorded forecast target proof without rejecting the raw record.

    Legacy aliases can establish a narrow exact-date proof, but any retained
    conflicting target fails closed.  V1 evidence additionally requires its
    explicit forecast typing and source-local target mapping.
    """

    normalized_market_date = _normalized_date(market_date)
    version = source_record.get("source_evidence_version")
    mapping = source_record.get("target_mapping") if isinstance(source_record.get("target_mapping"), Mapping) else {}
    if str(version).strip() == "1":
        evidence_type = str(source_record.get("evidence_type") or "").strip().lower()
        if evidence_type != "forecast" or source_record.get("scoreable_forecast") is not True:
            return {"status": "unusable_v1_forecast_not_scoreable", "market_target_date": normalized_market_date, "source_target_date": None}
        market_targets = _normalized_targets(mapping.get("market_target_date"), source_record.get("market_target_date"))
        source_targets = _normalized_targets(
            mapping.get("source_target_date"), source_record.get("source_target_date"),
            source_record.get("target_forecast_date"), source_record.get("forecast_target"), source_record.get("forecast_date"),
        )
        mapped_market_date = market_targets[0] if market_targets else None
        source_target_date = source_targets[0] if source_targets else None
        if not mapping or not normalized_market_date or not market_targets or not source_targets:
            status = "unusable_v1_target_unproven"
        elif any(value != normalized_market_date for value in (*market_targets, *source_targets)):
            status = "unusable_v1_target_mismatch"
        else:
            status = _ELIGIBLE_TARGET_PROOF
        return {
            "status": status, "evidence_version": 1, "market_target_date": mapped_market_date,
            "source_target_date": source_target_date,
        }

    # Legacy records may prove only what the source itself attested.  In
    # particular, market-owned dates (including a snapshot's date and a
    # target_mapping.market_target_date) establish the market context but are
    # never evidence that a provider forecast targeted that date.
    del snapshot
    aliases = [mapping.get("source_target_date"), *[source_record.get(key) for key in _TARGET_ALIASES]]
    normalized_aliases = [value for value in (_normalized_date(alias) for alias in aliases) if value]
    source_target_date = normalized_aliases[0] if normalized_aliases else None
    if normalized_market_date and any(value != normalized_market_date for value in normalized_aliases):
        status = "unusable_legacy_target_mismatch"
    elif normalized_market_date and normalized_aliases:
        status = _ELIGIBLE_TARGET_PROOF
    else:
        status = "unusable_legacy_target_unproven"
    return {
        "status": status, "evidence_version": "legacy", "market_target_date": normalized_market_date,
        "source_target_date": source_target_date,
    }


def _normalized_date(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10]).isoformat()
    except ValueError:
        return None


def _normalized_targets(*values: Any) -> list[str]:
    texts = [str(value).strip() for value in values if value is not None and str(value).strip()]
    normalized = [_normalized_date(value) for value in texts]
    return [] if len(normalized) != len(texts) or any(value is None for value in normalized) else normalized


def _outcome_index(path: Path, counters: Counter[str]) -> tuple[dict[tuple[str, ...], dict[str, Any]], set[tuple[str, ...]]]:
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for _, row in _read_jsonl(path):
        counters["outcome_records_seen"] += 1
        normalized = _normalized_outcome(row)
        if normalized is None:
            counters["invalid_outcomes"] += 1
            continue
        grouped[_identity_key(normalized["canonical_input_sha256"], normalized["decision_key"])].append(normalized)
    accepted: dict[tuple[str, ...], dict[str, Any]] = {}
    conflicts: set[tuple[str, ...]] = set()
    for key, rows in grouped.items():
        unique = {_canonical_bytes(row) for row in rows}
        if len(unique) == 1:
            accepted[key] = rows[0]
            counters["duplicate_outcomes"] += len(rows) - 1
        else:
            conflicts.add(key)
            counters["conflicting_outcomes"] += len(rows)
    return accepted, conflicts


def _normalized_outcome(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(row, Mapping):
        return None
    identity = _decision_identity(row)
    status = str(row.get("market_status") or "finalized").lower()
    if identity is None or status not in {"finalized", "void_resolution"}:
        return None
    official = str(row.get("official_outcome") or "").upper()
    settlement = _text(row.get("settlement_ts"))
    resolution_id = _text(row.get("resolution_id"))
    if official not in {"YES", "NO", "VOID"} or not settlement or _parse_time(settlement) is None or not resolution_id:
        return None
    if (status == "void_resolution") != (official == "VOID"):
        return None
    return {
        **identity, "official_outcome": official, "settlement_ts": settlement, "resolution_id": resolution_id,
        "resolution_resolved_at": _first_text(_text(row.get("resolved_at")), _nested_text(row, "resolution", "resolved_at")),
        "resolution_retrieved_at": _first_text(_text(row.get("retrieved_at")), _nested_text(row, "provenance", "retrieved_at")),
        "provenance": row.get("provenance") if isinstance(row.get("provenance"), Mapping) else None,
    }


def _unusable_row(pending: Mapping[str, Any], reason: str, *, outcome: Mapping[str, Any] | None = None) -> dict[str, Any]:
    row = {**pending, "schema_name": "unsettled_or_unusable_source_observation", "schema_version": SCHEMA_VERSION, "disposition_reason": reason}
    if outcome is not None:
        row.update({
            "authoritative_resolution_present": True, "settlement_ts": outcome["settlement_ts"],
            "resolution_id": outcome["resolution_id"], "resolution_resolved_at": outcome.get("resolution_resolved_at"),
        })
    return row


def _void_row(pending: Mapping[str, Any], outcome: Mapping[str, Any]) -> dict[str, Any]:
    """Retain a settled VOID receipt without scoring or source-history eligibility."""
    return {
        **pending,
        "schema_name": "void_source_observation",
        "schema_version": SCHEMA_VERSION,
        "disposition_reason": "void_resolution",
        "official_outcome": "VOID",
        "eligible_for_reliability": False,
        "eligible_for_source_history": False,
        "settlement_ts": outcome["settlement_ts"],
        "resolution_id": outcome["resolution_id"],
        "resolution_resolved_at": outcome.get("resolution_resolved_at"),
        "resolution_retrieved_at": outcome.get("resolution_retrieved_at"),
        "resolution_provenance": outcome.get("provenance"),
    }


def _source_implied_outcome(row: Mapping[str, Any]) -> str | None:
    direction = infer_direction_from_value(_number(row.get("forecast_temp_f")), _number(row.get("threshold")))
    return infer_predicted_outcome(str(row.get("question_side") or ""), direction)


def _decision_identity(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(row, Mapping):
        return None
    digest, key, market_id = row.get("canonical_input_sha256"), row.get("decision_key"), row.get("market_id")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest) or not isinstance(key, Mapping):
        return None
    normalized_key = {field: _text(key.get(field)) for field in _IDENTITY_FIELDS}
    if any(value is None for value in normalized_key.values()) or normalized_key["market_id"] != _text(market_id):
        return None
    return {"canonical_input_sha256": digest.lower(), "decision_key": normalized_key, "market_id": normalized_key["market_id"]}


def _identity_key(digest: str, key: Mapping[str, Any]) -> tuple[str, ...]:
    return (digest, *(str(key[field]) for field in _IDENTITY_FIELDS))


def _weather_snapshot(row: Mapping[str, Any]) -> Mapping[str, Any]:
    inputs = row.get("source_inputs") if isinstance(row.get("source_inputs"), Mapping) else {}
    context = inputs.get("source_context") if isinstance(inputs.get("source_context"), Mapping) else {}
    data = context.get("data") if isinstance(context.get("data"), Mapping) else {}
    snapshot = data.get("weather_source_snapshot")
    return snapshot if isinstance(snapshot, Mapping) else {}


def _source_records(snapshot: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    signal = snapshot.get("source_signal") if isinstance(snapshot.get("source_signal"), Mapping) else {}
    signal_data = signal.get("data") if isinstance(signal.get("data"), Mapping) else {}
    records: list[Mapping[str, Any]] = []
    for value in (snapshot.get("sources"), signal_data.get("source_details"), signal_data.get("sources")):
        if isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    records.append(item)
    return records


def _snapshot_with_only_source(snapshot: Mapping[str, Any], source_record: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(snapshot)
    result["sources"] = [dict(source_record)]
    signal = result.get("source_signal")
    if isinstance(signal, Mapping):
        signal = dict(signal)
        data = signal.get("data")
        if isinstance(data, Mapping):
            data = dict(data)
            data.pop("sources", None)
            data.pop("source_details", None)
            signal["data"] = data
        result["source_signal"] = signal
    return result


def _prepare_output_dir(path: Path) -> None:
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            raise SourceObservationLedgerError("output directory must be new or empty; refusing to overwrite artifacts")
    else:
        path.mkdir(parents=True, exist_ok=False)


def _read_jsonl(path: Path) -> Iterable[tuple[int, Mapping[str, Any] | None]]:
    with path.open(encoding="utf-8") as stream:
        for line_number, raw in enumerate(stream, start=1):
            if not raw.strip():
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = None
            yield line_number, payload if isinstance(payload, Mapping) else None


def _jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(_canonical_bytes(row) + b"\n" for row in rows)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and parsed not in (float("inf"), float("-inf")) else None


def _text(value: Any) -> str | None:
    return str(value).strip() if value is not None and str(value).strip() else None


def _mapping_text(mapping: Mapping[str, Any], *keys: str) -> str | None:
    return _first_text(*(_text(mapping.get(key)) for key in keys))


def _nested_text(mapping: Mapping[str, Any], *keys: str) -> str | None:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return _text(current)


def _first_text(*values: str | None) -> str | None:
    return next((value for value in values if value), None)
