"""Append-only paper decision audit rows for the unified agent ledger."""

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
from bot.paper_wallets import build_paper_accounting_ref, resolve_paper_wallet_contract
from bot.shared_market_feed import shared_candidate_id_from_row
from bot.strategy_policy import strategy_policy_status

logger = logging.getLogger(__name__)

PAPER_AGENT_ID = "paper"
PAPER_RUNTIME = "paper"
PAPER_DECISION_ROLE = "paper"


def append_paper_agent_run_once(
    *,
    data_dir: str | Path,
    session_id: str,
    config: dict[str, Any],
    started_at: str | datetime | None = None,
    candidate_dataset_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Append one paper run sidecar row for a session when it does not exist."""

    data_path = Path(data_dir)
    wallet_contract = resolve_paper_wallet_contract(config, session_id=session_id, data_dir=data_path)
    run_id = str(session_id or "unknown")
    agent_run_id = build_agent_run_id(agent_id=PAPER_AGENT_ID, run_id=run_id)
    path = data_path / "agent_runs.jsonl"
    if any(str(row.get("agent_run_id") or "") == agent_run_id for row in load_jsonl(path)):
        return None

    row = build_agent_run_row(
        agent_id=PAPER_AGENT_ID,
        runtime=PAPER_RUNTIME,
        policy=_paper_policy(config),
        mode="paper",
        run_id=run_id,
        started_at=started_at or datetime.now(timezone.utc),
        finished_at=None,
        status="running",
        candidate_dataset_path=candidate_dataset_path or _paper_session_path(data_path, session_id),
        decision_ledger_path=data_path / "agent_decisions.jsonl",
        accounting_namespace=data_path,
        mutates_accounting=True,
        notes="paper sidecar audit only; accounting remains owned by existing paper session/risk path",
    )
    row.update(
        {
            "wallet_id": wallet_contract.wallet_id,
            "policy_id": wallet_contract.policy_id,
            "wallet_namespace": wallet_contract.namespace,
            "accounting_root": str(wallet_contract.root_dir),
            "risk_state_path": str(wallet_contract.risk_state_path),
            "session_path": str(wallet_contract.session_path) if wallet_contract.session_path is not None else None,
            "places_live_orders": wallet_contract.places_live_orders,
        }
    )
    append_jsonl(path, row)
    return row


def append_paper_decision_audit(
    *,
    data_dir: str | Path,
    session_id: str,
    scan_count: int,
    signal: dict[str, Any] | None,
    decision_artifact: dict[str, Any],
    execution_result: Any | None = None,
    trade_id: str | None = None,
    config: dict[str, Any] | None = None,
    accounting_mutated: bool = False,
) -> dict[str, Any]:
    """Append one paper decision row without changing paper execution state."""

    data_path = Path(data_dir)
    config = config or {}
    wallet_contract = resolve_paper_wallet_contract(config, session_id=session_id, data_dir=data_path)
    signal_row = dict(signal or {})
    shared_candidate_id = shared_candidate_id_from_row(signal_row) or shared_candidate_id_from_row(decision_artifact)
    market_id = _market_id(decision_artifact, signal_row)
    observed_at = str(decision_artifact.get("observed_at") or datetime.now(timezone.utc).isoformat())
    run_id = str(session_id or "unknown")
    legacy_identity = None
    if shared_candidate_id in (None, ""):
        legacy_identity = {
            "identity_type": "legacy_paper_signal",
            "session_id": run_id,
            "scan_count": int(scan_count or 0),
            "market_id": market_id,
            "observed_at": observed_at,
        }
        logger.warning(
            "paper decision audit using legacy candidate identity; shared_candidate_id missing session_id=%s market_id=%s",
            run_id,
            market_id,
        )

    action = str(decision_artifact.get("final_action") or "SKIP")
    decision = _decision_dict(decision_artifact)
    result_trade_id = trade_id or str(getattr(execution_result, "trade_id", "") or "")
    row = validate_agent_decision_row_with_legacy_identity(
        {
            "schema_name": "agent_decision",
            "schema_version": 1,
            "decision_id": build_agent_decision_id(
                agent_run_id=build_agent_run_id(agent_id=PAPER_AGENT_ID, run_id=run_id),
                agent_id=PAPER_AGENT_ID,
                runtime=PAPER_RUNTIME,
                policy=_paper_policy(config),
                decision_role=PAPER_DECISION_ROLE,
                shared_candidate_id=str(shared_candidate_id or ""),
                run_id=run_id,
                market_id=market_id,
                observed_at=observed_at,
                legacy_candidate_identity=legacy_identity,
            ),
            "agent_run_id": build_agent_run_id(agent_id=PAPER_AGENT_ID, run_id=run_id),
            "agent_id": PAPER_AGENT_ID,
            "runtime": PAPER_RUNTIME,
            "policy": _paper_policy(config),
            "decision_role": PAPER_DECISION_ROLE,
            "shared_candidate_id": shared_candidate_id,
            "legacy_candidate_identity": legacy_identity,
            "candidate_dataset_path": str(signal_row.get("candidate_dataset_path") or _paper_session_path(data_path, session_id)),
            "candidate_dataset_identity": (
                "shared_candidate_dataset"
                if shared_candidate_id not in (None, "")
                else f"legacy:paper_session:{run_id}"
            ),
            "run_id": run_id,
            "market_id": market_id,
            "observed_at": observed_at,
            "decided_at": observed_at,
            "action": action,
            "side": _side_from_action(action),
            "requested_position_size_usd": _number(decision.get("requested_position_size")),
            "approved_position_size_usd": _approved_size(decision, execution_result),
            "reason_code": decision_artifact.get("final_reason_code") or decision.get("reason_code"),
            "reason": decision_artifact.get("final_reason") or decision.get("reason"),
            "selected_lane": _selected_lane(decision),
            "confidence": _number(decision.get("confidence"), signal_row.get("confidence")),
            "edge": _number(decision.get("edge"), signal_row.get("edge")),
            "model_probability": _number(decision.get("win_probability"), signal_row.get("model_probability")),
            "entry_price": _number(decision.get("entry_price"), signal_row.get("market_price")),
            "wallet_id": wallet_contract.wallet_id,
            "policy_id": wallet_contract.policy_id,
            "accounting_ref": build_paper_accounting_ref(
                config,
                session_id=run_id,
                data_dir=data_path,
                trade_id=result_trade_id or None,
                mutates_accounting=bool(accounting_mutated),
            ),
            "mutation_contract": {
                "mutates_shared_candidate": False,
                "mutates_accounting": bool(accounting_mutated),
                "accounting_mutation_scope": "paper_only",
                "accounting_mutation_path": "existing_paper_execution_path",
                "places_orders": False,
            },
            "provenance": {
                "known_at_time": True,
                "source": "live_known_at_time",
                "paper_session_id": run_id,
                "scan_count": int(scan_count or 0),
                "config_hash": decision_artifact.get("config_hash"),
            },
        }
    )
    append_jsonl(data_path / "agent_decisions.jsonl", row)
    return row


def _paper_policy(config: dict[str, Any]) -> str:
    status = strategy_policy_status(config.get("strategy_policy_normalized") or config.get("strategy_policy") or {})
    if status.get("enforce"):
        return "beta_enforce"
    return "normal"


def _paper_session_path(data_path: Path, session_id: str) -> Path:
    return data_path / f"sim_{session_id}.json"


def _market_id(decision_artifact: dict[str, Any], signal: dict[str, Any]) -> str:
    return str(decision_artifact.get("market_id") or signal.get("market_id") or "")


def _decision_dict(decision_artifact: dict[str, Any]) -> dict[str, Any]:
    decision = decision_artifact.get("shared_core_decision")
    return dict(decision) if isinstance(decision, dict) else {}


def _selected_lane(decision: dict[str, Any]) -> str | None:
    reasoning = decision.get("reasoning")
    if not isinstance(reasoning, dict):
        return None
    lane = reasoning.get("strategy_lane")
    if not isinstance(lane, dict):
        return None
    lane_id = lane.get("lane_id")
    return str(lane_id) if lane_id not in (None, "") else None


def _approved_size(decision: dict[str, Any], execution_result: Any | None) -> float | int | None:
    filled_size = _number(getattr(execution_result, "filled_size", None))
    if filled_size is not None:
        return filled_size
    return _number(decision.get("position_size"))


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
