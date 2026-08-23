"""Validated, read-only registry entries for historical source evidence."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


class SourceHistoryManifestError(ValueError):
    """A history manifest is malformed, unsafe, or no longer matches its evidence."""


@dataclass(frozen=True, slots=True)
class SourceHistoryManifest:
    manifest_path: Path
    source_ledger_path: Path
    strict_resolution_path: Path
    raw_archive_path: Path
    replay_index_path: Path
    replay_manifest_path: Path
    historical_counterfactual_only: bool
    non_mutating: bool
    join_key: str
    eligibility_filter: str
    availability_field: str
    sha256: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class StrictSourceHistoryCollapseResult:
    output_dir: Path
    collapsed_path: Path
    metadata_path: Path
    metadata: Mapping[str, Any]


_REQUIRED_PATHS = {
    "source_ledger": "source_ledger_path",
    "strict_resolution": "strict_resolution_path",
    "raw_archive": "raw_archive_path",
    "replay_index": "replay_index_path",
    "replay_manifest": "replay_manifest_path",
}


def load_source_history_manifest(path: str | Path) -> SourceHistoryManifest:
    """Load a paper-only historical evidence manifest after hash verification."""
    manifest_path = Path(path).expanduser().resolve()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SourceHistoryManifestError(f"history manifest does not exist: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise SourceHistoryManifestError(f"history manifest is not valid JSON: {manifest_path}") from exc
    if not isinstance(payload, Mapping):
        raise SourceHistoryManifestError("history manifest must be a JSON object")
    if payload.get("schema_name") != "source_history_manifest" or payload.get("schema_version") != 1:
        raise SourceHistoryManifestError("unsupported source history manifest schema")
    if payload.get("historical_counterfactual_only") is not True:
        raise SourceHistoryManifestError("history manifest must be historical_counterfactual_only")
    if payload.get("non_mutating") is not True:
        raise SourceHistoryManifestError("history manifest must be non_mutating")
    if payload.get("join_key") != "market_id":
        raise SourceHistoryManifestError("history manifest join_key must be market_id")
    if payload.get("availability_field") != "settlement_ts":
        raise SourceHistoryManifestError("history manifest availability_field must be settlement_ts")
    if not isinstance(payload.get("eligibility_filter"), str) or not payload["eligibility_filter"].strip():
        raise SourceHistoryManifestError("history manifest must declare an eligibility_filter")
    digests = payload.get("sha256")
    if not isinstance(digests, Mapping):
        raise SourceHistoryManifestError("history manifest sha256 must be an object")

    resolved_paths: dict[str, Path] = {}
    for digest_key, path_key in _REQUIRED_PATHS.items():
        raw_path = payload.get(path_key)
        expected = digests.get(digest_key)
        if not isinstance(raw_path, str) or not raw_path:
            raise SourceHistoryManifestError(f"history manifest missing {path_key}")
        if not isinstance(expected, str) or len(expected) != 64:
            raise SourceHistoryManifestError(f"history manifest missing sha256 for {digest_key}")
        source_path = Path(raw_path).expanduser()
        if not source_path.is_absolute():
            source_path = manifest_path.parent / source_path
        source_path = source_path.resolve()
        if not source_path.is_file():
            raise SourceHistoryManifestError(f"history evidence file does not exist: {digest_key}")
        actual = _sha256(source_path)
        if actual != expected.lower():
            raise SourceHistoryManifestError(f"sha256 mismatch for {digest_key}: expected {expected}, got {actual}")
        resolved_paths[digest_key] = source_path

    return SourceHistoryManifest(
        manifest_path=manifest_path,
        source_ledger_path=resolved_paths["source_ledger"],
        strict_resolution_path=resolved_paths["strict_resolution"],
        raw_archive_path=resolved_paths["raw_archive"],
        replay_index_path=resolved_paths["replay_index"],
        replay_manifest_path=resolved_paths["replay_manifest"],
        historical_counterfactual_only=True,
        non_mutating=True,
        join_key="market_id",
        eligibility_filter=payload["eligibility_filter"],
        availability_field="settlement_ts",
        sha256={key: str(value).lower() for key, value in digests.items()},
    )


def materialize_strict_source_history_collapse(
    settled_source_ledger_path: str | Path, output_dir: str | Path,
) -> StrictSourceHistoryCollapseResult:
    """Write a hash-bound, derived-only collapse artifact from settled observations."""
    source_path = Path(settled_source_ledger_path).expanduser().resolve()
    target_dir = Path(output_dir).expanduser().resolve()
    if not source_path.is_file():
        raise SourceHistoryManifestError(f"settled source ledger does not exist: {source_path}")
    if target_dir.exists() and any(target_dir.iterdir()):
        raise SourceHistoryManifestError(f"collapse output directory must be new or empty: {target_dir}")
    target_dir.mkdir(parents=True, exist_ok=True)
    raw_bytes = source_path.read_bytes()
    try:
        rows = [json.loads(line) for line in raw_bytes.splitlines() if line.strip()]
    except json.JSONDecodeError as exc:
        raise SourceHistoryManifestError("settled source ledger is not valid JSONL") from exc
    if any(not isinstance(row, Mapping) for row in rows):
        raise SourceHistoryManifestError("settled source ledger contains a non-object row")
    collapsed, counts = collapse_strict_source_history_rows(rows)
    collapsed_bytes = b"".join(
        json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        for row in collapsed
    )
    collapsed_path = target_dir / "strict_independent_source_history.jsonl"
    collapsed_path.write_bytes(collapsed_bytes)
    metadata = {
        "schema_name": "strict_source_history_collapse",
        "schema_version": 1,
        "mode": "offline_derived_strict_source_history_collapse",
        "non_mutating": True,
        "selection": "earliest_source_as_of_then_observed_at_then_source_record_sha256",
        "independence_key": "source_id,event_or_market,city_id,market_date,market_kind,contract_shape,question_side",
        "inputs": {"settled_source_ledger_path": str(source_path), "settled_source_ledger_sha256": hashlib.sha256(raw_bytes).hexdigest()},
        "counts": counts,
        "output_artifact": {"path": collapsed_path.name, "sha256": hashlib.sha256(collapsed_bytes).hexdigest(), "record_count": len(collapsed)},
    }
    metadata_path = target_dir / "strict_source_history_collapse.metadata.json"
    metadata_path.write_bytes(json.dumps(metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    return StrictSourceHistoryCollapseResult(target_dir, collapsed_path, metadata_path, metadata)


def collapse_strict_source_history_rows(rows: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Collapse repeat polls to one outcome-blind strict row per source/event unit."""
    representatives: dict[tuple[str, ...], dict[str, Any]] = {}
    metadata = {"rows_seen": 0, "strict_rows_seen": 0, "non_strict_rows_rejected": 0}
    for raw_row in rows:
        metadata["rows_seen"] += 1
        if not _is_strict_history_row(raw_row):
            metadata["non_strict_rows_rejected"] += 1
            continue
        metadata["strict_rows_seen"] += 1
        row = dict(raw_row)
        unit = _strict_independence_key(row)
        representative = representatives.get(unit)
        if representative is None or _strict_recorded_sort_key(row) < _strict_recorded_sort_key(representative):
            representatives[unit] = row
    collapsed = sorted(representatives.values(), key=lambda row: (_strict_recorded_sort_key(row), _strict_independence_key(row)))
    metadata["independent_rows"] = len(collapsed)
    metadata["collapsed_repeat_polls"] = metadata["strict_rows_seen"] - len(collapsed)
    return collapsed, metadata


def _is_strict_history_row(row: Mapping[str, Any]) -> bool:
    return row.get("eligible_for_source_history") is True and row.get("source_correctness_eligibility") == "eligible_strict_source_proof"


def _strict_independence_key(row: Mapping[str, Any]) -> tuple[str, ...]:
    """Use source plus settled event/contract dimensions; never use outcomes."""
    def text(value: Any) -> str:
        return str(value).strip().casefold() if value is not None else ""
    event = text(row.get("event_ticker") or row.get("event_id"))
    market = text(row.get("market_id") or row.get("market_ticker"))
    anchor = event or market
    if not anchor:
        anchor = text(row.get("source_observation_id") or row.get("observation_id"))
    return (
        text(row.get("source_id")), anchor,
        text(row.get("city_id")), text(row.get("market_date")),
        text(row.get("market_kind")), text(row.get("contract_shape")), text(row.get("question_side")),
    )


def _strict_recorded_sort_key(row: Mapping[str, Any]) -> tuple[datetime, datetime, str]:
    earliest = datetime.min.replace(tzinfo=timezone.utc)
    source_as_of = _parse_time(row.get("source_as_of")) or earliest
    observed_at = _parse_time(row.get("observed_at")) or earliest
    provenance = row.get("source_provenance")
    raw_hash = str(provenance.get("source_record_sha256") if isinstance(provenance, Mapping) else "")
    return source_as_of, observed_at, raw_hash


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
