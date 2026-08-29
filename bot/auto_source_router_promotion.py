"""Safe, derived-only promotion of finalized collector evidence to Source Router history.

The pipeline is intentionally a post-resolver artifact builder: immutable
collector snapshots are sanitized into sealed replay inputs, independently
finalized strict resolutions are bound by exact decision identity, and only
strict source observations become router history.  It never changes snapshots,
replay inputs, orders, wallets, or runtime configuration.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bot.collector_replay_inputs import DERIVED_REPORTS_ROOT, export_collector_replay_inputs
from bot.replay_outcome_binding import bind_replay_finalized_outcomes
from bot.weather.source_history_manifest import materialize_strict_source_history_collapse
from bot.weather.source_observation_ledger import materialize_source_observation_ledger

PIPELINE_SCHEMA_VERSION = 1
MANIFEST_FILENAME = "source_router_history_promotion.manifest.json"


@dataclass(frozen=True, slots=True)
class AutoSourceRouterPromotionResult:
    status: str
    reused: bool
    generation_dir: Path
    history_path: Path
    scoreboard_path: Path
    manifest_path: Path
    counts: dict[str, int]


def auto_populate_source_router_history(
    *, collector_snapshots_path: str | Path, strict_resolutions_path: str | Path, output_root: str | Path,
) -> AutoSourceRouterPromotionResult:
    """Build one hash-addressed Source Router history generation after resolution.

    The returned ``history_path`` is the strict settled ledger accepted by the
    existing Source Router history loader.  ``scoreboard_path`` is the collapsed
    independent-observation artifact for audit/sample accounting.  A repeated
    invocation for byte-identical inputs only returns the completed generation.
    """
    snapshots = Path(collector_snapshots_path).expanduser().resolve()
    resolutions = Path(strict_resolutions_path).expanduser().resolve()
    if not snapshots.is_file() or not resolutions.is_file():
        raise ValueError("collector snapshots and strict resolutions must be readable files")
    root = _prepare_root(output_root)
    snapshot_sha256, resolution_sha256 = _sha256_file(snapshots), _sha256_file(resolutions)
    generation_id = hashlib.sha256(_canonical_bytes({
        "schema_name": "auto_source_router_history_promotion",
        "schema_version": PIPELINE_SCHEMA_VERSION,
        "collector_snapshots_sha256": snapshot_sha256,
        "strict_resolutions_sha256": resolution_sha256,
    })).hexdigest()
    generation_dir = root / "generations" / generation_id
    manifest_path = generation_dir / MANIFEST_FILENAME
    if generation_dir.exists():
        if not manifest_path.is_file():
            raise ValueError(f"incomplete promotion generation exists; refusing overwrite: {generation_dir}")
        manifest = _load_manifest(manifest_path)
        if manifest.get("input_sha256") != {"collector_snapshots": snapshot_sha256, "strict_resolutions": resolution_sha256}:
            raise ValueError("existing promotion generation input hash mismatch")
        return _result_from_manifest(manifest, manifest_path, reused=True)

    generation_dir.mkdir(parents=True, exist_ok=False)
    exported = export_collector_replay_inputs(
        source_archive=snapshots, output_dir=generation_dir / "replay_inputs",
    )
    bound = bind_replay_finalized_outcomes(
        replay_inputs_path=exported.records_path,
        strict_resolutions_path=resolutions,
        output_dir=generation_dir / "finalized_outcomes",
    )
    observations = materialize_source_observation_ledger(
        replay_inputs_path=exported.records_path,
        finalized_outcomes_path=bound.outcomes_path,
        output_dir=generation_dir / "source_observations",
    )
    collapsed = materialize_strict_source_history_collapse(
        observations.settled_path, generation_dir / "source_router_scoreboard",
    )
    counts = _counts(exported.metadata, bound.metadata, observations.metadata, dict(collapsed.metadata))
    status = "history_ready" if counts["eligible"] else "no_router_history"
    manifest = {
        "schema_name": "auto_source_router_history_promotion",
        "schema_version": PIPELINE_SCHEMA_VERSION,
        "mode": "derived_only_post_resolver_source_router_history",
        "non_mutating": True,
        "network_access": False,
        "orders_wallets_or_trading_mutated": False,
        "promotion_result": "not_a_live_or_lane_promotion_result",
        "status": status,
        "generation_id": generation_id,
        "input_sha256": {"collector_snapshots": snapshot_sha256, "strict_resolutions": resolution_sha256},
        "inputs": {"collector_snapshots_path": str(snapshots), "strict_resolutions_path": str(resolutions)},
        "chronology": {
            "availability_field": "settlement_ts",
            "eligibility": "eligible_for_source_history and settlement_ts < later_decision_time",
            "outcomes_never_written_to": "collector snapshots or replay decision inputs",
        },
        "runtime_consumption": {
            "history_ledger_path": str(observations.settled_path),
            "scoreboard_path": str(collapsed.collapsed_path),
            "consumer": "scripts/weather_source_router_replay.py --history-ledger-input",
        },
        "counts": counts,
        "artifacts": {
            "replay_inputs": str(exported.records_path),
            "finalized_outcomes": str(bound.outcomes_path),
            "unbound_outcomes": str(bound.unbound_path),
            "history_ledger": str(observations.settled_path),
            "unusable_observations": str(observations.unsettled_path),
            "scoreboard": str(collapsed.collapsed_path),
        },
    }
    manifest_path.write_bytes(_canonical_bytes(manifest))
    return _result_from_manifest(manifest, manifest_path, reused=False)


def _prepare_root(value: str | Path) -> Path:
    root = Path(value).expanduser().resolve()
    derived_root = DERIVED_REPORTS_ROOT.resolve()
    if root == derived_root or derived_root not in root.parents:
        raise ValueError(f"output root must be below {derived_root}")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _counts(export: dict[str, Any], binding: dict[str, Any], observations: dict[str, Any], collapse: dict[str, Any]) -> dict[str, int]:
    binding_counts = binding.get("binding_counts") or {}
    input_counts = observations.get("counts") or {}
    collapse_counts = collapse.get("counts") or {}
    return {
        "pending": int(input_counts.get("pending") or 0),
        "unresolved": int(binding_counts.get("unbound") or 0),
        "invalid": int(export.get("rejected_row_count") or 0) + int(binding_counts.get("invalid") or 0) + int(input_counts.get("invalid_input_records") or 0) + int(input_counts.get("invalid_outcomes") or 0),
        "eligible": int(collapse_counts.get("independent_rows") or 0),
        "collapsed": int(collapse_counts.get("collapsed_repeat_polls") or 0),
    }


def _result_from_manifest(manifest: dict[str, Any], manifest_path: Path, *, reused: bool) -> AutoSourceRouterPromotionResult:
    runtime = manifest.get("runtime_consumption") or {}
    counts = {key: int((manifest.get("counts") or {}).get(key) or 0) for key in ("pending", "unresolved", "invalid", "eligible", "collapsed")}
    return AutoSourceRouterPromotionResult(
        status=str(manifest.get("status")), reused=reused, generation_dir=manifest_path.parent,
        history_path=Path(str(runtime["history_ledger_path"])), scoreboard_path=Path(str(runtime["scoreboard_path"])),
        manifest_path=manifest_path, counts=counts,
    )


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"promotion manifest is invalid JSON: {path}") from error
    if not isinstance(value, dict) or value.get("schema_name") != "auto_source_router_history_promotion":
        raise ValueError(f"promotion manifest is invalid: {path}")
    return value


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
