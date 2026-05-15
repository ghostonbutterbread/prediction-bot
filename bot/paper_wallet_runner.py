"""Minimal dual-wallet paper runner for shared-candidate A/B evaluation."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from bot.paper_evaluator_input import (
    SharedCandidatePaperInputLoadResult,
    load_shared_candidate_paper_inputs,
)
from bot.file_ops import load_jsonl
from bot.paper_shadow_lanes import paper_shadow_lanes_enabled, write_paper_shadow_lane_decisions
from bot.paper_wallets import (
    BETA_PAPER_WALLET_ID,
    STABLE_PAPER_WALLET_ID,
    build_paper_wallet_contracts,
    resolve_paper_wallet_contract,
)
from bot.simulator import Simulator
from bot.strategy_policy import coerce_strategy_policy, normalize_strategy_policy


@dataclass(frozen=True, slots=True)
class PaperWalletEvaluationRun:
    wallet_id: str
    policy_id: str
    policy: str
    session_id: str
    data_dir: str
    root_dir: str
    risk_state_path: str
    session_path: str
    agent_run_path: str
    agent_decision_path: str
    candidate_dataset_path: str
    shared_candidate_ids: tuple[str, ...]
    accepted_trade_ids: tuple[str, ...]
    skipped_shared_candidate_ids: tuple[str, ...]
    balance: float
    available_cash: float
    reserved_capital: float


@dataclass(frozen=True, slots=True)
class DualPaperWalletEvaluationResult:
    candidate_dataset_path: str
    loaded_row_count: int
    accepted_candidate_count: int
    wallet_runs: dict[str, PaperWalletEvaluationRun]
    shared_candidate_ids: tuple[str, ...]
    paper_lane_decision_path: str | None = None
    paper_lane_decision_count: int = 0
    paper_lane_ids: tuple[str, ...] = ()


def build_paper_wallet_runner_config(
    config: Mapping[str, Any] | None,
    *,
    wallet_id: str,
    data_dir: str | Path | None = None,
    candidate_dataset_path: str | Path | None = None,
) -> dict[str, Any]:
    """Materialize a wallet-specific paper config for stable or beta evaluation."""

    _validate_wallet_id(wallet_id)
    materialized = copy.deepcopy(dict(config or {}))
    contract = resolve_paper_wallet_contract(materialized, wallet_id=wallet_id, data_dir=data_dir)
    runtime_base_dir = contract.root_dir.parent
    raw_policy = _wallet_strategy_policy(materialized, wallet_id=wallet_id)
    normalized_policy = normalize_strategy_policy(raw_policy)

    runtime = dict(materialized.get("runtime", {}) or {})
    runtime["base_dir"] = str(runtime_base_dir)
    runtime["mode"] = "paper"
    runtime["mode_dir"] = str(contract.root_dir)
    runtime["paper_wallet_id"] = wallet_id
    materialized["runtime"] = runtime
    materialized["data_dir"] = str(contract.root_dir)
    materialized["log_dir"] = str(contract.root_dir)
    if candidate_dataset_path not in (None, ""):
        materialized["paper_candidate_dataset_path"] = str(candidate_dataset_path)
    materialized["strategy_policy"] = raw_policy
    materialized["strategy_policy_normalized"] = normalized_policy

    trading = dict(materialized.get("trading", {}) or {})
    trading["mode"] = "paper"
    materialized["trading"] = trading

    strategy = dict(materialized.get("strategy", {}) or {})
    strategy["strategy_policy"] = copy.deepcopy(raw_policy)
    strategy["strategy_policy_normalized"] = normalized_policy
    materialized["strategy"] = strategy

    paper_wallets = copy.deepcopy(dict(materialized.get("paper_wallets", {}) or {}))
    paper_wallets["active_wallet_id"] = wallet_id
    materialized["paper_wallets"] = paper_wallets
    materialized["paper_wallets"] = build_paper_wallet_contracts(materialized)
    return materialized


def run_shared_candidate_paper_evaluation(
    candidate_dataset_path: str | Path | None = None,
    *,
    config: Mapping[str, Any] | None = None,
    data_dir: str | Path | None = None,
    wallet_ids: tuple[str, ...] | list[str] | None = None,
    load_result: SharedCandidatePaperInputLoadResult | None = None,
) -> DualPaperWalletEvaluationResult:
    """Evaluate one shared-candidate dataset across isolated stable and beta wallets."""

    resolved_wallet_ids = _normalize_wallet_ids(wallet_ids)
    if load_result is None:
        if candidate_dataset_path is None:
            raise ValueError("candidate_dataset_path or load_result is required")
        load_result = load_shared_candidate_paper_inputs(
            candidate_dataset_path,
            config=config,
            data_dir=data_dir,
            wallet_ids=resolved_wallet_ids,
        )

    simulators = {
        wallet_id: Simulator(
            build_paper_wallet_runner_config(
                config,
                wallet_id=wallet_id,
                data_dir=data_dir,
                candidate_dataset_path=load_result.candidate_dataset_path,
            )
        )
        for wallet_id in resolved_wallet_ids
    }
    processed_candidate_ids = {wallet_id: [] for wallet_id in resolved_wallet_ids}
    skipped_candidate_ids = {wallet_id: [] for wallet_id in resolved_wallet_ids}
    accepted_trade_ids = {wallet_id: [] for wallet_id in resolved_wallet_ids}

    try:
        for shared_candidate_id, wallet_inputs in load_result.inputs_by_shared_candidate_id.items():
            for wallet_id in resolved_wallet_ids:
                wallet_input = wallet_inputs.get(wallet_id)
                if wallet_input is None:
                    continue
                processed_candidate_ids[wallet_id].append(shared_candidate_id)
                trade = simulators[wallet_id].submit_paper_signal(copy.deepcopy(wallet_input.signal), persist=False)
                if trade is None:
                    skipped_candidate_ids[wallet_id].append(shared_candidate_id)
                    continue
                accepted_trade_ids[wallet_id].append(str(trade.id))

        for simulator in simulators.values():
            simulator._save_session()
    finally:
        pass

    wallet_runs: dict[str, PaperWalletEvaluationRun] = {}
    for wallet_id, simulator in simulators.items():
        contract = resolve_paper_wallet_contract(
            simulator.config,
            wallet_id=wallet_id,
            session_id=simulator.session_id,
            data_dir=simulator.data_dir,
        )
        wallet_runs[wallet_id] = PaperWalletEvaluationRun(
            wallet_id=contract.wallet_id,
            policy_id=contract.policy_id,
            policy=_wallet_policy_label(simulator.config),
            session_id=simulator.session_id,
            data_dir=str(simulator.data_dir),
            root_dir=str(contract.root_dir),
            risk_state_path=str(contract.risk_state_path),
            session_path=str(contract.session_path),
            agent_run_path=str(simulator.data_dir / "agent_runs.jsonl"),
            agent_decision_path=str(simulator.data_dir / "agent_decisions.jsonl"),
            candidate_dataset_path=load_result.candidate_dataset_path,
            shared_candidate_ids=tuple(processed_candidate_ids[wallet_id]),
            accepted_trade_ids=tuple(accepted_trade_ids[wallet_id]),
            skipped_shared_candidate_ids=tuple(skipped_candidate_ids[wallet_id]),
            balance=round(float(simulator.balance), 2),
            available_cash=round(float(simulator.available_cash), 2),
            reserved_capital=round(float(simulator.reserved_capital), 2),
        )

    lane_write_result = None
    if paper_shadow_lanes_enabled(config):
        ledger_root = _paper_lane_ledger_root(wallet_runs)
        lane_write_result = write_paper_shadow_lane_decisions(
            config=config,
            candidate_dataset_path=load_result.candidate_dataset_path,
            inputs_by_shared_candidate_id=load_result.inputs_by_shared_candidate_id,
            wallet_decision_rows={
                wallet_id: load_jsonl(Path(run.agent_decision_path))
                for wallet_id, run in wallet_runs.items()
            },
            wallet_runs=wallet_runs,
            ledger_root=ledger_root,
        )

    return DualPaperWalletEvaluationResult(
        candidate_dataset_path=load_result.candidate_dataset_path,
        loaded_row_count=load_result.loaded_row_count,
        accepted_candidate_count=load_result.accepted_candidate_count,
        wallet_runs=wallet_runs,
        shared_candidate_ids=tuple(load_result.inputs_by_shared_candidate_id.keys()),
        paper_lane_decision_path=lane_write_result.decision_path if lane_write_result is not None else None,
        paper_lane_decision_count=lane_write_result.rows_written if lane_write_result is not None else 0,
        paper_lane_ids=lane_write_result.lane_ids if lane_write_result is not None else (),
    )


def _wallet_strategy_policy(config: Mapping[str, Any], *, wallet_id: str) -> dict[str, Any]:
    policy = coerce_strategy_policy(config.get("strategy_policy_normalized") or config.get("strategy_policy") or {})
    configured_features = dict(policy.get("configured_features", {}) or {})
    if wallet_id == STABLE_PAPER_WALLET_ID:
        return {
            "version": "stable",
            "beta": {
                "mode": "off",
                "features": configured_features,
            },
        }
    return {
        "version": "beta",
        "beta": {
            "mode": "enforce",
            "features": configured_features,
        },
    }


def _wallet_policy_label(config: Mapping[str, Any]) -> str:
    policy = coerce_strategy_policy(config.get("strategy_policy_normalized") or config.get("strategy_policy") or {})
    if policy.is_enforce:
        return "beta_enforce"
    return "normal"


def _paper_lane_ledger_root(wallet_runs: Mapping[str, PaperWalletEvaluationRun]) -> Path:
    stable = wallet_runs.get(STABLE_PAPER_WALLET_ID)
    if stable is not None:
        return Path(stable.root_dir)
    for run in wallet_runs.values():
        return Path(run.root_dir)
    return Path("data") / "paper"


def _normalize_wallet_ids(wallet_ids: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    requested = tuple(wallet_ids or (STABLE_PAPER_WALLET_ID, BETA_PAPER_WALLET_ID))
    deduped: list[str] = []
    for wallet_id in requested:
        _validate_wallet_id(wallet_id)
        if wallet_id not in deduped:
            deduped.append(wallet_id)
    return tuple(deduped)


def _validate_wallet_id(wallet_id: str) -> None:
    if wallet_id not in {STABLE_PAPER_WALLET_ID, BETA_PAPER_WALLET_ID}:
        raise ValueError(f"unknown paper wallet id: {wallet_id}")


__all__ = [
    "DualPaperWalletEvaluationResult",
    "PaperWalletEvaluationRun",
    "build_paper_wallet_runner_config",
    "run_shared_candidate_paper_evaluation",
]
