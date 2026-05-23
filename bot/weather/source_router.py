"""Replay-first source router for weather decisions.

This module is intentionally pure and report-only. It starts from the current
shared market candidate/source row, uses only prior/as-of resolved
source-scoreboard history to pick the best weather source for the candidate
slice, and writes hypothetical stable-sized comparisons against the stable
baseline. It does not mutate paper/live accounting.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha1
from typing import Any, Iterable, Mapping

from bot.weather.source_reliability import (
    build_source_edge_evaluation_row,
    build_source_outcome_ledger_rows_for_row,
)


SOURCE_ROUTER_SCHEMA_VERSION = 1
SOURCE_ROUTER_ROW_TYPE = "source_router_replay_decision"
SOURCE_ROUTER_SUMMARY_ROW_TYPE = "source_router_replay_summary"
DEFAULT_MIN_SAMPLE_COUNT = 5

BUY_YES = "BUY_YES"
BUY_NO = "BUY_NO"
SKIP = "SKIP"


def build_source_router_replay_rows(
    ledger_rows: Iterable[Mapping[str, Any]],
    *,
    outcome_lookup: Mapping[Any, Any] | None = None,
    min_sample_count: int = DEFAULT_MIN_SAMPLE_COUNT,
) -> list[dict[str, Any]]:
    """Build replay rows comparing stable baseline vs source-routed augmentation.

    ``ledger_rows`` should be source-outcome ledger rows containing known-at-time
    source observations and stable decision metadata. ``outcome_lookup`` is an
    explicit finalized market outcome lookup, identical in spirit to the
    scoreboard edge validator. The router never fetches network data.
    """

    lookup = outcome_lookup or {}
    paired_rows: list[dict[str, Any]] = []
    for row in ledger_rows:
        if not isinstance(row, Mapping):
            continue
        ledger = dict(row)
        edge = build_source_edge_evaluation_row(ledger, outcome_lookup=lookup)
        if ledger.get("source_router_history_only") is True:
            _allow_history_actual_outcome(edge, ledger)
        if edge.get("outcome_known_at") in (None, ""):
            edge["outcome_known_at"] = _optional_text(ledger.get("outcome_known_at"), ledger.get("known_after"))
        paired_rows.append({"ledger": ledger, "edge": edge})

    paired_rows.sort(
        key=lambda pair: (
            _parse_dt(pair["edge"].get("observed_at")) or datetime.max.replace(tzinfo=timezone.utc),
            str(pair["edge"].get("shared_candidate_id") or ""),
            str(pair["edge"].get("source_id") or ""),
        )
    )

    decisions: list[dict[str, Any]] = []
    current_group_key: tuple[str, str] | None = None
    current_group: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []

    def flush_group() -> None:
        if not current_group:
            return
        if all(pair["ledger"].get("source_router_history_only") is True for pair in current_group):
            return
        decisions.append(
            build_source_router_replay_row(
                [pair["ledger"] for pair in current_group],
                edge_rows=[pair["edge"] for pair in current_group],
                history_edge_rows=history,
                min_sample_count=min_sample_count,
            )
        )

    for pair in paired_rows:
        edge = pair["edge"]
        key = _candidate_key(edge)
        if current_group_key is None:
            current_group_key = key
        if key != current_group_key:
            flush_group()
            history.extend(pair_["edge"] for pair_ in current_group)
            current_group = []
            current_group_key = key
        current_group.append(pair)

    flush_group()
    return decisions


def build_joined_source_router_ledger_rows(
    source_rows: Iterable[Mapping[str, Any]],
    decision_rows: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build source-outcome ledger rows enriched with matched stable decisions.

    Source snapshots and stable/main decision rows intentionally live in
    separate ledgers. This helper joins them in memory so the replay can use
    source observations, stable action, stable size, and stable entry price
    without writing a permanent merged file.
    """

    index = _DecisionIndex(decision_rows)
    joined_rows: list[dict[str, Any]] = []
    stats: Counter[str] = Counter()
    for source_row in source_rows:
        if not isinstance(source_row, Mapping):
            stats["invalid_source_rows"] += 1
            continue
        stats["source_rows"] += 1
        decision = index.match(source_row)
        if decision is None:
            stats["source_rows_without_decision"] += 1
        else:
            stats["source_rows_with_decision"] += 1
        ledger_rows = build_source_outcome_ledger_rows_for_row(source_row)
        if not ledger_rows:
            stats["source_rows_without_observations"] += 1
        for ledger_row in ledger_rows:
            if decision is not None:
                _apply_stable_decision(ledger_row, decision)
                stats["ledger_rows_with_decision"] += 1
            else:
                stats["ledger_rows_without_decision"] += 1
            joined_rows.append(ledger_row)
            stats["ledger_rows"] += 1
    stats["decision_rows"] = index.row_count
    stats["decision_rows_indexed"] = index.indexed_count
    return joined_rows, dict(stats)


def build_source_router_replay_row(
    ledger_rows: Iterable[Mapping[str, Any]],
    *,
    edge_rows: Iterable[Mapping[str, Any]],
    history_edge_rows: Iterable[Mapping[str, Any]],
    min_sample_count: int = DEFAULT_MIN_SAMPLE_COUNT,
) -> dict[str, Any]:
    """Build one source-router replay row for a stable candidate."""

    ledgers = [dict(row) for row in ledger_rows if isinstance(row, Mapping)]
    edges = [dict(row) for row in edge_rows if isinstance(row, Mapping)]
    first_edge = edges[0] if edges else {}
    first_ledger = ledgers[0] if ledgers else {}
    observed_at = _optional_text(first_edge.get("observed_at"), first_ledger.get("observed_at"))
    history_cutoff = observed_at
    stable_action = _normalize_action(
        _first_present(
            first_ledger.get("stable_action"),
            first_ledger.get("action"),
            first_edge.get("stable_action"),
            first_edge.get("action"),
        )
    )
    stable_side = _side_from_action(stable_action)
    stable_notional = _number(
        first_ledger.get("stable_approved_position_size_usd"),
        first_ledger.get("approved_position_size_usd"),
        first_ledger.get("stable_requested_position_size_usd"),
        first_ledger.get("requested_position_size_usd"),
    )
    official_outcome = _normalized_side(first_edge.get("official_outcome"))
    stable_price = _side_price(first_ledger, stable_side)
    stable_pnl = _pnl(action=stable_action, side=stable_side, outcome=official_outcome, price=stable_price, stake=stable_notional)

    selector = select_source_for_candidate(
        first_edge,
        history_edge_rows,
        min_sample_count=min_sample_count,
        history_cutoff=history_cutoff,
    )
    current_by_source = {
        _source_key(edge.get("source_id")): edge
        for edge in edges
        if _source_key(edge.get("source_id"))
    }
    chosen_source_id = _source_key(selector.get("chosen_source_id"))
    chosen_edge = current_by_source.get(chosen_source_id) if chosen_source_id else None
    source_side = _normalized_side((chosen_edge or {}).get("source_implied_side"))
    source_supports_stable = bool(stable_side and source_side and stable_side == source_side)
    routeable = bool(selector.get("routeable")) and chosen_edge is not None
    filter_action = stable_action if routeable and source_supports_stable and stable_action in {BUY_YES, BUY_NO} else SKIP
    confirm_action = filter_action
    filter_pnl = _pnl(action=filter_action, side=stable_side, outcome=official_outcome, price=stable_price, stake=stable_notional)
    source_router_action = _action_from_side(source_side) if routeable else SKIP
    source_router_price = _source_router_price(chosen_edge, first_ledger, source_side)
    source_router_pnl = _pnl(
        action=source_router_action,
        side=source_side,
        outcome=official_outcome,
        price=source_router_price,
        stake=stable_notional,
    )

    blockers: list[str] = []
    if stable_action not in {BUY_YES, BUY_NO}:
        blockers.append("stable_baseline_not_buy")
    if not routeable:
        blockers.extend(selector.get("blockers") or ["no_route"])
    if routeable and chosen_edge is None:
        blockers.append("chosen_source_missing_current_observation")
    if routeable and source_side not in {"YES", "NO"}:
        blockers.append("missing_source_router_side")
    if source_router_price is None and source_router_action in {BUY_YES, BUY_NO}:
        blockers.append("missing_source_router_side_price")
    if stable_price is None and stable_action in {BUY_YES, BUY_NO}:
        blockers.append("missing_stable_side_price")
    if stable_notional is None and (stable_action in {BUY_YES, BUY_NO} or source_router_action in {BUY_YES, BUY_NO}):
        blockers.append("missing_comparison_size")
    if official_outcome not in {"YES", "NO"}:
        blockers.append("missing_official_outcome")

    row = {
        "schema_version": SOURCE_ROUTER_SCHEMA_VERSION,
        "row_type": SOURCE_ROUTER_ROW_TYPE,
        "non_mutating": True,
        "mode": "replay",
        "shared_candidate_id": _optional_text(first_edge.get("shared_candidate_id"), first_ledger.get("shared_candidate_id")),
        "market_id": _optional_text(first_edge.get("market_id"), first_ledger.get("market_id")),
        "observed_at": observed_at,
        "history_cutoff": history_cutoff,
        "city_id": _optional_text(first_edge.get("city_id")) or "unknown",
        "market_kind": _optional_text(first_edge.get("market_kind")) or "unknown",
        "contract_shape": _optional_text(first_edge.get("contract_shape")) or "unknown",
        "question_side": _optional_text(first_edge.get("question_side")) or "unknown",
        "stable_baseline": {
            "action": stable_action,
            "side": stable_side,
            "notional_usd": _round(stable_notional),
            "side_price": _round(stable_price),
            "reason_code": _optional_text(first_ledger.get("stable_reason_code"), first_ledger.get("reason_code")),
            "pnl": stable_pnl,
        },
        "router": {
            **selector,
            "current_source_observation_found": chosen_edge is not None,
            "source_implied_side": source_side,
            "augmentation_action": "CONFIRM" if source_supports_stable else ("BLOCK" if routeable else "NO_ROUTE"),
        },
        "comparison": {
            "agrees_with_stable": source_supports_stable if routeable else None,
            "source_router_action": source_router_action,
            "source_router_side": source_side,
            "source_router_side_price": _round(source_router_price),
            "source_router_won": source_router_pnl.get("won"),
            "source_router_pnl_usd": source_router_pnl.get("pnl_usd"),
            "source_router_minus_stable_pnl_usd": _round(
                (_number(source_router_pnl.get("pnl_usd")) or 0.0) - (_number(stable_pnl.get("pnl_usd")) or 0.0)
                if stable_pnl.get("calculable") and source_router_pnl.get("calculable")
                else None
            ),
            "source_filter_action": filter_action,
            "source_confirm_action": confirm_action,
            "would_filter_stable_buy": bool(stable_action in {BUY_YES, BUY_NO} and filter_action == SKIP),
            "stable_won": stable_pnl.get("won"),
            "source_filter_won": filter_pnl.get("won"),
            "stable_pnl_usd": stable_pnl.get("pnl_usd"),
            "source_filter_pnl_usd": filter_pnl.get("pnl_usd"),
            "source_filter_minus_stable_pnl_usd": _round(
                (_number(filter_pnl.get("pnl_usd")) or 0.0) - (_number(stable_pnl.get("pnl_usd")) or 0.0)
                if stable_pnl.get("calculable") and filter_pnl.get("calculable")
                else None
            ),
        },
        "hypothetical_execution": {
            "primary_notional_policy": "stable_sizing_for_head_to_head_replay",
            "stable_notional_usd": _round(stable_notional),
            "stable_side_price": _round(stable_price),
            "source_router_notional_usd": _round(stable_notional),
            "source_router_side_price": _round(source_router_price),
            "fee_model_version": "none_v1",
        },
        "resolution_join": {
            "official_outcome": official_outcome,
            "outcome_source": _optional_text(first_edge.get("outcome_source")),
            "outcome_known_at": _optional_text(first_edge.get("outcome_known_at")),
        },
        "blockers": sorted(set(blockers)),
    }
    row["source_router_decision_id"] = _decision_id(row)
    return row


def select_source_for_candidate(
    candidate_edge_row: Mapping[str, Any],
    history_edge_rows: Iterable[Mapping[str, Any]],
    *,
    min_sample_count: int = DEFAULT_MIN_SAMPLE_COUNT,
    history_cutoff: Any = None,
) -> dict[str, Any]:
    """Choose the best prior/as-of source for a candidate slice."""

    cutoff_dt = _parse_dt(history_cutoff or candidate_edge_row.get("observed_at"))
    slice_key = _slice_key(candidate_edge_row)
    candidates: dict[str, list[dict[str, Any]]] = {}
    for row in history_edge_rows:
        if not isinstance(row, Mapping):
            continue
        if row.get("eligible_for_edge_validation") is not True:
            continue
        known_dt = _parse_dt(row.get("outcome_known_at") or row.get("known_after") or row.get("observed_at"))
        if cutoff_dt is None or known_dt is None or known_dt >= cutoff_dt:
            continue
        if _slice_key(row) != slice_key:
            continue
        source_id = _source_key(row.get("source_id"))
        if not source_id:
            continue
        candidates.setdefault(source_id, []).append(dict(row))

    ranked = [_source_stats(source_id, rows) for source_id, rows in candidates.items()]
    ranked.sort(
        key=lambda row: (
            row["sample_count"] >= min_sample_count,
            row["win_rate"] if row["win_rate"] is not None else -1.0,
            row["avg_binary_edge_realized"] if row["avg_binary_edge_realized"] is not None else -999.0,
            row["sample_count"],
            row["source_id"],
        ),
        reverse=True,
    )
    usable = [row for row in ranked if row["sample_count"] >= min_sample_count]
    if not usable:
        return {
            "routeable": False,
            "selection_mode": "as_of_resolved_scoreboard",
            "slice_key": "|".join(slice_key),
            "chosen_source_id": None,
            "chosen_source_name": None,
            "prior_sample_count": 0 if not ranked else ranked[0]["sample_count"],
            "prior_win_rate": None if not ranked else ranked[0]["win_rate"],
            "prior_avg_binary_edge_realized": None if not ranked else ranked[0]["avg_binary_edge_realized"],
            "min_sample_count": min_sample_count,
            "blockers": ["insufficient_prior_history"],
            "candidate_source_count": len(ranked),
        }
    chosen = usable[0]
    return {
        "routeable": True,
        "selection_mode": "as_of_resolved_scoreboard",
        "slice_key": "|".join(slice_key),
        "chosen_source_id": chosen["source_id"],
        "chosen_source_name": chosen["source_name"],
        "chosen_source_backoff_level": "city_kind_shape_question_side",
        "prior_sample_count": chosen["sample_count"],
        "prior_win_rate": chosen["win_rate"],
        "prior_avg_binary_edge_realized": chosen["avg_binary_edge_realized"],
        "min_sample_count": min_sample_count,
        "blockers": [],
        "candidate_source_count": len(ranked),
    }


def summarize_source_router_replay_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    materialized = [dict(row) for row in rows if isinstance(row, Mapping)]
    totals = {
        "input_rows": len(materialized),
        "routeable_rows": sum(1 for row in materialized if _mapping(row.get("router")).get("routeable") is True),
        "source_router_buy_rows": sum(1 for row in materialized if _mapping(row.get("comparison")).get("source_router_action") in {BUY_YES, BUY_NO}),
        "confirmed_rows": sum(1 for row in materialized if _mapping(row.get("comparison")).get("source_filter_action") in {BUY_YES, BUY_NO}),
        "blocked_stable_buy_rows": sum(1 for row in materialized if _mapping(row.get("comparison")).get("would_filter_stable_buy") is True),
    }
    stable_pnls = [_number(_mapping(row.get("comparison")).get("stable_pnl_usd")) for row in materialized]
    source_router_pnls = [_number(_mapping(row.get("comparison")).get("source_router_pnl_usd")) for row in materialized]
    filter_pnls = [_number(_mapping(row.get("comparison")).get("source_filter_pnl_usd")) for row in materialized]
    stable_pnls = [value for value in stable_pnls if value is not None]
    source_router_pnls = [value for value in source_router_pnls if value is not None]
    filter_pnls = [value for value in filter_pnls if value is not None]
    source_counts = Counter(
        _mapping(row.get("router")).get("chosen_source_id") or "no_route"
        for row in materialized
    )
    blocker_counts: Counter[str] = Counter()
    for row in materialized:
        for blocker in row.get("blockers") or []:
            blocker_counts[str(blocker)] += 1
    return {
        "schema_version": SOURCE_ROUTER_SCHEMA_VERSION,
        "row_type": SOURCE_ROUTER_SUMMARY_ROW_TYPE,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            **totals,
            "stable_pnl_usd": _round(sum(stable_pnls)) if stable_pnls else 0.0,
            "source_router_pnl_usd": _round(sum(source_router_pnls)) if source_router_pnls else 0.0,
            "source_router_minus_stable_pnl_usd": _round(sum(source_router_pnls) - sum(stable_pnls)) if stable_pnls or source_router_pnls else 0.0,
            "source_filter_pnl_usd": _round(sum(filter_pnls)) if filter_pnls else 0.0,
            "source_filter_minus_stable_pnl_usd": _round(sum(filter_pnls) - sum(stable_pnls)) if stable_pnls or filter_pnls else 0.0,
            "chosen_source_counts": dict(sorted(source_counts.items())),
            "blocker_counts": dict(sorted(blocker_counts.items())),
        },
        "slices": _summarize_router_slices(materialized),
    }


def _summarize_router_slices(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (
            str(row.get("city_id") or "unknown"),
            str(row.get("market_kind") or "unknown"),
            str(row.get("contract_shape") or "unknown"),
            str(row.get("question_side") or "unknown"),
        )
        grouped.setdefault(key, []).append(row)
    summaries = []
    for key, group in grouped.items():
        city_id, market_kind, contract_shape, question_side = key
        stable_pnl = sum(_number(_mapping(row.get("comparison")).get("stable_pnl_usd")) or 0.0 for row in group)
        source_router_pnl = sum(_number(_mapping(row.get("comparison")).get("source_router_pnl_usd")) or 0.0 for row in group)
        filter_pnl = sum(_number(_mapping(row.get("comparison")).get("source_filter_pnl_usd")) or 0.0 for row in group)
        summaries.append(
            {
                "city_id": city_id,
                "market_kind": market_kind,
                "contract_shape": contract_shape,
                "question_side": question_side,
                "rows": len(group),
                "routeable_rows": sum(1 for row in group if _mapping(row.get("router")).get("routeable") is True),
                "source_router_buy_rows": sum(1 for row in group if _mapping(row.get("comparison")).get("source_router_action") in {BUY_YES, BUY_NO}),
                "confirmed_rows": sum(1 for row in group if _mapping(row.get("comparison")).get("source_filter_action") in {BUY_YES, BUY_NO}),
                "stable_pnl_usd": _round(stable_pnl),
                "source_router_pnl_usd": _round(source_router_pnl),
                "source_router_minus_stable_pnl_usd": _round(source_router_pnl - stable_pnl),
                "source_filter_pnl_usd": _round(filter_pnl),
                "source_filter_minus_stable_pnl_usd": _round(filter_pnl - stable_pnl),
            }
        )
    return sorted(summaries, key=lambda row: (row["routeable_rows"], row["rows"]), reverse=True)


class _DecisionIndex:
    def __init__(self, rows: Iterable[Mapping[str, Any]]) -> None:
        self._by_shared_candidate: dict[str, list[dict[str, Any]]] = {}
        self._by_market: dict[str, list[dict[str, Any]]] = {}
        self.row_count = 0
        self.indexed_count = 0
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            self.row_count += 1
            if not _is_stable_decision(row):
                continue
            copied = dict(row)
            shared_candidate_id = _optional_text(copied.get("shared_candidate_id"), _mapping(copied.get("shared_candidate")).get("shared_candidate_id"), _mapping(copied.get("shared_candidate")).get("candidate_id"))
            market_id = _optional_text(copied.get("market_id"), _mapping(copied.get("shared_candidate")).get("market_id"))
            if shared_candidate_id:
                self._by_shared_candidate.setdefault(shared_candidate_id, []).append(copied)
                self.indexed_count += 1
            if market_id:
                self._by_market.setdefault(market_id, []).append(copied)
                if not shared_candidate_id:
                    self.indexed_count += 1

    def match(self, source_row: Mapping[str, Any]) -> dict[str, Any] | None:
        shared_candidate_id = _optional_text(source_row.get("shared_candidate_id"), source_row.get("candidate_id"))
        market_id = _optional_text(source_row.get("market_id"), source_row.get("ticker"), _mapping(source_row.get("decision_artifact")).get("market_id"))
        candidates: list[dict[str, Any]] = []
        if shared_candidate_id:
            candidates.extend(self._by_shared_candidate.get(shared_candidate_id, []))
        if not candidates and market_id:
            candidates.extend(self._by_market.get(market_id, []))
        if not candidates:
            return None
        source_dt = _parse_dt(
            source_row.get("observed_at"),
        )
        return sorted(candidates, key=lambda row: _decision_match_key(row, source_dt), reverse=True)[0]


def _is_stable_decision(row: Mapping[str, Any]) -> bool:
    policy = (_optional_text(row.get("policy"), row.get("selected_lane")) or "").lower()
    wallet = (_optional_text(row.get("wallet_id"), _mapping(row.get("provenance")).get("source_wallet_id")) or "").lower()
    role = (_optional_text(row.get("decision_role")) or "").lower()
    if "beta" in policy or wallet == "beta_paper":
        return False
    if policy in {"control_stable", "normal", "stable"}:
        return True
    if wallet == "stable_paper":
        return True
    if role == "paper_shadow" and policy in {"", "normal"}:
        return True
    return False


def _decision_match_key(row: Mapping[str, Any], source_dt: datetime | None) -> tuple[int, float]:
    score = 0
    policy = (_optional_text(row.get("policy"), row.get("selected_lane")) or "").lower()
    wallet = (_optional_text(row.get("wallet_id"), _mapping(row.get("provenance")).get("source_wallet_id")) or "").lower()
    if policy == "control_stable":
        score += 50
    if wallet == "stable_paper":
        score += 40
    if _normalize_action(row.get("action")) in {BUY_YES, BUY_NO}:
        score += 5
    decision_dt = _parse_dt(row.get("observed_at") or row.get("decided_at"))
    if source_dt is None or decision_dt is None:
        return (score, 0.0)
    age = abs((decision_dt - source_dt).total_seconds())
    return (score, -age)


def _apply_stable_decision(ledger_row: dict[str, Any], decision: Mapping[str, Any]) -> None:
    action = _normalize_action(decision.get("action"))
    side = _optional_text(decision.get("side")) or _side_from_action(action)
    ledger_row["stable_decision_id"] = _optional_text(decision.get("decision_id"))
    ledger_row["stable_policy"] = _optional_text(decision.get("policy"), decision.get("selected_lane"))
    ledger_row["stable_action"] = action
    ledger_row["stable_side"] = side
    ledger_row["stable_reason_code"] = _optional_text(decision.get("reason_code"))
    ledger_row["stable_reason"] = _optional_text(decision.get("reason"))
    ledger_row["stable_requested_position_size_usd"] = _number(decision.get("requested_position_size_usd"))
    ledger_row["stable_approved_position_size_usd"] = _number(decision.get("approved_position_size_usd"))
    for key in ("entry_price", "price", "yes_price", "no_price", "best_yes_ask", "best_no_ask"):
        value = _number(decision.get(key))
        if value is not None:
            ledger_row[key] = value


def _source_stats(source_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    wins = sum(1 for row in rows if row.get("win") is True)
    edge_values = [_number(row.get("binary_edge_realized")) for row in rows]
    edge_values = [value for value in edge_values if value is not None]
    return {
        "source_id": source_id,
        "source_name": next((_optional_text(row.get("source_name")) for row in rows if _optional_text(row.get("source_name"))), source_id),
        "sample_count": len(rows),
        "win_rate": _round(wins / len(rows)) if rows else None,
        "avg_binary_edge_realized": _round(sum(edge_values) / len(edge_values)) if edge_values else None,
    }


def _allow_history_actual_outcome(edge: dict[str, Any], ledger: Mapping[str, Any]) -> None:
    official = _normalized_side(ledger.get("actual_outcome"))
    source_side = _normalized_side(edge.get("source_implied_side"))
    price = _number(edge.get("source_side_price"))
    if official not in {"YES", "NO"}:
        return
    edge["official_outcome"] = official
    edge["outcome_source"] = "source_outcome_ledger_actual"
    edge["outcome_known_at"] = _optional_text(ledger.get("known_after"), ledger.get("resolved_at"), edge.get("outcome_known_at"))
    if source_side in {"YES", "NO"}:
        win = source_side == official
        edge["win"] = win
        if price is not None:
            edge["binary_edge_realized"] = _round((1.0 if win else 0.0) - price)
            edge["flat_1usd_pnl"] = _round((1.0 - price) if win else -price)
    blockers = [
        part
        for part in str(edge.get("exclusion_reason") or "").split(";")
        if part and part != "missing_official_outcome"
    ]
    edge["eligible_for_edge_validation"] = not blockers
    edge["exclusion_reason"] = ";".join(blockers) if blockers else None


def _pnl(*, action: str, side: str | None, outcome: str | None, price: float | None, stake: float | None) -> dict[str, Any]:
    if action == SKIP:
        return {"calculable": True, "stake_usd": 0.0, "contracts": 0.0, "payout_usd": 0.0, "pnl_usd": 0.0, "won": None}
    if action not in {BUY_YES, BUY_NO} or side not in {"YES", "NO"} or outcome not in {"YES", "NO"} or price is None or price <= 0 or stake is None or stake <= 0:
        return {"calculable": False, "stake_usd": _round(stake), "contracts": None, "payout_usd": None, "pnl_usd": None, "won": None}
    contracts = stake / price
    won = side == outcome
    payout = contracts if won else 0.0
    return {
        "calculable": True,
        "stake_usd": _round(stake),
        "contracts": _round(contracts),
        "payout_usd": _round(payout),
        "pnl_usd": _round(payout - stake),
        "won": won,
    }


def _candidate_key(row: Mapping[str, Any]) -> tuple[str, str]:
    return (
        _optional_text(row.get("shared_candidate_id")) or "",
        _optional_text(row.get("market_id")) or "",
    )


def _slice_key(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        _optional_text(row.get("city_id")) or "unknown",
        _optional_text(row.get("market_kind")) or "unknown",
        _optional_text(row.get("contract_shape")) or "unknown",
        _optional_text(row.get("question_side")) or "unknown",
    )


def _side_price(row: Mapping[str, Any], side: str | None) -> float | None:
    if side == "YES":
        return _number(row.get("source_side_price"), row.get("best_yes_ask"), row.get("yes_ask"), row.get("yes_price"), row.get("entry_price"), row.get("price"))
    if side == "NO":
        return _number(row.get("source_side_price"), row.get("best_no_ask"), row.get("no_ask"), row.get("no_price"), row.get("entry_price"), row.get("price"))
    return None


def _source_router_price(chosen_edge: Mapping[str, Any] | None, fallback_row: Mapping[str, Any], side: str | None) -> float | None:
    edge = _mapping(chosen_edge)
    price = _number(edge.get("source_side_price"), edge.get("market_implied_probability"))
    if price is not None:
        return price
    return _side_price(fallback_row, side)


def _side_from_action(action: Any) -> str | None:
    normalized = _normalize_action(action)
    if normalized == BUY_YES:
        return "YES"
    if normalized == BUY_NO:
        return "NO"
    return None


def _action_from_side(side: str | None) -> str:
    if side == "YES":
        return BUY_YES
    if side == "NO":
        return BUY_NO
    return SKIP


def _normalize_action(value: Any) -> str:
    text = str(value or SKIP).strip().upper()
    if text in {"BUY", "YES", BUY_YES}:
        return BUY_YES
    if text in {"NO", BUY_NO}:
        return BUY_NO
    return SKIP


def _normalized_side(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if text in {"YES", "BUY_YES", "TRUE", "1"}:
        return "YES"
    if text in {"NO", "BUY_NO", "FALSE", "0"}:
        return "NO"
    return None


def _source_key(value: Any) -> str | None:
    text = _optional_text(value)
    if not text:
        return None
    return text.strip().lower().replace("-", "_")


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = _optional_text(value)
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _decision_id(row: Mapping[str, Any]) -> str:
    payload = {
        "shared_candidate_id": row.get("shared_candidate_id"),
        "market_id": row.get("market_id"),
        "observed_at": row.get("observed_at"),
        "chosen_source_id": _mapping(row.get("router")).get("chosen_source_id"),
        "stable_action": _mapping(row.get("stable_baseline")).get("action"),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return sha1(raw.encode("utf-8")).hexdigest()


def _number(*values: Any) -> float | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed):
            return parsed
    return None


def _round(value: float | None) -> float | None:
    return round(value, 6) if value is not None and math.isfinite(value) else None


def _optional_text(*values: Any) -> str | None:
    for value in values:
        if value in (None, ""):
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


__all__ = [
    "SOURCE_ROUTER_SCHEMA_VERSION",
    "build_joined_source_router_ledger_rows",
    "build_source_router_replay_row",
    "build_source_router_replay_rows",
    "select_source_for_candidate",
    "summarize_source_router_replay_rows",
]
