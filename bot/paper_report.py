"""Read-only stable-vs-beta paper wallet comparison reporting."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from bot.paper_evaluator_input import SharedCandidatePaperInputLoadResult, load_shared_candidate_paper_inputs
from bot.paper_wallet_runner import DualPaperWalletEvaluationResult, PaperWalletEvaluationRun
from bot.paper_wallets import BETA_PAPER_WALLET_ID, STABLE_PAPER_WALLET_ID, resolve_paper_wallet_contract
from bot.shared_market_feed import shared_candidate_id_from_row

_ACTIVE_ACTIONS = {"BUY_YES", "BUY_NO"}
_DELTA_ACTION = "action_delta"


@dataclass(frozen=True, slots=True)
class _WalletArtifactSource:
    wallet_id: str
    policy_id: str
    policy: str
    root_dir: Path
    risk_state_path: Path
    session_path: Path | None
    session_id: str | None
    agent_run_path: Path
    agent_decision_path: Path
    candidate_dataset_path: str
    shared_candidate_ids: tuple[str, ...]
    accepted_trade_ids: tuple[str, ...]
    require_exact_run_id: bool


def build_stable_beta_paper_report(
    candidate_dataset_path: str | Path | None = None,
    *,
    config: Mapping[str, Any] | None = None,
    data_dir: str | Path | None = None,
    load_result: SharedCandidatePaperInputLoadResult | None = None,
    evaluation_result: DualPaperWalletEvaluationResult | None = None,
) -> dict[str, Any]:
    """Compare stable and beta paper wallets without mutating paper state."""

    if load_result is None:
        resolved_dataset_path = (
            str(candidate_dataset_path)
            if candidate_dataset_path not in (None, "")
            else (evaluation_result.candidate_dataset_path if evaluation_result is not None else None)
        )
        if resolved_dataset_path in (None, ""):
            raise ValueError("candidate_dataset_path, load_result, or evaluation_result is required")
        load_result = load_shared_candidate_paper_inputs(
            resolved_dataset_path,
            config=config,
            data_dir=data_dir,
        )

    artifact_sources = _resolve_wallet_artifact_sources(
        load_result=load_result,
        evaluation_result=evaluation_result,
        config=config,
        data_dir=data_dir,
    )
    shared_candidate_ids = tuple(load_result.inputs_by_shared_candidate_id.keys())
    report: dict[str, Any] = {
        "ready": True,
        "candidate_dataset_path": load_result.candidate_dataset_path,
        "loaded_row_count": load_result.loaded_row_count,
        "accepted_candidate_count": load_result.accepted_candidate_count,
        "skipped_rows": [
            {
                "row_index": skip.row_index,
                "reason_code": skip.reason_code,
                "market_id": skip.market_id,
                "shared_candidate_id": skip.shared_candidate_id,
            }
            for skip in load_result.skipped_rows
        ],
        "shared_candidate_ids": list(shared_candidate_ids),
        "wallets": {},
        "comparisons": [],
        "issues": [],
        "warnings": [],
        "summary": {
            "comparison_rows": len(shared_candidate_ids),
            "candidate_rows_with_any_decision": 0,
            "candidate_rows_with_both_decisions": 0,
            "candidate_rows_with_any_accounting_effect": 0,
            "delta_category_counts": {},
            "outcome_category_counts": {},
            "stable_resolved_pnl": 0.0,
            "beta_resolved_pnl": 0.0,
            "beta_minus_stable_resolved_pnl": 0.0,
        },
    }

    decisions_by_wallet: dict[str, dict[str, dict[str, Any]]] = {}
    trades_by_wallet: dict[str, dict[str, dict[str, Any]]] = {}
    for wallet_id in (STABLE_PAPER_WALLET_ID, BETA_PAPER_WALLET_ID):
        artifact_source = artifact_sources[wallet_id]
        wallet_report, decision_rows, trade_rows = _build_wallet_report(
            artifact_source,
            shared_candidate_ids=shared_candidate_ids,
            report=report,
        )
        report["wallets"][wallet_id] = wallet_report
        decisions_by_wallet[wallet_id] = decision_rows
        trades_by_wallet[wallet_id] = trade_rows

    delta_counts: Counter[str] = Counter()
    outcome_counts: Counter[str] = Counter()
    stable_resolved_pnl = 0.0
    beta_resolved_pnl = 0.0
    any_decision = 0
    both_decisions = 0
    any_trade = 0
    for shared_candidate_id, wallet_inputs in load_result.inputs_by_shared_candidate_id.items():
        comparison = _build_candidate_comparison(
            shared_candidate_id,
            wallet_inputs=wallet_inputs,
            stable_decision=decisions_by_wallet[STABLE_PAPER_WALLET_ID].get(shared_candidate_id),
            beta_decision=decisions_by_wallet[BETA_PAPER_WALLET_ID].get(shared_candidate_id),
            stable_trade=trades_by_wallet[STABLE_PAPER_WALLET_ID].get(shared_candidate_id),
            beta_trade=trades_by_wallet[BETA_PAPER_WALLET_ID].get(shared_candidate_id),
        )
        report["comparisons"].append(comparison)
        delta_counts.update(comparison["delta_categories"])
        outcome = comparison.get("outcome_comparison") or {}
        if outcome.get("outcome_category"):
            outcome_counts[str(outcome["outcome_category"])] += 1
        stable_resolved_pnl += float(outcome.get("stable_net_pnl") or 0.0)
        beta_resolved_pnl += float(outcome.get("beta_net_pnl") or 0.0)
        if comparison["stable_paper"]["decision"] or comparison["beta_paper"]["decision"]:
            any_decision += 1
        if comparison["stable_paper"]["decision"] and comparison["beta_paper"]["decision"]:
            both_decisions += 1
        if comparison["stable_paper"]["accounting_effect"] or comparison["beta_paper"]["accounting_effect"]:
            any_trade += 1

    report["summary"]["candidate_rows_with_any_decision"] = any_decision
    report["summary"]["candidate_rows_with_both_decisions"] = both_decisions
    report["summary"]["candidate_rows_with_any_accounting_effect"] = any_trade
    report["summary"]["delta_category_counts"] = dict(sorted(delta_counts.items()))
    report["summary"]["outcome_category_counts"] = dict(sorted(outcome_counts.items()))
    report["summary"]["stable_resolved_pnl"] = round(stable_resolved_pnl, 2)
    report["summary"]["beta_resolved_pnl"] = round(beta_resolved_pnl, 2)
    report["summary"]["beta_minus_stable_resolved_pnl"] = round(beta_resolved_pnl - stable_resolved_pnl, 2)
    if report["issues"]:
        report["ready"] = False
    return report


def _resolve_wallet_artifact_sources(
    *,
    load_result: SharedCandidatePaperInputLoadResult,
    evaluation_result: DualPaperWalletEvaluationResult | None,
    config: Mapping[str, Any] | None,
    data_dir: str | Path | None,
) -> dict[str, _WalletArtifactSource]:
    if evaluation_result is not None:
        return {
            wallet_id: _artifact_source_from_run(wallet_run, candidate_dataset_path=load_result.candidate_dataset_path)
            for wallet_id, wallet_run in evaluation_result.wallet_runs.items()
            if wallet_id in {STABLE_PAPER_WALLET_ID, BETA_PAPER_WALLET_ID}
        }

    sources: dict[str, _WalletArtifactSource] = {}
    for wallet_id in (STABLE_PAPER_WALLET_ID, BETA_PAPER_WALLET_ID):
        contract = resolve_paper_wallet_contract(config, wallet_id=wallet_id, data_dir=data_dir)
        agent_run_path = contract.root_dir / "agent_runs.jsonl"
        matched_run = _find_run_for_candidate_dataset(agent_run_path, wallet_id=wallet_id, candidate_dataset_path=load_result.candidate_dataset_path)
        session_path = Path(matched_run["session_path"]) if matched_run and matched_run.get("session_path") not in (None, "") else None
        session_id = _optional_text(matched_run.get("run_id")) if matched_run else None
        sources[wallet_id] = _WalletArtifactSource(
            wallet_id=contract.wallet_id,
            policy_id=contract.policy_id,
            policy="beta_enforce" if wallet_id == BETA_PAPER_WALLET_ID else "normal",
            root_dir=contract.root_dir,
            risk_state_path=contract.risk_state_path,
            session_path=session_path,
            session_id=session_id,
            agent_run_path=agent_run_path,
            agent_decision_path=contract.root_dir / "agent_decisions.jsonl",
            candidate_dataset_path=load_result.candidate_dataset_path,
            shared_candidate_ids=tuple(load_result.inputs_by_shared_candidate_id.keys()),
            accepted_trade_ids=(),
            require_exact_run_id=True,
        )
    return sources


def _artifact_source_from_run(
    wallet_run: PaperWalletEvaluationRun,
    *,
    candidate_dataset_path: str,
) -> _WalletArtifactSource:
    session_path = Path(wallet_run.session_path) if wallet_run.session_path not in (None, "") else None
    return _WalletArtifactSource(
        wallet_id=wallet_run.wallet_id,
        policy_id=wallet_run.policy_id,
        policy=wallet_run.policy,
        root_dir=Path(wallet_run.root_dir),
        risk_state_path=Path(wallet_run.risk_state_path),
        session_path=session_path,
        session_id=wallet_run.session_id,
        agent_run_path=Path(wallet_run.agent_run_path),
        agent_decision_path=Path(wallet_run.agent_decision_path),
        candidate_dataset_path=candidate_dataset_path,
        shared_candidate_ids=wallet_run.shared_candidate_ids,
        accepted_trade_ids=wallet_run.accepted_trade_ids,
        require_exact_run_id=True,
    )


def _build_wallet_report(
    artifact_source: _WalletArtifactSource,
    *,
    shared_candidate_ids: tuple[str, ...],
    report: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    risk_state = _read_json_object(artifact_source.risk_state_path, report, required=False, label="risk_state")
    session = (
        _read_json_object(artifact_source.session_path, report, required=False, label="session")
        if artifact_source.session_path is not None
        else None
    )
    agent_run_rows = _read_jsonl_rows(artifact_source.agent_run_path, report, required=True, label=f"{artifact_source.wallet_id} agent_run")
    decision_rows = _read_jsonl_rows(artifact_source.agent_decision_path, report, required=True, label=f"{artifact_source.wallet_id} agent_decision")
    if artifact_source.require_exact_run_id and artifact_source.session_id in (None, ""):
        report["issues"].append(
            f"missing matching {artifact_source.wallet_id} run for candidate dataset: {artifact_source.candidate_dataset_path}"
        )
    decision_by_candidate = _decision_rows_by_candidate(
        decision_rows,
        wallet_id=artifact_source.wallet_id,
        session_id=artifact_source.session_id,
        shared_candidate_ids=shared_candidate_ids,
        require_exact_run_id=artifact_source.require_exact_run_id,
    )
    trade_by_candidate = _session_trades_by_candidate(session, shared_candidate_ids=shared_candidate_ids)
    open_position_count = sum(1 for trade in (session or {}).get("trades", []) if not bool(trade.get("resolved")))
    resolved_trade_count = sum(1 for trade in (session or {}).get("trades", []) if bool(trade.get("resolved")))
    matched_run = _agent_run_row(
        agent_run_rows,
        wallet_id=artifact_source.wallet_id,
        session_id=artifact_source.session_id,
        require_exact_run_id=artifact_source.require_exact_run_id,
    )

    wallet_report = {
        "wallet_id": artifact_source.wallet_id,
        "policy_id": artifact_source.policy_id,
        "policy": artifact_source.policy,
        "root_dir": str(artifact_source.root_dir),
        "candidate_dataset_path": artifact_source.candidate_dataset_path,
        "paths": {
            "risk_state_path": str(artifact_source.risk_state_path),
            "session_path": str(artifact_source.session_path) if artifact_source.session_path is not None else None,
            "agent_run_path": str(artifact_source.agent_run_path),
            "agent_decision_path": str(artifact_source.agent_decision_path),
        },
        "agent_run": matched_run,
        "accounting_state": {
            "risk_state": _summarize_risk_state(risk_state),
            "session": _summarize_session_state(session, session_id=artifact_source.session_id),
        },
        "decision_count": len(decision_by_candidate),
        "matched_trade_count": len(trade_by_candidate),
        "shared_candidate_ids": list(artifact_source.shared_candidate_ids),
        "accepted_trade_ids": list(artifact_source.accepted_trade_ids),
        "open_position_count": max(
            open_position_count,
            int((_mapping(risk_state).get("open_positions") or 0) if isinstance(risk_state, dict) else 0),
        ),
        "resolved_trade_count": resolved_trade_count,
    }
    return wallet_report, decision_by_candidate, trade_by_candidate


def _build_candidate_comparison(
    shared_candidate_id: str,
    *,
    wallet_inputs: Mapping[str, Any],
    stable_decision: dict[str, Any] | None,
    beta_decision: dict[str, Any] | None,
    stable_trade: dict[str, Any] | None,
    beta_trade: dict[str, Any] | None,
) -> dict[str, Any]:
    candidate_input = wallet_inputs.get(STABLE_PAPER_WALLET_ID) or wallet_inputs.get(BETA_PAPER_WALLET_ID)
    assert candidate_input is not None
    stable_decision_view = _decision_view(stable_decision)
    beta_decision_view = _decision_view(beta_decision)
    stable_trade_view = _trade_view(stable_trade)
    beta_trade_view = _trade_view(beta_trade)
    comparison = {
        "shared_candidate_id": shared_candidate_id,
        "candidate_evidence": _candidate_evidence(candidate_input),
        "stable_paper": {
            "decision": stable_decision_view,
            "accounting_effect": stable_trade_view,
        },
        "beta_paper": {
            "decision": beta_decision_view,
            "accounting_effect": beta_trade_view,
        },
    }
    comparison["delta_categories"] = _delta_categories(stable_decision_view, beta_decision_view)
    comparison["outcome_comparison"] = _outcome_comparison(stable_trade_view, beta_trade_view)
    return comparison


def _candidate_evidence(candidate_input: Any) -> dict[str, Any]:
    shared_candidate = _mapping(candidate_input.shared_candidate)
    market = _mapping(shared_candidate.get("market"))
    prices = _mapping(shared_candidate.get("prices"))
    decision = _mapping(shared_candidate.get("decision"))
    evidence = _mapping(shared_candidate.get("evidence"))
    signal = _mapping(candidate_input.signal)
    return {
        "candidate_dataset_path": candidate_input.candidate_dataset_path,
        "observed_at": candidate_input.observed_at,
        "source_runtime": _optional_text(shared_candidate.get("source_runtime")),
        "provenance": _optional_text(shared_candidate.get("provenance")),
        "snapshot_as_of": _optional_text(shared_candidate.get("snapshot_as_of")),
        "market": {
            "market_id": _optional_text(shared_candidate.get("market_id") or market.get("id")),
            "question": _optional_text(market.get("question")),
            "exchange": _optional_text(market.get("exchange")),
            "category": _optional_text(market.get("category") or market.get("series")),
            "group": _optional_text(market.get("group")),
            "route": _mapping(market.get("route")),
        },
        "prices": {
            "market_price": _number(signal.get("market_price")),
            "yes_price": _number(prices.get("yes_price"), signal.get("yes_price"), signal.get("yes_market_price")),
            "no_price": _number(prices.get("no_price"), signal.get("no_price"), signal.get("no_market_price")),
            "best_yes_bid": _number(prices.get("best_yes_bid")),
            "best_yes_ask": _number(prices.get("best_yes_ask")),
            "best_no_bid": _number(prices.get("best_no_bid")),
            "best_no_ask": _number(prices.get("best_no_ask")),
        },
        "decision_context": {
            "direction": _optional_text(signal.get("direction") or decision.get("direction")),
            "model_probability": _number(signal.get("model_probability"), decision.get("model_probability")),
            "edge": _number(signal.get("edge"), decision.get("edge")),
            "confidence": _number(signal.get("confidence"), decision.get("confidence")),
        },
        "evidence": {
            "station_id": _optional_text(signal.get("station_id"), evidence.get("station_id")),
            "source_mode": _optional_text(signal.get("source_mode"), evidence.get("source_mode")),
            "source_as_of": _optional_text(signal.get("source_as_of")),
            "source_agreement_score": _number(signal.get("source_agreement_score"), evidence.get("source_agreement_score")),
            "weather_confidence_score": _number(signal.get("weather_confidence_score"), evidence.get("weather_confidence_score")),
        },
    }


def _decision_rows_by_candidate(
    rows: list[dict[str, Any]],
    *,
    wallet_id: str,
    session_id: str | None,
    shared_candidate_ids: tuple[str, ...],
    require_exact_run_id: bool = False,
) -> dict[str, dict[str, Any]]:
    if require_exact_run_id and session_id in (None, ""):
        return {}
    requested_ids = set(shared_candidate_ids)
    by_candidate: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        candidate_id = shared_candidate_id_from_row(row)
        if candidate_id in (None, "") or candidate_id not in requested_ids:
            continue
        if _optional_text(row.get("wallet_id")) not in (None, wallet_id):
            continue
        row_run_id = _optional_text(row.get("run_id"))
        if require_exact_run_id and row_run_id != session_id:
            continue
        if not require_exact_run_id and session_id not in (None, "") and row_run_id not in (None, session_id):
            continue
        by_candidate[str(candidate_id)] = row
    return by_candidate


def _session_trades_by_candidate(
    session: dict[str, Any] | None,
    *,
    shared_candidate_ids: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    requested_ids = set(shared_candidate_ids)
    trades = (session or {}).get("trades", [])
    if not isinstance(trades, list):
        return {}
    by_candidate: dict[str, dict[str, Any]] = {}
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        candidate_id = _trade_shared_candidate_id(trade)
        if candidate_id in (None, "") or candidate_id not in requested_ids:
            continue
        by_candidate[str(candidate_id)] = trade
    return by_candidate


def _trade_shared_candidate_id(trade: Mapping[str, Any]) -> str | None:
    for candidate in (
        trade,
        _mapping(_mapping(trade.get("decision_artifact")).get("strategy_signal")),
        _mapping(_mapping(trade.get("decision_artifact")).get("trade_context")).get("source_context"),
        _mapping(trade.get("original_signal_snapshot")),
    ):
        candidate_id = shared_candidate_id_from_row(candidate)
        if candidate_id not in (None, ""):
            return str(candidate_id)
    return None


def _agent_run_row(
    rows: list[dict[str, Any]],
    *,
    wallet_id: str,
    session_id: str | None,
    require_exact_run_id: bool = False,
) -> dict[str, Any] | None:
    if require_exact_run_id and session_id in (None, ""):
        return None
    matched: dict[str, Any] | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if _optional_text(row.get("wallet_id")) not in (None, wallet_id):
            continue
        row_run_id = _optional_text(row.get("run_id"))
        if require_exact_run_id and row_run_id != session_id:
            continue
        if not require_exact_run_id and session_id not in (None, "") and row_run_id not in (None, session_id):
            continue
        matched = row
    if matched is None:
        return None
    return {
        "agent_run_id": _optional_text(matched.get("agent_run_id")),
        "run_id": _optional_text(matched.get("run_id")),
        "status": _optional_text(matched.get("status")),
        "started_at": _optional_text(matched.get("started_at")),
        "finished_at": _optional_text(matched.get("finished_at")),
        "candidate_dataset_path": _optional_text(matched.get("candidate_dataset_path")),
        "decision_ledger_path": _optional_text(matched.get("decision_ledger_path")),
        "accounting_namespace": _optional_text(matched.get("accounting_namespace")),
        "accounting_root": _optional_text(matched.get("accounting_root")),
        "risk_state_path": _optional_text(matched.get("risk_state_path")),
        "session_path": _optional_text(matched.get("session_path")),
    }


def _summarize_risk_state(risk_state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(risk_state, dict):
        return None
    return {
        "current_balance": _number(risk_state.get("current_balance")),
        "available_cash": _number(risk_state.get("available_cash")),
        "reserved_capital": _number(risk_state.get("reserved_capital")),
        "open_positions": _int_value(risk_state.get("open_positions")),
        "total_exposure": _number(risk_state.get("total_exposure")),
        "daily_pnl": _number(risk_state.get("daily_pnl")),
        "standby_active": bool(risk_state.get("standby_active", False)),
        "standby_reason_codes": list(risk_state.get("standby_reason_codes") or []),
    }


def _summarize_session_state(session: dict[str, Any] | None, *, session_id: str | None) -> dict[str, Any] | None:
    if not isinstance(session, dict):
        return None
    trades = session.get("trades", [])
    if not isinstance(trades, list):
        trades = []
    open_trades = [trade for trade in trades if isinstance(trade, dict) and not bool(trade.get("resolved"))]
    resolved_trades = [trade for trade in trades if isinstance(trade, dict) and bool(trade.get("resolved"))]
    report = _mapping(session.get("report"))
    return {
        "session_id": _optional_text(session.get("session_id")) or session_id,
        "starting_balance": _number(session.get("starting_balance")),
        "balance": _number(session.get("balance")),
        "available_cash": _number(session.get("available_cash")),
        "reserved_capital": _number(session.get("reserved_capital")),
        "scan_count": _int_value(session.get("scan_count")),
        "open_position_count": len(open_trades),
        "resolved_trade_count": len(resolved_trades),
        "total_trade_count": len(trades),
        "report_pnl": _number(report.get("pnl")),
        "report_total_trades": _int_value(report.get("total_trades")),
        "report_resolved_trades": _int_value(report.get("resolved_trades")),
    }


def _decision_view(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    return {
        "decision_id": _optional_text(row.get("decision_id")),
        "agent_run_id": _optional_text(row.get("agent_run_id")),
        "run_id": _optional_text(row.get("run_id")),
        "action": _normalized_action(row.get("action")),
        "side": _optional_text(row.get("side")),
        "requested_position_size_usd": _number(row.get("requested_position_size_usd")),
        "approved_position_size_usd": _number(row.get("approved_position_size_usd")),
        "reason_code": _optional_text(row.get("reason_code")),
        "reason": _optional_text(row.get("reason")),
        "entry_price": _number(row.get("entry_price"), row.get("price")),
        "observed_at": _optional_text(row.get("observed_at")),
        "decided_at": _optional_text(row.get("decided_at")),
        "wallet_id": _optional_text(row.get("wallet_id")),
        "policy_id": _optional_text(row.get("policy_id")),
        "accounting_ref": _mapping(row.get("accounting_ref")) or None,
    }


def _trade_view(trade: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(trade, dict):
        return None
    return {
        "trade_id": _optional_text(trade.get("id")),
        "status": _optional_text(trade.get("status")),
        "lifecycle_state": _optional_text(trade.get("lifecycle_state")),
        "direction": _normalized_action(trade.get("direction")),
        "position_size_usd": _number(trade.get("position_size")),
        "reserved_capital": _number(trade.get("reserved_capital")),
        "entry_price": _number(trade.get("entry_price"), trade.get("market_price")),
        "resolved": bool(trade.get("resolved", False)),
        "outcome": _optional_text(trade.get("outcome")),
        "net_pnl": _number(trade.get("net_pnl"), trade.get("pnl")),
        "resolution_type": _optional_text(trade.get("resolution_type")),
        "resolved_at": _optional_text(trade.get("resolved_at")),
    }


def _delta_categories(stable_decision: dict[str, Any] | None, beta_decision: dict[str, Any] | None) -> list[str]:
    stable_action = _normalized_action((stable_decision or {}).get("action"))
    beta_action = _normalized_action((beta_decision or {}).get("action"))
    stable_active = stable_action in _ACTIVE_ACTIONS
    beta_active = beta_action in _ACTIVE_ACTIONS
    categories: list[str] = []

    if stable_action not in (None, "") and stable_action == beta_action:
        categories.append("same_action")
    elif stable_active and not beta_active:
        categories.append("stable_only")
    elif beta_active and not stable_active:
        categories.append("beta_only")
    elif stable_action not in (None, "") and beta_action not in (None, "") and stable_action != beta_action:
        categories.append(_DELTA_ACTION)

    stable_size = _number((stable_decision or {}).get("approved_position_size_usd"), (stable_decision or {}).get("requested_position_size_usd"))
    beta_size = _number((beta_decision or {}).get("approved_position_size_usd"), (beta_decision or {}).get("requested_position_size_usd"))
    if stable_active and beta_active and stable_size is not None and beta_size is not None and abs(stable_size - beta_size) > 0.009:
        categories.append("size_delta")

    stable_reason = _optional_text((stable_decision or {}).get("reason_code"), (stable_decision or {}).get("reason"))
    beta_reason = _optional_text((beta_decision or {}).get("reason_code"), (beta_decision or {}).get("reason"))
    if stable_reason != beta_reason and (stable_reason is not None or beta_reason is not None):
        categories.append("reason_delta")
    return categories


def _find_run_for_candidate_dataset(
    agent_run_path: Path,
    *,
    wallet_id: str,
    candidate_dataset_path: str,
) -> dict[str, Any] | None:
    matched: dict[str, Any] | None = None
    if not agent_run_path.exists():
        return None
    try:
        lines = agent_run_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if _optional_text(row.get("wallet_id")) not in (None, wallet_id):
            continue
        if _optional_text(row.get("candidate_dataset_path")) != str(candidate_dataset_path):
            continue
        matched = row
    return matched


def _read_jsonl_rows(
    path: Path,
    report: dict[str, Any],
    *,
    required: bool,
    label: str,
) -> list[dict[str, Any]]:
    if not path.exists():
        message = f"missing {label} JSONL file: {path}"
        if required:
            report["issues"].append(message)
        else:
            report["warnings"].append(message)
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        message = f"unreadable {label} JSONL file {path}: {exc}"
        if required:
            report["issues"].append(message)
        else:
            report["warnings"].append(message)
        return []
    for line_number, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            message = f"invalid JSON in {label} JSONL file {path}:{line_number}: {exc.msg}"
            if required:
                report["issues"].append(message)
            else:
                report["warnings"].append(message)
            continue
        if not isinstance(row, dict):
            message = f"non-object row in {label} JSONL file {path}:{line_number}"
            if required:
                report["issues"].append(message)
            else:
                report["warnings"].append(message)
            continue
        rows.append(row)
    return rows


def _outcome_comparison(stable_trade: dict[str, Any] | None, beta_trade: dict[str, Any] | None) -> dict[str, Any]:
    stable_pnl = _resolved_pnl(stable_trade)
    beta_pnl = _resolved_pnl(beta_trade)
    category = None
    if stable_pnl is not None and beta_pnl is not None:
        delta = beta_pnl - stable_pnl
        if abs(delta) <= 0.009:
            category = "same_resolved_pnl"
        elif delta > 0:
            category = "beta_outperformed"
        else:
            category = "stable_outperformed"
    elif stable_pnl is not None and beta_trade is None:
        if stable_pnl < 0:
            category = "beta_avoided_stable_loss"
        elif stable_pnl > 0:
            category = "beta_missed_stable_winner"
        else:
            category = "stable_only_flat"
    elif beta_pnl is not None and stable_trade is None:
        if beta_pnl > 0:
            category = "beta_only_winner"
        elif beta_pnl < 0:
            category = "beta_only_loss"
        else:
            category = "beta_only_flat"
    elif stable_trade is not None or beta_trade is not None:
        category = "unresolved_trade"
    return {
        "stable_net_pnl": stable_pnl,
        "beta_net_pnl": beta_pnl,
        "beta_minus_stable_net_pnl": (round((beta_pnl or 0.0) - (stable_pnl or 0.0), 2) if stable_pnl is not None or beta_pnl is not None else None),
        "outcome_category": category,
    }


def _resolved_pnl(trade: dict[str, Any] | None) -> float | None:
    if not isinstance(trade, dict) or not bool(trade.get("resolved")):
        return None
    return _number(trade.get("net_pnl"))


def _read_json_object(
    path: Path | None,
    report: dict[str, Any],
    *,
    required: bool,
    label: str,
) -> dict[str, Any] | None:
    if path is None:
        if required:
            report["issues"].append(f"missing required {label} path")
        return None
    if not path.exists():
        message = f"missing {label} file: {path}"
        if required:
            report["issues"].append(message)
        else:
            report["warnings"].append(message)
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        message = f"invalid JSON in {label} file {path}: {exc.msg}"
        if required:
            report["issues"].append(message)
        else:
            report["warnings"].append(message)
        return None
    if not isinstance(payload, dict):
        message = f"non-object JSON in {label} file {path}"
        if required:
            report["issues"].append(message)
        else:
            report["warnings"].append(message)
        return None
    return payload


def _latest_session_path(root_dir: Path) -> Path | None:
    session_files = sorted(root_dir.glob("sim_*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    return session_files[0] if session_files else None


def _session_id_from_path(path: Path | None) -> str | None:
    if path is None:
        return None
    stem = path.stem
    if not stem.startswith("sim_"):
        return None
    return stem[4:] or None


def _normalized_action(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value).strip().upper()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _number(*values: Any) -> float | None:
    for value in values:
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _int_value(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_text(*values: Any) -> str | None:
    for value in values:
        if value not in (None, ""):
            return str(value)
    return None


__all__ = ["build_stable_beta_paper_report"]
