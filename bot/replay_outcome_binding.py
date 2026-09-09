"""Bind sealed sanitized replay inputs to authoritative strict resolutions.

This is an offline, derived-data-only boundary.  It deliberately creates a
separate outcome ledger keyed by each sealed decision identity; it never adds
settlement information to replay inputs or decision artifacts.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from bot.collector_paths import derived_reports_root
from bot.replay_decision_input import verify_replay_decision_input_record_v1
OUTCOMES_FILENAME = "finalized_replay_outcomes.jsonl"
UNBOUND_FILENAME = "unbound_replay_inputs.jsonl"
METADATA_FILENAME = "run_metadata.json"
_IDENTITY_FIELDS = (
    "shared_snapshot_id", "shared_candidate_id", "market_id", "observed_at_utc", "raw_row_sha256",
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ReplayOutcomeBindingResult:
    """Locations and metadata for one immutable replay-outcome binding run."""

    output_dir: Path
    outcomes_path: Path
    unbound_path: Path
    metadata_path: Path
    metadata: dict[str, Any]


def bind_replay_finalized_outcomes(
    *, replay_inputs_path: str | Path, strict_resolutions_path: str | Path, output_dir: str | Path,
    storage_root: str | Path | None = None,
) -> ReplayOutcomeBindingResult:
    """Write exact-decision outcome bindings from unambiguous strict resolutions.

    Resolution lookup is intentionally market-id-only.  Each valid sealed
    input observation receives its own output row, so repeated observations are
    preserved in source order.  Missing, malformed, or ambiguous authority is
    reported as unbound rather than guessed or silently substituted.
    """
    inputs_path = Path(replay_inputs_path).resolve()
    resolutions_path = Path(strict_resolutions_path).resolve()
    if not inputs_path.is_file() or not resolutions_path.is_file():
        raise ValueError("replay inputs and strict resolutions must be readable files")
    target_dir = _prepare_output_dir(output_dir, storage_root=storage_root)
    inputs_sha256 = _sha256_file(inputs_path)
    resolutions_sha256 = _sha256_file(resolutions_path)

    resolution_rows = list(_read_jsonl_with_raw(resolutions_path))
    resolution_index, resolution_stats = _index_strict_resolutions(resolution_rows)
    outcomes: list[dict[str, Any]] = []
    unbound: list[dict[str, Any]] = []
    input_stats: Counter[str] = Counter()
    binding_stats: Counter[str] = Counter()
    for _, row, _ in _read_jsonl_with_raw(inputs_path):
        if not verify_replay_decision_input_record_v1(row):
            input_stats["invalid_input_records"] += 1
            binding_stats["invalid"] += 1
            continue
        identity = _decision_identity(row)
        if identity is None:
            input_stats["invalid_input_records"] += 1
            binding_stats["invalid"] += 1
            continue
        input_stats["valid_input_records"] += 1
        binding_stats["candidate"] += 1
        market_id = identity["market_id"]
        resolution = resolution_index.accepted.get(market_id)
        if resolution is not None:
            outcomes.append(_bound_outcome_row(identity, resolution, strict_resolution_source_sha256=resolutions_sha256))
            binding_stats["bound"] += 1
            continue
        binding_stats["unbound"] += 1
        if market_id in resolution_index.ambiguous_market_ids:
            binding_stats["ambiguous"] += 1
            reason = "ambiguous_market_id_resolution"
        elif market_id in resolution_index.invalid_market_ids:
            reason = "invalid_authoritative_resolution"
        else:
            input_stats["missing_resolution_records"] += 1
            reason = "missing_authoritative_resolution"
        unbound.append(_unbound_row(identity, reason))

    outcomes_path = target_dir / OUTCOMES_FILENAME
    unbound_path = target_dir / UNBOUND_FILENAME
    metadata_path = target_dir / METADATA_FILENAME
    outcomes_bytes = _jsonl_bytes(outcomes)
    unbound_bytes = _jsonl_bytes(unbound)
    outcomes_path.write_bytes(outcomes_bytes)
    unbound_path.write_bytes(unbound_bytes)
    binding_counts = {
        key: int(binding_stats[key]) for key in ("candidate", "bound", "unbound", "ambiguous", "invalid")
    }
    metadata = {
        "schema_name": "replay_finalized_outcome_binding",
        "schema_version": 1,
        "mode": "offline_derived_exact_decision_outcome_binding",
        "non_mutating": True,
        "network_access": False,
        "outcome_available_only_after_sealed_decision": True,
        "inputs": {
            "replay_inputs_path": str(inputs_path),
            "replay_inputs_sha256": inputs_sha256,
            "strict_resolutions_path": str(resolutions_path),
            "strict_resolutions_sha256": resolutions_sha256,
        },
        "input_counts": {
            "records_seen": input_stats["valid_input_records"] + input_stats["invalid_input_records"],
            "valid_records": input_stats["valid_input_records"],
            "invalid_records": input_stats["invalid_input_records"],
            "missing_resolution_records": input_stats["missing_resolution_records"],
        },
        "resolution_counts": resolution_stats,
        "binding_counts": binding_counts,
        "output_hash": hashlib.sha256(outcomes_bytes).hexdigest(),
        "output_artifacts": {
            OUTCOMES_FILENAME: {"sha256": hashlib.sha256(outcomes_bytes).hexdigest(), "record_count": len(outcomes)},
            UNBOUND_FILENAME: {"sha256": hashlib.sha256(unbound_bytes).hexdigest(), "record_count": len(unbound)},
        },
    }
    metadata_path.write_bytes(_canonical_json_bytes(metadata))
    return ReplayOutcomeBindingResult(target_dir, outcomes_path, unbound_path, metadata_path, metadata)


@dataclass(frozen=True, slots=True)
class _ResolutionIndex:
    accepted: dict[str, dict[str, Any]]
    ambiguous_market_ids: set[str]
    invalid_market_ids: set[str]


def _index_strict_resolutions(
    rows: Iterable[tuple[int, Mapping[str, Any] | None, bytes]],
) -> tuple[_ResolutionIndex, dict[str, int]]:
    candidates: dict[str, tuple[int, Mapping[str, Any] | None, bytes]] = {}
    ambiguous_counts: Counter[str] = Counter()
    stats: Counter[str] = Counter()
    for line_number, row, raw_line in rows:
        stats["records_seen"] += 1
        market_id = row.get("market_id") if isinstance(row, Mapping) else None
        if not isinstance(market_id, str) or not market_id:
            stats["invalid_records"] += 1
            continue
        if market_id in ambiguous_counts:
            ambiguous_counts[market_id] += 1
            continue
        if market_id in candidates:
            candidates.pop(market_id)
            ambiguous_counts[market_id] = 2
            continue
        candidates[market_id] = (line_number, row, raw_line)

    accepted: dict[str, dict[str, Any]] = {}
    ambiguous_market_ids = set(ambiguous_counts)
    invalid_market_ids: set[str] = set()
    for market_id, (line_number, row, raw_line) in candidates.items():
        normalized = _normalize_strict_resolution(row, market_id)
        if normalized is None:
            invalid_market_ids.add(market_id)
            stats["invalid_records"] += 1
            continue
        accepted[market_id] = {
            **normalized,
            "source_line_number": line_number,
            "raw_row_sha256": hashlib.sha256(raw_line).hexdigest(),
        }
        stats["accepted_records"] += 1
    stats["ambiguous_market_ids"] = len(ambiguous_market_ids)
    stats["ambiguous_resolution_records"] = sum(ambiguous_counts.values())
    return _ResolutionIndex(accepted, ambiguous_market_ids, invalid_market_ids), {
        key: int(stats[key])
        for key in ("records_seen", "accepted_records", "invalid_records", "ambiguous_market_ids", "ambiguous_resolution_records")
    }


def _normalize_strict_resolution(row: Mapping[str, Any] | None, market_id: str) -> dict[str, str] | None:
    if not isinstance(row, Mapping) or str(row.get("market_status") or "").lower() != "finalized":
        return None
    if row.get("requested_market_id") != market_id or row.get("returned_market_id") != market_id:
        return None
    settlement = _timestamp(_parse_datetime(row.get("settlement_ts")))
    resolution_id = row.get("resolution_id")
    if settlement is None or not isinstance(resolution_id, str) or not resolution_id:
        return None
    values: set[str] = set()
    kalshi = row.get("kalshi_result")
    if isinstance(kalshi, str) and kalshi.lower() in {"yes", "no", "void"}:
        values.add(kalshi.upper())
    resolution = row.get("resolution")
    outcome = resolution.get("outcome") if isinstance(resolution, Mapping) else None
    if isinstance(outcome, str) and outcome.upper() in {"YES", "NO", "VOID"}:
        values.add(outcome.upper())
    if len(values) != 1:
        return None
    return {"official_outcome": values.pop(), "settlement_ts": settlement, "resolution_id": resolution_id}


def _decision_identity(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(row, Mapping):
        return None
    digest = row.get("canonical_input_sha256")
    key = row.get("decision_key")
    market_id = row.get("market_id")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest) or not isinstance(key, Mapping):
        return None
    identity_key = {field: key.get(field) for field in _IDENTITY_FIELDS}
    if any(not isinstance(value, str) or not value for value in identity_key.values()):
        return None
    if identity_key["market_id"] != market_id:
        return None
    return {"canonical_input_sha256": digest.lower(), "decision_key": identity_key, "market_id": market_id}


def _bound_outcome_row(
    identity: Mapping[str, Any], resolution: Mapping[str, Any], *, strict_resolution_source_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_name": "replay_finalized_outcome_binding",
        "schema_version": 1,
        "market_status": "void_resolution" if resolution["official_outcome"] == "VOID" else "finalized",
        "canonical_input_sha256": identity["canonical_input_sha256"],
        "decision_key": identity["decision_key"],
        "market_id": identity["market_id"],
        "official_outcome": resolution["official_outcome"],
        "settlement_ts": resolution["settlement_ts"],
        "resolution_id": resolution["resolution_id"],
        "provenance": {
            "strict_resolution_source_sha256": strict_resolution_source_sha256,
            "strict_resolution_source_line_number": resolution["source_line_number"],
            "raw_resolution_row_sha256": resolution["raw_row_sha256"],
        },
    }


def _unbound_row(identity: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return {
        "schema_name": "replay_finalized_outcome_binding_unbound",
        "schema_version": 1,
        "canonical_input_sha256": identity["canonical_input_sha256"],
        "decision_key": identity["decision_key"],
        "market_id": identity["market_id"],
        "binding_reason": reason,
    }


def _read_jsonl_with_raw(path: Path) -> Iterable[tuple[int, Mapping[str, Any] | None, bytes]]:
    with path.open("rb") as stream:
        for line_number, raw in enumerate(stream, start=1):
            raw_line = raw.rstrip(b"\r\n")
            if not raw_line.strip():
                continue
            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError:
                payload = None
            yield line_number, payload if isinstance(payload, Mapping) else None, raw_line


def _prepare_output_dir(value: str | Path, *, storage_root: str | Path | None = None) -> Path:
    target_dir = Path(value).resolve()
    root = derived_reports_root(storage_root).resolve()
    if target_dir == root or root not in target_dir.parents:
        raise ValueError(f"output directory must be under {root}")
    if target_dir.exists():
        if not target_dir.is_dir() or any(target_dir.iterdir()):
            raise ValueError("output directory must be new or empty; refusing to overwrite artifacts")
    else:
        target_dir.mkdir(parents=True, exist_ok=False)
    return target_dir


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


def _canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(_canonical_json_bytes(row) for row in rows)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["ReplayOutcomeBindingResult", "bind_replay_finalized_outcomes"]
