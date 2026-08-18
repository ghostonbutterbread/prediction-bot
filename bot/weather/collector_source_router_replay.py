"""Offline source-only control versus source-router replay.

This narrow research slice consumes only sanitized collector replay inputs and
an explicit, separately supplied finalized-outcome ledger.  It creates sealed
decision artifacts first; realized outcomes and fixed-stake PnL exist only in
the separate resolution report.  It never reads legacy paper decisions, calls
the network, or changes a lane, wallet, or raw archive.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from bot.replay_decision_input import verify_replay_decision_input_record_v1
from bot.weather.source_reliability import (
    build_source_edge_evaluation_row,
    build_source_outcome_ledger_rows_for_row,
)
from bot.weather.source_observation_ledger import source_correctness_target_proof
from bot.weather.source_history_manifest import load_source_history_manifest
from bot.weather.source_correctness_cohorts import DEFAULT_PER_SHAPE_TARGET, select_source_correctness_cohort
from bot.weather.source_router import (
    BUY_NO, BUY_YES, SKIP, source_history_target_proof_rejection_key, select_source_for_candidate,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DERIVED_REPORTS_ROOT = PROJECT_ROOT / "data" / "derived_reports"
CONTROL_LANE_ID = "source_probability_control_v1"
CANDIDATE_LANE_ID = "source_router_candidate_v1"
POLICY_VERSION = "source_probability_replay_v1"
MIN_ABSOLUTE_EDGE = 0.05
FIXED_STAKE_USD = 1.0
_IDENTITY_FIELDS = (
    "shared_snapshot_id", "shared_candidate_id", "market_id", "observed_at_utc", "raw_row_sha256",
)


@dataclass(frozen=True, slots=True)
class CollectorSourceRouterReplayResult:
    output_dir: Path
    control_decisions_path: Path
    candidate_decisions_path: Path
    resolution_report_path: Path
    cohort_report_path: Path
    metadata_path: Path
    metadata: dict[str, Any]


def build_sealed_source_probability_decisions(
    replay_inputs: Iterable[Mapping[str, Any]],
    finalized_outcomes: Iterable[Mapping[str, Any]],
    *,
    history_ledger: Iterable[Mapping[str, Any]] | None = None,
    min_sample_count: int = 5,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Build matched sealed control/candidate decisions with no outcome fields.

    ``finalized_outcomes`` is deliberately not consulted here: current records
    are decision inputs only.  Candidate selection uses only separately
    supplied historical source-ledger observations whose settlement timestamp
    is strictly before the candidate decision timestamp.  Exact-identity
    outcome binding happens later in ``resolve_sealed_source_probability_decisions``.
    """
    if min_sample_count < 1:
        raise ValueError("min_sample_count must be at least 1")

    del finalized_outcomes
    records = [dict(row) for row in replay_inputs if isinstance(row, Mapping)]
    records.sort(key=lambda row: (_parse_datetime(row.get("observed_at")) or _max_datetime(), _identity_sort_key(row)))
    historical_edges, history_stats = _source_history_edge_rows(history_ledger or ())
    prepared: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    invalid_records = 0
    for record in records:
        identity = _identity(record)
        if identity is None:
            invalid_records += 1
            continue
        prepared.append((record, _source_edge_rows(record)))

    control: list[dict[str, Any]] = []
    candidate: list[dict[str, Any]] = []
    for record, current_edges in prepared:
        control.append(_control_decision(record))
        candidate.append(_candidate_decision(record, current_edges, historical_edges, min_sample_count=min_sample_count))

    stats = {
        "input_records_seen": len(records),
        "input_records_invalid_identity": invalid_records,
        "sealed_control_decisions": len(control),
        "sealed_candidate_decisions": len(candidate),
        "current_source_observations_seen": sum(len(edges) for _, edges in prepared),
        "selector_history_only": True,
        "selector_history": history_stats,
    }
    return control, candidate, stats


def resolve_sealed_source_probability_decisions(
    decisions: Iterable[Mapping[str, Any]], finalized_outcomes: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Join sealed decisions to explicit outcomes by the complete input identity."""
    outcome_index, outcome_stats = _strict_outcome_index(finalized_outcomes)
    resolved: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    source_correctness_observations: list[dict[str, Any]] = []
    for decision in decisions:
        if not isinstance(decision, Mapping):
            continue
        identity = _identity(decision)
        outcome = outcome_index.get(identity) if identity is not None else None
        base = {
            "decision_id": decision.get("decision_id"),
            "lane_id": decision.get("lane_id"),
            "policy_version": decision.get("policy_version"),
            "market_id": decision.get("market_id"),
            "canonical_input_sha256": decision.get("canonical_input_sha256"),
            "decision_key": decision.get("decision_key"),
            "action": decision.get("action"),
            "side": decision.get("side"),
            "entry_price": decision.get("entry_price"),
            "price_basis": decision.get("price_basis"),
            "action_label": decision.get("action_label"),
            "fixed_stake_usd": decision.get("fixed_stake_usd"),
        }
        if outcome is None:
            unresolved.append({**base, "resolution_status": "unresolved_exact_identity_required"})
            continue
        official_outcome = outcome["official_outcome"]
        source_correctness_observation = _source_selection_correctness_observation(
            decision, official_outcome,
        )
        if source_correctness_observation is not None:
            source_correctness_observations.append(source_correctness_observation)
        side = decision.get("side")
        price = _finite_number(decision.get("entry_price"))
        stake = _finite_number(decision.get("fixed_stake_usd"))
        action = decision.get("action")
        pnl_category = _pnl_category(action, side, price, stake, decision.get("price_basis"))
        calculable = pnl_category is not None
        won = side == official_outcome if calculable else None
        pnl = round(stake * ((1.0 - price) if won else -price), 6) if calculable and won is not None else None
        resolved.append({
            **base,
            "resolution_status": "resolved_exact_identity",
            "official_outcome": official_outcome,
            "settlement_ts": outcome["settlement_ts"],
            "resolution_id": outcome.get("resolution_id"),
            "won": won,
            "pnl_category": pnl_category,
            "executable_pnl_usd": pnl if pnl_category == "executable_pnl" else None,
            "reference_price_proxy_pnl_usd": pnl if pnl_category == "reference_price_proxy_pnl" else None,
        })

    executable_pnl_rows = [row for row in resolved if row.get("executable_pnl_usd") is not None]
    reference_price_proxy_rows = [row for row in resolved if row.get("reference_price_proxy_pnl_usd") is not None]
    return {
        "schema_name": "source_probability_resolution_report",
        "schema_version": 3,
        "research_status": "offline_source_only_research",
        "non_mutating": True,
        "promotion_requirement": "forward_paper_required_before_promotion",
        "resolution_join": "exact_decision_key_and_canonical_input_sha256",
        "fixed_stake_policy": {"fixed_stake_usd": FIXED_STAKE_USD, "fee_model_version": "none_v1"},
        "pnl_semantics": {
            "executable_pnl": "only decisions priced from a recorded positive order-book ask",
            "reference_price_proxy_pnl": "diagnostic proxy only; never an executable fill or realizable P&L",
        },
        "summary": {
            "sealed_decisions_seen": len(resolved) + len(unresolved),
            "exactly_resolved_decisions": len(resolved),
            "unresolved_decisions": len(unresolved),
            "executable_pnl_decisions": len(executable_pnl_rows),
            "executable_pnl_total_usd": round(sum(float(row["executable_pnl_usd"]) for row in executable_pnl_rows), 6),
            "reference_price_proxy_pnl_decisions": len(reference_price_proxy_rows),
            "reference_price_proxy_pnl_total_usd": round(
                sum(float(row["reference_price_proxy_pnl_usd"]) for row in reference_price_proxy_rows), 6,
            ),
            "outcome_ledger": outcome_stats,
        },
        "source_correctness": _source_selection_correctness_report(source_correctness_observations),
        "resolved_decisions": resolved,
        "unresolved_decisions": unresolved,
    }


def _source_selection_correctness_observation(
    decision: Mapping[str, Any], official_outcome: str,
) -> dict[str, Any] | None:
    """Return a post-resolution source-selection observation for one candidate.

    A selected source represents a routeable router choice in the sealed
    candidate artifact.  Whether that choice later qualified for a trade is
    intentionally irrelevant here.
    """
    if decision.get("lane_id") != CANDIDATE_LANE_ID:
        return None
    raw_source_id = decision.get("selected_source_id")
    if not isinstance(raw_source_id, str) or not raw_source_id.strip():
        return None
    source_id = raw_source_id.strip()
    raw_source_name = decision.get("selected_source_name")
    source_name = raw_source_name.strip() if isinstance(raw_source_name, str) and raw_source_name.strip() else source_id
    side = decision.get("side")
    if side not in {"YES", "NO"}:
        correctness = "unavailable"
    else:
        correctness = "correct" if side == official_outcome else "incorrect"
    return {
        "decision_id": decision.get("decision_id"),
        "market_id": decision.get("market_id"),
        "canonical_input_sha256": decision.get("canonical_input_sha256"),
        "decision_key": dict(decision["decision_key"]) if isinstance(decision.get("decision_key"), Mapping) else None,
        "selected_source_id": source_id,
        "selected_source_name": source_name,
        "source_implied_side": side,
        "official_outcome": official_outcome,
        "action": decision.get("action"),
        "source_selection_correctness": correctness,
    }


def _source_selection_correctness_report(observations: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate exact-resolution source-selection correctness only.

    This diagnostic deliberately ignores action, price, edge, price basis, and
    stake.  ``unavailable`` records a selected source without a valid source
    side; it is neither correct nor incorrect and is excluded from the rate.
    """
    materialized = [dict(row) for row in observations if isinstance(row, Mapping)]
    totals: Counter[str] = Counter(row.get("source_selection_correctness") for row in materialized)
    per_source: dict[str, Counter[str]] = {}
    source_names: dict[str, str] = {}
    for row in materialized:
        source_id = str(row["selected_source_id"])
        source_name = str(row["selected_source_name"])
        counts = per_source.setdefault(source_id, Counter())
        source_names[source_id] = min(source_names.get(source_id, source_name), source_name)
        counts["total"] += 1
        counts[str(row.get("source_selection_correctness"))] += 1

    def _rate(counts: Mapping[str, int]) -> float | None:
        comparable = counts.get("correct", 0) + counts.get("incorrect", 0)
        return _round(counts.get("correct", 0) / comparable) if comparable else None

    per_source_counts = [
        {
            "selected_source_id": source_id,
            "selected_source_name": source_names[source_id],
            "total_routeable_resolved_selected_source_observations": counts["total"],
            "correct": counts["correct"],
            "incorrect": counts["incorrect"],
            "unavailable": counts["unavailable"],
            "correctness_rate": _rate(counts),
        }
        for source_id, counts in sorted(per_source.items())
    ]
    comparable = totals["correct"] + totals["incorrect"]
    return {
        "diagnostic_type": "source_selection_correctness",
        "semantics": (
            "selected source implied-side agreement with an independently resolved official outcome; "
            "not P&L, trade accuracy, or promotion proof"
        ),
        "eligibility": (
            "exactly resolved candidate decisions with a selected source; action, price, edge threshold, "
            "price basis, reference/executable status, and stake are ignored"
        ),
        "unavailable_semantics": "selected source has no valid source-implied YES/NO side; excluded from correctness rate",
        "total_routeable_resolved_selected_source_observations": len(materialized),
        "correct": totals["correct"],
        "incorrect": totals["incorrect"],
        "unavailable": totals["unavailable"],
        "correctness_rate": _round(totals["correct"] / comparable) if comparable else None,
        "per_source": per_source_counts,
        "observations": materialized,
    }


def run_collector_source_router_replay(
    *,
    replay_inputs_path: str | Path,
    finalized_outcomes_path: str | Path,
    output_dir: str | Path,
    min_sample_count: int = 5,
    max_rows: int | None = None,
    history_manifest_path: str | Path | None = None,
    history_ledger_path: str | Path | None = None,
    cohort_per_shape_target: int = DEFAULT_PER_SHAPE_TARGET,
) -> CollectorSourceRouterReplayResult:
    """Run the complete offline slice and write only new derived artifacts."""
    if max_rows is not None and max_rows < 0:
        raise ValueError("max_rows must be non-negative")
    if cohort_per_shape_target < 1:
        raise ValueError("cohort_per_shape_target must be at least 1")
    if history_ledger_path is not None and history_manifest_path is None:
        raise ValueError("history manifest is required when providing selector history")
    replay_path = Path(replay_inputs_path).resolve()
    outcomes_path = Path(finalized_outcomes_path).resolve()
    if not replay_path.is_file() or not outcomes_path.is_file():
        raise ValueError("replay inputs and finalized outcomes must be readable files")
    records = _read_jsonl(replay_path, max_rows=max_rows)
    invalid_hash_records = sum(not verify_replay_decision_input_record_v1(row) for row in records)
    if invalid_hash_records:
        raise ValueError(f"replay input canonical hash verification failed for {invalid_hash_records} record(s)")
    history_ledger, history_provenance = _load_selector_history(
        history_manifest_path=history_manifest_path, history_ledger_path=history_ledger_path,
    )
    target_dir = _prepare_output_dir(output_dir)
    outcomes = _read_jsonl(outcomes_path)
    control, candidate, decision_stats = build_sealed_source_probability_decisions(
        records, outcomes, history_ledger=history_ledger, min_sample_count=min_sample_count,
    )
    report = resolve_sealed_source_probability_decisions(control + candidate, outcomes)
    report["source_correctness"]["historical_source_quality"] = {
        "interpretation": decision_stats["selector_history"]["source_quality_interpretation"],
        "target_proof_rejections": {
            key: decision_stats["selector_history"].get(key, 0)
            for key in (
                "source_history_rows_rejected_missing_exact_target_proof_marker",
                "source_history_rows_rejected_target_mismatch",
                "source_history_rows_rejected_target_unproven",
                "source_history_rows_rejected_v1_forecast_not_scoreable",
            )
        },
    }
    cohort_report = select_source_correctness_cohort(
        candidate, report["source_correctness"]["observations"], per_shape_target=cohort_per_shape_target,
    )

    control_path = target_dir / "sealed_control_decisions.jsonl"
    candidate_path = target_dir / "sealed_candidate_decisions.jsonl"
    report_path = target_dir / "post_decision_resolution_report.json"
    cohort_path = target_dir / "source_correctness_shape_cohort_report.json"
    metadata_path = target_dir / "run_metadata.json"
    control_bytes = _jsonl_bytes(control)
    candidate_bytes = _jsonl_bytes(candidate)
    report_bytes = _canonical_json_bytes(report)
    cohort_bytes = _canonical_json_bytes(cohort_report)
    control_path.write_bytes(control_bytes)
    candidate_path.write_bytes(candidate_bytes)
    report_path.write_bytes(report_bytes)
    cohort_path.write_bytes(cohort_bytes)
    metadata = {
        "schema_name": "collector_source_router_replay_run",
        "schema_version": 3,
        "research_status": "offline_source_only_research",
        "offline": True,
        "network_access": False,
        "non_mutating": True,
        "live_or_paper_lane_enabled": False,
        "control_lane_id": CONTROL_LANE_ID,
        "candidate_lane_id": CANDIDATE_LANE_ID,
        "policy_version": POLICY_VERSION,
        "decision_policy": {
            "control": "weather_source_snapshot.predicted_prob; buy only when side_probability minus recorded decision-time price is at least 0.05",
            "candidate": "existing source_router selection; selected source prior win rate versus recorded decision-time same-side price; prior settlement strictly before decision timestamp",
            "price_basis": "positive order-book asks are recorded_executable_ask; yes_price/no_price fallback is recorded_market_reference and reference_price_diagnostic only",
            "promotion_requirement": "downstream forward paper evidence is required before any promotion",
            "fixed_stake_usd": FIXED_STAKE_USD,
            "selector_history_only": True,
        },
        "inputs": {
            "replay_inputs_path": str(replay_path), "replay_inputs_sha256": _sha256_file(replay_path),
            "finalized_outcomes_path": str(outcomes_path), "finalized_outcomes_sha256": _sha256_file(outcomes_path),
            "max_rows": max_rows,
            "selector_history": history_provenance,
        },
        "decision_stats": decision_stats,
        "resolution_summary": report["summary"],
        "source_correctness_cohort_summary": {
            "per_shape": [
                {
                    "contract_shape": row["contract_shape"],
                    "available_count": row["available_count"],
                    "selected_count": row["selected_count"],
                    "shortfall": row["shortfall"],
                }
                for row in cohort_report["per_shape"]
            ],
            "selected_global_unique_event_count": cohort_report["aggregate"]["selected_global_unique_event_count"],
            "cross_shape_shared_event_warning": cohort_report["aggregate"]["cross_shape_shared_event_warning"],
        },
        "output_artifacts": {
            control_path.name: {"sha256": hashlib.sha256(control_bytes).hexdigest(), "record_count": len(control)},
            candidate_path.name: {"sha256": hashlib.sha256(candidate_bytes).hexdigest(), "record_count": len(candidate)},
            report_path.name: {"sha256": hashlib.sha256(report_bytes).hexdigest()},
            cohort_path.name: {"sha256": hashlib.sha256(cohort_bytes).hexdigest()},
        },
    }
    metadata_path.write_bytes(_canonical_json_bytes(metadata))
    return CollectorSourceRouterReplayResult(
        output_dir=target_dir,
        control_decisions_path=control_path,
        candidate_decisions_path=candidate_path,
        resolution_report_path=report_path,
        cohort_report_path=cohort_path,
        metadata_path=metadata_path,
        metadata=metadata,
    )


def _control_decision(record: Mapping[str, Any]) -> dict[str, Any]:
    probability = _snapshot_probability(record)
    side = "YES" if probability is not None and probability >= 0.5 else ("NO" if probability is not None else None)
    price, price_basis = _select_price(record, side)
    action, reason = _threshold_action(probability, side, price)
    return _sealed_decision(
        record, lane_id=CONTROL_LANE_ID, probability=probability, side=side, action=action,
        entry_price=price if action != SKIP else None, price_basis=price_basis, skip_reason=reason,
        extra={"probability_basis": "weather_source_snapshot.predicted_prob"},
    )


def _candidate_decision(
    record: Mapping[str, Any], current_edges: list[dict[str, Any]], history_edges: list[dict[str, Any]], *, min_sample_count: int,
) -> dict[str, Any]:
    # Use the source-observation adapter's own slice classification for both
    # current candidate and history.  This is the same shape passed to the
    # existing router, avoiding a second, potentially divergent classifier.
    candidate_context = current_edges[0] if current_edges else _candidate_context(record)
    selector = select_source_for_candidate(
        candidate_context, history_edges, min_sample_count=min_sample_count, history_cutoff=record.get("observed_at"),
    )
    if not selector.get("routeable"):
        return _sealed_decision(
            record, lane_id=CANDIDATE_LANE_ID, probability=None, side=None, action=SKIP, entry_price=None,
            price_basis=None, skip_reason=(selector.get("blockers") or ["insufficient_prior_history"])[0],
            extra={"selected_source_id": None, "prior_sample_count": selector.get("prior_sample_count", 0), "router_selection_mode": selector.get("selection_mode")},
        )
    selected_id = str(selector.get("chosen_source_id") or "")
    current = next((edge for edge in current_edges if str(edge.get("source_id") or "") == selected_id), None)
    side = current.get("source_implied_side") if current else None
    probability = _finite_number(selector.get("prior_win_rate"))
    price, price_basis = _select_price(record, side)
    action, reason = _threshold_action(probability, side, price)
    if current is None:
        action, reason = SKIP, "chosen_source_missing_current_observation"
    return _sealed_decision(
        record, lane_id=CANDIDATE_LANE_ID, probability=probability, side=side, action=action,
        entry_price=price if action != SKIP else None, price_basis=price_basis, skip_reason=reason,
        extra={
            "selected_source_id": selector.get("chosen_source_id"),
            "selected_source_name": selector.get("chosen_source_name"),
            "prior_sample_count": selector.get("prior_sample_count"),
            "router_selection_mode": selector.get("selection_mode"),
        },
    )


def _sealed_decision(
    record: Mapping[str, Any], *, lane_id: str, probability: float | None, side: str | None, action: str,
    entry_price: float | None, price_basis: str | None, skip_reason: str | None, extra: Mapping[str, Any],
) -> dict[str, Any]:
    identity = _identity(record)
    if identity is None:  # protected by the caller; retains a clear local invariant.
        raise ValueError("sealed decision requires a complete sanitized input identity")
    row = {
        "schema_name": "sealed_source_probability_decision",
        "schema_version": 3,
        "sealed": True,
        "research_status": "offline_source_only_research",
        "lane_id": lane_id,
        "policy_version": POLICY_VERSION,
        "canonical_input_sha256": record["canonical_input_sha256"],
        "decision_key": dict(record["decision_key"]),
        "market_id": record["market_id"],
        "decision_timestamp": record.get("observed_at"),
        **_decision_time_market_traits(record),
        "probability": _round(probability),
        "side": side,
        "action": action,
        "entry_price": _round(entry_price),
        "price_basis": price_basis,
        "action_label": "reference_price_diagnostic" if action != SKIP and price_basis == "recorded_market_reference" else None,
        "fixed_stake_usd": FIXED_STAKE_USD,
        "skip_reason": skip_reason if action == SKIP else None,
        **dict(extra),
    }
    row["decision_id"] = "sha256:" + hashlib.sha256(_canonical_json_bytes(row)).hexdigest()
    return row


def _decision_time_market_traits(record: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only reviewed decision-time market traits into sealed artifacts."""
    market = record.get("market") if isinstance(record.get("market"), Mapping) else {}
    metadata = market.get("market_metadata") if isinstance(market.get("market_metadata"), Mapping) else {}
    source_inputs = record.get("source_inputs") if isinstance(record.get("source_inputs"), Mapping) else {}
    source_context = source_inputs.get("source_context") if isinstance(source_inputs.get("source_context"), Mapping) else {}
    source_data = source_context.get("data") if isinstance(source_context.get("data"), Mapping) else {}
    source_metadata = source_data.get("market_metadata") if isinstance(source_data.get("market_metadata"), Mapping) else {}
    route = _mapping(metadata.get("market_route")) or _mapping(source_metadata.get("market_route")) or _mapping(source_data.get("market_route"))
    evidence = _mapping(route.get("evidence"))
    snapshot = _weather_snapshot(record) or {}
    forecast = _mapping(snapshot.get("forecast"))
    signal = _mapping(snapshot.get("source_signal"))
    station = _mapping(snapshot.get("station_resolution"))
    event_ticker = _first_text(metadata.get("event_ticker"), source_metadata.get("event_ticker"), route.get("event_ticker"), evidence.get("event_ticker"))
    contract_shape = _first_text(metadata.get("contract_shape"), source_metadata.get("contract_shape"), route.get("contract_shape"), evidence.get("shape")) or "unknown"
    subcategory = _first_text(metadata.get("subcategory"), source_metadata.get("subcategory"), route.get("subcategory"))
    return {
        "event_ticker": event_ticker,
        "unit_id": event_ticker or record["market_id"],
        "independence_quality": "event_ticker" if event_ticker is not None else "contract_only",
        "city_id": _first_text(metadata.get("city_id"), source_metadata.get("city_id"), route.get("city_id"), station.get("city_id")) or "unknown",
        "market_kind": _first_text(metadata.get("market_kind"), source_metadata.get("market_kind"), route.get("market_kind")) or "unknown",
        "contract_shape": contract_shape,
        "subcategory": subcategory,
        "question_side": _first_text(forecast.get("question_side"), signal.get("question_side"), metadata.get("question_side"), source_metadata.get("question_side")) or "unknown",
    }


def _source_edge_rows(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = _source_adapter(record)
    if adapter is None:
        return []
    rows = build_source_outcome_ledger_rows_for_row(adapter)
    snapshot = _weather_snapshot(record) or {}
    source_records = {
        str(source.get("source_id") or source.get("source_name") or "").strip().lower(): source
        for source in snapshot.get("sources", []) if isinstance(source, Mapping)
    }
    edges: list[dict[str, Any]] = []
    for row in rows:
        source = source_records.get(str(row.get("source_id") or "").strip().lower(), {})
        proof = source_correctness_target_proof(
            source_record=source, snapshot=snapshot, market_date=row.get("market_date"),
        )
        row["source_correctness_eligibility"] = proof["status"]
        row["source_target_proof"] = proof
        edges.append(build_source_edge_evaluation_row(row))
    return edges


def _source_history_edge_rows(rows: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Create selector-only edges from pre-existing settled source history."""
    stats: Counter[str] = Counter()
    edges: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            stats["invalid_rows"] += 1
            continue
        stats["rows_seen"] += 1
        rejection_key = source_history_target_proof_rejection_key(row)
        if rejection_key is not None:
            stats["source_" + rejection_key] += 1
            continue
        if row.get("eligible_for_reliability") is not True:
            stats["ineligible_for_reliability"] += 1
            continue
        settlement = _parse_datetime(row.get("settlement_ts"))
        outcome = str(row.get("actual_outcome") or row.get("official_outcome") or "").upper()
        if settlement is None:
            stats["missing_authoritative_settlement_ts"] += 1
            continue
        if outcome not in {"YES", "NO"}:
            stats["missing_authoritative_outcome"] += 1
            continue
        historical = dict(row)
        historical["official_outcome"] = outcome
        historical["settlement_ts"] = _timestamp(settlement)
        edge = build_source_edge_evaluation_row(historical)
        # The source ledger's hash-verified settlement timestamp is the sole
        # availability time for this selector-only history.
        edge["settlement_ts"] = historical["settlement_ts"]
        edge["outcome_known_at"] = historical["settlement_ts"]
        if edge.get("eligible_for_edge_validation") is not True:
            stats["ineligible_for_selector"] += 1
            continue
        edges.append(edge)
        stats["selector_edges_accepted"] += 1
    stats["source_quality_interpretation"] = (
        "quarantined_rows_without_exact_target_proof"
        if any(stats[key] for key in (
            "source_history_rows_rejected_missing_exact_target_proof_marker",
            "source_history_rows_rejected_target_mismatch",
            "source_history_rows_rejected_target_unproven",
            "source_history_rows_rejected_v1_forecast_not_scoreable",
        ))
        else "exact_target_proof_only"
    )
    return edges, dict(stats)


def _load_selector_history(
    *, history_manifest_path: str | Path | None, history_ledger_path: str | Path | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load optional selector-only history, validating a manifest before writes."""
    manifest = load_source_history_manifest(history_manifest_path) if history_manifest_path is not None else None
    manifest_ledger = manifest.source_ledger_path if manifest is not None else None
    explicit_ledger = Path(history_ledger_path).expanduser().resolve() if history_ledger_path is not None else None
    if manifest_ledger is not None and explicit_ledger is not None and manifest_ledger != explicit_ledger:
        raise ValueError("history_ledger_path must match the hash-verified manifest source_ledger_path")
    ledger_path = manifest_ledger or explicit_ledger
    if ledger_path is None:
        return [], {
            "selector_history_only": True,
            "provided": False,
            "rows_loaded": 0,
            "verification": "not_provided",
        }
    if not ledger_path.is_file():
        raise ValueError("history ledger must be a readable file")
    ledger = _read_jsonl(ledger_path)
    provenance: dict[str, Any] = {
        "selector_history_only": True,
        "provided": True,
        "history_ledger_path": str(ledger_path),
        "history_ledger_sha256": _sha256_file(ledger_path),
        "rows_loaded": len(ledger),
        "verification": "manifest_sha256_verified" if manifest is not None else "direct_path_sha256_recorded",
    }
    if manifest is not None:
        strict_resolutions = _read_jsonl(manifest.strict_resolution_path)
        resolution_index, resolution_stats = _strict_history_resolution_index(strict_resolutions)
        joined_ledger: list[dict[str, Any]] = []
        for row in ledger:
            market_id = row.get("market_id")
            resolution = resolution_index.get(str(market_id)) if isinstance(market_id, str) and market_id else None
            if resolution is None:
                resolution_stats["source_history_rows_without_exact_authoritative_resolution"] += 1
                continue
            # The historical source row supplies the forecast observation only.
            # Its resolved outcome and availability are independently supplied
            # by the manifest's verified strict-resolution artifact.
            joined_ledger.append({
                **row,
                "official_outcome": resolution["official_outcome"],
                "settlement_ts": resolution["settlement_ts"],
                "resolution_id": resolution.get("resolution_id"),
            })
            resolution_stats["source_history_rows_with_exact_authoritative_resolution"] += 1
        ledger = joined_ledger
        provenance.update({
            "history_manifest_path": str(manifest.manifest_path),
            "history_manifest_source_ledger_sha256": manifest.sha256["source_ledger"],
            "strict_resolution_path": str(manifest.strict_resolution_path),
            "strict_resolution_sha256": manifest.sha256["strict_resolution"],
            "manifest_historical_counterfactual_only": manifest.historical_counterfactual_only,
            "manifest_non_mutating": manifest.non_mutating,
            "strict_resolution_join": "exact_market_id_required",
            "strict_resolution_join_counts": dict(resolution_stats),
        })
    return ledger, provenance


def _strict_history_resolution_index(rows: Iterable[Mapping[str, Any]]) -> tuple[dict[str, dict[str, Any]], Counter[str]]:
    """Index only unambiguous finalized strict resolutions for history joins."""
    index: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    stats: Counter[str] = Counter()
    for row in rows:
        if not isinstance(row, Mapping):
            stats["invalid_rows"] += 1
            continue
        market_id = row.get("market_id")
        resolution = row.get("resolution") if isinstance(row.get("resolution"), Mapping) else {}
        outcome = str(row.get("official_outcome") or resolution.get("official_outcome") or resolution.get("outcome") or row.get("kalshi_result") or "").upper()
        settlement = _parse_datetime(row.get("settlement_ts"))
        if not isinstance(market_id, str) or not market_id or str(row.get("market_status") or "").lower() != "finalized" or outcome not in {"YES", "NO"} or settlement is None:
            stats["invalid_rows"] += 1
            continue
        if market_id in index:
            duplicates.add(market_id)
            stats["duplicate_market_id_rows"] += 1
            continue
        index[market_id] = {
            "official_outcome": outcome,
            "settlement_ts": _timestamp(settlement),
            "resolution_id": row.get("resolution_id"),
        }
        stats["accepted_rows"] += 1
    for market_id in duplicates:
        index.pop(market_id, None)
    stats["ambiguous_market_ids"] = len(duplicates)
    return index, stats


def _source_adapter(record: Mapping[str, Any]) -> dict[str, Any] | None:
    snapshot = _weather_snapshot(record)
    if snapshot is None:
        return None
    market = record.get("market") if isinstance(record.get("market"), Mapping) else {}
    metadata = market.get("market_metadata") if isinstance(market.get("market_metadata"), Mapping) else {}
    forecast = snapshot.get("forecast") if isinstance(snapshot.get("forecast"), Mapping) else {}
    return {
        "market_id": record.get("market_id"), "shared_candidate_id": record.get("shared_candidate_id"),
        "question": market.get("question"), "observed_at": record.get("observed_at"),
        "best_yes_ask": market.get("best_yes_ask"), "best_no_ask": market.get("best_no_ask"),
        "weather_source_snapshot": snapshot,
        "market": {
            "market_id": record.get("market_id"), "question": market.get("question"),
            "city_id": metadata.get("city_id"), "market_kind": metadata.get("market_kind"),
            "contract_shape": metadata.get("contract_shape"), "question_side": forecast.get("question_side"),
            "threshold": forecast.get("threshold"), "market_date": snapshot.get("market_date"),
        },
    }


def _candidate_context(record: Mapping[str, Any]) -> dict[str, Any]:
    adapter = _source_adapter(record) or {}
    market = adapter.get("market") if isinstance(adapter.get("market"), Mapping) else {}
    return {
        "city_id": market.get("city_id") or "unknown", "market_kind": market.get("market_kind") or "unknown",
        "contract_shape": market.get("contract_shape") or "unknown", "question_side": market.get("question_side") or "unknown",
        "observed_at": record.get("observed_at"),
    }


def _threshold_action(probability: float | None, side: str | None, price: float | None) -> tuple[str, str | None]:
    if probability is None:
        return SKIP, "missing_probability"
    if side not in {"YES", "NO"}:
        return SKIP, "missing_source_implied_side"
    if price is None:
        return SKIP, "missing_decision_price"
    if probability - price < MIN_ABSOLUTE_EDGE:
        return SKIP, "below_source_probability_edge_threshold"
    return (BUY_YES if side == "YES" else BUY_NO), None


def _snapshot_probability(record: Mapping[str, Any]) -> float | None:
    snapshot = _weather_snapshot(record)
    if snapshot is None:
        return None
    signal = snapshot.get("source_signal") if isinstance(snapshot.get("source_signal"), Mapping) else {}
    return _probability(snapshot.get("predicted_prob"), signal.get("predicted_prob"))


def _weather_snapshot(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    source_inputs = record.get("source_inputs") if isinstance(record.get("source_inputs"), Mapping) else {}
    context = source_inputs.get("source_context") if isinstance(source_inputs.get("source_context"), Mapping) else {}
    data = context.get("data") if isinstance(context.get("data"), Mapping) else {}
    snapshot = data.get("weather_source_snapshot")
    return snapshot if isinstance(snapshot, Mapping) else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _select_price(record: Mapping[str, Any], side: str | None) -> tuple[float | None, str | None]:
    market = record.get("market") if isinstance(record.get("market"), Mapping) else {}
    execution = market.get("execution_snapshot") if isinstance(market.get("execution_snapshot"), Mapping) else {}
    if side == "YES":
        executable_ask = _price(execution.get("best_yes_ask"), market.get("best_yes_ask"))
        reference_price = _price(market.get("yes_price"))
    if side == "NO":
        executable_ask = _price(execution.get("best_no_ask"), market.get("best_no_ask"))
        reference_price = _price(market.get("no_price"))
    if side not in {"YES", "NO"}:
        return None, None
    if executable_ask is not None:
        return executable_ask, "recorded_executable_ask"
    if reference_price is not None:
        return reference_price, "recorded_market_reference"
    return None, None


def _pnl_category(
    action: Any, side: Any, price: float | None, stake: float | None, price_basis: Any,
) -> str | None:
    if action not in {BUY_YES, BUY_NO} or side not in {"YES", "NO"} or price is None or stake is None:
        return None
    if price_basis == "recorded_executable_ask":
        return "executable_pnl"
    if price_basis == "recorded_market_reference":
        return "reference_price_proxy_pnl"
    return None


def _strict_outcome_index(rows: Iterable[Mapping[str, Any]]) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, int]]:
    index: dict[tuple[str, str], dict[str, Any]] = {}
    duplicates: set[tuple[str, str]] = set()
    stats: Counter[str] = Counter()
    for row in rows:
        if not isinstance(row, Mapping):
            stats["invalid_rows"] += 1
            continue
        identity = _identity(row)
        outcome = str(row.get("official_outcome") or "").upper()
        settled = _parse_datetime(row.get("settlement_ts"))
        if identity is None or outcome not in {"YES", "NO"} or settled is None or str(row.get("market_status") or "").lower() != "finalized":
            stats["invalid_rows"] += 1
            continue
        if identity in index:
            duplicates.add(identity)
            stats["duplicate_exact_identity_rows"] += 1
            continue
        index[identity] = {
            "official_outcome": outcome, "settlement_ts": _timestamp(settled), "resolution_id": row.get("resolution_id"),
        }
        stats["accepted_rows"] += 1
    for identity in duplicates:
        index.pop(identity, None)
    stats["ambiguous_exact_identity"] = len(duplicates)
    return index, dict(stats)


def _identity(row: Mapping[str, Any]) -> tuple[str, str] | None:
    digest = row.get("canonical_input_sha256")
    key = row.get("decision_key")
    if not isinstance(digest, str) or len(digest) != 64 or not isinstance(key, Mapping):
        return None
    extracted = {field: key.get(field) for field in _IDENTITY_FIELDS}
    if any(not isinstance(value, str) or not value for value in extracted.values()):
        return None
    if extracted["market_id"] != row.get("market_id"):
        return None
    return digest.lower(), json.dumps(extracted, sort_keys=True, separators=(",", ":"))


def _prepare_output_dir(value: str | Path) -> Path:
    target = Path(value).resolve()
    root = DERIVED_REPORTS_ROOT.resolve()
    if target == root or root not in target.parents:
        raise ValueError(f"output directory must be under {root}")
    if target.exists():
        if not target.is_dir() or any(target.iterdir()):
            raise ValueError("output directory must be new or empty; refusing to overwrite artifacts")
    else:
        target.mkdir(parents=True, exist_ok=False)
    return target


def _read_jsonl(path: Path, *, max_rows: int | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if max_rows is not None and len(rows) >= max_rows:
                break
            if not line.strip():
                continue
            payload = json.loads(line)
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


def _probability(*values: Any) -> float | None:
    for value in values:
        parsed = _finite_number(value)
        if parsed is not None and 0.0 <= parsed <= 1.0:
            return parsed
    return None


def _price(*values: Any) -> float | None:
    for value in values:
        parsed = _finite_number(value)
        if parsed is not None and 0.0 < parsed < 1.0:
            return parsed
    return None


def _finite_number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _max_datetime() -> datetime:
    return datetime.max.replace(tzinfo=timezone.utc)


def _identity_sort_key(row: Mapping[str, Any]) -> str:
    identity = _identity(row)
    return "|".join(identity) if identity else ""


def _round(value: float | None) -> float | None:
    return round(value, 6) if value is not None else None


def _canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(_canonical_json_bytes(row) for row in rows)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "CANDIDATE_LANE_ID", "CONTROL_LANE_ID", "FIXED_STAKE_USD",
    "build_sealed_source_probability_decisions", "resolve_sealed_source_probability_decisions",
    "run_collector_source_router_replay",
]
