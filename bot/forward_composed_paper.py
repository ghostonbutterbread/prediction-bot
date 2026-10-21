"""Disabled, derived-only forward observer over already-emitted shared-candidate lanes.

No exchange, collector, resolver, or paper account is called here. The input is
one append-only forward decision cohort; resolutions enter only the later wallet.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any
import hashlib
import json
import math

from bot.composed_lane_wallet import _reject_outcome_fields, _timestamp, evaluate_composed_intents
from bot.feeds.weather_pro import project_probability_evidence
from bot.shared_market_runtime import shared_snapshot_is_fresh

CONTROL = "control_stable"
PICKER = "shadow_pre_risk_picker"
ROUTER = "shadow_source_router"
GUARD = "shadow_fee_aware_loss_guard"
PRE_RISK_GUARD = "shadow_pre_risk_fee_aware_loss_guard"
_BUYS = {"BUY_YES", "BUY_NO"}
_IDENTITY = ("shared_candidate_id", "shared_snapshot_id", "run_id", "market_id", "observed_at")

def validate_forward_combination(combination: Mapping[str, Any]) -> None:
    """Accept only the saved pattern this observer actually implements."""
    baseline = combination.get("base_lane")
    guard = GUARD if baseline == CONTROL else PRE_RISK_GUARD if baseline == PICKER else None
    supported_vetoes = [
        {"lane": ROUTER, "mode": "require_agreement", "required": True},
        {"lane": guard, "mode": "require_agreement", "required": True},
    ]
    if (guard is None
            or combination.get("action_lane") != baseline
            or combination.get("sizing_lane") != baseline
            or combination.get("price_lane") != guard
            or combination.get("fallback_to_base") is not False
            or combination.get("vetoes") not in (supported_vetoes, supported_vetoes[1:])
            or combination.get("gates")
            or combination.get("exposure")
            or combination.get("fixed_notional_usd") is not None):
        raise ValueError("unsupported forward combination: replay may test this idea, but the paper observer does not implement it")

def compose_forward_rows(rows: Sequence[Mapping[str, Any]], *, combination: Mapping[str, Any] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pair recorded pre-risk picker (or legacy stable) intent with selected vetoes/guard.

    New cohorts use the captured pre-risk picker lane; accepted-wallet rows are
    retained as separate audit evidence. Historical cohorts without that lane
    remain explicitly labeled legacy accepted-only, never pre-risk coverage.
    """
    pre_risk = any(row.get("policy") == PICKER for row in rows)
    baseline_policy = PICKER if pre_risk else CONTROL
    guard_policy = PRE_RISK_GUARD if pre_risk else GUARD
    if combination is not None:
        validate_forward_combination(combination)
        if pre_risk != (combination["base_lane"] == PICKER):
            if pre_risk:
                raise ValueError("unsupported forward combination: stable lanes cannot select a pre-risk picker cohort")
            raise ValueError("unsupported forward combination: pre-risk lanes cannot select a stable cohort")
    require_router = combination is None or any(veto["lane"] == ROUTER for veto in combination["vetoes"])
    indexed: dict[tuple[str, ...], dict[str, list[Mapping[str, Any]]]] = {}
    for row in rows:
        policy = row.get("policy")
        if policy not in {CONTROL, PICKER, ROUTER, GUARD, PRE_RISK_GUARD}:
            continue
        # Router source-history provenance may legitimately describe earlier
        # outcomes. Never forward it: project only scalar decision-time grade
        # evidence and an explicit selection marker for the combined wallet.
        if policy == ROUTER:
            _reject_outcome_fields({k: v for k, v in row.items() if k != "provenance"})
        else:
            _reject_outcome_fields(row)
        key = tuple(str(row.get(k) or "") for k in _IDENTITY)
        indexed.setdefault(key, {}).setdefault(str(policy), []).append(row)
    pairs, intents = [], []
    for key, group in sorted(indexed.items()):
        if baseline_policy not in group:
            if pre_risk and CONTROL in group:
                pairs.append({**dict(zip(_IDENTITY, key)), "stable_wallet_decision_id": group[CONTROL][0].get("decision_id"),
                              "stable_wallet_action": group[CONTROL][0].get("action"),
                              "control_action": "SKIP", "combined_action": "SKIP",
                              "reason_code": "missing_picker_intent", "non_mutating": True,
                              "quote_not_fill": True})
            continue
        for control in group[baseline_policy]:
            identity = dict(zip(_IDENTITY, key))
            action = str(control.get("action") or "SKIP").upper()
            reason = None
            router: Mapping[str, Any] = {}
            history: Mapping[str, Any] = {}
            if not all(key) or not _valid_time(key[-1]):
                reason = "incomplete_shared_identity"
            elif len(group[baseline_policy]) != 1:
                reason = "duplicate_control"
            elif pre_risk and (control.get("provenance") or {}).get("picker_intent_source") != "decision_artifact.strategy_signal":
                reason = "picker_intent_not_recorded"
            elif action not in _BUYS:
                reason = "picker_intent_not_recorded" if pre_risk else "baseline_skip"
            if reason is None and require_router:
                if len(group.get(ROUTER, [])) != 1:
                    reason = "missing_router" if not group.get(ROUTER) else "duplicate_router"
                else:
                    router = group[ROUTER][0]
                    history = (router.get("provenance") or {}).get("source_router") or {}
                    if (history.get("direct_history") or {}).get("mode") != "committed_index_direct_read":
                        reason = "router_history_unverified"
                    elif router.get("action") != action:
                        reason = str(router.get("reason_code") or "router_disagrees") if router.get("action") == "SKIP" else "router_disagrees"
            if reason is None:
                if len(group.get(guard_policy, [])) != 1:
                    reason = "missing_guard" if not group.get(guard_policy) else "duplicate_guard"
                elif group[guard_policy][0].get("action") != action:
                    reason = str(group[guard_policy][0].get("reason_code") or "guard_disagrees") if group[guard_policy][0].get("action") == "SKIP" else "guard_disagrees"
            price = None
            quoted_ask = None
            guard = group.get(guard_policy, [None])[0]
            economics = (guard.get("provenance") or {}).get("payout_aware") if guard else None
            if isinstance(economics, Mapping) and economics.get("quote_not_fill") is True:
                try:
                    quote, proposed = float(economics["selected_side_ask"]), float(economics["entry_price"])
                    if all(math.isfinite(v) and 0 < v < 1 for v in (quote, proposed)) and proposed >= quote:
                        quoted_ask, price = quote, proposed
                except (KeyError, TypeError, ValueError):
                    pass
            if reason is None and (price is None or _finite_price(guard.get("entry_price")) != price):
                reason = "guard_quote_unavailable"
            stable_price = _finite_price(control.get("entry_price"))
            control_quoted = quoted_ask is not None and stable_price == quoted_ask
            route = control.get("market_route") or (control.get("provenance") or {}).get("market_route")
            question = control.get("question") or (control.get("provenance") or {}).get("future_pnl_inputs", {}).get("question")
            wallet_ready = (bool(question) and isinstance(route, Mapping)
                            and route.get("allowed") is True and bool(route.get("handler_id"))
                            and _finite_price(control.get("model_probability")) is not None
                            and _finite_price(control.get("confidence")) is not None)
            if reason is None and not wallet_ready:
                reason = "wallet_inputs_unavailable"
            combined = action if reason is None else "SKIP"
            stable_audit = (group.get(CONTROL) or [{}])[0]
            pair = {**identity, "control_decision_id": control.get("decision_id"),
                    "stable_wallet_decision_id": stable_audit.get("decision_id") if pre_risk else control.get("decision_id"),
                    "stable_wallet_action": stable_audit.get("action") if pre_risk else action,
                    "picker_intent_source": (control.get("provenance") or {}).get("picker_intent_source") if pre_risk else None,
                    "router_decision_id": (group.get(ROUTER) or [{}])[0].get("decision_id"),
                    "guard_decision_id": (group.get(guard_policy) or [{}])[0].get("decision_id"),
                    "control_action": action, "combined_action": combined,
                    "control_economics_status": "quote_proxy" if control_quoted else "control_quote_unavailable",
                    "wallet_input_status": "available" if wallet_ready else "wallet_inputs_unavailable",
                    "reason_code": reason or ("router_confirmed_guard_passed" if require_router else "guard_passed_without_router"),
                    "non_mutating": True, "quote_not_fill": True}
            pairs.append(pair)
            if (action in _BUYS and all(key) and _valid_time(key[-1]) and wallet_ready
                    and (not pre_risk or pair["picker_intent_source"] == "decision_artifact.strategy_signal")
                    and len(group[baseline_policy]) == 1):
                # Both wallets start from the same recorded baseline intent;
                # only the combined wallet applies Router/guard vetoes.
                probability = control.get("model_probability")
                confidence = control.get("confidence")
                event_key = control.get("event_ticker") or (control.get("shared_candidate") or {}).get("event_ticker") or identity["market_id"]
                base = {**identity, "question": question,
                        "exchange": control.get("exchange") or "kalshi", "model_probability": probability,
                        "confidence": confidence, "event_key": event_key,
                        "source_context": {"market_route": route}, "quote_not_fill": True}
                evidence = project_probability_evidence(control.get("probability_evidence"), probability)
                if evidence is not None:
                    base["source_context"]["probability_evidence"] = evidence
                if control_quoted:
                    intents.append({**base, "lane_id": CONTROL, "decision_id": "forward-control:" + _digest(identity),
                                    "action": action, "entry_price": stable_price})
                if reason is None:
                    combined_source = dict(base["source_context"])
                    if require_router:
                        # Do not transport source history, observations, or scores.
                        # Missing/malformed grades remain marked as Router-selected
                        # so an enabled risk cap policy can reject them explicitly.
                        grade = history.get("source_grade")
                        combined_source.update(
                            router_selection={"role": "veto", "policy": ROUTER,
                                              "decision_id": router.get("decision_id")},
                            router_decision_metadata={"source_grade": grade if isinstance(grade, str) else None},
                        )
                    intents.append({**base, "source_context": combined_source,
                                    "lane_id": "forward_composed_paper", "decision_id": "forward-combined:" + _digest(identity),
                                    "action": action, "entry_price": price})
    return pairs, intents


def evaluate_forward_composition(
    rows: list[Mapping[str, Any]], resolutions: list[Mapping[str, Any]], *,
    starting_balance_usd: float, kelly_sizer: Any, risk_policy: Any,
    min_edge: float, min_confidence: float, max_entry_price: float,
    fee_rate: float = 0.07, event_cap_pct: float = 0.10,
    combination: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Rebuild both in-memory wallets from the complete forward cohort."""
    if combination is not None:
        validate_forward_combination(combination)
    pairs, intents = compose_forward_rows(rows, combination=combination)
    kwargs = dict(resolutions=resolutions, starting_balance_usd=starting_balance_usd,
                  kelly_sizer=kelly_sizer, risk_policy=risk_policy, min_edge=min_edge,
                  min_confidence=min_confidence, max_entry_price=max_entry_price,
                  fee_rate=fee_rate, event_cap_pct=event_cap_pct)
    wallets = {}
    for name, lane in (("control_wallet", CONTROL), ("combined_wallet", "forward_composed_paper")):
        valid = [row for row in intents if row["lane_id"] == lane]
        wallets[name] = evaluate_composed_intents(intents=valid, **kwargs)
    return {"schema_name": "forward_composed_paper_observer", "non_mutating": True,
            "composition_name": combination.get("name") if combination is not None else None,
            "paper_only": True, "promotion_eligible": False,
            "candidate_universe": ("recorded_pre_risk_picker_intents" if any(row.get("policy") == PICKER for row in rows)
                                   else "recorded_control_stable_rows_not_all_pre_risk_picker_intents"),
            "pairs": pairs, "reason_counts": dict(Counter(row["reason_code"] for row in pairs)),
            **wallets}


def observe_fresh_forward_snapshot(
    rows: list[Mapping[str, Any]], resolutions: list[Mapping[str, Any]],
    state: Mapping[str, Any], *, enabled: bool = False, now: Any = None,
    max_snapshot_age_seconds: int = 120, require_pre_risk_picker: bool = False,
    **wallet_policy: Any,
) -> dict[str, Any]:
    """Explicit disabled observer gate for a collector-owned forward snapshot.

    The caller supplies the complete append-only decision cohort, not just its
    latest slice, so unresolved synthetic positions survive later observations.
    This only reads state and returns derived data; no collector or wallet writes.
    """
    if not enabled:
        return {"status": "skipped", "reason_code": "observer_disabled"}
    snapshot, publisher = state.get("latest_snapshot"), state.get("publisher")
    if (not isinstance(snapshot, Mapping) or not isinstance(publisher, Mapping)
            or not snapshot.get("snapshot_id")
            or snapshot.get("publisher_runtime") != publisher.get("runtime_kind")
            or snapshot.get("publisher_instance_id") != publisher.get("instance_id")
            or not shared_snapshot_is_fresh(snapshot, now=now,
                                            max_snapshot_age_seconds=max_snapshot_age_seconds)
            or not any(row.get("shared_snapshot_id") == snapshot["snapshot_id"]
                       and row.get("policy") == CONTROL for row in rows)):
        return {"status": "skipped", "reason_code": "shared_snapshot_stale_or_mismatched"}
    if require_pre_risk_picker:
        latest = [row for row in rows if row.get("shared_snapshot_id") == snapshot["snapshot_id"]]
        controls = {tuple(str(row.get(k) or "") for k in _IDENTITY)
                    for row in latest if row.get("policy") == CONTROL}
        pickers = Counter(tuple(str(row.get(k) or "") for k in _IDENTITY)
                          for row in latest if row.get("policy") == PICKER)
        if not pickers:
            return {"status": "skipped", "reason_code": "pre_risk_picker_lane_missing"}
        if any(pickers[key] != 1 for key in controls):
            return {"status": "skipped", "reason_code": "pre_risk_picker_lane_incomplete"}
    result = evaluate_forward_composition(rows, resolutions, **wallet_policy)
    result["latest_shared_snapshot_id"] = snapshot["snapshot_id"]
    return result


def _valid_time(value: str) -> bool:
    try:
        _timestamp(value)
        return True
    except ValueError:
        return False


def _finite_price(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and 0 < number < 1 else None


def _digest(identity: Mapping[str, str]) -> str:
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
