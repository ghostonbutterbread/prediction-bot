"""Read-only migration/canary planning for dual paper wallet state."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from bot.paper_wallets import (
    BETA_PAPER_WALLET_ID,
    STABLE_PAPER_WALLET_ID,
    resolve_paper_wallet_contract,
)

RAW_MARKET_SNAPSHOTS = "market_snapshots.jsonl"
RAW_PREDICTIONS = "predictions.jsonl"
CANONICAL_ANALYSIS_LEDGER = "market_snapshots_upgraded.jsonl"
_ACCOUNTING_LEDGER_FILES = {
    "agent_decisions.jsonl",
    "agent_runs.jsonl",
    "lifecycle.jsonl",
    "reconciliation.jsonl",
    "shadow_intents.jsonl",
}


def build_paper_migration_canary_plan(
    config: Mapping[str, Any] | None = None,
    *,
    data_dir: str | Path | None = None,
    shared_candidates_root: str | Path | None = None,
    deep_scan: bool = False,
) -> dict[str, Any]:
    """Return a read-only migration/canary plan for current paper state."""

    stable = resolve_paper_wallet_contract(
        config,
        wallet_id=STABLE_PAPER_WALLET_ID,
        data_dir=data_dir,
        session_id="phase5_canary",
    )
    beta = resolve_paper_wallet_contract(
        config,
        wallet_id=BETA_PAPER_WALLET_ID,
        data_dir=data_dir,
        session_id="phase5_canary",
    )
    shared_root = (
        Path(shared_candidates_root).resolve(strict=False)
        if shared_candidates_root not in (None, "")
        else _default_shared_candidates_root(stable.root_dir, beta.root_dir)
    )
    isolation = _wallet_isolation_report(stable.root_dir, beta.root_dir, stable.risk_state_path, beta.risk_state_path)
    datasets = _detect_prediction_lab_datasets(
        stable.root_dir,
        beta.root_dir,
        shared_candidates_root=shared_root,
        deep_scan=deep_scan,
    )
    copy_plan = _build_copy_plan(datasets)
    backfill_plan = _build_backfill_plan(copy_plan, shared_root=shared_root)
    blockers = list(isolation["errors"])
    status = "ready" if not blockers else "blocked"

    return {
        "status": status,
        "mode": "read_only_migration_canary",
        "scan_mode": {
            "deep_scan": bool(deep_scan),
            "row_counting": "enabled" if deep_scan else "disabled_stat_only",
        },
        "compatibility_mapping": {
            "strategy": "preserve_existing_wallet_roots",
            "summary": (
                "Current compatibility mapping keeps existing paper accounting in place: "
                f"{stable.root_dir} -> {STABLE_PAPER_WALLET_ID}, {beta.root_dir} -> {BETA_PAPER_WALLET_ID}."
            ),
            "stable_paper_root": str(stable.root_dir),
            "beta_paper_root": str(beta.root_dir),
            "stable_policy_id": stable.policy_id,
            "beta_policy_id": beta.policy_id,
        },
        "shared_candidates_root": str(shared_root),
        "wallet_isolation": isolation,
        "wallet_state": {
            STABLE_PAPER_WALLET_ID: _wallet_state_inventory(stable.root_dir),
            BETA_PAPER_WALLET_ID: _wallet_state_inventory(beta.root_dir),
        },
        "candidate_datasets_under_wallet_roots": datasets,
        "copy_plan": copy_plan,
        "backfill_plan": backfill_plan,
        "blockers": blockers,
        "operator_steps": _operator_steps(
            stable_root=stable.root_dir,
            beta_root=beta.root_dir,
            shared_root=shared_root,
            copy_plan=copy_plan,
            backfill_plan=backfill_plan,
        ),
    }


def format_paper_migration_canary_plan(plan: Mapping[str, Any]) -> str:
    """Render a human-readable migration/canary summary."""

    compatibility = _mapping(plan.get("compatibility_mapping"))
    isolation = _mapping(plan.get("wallet_isolation"))
    wallet_state = _mapping(plan.get("wallet_state"))
    scan_mode = _mapping(plan.get("scan_mode"))
    datasets = list(plan.get("candidate_datasets_under_wallet_roots") or ())
    copy_plan = list(plan.get("copy_plan") or ())
    backfill_plan = list(plan.get("backfill_plan") or ())
    lines = [
        f"Status: {plan.get('status', 'unknown')}",
        "Mode: read-only migration/canary preview",
        f"Scan mode: {scan_mode.get('row_counting', 'unknown')}",
        "Compatibility mapping:",
        f"  stable_paper -> {compatibility.get('stable_paper_root')}",
        f"  beta_paper -> {compatibility.get('beta_paper_root')}",
        f"Shared candidate root preview: {plan.get('shared_candidates_root')}",
        (
            "Wallet isolation: ok"
            if isolation.get("ok")
            else "Wallet isolation: blocked"
        ),
    ]
    for check in isolation.get("checks", ()):
        lines.append(f"  - {check['name']}: {'ok' if check['ok'] else 'failed'}")
    if isolation.get("errors"):
        lines.append("Isolation blockers:")
        for error in isolation["errors"]:
            lines.append(f"  - {error}")

    lines.append("Existing wallet state:")
    for wallet_id in (STABLE_PAPER_WALLET_ID, BETA_PAPER_WALLET_ID):
        state = _mapping(wallet_state.get(wallet_id))
        lines.append(
            "  - "
            f"{wallet_id}: root_exists={state.get('root_exists')}, "
            f"risk_state_exists={state.get('risk_state_exists')}, "
            f"session_file_count={state.get('session_file_count')}"
        )

    if not datasets:
        lines.append("Candidate datasets under wallet roots: none detected")
    else:
        lines.append("Candidate datasets under wallet roots:")
        for item in datasets:
            row_count = item.get("row_count")
            rows = str(row_count) if row_count is not None else "not_counted"
            lines.append(
                "  - "
                f"{item['wallet_id']}: {item['source_path']} "
                f"[{item['dataset_kind']}, rows={rows}, size_bytes={item['size_bytes']}]"
            )

    if not copy_plan:
        lines.append("Copy preview: no candidate datasets would be copied")
    else:
        lines.append("Copy preview:")
        for item in copy_plan:
            lines.append(
                "  - "
                f"{item['wallet_id']}: {item['source_path']} -> {item['destination_path']}"
            )

    if backfill_plan:
        lines.append("Backfill preview:")
        for item in backfill_plan:
            lines.append(
                "  - "
                f"{item['wallet_id']}: {item['command']}"
            )

    lines.append("Operator steps:")
    for index, step in enumerate(plan.get("operator_steps") or (), start=1):
        lines.append(f"  {index}. {step}")
    return "\n".join(lines)


def _wallet_isolation_report(
    stable_root: Path,
    beta_root: Path,
    stable_risk_state: Path,
    beta_risk_state: Path,
) -> dict[str, Any]:
    stable_resolved = stable_root.resolve(strict=False)
    beta_resolved = beta_root.resolve(strict=False)
    checks = [
        {
            "name": "distinct_root_paths",
            "ok": stable_resolved != beta_resolved,
            "details": f"stable={stable_resolved}, beta={beta_resolved}",
        },
        {
            "name": "stable_root_not_nested_in_beta_root",
            "ok": stable_resolved not in beta_resolved.parents,
            "details": f"stable={stable_resolved}, beta={beta_resolved}",
        },
        {
            "name": "beta_root_not_nested_in_stable_root",
            "ok": beta_resolved not in stable_resolved.parents,
            "details": f"stable={stable_resolved}, beta={beta_resolved}",
        },
        {
            "name": "distinct_risk_state_paths",
            "ok": stable_risk_state.resolve(strict=False) != beta_risk_state.resolve(strict=False),
            "details": f"stable={stable_risk_state}, beta={beta_risk_state}",
        },
        {
            "name": "distinct_session_preview_paths",
            "ok": (stable_root / "sim_phase5_canary.json").resolve(strict=False)
            != (beta_root / "sim_phase5_canary.json").resolve(strict=False),
            "details": (
                f"stable={stable_root / 'sim_phase5_canary.json'}, "
                f"beta={beta_root / 'sim_phase5_canary.json'}"
            ),
        },
    ]
    errors = [check["details"] for check in checks if not check["ok"]]
    return {"ok": not errors, "checks": checks, "errors": errors}


def _wallet_state_inventory(root_dir: Path) -> dict[str, Any]:
    resolved_root = root_dir.resolve(strict=False)
    session_files = sorted(root_dir.glob("sim_*.json"), key=_mtime_sort_key)
    accounting_files = [
        str(path.name)
        for path in sorted(root_dir.glob("*.jsonl"))
        if path.name in _ACCOUNTING_LEDGER_FILES
    ]
    return {
        "root_dir": str(root_dir),
        "resolved_root_dir": str(resolved_root),
        "root_exists": root_dir.exists(),
        "risk_state_exists": (root_dir / "risk_state.json").exists(),
        "session_file_count": len(session_files),
        "latest_session_path": str(session_files[-1]) if session_files else None,
        "accounting_ledger_files": accounting_files,
        "mutation_policy": "keep_in_place_read_only",
    }


def _detect_prediction_lab_datasets(
    stable_root: Path,
    beta_root: Path,
    *,
    shared_candidates_root: Path,
    deep_scan: bool,
) -> list[dict[str, Any]]:
    detected: list[dict[str, Any]] = []
    for wallet_id, root_dir in (
        (STABLE_PAPER_WALLET_ID, stable_root),
        (BETA_PAPER_WALLET_ID, beta_root),
    ):
        if not root_dir.exists():
            continue
        prediction_lab_root = root_dir / "prediction_lab"
        if not prediction_lab_root.exists():
            continue
        for path in sorted(prediction_lab_root.rglob("*.jsonl")):
            dataset_kind = _classify_prediction_lab_dataset(path.relative_to(root_dir))
            if dataset_kind is None:
                continue
            relative_prediction_lab_path = path.relative_to(root_dir / "prediction_lab")
            size_bytes = path.stat().st_size
            detected.append(
                {
                    "wallet_id": wallet_id,
                    "wallet_root": str(root_dir),
                    "source_path": str(path),
                    "relative_wallet_path": str(path.relative_to(root_dir)),
                    "dataset_kind": dataset_kind,
                    "row_count": _count_nonempty_lines(path) if deep_scan else None,
                    "row_count_status": "counted" if deep_scan else "not_counted_stat_only",
                    "size_bytes": size_bytes,
                    "recommended_shared_path": str(
                        shared_candidates_root / "prediction_lab" / wallet_id / relative_prediction_lab_path
                    ),
                }
            )
    return detected


def _classify_prediction_lab_dataset(relative_path: Path) -> str | None:
    if not relative_path.parts or relative_path.parts[0] != "prediction_lab":
        return None
    if relative_path.name == RAW_MARKET_SNAPSHOTS:
        return "raw_market_snapshots"
    if relative_path.name == RAW_PREDICTIONS:
        return "raw_predictions"
    if relative_path.name == CANONICAL_ANALYSIS_LEDGER:
        return "canonical_analysis"
    if relative_path.suffix == ".jsonl":
        return "prediction_lab_jsonl"
    return None


def _build_copy_plan(detected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    for item in detected:
        plan.append(
            {
                "wallet_id": item["wallet_id"],
                "dataset_kind": item["dataset_kind"],
                "source_path": item["source_path"],
                "destination_path": item["recommended_shared_path"],
                "row_count": item["row_count"],
                "size_bytes": item["size_bytes"],
                "mode": "copy_only_never_move",
            }
        )
    return plan


def _build_backfill_plan(copy_plan: list[dict[str, Any]], *, shared_root: Path) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for item in copy_plan:
        wallet_id = str(item["wallet_id"])
        grouped.setdefault(
            wallet_id,
            {
                "wallet_id": wallet_id,
                "prediction_lab_dir": str(shared_root / "prediction_lab" / wallet_id),
                "analysis_dir": str(shared_root / "prediction_lab" / wallet_id / "analysis"),
                "raw_market_snapshots_present": False,
                "raw_predictions_present": False,
            },
        )
        if item["dataset_kind"] == "raw_market_snapshots":
            grouped[wallet_id]["raw_market_snapshots_present"] = True
        elif item["dataset_kind"] == "raw_predictions":
            grouped[wallet_id]["raw_predictions_present"] = True

    plan: list[dict[str, Any]] = []
    for wallet_id in (STABLE_PAPER_WALLET_ID, BETA_PAPER_WALLET_ID):
        item = grouped.get(wallet_id)
        if not item or not item["raw_market_snapshots_present"]:
            continue
        command = [
            "python3",
            "scripts/prediction_lab_backfill.py",
            "--canonical-analysis",
            "--prediction-lab-dir",
            item["prediction_lab_dir"],
            "--analysis-dir",
            item["analysis_dir"],
        ]
        if item["raw_predictions_present"]:
            command.append("--include-predictions")
        item["command"] = " ".join(command)
        plan.append(item)
    return plan


def _operator_steps(
    *,
    stable_root: Path,
    beta_root: Path,
    shared_root: Path,
    copy_plan: list[dict[str, Any]],
    backfill_plan: list[dict[str, Any]],
) -> list[str]:
    steps = [
        "Keep current wallet accounting roots in place. Preserve existing paper state by leaving "
        f"{stable_root} as stable_paper and {beta_root} as beta_paper.",
        "Do not move, delete, or rewrite risk_state.json, sim_*.json, lifecycle.jsonl, "
        "reconciliation.jsonl, agent_runs.jsonl, or agent_decisions.jsonl during this phase.",
    ]
    if copy_plan:
        steps.append(
            "If a later cutover is approved, copy Prediction Lab candidate datasets out of wallet roots into "
            f"{shared_root / 'prediction_lab'}/<wallet_id>/... using copy-only tooling such as cp -n or rsync "
            "--ignore-existing. Do not move files in place."
        )
    else:
        steps.append(
            "No Prediction Lab candidate datasets were found under wallet roots, so no copy/backfill preview is required."
        )
    if backfill_plan:
        steps.append(
            "After raw candidate datasets are copied, optionally build canonical shared analysis ledgers with "
            "scripts/prediction_lab_backfill.py --canonical-analysis against the copied shared-candidate directory."
        )
    steps.extend(
        [
            "Validate shared-candidate readers against the copied paths first. Only after that validation should any "
            "future collector/report config be repointed away from wallet-root prediction_lab directories.",
            "Later supervised cutover, outside this phase: schedule a maintenance window, retarget only the "
            "Prediction Lab writer path to the shared-candidate root, verify new rows land there, and keep the old "
            "wallet-root prediction_lab files as historical read-only archives.",
        ]
    )
    return steps


def _default_shared_candidates_root(stable_root: Path, beta_root: Path) -> Path:
    common = _common_parent([stable_root.resolve(strict=False), beta_root.resolve(strict=False)])
    return common / "shared_candidates"


def _common_parent(paths: list[Path]) -> Path:
    common_parts = list(paths[0].parts)
    for path in paths[1:]:
        next_parts: list[str] = []
        for left, right in zip(common_parts, path.parts):
            if left != right:
                break
            next_parts.append(left)
        common_parts = next_parts
    return Path(*common_parts) if common_parts else Path(paths[0].anchor or "/")


def _count_nonempty_lines(path: Path) -> int:
    count = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                count += 1
    return count


def _mtime_sort_key(path: Path) -> tuple[float, str]:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (mtime, path.name)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


__all__ = [
    "CANONICAL_ANALYSIS_LEDGER",
    "RAW_MARKET_SNAPSHOTS",
    "RAW_PREDICTIONS",
    "build_paper_migration_canary_plan",
    "format_paper_migration_canary_plan",
]
