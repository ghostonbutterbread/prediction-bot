"""Append-only live read-only decision audit rows for the unified agent ledger."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bot.agent_decision_ledger import (
    build_agent_decision_id,
    build_agent_run_id,
    build_agent_run_row,
    validate_agent_decision_row_with_legacy_identity,
)
from bot.file_ops import append_jsonl, load_jsonl
from bot.shared_market_feed import shared_candidate_id_from_row
from bot.strategy_policy import strategy_policy_status

logger = logging.getLogger(__name__)

LIVE_AGENT_ID = "live"
LIVE_RUNTIME = "live"
LIVE_READONLY_MODE = "live_readonly"
LIVE_READONLY_DECISION_ROLE = "live_readonly"
LIVE_READONLY_SESSION_ID = "live-runner"
CANDIDATE_DATASET_NAME = "live_readonly_candidates.jsonl"


def append_live_readonly_agent_run_once(
    *,
    data_dir: str | Path,
    session_id: str = LIVE_READONLY_SESSION_ID,
    config: dict[str, Any] | None = None,
    started_at: str | datetime | None = None,
) -> dict[str, Any] | None:
    """Append one live-readonly run sidecar row for a live runner session."""

    data_path = Path(data_dir)
    run_id = str(session_id or LIVE_READONLY_SESSION_ID)
    agent_run_id = build_agent_run_id(agent_id=LIVE_AGENT_ID, run_id=run_id)
    path = data_path / "agent_runs.jsonl"
    if any(str(row.get("agent_run_id") or "") == agent_run_id for row in load_jsonl(path)):
        return None

    row = build_agent_run_row(
        agent_id=LIVE_AGENT_ID,
        runtime=LIVE_RUNTIME,
        policy=_live_policy(config or {}),
        mode=LIVE_READONLY_MODE,
        run_id=run_id,
        started_at=started_at or datetime.now(timezone.utc),
        finished_at=None,
        status="running",
        candidate_dataset_path=data_path / CANDIDATE_DATASET_NAME,
        decision_ledger_path=data_path / "agent_decisions.jsonl",
        accounting_namespace=data_path,
        mutates_accounting=False,
        notes="live read-only decision audit; does not place orders or mutate live accounting",
    )
    append_jsonl(path, row)
    return row


def append_live_readonly_decision_audit(
    *,
    data_dir: str | Path,
    session_id: str = LIVE_READONLY_SESSION_ID,
    scan_count: int = 0,
    signal: dict[str, Any] | None,
    decision_artifact: dict[str, Any],
    config: dict[str, Any] | None = None,
    audit_stage: str = "pre_execution",
) -> dict[str, Any]:
    """Append a non-mutating live decision row and matching candidate snapshot.

    The row records what the live path decided with known-at-time evidence. It is
    intentionally read-only: it never places orders and never marks accounting as
    mutated, even when the surrounding runtime later executes a separate live
    order through the normal execution path.
    """

    data_path = Path(data_dir)
    config = config or {}
    signal_row = dict(signal or {})
    append_live_readonly_agent_run_once(data_dir=data_path, session_id=session_id, config=config)

    run_id = _run_id(session_id=session_id, scan_count=scan_count)
    observed_at = str(decision_artifact.get("observed_at") or signal_row.get("observed_at") or datetime.now(timezone.utc).isoformat())
    market_id = _market_id(decision_artifact, signal_row)
    # Always write live-readonly candidate snapshots to our sidecar dataset.
    # Do not trust or append to signal["candidate_dataset_path"] because that
    # may point at the original shared candidate source, which this audit layer
    # promises not to mutate.
    candidate_dataset_path = data_path / CANDIDATE_DATASET_NAME
    source_candidate_dataset_path = signal_row.get("candidate_dataset_path")
    shared_candidate_id = shared_candidate_id_from_row(signal_row) or shared_candidate_id_from_row(decision_artifact)

    _append_candidate_snapshot(
        candidate_dataset_path,
        run_id=run_id,
        observed_at=observed_at,
        signal=signal_row,
        decision_artifact=decision_artifact,
        shared_candidate_id=shared_candidate_id,
        audit_stage=audit_stage,
    )

    legacy_identity = None
    if shared_candidate_id in (None, ""):
        legacy_identity = {
            "identity_type": "legacy_live_readonly_signal",
            "session_id": str(session_id or LIVE_READONLY_SESSION_ID),
            "scan_count": int(scan_count or 0),
            "market_id": market_id,
            "observed_at": observed_at,
            "audit_stage": str(audit_stage or "unknown"),
        }
        logger.warning(
            "live read-only decision audit using legacy candidate identity; shared_candidate_id missing session_id=%s market_id=%s",
            session_id,
            market_id,
        )

    decision = _decision_dict(decision_artifact)
    shared_market_provenance = _shared_market_provenance(signal_row, decision_artifact)
    action = str(decision_artifact.get("final_action") or _action_from_decision(decision) or "SKIP")
    row = validate_agent_decision_row_with_legacy_identity(
        {
            "schema_name": "agent_decision",
            "schema_version": 1,
            "decision_id": build_agent_decision_id(
                agent_run_id=build_agent_run_id(agent_id=LIVE_AGENT_ID, run_id=str(session_id or LIVE_READONLY_SESSION_ID)),
                agent_id=LIVE_AGENT_ID,
                runtime=LIVE_RUNTIME,
                policy=_live_policy(config),
                decision_role=LIVE_READONLY_DECISION_ROLE,
                shared_candidate_id=str(shared_candidate_id or ""),
                run_id=run_id,
                market_id=market_id,
                observed_at=observed_at,
                legacy_candidate_identity=legacy_identity,
            ),
            "agent_run_id": build_agent_run_id(agent_id=LIVE_AGENT_ID, run_id=str(session_id or LIVE_READONLY_SESSION_ID)),
            "agent_id": LIVE_AGENT_ID,
            "runtime": LIVE_RUNTIME,
            "policy": _live_policy(config),
            "decision_role": LIVE_READONLY_DECISION_ROLE,
            "shared_candidate_id": shared_candidate_id,
            "legacy_candidate_identity": legacy_identity,
            "candidate_dataset_path": str(candidate_dataset_path),
            "candidate_dataset_identity": (
                "shared_candidate_dataset" if shared_candidate_id not in (None, "") else f"legacy:live_readonly:{session_id or LIVE_READONLY_SESSION_ID}"
            ),
            "run_id": run_id,
            "market_id": market_id,
            "observed_at": observed_at,
            "decided_at": observed_at,
            "action": action,
            "side": _side_from_action(action),
            "requested_position_size_usd": _number(decision.get("requested_position_size"), decision.get("position_size")),
            "approved_position_size_usd": _approved_size(decision),
            "reason_code": decision_artifact.get("final_reason_code") or decision.get("reason_code"),
            "reason": decision_artifact.get("final_reason") or decision.get("reason"),
            "selected_lane": _selected_lane(decision),
            "confidence": _number(decision.get("confidence"), signal_row.get("confidence")),
            "edge": _number(decision.get("edge"), signal_row.get("edge")),
            "model_probability": _number(decision.get("win_probability"), signal_row.get("model_probability")),
            "entry_price": _number(decision.get("entry_price"), signal_row.get("market_price")),
            "accounting_ref": {
                "namespace": str(data_path),
                "ledger_path": None,
                "trade_id": None,
                "mutates_balance": False,
                "balance_model": "live_balance_readonly",
            },
            "mutation_contract": {
                "mutates_shared_candidate": False,
                "mutates_accounting": False,
                "accounting_mutation_scope": "none_live_readonly",
                "accounting_mutation_path": None,
                "places_orders": False,
            },
            "provenance": {
                "known_at_time": True,
                "source": "live_known_at_time",
                "live_readonly": True,
                "audit_stage": str(audit_stage or "unknown"),
                "session_id": str(session_id or LIVE_READONLY_SESSION_ID),
                "scan_count": int(scan_count or 0),
                "config_hash": decision_artifact.get("config_hash"),
                "source_candidate_dataset_path": str(source_candidate_dataset_path) if source_candidate_dataset_path else None,
                "shared_market": shared_market_provenance,
            },
        }
    )
    append_jsonl(data_path / "agent_decisions.jsonl", row)
    return row


def _append_candidate_snapshot(
    path: Path,
    *,
    run_id: str,
    observed_at: str,
    signal: dict[str, Any],
    decision_artifact: dict[str, Any],
    shared_candidate_id: str | None,
    audit_stage: str,
) -> None:
    row = {
        "schema_name": "live_readonly_candidate_snapshot",
        "schema_version": 1,
        "run_id": run_id,
        "observed_at": observed_at,
        "market_id": _market_id(decision_artifact, signal),
        "shared_candidate_id": shared_candidate_id,
        "audit_stage": str(audit_stage or "unknown"),
        "signal": signal,
        "decision_summary": {
            "action": decision_artifact.get("final_action"),
            "reason_code": decision_artifact.get("final_reason_code"),
            "reason": decision_artifact.get("final_reason"),
        },
    }
    append_jsonl(path, row)


def _live_policy(config: dict[str, Any]) -> str:
    status = strategy_policy_status(config.get("strategy_policy_normalized") or config.get("strategy_policy") or {})
    if status.get("enforce"):
        return "beta_enforce"
    if status.get("shadow"):
        return "stable_with_beta_shadow"
    return "stable"


def _shared_market_provenance(signal: dict[str, Any], decision_artifact: dict[str, Any]) -> dict[str, Any] | None:
    for value in (
        signal.get("shared_market"),
        signal.get("shared_market_provenance"),
        (decision_artifact.get("strategy_signal") or {}).get("shared_market") if isinstance(decision_artifact.get("strategy_signal"), dict) else None,
        (decision_artifact.get("strategy_signal") or {}).get("shared_market_provenance") if isinstance(decision_artifact.get("strategy_signal"), dict) else None,
        ((decision_artifact.get("source_context") or {}).get("source") or {}).get("shared_market")
        if isinstance((decision_artifact.get("source_context") or {}).get("source"), dict)
        else None,
    ):
        if isinstance(value, dict):
            return dict(value)
    return None


def _run_id(*, session_id: str, scan_count: int) -> str:
    return f"{str(session_id or LIVE_READONLY_SESSION_ID)}:scan-{int(scan_count or 0)}"


def _market_id(decision_artifact: dict[str, Any], signal: dict[str, Any]) -> str:
    return str(decision_artifact.get("market_id") or signal.get("market_id") or "")


def _decision_dict(decision_artifact: dict[str, Any]) -> dict[str, Any]:
    decision = decision_artifact.get("shared_core_decision")
    return dict(decision) if isinstance(decision, dict) else {}


def _action_from_decision(decision: dict[str, Any]) -> str | None:
    approved = decision.get("approved")
    action = decision.get("action")
    if approved is False:
        return "SKIP"
    if action:
        return str(action)
    return None


def _selected_lane(decision: dict[str, Any]) -> str | None:
    reasoning = decision.get("reasoning")
    if not isinstance(reasoning, dict):
        return None
    lane = reasoning.get("strategy_lane")
    if not isinstance(lane, dict):
        return None
    lane_id = lane.get("lane_id")
    return str(lane_id) if lane_id not in (None, "") else None



def _approved_size(decision: dict[str, Any]) -> float | int | None:
    approved = decision.get("approved")
    if approved is False:
        return 0.0
    return _number(decision.get("position_size"), decision.get("approved_position_size"))

def _number(*values: Any) -> float | int | None:
    for value in values:
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            return value
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _side_from_action(action: str) -> str | None:
    text = str(action or "").upper()
    if "YES" in text:
        return "YES"
    if "NO" in text:
        return "NO"
    return None
