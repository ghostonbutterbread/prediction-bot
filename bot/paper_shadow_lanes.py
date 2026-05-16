"""Lightweight paper-only decision lanes over shared candidates."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from bot.agent_decision_ledger import (
    build_agent_decision_id,
    build_agent_run_id,
    validate_agent_decision_row,
)
from bot.file_ops import append_jsonl, load_jsonl
from bot.paper_wallets import BETA_PAPER_WALLET_ID, STABLE_PAPER_WALLET_ID

PAPER_LANE_AGENT_ID = "paper"
PAPER_LANE_RUNTIME = "paper"
PAPER_LANE_DECISION_ROLE = "paper_lane"
PAPER_LANE_SCHEMA_VERSION = 1
DEFAULT_CONFIDENCE_FLOOR = 0.58
DEFAULT_LANE_IDS = ("control_stable", "shadow_current_beta", "shadow_confidence_floor")
PREMIUM_CITY_LANE_ID = "shadow_premium_city"
SOURCE_RELIABILITY_LANE_ID = "shadow_source_reliability"
KNOWN_LANE_IDS = (*DEFAULT_LANE_IDS, PREMIUM_CITY_LANE_ID, SOURCE_RELIABILITY_LANE_ID)
REPO_ROOT = Path(__file__).resolve().parent.parent

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
        "entry_price": _number(signal.get("market_price"), (source_row or {}).get("entry_price")),
        "price": _number(signal.get("market_price"), (source_row or {}).get("price"), (source_row or {}).get("entry_price")),
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
    if lane.definition_path:
        row["lane_definition_path"] = lane.definition_path
        row["provenance"]["lane_definition_path"] = lane.definition_path
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
    elif lane.lane_id == SOURCE_RELIABILITY_LANE_ID:
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


LANE_EVALUATORS = {
    "passthrough": _passthrough_decision,
    "confidence_floor": _confidence_floor_decision,
    "premium_city": _premium_city_decision,
    "source_reliability": _source_reliability_decision,
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
        reliability_cfg = raw_by_id.get(SOURCE_RELIABILITY_LANE_ID, {"id": SOURCE_RELIABILITY_LANE_ID})
        parameters = _mapping(reliability_cfg.get("parameters"))
        parameters["scoreboard_path"] = config.get(key)
        reliability_cfg["parameters"] = parameters
        raw_by_id[SOURCE_RELIABILITY_LANE_ID] = reliability_cfg
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
    return _text(row.get("policy"), row.get("selected_lane"), _mapping(row.get("provenance")).get("lane_id"), "unknown")


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
    "paper_shadow_lanes_enabled",
    "summarize_paper_shadow_lane_report",
    "write_paper_shadow_lane_decisions",
]
