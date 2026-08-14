"""Append-only, paper-only source-performance evidence materialization.

This module is deliberately downstream of the resolver: it reads immutable
collector snapshots and finalized local resolution artifacts, then appends
provenanced source observations. It never modifies decisions, wallets, or raw
collector data.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from bot.file_ops import atomic_write_json, locked_file
from bot.weather.source_reliability import build_source_outcome_ledger_rows_for_row

SCHEMA_NAME = "source_performance_outcome"
SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class SourcePerformanceMaterializationResult:
    output_path: Path
    report_path: Path | None
    snapshot_rows_read: int
    source_observations_seen: int
    finalized_resolution_rows_seen: int
    rows_appended: int
    duplicate_observation_rows: int
    unresolved_observation_rows: int
    invalid_resolution_rows: int


def materialize_source_performance_once(*, cohort_id: str, snapshot_paths: Iterable[str | Path], resolution_path: str | Path, output_path: str | Path, report_path: str | Path | None = None) -> SourcePerformanceMaterializationResult:
    """Append finalized source-performance evidence; all joins are exact market IDs."""
    sources = tuple(Path(p) for p in snapshot_paths)
    resolution_file, destination = Path(resolution_path), Path(output_path)
    resolutions, final_count, invalid = build_finalized_resolution_index(resolution_file)
    candidates: list[dict[str, Any]] = []
    rows_read = sources_seen = unresolved = 0
    for snapshot_path in sources:
        if not snapshot_path.exists():
            continue
        snapshot_hash = _sha256(snapshot_path)
        for line_number, raw in _jsonl(snapshot_path):
            rows_read += 1
            market_id = _market_id(raw)
            if not market_id or market_id not in resolutions:
                unresolved += 1
                continue
            resolution = resolutions[market_id]
            known_after = _settlement_ts(resolution) or _resolved_at(resolution)
            augmented = dict(raw)
            augmented["resolution"] = {
                "resolved_at": _resolved_at(resolution), "settlement_ts": _settlement_ts(resolution),
                "outcome": _outcome(resolution),
            }
            augmented["known_after"] = known_after
            for source_index, base in enumerate(build_source_outcome_ledger_rows_for_row(augmented, source_row_path=str(snapshot_path), source_line_number=line_number)):
                sources_seen += 1
                predicted = base.get("predicted_outcome")
                if predicted not in {"YES", "NO"}:
                    continue
                identity = source_observation_identity(cohort_id=cohort_id, raw=raw, market_id=market_id, source_id=str(base.get("source_id") or "unknown"), source_index=source_index, forecast=base.get("forecast_temp_f"))
                official = _outcome(resolution)
                row = dict(base)
                exclusions = [reason for reason in (base.get("exclusion_reasons") or []) if reason not in {"missing_actual_temp", "missing_actual_outcome"}]
                row.update({
                    "schema_name": SCHEMA_NAME, "schema_version": SCHEMA_VERSION, "cohort_id": cohort_id,
                    "source_observation_id": identity, "market_id": market_id,
                    "official_outcome": official, "actual_outcome": official,
                    "direction_correct": predicted == official,
                    "eligible_for_reliability": True, "exclusion_reasons": exclusions or None, "known_after": known_after,
                    "settlement_ts": resolution.get("settlement_ts"), "resolution_id": resolution.get("resolution_id"),
                    "resolution_source": _resolution_source(resolution), "resolution_resolved_at": _resolved_at(resolution),
                    "resolution_ledger_path": str(resolution_file), "source_snapshot_path": str(snapshot_path),
                    "source_snapshot_line_number": line_number, "source_snapshot_sha256": snapshot_hash,
                    "materialized_at": datetime.now(timezone.utc).isoformat(), "non_mutating": True,
                })
                candidates.append(row)
    destination.parent.mkdir(parents=True, exist_ok=True)
    existing = _existing_ids(destination)
    appended = duplicates = 0
    with locked_file(destination, "a+") as handle:
        handle.seek(0)
        existing |= {str(json.loads(line).get("source_observation_id")) for line in handle if line.strip()}
        for row in candidates:
            if row["source_observation_id"] in existing:
                duplicates += 1; continue
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            existing.add(row["source_observation_id"]); appended += 1
    report = {"schema_name":"source_performance_materializer_report", "schema_version":1, "cohort_id":cohort_id, "snapshot_paths":[str(p) for p in sources], "resolution_path":str(resolution_file), "output_path":str(destination), "snapshot_rows_read":rows_read, "source_observations_seen":sources_seen, "finalized_resolution_rows_seen":final_count, "rows_appended":appended, "duplicate_observation_rows":duplicates, "unresolved_observation_rows":unresolved, "invalid_resolution_rows":invalid, "non_mutating":True}
    report_file = Path(report_path) if report_path else None
    if report_file: atomic_write_json(report_file, report)
    return SourcePerformanceMaterializationResult(destination, report_file, rows_read, sources_seen, final_count, appended, duplicates, unresolved, invalid)


def build_finalized_resolution_index(path: Path) -> tuple[dict[str, dict[str, Any]], int, int]:
    index: dict[str, dict[str, Any]] = {}; invalid = 0
    for _, row in _jsonl(path):
        market_id, outcome, resolved = _market_id(row), _outcome(row), _resolved_at(row)
        if not market_id or outcome not in {"YES", "NO"} or not resolved or str(row.get("market_status") or "").lower() != "finalized" or market_id in index:
            invalid += 1; continue
        index[market_id] = row
    return index, len(index), invalid


def source_observation_identity(*, cohort_id: str, raw: Mapping[str, Any], market_id: str, source_id: str, source_index: int, forecast: Any) -> str:
    payload = {"cohort_id":cohort_id,"market_id":market_id,"shared_candidate_id":raw.get("shared_candidate_id"),"shared_snapshot_id":raw.get("shared_snapshot_id"),"observed_at":raw.get("observed_at"),"source_id":source_id,"source_index":source_index,"forecast":forecast}
    return "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",",":"), default=str).encode()).hexdigest()


def _jsonl(path: Path):
    if not path.exists(): return
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if line.strip():
                try: yield number, json.loads(line)
                except json.JSONDecodeError: continue

def _market_id(row: Mapping[str, Any]) -> str | None:
    for key in ("market_id", "ticker"):
        if row.get(key): return str(row[key])
    return None

def _outcome(row: Mapping[str, Any]) -> str | None:
    value = row.get("kalshi_result") or (row.get("resolution") or {}).get("outcome")
    value = str(value or "").upper()
    return value if value in {"YES", "NO"} else None

def _resolved_at(row: Mapping[str, Any]) -> str | None:
    return str((row.get("resolution") or {}).get("resolved_at") or row.get("resolved_at") or "") or None

def _settlement_ts(row: Mapping[str, Any]) -> str | None:
    return str((row.get("resolution") or {}).get("settlement_ts") or row.get("settlement_ts") or "") or None

def _resolution_source(row: Mapping[str, Any]) -> str | None:
    return str((row.get("resolution") or {}).get("source") or row.get("resolution_source") or "") or None

def _existing_ids(path: Path) -> set[str]:
    return {str(row.get("source_observation_id")) for _, row in _jsonl(path) if row.get("source_observation_id")}
def _sha256(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024), b""): h.update(chunk)
    return h.hexdigest()
