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
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bot.collector_replay_inputs import DERIVED_REPORTS_ROOT, export_collector_replay_inputs
from bot.replay_outcome_binding import bind_replay_finalized_outcomes
from bot.weather.source_history_manifest import materialize_strict_source_history_collapse
from bot.weather.source_observation_ledger import materialize_source_observation_ledger

PIPELINE_SCHEMA_VERSION = 1
MANIFEST_FILENAME = "source_router_history_promotion.manifest.json"
CURRENT_GENERATION_LINKNAME = "current"
STRICT_SCORECARD_RELATIVE_PATH = Path("source_router_scoreboard") / "strict_finalized_source_scoreboard.jsonl"


@dataclass(frozen=True, slots=True)
class AutoSourceRouterPromotionResult:
    status: str
    reused: bool
    generation_dir: Path
    history_path: Path
    scoreboard_path: Path
    manifest_path: Path
    history_manifest_path: Path
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
    # Consume each input exactly once before deriving its generation identity.
    # All downstream helpers receive the materialized bytes, not a path that a
    # collector or resolver can append between hash calculation and reread.
    snapshot_bytes, resolution_bytes = snapshots.read_bytes(), resolutions.read_bytes()
    snapshot_sha256 = hashlib.sha256(snapshot_bytes).hexdigest()
    resolution_sha256 = hashlib.sha256(resolution_bytes).hexdigest()
    generation_id = hashlib.sha256(_canonical_bytes({
        "schema_name": "auto_source_router_history_promotion",
        "schema_version": PIPELINE_SCHEMA_VERSION,
        "collector_snapshots_sha256": snapshot_sha256,
        "strict_resolutions_sha256": resolution_sha256,
    })).hexdigest()
    generation_dir = root / "generations" / generation_id
    manifest_path = generation_dir / MANIFEST_FILENAME
    if generation_dir.exists():
        return _load_completed_generation(manifest_path, snapshot_sha256, resolution_sha256)

    (root / "generations").mkdir(parents=True, exist_ok=True)
    staging_root = root / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix=f"{generation_id}.", dir=staging_root))
    try:
        materialized_inputs = staging_dir / "materialized_inputs"
        materialized_inputs.mkdir()
        materialized_snapshots = materialized_inputs / "collector_snapshots.jsonl"
        materialized_resolutions = materialized_inputs / "strict_resolutions.jsonl"
        materialized_snapshots.write_bytes(snapshot_bytes)
        materialized_resolutions.write_bytes(resolution_bytes)
        exported = export_collector_replay_inputs(
            source_archive=materialized_snapshots, output_dir=staging_dir / "replay_inputs",
        )
        bound = bind_replay_finalized_outcomes(
            replay_inputs_path=exported.records_path,
            strict_resolutions_path=materialized_resolutions,
            output_dir=staging_dir / "finalized_outcomes",
        )
        observations = materialize_source_observation_ledger(
            replay_inputs_path=exported.records_path,
            finalized_outcomes_path=bound.outcomes_path,
            output_dir=staging_dir / "source_observations",
        )
        collapsed = materialize_strict_source_history_collapse(
            observations.settled_path, staging_dir / "source_router_scoreboard",
        )
        strict_scorecard_path = _materialize_strict_finalized_scorecard(
            collapsed.collapsed_path, staging_dir / "source_router_scoreboard" / "strict_finalized_source_scoreboard.jsonl",
        )
        # Helper metadata is produced while staging, but a published generation
        # must not retain an unresolvable random staging location. Rebase before
        # hashing the replay manifest into source_history_manifest.
        _rebase_staged_metadata_paths(staging_dir, generation_dir)
        counts = _counts(exported.metadata, bound.metadata, observations.metadata, dict(collapsed.metadata))
        status = "history_ready" if counts["eligible"] else "no_router_history"
        published = lambda path: generation_dir / path.relative_to(staging_dir)
        history_manifest_path = staging_dir / "source_history_manifest.json"
        history_manifest = _source_history_manifest(
            source_ledger=(published(observations.settled_path), observations.settled_path),
            strict_resolution=(published(materialized_resolutions), materialized_resolutions),
            raw_archive=(published(materialized_snapshots), materialized_snapshots),
            replay_index=(published(exported.records_path), exported.records_path),
            replay_manifest=(published(exported.metadata_path), exported.metadata_path),
        )
        history_manifest_path.write_bytes(_canonical_bytes(history_manifest))
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
                "history_ledger_path": str(published(observations.settled_path)),
                "history_manifest_path": str(published(history_manifest_path)),
                "scoreboard_path": str(published(strict_scorecard_path)),
                "current_scoreboard_path": str(root / CURRENT_GENERATION_LINKNAME / STRICT_SCORECARD_RELATIVE_PATH),
                "consumer": "bot.paper_shadow_lanes._source_router_decision via load_scoreboard_rows/build_source_confidence_row (disabled beta paper lane handoff)",
            },
        "counts": counts,
        "artifacts": {
            "replay_inputs": str(published(exported.records_path)),
            "finalized_outcomes": str(published(bound.outcomes_path)),
            "unbound_outcomes": str(published(bound.unbound_path)),
            "history_ledger": str(published(observations.settled_path)),
            "unusable_observations": str(published(observations.unsettled_path)),
            "scoreboard": str(published(strict_scorecard_path)),
            "strict_independent_history": str(published(collapsed.collapsed_path)),
        },
            "source_history_manifest": {
                "path": str(published(history_manifest_path)),
                "sha256": history_manifest["sha256"],
            },
        }
        staged_manifest_path = staging_dir / MANIFEST_FILENAME
        staged_manifest_path.write_bytes(_canonical_bytes(manifest))
        try:
            os.replace(staging_dir, generation_dir)
        except FileExistsError:
            return _load_completed_generation(manifest_path, snapshot_sha256, resolution_sha256)
        _publish_current_generation(root, generation_dir)
        return _result_from_manifest(manifest, manifest_path, reused=False)
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)


def _prepare_root(value: str | Path) -> Path:
    root = Path(value).expanduser().resolve()
    derived_root = DERIVED_REPORTS_ROOT.resolve()
    if root == derived_root or derived_root not in root.parents:
        raise ValueError(f"output root must be below {derived_root}")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _publish_current_generation(root: Path, generation_dir: Path) -> None:
    """Atomically repoint the stable consumer handoff after full publication."""
    if not (generation_dir / MANIFEST_FILENAME).is_file():
        raise ValueError(f"refusing to hand off incomplete promotion generation: {generation_dir}")
    current_link = root / CURRENT_GENERATION_LINKNAME
    temporary_link = root / f".{CURRENT_GENERATION_LINKNAME}.{uuid.uuid4().hex}"
    try:
        temporary_link.symlink_to(Path("generations") / generation_dir.name, target_is_directory=True)
        os.replace(temporary_link, current_link)
    finally:
        if temporary_link.is_symlink():
            temporary_link.unlink()


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



def _materialize_strict_finalized_scorecard(source_path: Path, output_path: Path) -> Path:
    """Aggregate only collapsed strict finalized observations for the paper router.

    The row fields intentionally match ``load_scoreboard_rows`` and
    ``build_source_confidence_row``; this is not the legacy loose scoreboard.
    """
    source_bytes = source_path.read_bytes()
    rows = [json.loads(line) for line in source_bytes.splitlines() if line.strip()]
    slices: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("eligible_for_source_history") is not True or row.get("source_correctness_eligibility") != "eligible_strict_source_proof":
            continue
        direction_correct = row.get("direction_correct")
        if not isinstance(direction_correct, bool):
            continue
        source_id = str(row.get("source_id") or "unknown")
        city_id = str(row.get("city_id") or "unknown")
        market_kind = str(row.get("market_kind") or "unknown")
        contract_shape = str(row.get("contract_shape") or "unknown")
        source_name = str(row.get("source_name") or "unknown")
        key = (source_id, city_id, market_kind, contract_shape, source_name)
        aggregate = slices.setdefault(key, {"sample_count": 0, "correct_count": 0, "settlement_ts": []})
        aggregate["sample_count"] += 1
        aggregate["correct_count"] += int(direction_correct)
        aggregate["settlement_ts"].append(str(row.get("settlement_ts") or ""))

    scorecard_rows: list[dict[str, Any]] = []
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    for (source_id, city_id, market_kind, contract_shape, source_name), aggregate in sorted(slices.items()):
        sample_count = int(aggregate["sample_count"])
        scorecard_rows.append({
            "schema_name": "strict_finalized_source_router_scorecard",
            "schema_version": 1,
            "source_id": source_id,
            "source_name": source_name,
            "city_id": city_id,
            "market_kind": market_kind,
            "contract_shape": contract_shape,
            "sample_count": sample_count,
            "threshold_sample_count": sample_count,
            "threshold_correct_count": int(aggregate["correct_count"]),
            "threshold_direction_accuracy": round(int(aggregate["correct_count"]) / sample_count, 6),
            "provenance": {
                "input_kind": "collapsed_strict_finalized_source_history",
                "source_history_sha256": source_sha256,
                "eligibility": "eligible_for_source_history and eligible_strict_source_proof",
                "availability_field": "settlement_ts",
                "settlement_ts": sorted(value for value in aggregate["settlement_ts"] if value),
            },
        })
    output_path.write_bytes(b"".join(_canonical_bytes(row) for row in scorecard_rows))
    return output_path


def _rebase_staged_metadata_paths(staging_dir: Path, generation_dir: Path) -> None:
    """Replace private staging roots before metadata hashes become public evidence."""
    old, new = str(staging_dir).encode("utf-8"), str(generation_dir).encode("utf-8")
    for path in staging_dir.rglob("*.json"):
        path.write_bytes(path.read_bytes().replace(old, new))


def _result_from_manifest(manifest: dict[str, Any], manifest_path: Path, *, reused: bool) -> AutoSourceRouterPromotionResult:
    runtime = manifest.get("runtime_consumption") or {}
    counts = {key: int((manifest.get("counts") or {}).get(key) or 0) for key in ("pending", "unresolved", "invalid", "eligible", "collapsed")}
    return AutoSourceRouterPromotionResult(
        status=str(manifest.get("status")), reused=reused, generation_dir=manifest_path.parent,
        history_path=Path(str(runtime["history_ledger_path"])), scoreboard_path=Path(str(runtime["scoreboard_path"])),
        manifest_path=manifest_path, history_manifest_path=Path(str(runtime["history_manifest_path"])), counts=counts,
    )


def _load_completed_generation(
    manifest_path: Path, snapshot_sha256: str, resolution_sha256: str,
) -> AutoSourceRouterPromotionResult:
    if not manifest_path.is_file():
        raise ValueError(f"incomplete promotion generation exists; refusing to consume: {manifest_path.parent}")
    manifest = _load_manifest(manifest_path)
    if manifest.get("input_sha256") != {"collector_snapshots": snapshot_sha256, "strict_resolutions": resolution_sha256}:
        raise ValueError("existing promotion generation input hash mismatch")
    result = _result_from_manifest(manifest, manifest_path, reused=True)
    if not result.history_manifest_path.is_file():
        raise ValueError("completed promotion generation is missing its source history manifest")
    return result


def _source_history_manifest(
    *,
    source_ledger: tuple[Path, Path],
    strict_resolution: tuple[Path, Path],
    raw_archive: tuple[Path, Path],
    replay_index: tuple[Path, Path],
    replay_manifest: tuple[Path, Path],
) -> dict[str, Any]:
    """Build the existing verified collector-history contract from staged bytes."""
    artifacts = {
        "source_ledger": source_ledger,
        "strict_resolution": strict_resolution,
        "raw_archive": raw_archive,
        "replay_index": replay_index,
        "replay_manifest": replay_manifest,
    }
    return {
        "schema_name": "source_history_manifest",
        "schema_version": 1,
        "historical_counterfactual_only": True,
        "non_mutating": True,
        "join_key": "market_id",
        "eligibility_filter": "eligible_for_source_history == true and source_correctness_eligibility == eligible_strict_source_proof",
        "availability_field": "settlement_ts",
        **{f"{name}_path": str(published_path) for name, (published_path, _) in artifacts.items()},
        "sha256": {name: _sha256_file(staged_path) for name, (_, staged_path) in artifacts.items()},
    }


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
