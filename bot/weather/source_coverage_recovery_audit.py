"""Offline coverage audit for source observations awaiting usable outcomes.

This module is deliberately an audit gate, not a resolver or outcome binder.
It only reads supplied JSONL evidence and writes fresh derived artifacts.  In
particular, it never invokes a scoreboard client or treats a current lookup as
historical source evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 1
COVERAGE_QUEUE_FILENAME = "coverage_backfill_queue.jsonl"
SOURCE_ALIGNMENT_FILENAME = "source_target_alignment_audit.jsonl"
METADATA_FILENAME = "run_metadata.json"
_IDENTITY_FIELDS = (
    "shared_snapshot_id", "shared_candidate_id", "market_id", "observed_at_utc", "raw_row_sha256",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)


class SourceCoverageRecoveryAuditError(ValueError):
    """The supplied evidence cannot be audited as a derived-only run."""


@dataclass(frozen=True, slots=True)
class SourceCoverageRecoveryAuditResult:
    output_dir: Path
    coverage_queue_path: Path
    source_alignment_path: Path
    metadata_path: Path
    metadata: dict[str, Any]


def audit_source_coverage_recovery(
    *, observation_paths: Iterable[str | Path], strict_resolution_path: str | Path, output_dir: str | Path,
) -> SourceCoverageRecoveryAuditResult:
    """Audit exact-resolution coverage without binding or fetching outcomes.

    The candidate resolution ledger must itself contain the full sealed decision
    identity.  A market-only row is not a valid substitute, even if its market
    id happens to match.  Output queue rows preserve observation and ledger
    provenance, but intentionally omit outcome values so this audit cannot be
    mistaken for a settlement binder.
    """

    paths = [Path(path).resolve() for path in observation_paths]
    resolution_path = Path(strict_resolution_path).resolve()
    target_dir = Path(output_dir).resolve()
    if not paths or any(not path.is_file() for path in paths) or not resolution_path.is_file():
        raise SourceCoverageRecoveryAuditError("observation paths and strict resolution path must be readable files")
    _prepare_output_dir(target_dir)

    observations, observation_stats = _read_observations(paths)
    resolutions, resolution_stats = _read_resolutions(resolution_path)
    grouped: dict[tuple[str, ...], list[_ResolutionCandidate]] = defaultdict(list)
    for resolution in resolutions:
        grouped[resolution.identity].append(resolution)

    coverage_counts: Counter[str] = Counter()
    queue: list[dict[str, Any]] = []
    alignment: list[dict[str, Any]] = []
    for observation in observations:
        candidates = grouped.get(observation.identity, [])
        classification, selected = _coverage_classification(candidates)
        coverage_counts[classification] += 1
        queue.append(_coverage_queue_row(observation, classification, selected))
        if _is_source_side_unavailable(observation.row):
            alignment.append(_source_alignment_row(observation))

    queue_bytes = _jsonl_bytes(queue)
    alignment_bytes = _jsonl_bytes(alignment)
    queue_path = target_dir / COVERAGE_QUEUE_FILENAME
    alignment_path = target_dir / SOURCE_ALIGNMENT_FILENAME
    queue_path.write_bytes(queue_bytes)
    alignment_path.write_bytes(alignment_bytes)
    metadata = {
        "schema_name": "source_coverage_recovery_audit",
        "schema_version": SCHEMA_VERSION,
        "mode": "offline_derived_exact_identity_coverage_audit",
        "non_mutating": True,
        "network_access": False,
        "resolver_invoked": False,
        "outcome_binding_performed": False,
        "strict_resolution_requirement": "complete canonical_input_sha256 plus decision_key identity",
        "inputs": {
            "observation_paths": [str(path) for path in paths],
            "observation_sha256": {str(path): _sha256_file(path) for path in paths},
            "strict_resolution_path": str(resolution_path),
            "strict_resolution_sha256": _sha256_file(resolution_path),
        },
        "observation_counts": observation_stats,
        "resolution_counts": resolution_stats,
        "coverage_counts": {
            "observations_seen": len(observations),
            "invalid_observations": observation_stats["invalid_observations"],
            **{key: int(coverage_counts[key]) for key in (
                "exact_resolved", "missing_exact_resolution", "exact_resolution_ambiguity_or_conflict",
                "invalid_settlement_timestamp",
            )},
        },
        "source_alignment_counts": {
            "source_side_unavailable_rows": len(alignment),
            **{key: sum(1 for row in alignment if row["target_alignment"] == key) for key in (
                "target_matches_market_date", "source_target_date_mismatch", "source_target_nonforecast_shape",
                "source_target_missing", "market_date_missing",
            )},
        },
        "output_artifacts": {
            COVERAGE_QUEUE_FILENAME: {"sha256": hashlib.sha256(queue_bytes).hexdigest(), "record_count": len(queue)},
            SOURCE_ALIGNMENT_FILENAME: {"sha256": hashlib.sha256(alignment_bytes).hexdigest(), "record_count": len(alignment)},
        },
    }
    metadata_path = target_dir / METADATA_FILENAME
    metadata_path.write_bytes(_canonical_bytes(metadata))
    return SourceCoverageRecoveryAuditResult(target_dir, queue_path, alignment_path, metadata_path, metadata)


@dataclass(frozen=True, slots=True)
class _Observation:
    row: Mapping[str, Any]
    identity: tuple[str, ...]
    line_number: int
    source_path: Path
    raw_row_sha256: str


@dataclass(frozen=True, slots=True)
class _ResolutionCandidate:
    row: Mapping[str, Any]
    identity: tuple[str, ...]
    line_number: int
    raw_row_sha256: str


def _read_observations(paths: Iterable[Path]) -> tuple[list[_Observation], dict[str, int]]:
    observations: list[_Observation] = []
    stats: Counter[str] = Counter()
    for path in paths:
        for line_number, row, raw in _read_jsonl(path):
            stats["records_seen"] += 1
            identity = _identity(row)
            if identity is None:
                stats["invalid_observations"] += 1
                continue
            observations.append(_Observation(row, identity, line_number, path, hashlib.sha256(raw).hexdigest()))
    return observations, {key: int(stats[key]) for key in ("records_seen", "invalid_observations")}


def _read_resolutions(path: Path) -> tuple[list[_ResolutionCandidate], dict[str, int]]:
    resolutions: list[_ResolutionCandidate] = []
    stats: Counter[str] = Counter()
    for line_number, row, raw in _read_jsonl(path):
        stats["records_seen"] += 1
        identity = _identity(row)
        if identity is None:
            stats["invalid_identity_records"] += 1
            continue
        if not _has_strict_outcome_shape(row):
            stats["invalid_strict_resolution_records"] += 1
            continue
        resolutions.append(_ResolutionCandidate(row, identity, line_number, hashlib.sha256(raw).hexdigest()))
    return resolutions, {key: int(stats[key]) for key in (
        "records_seen", "invalid_identity_records", "invalid_strict_resolution_records",
    )}


def _coverage_classification(candidates: list[_ResolutionCandidate]) -> tuple[str, _ResolutionCandidate | None]:
    if not candidates:
        return "missing_exact_resolution", None
    if len(candidates) != 1:
        return "exact_resolution_ambiguity_or_conflict", None
    candidate = candidates[0]
    if _parse_timestamp(candidate.row.get("settlement_ts")) is None:
        return "invalid_settlement_timestamp", None
    return "exact_resolved", candidate


def _coverage_queue_row(
    observation: _Observation, classification: str, resolution: _ResolutionCandidate | None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "schema_name": "source_coverage_backfill_queue",
        "schema_version": SCHEMA_VERSION,
        "coverage_classification": classification,
        "source_observation_id": observation.row.get("source_observation_id"),
        "canonical_input_sha256": observation.identity[0],
        "decision_key": _identity_mapping(observation.identity),
        "market_id": observation.identity[3],
        "observation_provenance": {
            "source_path": str(observation.source_path),
            "source_line_number": observation.line_number,
            "raw_observation_row_sha256": observation.raw_row_sha256,
            "source_id": observation.row.get("source_id"),
            "disposition_reason": observation.row.get("disposition_reason"),
        },
    }
    if resolution is not None:
        row["strict_resolution_reference"] = {
            "source_line_number": resolution.line_number,
            "raw_resolution_row_sha256": resolution.raw_row_sha256,
            "resolution_id": resolution.row.get("resolution_id"),
            "settlement_ts": resolution.row.get("settlement_ts"),
        }
    return row


def _source_alignment_row(observation: _Observation) -> dict[str, Any]:
    row = observation.row
    target = row.get("target_identity") if isinstance(row.get("target_identity"), Mapping) else {}
    market_date = _text(row.get("market_date")) or _text(target.get("market_date"))
    source_target = _text(target.get("source_target"))
    if market_date is None:
        status, recovery_reason = "market_date_missing", "market_date_not_retained"
    elif source_target is None:
        status, recovery_reason = "source_target_missing", "no_recorded_source_target"
    elif source_target == "current_observation":
        status, recovery_reason = "source_target_nonforecast_shape", "recorded_source_is_current_observation_not_forecast"
    elif source_target != market_date:
        status, recovery_reason = "source_target_date_mismatch", "no_exact_recorded_source_target_for_market_date"
    else:
        status, recovery_reason = "target_matches_market_date", "recorded_target_is_exact"
    viable = status == "target_matches_market_date" and _finite_number(row.get("forecast_temp_f")) and _finite_number(row.get("threshold"))
    return {
        "schema_name": "source_target_alignment_audit",
        "schema_version": SCHEMA_VERSION,
        "source_observation_id": row.get("source_observation_id"),
        "canonical_input_sha256": observation.identity[0],
        "decision_key": _identity_mapping(observation.identity),
        "market_id": observation.identity[3],
        "market_date": market_date,
        "contract_shape": row.get("contract_shape"),
        "market_target_shape": "forecast_for_calendar_date" if market_date is not None else "unknown",
        "source_id": row.get("source_id"),
        "recorded_source_target": source_target,
        "source_target_shape": _source_target_shape(source_target),
        "recorded_forecast_start": target.get("forecast_start"),
        "recorded_forecast_end": target.get("forecast_end"),
        "target_alignment": status,
        "recovery_possible_from_retained_fields": viable,
        "recovery_reason": "retained_exact_target_and_values_available" if viable else recovery_reason,
        "timezone_conversion_applied": False,
        "forecast_fabricated": False,
        "observation_provenance": {
            "source_path": str(observation.source_path),
            "source_line_number": observation.line_number,
            "raw_observation_row_sha256": observation.raw_row_sha256,
            "source_missing_reasons": row.get("source_missing_reasons"),
        },
    }


def _source_target_shape(source_target: str | None) -> str:
    if source_target == "current_observation":
        return "current_observation"
    if source_target is None:
        return "missing"
    return "calendar_date" if _parse_date(source_target) else "unrecognized"


def _is_source_side_unavailable(row: Mapping[str, Any]) -> bool:
    reason = _text(row.get("disposition_reason")) or ""
    return reason == "unavailable_source_implied_side" or bool(row.get("source_missing_reasons"))


def _has_strict_outcome_shape(row: Mapping[str, Any] | None) -> bool:
    if not isinstance(row, Mapping) or str(row.get("market_status") or "").lower() != "finalized":
        return False
    return isinstance(row.get("resolution_id"), str) and bool(row["resolution_id"].strip()) and str(row.get("official_outcome") or "").upper() in {"YES", "NO"}


def _identity(row: Mapping[str, Any] | None) -> tuple[str, ...] | None:
    if not isinstance(row, Mapping) or not isinstance(row.get("canonical_input_sha256"), str):
        return None
    digest = row["canonical_input_sha256"].lower()
    key = row.get("decision_key")
    if not _SHA256_RE.fullmatch(digest) or not isinstance(key, Mapping):
        return None
    values = tuple(_text(key.get(field)) for field in _IDENTITY_FIELDS)
    if any(value is None for value in values) or values[2] != _text(row.get("market_id")):
        return None
    if not _SHA256_RE.fullmatch(values[4]):
        return None
    return (digest, *values)  # type: ignore[arg-type]


def _identity_mapping(identity: tuple[str, ...]) -> dict[str, str]:
    return {field: identity[index + 1] for index, field in enumerate(_IDENTITY_FIELDS)}


def _read_jsonl(path: Path) -> Iterable[tuple[int, Mapping[str, Any] | None, bytes]]:
    with path.open("rb") as stream:
        for line_number, raw in enumerate(stream, start=1):
            raw_line = raw.rstrip(b"\r\n")
            if not raw_line.strip():
                continue
            try:
                row = json.loads(raw_line)
            except json.JSONDecodeError:
                row = None
            yield line_number, row if isinstance(row, Mapping) else None, raw_line


def _prepare_output_dir(path: Path) -> None:
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            raise SourceCoverageRecoveryAuditError(f"output directory must be new and empty: {path}")
    else:
        path.mkdir(parents=True)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return timestamp if timestamp.tzinfo is not None else None


def _parse_date(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _finite_number(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return number == number and abs(number) != float("inf")


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(_canonical_bytes(row) + b"\n" for row in rows)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
