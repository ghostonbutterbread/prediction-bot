"""Lightweight paper-only decision lanes over shared candidates."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import json
import re
from typing import Any, Iterable, Mapping

from bot.agent_decision_ledger import (
    build_agent_decision_id,
    build_agent_run_id,
    validate_agent_decision_row,
)
from bot.file_ops import append_jsonl, atomic_write_json, load_jsonl
from bot.paper_wallets import BETA_PAPER_WALLET_ID, STABLE_PAPER_WALLET_ID
from bot.weather.thresholds import extract_threshold_value, infer_question_side

PAPER_LANE_AGENT_ID = "paper"
PAPER_LANE_RUNTIME = "paper"
PAPER_LANE_DECISION_ROLE = "paper_lane"
PAPER_LANE_SCHEMA_VERSION = 1
PAPER_LANE_RESOLUTION_SCHEMA_VERSION = 1
PAPER_LANE_INCREMENTAL_PNL_SCHEMA_VERSION = 1
DEFAULT_CONFIDENCE_FLOOR = 0.58
DEFAULT_LANE_IDS = ("control_stable", "shadow_current_beta", "shadow_confidence_floor")
PREMIUM_CITY_LANE_ID = "shadow_premium_city"
SOURCE_RELIABILITY_LANE_ID = "shadow_source_reliability"
SOURCE_SCOREBOARD_LANE_ID = "shadow_source_scoreboard"
SOURCE_ROUTER_LANE_ID = "shadow_source_router"
SOURCE_ROUTER_NO_PRICE_GUARD_LANE_ID = "shadow_source_router_no_price_guard"
SOURCE_RELIABILITY_EVALUATOR_LANE_IDS = frozenset({SOURCE_RELIABILITY_LANE_ID, SOURCE_SCOREBOARD_LANE_ID})
SOURCE_SCOREBOARD_LANE_IDS = frozenset({SOURCE_SCOREBOARD_LANE_ID})
SOURCE_COLLECTION_LANE_IDS = frozenset(
    {SOURCE_SCOREBOARD_LANE_ID, SOURCE_ROUTER_LANE_ID, SOURCE_ROUTER_NO_PRICE_GUARD_LANE_ID}
)
SOURCE_SCOREBOARD_CONFIG_LANE_IDS = frozenset(
    {
        SOURCE_RELIABILITY_LANE_ID,
        SOURCE_SCOREBOARD_LANE_ID,
        SOURCE_ROUTER_LANE_ID,
        SOURCE_ROUTER_NO_PRICE_GUARD_LANE_ID,
    }
)
KNOWN_LANE_IDS = (
    *DEFAULT_LANE_IDS,
    PREMIUM_CITY_LANE_ID,
    SOURCE_RELIABILITY_LANE_ID,
    SOURCE_SCOREBOARD_LANE_ID,
    SOURCE_ROUTER_LANE_ID,
    SOURCE_ROUTER_NO_PRICE_GUARD_LANE_ID,
)
REPO_ROOT = Path(__file__).resolve().parent.parent
MAX_COMPACT_FUTURE_PNL_QUESTION_CHARS = 200

try:
    import yaml
except ImportError:  # pragma: no cover - requirements include PyYAML.
    yaml = None


@dataclass(frozen=True, slots=True)
class PaperShadowLaneWriteResult:
    decision_path: str
    rows_written: int
    lane_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _LaneDefinition:
    lane_id: str
    lane_type: str = "passthrough"
    source_wallet_id: str | None = None
    source_role: str = "baseline"
    input_source: str = "shared_candidate_dataset"
    input_market_source: str = "shared_market"
    description: str | None = None
    enabled: bool = True
    parameters: Mapping[str, Any] = field(default_factory=dict)
    definition_path: str | None = None


def paper_shadow_lanes_enabled(config: Mapping[str, Any] | None) -> bool:
    cfg = _lane_config(config)
    return _truthy(cfg.get("enabled", False))


def write_paper_shadow_lane_decisions(
    *,
    config: Mapping[str, Any] | None,
    candidate_dataset_path: str | Path,
    inputs_by_shared_candidate_id: Mapping[str, Mapping[str, Any]],
    wallet_decision_rows: Mapping[str, list[dict[str, Any]]],
    wallet_runs: Mapping[str, Any],
    ledger_root: str | Path,
) -> PaperShadowLaneWriteResult:
    """Append compact non-mutating paper lane decisions for shared candidates."""

    cfg = _lane_config(config)
    lane_definitions = _lane_definitions(cfg)
    decision_path = _lane_decision_path(cfg, ledger_root=ledger_root)
    indexed_decisions = {
        wallet_id: _index_decisions_by_candidate(
            rows,
            wallet_id=wallet_id,
            run_id=str(getattr(wallet_runs.get(wallet_id), "session_id", "") or ""),
            candidate_dataset_path=str(candidate_dataset_path),
        )
        for wallet_id, rows in wallet_decision_rows.items()
    }
    run_id = _lane_run_id(wallet_runs)
    agent_run_id = build_agent_run_id(agent_id=PAPER_LANE_AGENT_ID, run_id=run_id)
    rows_written = 0

    for shared_candidate_id, wallet_inputs in inputs_by_shared_candidate_id.items():
        candidate_input = _candidate_input(wallet_inputs)
        signal = _candidate_signal(candidate_input)
        shared_candidate = _shared_candidate(candidate_input)
        stable_decision = indexed_decisions.get(STABLE_PAPER_WALLET_ID, {}).get(shared_candidate_id)
        beta_decision = indexed_decisions.get(BETA_PAPER_WALLET_ID, {}).get(shared_candidate_id)
        source_rows = {
            STABLE_PAPER_WALLET_ID: stable_decision,
            BETA_PAPER_WALLET_ID: beta_decision,
        }
        for lane in lane_definitions:
            row = _build_lane_row(
                lane,
                config=cfg,
                run_id=run_id,
                agent_run_id=agent_run_id,
                candidate_dataset_path=candidate_dataset_path,
                shared_candidate_id=shared_candidate_id,
                signal=signal,
                shared_candidate=shared_candidate,
                source_rows=source_rows,
                decision_path=decision_path,
            )
            append_jsonl(decision_path, row)
            rows_written += 1

    return PaperShadowLaneWriteResult(
        decision_path=str(decision_path),
        rows_written=rows_written,
        lane_ids=tuple(lane.lane_id for lane in lane_definitions),
    )


def _build_lane_row(
    lane: _LaneDefinition,
    *,
    config: Mapping[str, Any],
    run_id: str,
    agent_run_id: str,
    candidate_dataset_path: str | Path,
    shared_candidate_id: str,
    signal: Mapping[str, Any],
    shared_candidate: Mapping[str, Any],
    source_rows: Mapping[str, dict[str, Any] | None],
    decision_path: Path,
) -> dict[str, Any]:
    decision = _evaluate_lane_decision(lane, signal, shared_candidate, source_rows)

    observed_at = _observed_at(decision.get("source_row"), signal)
    market_id = _text(
        signal.get("market_id"),
        (decision.get("source_row") or {}).get("market_id") if isinstance(decision.get("source_row"), dict) else None,
    )
    policy = lane.lane_id
    source_row = decision.get("source_row") if isinstance(decision.get("source_row"), dict) else None
    baseline_row = source_rows.get(STABLE_PAPER_WALLET_ID)
    comparison_row = source_rows.get(BETA_PAPER_WALLET_ID)
    input_confidence = _number(signal.get("confidence"))
    source_confidence = _number((source_row or {}).get("confidence"))
    baseline_confidence = _number((baseline_row or {}).get("confidence"))
    comparison_confidence = _number((comparison_row or {}).get("confidence"))
    confidence_before = _number(input_confidence, source_confidence)
    confidence_after = _number(decision.get("confidence_after"), confidence_before)
    action = str(decision.get("action") or "SKIP")
    shared_candidate_ref = _shared_candidate_ref(
        shared_candidate,
        signal=signal,
        shared_candidate_id=shared_candidate_id,
        candidate_dataset_path=candidate_dataset_path,
    )
    shared_snapshot_id = _optional_text(shared_candidate_ref.get("shared_snapshot_id"))
    row = {
        "schema_name": "agent_decision",
        "schema_version": 1,
        "decision_id": build_agent_decision_id(
            agent_run_id=agent_run_id,
            agent_id=PAPER_LANE_AGENT_ID,
            runtime=PAPER_LANE_RUNTIME,
            policy=policy,
            decision_role=PAPER_LANE_DECISION_ROLE,
            shared_candidate_id=shared_candidate_id,
            run_id=run_id,
            market_id=market_id,
            observed_at=observed_at,
        ),
        "agent_run_id": agent_run_id,
        "agent_id": PAPER_LANE_AGENT_ID,
        "runtime": PAPER_LANE_RUNTIME,
        "policy": policy,
        "decision_role": PAPER_LANE_DECISION_ROLE,
        "shared_candidate_id": shared_candidate_id,
        "candidate_dataset_path": str(candidate_dataset_path),
        "candidate_dataset_identity": lane.input_source,
        "input_source": lane.input_source,
        "input_market_source": lane.input_market_source,
        "shared_candidate": shared_candidate_ref,
        "shared_candidate_source_runtime": shared_candidate_ref.get("source_runtime"),
        "shared_candidate_provenance": shared_candidate_ref.get("provenance"),
        "shared_candidate_observed_at": shared_candidate_ref.get("observed_at"),
        "shared_candidate_snapshot_as_of": shared_candidate_ref.get("snapshot_as_of"),
        "run_id": run_id,
        "market_id": market_id,
        "observed_at": observed_at,
        "decided_at": observed_at,
        "action": action,
        "side": _side_from_action(action),
        "requested_position_size_usd": _number(decision.get("requested_position_size_usd")),
        "approved_position_size_usd": _number(decision.get("approved_position_size_usd")),
        "reason_code": decision.get("reason_code"),
        "reason": decision.get("reason"),
        "selected_lane": lane.lane_id,
        "lane_description": lane.description,
        "confidence": confidence_after,
        "edge": _number(signal.get("edge"), (source_row or {}).get("edge")),
        "model_probability": _number(signal.get("model_probability"), (source_row or {}).get("model_probability")),
        "entry_price": _shadow_entry_price_for_side(
            _side_from_action(action),
            signal,
            source_row or {},
        ),
        "price": _shadow_entry_price_for_side(
            _side_from_action(action),
            signal,
            source_row or {},
        ),
        "accounting_ref": {
            "wallet_id": "paper_shadow_lanes",
            "policy_id": policy,
            "policy_version": PAPER_LANE_SCHEMA_VERSION,
            "namespace": str(decision_path.parent),
            "ledger_path": str(decision_path),
            "mutates_balance": False,
            "mutates_accounting": False,
            "places_live_orders": False,
            "balance_model": "none_decision_only",
        },
        "mutation_contract": {
            "mutates_shared_candidate": False,
            "mutates_accounting": False,
            "accounting_mutation_scope": "none",
            "accounting_mutation_path": None,
            "places_orders": False,
        },
        "provenance": {
            "known_at_time": True,
            "source": "paper_shadow_lanes",
            "lane_id": lane.lane_id,
            "lane_description": lane.description,
            "lane_version": PAPER_LANE_SCHEMA_VERSION,
            "input_source": lane.input_source,
            "input_market_source": lane.input_market_source,
            "shared_candidate": shared_candidate_ref,
            "shared_candidate_id": shared_candidate_id,
            "candidate_dataset_path": str(candidate_dataset_path),
            "input_confidence": input_confidence,
            "source_confidence": source_confidence,
            "baseline_confidence": baseline_confidence,
            "comparison_confidence": comparison_confidence,
            "confidence_before": confidence_before,
            "confidence_after": confidence_after,
            "baseline_decision_id": (baseline_row or {}).get("decision_id"),
            "baseline_policy": (baseline_row or {}).get("policy"),
            "baseline_decision_role": (baseline_row or {}).get("decision_role"),
            "baseline_wallet_id": (baseline_row or {}).get("wallet_id"),
            "baseline_action": (baseline_row or {}).get("action"),
            "comparison_decision_id": (comparison_row or {}).get("decision_id"),
            "comparison_policy": (comparison_row or {}).get("policy"),
            "comparison_decision_role": (comparison_row or {}).get("decision_role"),
            "comparison_wallet_id": (comparison_row or {}).get("wallet_id"),
            "comparison_action": (comparison_row or {}).get("action"),
            "source_decision_id": (source_row or {}).get("decision_id"),
            "source_policy": (source_row or {}).get("policy"),
            "source_decision_role": (source_row or {}).get("decision_role"),
            "source_wallet_id": (source_row or {}).get("wallet_id"),
            "source_role": lane.source_role,
            "source_action": (source_row or {}).get("action"),
            "decision_only": True,
        },
    }
    if isinstance(decision.get("source_reliability"), Mapping):
        row["provenance"]["source_reliability"] = dict(decision["source_reliability"])
        if _is_source_scoreboard_lane(lane.lane_id):
            source_scoreboard = _source_scoreboard_provenance(
                lane,
                signal=signal,
                shared_candidate=shared_candidate,
                source_row=source_row,
                source_reliability=decision["source_reliability"],
            )
            row["provenance"]["source_scoreboard"] = source_scoreboard
            if isinstance(source_scoreboard.get("future_pnl_inputs"), Mapping):
                row["provenance"]["future_pnl_inputs"] = dict(source_scoreboard["future_pnl_inputs"])
                side_price = _price_number(source_scoreboard["future_pnl_inputs"].get("estimated_fill_price"))
                if side_price is not None and _is_buy_action(action):
                    row["entry_price"] = side_price
                    row["price"] = side_price
    if isinstance(decision.get("source_router"), Mapping):
        source_router = _source_router_provenance(
            lane,
            signal=signal,
            shared_candidate=shared_candidate,
            source_row=source_row,
            source_router=decision["source_router"],
            requested_position_size_usd=_number(decision.get("requested_position_size_usd")),
            approved_position_size_usd=_number(decision.get("approved_position_size_usd")),
        )
        row["provenance"]["source_router"] = source_router
        if isinstance(source_router.get("future_pnl_inputs"), Mapping):
            row["provenance"]["future_pnl_inputs"] = dict(source_router["future_pnl_inputs"])
            side_price = _price_number(source_router["future_pnl_inputs"].get("estimated_fill_price"))
            if side_price is not None:
                row["entry_price"] = side_price
                row["price"] = side_price
            elif _is_buy_action(action):
                row["entry_price"] = _price_number(row.get("entry_price"))
                row["price"] = _price_number(row.get("price"))
    if lane.definition_path:
        row["lane_definition_path"] = lane.definition_path
        row["provenance"]["lane_definition_path"] = lane.definition_path
    if shared_snapshot_id:
        row["shared_snapshot_id"] = shared_snapshot_id
        row["provenance"]["shared_snapshot_id"] = shared_snapshot_id
    return validate_agent_decision_row(row)


def _evaluate_lane_decision(
    lane: _LaneDefinition,
    signal: Mapping[str, Any],
    shared_candidate: Mapping[str, Any],
    source_rows: Mapping[str, dict[str, Any] | None],
) -> dict[str, Any]:
    source_wallet_id = lane.source_wallet_id or STABLE_PAPER_WALLET_ID
    evaluator = _lane_evaluator(lane)
    return evaluator(lane, signal, source_rows.get(source_wallet_id), shared_candidate)


def _lane_evaluator(lane: _LaneDefinition):
    lane_type = str(lane.lane_type or "").strip()
    if lane.lane_id == "shadow_confidence_floor":
        lane_type = "confidence_floor"
    elif lane.lane_id == PREMIUM_CITY_LANE_ID:
        lane_type = "premium_city"
    elif lane.lane_id == SOURCE_ROUTER_LANE_ID:
        lane_type = "source_router"
    elif lane.lane_id in SOURCE_RELIABILITY_EVALUATOR_LANE_IDS:
        lane_type = "source_reliability"
    elif lane_type == "source_scoreboard":
        lane_type = "source_reliability"
    evaluator = LANE_EVALUATORS.get(lane_type)
    if evaluator is None:
        raise ValueError(f"unknown paper shadow lane type for {lane.lane_id}: {lane_type}")
    return evaluator


def _passthrough_decision(
    lane: _LaneDefinition,
    signal: Mapping[str, Any],
    source_row: dict[str, Any] | None,
    shared_candidate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not source_row:
        return {
            "source_row": None,
            "action": "SKIP",
            "reason_code": f"{lane.source_wallet_id or 'source'}_decision_missing",
            "reason": "Source wallet decision row was not available",
            "confidence_after": _number(signal.get("confidence")),
            "requested_position_size_usd": None,
            "approved_position_size_usd": 0.0,
        }
    return {
        "source_row": source_row,
        "action": source_row.get("action") or "SKIP",
        "reason_code": source_row.get("reason_code") or "source_decision",
        "reason": source_row.get("reason"),
        "confidence_after": _number(signal.get("confidence"), source_row.get("confidence")),
        "requested_position_size_usd": _number(source_row.get("requested_position_size_usd")),
        "approved_position_size_usd": _number(source_row.get("approved_position_size_usd")),
    }


def _confidence_floor_decision(
    lane: _LaneDefinition,
    signal: Mapping[str, Any],
    source_row: dict[str, Any] | None,
    shared_candidate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    baseline = _passthrough_decision(
        _LaneDefinition("control_stable", source_wallet_id=lane.source_wallet_id or STABLE_PAPER_WALLET_ID),
        signal,
        source_row,
        shared_candidate,
    )
    confidence = _number(signal.get("confidence"), (source_row or {}).get("confidence"))
    floor = _confidence_floor(lane)
    baseline["confidence_after"] = confidence
    action = str(baseline.get("action") or "SKIP").upper()
    if action not in {"BUY_YES", "BUY_NO"}:
        baseline.update(
            {
                "action": "SKIP",
                "reason_code": "baseline_skip",
                "reason": baseline.get("reason") or "Stable baseline did not produce a buy decision",
                "approved_position_size_usd": 0.0,
            }
        )
        return baseline
    if confidence is None or confidence < floor:
        baseline.update(
            {
                "action": "SKIP",
                "reason_code": "confidence_below_floor",
                "reason": f"Stable paper buy requires confidence >= configured floor {floor:.2f}",
                "approved_position_size_usd": 0.0,
            }
        )
        return baseline
    baseline.update(
        {
            "reason_code": "approved_confidence_floor",
            "reason": f"Stable paper buy meets configured confidence floor {floor:.2f}",
        }
    )
    return baseline


def _premium_city_decision(
    lane: _LaneDefinition,
    signal: Mapping[str, Any],
    source_row: dict[str, Any] | None,
    shared_candidate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    baseline = _passthrough_decision(
        _LaneDefinition("control_stable", source_wallet_id=lane.source_wallet_id or STABLE_PAPER_WALLET_ID),
        signal,
        source_row,
        shared_candidate,
    )
    allowlist = _city_token_set(lane.parameters.get("allowlist", []))
    if allowlist and _candidate_city_tokens(signal) & allowlist:
        baseline["reason_code"] = "approved_premium_city"
        return baseline
    baseline.update(
        {
            "action": "SKIP",
            "reason_code": "premium_city_not_allowlisted",
            "reason": "Candidate city is not enabled for the premium city lane",
            "approved_position_size_usd": 0.0,
        }
    )
    return baseline


def _source_reliability_decision(
    lane: _LaneDefinition,
    signal: Mapping[str, Any],
    source_row: dict[str, Any] | None,
    shared_candidate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    baseline = _passthrough_decision(
        _LaneDefinition("control_stable", source_wallet_id=lane.source_wallet_id or STABLE_PAPER_WALLET_ID),
        signal,
        source_row,
        shared_candidate,
    )
    scoreboard_path = _source_reliability_scoreboard_path(lane)
    if not scoreboard_path:
        baseline["source_reliability"] = {
            "available": False,
            "reason_code": "source_reliability_unavailable",
            "recommended_action": "SKIP",
            "effect": "unavailable",
            "reason": "Source reliability scoreboard is not configured for this shadow lane",
        }
        return baseline
    if not Path(scoreboard_path).exists():
        baseline["source_reliability"] = {
            "available": False,
            "reason_code": "source_reliability_scoreboard_missing",
            "recommended_action": "SKIP",
            "effect": "unavailable",
            "reason": "Source reliability scoreboard file is not available for this shadow lane",
            "scoreboard_path": scoreboard_path,
        }
        return baseline

    from bot.weather.source_reliability import (
        SourceReliabilityTable,
        apply_source_reliability_confidence,
        build_reliability_candidate_row,
        evaluate_source_reliability_candidate,
    )

    table = SourceReliabilityTable.from_path(scoreboard_path)
    candidate_row = build_reliability_candidate_row(signal, shared_candidate)
    evaluation = evaluate_source_reliability_candidate(
        candidate_row,
        table,
        action=str(baseline.get("action") or "SKIP"),
    )
    metadata = evaluation.to_dict()
    metadata["scoreboard_path"] = scoreboard_path
    metadata["available"] = True
    confidence_before = _number(signal.get("confidence"), baseline.get("confidence_after"))
    metadata["confidence_before"] = confidence_before
    metadata["confidence_after"] = apply_source_reliability_confidence(confidence_before, evaluation)
    metadata["decision_contract"] = "recommendation_only_top_level_lane_action_unchanged"
    baseline["source_reliability"] = metadata
    return baseline


def _source_router_price_guard(
    signal: Mapping[str, Any],
    action: str,
    parameters: Mapping[str, Any],
) -> str | None:
    """Return a fail-closed reason when an optional router action/price guard rejects."""
    allowed_actions = {
        str(value).strip().upper()
        for value in (parameters.get("allowed_actions") or [])
        if str(value).strip()
    }
    normalized_action = str(action or "").strip().upper()
    if allowed_actions and normalized_action not in allowed_actions:
        return "source_router_action_not_allowed"

    configured_ranges = parameters.get("allowed_entry_price_ranges") or []
    if not configured_ranges:
        return None
    price_key = "best_yes_ask" if normalized_action == "BUY_YES" else "best_no_ask"
    price = _number(signal.get(price_key))
    if price is None:
        return "source_router_price_unavailable"
    for candidate_range in configured_ranges:
        if not isinstance(candidate_range, (list, tuple)) or len(candidate_range) != 2:
            continue
        lower, upper = _number(candidate_range[0]), _number(candidate_range[1])
        if lower is not None and upper is not None and lower <= price < upper:
            return None
    return "source_router_price_outside_allowed_ranges"


def _source_router_decision(
    lane: _LaneDefinition,
    signal: Mapping[str, Any],
    source_row: dict[str, Any] | None,
    shared_candidate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    baseline = _passthrough_decision(
        _LaneDefinition("control_stable", source_wallet_id=lane.source_wallet_id or STABLE_PAPER_WALLET_ID),
        signal,
        source_row,
        shared_candidate,
    )
    from bot.weather.source_confidence import build_source_confidence_row
    from bot.weather.source_reliability import build_reliability_candidate_row, load_scoreboard_rows

    candidate_row = build_reliability_candidate_row(signal, shared_candidate)
    if not _optional_text(candidate_row.get("predicted_outcome")):
        candidate_row["predicted_outcome"] = "YES"
        candidate_row["source_router_candidate_outcome_default"] = "market_yes_event"
    scoreboard_path = _source_reliability_scoreboard_path(lane)
    reliability_rows = load_scoreboard_rows(scoreboard_path) if scoreboard_path and Path(scoreboard_path).exists() else None
    confidence_row = build_source_confidence_row(candidate_row, reliability_table=reliability_rows)
    source_direction = _optional_text(confidence_row.get("source_direction"))
    action = _action_from_source_direction(source_direction)
    if action == "SKIP":
        reason_code = _optional_text(confidence_row.get("reason_code")) or "source_router_no_trade"
        reason = "Source router did not have a usable source direction for this candidate"
        requested = 0.0
        approved = 0.0
    else:
        guard_reason = _source_router_price_guard(signal, action, lane.parameters)
        if guard_reason:
            action = "SKIP"
            reason_code = guard_reason
            reason = "Source router decision did not satisfy this lane's action/price guard"
            requested = 0.0
            approved = 0.0
        else:
            reason_code = f"source_router_{source_direction.lower()}"
            reason = "Source router shadow lane selected the source-implied market side"
            requested = _source_router_notional(lane, baseline)
            approved = requested
            min_edge = _number(lane.parameters.get("min_edge")) or 0.0
            if min_edge > 0:
                edge = _compute_source_router_edge(signal, action)
                if edge is None:
                    action = "SKIP"
                    reason_code = "source_router_edge_unavailable"
                    reason = "Source router selected a side but edge could not be computed"
                    requested = 0.0
                    approved = 0.0
                elif edge < min_edge:
                    action = "SKIP"
                    reason_code = "source_router_insufficient_edge"
                    reason = (
                        f"Source router selected {source_direction} but edge "
                        f"{edge:.4f} < minimum {min_edge:.4f}"
                    )
                    requested = 0.0
                    approved = 0.0
    return {
        "source_row": source_row,
        "action": action,
        "reason_code": reason_code,
        "reason": reason,
        "confidence_after": _number(confidence_row.get("source_confidence_score"), signal.get("confidence")),
        "requested_position_size_usd": requested,
        "approved_position_size_usd": approved,
        "source_router": {
            "available": True,
            "schema": confidence_row.get("schema"),
            "engine_version": confidence_row.get("engine_version"),
            "source_direction": source_direction,
            "source_grade": confidence_row.get("source_grade"),
            "source_confidence_score": confidence_row.get("source_confidence_score"),
            "confidence_type": confidence_row.get("confidence_type"),
            "recommended_action": action,
            "source_confidence_recommended_action": confidence_row.get("recommended_action"),
            "reason_code": reason_code,
            "source_confidence_reason_code": confidence_row.get("reason_code"),
            "agreement_state": confidence_row.get("agreement_state"),
            "weighted_support": confidence_row.get("weighted_support"),
            "weighted_dissent": confidence_row.get("weighted_dissent"),
            "sources_used": confidence_row.get("sources_used") or [],
            "sources_excluded": confidence_row.get("sources_excluded") or [],
            "source_observations": confidence_row.get("source_observations") or [],
            "data_quality": confidence_row.get("data_quality") or {},
            "scoreboard_path": scoreboard_path,
            "decision_contract": "shadow_lane_recommendation_only_no_accounting_mutation",
        },
    }


LANE_EVALUATORS = {
    "passthrough": _passthrough_decision,
    "confidence_floor": _confidence_floor_decision,
    "premium_city": _premium_city_decision,
    "source_reliability": _source_reliability_decision,
    "source_router": _source_router_decision,
}


def _lane_definitions(config: Mapping[str, Any]) -> tuple[_LaneDefinition, ...]:
    definitions_by_id = _configured_lane_definition_map(config)
    requested = config.get("enabled_lanes")
    if requested in (None, ""):
        requested = config.get("lanes")

    if requested in (None, ""):
        lane_ids = [lane_id for lane_id in DEFAULT_LANE_IDS if definitions_by_id[lane_id].enabled]
        if definitions_by_id[PREMIUM_CITY_LANE_ID].enabled:
            lane_ids.append(PREMIUM_CITY_LANE_ID)
    else:
        lane_ids = _requested_lane_ids(requested)

    definitions: list[_LaneDefinition] = []
    for lane_id in lane_ids:
        if lane_id not in definitions_by_id:
            raise ValueError(f"unknown paper shadow lane: {lane_id}")
        definitions.append(definitions_by_id[lane_id])
    return tuple(definitions)


def summarize_paper_shadow_lane_report(
    lane_rows: Iterable[Mapping[str, Any]] | None = None,
    *,
    lane_decision_path: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
    shared_candidate_ids: Iterable[str] | None = None,
    candidate_dataset_path: str | Path | None = None,
    run_id: str | None = None,
    agent_run_id: str | None = None,
    baseline_rows: Iterable[Mapping[str, Any]] | None = None,
    comparison_rows: Iterable[Mapping[str, Any]] | None = None,
    sample_limit: int = 10,
) -> dict[str, Any]:
    """Build a read-only smoke report for paper shadow lane rows."""

    all_rows = _report_lane_rows(lane_rows, lane_decision_path=lane_decision_path)
    enabled_lane_ids = _report_enabled_lane_ids(config, all_rows)
    requested_candidate_ids = {str(value) for value in (shared_candidate_ids or []) if str(value)}
    scoped_candidate_dataset_path = _report_candidate_dataset_path(candidate_dataset_path, baseline_rows, comparison_rows)
    scoped_run_id = _report_run_id(run_id, baseline_rows, comparison_rows)
    rows = _filter_report_rows(
        all_rows,
        requested_candidate_ids=requested_candidate_ids,
        candidate_dataset_path=scoped_candidate_dataset_path,
        run_id=scoped_run_id,
        agent_run_id=agent_run_id,
        enabled_lane_ids=set(enabled_lane_ids) if config is not None else None,
    )
    observed_candidate_ids = {
        candidate_id for candidate_id in (_row_shared_candidate_id(row) for row in rows) if candidate_id
    }
    baseline_by_candidate = _reference_rows_by_candidate(baseline_rows)
    comparison_by_candidate = _reference_rows_by_candidate(comparison_rows)

    lane_row_counts = _counts_sorted(_lane_id_for_row(row) for row in rows)
    buy_counts = _counts_sorted(_lane_id_for_row(row) for row in rows if _is_buy_action(row.get("action")))
    skip_counts = _counts_sorted(_lane_id_for_row(row) for row in rows if _is_skip_action(row.get("action")))
    action_counts = _counts_sorted(_action_label(row.get("action")) for row in rows)
    source_scoreboard = _summarize_source_scoreboard_rows(rows)
    source_reliability = _summarize_source_reliability_rows(rows)
    source_scoreboard_readiness = _summarize_source_scoreboard_readiness(rows)
    return {
        "schema_version": PAPER_LANE_SCHEMA_VERSION,
        "enabled_lane_ids": enabled_lane_ids,
        "candidate_count": len(observed_candidate_ids),
        "observed_candidate_count": len(observed_candidate_ids),
        "requested_candidate_count": len(requested_candidate_ids) if shared_candidate_ids is not None else None,
        "rows_loaded": len(all_rows),
        "rows_written": len(rows),
        "candidate_dataset_path": scoped_candidate_dataset_path,
        "run_id": scoped_run_id,
        "agent_run_id": agent_run_id,
        "lane_row_counts": lane_row_counts,
        "buy_counts": buy_counts,
        "skip_counts": skip_counts,
        "action_counts": action_counts,
        "source_scoreboard": source_scoreboard,
        "source_reliability": source_reliability,
        "source_scoreboard_readiness": source_scoreboard_readiness,
        "drift": {
            "vs_baseline": _summarize_reference_drift(
                rows,
                baseline_by_candidate,
                provenance_action_key="baseline_action",
                sample_limit=sample_limit,
            ),
            "vs_comparison": _summarize_reference_drift(
                rows,
                comparison_by_candidate,
                provenance_action_key="comparison_action",
                sample_limit=sample_limit,
            ),
        },
    }


def _configured_lane_definition_map(config: Mapping[str, Any]) -> dict[str, _LaneDefinition]:
    raw_by_id = _built_in_lane_definition_map()
    for lane_id, file_definition in _load_lane_definition_files(config).items():
        raw_by_id[lane_id] = _deep_merge(raw_by_id.get(lane_id, {"id": lane_id}), file_definition)

    for lane_id in KNOWN_LANE_IDS:
        inline = _mapping(config.get(lane_id))
        if inline:
            raw_by_id[lane_id] = _deep_merge(raw_by_id.get(lane_id, {"id": lane_id}), inline)

    lanes_cfg = config.get("lanes")
    if isinstance(lanes_cfg, Mapping):
        for lane_id, lane_cfg in lanes_cfg.items():
            raw_by_id[str(lane_id)] = _deep_merge(
                raw_by_id.get(str(lane_id), {"id": str(lane_id)}),
                _mapping(lane_cfg),
            )

    enabled_lanes_cfg = config.get("enabled_lanes")
    if isinstance(enabled_lanes_cfg, Mapping):
        for lane_id, lane_cfg in enabled_lanes_cfg.items():
            raw_by_id[str(lane_id)] = _deep_merge(
                raw_by_id.get(str(lane_id), {"id": str(lane_id)}),
                _mapping(lane_cfg),
            )

    if config.get("confidence_floor") not in (None, "") or config.get("min_confidence") not in (None, ""):
        confidence_cfg = raw_by_id.get("shadow_confidence_floor", {"id": "shadow_confidence_floor"})
        parameters = _mapping(confidence_cfg.get("parameters"))
        if config.get("confidence_floor") not in (None, ""):
            parameters["confidence_floor"] = config.get("confidence_floor")
        if config.get("min_confidence") not in (None, ""):
            parameters["min_confidence"] = config.get("min_confidence")
        confidence_cfg["parameters"] = parameters
        raw_by_id["shadow_confidence_floor"] = confidence_cfg

    for key in ("source_reliability_scoreboard", "source_reliability_scoreboard_path", "source_scoreboard_path"):
        if config.get(key) in (None, ""):
            continue
        for lane_id in SOURCE_SCOREBOARD_CONFIG_LANE_IDS:
            reliability_cfg = raw_by_id.get(lane_id, {"id": lane_id})
            parameters = _mapping(reliability_cfg.get("parameters"))
            parameters["scoreboard_path"] = config.get(key)
            reliability_cfg["parameters"] = parameters
            raw_by_id[lane_id] = reliability_cfg
        break

    return {
        lane_id: _lane_definition_from_raw(lane_id, raw)
        for lane_id, raw in raw_by_id.items()
        if lane_id in KNOWN_LANE_IDS
    }


def _built_in_lane_definition_map() -> dict[str, dict[str, Any]]:
    return {
        "control_stable": {
            "id": "control_stable",
            "type": "passthrough",
            "source_wallet": STABLE_PAPER_WALLET_ID,
            "source_role": "baseline",
            "input_source": "shared_candidate_dataset",
            "input_market_source": "shared_market",
            "enabled": True,
            "description": "Shared-candidate-fed control lane that mirrors the stable paper wallet decision as baseline provenance.",
        },
        "shadow_current_beta": {
            "id": "shadow_current_beta",
            "type": "passthrough",
            "source_wallet": BETA_PAPER_WALLET_ID,
            "source_role": "comparison",
            "input_source": "shared_candidate_dataset",
            "input_market_source": "shared_market",
            "enabled": True,
            "description": "Shared-candidate-fed shadow lane that mirrors the current beta paper wallet decision as comparison provenance.",
        },
        "shadow_confidence_floor": {
            "id": "shadow_confidence_floor",
            "type": "confidence_floor",
            "source_wallet": STABLE_PAPER_WALLET_ID,
            "source_role": "baseline",
            "input_source": "shared_candidate_dataset",
            "input_market_source": "shared_market",
            "enabled": True,
            "description": "Shared-candidate-fed lane that starts from the stable baseline decision and only overrides the shared signal confidence floor outcome.",
            "parameters": {"confidence_floor": DEFAULT_CONFIDENCE_FLOOR},
        },
        PREMIUM_CITY_LANE_ID: {
            "id": PREMIUM_CITY_LANE_ID,
            "type": "premium_city",
            "source_wallet": STABLE_PAPER_WALLET_ID,
            "source_role": "baseline",
            "input_source": "shared_candidate_dataset",
            "input_market_source": "shared_market",
            "enabled": False,
            "description": "Shared-candidate-fed lane that starts from the stable baseline decision and only allows configured premium cities.",
            "parameters": {"allowlist": []},
        },
        SOURCE_RELIABILITY_LANE_ID: {
            "id": SOURCE_RELIABILITY_LANE_ID,
            "type": "source_reliability",
            "source_wallet": STABLE_PAPER_WALLET_ID,
            "source_role": "baseline",
            "input_source": "shared_candidate_dataset",
            "input_market_source": "shared_market",
            "enabled": False,
            "description": "Shared-candidate-fed lane that starts from stable baseline and applies source reliability metadata only.",
            "parameters": {},
        },
        SOURCE_SCOREBOARD_LANE_ID: {
            "id": SOURCE_SCOREBOARD_LANE_ID,
            "type": "source_reliability",
            "source_wallet": STABLE_PAPER_WALLET_ID,
            "source_role": "baseline",
            "input_source": "shared_candidate_dataset",
            "input_market_source": "shared_market",
            "enabled": False,
            "description": "Shared-candidate-fed lane that starts from stable baseline and records source scoreboard recommendations plus future-PnL provenance only.",
            "parameters": {},
        },
        SOURCE_ROUTER_LANE_ID: {
            "id": SOURCE_ROUTER_LANE_ID,
            "type": "source_router",
            "source_wallet": STABLE_PAPER_WALLET_ID,
            "source_role": "baseline",
            "input_source": "shared_candidate_dataset",
            "input_market_source": "shared_market",
            "enabled": False,
            "description": "Shared-candidate-fed source-router lane that records independent source-implied BUY_YES/BUY_NO/SKIP decisions with future-PnL provenance only.",
            "parameters": {"hypothetical_notional_usd": 10.0},
        },
    }


def _load_lane_definition_files(config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    definitions_dir = config.get("definitions_dir") or config.get("definition_dir")
    if definitions_dir in (None, ""):
        return {}
    path = _resolve_definition_dir(definitions_dir)
    if not path.exists():
        return {}
    if yaml is None:
        raise RuntimeError("PyYAML is required to load paper shadow lane definitions")

    definitions: dict[str, dict[str, Any]] = {}
    for definition_path in sorted([*path.glob("*.yaml"), *path.glob("*.yml")]):
        with definition_path.open() as handle:
            loaded = yaml.safe_load(handle) or {}
        if not isinstance(loaded, Mapping):
            raise ValueError(f"paper shadow lane definition must be a mapping: {definition_path}")
        lane_id = str(loaded.get("id") or definition_path.stem)
        raw = dict(loaded)
        raw["id"] = lane_id
        raw["definition_path"] = str(definition_path)
        definitions[lane_id] = raw
    return definitions


def _resolve_definition_dir(path_value: Any) -> Path:
    path = Path(str(path_value))
    if path.is_absolute():
        return path
    cwd_path = Path.cwd() / path
    if cwd_path.exists():
        return cwd_path
    return REPO_ROOT / path


def _lane_definition_from_raw(lane_id: str, raw: Mapping[str, Any]) -> _LaneDefinition:
    if lane_id not in KNOWN_LANE_IDS:
        raise ValueError(f"unknown paper shadow lane: {lane_id}")
    parameters = _mapping(raw.get("parameters"))
    for key in (
        "confidence_floor",
        "min_confidence",
        "allowlist",
        "scoreboard_path",
        "source_reliability_scoreboard",
        "source_reliability_scoreboard_path",
        "source_scoreboard_path",
        "hypothetical_notional_usd",
    ):
        if key in raw:
            parameters[key] = raw.get(key)
    input_source = str(raw.get("input_source") or "shared_candidate_dataset")
    input_market_source = str(raw.get("input_market_source") or "shared_market")
    if input_source != "shared_candidate_dataset":
        raise ValueError(f"paper shadow lane {lane_id} must use input_source=shared_candidate_dataset")
    if input_market_source != "shared_market":
        raise ValueError(f"paper shadow lane {lane_id} must use input_market_source=shared_market")
    return _LaneDefinition(
        lane_id=lane_id,
        lane_type=str(raw.get("type") or raw.get("lane_type") or "passthrough"),
        source_wallet_id=_source_wallet_id(
            raw.get("source_wallet_id") or raw.get("source_wallet") or raw.get("source")
        ),
        source_role=str(raw.get("source_role") or raw.get("source_wallet_role") or "baseline"),
        input_source=input_source,
        input_market_source=input_market_source,
        description=str(raw.get("description") or "") or None,
        enabled=_truthy(raw.get("enabled", True)),
        parameters=parameters,
        definition_path=str(raw.get("definition_path")) if raw.get("definition_path") not in (None, "") else None,
    )


def requested_paper_shadow_lane_ids(value: Any) -> list[str]:
    """Return requested enabled lane ids using shared lane config semantics."""

    return _requested_lane_ids(value)


def _requested_lane_ids(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        return [str(lane_id) for lane_id, lane_cfg in value.items() if _lane_mapping_enabled(lane_cfg)]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(lane_id) for lane_id in (value or []) if str(lane_id).strip()]


def _lane_mapping_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    lane_cfg = _mapping(value)
    if "enabled" not in lane_cfg:
        return True
    enabled = lane_cfg.get("enabled")
    return enabled if isinstance(enabled, bool) else _truthy(enabled)


def _source_wallet_id(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.lower().replace("-", "_")
    if normalized in {"stable", "stable_paper"}:
        return STABLE_PAPER_WALLET_ID
    if normalized in {"beta", "current_beta", "beta_paper"}:
        return BETA_PAPER_WALLET_ID
    return text


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base or {})
    for key, value in dict(override or {}).items():
        if isinstance(result.get(key), Mapping) and isinstance(value, Mapping):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _lane_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = _mapping(_mapping(config).get("paper_shadow_lanes"))
    if not raw:
        raw = _mapping(_mapping(config).get("paper_decision_lanes"))
    return raw


def _lane_decision_path(config: Mapping[str, Any], *, ledger_root: str | Path) -> Path:
    configured = config.get("decision_ledger_path") or config.get("ledger_path")
    if configured not in (None, ""):
        return Path(configured)
    return Path(ledger_root) / "paper_shadow_lane_decisions.jsonl"


def _lane_run_id(wallet_runs: Mapping[str, Any]) -> str:
    stable = wallet_runs.get(STABLE_PAPER_WALLET_ID)
    if stable is not None:
        session_id = getattr(stable, "session_id", None)
        if session_id not in (None, ""):
            return f"{session_id}:paper_lanes"
    for run in wallet_runs.values():
        session_id = getattr(run, "session_id", None)
        if session_id not in (None, ""):
            return f"{session_id}:paper_lanes"
    return "paper_lanes"


def _index_decisions_by_candidate(
    rows: list[dict[str, Any]],
    *,
    wallet_id: str,
    run_id: str,
    candidate_dataset_path: str,
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("wallet_id") or "") != str(wallet_id):
            continue
        if str(row.get("run_id") or "") != str(run_id):
            continue
        if str(row.get("candidate_dataset_path") or "") != str(candidate_dataset_path):
            continue
        if str(row.get("decision_role") or "") != "paper_shadow":
            continue
        candidate_id = row.get("shared_candidate_id")
        if candidate_id in (None, ""):
            continue
        indexed[str(candidate_id)] = row
    return indexed


def _candidate_input(wallet_inputs: Mapping[str, Any]) -> Any:
    if STABLE_PAPER_WALLET_ID in wallet_inputs:
        return wallet_inputs[STABLE_PAPER_WALLET_ID]
    for value in wallet_inputs.values():
        return value
    return None


def _candidate_signal(candidate_input: Any) -> dict[str, Any]:
    signal = getattr(candidate_input, "signal", None)
    return dict(signal) if isinstance(signal, dict) else {}


def _report_lane_rows(
    lane_rows: Iterable[Mapping[str, Any]] | None,
    *,
    lane_decision_path: str | Path | None,
) -> list[dict[str, Any]]:
    if lane_rows is None and lane_decision_path not in (None, ""):
        lane_rows = load_jsonl(Path(lane_decision_path))
    return [dict(row) for row in (lane_rows or []) if isinstance(row, Mapping)]


def _filter_report_rows(
    rows: list[dict[str, Any]],
    *,
    requested_candidate_ids: set[str],
    candidate_dataset_path: str | None,
    run_id: str | None,
    agent_run_id: str | None,
    enabled_lane_ids: set[str] | None,
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for row in rows:
        if requested_candidate_ids and _row_shared_candidate_id(row) not in requested_candidate_ids:
            continue
        if candidate_dataset_path is not None and str(row.get("candidate_dataset_path") or "") != candidate_dataset_path:
            continue
        if run_id is not None and str(row.get("run_id") or "") != run_id:
            continue
        if agent_run_id is not None and str(row.get("agent_run_id") or "") != agent_run_id:
            continue
        if enabled_lane_ids is not None and _lane_id_for_row(row) not in enabled_lane_ids:
            continue
        filtered.append(row)
    return filtered


def _report_candidate_dataset_path(
    candidate_dataset_path: str | Path | None,
    baseline_rows: Iterable[Mapping[str, Any]] | None,
    comparison_rows: Iterable[Mapping[str, Any]] | None,
) -> str | None:
    if candidate_dataset_path not in (None, ""):
        return str(candidate_dataset_path)
    return _first_row_value("candidate_dataset_path", baseline_rows, comparison_rows)


def _report_run_id(
    run_id: str | None,
    baseline_rows: Iterable[Mapping[str, Any]] | None,
    comparison_rows: Iterable[Mapping[str, Any]] | None,
) -> str | None:
    if run_id not in (None, ""):
        return str(run_id)
    source_run_id = _first_row_value("run_id", baseline_rows, comparison_rows)
    if source_run_id in (None, ""):
        return None
    text = str(source_run_id)
    return text if text.endswith(":paper_lanes") else f"{text}:paper_lanes"


def _first_row_value(key: str, *row_groups: Iterable[Mapping[str, Any]] | None) -> str | None:
    for rows in row_groups:
        for row in rows or []:
            if not isinstance(row, Mapping):
                continue
            value = row.get(key)
            if value not in (None, ""):
                return str(value)
    return None


def _report_enabled_lane_ids(config: Mapping[str, Any] | None, rows: list[dict[str, Any]]) -> tuple[str, ...]:
    if config is not None:
        if not paper_shadow_lanes_enabled(config):
            return ()
        return tuple(lane.lane_id for lane in _lane_definitions(_lane_config(config)))
    lane_ids: list[str] = []
    for row in rows:
        lane_id = _lane_id_for_row(row)
        if lane_id and lane_id not in lane_ids:
            lane_ids.append(lane_id)
    return tuple(lane_ids)


def _reference_rows_by_candidate(rows: Iterable[Mapping[str, Any]] | None) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        candidate_id = _row_shared_candidate_id(row)
        if candidate_id:
            indexed[candidate_id] = dict(row)
    return indexed


def _summarize_reference_drift(
    rows: list[dict[str, Any]],
    reference_by_candidate: Mapping[str, Mapping[str, Any]],
    *,
    provenance_action_key: str,
    sample_limit: int,
) -> dict[str, Any]:
    by_lane: dict[str, int] = {}
    by_candidate: dict[str, int] = {}
    samples: list[dict[str, Any]] = []
    reference_candidate_ids: set[str] = set()
    for row in rows:
        candidate_id = _row_shared_candidate_id(row)
        if not candidate_id:
            continue
        lane_id = _lane_id_for_row(row)
        reference_action = _reference_action(row, reference_by_candidate.get(candidate_id), provenance_action_key)
        if reference_action is None:
            continue
        reference_candidate_ids.add(candidate_id)
        lane_action = _action_label(row.get("action"))
        if lane_action == reference_action:
            continue
        by_lane[lane_id] = by_lane.get(lane_id, 0) + 1
        by_candidate[candidate_id] = by_candidate.get(candidate_id, 0) + 1
        if len(samples) < sample_limit:
            samples.append(
                {
                    "shared_candidate_id": candidate_id,
                    "lane_id": lane_id,
                    "lane_action": lane_action,
                    "reference_action": reference_action,
                }
            )
    return {
        "reference_candidate_count": len(reference_candidate_ids),
        "candidate_count_with_action_drift": len(by_candidate),
        "row_count_with_action_drift": sum(by_lane.values()),
        "by_lane": _sorted_count_dict(by_lane),
        "by_shared_candidate_id": _sorted_count_dict(by_candidate),
        "samples": samples,
    }


def _reference_action(
    lane_row: Mapping[str, Any],
    reference_row: Mapping[str, Any] | None,
    provenance_action_key: str,
) -> str | None:
    if reference_row is not None:
        return _optional_action_label(reference_row.get("action"))
    provenance = _mapping(lane_row.get("provenance"))
    return _optional_action_label(provenance.get(provenance_action_key))


def _lane_id_for_row(row: Mapping[str, Any]) -> str:
    return _text(
        row.get("policy"),
        row.get("selected_lane"),
        row.get("lane_id"),
        _mapping(row.get("provenance")).get("lane_id"),
        "unknown",
    )


def _row_shared_candidate_id(row: Mapping[str, Any]) -> str | None:
    value = row.get("shared_candidate_id")
    if value not in (None, ""):
        return str(value)
    shared_candidate = _mapping(row.get("shared_candidate"))
    value = shared_candidate.get("candidate_id") or shared_candidate.get("shared_candidate_id")
    if value not in (None, ""):
        return str(value)
    return None


def _is_buy_action(value: Any) -> bool:
    return _action_label(value) in {"BUY_YES", "BUY_NO"}


def _is_skip_action(value: Any) -> bool:
    return _action_label(value) == "SKIP"


def _action_label(value: Any) -> str:
    return str(value or "unknown").upper()


def _optional_action_label(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return _action_label(value)


def _counts_sorted(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[str(value)] = counts.get(str(value), 0) + 1
    return _sorted_count_dict(counts)


def _sorted_count_dict(counts: Mapping[str, int]) -> dict[str, int]:
    return {key: int(counts[key]) for key in sorted(counts)}



def summarize_paper_shadow_lane_resolved_pnl(
    *,
    lane_rows: Iterable[Mapping[str, Any]] | None = None,
    lane_decision_path: str | Path | None = None,
    resolution_rows: Iterable[Mapping[str, Any]] | None = None,
    resolution_path: str | Path | None = None,
) -> dict[str, Any]:
    """Join paper shadow lane rows to finalized outcomes and compute read-only hypothetical PnL.

    This never mutates paper wallet state. It is an audit/reporting join over lane
    decisions plus resolution rows so recommendation-only lanes can be scored once
    outcomes are known.
    """

    if lane_rows is None and lane_decision_path not in (None, ""):
        resolutions = (
            [dict(row) for row in resolution_rows]
            if resolution_rows is not None
            else (load_jsonl(Path(resolution_path)) if resolution_path not in (None, "") else [])
        )
        resolution_index = _resolution_index(resolutions, resolution_path=resolution_path)
        return _summarize_paper_shadow_lane_resolution_stream(
            _iter_jsonl_mappings(Path(lane_decision_path or "")),
            resolution_index=resolution_index,
        )

    joined_rows = build_paper_shadow_lane_resolution_rows(
        lane_rows=lane_rows,
        lane_decision_path=lane_decision_path,
        resolution_rows=resolution_rows,
        resolution_path=resolution_path,
    )
    return summarize_paper_shadow_lane_resolution_rows(joined_rows)


def update_paper_shadow_lane_incremental_pnl(
    *,
    lane_decision_path: str | Path,
    resolution_path: str | Path,
    state_path: str | Path,
    event_output_path: str | Path | None = None,
    starting_balance_usd: float = 100.0,
    sizing_mode: str = "recorded_notional",
    balance_fraction: float = 0.1,
    max_new_rows: int = 10000,
    max_pending_rows: int = 50000,
    reset: bool = False,
) -> dict[str, Any]:
    """Advance a derived, non-mutating PnL replay state for paper shadow lanes.

    The state is a synthetic balance ledger for reporting. It never writes paper
    wallet/accounting files and can be rerun with different `state_path` values to
    compare starting balances or sizing assumptions.
    """

    lane_path = Path(lane_decision_path)
    resolution_rows = load_jsonl(Path(resolution_path))
    resolution_index = _resolution_index(resolution_rows, resolution_path=resolution_path)
    state_file = Path(state_path)
    state = {} if reset else _load_incremental_pnl_state(state_file)
    file_size = lane_path.stat().st_size if lane_path.exists() else 0
    cursor = int(state.get("cursor_offset") or 0)
    if cursor > file_size:
        cursor = 0

    replay_config = {
        "starting_balance_usd": round(float(starting_balance_usd), 4),
        "sizing_mode": _incremental_sizing_mode(sizing_mode),
        "balance_fraction": round(float(balance_fraction), 6),
    }
    lanes = _incremental_lanes(state.get("lanes"), starting_balance_usd=float(starting_balance_usd))
    pending_rows = list(state.get("pending_rows") or [])
    if not isinstance(pending_rows, list):
        pending_rows = []

    events: list[dict[str, Any]] = []
    next_pending: list[dict[str, Any]] = []
    applied = 0
    resolved_pending = 0
    still_pending = 0
    blocker_rows = 0

    for pending in pending_rows:
        row = _mapping(pending.get("row"))
        joined = _build_lane_resolution_row(row, resolution_index)
        if joined.get("blocker") == "missing_resolution":
            if len(next_pending) < max_pending_rows:
                next_pending.append({"key": pending.get("key") or _incremental_row_key(row), "row": dict(row)})
            still_pending += 1
            continue
        event = _apply_incremental_pnl_row(joined, lanes=lanes, replay_config=replay_config)
        events.append(event)
        applied += 1
        resolved_pending += 1
        if event.get("blocker"):
            blocker_rows += 1

    rows_read = 0
    if lane_path.exists() and max_new_rows > 0:
        with lane_path.open("r", encoding="utf-8") as handle:
            handle.seek(cursor)
            while rows_read < max_new_rows:
                line = handle.readline()
                if not line:
                    break
                rows_read += 1
                try:
                    raw_row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(raw_row, Mapping):
                    continue
                row = _compact_incremental_lane_row(raw_row)
                joined = _build_lane_resolution_row(row, resolution_index)
                if joined.get("blocker") == "missing_resolution":
                    if len(next_pending) < max_pending_rows:
                        next_pending.append({"key": _incremental_row_key(row), "row": row})
                    still_pending += 1
                    continue
                event = _apply_incremental_pnl_row(joined, lanes=lanes, replay_config=replay_config)
                events.append(event)
                applied += 1
                if event.get("blocker"):
                    blocker_rows += 1
            cursor = handle.tell()

    event_path = Path(event_output_path) if event_output_path not in (None, "") else None
    if event_path:
        for event in events:
            append_jsonl(event_path, event)

    updated_state = {
        "schema_name": "paper_shadow_lane_incremental_pnl_state",
        "schema_version": PAPER_LANE_INCREMENTAL_PNL_SCHEMA_VERSION,
        "non_mutating": True,
        "lane_decision_path": str(lane_path),
        "resolution_path": str(resolution_path),
        "cursor_offset": cursor,
        "cursor_file_size": file_size,
        "replay_config": replay_config,
        "lanes": _finalize_incremental_lanes(lanes),
        "pending_rows": next_pending,
        "pending_count": len(next_pending),
        "last_run": {
            "new_rows_read": rows_read,
            "events_written": len(events),
            "applied_rows": applied,
            "resolved_pending_rows": resolved_pending,
            "still_pending_rows": still_pending,
            "blocker_rows": blocker_rows,
        },
        "summary": _incremental_summary(lanes),
    }
    atomic_write_json(state_file, updated_state, lock_path=state_file.with_suffix(state_file.suffix + ".lock"))
    return updated_state


def summarize_paper_shadow_lane_resolution_rows(
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Summarize prebuilt paper shadow lane resolution rows."""

    totals = _empty_pnl_bucket()
    by_lane: dict[str, dict[str, Any]] = {}
    blocker_counts: dict[str, int] = {}
    joined_rows = [dict(row) for row in rows]

    for joined in joined_rows:
        _accumulate_paper_shadow_lane_resolution_row(
            joined,
            totals=totals,
            by_lane=by_lane,
            blocker_counts=blocker_counts,
        )

    source_router = _summarize_source_router_resolution_rows(joined_rows)
    return _finalize_pnl_bucket(
        {**totals, "by_lane": by_lane, "blocker_counts": blocker_counts, "source_router": source_router}
    )


def _summarize_paper_shadow_lane_resolution_stream(
    lane_rows: Iterable[Mapping[str, Any]],
    *,
    resolution_index: Mapping[str, Any],
) -> dict[str, Any]:
    totals = _empty_pnl_bucket()
    by_lane: dict[str, dict[str, Any]] = {}
    blocker_counts: dict[str, int] = {}

    for row in lane_rows:
        joined = _build_lane_resolution_row(row, resolution_index)
        _accumulate_paper_shadow_lane_resolution_row(
            joined,
            totals=totals,
            by_lane=by_lane,
            blocker_counts=blocker_counts,
        )

    return _finalize_pnl_bucket(
        {
            **totals,
            "by_lane": by_lane,
            "blocker_counts": blocker_counts,
            "source_router": _finalize_pnl_bucket(_empty_pnl_bucket()),
        }
    )


def _accumulate_paper_shadow_lane_resolution_row(
    joined: Mapping[str, Any],
    *,
    totals: dict[str, Any],
    by_lane: dict[str, dict[str, Any]],
    blocker_counts: dict[str, int],
) -> None:
    lane_id = str(joined.get("lane_id") or "unknown")
    bucket = by_lane.setdefault(lane_id, _empty_pnl_bucket())
    _increment_pnl_bucket(totals, "evaluated_rows")
    _increment_pnl_bucket(bucket, "evaluated_rows")

    blocker = _optional_text(joined.get("blocker"))
    resolution = _mapping(joined.get("resolution"))
    if resolution.get("outcome") is not None:
        _increment_pnl_bucket(totals, "resolved_rows")
        _increment_pnl_bucket(bucket, "resolved_rows")
    if blocker:
        _add_blocker(blocker_counts, blocker)
        _add_blocker(bucket["blocker_counts"], blocker)
        return

    action = _action_label(joined.get("action"))
    if _is_skip_action(action):
        _increment_pnl_bucket(totals, "skip_rows")
        _increment_pnl_bucket(bucket, "skip_rows")
        _increment_pnl_bucket(totals, "pnl_calculable_rows")
        _increment_pnl_bucket(bucket, "pnl_calculable_rows")
        return

    pnl = _mapping(joined.get("pnl"))
    stake_f = float(_number(pnl.get("stake_usd")) or 0.0)
    payout = float(_number(pnl.get("payout_usd")) or 0.0)
    pnl_value = float(_number(pnl.get("pnl_usd")) or 0.0)
    won = bool(pnl.get("won"))

    _increment_pnl_bucket(totals, "buy_rows")
    _increment_pnl_bucket(bucket, "buy_rows")
    _increment_pnl_bucket(totals, "pnl_calculable_rows")
    _increment_pnl_bucket(bucket, "pnl_calculable_rows")
    _increment_pnl_bucket(totals, "winning_buy_rows" if won else "losing_buy_rows")
    _increment_pnl_bucket(bucket, "winning_buy_rows" if won else "losing_buy_rows")
    for target in (totals, bucket):
        target["total_stake_usd"] += stake_f
        target["total_payout_usd"] += payout
        target["total_pnl_usd"] += pnl_value


def _load_incremental_pnl_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _incremental_sizing_mode(value: str) -> str:
    mode = str(value or "recorded_notional").strip()
    if mode not in {"recorded_notional", "balance_scaled", "balance_fraction"}:
        raise ValueError(f"unsupported incremental PnL sizing mode: {mode}")
    return mode


def _incremental_lanes(raw_lanes: Any, *, starting_balance_usd: float) -> dict[str, dict[str, Any]]:
    lanes: dict[str, dict[str, Any]] = {}
    if isinstance(raw_lanes, Mapping):
        for lane_id, raw in raw_lanes.items():
            if not isinstance(raw, Mapping):
                continue
            lanes[str(lane_id)] = {
                "starting_balance_usd": _money(raw.get("starting_balance_usd"), starting_balance_usd),
                "balance_usd": _money(raw.get("balance_usd"), starting_balance_usd),
                "total_pnl_usd": _money(raw.get("total_pnl_usd"), 0.0),
                "total_stake_usd": _money(raw.get("total_stake_usd"), 0.0),
                "total_payout_usd": _money(raw.get("total_payout_usd"), 0.0),
                "applied_rows": int(raw.get("applied_rows") or 0),
                "buy_rows": int(raw.get("buy_rows") or 0),
                "skip_rows": int(raw.get("skip_rows") or 0),
                "winning_buy_rows": int(raw.get("winning_buy_rows") or 0),
                "losing_buy_rows": int(raw.get("losing_buy_rows") or 0),
                "blocker_counts": dict(raw.get("blocker_counts") or {}),
            }
    return lanes


def _compact_incremental_lane_row(row: Mapping[str, Any]) -> dict[str, Any]:
    provenance = _mapping(row.get("provenance"))
    future_inputs = _row_future_pnl_inputs(row)
    compact: dict[str, Any] = {
        key: row.get(key)
        for key in (
            "decision_id",
            "agent_run_id",
            "run_id",
            "policy",
            "selected_lane",
            "shared_candidate_id",
            "market_id",
            "observed_at",
            "action",
            "side",
            "entry_price",
            "price",
            "requested_position_size_usd",
            "approved_position_size_usd",
        )
        if row.get(key) not in (None, "")
    }
    compact["provenance"] = {"future_pnl_inputs": dict(future_inputs)}
    if provenance.get("source_scoreboard"):
        compact["provenance"]["source_scoreboard"] = {
            "future_pnl_inputs": dict(future_inputs),
            "recommended_action": future_inputs.get("recommended_action"),
        }
    if provenance.get("source_router"):
        compact["provenance"]["source_router"] = {
            "future_pnl_inputs": dict(future_inputs),
            "recommended_action": future_inputs.get("recommended_action"),
        }
    return compact


def _incremental_row_key(row: Mapping[str, Any]) -> str:
    for key in ("decision_id", "shared_candidate_id"):
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    parts = [
        _optional_text(row.get("run_id")) or "",
        _lane_id_for_row(row) or "",
        _optional_text(row.get("market_id"), _row_future_pnl_inputs(row).get("market_id")) or "",
        _optional_text(row.get("observed_at"), _row_future_pnl_inputs(row).get("observed_at")) or "",
    ]
    return "|".join(parts)


def _apply_incremental_pnl_row(
    joined: Mapping[str, Any],
    *,
    lanes: dict[str, dict[str, Any]],
    replay_config: Mapping[str, Any],
) -> dict[str, Any]:
    lane_id = str(joined.get("lane_id") or "unknown")
    lane = lanes.setdefault(
        lane_id,
        {
            "starting_balance_usd": float(replay_config.get("starting_balance_usd") or 100.0),
            "balance_usd": float(replay_config.get("starting_balance_usd") or 100.0),
            "total_pnl_usd": 0.0,
            "total_stake_usd": 0.0,
            "total_payout_usd": 0.0,
            "applied_rows": 0,
            "buy_rows": 0,
            "skip_rows": 0,
            "winning_buy_rows": 0,
            "losing_buy_rows": 0,
            "blocker_counts": {},
        },
    )
    balance_before = float(lane.get("balance_usd") or 0.0)
    blocker = _optional_text(joined.get("blocker"))
    action = _action_label(joined.get("action"))
    pnl = None if blocker else _incremental_row_pnl(joined, balance_before=balance_before, replay_config=replay_config)
    pnl_usd = float(_mapping(pnl).get("pnl_usd") or 0.0)
    stake_usd = float(_mapping(pnl).get("stake_usd") or 0.0)
    payout_usd = float(_mapping(pnl).get("payout_usd") or 0.0)
    balance_after = balance_before + pnl_usd

    lane["applied_rows"] = int(lane.get("applied_rows") or 0) + 1
    if blocker:
        _add_blocker(lane.setdefault("blocker_counts", {}), blocker)
    elif _is_skip_action(action):
        lane["skip_rows"] = int(lane.get("skip_rows") or 0) + 1
    elif _is_buy_action(action):
        lane["buy_rows"] = int(lane.get("buy_rows") or 0) + 1
        lane["winning_buy_rows" if bool(_mapping(pnl).get("won")) else "losing_buy_rows"] = (
            int(lane.get("winning_buy_rows" if bool(_mapping(pnl).get("won")) else "losing_buy_rows") or 0) + 1
        )
    lane["balance_usd"] = round(balance_after, 4)
    lane["total_pnl_usd"] = round(float(lane.get("total_pnl_usd") or 0.0) + pnl_usd, 4)
    lane["total_stake_usd"] = round(float(lane.get("total_stake_usd") or 0.0) + stake_usd, 4)
    lane["total_payout_usd"] = round(float(lane.get("total_payout_usd") or 0.0) + payout_usd, 4)

    return {
        "schema_name": "paper_shadow_lane_incremental_pnl_event",
        "schema_version": PAPER_LANE_INCREMENTAL_PNL_SCHEMA_VERSION,
        "non_mutating": True,
        "lane_decision_id": joined.get("lane_decision_id"),
        "run_id": joined.get("run_id"),
        "lane_id": lane_id,
        "shared_candidate_id": joined.get("shared_candidate_id"),
        "market_id": joined.get("market_id"),
        "observed_at": joined.get("observed_at"),
        "action": action,
        "side": joined.get("side"),
        "resolution": joined.get("resolution"),
        "blocker": blocker,
        "sizing_mode": replay_config.get("sizing_mode"),
        "balance_before_usd": round(balance_before, 4),
        "balance_after_usd": round(balance_after, 4),
        "pnl": pnl,
    }


def _incremental_row_pnl(
    joined: Mapping[str, Any],
    *,
    balance_before: float,
    replay_config: Mapping[str, Any],
) -> dict[str, Any]:
    action = _action_label(joined.get("action"))
    if _is_skip_action(action):
        return {"calculable": True, "stake_usd": 0.0, "contracts": 0.0, "payout_usd": 0.0, "pnl_usd": 0.0, "won": None}
    fill_price = _number(joined.get("fill_price"))
    side = _optional_text(joined.get("side"))
    outcome = _optional_text(_mapping(joined.get("resolution")).get("outcome"))
    recorded_stake = _number(joined.get("notional_usd"))
    stake = _incremental_stake(
        recorded_stake=recorded_stake,
        balance_before=balance_before,
        joined=joined,
        replay_config=replay_config,
    )
    if fill_price is None or fill_price <= 0 or stake <= 0 or side not in {"YES", "NO"} or outcome not in {"YES", "NO"}:
        return {"calculable": False, "stake_usd": round(stake, 4), "contracts": 0.0, "payout_usd": 0.0, "pnl_usd": 0.0, "won": None}
    contracts = stake / fill_price
    won = side == outcome
    payout = contracts if won else 0.0
    return {
        "calculable": True,
        "stake_usd": round(stake, 4),
        "contracts": round(contracts, 4),
        "payout_usd": round(payout, 4),
        "pnl_usd": round(payout - stake, 4),
        "won": won,
    }


def _incremental_stake(
    *,
    recorded_stake: float | None,
    balance_before: float,
    joined: Mapping[str, Any],
    replay_config: Mapping[str, Any],
) -> float:
    mode = str(replay_config.get("sizing_mode") or "recorded_notional")
    if mode == "balance_fraction":
        fraction = max(0.0, float(replay_config.get("balance_fraction") or 0.0))
        return round(max(0.0, balance_before) * fraction, 4)
    if mode == "balance_scaled":
        base_balance = _number(_mapping(joined.get("replay_sizing")).get("starting_balance_usd"), replay_config.get("starting_balance_usd")) or 0.0
        if not recorded_stake or base_balance <= 0:
            return 0.0
        return round(max(0.0, float(recorded_stake) * (balance_before / base_balance)), 4)
    return round(max(0.0, float(recorded_stake or 0.0)), 4)


def _finalize_incremental_lanes(lanes: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(lane_id): _finalize_incremental_lane(lane) for lane_id, lane in sorted(lanes.items())}


def _finalize_incremental_lane(lane: Mapping[str, Any]) -> dict[str, Any]:
    stake = float(lane.get("total_stake_usd") or 0.0)
    starting = float(lane.get("starting_balance_usd") or 0.0)
    pnl = float(lane.get("total_pnl_usd") or 0.0)
    return {
        **dict(lane),
        "starting_balance_usd": round(starting, 4),
        "balance_usd": round(float(lane.get("balance_usd") or 0.0), 4),
        "total_pnl_usd": round(pnl, 4),
        "total_stake_usd": round(stake, 4),
        "total_payout_usd": round(float(lane.get("total_payout_usd") or 0.0), 4),
        "roi_pct": round((pnl / stake) * 100.0, 2) if stake else None,
        "balance_return_pct": round((pnl / starting) * 100.0, 2) if starting else None,
        "blocker_counts": {str(k): int(v) for k, v in sorted(_mapping(lane.get("blocker_counts")).items()) if int(v)},
    }


def _incremental_summary(lanes: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    finalized = _finalize_incremental_lanes(lanes)
    return {
        "lane_count": len(finalized),
        "total_applied_rows": sum(int(lane.get("applied_rows") or 0) for lane in finalized.values()),
        "total_buy_rows": sum(int(lane.get("buy_rows") or 0) for lane in finalized.values()),
        "total_pnl_usd": round(sum(float(lane.get("total_pnl_usd") or 0.0) for lane in finalized.values()), 4),
        "lanes": finalized,
    }


def _money(value: Any, default: float) -> float:
    number = _number(value)
    return round(float(default if number is None else number), 4)


def build_paper_shadow_lane_resolution_rows(
    *,
    lane_rows: Iterable[Mapping[str, Any]] | None = None,
    lane_decision_path: str | Path | None = None,
    resolution_rows: Iterable[Mapping[str, Any]] | None = None,
    resolution_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return replayable, read-only resolution rows for paper shadow lane decisions.

    The artifact is intentionally separate from wallet accounting. It preserves the
    lane's recorded action/side/price/notional inputs plus matched market outcome so
    later analyses can replay PnL under different balance or sizing assumptions.
    """

    rows = [dict(row) for row in lane_rows] if lane_rows is not None else load_jsonl(Path(lane_decision_path or ""))
    resolutions = (
        [dict(row) for row in resolution_rows]
        if resolution_rows is not None
        else (load_jsonl(Path(resolution_path)) if resolution_path not in (None, "") else [])
    )
    resolution_index = _resolution_index(resolutions, resolution_path=resolution_path)
    return [_build_lane_resolution_row(row, resolution_index) for row in rows]


def _build_lane_resolution_row(
    row: Mapping[str, Any],
    resolution_index: Mapping[str, Any],
) -> dict[str, Any]:
    future_inputs = _row_future_pnl_inputs(row)
    shared_candidate_id = _optional_text(row.get("shared_candidate_id"), future_inputs.get("shared_candidate_id"))
    market_id = _optional_text(row.get("market_id"), future_inputs.get("market_id"))
    resolution_match = _find_resolution(
        resolution_index,
        shared_candidate_id=shared_candidate_id,
        run_id=_optional_text(row.get("run_id"), future_inputs.get("run_id")),
        market_id=market_id,
    )
    resolution = resolution_match.get("row") if isinstance(resolution_match.get("row"), Mapping) else None
    resolution_blocker = _optional_text(resolution_match.get("blocker"))
    outcome = _normalized_resolution_outcome(resolution)
    action = _action_label(_optional_text(future_inputs.get("recommended_action"), row.get("action"), row.get("side")))
    side = _side_from_action(action) or _optional_text(future_inputs.get("side"))
    fill_price = _number(future_inputs.get("estimated_fill_price"), future_inputs.get("entry_price"), row.get("entry_price"), row.get("price"))
    entry_price = _number(future_inputs.get("entry_price"), row.get("entry_price"), row.get("price"))
    stake = _number(
        row.get("approved_position_size_usd"),
        row.get("requested_position_size_usd"),
        future_inputs.get("approved_position_size_usd"),
        future_inputs.get("requested_position_size_usd"),
        future_inputs.get("stable_approved_position_size_usd"),
        future_inputs.get("stable_requested_position_size_usd"),
    )
    blocker = resolution_blocker or _resolution_row_blocker(action=action, outcome=outcome, side=side, fill_price=fill_price, stake=stake)
    pnl = _resolution_row_pnl(action=action, outcome=outcome, side=side, fill_price=fill_price, stake=stake) if blocker is None else None
    return {
        "schema_name": "paper_shadow_lane_resolution",
        "schema_version": PAPER_LANE_RESOLUTION_SCHEMA_VERSION,
        "non_mutating": True,
        "lane_decision_id": _optional_text(row.get("decision_id")),
        "agent_run_id": _optional_text(row.get("agent_run_id")),
        "run_id": _optional_text(row.get("run_id")),
        "lane_id": _lane_id_for_row(row),
        "shared_candidate_id": shared_candidate_id,
        "market_id": market_id,
        "observed_at": _optional_text(row.get("observed_at"), future_inputs.get("observed_at")),
        "action": action,
        "side": side,
        "entry_price": entry_price,
        "fill_price": fill_price,
        "notional_usd": stake,
        "requested_position_size_usd": _number(row.get("requested_position_size_usd"), future_inputs.get("requested_position_size_usd"), future_inputs.get("stable_requested_position_size_usd")),
        "approved_position_size_usd": _number(row.get("approved_position_size_usd"), future_inputs.get("approved_position_size_usd"), future_inputs.get("stable_approved_position_size_usd")),
        "resolution": {
            "matched": resolution is not None or outcome is not None,
            "match_source": _optional_text(resolution_match.get("matched_by")) if resolution is not None else ("future_pnl_inputs" if outcome is not None else None),
            "matched_by": _optional_text(resolution_match.get("matched_by")),
            "match_key": _optional_text(resolution_match.get("match_key")),
            "outcome": outcome,
            "resolved_at": _optional_text(_mapping(resolution).get("resolved_at"), _mapping(_mapping(resolution).get("resolution")).get("resolved_at"), future_inputs.get("resolved_at")),
            "market_id": _optional_text(_mapping(resolution).get("market_id"), market_id),
            "shared_candidate_id": _optional_text(_mapping(resolution).get("shared_candidate_id"), shared_candidate_id),
            "resolution_source_path": _optional_text(resolution_match.get("resolution_source_path")),
            "resolution_row_id": _optional_text(_mapping(resolution).get("prediction_id"), _mapping(resolution).get("resolution_id"), _mapping(resolution).get("decision_id")),
            "candidate_match_count": int(resolution_match.get("candidate_match_count") or 0) if resolution_match.get("candidate_match_count") is not None else None,
        },
        "pnl": pnl,
        "blocker": blocker,
        "replay_sizing": {
            "mode": "recorded_fixed_notional",
            "recorded_notional_usd": stake,
            "sizing_source": _notional_source(row, future_inputs),
            "starting_balance_usd": _number(future_inputs.get("starting_balance_usd")),
            "replayable_with_alternate_balance": True,
        },
        "cost_model": {"fees_supported": False, "fees_usd": 0.0, "slippage_supported": False},
        "source_inputs": {"future_pnl_inputs": future_inputs},
    }


def _resolution_row_blocker(*, action: str, outcome: str | None, side: str | None, fill_price: float | None, stake: float | None) -> str | None:
    if not outcome:
        return "missing_resolution"
    if outcome == "VOID":
        return "void_resolution"
    if _is_skip_action(action):
        return None
    if not _is_buy_action(action):
        return "unsupported_action"
    if side not in {"YES", "NO"}:
        return "missing_side"
    if fill_price is None or float(fill_price) <= 0:
        return "missing_fill_price"
    if stake is None or float(stake) <= 0:
        return "missing_position_size"
    return None


def _resolution_row_pnl(*, action: str, outcome: str | None, side: str | None, fill_price: float | None, stake: float | None) -> dict[str, Any]:
    if _is_skip_action(action):
        return {"calculable": True, "stake_usd": 0.0, "contracts": 0.0, "payout_usd": 0.0, "pnl_usd": 0.0, "won": None}
    stake_f = float(stake or 0.0)
    fill_f = float(fill_price or 0.0)
    contracts = stake_f / fill_f
    won = side == outcome
    payout = contracts if won else 0.0
    return {
        "calculable": True,
        "stake_usd": round(stake_f, 4),
        "contracts": round(contracts, 4),
        "payout_usd": round(payout, 4),
        "pnl_usd": round(payout - stake_f, 4),
        "won": won,
    }


def _notional_source(row: Mapping[str, Any], future_inputs: Mapping[str, Any]) -> str | None:
    for key, source in (
        ("approved_position_size_usd", "lane_approved_position_size_usd"),
        ("requested_position_size_usd", "lane_requested_position_size_usd"),
    ):
        if _number(row.get(key)) is not None:
            return source
    for key, source in (
        ("approved_position_size_usd", "future_approved_position_size_usd"),
        ("requested_position_size_usd", "future_requested_position_size_usd"),
        ("stable_approved_position_size_usd", "stable_approved_position_size_usd"),
        ("stable_requested_position_size_usd", "stable_requested_position_size_usd"),
    ):
        if _number(future_inputs.get(key)) is not None:
            return source
    return None

_FUTURE_PNL_NON_DECISION_TIME_KEYS = frozenset(
    {
        "actual_temp_used",
        "actual_outcome",
        "actual_source",
        "known_after",
        "label_target",
        "resolved_at",
        "resolved_outcome",
        "settled_side",
        "settlement_source",
    }
)


def _row_future_pnl_inputs(row: Mapping[str, Any]) -> dict[str, Any]:
    provenance = _mapping(row.get("provenance"))
    direct = _mapping(provenance.get("future_pnl_inputs"))
    if direct:
        return _decision_time_pnl_inputs(direct)
    scoreboard = _mapping(provenance.get("source_scoreboard"))
    return _decision_time_pnl_inputs(_mapping(scoreboard.get("future_pnl_inputs")))


def _decision_time_pnl_inputs(inputs: Mapping[str, Any]) -> dict[str, Any]:
    """Drop settlement/outcome metadata when reading legacy lane records.

    New lane decisions never emit these fields.  The defensive read-side filter
    keeps derived resolution/replay artifacts from re-propagating them when a
    historical raw ledger is inspected.
    """

    return {
        str(key): value
        for key, value in inputs.items()
        if str(key).strip().lower() not in _FUTURE_PNL_NON_DECISION_TIME_KEYS
    }


def _resolution_index(
    rows: Iterable[Mapping[str, Any]],
    *,
    resolution_path: str | Path | None = None,
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    by_shared_candidate_id: dict[str, list[int]] = {}
    by_run_market: dict[tuple[str, str], list[int]] = {}
    by_market_id: dict[str, list[int]] = {}
    source_path = str(resolution_path) if resolution_path not in (None, "") else None
    for row in rows:
        entry = {"row": dict(row), "resolution_source_path": source_path}
        entry_index = len(entries)
        entries.append(entry)
        shared_candidate_id = _optional_text(row.get("shared_candidate_id"))
        market_id = _optional_text(row.get("market_id"), row.get("ticker"))
        run_id = _optional_text(row.get("run_id"))
        if shared_candidate_id:
            by_shared_candidate_id.setdefault(shared_candidate_id, []).append(entry_index)
        if run_id and market_id:
            by_run_market.setdefault((run_id, market_id), []).append(entry_index)
        if market_id:
            by_market_id.setdefault(market_id, []).append(entry_index)
    return {
        "entries": entries,
        "by_shared_candidate_id": by_shared_candidate_id,
        "by_run_market": by_run_market,
        "by_market_id": by_market_id,
    }


def _iter_jsonl_mappings(path: Path) -> Iterable[Mapping[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, Mapping):
                yield row


def _find_resolution(
    index: Mapping[str, Any],
    *,
    shared_candidate_id: str | None,
    run_id: str | None,
    market_id: str | None,
) -> dict[str, Any]:
    lookup_plan: list[tuple[str, Any, str | None]] = []
    if shared_candidate_id:
        lookup_plan.append(("shared_candidate_id", shared_candidate_id, shared_candidate_id))
    if run_id and market_id:
        lookup_plan.append(("run_id_market_id", (run_id, market_id), f"{run_id}|{market_id}"))
    if market_id:
        lookup_plan.append(("market_id", market_id, market_id))

    entries = list(index.get("entries", []) or [])
    for matched_by, key, display_key in lookup_plan:
        bucket_name = {
            "shared_candidate_id": "by_shared_candidate_id",
            "run_id_market_id": "by_run_market",
            "market_id": "by_market_id",
        }[matched_by]
        entry_indexes = list(_mapping(index.get(bucket_name)).get(key, []) or [])
        if not entry_indexes:
            continue
        if len(entry_indexes) > 1:
            outcomes = {
                _normalized_resolution_outcome(entries[i].get("row"))
                for i in entry_indexes
                if i < len(entries)
            }
            outcomes.discard(None)
            if len(outcomes) != 1 or matched_by == "market_id":
                return {
                    "row": None,
                    "blocker": "ambiguous_resolution",
                    "matched_by": matched_by,
                    "match_key": display_key,
                    "candidate_match_count": len(entry_indexes),
                }
        entry = entries[entry_indexes[0]] if entry_indexes[0] < len(entries) else {}
        return {
            "row": _mapping(entry.get("row")),
            "matched_by": matched_by,
            "match_key": display_key,
            "resolution_source_path": _optional_text(entry.get("resolution_source_path")),
            "candidate_match_count": len(entry_indexes),
        }
    return {"row": None, "matched_by": None, "match_key": None}

def _normalized_resolution_outcome(row: Mapping[str, Any] | None) -> str | None:
    if not isinstance(row, Mapping):
        return None
    for source in (row, _mapping(row.get("resolution"))):
        if not source:
            continue
        for key in ("resolved_outcome", "actual_outcome", "outcome", "result", "settled_side", "settlement_value"):
            value = _optional_text(source.get(key))
            if value in {"YES", "NO", "VOID"}:
                return value
            upper = str(value or "").strip().upper()
            if upper in {"YES", "NO", "VOID"}:
                return upper
            if upper in {"TRUE", "1", "1.0"}:
                return "YES"
            if upper in {"FALSE", "0", "0.0"}:
                return "NO"
    return None


def _empty_pnl_bucket() -> dict[str, Any]:
    return {
        "evaluated_rows": 0,
        "resolved_rows": 0,
        "buy_rows": 0,
        "skip_rows": 0,
        "pnl_calculable_rows": 0,
        "winning_buy_rows": 0,
        "losing_buy_rows": 0,
        "total_stake_usd": 0.0,
        "total_payout_usd": 0.0,
        "total_pnl_usd": 0.0,
        "blocker_counts": {},
    }


def _summarize_source_router_resolution_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Return first-class source-router win-rate and standardized-PnL metrics."""

    summary = _empty_pnl_bucket()
    action_counts: dict[str, int] = {}
    side_counts: dict[str, int] = {}
    blocker_counts: dict[str, int] = {}
    resolved_buy_rows = 0
    correct_side_rows = 0

    for row in rows:
        if _lane_id_for_row(row) != SOURCE_ROUTER_LANE_ID:
            continue
        _increment_pnl_bucket(summary, "evaluated_rows")
        action = _action_label(row.get("action"))
        action_counts[action] = int(action_counts.get(action, 0)) + 1
        side = _optional_text(row.get("side"))
        if side:
            side_counts[side] = int(side_counts.get(side, 0)) + 1

        resolution = _mapping(row.get("resolution"))
        outcome = _optional_text(resolution.get("outcome"))
        if outcome is not None:
            _increment_pnl_bucket(summary, "resolved_rows")

        blocker = _optional_text(row.get("blocker"))
        if blocker:
            _add_blocker(blocker_counts, blocker)
            _add_blocker(summary["blocker_counts"], blocker)
            continue

        if _is_skip_action(action):
            _increment_pnl_bucket(summary, "skip_rows")
            _increment_pnl_bucket(summary, "pnl_calculable_rows")
            continue
        if not _is_buy_action(action):
            continue

        pnl = _mapping(row.get("pnl"))
        won = bool(pnl.get("won"))
        stake_f = float(_number(pnl.get("stake_usd")) or 0.0)
        payout = float(_number(pnl.get("payout_usd")) or 0.0)
        pnl_value = float(_number(pnl.get("pnl_usd")) or 0.0)

        resolved_buy_rows += 1
        if won:
            correct_side_rows += 1
        _increment_pnl_bucket(summary, "buy_rows")
        _increment_pnl_bucket(summary, "pnl_calculable_rows")
        _increment_pnl_bucket(summary, "winning_buy_rows" if won else "losing_buy_rows")
        summary["total_stake_usd"] += stake_f
        summary["total_payout_usd"] += payout
        summary["total_pnl_usd"] += pnl_value

    finalized = _finalize_pnl_bucket(summary)
    finalized["raw_router_resolved_buy_rows"] = resolved_buy_rows
    finalized["raw_router_correct_side_rows"] = correct_side_rows
    finalized["raw_router_win_rate_pct"] = _coverage_pct(correct_side_rows, resolved_buy_rows)
    finalized["standardized_hypothetical_stake_usd"] = finalized.get("total_stake_usd")
    finalized["standardized_hypothetical_pnl_usd"] = finalized.get("total_pnl_usd")
    finalized["standardized_hypothetical_roi_pct"] = finalized.get("roi_pct")
    finalized["action_counts"] = _sorted_count_dict(action_counts)
    finalized["side_counts"] = _sorted_count_dict(side_counts)
    finalized["blocker_counts"] = {str(k): int(v) for k, v in sorted(blocker_counts.items()) if int(v)}
    return finalized


def _increment_pnl_bucket(bucket: dict[str, Any], key: str) -> None:
    bucket[key] = int(bucket.get(key, 0)) + 1


def _add_blocker(counts: dict[str, int], key: str) -> None:
    counts[key] = int(counts.get(key, 0)) + 1


def _finalize_pnl_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    finalized: dict[str, Any] = {}
    for key, value in bucket.items():
        if key == "by_lane" and isinstance(value, Mapping):
            finalized[key] = {lane_id: _finalize_pnl_bucket(dict(lane_bucket)) for lane_id, lane_bucket in value.items()}
        elif key == "blocker_counts" and isinstance(value, Mapping):
            finalized[key] = {str(k): int(v) for k, v in sorted(value.items()) if int(v)}
        elif isinstance(value, float):
            finalized[key] = round(value, 4)
        else:
            finalized[key] = value
    stake = float(finalized.get("total_stake_usd") or 0.0)
    finalized["roi_pct"] = round((float(finalized.get("total_pnl_usd") or 0.0) / stake) * 100.0, 2) if stake else None
    return finalized

def _coverage_pct(count: int, total: int) -> float | None:
    if total <= 0:
        return None
    return round((count / total) * 100.0, 2)


def _label_source_name(future_pnl_inputs: Mapping[str, Any]) -> str:
    return (
        _optional_text(
            future_pnl_inputs.get("label_target"),
            future_pnl_inputs.get("actual_source"),
            future_pnl_inputs.get("settlement_source"),
        )
        or "unknown"
    )


def _label_source_classification(label_source: str, *, settlement_source: str | None) -> str:
    normalized = _normalized_source_label(label_source)
    settlement_normalized = _normalized_source_label(settlement_source or "")
    if normalized in ("", "unknown"):
        return "unknown"
    if _is_independent_label_source(normalized):
        return "independent"
    if _is_settlement_derived_label_source(normalized) or (
        settlement_normalized and normalized == settlement_normalized
    ):
        return "settlement_derived"
    return "explicit_non_independent"


def _is_independent_label_source(value: str) -> bool:
    normalized = _normalized_source_label(value)
    if _is_settlement_derived_label_source(normalized):
        return False
    return any(token in normalized for token in ("observed", "daily", "archive", "asos", "station", "iem"))


def _is_settlement_derived_label_source(value: str) -> bool:
    normalized = _normalized_source_label(value)
    return (
        "settlement" in normalized
        or normalized in {"kalshi", "nws"}
        or normalized.startswith("nws_")
        or normalized.endswith("_nws")
    )


def _normalized_source_label(value: Any) -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def _has_order_book_quotes(future_pnl_inputs: Mapping[str, Any]) -> bool:
    return any(
        _number(future_pnl_inputs.get(key)) is not None
        for key in ("best_yes_ask", "best_yes_bid", "best_no_ask", "best_no_bid")
    )


def _has_execution_snapshot(future_pnl_inputs: Mapping[str, Any]) -> bool:
    return bool(
        _optional_text(
            future_pnl_inputs.get("execution_snapshot_source"),
            future_pnl_inputs.get("execution_snapshot_as_of"),
        )
    )


def _parse_timestamp(value: Any) -> datetime | None:
    text = _optional_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _shared_candidate(candidate_input: Any) -> dict[str, Any]:
    shared_candidate = getattr(candidate_input, "shared_candidate", None)
    return dict(shared_candidate) if isinstance(shared_candidate, dict) else {}


def _shared_candidate_ref(
    shared_candidate: Mapping[str, Any],
    *,
    signal: Mapping[str, Any],
    shared_candidate_id: str,
    candidate_dataset_path: str | Path,
) -> dict[str, Any]:
    market = _mapping(shared_candidate.get("market"))
    return {
        "input_source": "shared_candidate_dataset",
        "market_source": "shared_market",
        "candidate_id": _text(shared_candidate.get("candidate_id"), signal.get("shared_candidate_id"), shared_candidate_id),
        "shared_candidate_id": _text(shared_candidate.get("candidate_id"), signal.get("shared_candidate_id"), shared_candidate_id),
        "candidate_dataset_path": str(candidate_dataset_path),
        "market_id": _text(shared_candidate.get("market_id"), market.get("id"), signal.get("market_id")),
        "source_runtime": _optional_text(shared_candidate.get("source_runtime"), signal.get("candidate_source_runtime")),
        "provenance": _optional_text(shared_candidate.get("provenance"), signal.get("candidate_provenance")),
        "observed_at": _optional_text(shared_candidate.get("observed_at"), signal.get("candidate_observed_at"), signal.get("observed_at")),
        "snapshot_as_of": _optional_text(shared_candidate.get("snapshot_as_of"), signal.get("snapshot_as_of"), signal.get("source_as_of")),
        "shared_snapshot_id": _optional_text(
            shared_candidate.get("shared_snapshot_id"),
            shared_candidate.get("snapshot_id"),
            signal.get("shared_snapshot_id"),
        ),
        "snapshot_id": _optional_text(
            shared_candidate.get("shared_snapshot_id"),
            shared_candidate.get("snapshot_id"),
            signal.get("shared_snapshot_id"),
        ),
        "snapshot_ttl_seconds": _first_present(shared_candidate.get("snapshot_ttl_seconds"), signal.get("snapshot_ttl_seconds")),
    }


def _observed_at(source_row: dict[str, Any] | None, signal: Mapping[str, Any]) -> str:
    value = signal.get("candidate_observed_at") or signal.get("observed_at") or (source_row or {}).get("observed_at")
    if value not in (None, ""):
        return str(value)
    return datetime.now(timezone.utc).isoformat()


def _confidence_floor(lane: _LaneDefinition) -> float:
    for key in ("confidence_floor", "min_confidence"):
        if key not in lane.parameters or lane.parameters.get(key) in (None, ""):
            continue
        value = lane.parameters.get(key)
        number = _number(value)
        if number is None:
            raise ValueError(f"paper shadow lane {lane.lane_id} has invalid {key}: {value!r}")
        return float(number)
    return DEFAULT_CONFIDENCE_FLOOR


def _source_reliability_scoreboard_path(lane: _LaneDefinition) -> str | None:
    for key in (
        "scoreboard_path",
        "source_reliability_scoreboard",
        "source_reliability_scoreboard_path",
        "source_scoreboard_path",
    ):
        value = lane.parameters.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _source_scoreboard_provenance(
    lane: _LaneDefinition,
    *,
    signal: Mapping[str, Any],
    shared_candidate: Mapping[str, Any],
    source_row: Mapping[str, Any] | None,
    source_reliability: Mapping[str, Any],
) -> dict[str, Any]:
    future_pnl_inputs = _future_pnl_inputs(
        signal=signal,
        shared_candidate=shared_candidate,
        source_row=source_row,
        source_reliability=source_reliability,
    )
    summary = {
        "lane_id": lane.lane_id,
        "available": bool(source_reliability.get("available")),
        "recommended_action": _optional_text(source_reliability.get("recommended_action")),
        "recommended_side": _side_from_action(str(source_reliability.get("recommended_action") or "")),
        "reason_code": _optional_text(source_reliability.get("reason_code")),
        "reason": _optional_text(source_reliability.get("reason")),
        "effect": _optional_text(source_reliability.get("effect")),
        "scoreboard_path": _optional_text(source_reliability.get("scoreboard_path")),
        "label_target": _optional_text(future_pnl_inputs.get("label_target")),
        "future_pnl_inputs": future_pnl_inputs or None,
    }
    return {key: value for key, value in summary.items() if value not in (None, "", {}, []) or key == "available"}


def _source_router_provenance(
    lane: _LaneDefinition,
    *,
    signal: Mapping[str, Any],
    shared_candidate: Mapping[str, Any],
    source_row: Mapping[str, Any] | None,
    source_router: Mapping[str, Any],
    requested_position_size_usd: float | None,
    approved_position_size_usd: float | None,
) -> dict[str, Any]:
    action = _optional_text(source_router.get("recommended_action")) or "SKIP"
    future_pnl_inputs = _future_pnl_inputs(
        signal=signal,
        shared_candidate=shared_candidate,
        source_row=source_row,
        source_reliability={
            "recommended_action": action,
            "reason_code": source_router.get("reason_code"),
            "reason": "Source router shadow lane selected the source-implied side",
        },
    )
    side = _side_from_action(action)
    side_price = _side_specific_price(future_pnl_inputs, side)
    if side_price is not None:
        future_pnl_inputs["entry_price"] = side_price
        future_pnl_inputs["estimated_fill_price"] = side_price
    future_pnl_inputs["recommended_action"] = action
    future_pnl_inputs["recommended_side"] = side
    future_pnl_inputs["side"] = side
    future_pnl_inputs["requested_position_size_usd"] = requested_position_size_usd
    future_pnl_inputs["approved_position_size_usd"] = approved_position_size_usd
    future_pnl_inputs["source_router_source_direction"] = _optional_text(source_router.get("source_direction"))
    future_pnl_inputs["source_router_source_grade"] = _optional_text(source_router.get("source_grade"))

    compact_observations = _compact_source_observations(source_router.get("source_observations"))
    summary = {
        "lane_id": lane.lane_id,
        "available": bool(source_router.get("available")),
        "recommended_action": action,
        "recommended_side": side,
        "reason_code": _optional_text(source_router.get("reason_code")),
        "source_confidence_reason_code": _optional_text(source_router.get("source_confidence_reason_code")),
        "source_direction": _optional_text(source_router.get("source_direction")),
        "source_grade": _optional_text(source_router.get("source_grade")),
        "source_confidence_score": _number(source_router.get("source_confidence_score")),
        "confidence_type": _optional_text(source_router.get("confidence_type")),
        "agreement_state": _optional_text(source_router.get("agreement_state")),
        "scoreboard_path": _optional_text(source_router.get("scoreboard_path")),
        "data_quality": _mapping(source_router.get("data_quality")),
        "sources_used": _list_of_mappings(source_router.get("sources_used")),
        "sources_excluded": _list_of_mappings(source_router.get("sources_excluded")),
        "source_observations": compact_observations,
        "future_pnl_inputs": future_pnl_inputs or None,
        "decision_contract": _optional_text(source_router.get("decision_contract")),
    }
    return {key: value for key, value in summary.items() if value not in (None, "", {}, []) or key == "available"}


def _future_pnl_inputs(
    *,
    signal: Mapping[str, Any],
    shared_candidate: Mapping[str, Any],
    source_row: Mapping[str, Any] | None,
    source_reliability: Mapping[str, Any],
) -> dict[str, Any]:
    source_row = source_row if isinstance(source_row, Mapping) else {}
    artifact = _candidate_artifact(signal, shared_candidate)
    artifact_order_book = _mapping(artifact.get("order_book"))
    order_book_snapshot = _mapping(artifact.get("order_book_snapshot"))
    order_book_data = _mapping(order_book_snapshot.get("data"))
    execution_snapshot = _mapping(artifact.get("execution_snapshot"))
    weather_snapshot = _candidate_weather_source_snapshot(signal, shared_candidate, artifact)
    weather_forecast = _mapping(weather_snapshot.get("forecast"))
    market = _mapping(shared_candidate.get("market"))
    route_evidence = _candidate_route_evidence(shared_candidate, artifact)
    market_metadata = _candidate_market_metadata(shared_candidate, artifact)
    question = _compact_question_text(
        signal.get("question"),
        shared_candidate.get("question"),
        market.get("question"),
        weather_snapshot.get("question"),
        market_metadata.get("question"),
        source_row.get("question"),
    )
    question_context = {
        **market_metadata,
        **route_evidence,
        **market,
        **weather_snapshot,
        **weather_forecast,
        **source_row,
        **shared_candidate,
        **signal,
    }
    question_side = _optional_text(
        signal.get("question_side"),
        shared_candidate.get("question_side"),
        market.get("question_side"),
        weather_forecast.get("question_side"),
        weather_snapshot.get("question_side"),
        source_row.get("question_side"),
    )
    if not question_side and question:
        inferred_question_side = infer_question_side(question, question_context)
        if inferred_question_side and inferred_question_side != "unknown":
            question_side = inferred_question_side
    threshold = _number(
        signal.get("threshold"),
        shared_candidate.get("threshold"),
        market.get("threshold"),
        weather_forecast.get("threshold"),
        weather_snapshot.get("threshold"),
        market_metadata.get("threshold"),
        route_evidence.get("threshold"),
        source_row.get("threshold"),
    )
    if threshold is None and question:
        threshold = _number(extract_threshold_value(question, question_context))
    market_kind = _future_pnl_market_kind(
        signal=signal,
        shared_candidate=shared_candidate,
        source_row=source_row,
        weather_snapshot=weather_snapshot,
        market=market,
        question=question,
    )
    contract_shape = _future_pnl_contract_shape(
        signal=signal,
        shared_candidate=shared_candidate,
        source_row=source_row,
        weather_snapshot=weather_snapshot,
        market=market,
        market_id=_optional_text(signal.get("market_id"), shared_candidate.get("market_id"), source_row.get("market_id")),
        question=question,
        question_side=question_side,
    )
    initial_side = _side_from_action(str(source_row.get("action") or signal.get("direction") or ""))
    entry_price = _shadow_entry_price_for_side(initial_side, signal, source_row)
    estimated_fill_price = _price_number(
        signal.get("estimated_fill_price"),
        execution_snapshot.get("estimated_fill_price"),
        order_book_data.get("estimated_fill_price"),
        artifact_order_book.get("estimated_fill_price"),
    )
    if estimated_fill_price is None:
        estimated_fill_price = _side_price_from_book(
            initial_side,
            {
                "best_yes_ask": _price_number(
                    signal.get("best_yes_ask"),
                    execution_snapshot.get("best_yes_ask"),
                    order_book_data.get("best_yes_ask"),
                    artifact_order_book.get("best_yes_ask"),
                ),
                "best_no_ask": _price_number(
                    signal.get("best_no_ask"),
                    execution_snapshot.get("best_no_ask"),
                    order_book_data.get("best_no_ask"),
                    artifact_order_book.get("best_no_ask"),
                ),
            },
        )
    if entry_price is None:
        entry_price = estimated_fill_price

    payload = {
        "shared_candidate_id": _optional_text(
            signal.get("shared_candidate_id"),
            shared_candidate.get("candidate_id"),
            source_row.get("shared_candidate_id"),
        ),
        "market_id": _optional_text(signal.get("market_id"), shared_candidate.get("market_id"), source_row.get("market_id")),
        "observed_at": _optional_text(
            signal.get("candidate_observed_at"),
            signal.get("observed_at"),
            shared_candidate.get("observed_at"),
            source_row.get("observed_at"),
        ),
        "stable_action": _optional_text(source_row.get("action")),
        "stable_reason_code": _optional_text(source_row.get("reason_code")),
        "stable_reason": _optional_text(source_row.get("reason")),
        "stable_requested_position_size_usd": _number(source_row.get("requested_position_size_usd")),
        "stable_approved_position_size_usd": _number(source_row.get("approved_position_size_usd")),
        "stable_confidence": _number(source_row.get("confidence"), signal.get("confidence")),
        "recommended_action": _optional_text(source_reliability.get("recommended_action")),
        "recommended_side": _side_from_action(str(source_reliability.get("recommended_action") or "")),
        "recommendation_reason_code": _optional_text(source_reliability.get("reason_code")),
        "recommendation_reason": _optional_text(source_reliability.get("reason")),
        "side": initial_side,
        "entry_price": entry_price,
        "estimated_fill_price": estimated_fill_price,
        "best_yes_ask": _price_number(
            signal.get("best_yes_ask"),
            execution_snapshot.get("best_yes_ask"),
            order_book_data.get("best_yes_ask"),
            artifact_order_book.get("best_yes_ask"),
        ),
        "best_yes_bid": _price_number(
            signal.get("best_yes_bid"),
            execution_snapshot.get("best_yes_bid"),
            order_book_data.get("best_yes_bid"),
            artifact_order_book.get("best_yes_bid"),
        ),
        "best_no_ask": _price_number(
            signal.get("best_no_ask"),
            execution_snapshot.get("best_no_ask"),
            order_book_data.get("best_no_ask"),
            artifact_order_book.get("best_no_ask"),
        ),
        "best_no_bid": _price_number(
            signal.get("best_no_bid"),
            execution_snapshot.get("best_no_bid"),
            order_book_data.get("best_no_bid"),
            artifact_order_book.get("best_no_bid"),
        ),
        "execution_snapshot_source": _optional_text(
            signal.get("execution_snapshot_source"),
            artifact.get("execution_snapshot_source"),
            execution_snapshot.get("source"),
        ),
        "execution_snapshot_marker": _optional_text(
            signal.get("execution_snapshot_marker"),
            execution_snapshot.get("marker"),
            execution_snapshot.get("snapshot_kind"),
        ),
        "hypothetical_execution_snapshot": _first_present(
            signal.get("hypothetical_execution_snapshot"),
            execution_snapshot.get("hypothetical"),
        ),
        "order_book_source": _optional_text(
            signal.get("order_book_source"),
            artifact.get("order_book_source"),
            order_book_snapshot.get("source"),
            artifact_order_book.get("source"),
        ),
        "snapshot_as_of": _optional_text(
            signal.get("snapshot_as_of"),
            signal.get("source_as_of"),
            shared_candidate.get("snapshot_as_of"),
            weather_snapshot.get("as_of"),
            artifact.get("source_as_of"),
        ),
        "execution_snapshot_as_of": _optional_text(execution_snapshot.get("as_of"), execution_snapshot.get("observed_at")),
        "order_book_snapshot_as_of": _optional_text(order_book_snapshot.get("as_of"), order_book_snapshot.get("observed_at")),
        "threshold": threshold,
        "question_side": question_side,
        "market_kind": market_kind,
        "contract_shape": contract_shape,
        "question": question,
    }
    return {key: value for key, value in payload.items() if value not in (None, "", [], {})}


def _action_from_source_direction(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text == "YES":
        return "BUY_YES"
    if text == "NO":
        return "BUY_NO"
    return "SKIP"


def _source_router_notional(lane: _LaneDefinition, baseline: Mapping[str, Any]) -> float:
    configured = _number(lane.parameters.get("hypothetical_notional_usd"), lane.parameters.get("notional_usd"))
    if configured is not None and configured > 0:
        return float(configured)
    baseline_size = _number(baseline.get("approved_position_size_usd"), baseline.get("requested_position_size_usd"))
    if baseline_size is not None and baseline_size > 0:
        return float(baseline_size)
    return 10.0


def _compute_source_router_edge(
    signal: Mapping[str, Any],
    action: str,
) -> float | None:
    """Compute the expected-value edge for a source-router trade.

    Returns the edge in decimal (e.g., 0.05 = 5% edge over market).
    Returns None when required data is missing.
    """
    model_prob = _number(signal.get("model_probability"))
    if model_prob is None:
        return None
    if action == "BUY_YES":
        price = _price_number(
            signal.get("best_yes_ask"),
            signal.get("market_price"),
        )
        if price is None:
            return None
        return model_prob - price
    # BUY_NO
    price = _price_number(
        signal.get("best_no_ask"),
    )
    if price is None:
        # Fallback: approximate NO price from YES midpoint
        mp = _price_number(signal.get("market_price"))
        if mp is None:
            return None
        price = 1.0 - mp
    return (1.0 - model_prob) - price


def _side_specific_price(future_pnl_inputs: Mapping[str, Any], side: str | None) -> float | None:
    if side == "YES":
        return _price_number(
            future_pnl_inputs.get("best_yes_ask"),
            future_pnl_inputs.get("entry_price") if future_pnl_inputs.get("side") == "YES" else None,
            future_pnl_inputs.get("estimated_fill_price") if future_pnl_inputs.get("side") == "YES" else None,
        )
    if side == "NO":
        return _price_number(
            future_pnl_inputs.get("best_no_ask"),
            future_pnl_inputs.get("entry_price") if future_pnl_inputs.get("side") == "NO" else None,
            future_pnl_inputs.get("estimated_fill_price") if future_pnl_inputs.get("side") == "NO" else None,
        )
    return None


def _shadow_entry_price_for_side(
    side: str | None,
    signal: Mapping[str, Any],
    source_row: Mapping[str, Any],
) -> float | int | None:
    side_price = _side_price_from_book(side, signal)
    if side_price is not None:
        return side_price
    return _price_number(
        signal.get("entry_price"),
        signal.get("market_price"),
        source_row.get("entry_price"),
        source_row.get("price"),
    )


def _side_price_from_book(side: str | None, values: Mapping[str, Any]) -> float | int | None:
    if side == "YES":
        return _price_number(
            values.get("best_yes_ask"),
            values.get("yes_market_price"),
            values.get("yes_price"),
        )
    if side == "NO":
        return _price_number(
            values.get("best_no_ask"),
            values.get("no_market_price"),
            values.get("no_price"),
        )
    return None


def _compact_source_observations(value: Any) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for row in _list_of_mappings(value):
        observations.append(
            {
                key: row.get(key)
                for key in (
                    "source_id",
                    "source_name",
                    "source_family",
                    "forecast_temp_f",
                    "forecast_target",
                    "forecast_valid_at",
                    "observed_at",
                    "fetched_at",
                    "known_at_time_assertion",
                    "adapter_version",
                    "normalizer_version",
                    "provenance",
                )
                if row.get(key) not in (None, "", [], {})
            }
        )
    return observations


def _list_of_mappings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _summarize_source_scoreboard_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    scoreboard_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if not _is_source_scoreboard_lane(_lane_id_for_row(row)):
            continue
        row_dict = dict(row)
        provenance = _mapping(row.get("provenance"))
        scoreboard = _mapping(provenance.get("source_scoreboard"))
        if not scoreboard:
            continue
        scoreboard_rows.append((row_dict, scoreboard))

    label_source_counts: dict[str, int] = {}
    actual_source_counts: dict[str, int] = {}
    settlement_source_counts: dict[str, int] = {}
    recommended_action_counts: dict[str, int] = {}
    reason_code_counts: dict[str, int] = {}
    lane_row_counts: dict[str, int] = {}
    available_rows = 0
    unavailable_rows = 0
    rows_with_estimated_fill_price = 0
    rows_with_order_book_execution_prices = 0

    for row, scoreboard in scoreboard_rows:
        lane_id = _lane_id_for_row(row)
        lane_row_counts[lane_id] = lane_row_counts.get(lane_id, 0) + 1
        if bool(scoreboard.get("available")):
            available_rows += 1
        else:
            unavailable_rows += 1
        recommended_action = _optional_text(scoreboard.get("recommended_action"))
        if recommended_action:
            recommended_action_counts[recommended_action] = recommended_action_counts.get(recommended_action, 0) + 1
        reason_code = _optional_text(scoreboard.get("reason_code"))
        if reason_code:
            reason_code_counts[reason_code] = reason_code_counts.get(reason_code, 0) + 1
        future_pnl_inputs = _mapping(scoreboard.get("future_pnl_inputs"))
        if _number(future_pnl_inputs.get("estimated_fill_price")) is not None:
            rows_with_estimated_fill_price += 1
        if any(
            _number(future_pnl_inputs.get(key)) is not None
            for key in ("best_yes_ask", "best_yes_bid", "best_no_ask", "best_no_bid")
        ):
            rows_with_order_book_execution_prices += 1
        label_source = _optional_text(
            future_pnl_inputs.get("label_target"),
            future_pnl_inputs.get("actual_source"),
            future_pnl_inputs.get("settlement_source"),
        )
        if label_source:
            label_source_counts[label_source] = label_source_counts.get(label_source, 0) + 1
        actual_source = _optional_text(future_pnl_inputs.get("actual_source"))
        if actual_source:
            actual_source_counts[actual_source] = actual_source_counts.get(actual_source, 0) + 1
        settlement_source = _optional_text(future_pnl_inputs.get("settlement_source"))
        if settlement_source:
            settlement_source_counts[settlement_source] = settlement_source_counts.get(settlement_source, 0) + 1

    return {
        "evaluated_rows": len(scoreboard_rows),
        "lane_row_counts": _sorted_count_dict(lane_row_counts),
        "available_rows": available_rows,
        "unavailable_rows": unavailable_rows,
        "recommended_action_counts": _sorted_count_dict(recommended_action_counts),
        "reason_code_counts": _sorted_count_dict(reason_code_counts),
        "rows_with_estimated_fill_price": rows_with_estimated_fill_price,
        "rows_with_order_book_execution_prices": rows_with_order_book_execution_prices,
        "label_source_counts": _sorted_count_dict(label_source_counts),
        "actual_source_counts": _sorted_count_dict(actual_source_counts),
        "settlement_source_counts": _sorted_count_dict(settlement_source_counts),
    }


def _summarize_source_reliability_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    reliability_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if not _is_source_reliability_lane(_lane_id_for_row(row)):
            continue
        row_dict = dict(row)
        provenance = _mapping(row.get("provenance"))
        reliability = _mapping(provenance.get("source_reliability"))
        if not reliability:
            continue
        reliability_rows.append((row_dict, reliability))

    recommended_action_counts: dict[str, int] = {}
    reason_code_counts: dict[str, int] = {}
    lane_row_counts: dict[str, int] = {}
    available_rows = 0
    unavailable_rows = 0

    for row, reliability in reliability_rows:
        lane_id = _lane_id_for_row(row)
        lane_row_counts[lane_id] = lane_row_counts.get(lane_id, 0) + 1
        if bool(reliability.get("available")):
            available_rows += 1
        else:
            unavailable_rows += 1
        recommended_action = _optional_text(reliability.get("recommended_action"))
        if recommended_action:
            recommended_action_counts[recommended_action] = recommended_action_counts.get(recommended_action, 0) + 1
        reason_code = _optional_text(reliability.get("reason_code"))
        if reason_code:
            reason_code_counts[reason_code] = reason_code_counts.get(reason_code, 0) + 1

    return {
        "evaluated_rows": len(reliability_rows),
        "lane_row_counts": _sorted_count_dict(lane_row_counts),
        "available_rows": available_rows,
        "unavailable_rows": unavailable_rows,
        "recommended_action_counts": _sorted_count_dict(recommended_action_counts),
        "reason_code_counts": _sorted_count_dict(reason_code_counts),
    }


def _summarize_source_scoreboard_readiness(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    readiness_rows: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if not _is_source_scoreboard_lane(_lane_id_for_row(row)):
            continue
        provenance = _mapping(row.get("provenance"))
        scoreboard = _mapping(provenance.get("source_scoreboard"))
        if not scoreboard:
            continue
        readiness_rows.append(
            (
                dict(row),
                scoreboard,
                _mapping(scoreboard.get("future_pnl_inputs")),
                _mapping(provenance.get("source_reliability")),
            )
        )

    label_source_counts: dict[str, int] = {}
    reliability_tier_counts: dict[str, int] = {}
    reason_code_counts: dict[str, int] = {}
    label_class_counts = {
        "explicit_non_independent": 0,
        "independent": 0,
        "settlement_derived": 0,
        "unknown": 0,
    }
    leak_risk_indicators = {
        "known_after_not_after_observed_at_rows": 0,
        "label_matches_settlement_source_rows": 0,
        "settlement_derived_label_rows": 0,
        "unknown_label_rows": 0,
    }
    missing_field_blockers = {
        "missing_actual_outcome_rows": 0,
        "missing_estimated_fill_price_rows": 0,
        "missing_execution_snapshot_rows": 0,
        "missing_known_after_rows": 0,
        "missing_label_source_rows": 0,
        "missing_market_id_rows": 0,
        "missing_observed_at_rows": 0,
        "missing_order_book_quotes_rows": 0,
        "missing_resolution_outcome_rows": 0,
        "missing_shared_candidate_id_rows": 0,
    }
    explicit_label_rows = 0
    independent_label_rows = 0
    order_book_quote_rows = 0
    execution_snapshot_rows = 0
    estimated_fill_price_rows = 0
    rows_with_trusted_sources = 0
    rows_with_neutral_sources = 0
    rows_with_excluded_sources = 0
    rows_with_any_blocker = 0
    rows_with_any_leak_risk = 0

    for row, scoreboard, future_pnl_inputs, source_reliability in readiness_rows:
        row_has_blocker = False
        row_has_leak_risk = False

        label_source = _label_source_name(future_pnl_inputs)
        label_source_counts[label_source] = label_source_counts.get(label_source, 0) + 1
        label_class = _label_source_classification(
            label_source,
            settlement_source=_optional_text(future_pnl_inputs.get("settlement_source")),
        )
        label_class_counts[label_class] += 1
        if label_source != "unknown":
            explicit_label_rows += 1
        else:
            leak_risk_indicators["unknown_label_rows"] += 1
            missing_field_blockers["missing_label_source_rows"] += 1
            row_has_blocker = True
            row_has_leak_risk = True
        if label_class == "independent":
            independent_label_rows += 1
        if label_class == "settlement_derived":
            leak_risk_indicators["settlement_derived_label_rows"] += 1
            row_has_leak_risk = True

        reason_code = _optional_text(scoreboard.get("reason_code"), source_reliability.get("reason_code"))
        if reason_code:
            reason_code_counts[reason_code] = reason_code_counts.get(reason_code, 0) + 1

        tier_counts = _mapping(source_reliability.get("tier_counts"))
        trusted_count = 0
        neutral_count = 0
        excluded_count = 0
        for key, value in tier_counts.items():
            count = int(value) if isinstance(value, (int, float)) else 0
            if count <= 0:
                continue
            tier = str(key)
            reliability_tier_counts[tier] = reliability_tier_counts.get(tier, 0) + count
            if tier in {"trusted", "strong_trusted"}:
                trusted_count += count
            elif tier == "neutral":
                neutral_count += count
            elif tier == "excluded":
                excluded_count += count
        if trusted_count > 0:
            rows_with_trusted_sources += 1
        if neutral_count > 0:
            rows_with_neutral_sources += 1
        if excluded_count > 0:
            rows_with_excluded_sources += 1

        if _has_order_book_quotes(future_pnl_inputs):
            order_book_quote_rows += 1
        else:
            missing_field_blockers["missing_order_book_quotes_rows"] += 1
            row_has_blocker = True
        if _has_execution_snapshot(future_pnl_inputs):
            execution_snapshot_rows += 1
        else:
            missing_field_blockers["missing_execution_snapshot_rows"] += 1
            row_has_blocker = True
        if _number(future_pnl_inputs.get("estimated_fill_price")) is not None:
            estimated_fill_price_rows += 1
        else:
            missing_field_blockers["missing_estimated_fill_price_rows"] += 1
            row_has_blocker = True

        if not _optional_text(future_pnl_inputs.get("shared_candidate_id")):
            missing_field_blockers["missing_shared_candidate_id_rows"] += 1
            row_has_blocker = True
        if not _optional_text(future_pnl_inputs.get("market_id")):
            missing_field_blockers["missing_market_id_rows"] += 1
            row_has_blocker = True
        observed_at = _optional_text(future_pnl_inputs.get("observed_at"))
        if not observed_at:
            missing_field_blockers["missing_observed_at_rows"] += 1
            row_has_blocker = True
        known_after = _optional_text(future_pnl_inputs.get("known_after"))
        if not known_after:
            missing_field_blockers["missing_known_after_rows"] += 1
            row_has_blocker = True
        actual_outcome = _optional_text(future_pnl_inputs.get("actual_outcome"))
        if not actual_outcome:
            missing_field_blockers["missing_actual_outcome_rows"] += 1
            row_has_blocker = True
        resolved_outcome = _optional_text(future_pnl_inputs.get("resolved_outcome"))
        if not resolved_outcome:
            missing_field_blockers["missing_resolution_outcome_rows"] += 1
            row_has_blocker = True

        settlement_source = _optional_text(future_pnl_inputs.get("settlement_source"))
        if label_source != "unknown" and settlement_source and label_source == settlement_source:
            leak_risk_indicators["label_matches_settlement_source_rows"] += 1
            row_has_leak_risk = True
        observed_dt = _parse_timestamp(observed_at)
        known_after_dt = _parse_timestamp(known_after)
        if observed_dt is not None and known_after_dt is not None and known_after_dt <= observed_dt:
            leak_risk_indicators["known_after_not_after_observed_at_rows"] += 1
            row_has_leak_risk = True

        if row_has_blocker:
            rows_with_any_blocker += 1
        if row_has_leak_risk:
            rows_with_any_leak_risk += 1

    total_rows = len(readiness_rows)
    return {
        "evaluated_rows": total_rows,
        "recommendation_only": True,
        "independence_inference": "heuristic_from_label_target_actual_source_and_settlement_source",
        "label_source_counts": _sorted_count_dict(label_source_counts),
        "label_class_counts": label_class_counts,
        "explicit_label_rows": explicit_label_rows,
        "explicit_label_coverage_pct": _coverage_pct(explicit_label_rows, total_rows),
        "independent_label_rows": independent_label_rows,
        "independent_label_coverage_pct": _coverage_pct(independent_label_rows, total_rows),
        "order_book_quote_rows": order_book_quote_rows,
        "order_book_quote_coverage_pct": _coverage_pct(order_book_quote_rows, total_rows),
        "execution_snapshot_rows": execution_snapshot_rows,
        "execution_snapshot_coverage_pct": _coverage_pct(execution_snapshot_rows, total_rows),
        "estimated_fill_price_rows": estimated_fill_price_rows,
        "estimated_fill_coverage_pct": _coverage_pct(estimated_fill_price_rows, total_rows),
        "reliability_tier_counts": _sorted_count_dict(reliability_tier_counts),
        "rows_with_trusted_sources": rows_with_trusted_sources,
        "rows_with_neutral_sources": rows_with_neutral_sources,
        "rows_with_excluded_sources": rows_with_excluded_sources,
        "reason_code_counts": _sorted_count_dict(reason_code_counts),
        "leak_risk_indicators": leak_risk_indicators,
        "missing_field_blockers": missing_field_blockers,
        "rows_with_any_blocker": rows_with_any_blocker,
        "rows_with_any_leak_risk": rows_with_any_leak_risk,
    }


def _is_source_scoreboard_lane(lane_id: Any) -> bool:
    return str(lane_id or "") in SOURCE_SCOREBOARD_LANE_IDS


def _is_source_reliability_lane(lane_id: Any) -> bool:
    return str(lane_id or "") == SOURCE_RELIABILITY_LANE_ID


def _candidate_artifact(signal: Mapping[str, Any], shared_candidate: Mapping[str, Any]) -> dict[str, Any]:
    for value in (
        signal.get("decision_artifact"),
        shared_candidate.get("decision_artifact"),
        shared_candidate.get("artifact"),
        _mapping(shared_candidate.get("evidence")).get("decision_artifact"),
    ):
        if isinstance(value, Mapping) and value:
            return dict(value)
    return {}


def _candidate_weather_source_snapshot(
    signal: Mapping[str, Any],
    shared_candidate: Mapping[str, Any],
    artifact: Mapping[str, Any],
) -> dict[str, Any]:
    for value in (
        signal.get("weather_source_snapshot"),
        shared_candidate.get("weather_source_snapshot"),
        _mapping(shared_candidate.get("evidence")).get("weather_source_snapshot"),
        _mapping(_mapping(_mapping(artifact.get("source_context")).get("data")).get("weather_source_snapshot")),
    ):
        if isinstance(value, Mapping) and value:
            return dict(value)
    return {}


def _candidate_route_evidence(shared_candidate: Mapping[str, Any], artifact: Mapping[str, Any]) -> dict[str, Any]:
    for value in (
        _mapping(shared_candidate.get("market_route")).get("evidence"),
        _mapping(artifact.get("market_route")).get("evidence"),
    ):
        if isinstance(value, Mapping) and value:
            return dict(value)
    return {}


def _candidate_market_metadata(shared_candidate: Mapping[str, Any], artifact: Mapping[str, Any]) -> dict[str, Any]:
    source_context_data = _mapping(_mapping(artifact.get("source_context")).get("data"))
    for value in (
        source_context_data.get("market_metadata"),
        shared_candidate.get("market_metadata"),
        shared_candidate.get("metadata"),
    ):
        if isinstance(value, Mapping) and value:
            return dict(value)
    return {}


def _label_target(
    signal: Mapping[str, Any],
    shared_candidate: Mapping[str, Any],
    source_row: Mapping[str, Any],
    weather_snapshot: Mapping[str, Any],
) -> str:
    for value in (
        signal.get("label_target"),
        weather_snapshot.get("label_target"),
        _mapping(signal.get("resolution")).get("label_target"),
        _mapping(shared_candidate.get("resolution")).get("label_target"),
        source_row.get("label_target"),
    ):
        text = _optional_text(value)
        if text:
            return text
    return (
        _optional_text(
            signal.get("actual_source"),
            weather_snapshot.get("actual_source"),
            _mapping(signal.get("resolution")).get("actual_source"),
            _mapping(shared_candidate.get("resolution")).get("actual_source"),
            signal.get("settlement_source"),
            weather_snapshot.get("settlement_source"),
            _mapping(signal.get("resolution")).get("settlement_source"),
            _mapping(shared_candidate.get("resolution")).get("settlement_source"),
        )
        or "unknown"
    )


def _future_pnl_market_kind(
    *,
    signal: Mapping[str, Any],
    shared_candidate: Mapping[str, Any],
    source_row: Mapping[str, Any],
    weather_snapshot: Mapping[str, Any],
    market: Mapping[str, Any],
    question: str | None,
) -> str | None:
    market_kind = _optional_text(
        signal.get("market_kind"),
        shared_candidate.get("market_kind"),
        market.get("market_kind"),
        _mapping(weather_snapshot.get("forecast")).get("market_kind"),
        weather_snapshot.get("market_kind"),
        source_row.get("market_kind"),
    )
    if market_kind and market_kind != "unknown":
        return market_kind
    candidates = " ".join(
        value.lower()
        for value in (
            _optional_text(signal.get("market_id"), shared_candidate.get("market_id"), source_row.get("market_id")),
            question,
            _optional_text(signal.get("market_type"), shared_candidate.get("market_type"), market.get("market_type")),
        )
        if value
    )
    if re.search(r"\b(high|max|maximum)\b", candidates) or "kxhigh" in candidates:
        return "high"
    if re.search(r"\b(low|min|minimum)\b", candidates) or "kxlow" in candidates:
        return "low"
    return None


def _future_pnl_contract_shape(
    *,
    signal: Mapping[str, Any],
    shared_candidate: Mapping[str, Any],
    source_row: Mapping[str, Any],
    weather_snapshot: Mapping[str, Any],
    market: Mapping[str, Any],
    market_id: str | None,
    question: str | None,
    question_side: str | None,
) -> str | None:
    contract_shape = _optional_text(
        signal.get("contract_shape"),
        shared_candidate.get("contract_shape"),
        market.get("contract_shape"),
        _mapping(weather_snapshot.get("forecast")).get("contract_shape"),
        weather_snapshot.get("contract_shape"),
        source_row.get("contract_shape"),
    )
    if contract_shape and contract_shape != "unknown":
        return contract_shape
    normalized_side = str(question_side or "").lower()
    text = " ".join(
        value.lower()
        for value in (
            market_id,
            question,
            _optional_text(signal.get("market_type"), shared_candidate.get("market_type"), market.get("market_type")),
        )
        if value
    )
    if normalized_side == "range" or "between" in text:
        return "range"
    if normalized_side == "binary_bucket" or "bucket" in text or re.search(r"-b-?\d", text):
        return "bucket"
    if normalized_side in {"above", "below"} or ">" in text or "<" in text:
        return "tail"
    return None


def _compact_question_text(*values: Any) -> str | None:
    question = _optional_text(*values)
    if not question:
        return None
    compact = " ".join(question.split())
    if len(compact) > MAX_COMPACT_FUTURE_PNL_QUESTION_CHARS:
        return None
    return compact


def _candidate_city_tokens(signal: Mapping[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for key in ("city", "weather_city", "station_city", "city_id", "weather_city_id"):
        tokens.update(_city_token_set([signal.get(key)]))
    for nested_key in ("weather_context", "weather_market_context", "weather", "metadata"):
        nested = signal.get(nested_key)
        if not isinstance(nested, Mapping):
            continue
        for key in ("city", "weather_city", "station_city", "city_id", "weather_city_id"):
            tokens.update(_city_token_set([nested.get(key)]))

    try:
        from bot.weather.station_mapping import resolve_weather_station
    except Exception:  # pragma: no cover - optional enrichment only.
        return tokens

    resolved = resolve_weather_station(signal)
    tokens.update(_city_token_set([resolved.city_id, resolved.city]))
    return tokens


def _city_token_set(values: Iterable[Any]) -> set[str]:
    tokens: set[str] = set()
    for value in values or ():
        text = str(value or "").strip().lower()
        if not text:
            continue
        tokens.add(text)
        normalized = " ".join(text.replace("_", " ").replace("-", " ").split())
        if normalized:
            tokens.add(normalized)
    return tokens


def _side_from_action(action: str) -> str | None:
    text = str(action or "").upper()
    if "YES" in text:
        return "YES"
    if "NO" in text:
        return "NO"
    return None


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


def _price_number(*values: Any) -> float | int | None:
    for value in values:
        number = _number(value)
        if number is None:
            continue
        if number <= 0:
            continue
        return number
    return None


def _text(*values: Any) -> str:
    for value in values:
        if value not in (None, ""):
            return str(value)
    return ""


def _optional_text(*values: Any) -> str | None:
    value = _text(*values)
    return value or None


def _first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return False


__all__ = [
    "DEFAULT_CONFIDENCE_FLOOR",
    "PAPER_LANE_DECISION_ROLE",
    "PaperShadowLaneWriteResult",
    "build_paper_shadow_lane_resolution_rows",
    "paper_shadow_lanes_enabled",
    "summarize_paper_shadow_lane_report",
    "summarize_paper_shadow_lane_resolved_pnl",
    "update_paper_shadow_lane_incremental_pnl",
    "write_paper_shadow_lane_decisions",
]
