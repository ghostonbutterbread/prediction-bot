"""Disabled, derived-only forward observer over already-emitted shared-candidate lanes.

No exchange, collector, resolver, or paper account is called here. The input is
one append-only forward decision cohort; resolutions enter only the later wallet.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any
import hashlib
import json
import math

from bot.composed_lane_wallet import _reject_outcome_fields, _timestamp, evaluate_composed_intents
from bot.shared_market_runtime import shared_snapshot_is_fresh

CONTROL = "control_stable"
ROUTER = "shadow_source_router"
GUARD = "shadow_fee_aware_loss_guard"
_BUYS = {"BUY_YES", "BUY_NO"}
_IDENTITY = ("shared_candidate_id", "shared_snapshot_id", "run_id", "market_id", "observed_at")


def compose_forward_rows(rows: list[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pair stable BUY intent with strict Router confirmation and same-side guard.

    Only stable rows establish coverage. Missing/mismatched components are SKIPs,
    never nearest-market matches. This is the producer's *recorded* stable action;
    callers must report whether their producer recorded pre-risk picker intents.
    """
    indexed: dict[tuple[str, ...], dict[str, list[Mapping[str, Any]]]] = {}
    for row in rows:
        policy = row.get("policy")
        if policy not in {CONTROL, ROUTER, GUARD}:
            continue
        # Router source-history provenance may legitimately describe earlier
        # outcomes. Never forward it: inspect only its allowlisted admission flag.
        if policy == ROUTER:
            _reject_outcome_fields({k: v for k, v in row.items() if k != "provenance"})
        else:
            _reject_outcome_fields(row)
        key = tuple(str(row.get(k) or "") for k in _IDENTITY)
        indexed.setdefault(key, {}).setdefault(str(policy), []).append(row)
    pairs, intents = [], []
    for key, group in sorted(indexed.items()):
        if CONTROL not in group:
            continue
        for control in group[CONTROL]:
            identity = dict(zip(_IDENTITY, key))
            action = str(control.get("action") or "SKIP").upper()
            reason = None
            if not all(key) or not _valid_time(key[-1]):
                reason = "incomplete_shared_identity"
            elif len(group[CONTROL]) != 1:
                reason = "duplicate_control"
            elif action not in _BUYS:
                reason = "baseline_skip"
            elif len(group.get(ROUTER, [])) != 1:
                reason = "missing_router" if not group.get(ROUTER) else "duplicate_router"
            else:
                router = group[ROUTER][0]
                history = (router.get("provenance") or {}).get("source_router") or {}
                if (history.get("direct_history") or {}).get("mode") != "committed_index_direct_read":
                    reason = "router_history_unverified"
                elif router.get("action") != action:
                    reason = str(router.get("reason_code") or "router_disagrees") if router.get("action") == "SKIP" else "router_disagrees"
                elif len(group.get(GUARD, [])) != 1:
                    reason = "missing_guard" if not group.get(GUARD) else "duplicate_guard"
                elif group[GUARD][0].get("action") != action:
                    reason = str(group[GUARD][0].get("reason_code") or "guard_disagrees") if group[GUARD][0].get("action") == "SKIP" else "guard_disagrees"
            price = None
            quoted_ask = None
            guard = group.get(GUARD, [None])[0]
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
            pair = {**identity, "control_decision_id": control.get("decision_id"),
                    "router_decision_id": (group.get(ROUTER) or [{}])[0].get("decision_id"),
                    "guard_decision_id": (group.get(GUARD) or [{}])[0].get("decision_id"),
                    "control_action": action, "combined_action": combined,
                    "control_economics_status": "quote_proxy" if control_quoted else "control_quote_unavailable",
                    "wallet_input_status": "available" if wallet_ready else "wallet_inputs_unavailable",
                    "reason_code": reason or "router_confirmed_guard_passed",
                    "non_mutating": True, "quote_not_fill": True}
            pairs.append(pair)
            if action in _BUYS and all(key) and _valid_time(key[-1]) and wallet_ready:
                # The control wallet sees the same stable intent, without a
                # veto; the combined wallet receives only passing BUYs.
                probability = control.get("model_probability")
                confidence = control.get("confidence")
                event_key = control.get("event_ticker") or (control.get("shared_candidate") or {}).get("event_ticker") or identity["market_id"]
                base = {**identity, "question": question,
                        "exchange": control.get("exchange") or "kalshi", "model_probability": probability,
                        "confidence": confidence, "event_key": event_key,
                        "source_context": {"market_route": route}, "quote_not_fill": True}
                if control_quoted:
                    intents.append({**base, "lane_id": CONTROL, "decision_id": "forward-control:" + _digest(identity),
                                    "action": action, "entry_price": stable_price})
                if reason is None:
                    intents.append({**base, "lane_id": "forward_composed_paper", "decision_id": "forward-combined:" + _digest(identity),
                                    "action": action, "entry_price": price})
    return pairs, intents


def evaluate_forward_composition(
    rows: list[Mapping[str, Any]], resolutions: list[Mapping[str, Any]], *,
    starting_balance_usd: float, kelly_sizer: Any, risk_policy: Any,
    min_edge: float, min_confidence: float, max_entry_price: float,
    fee_rate: float = 0.07, event_cap_pct: float = 0.10,
) -> dict[str, Any]:
    """Rebuild both in-memory wallets from the complete forward cohort."""
    pairs, intents = compose_forward_rows(rows)
    kwargs = dict(resolutions=resolutions, starting_balance_usd=starting_balance_usd,
                  kelly_sizer=kelly_sizer, risk_policy=risk_policy, min_edge=min_edge,
                  min_confidence=min_confidence, max_entry_price=max_entry_price,
                  fee_rate=fee_rate, event_cap_pct=event_cap_pct)
    wallets = {}
    for name, lane in (("control_wallet", CONTROL), ("combined_wallet", "forward_composed_paper")):
        valid = [row for row in intents if row["lane_id"] == lane]
        wallets[name] = evaluate_composed_intents(intents=valid, **kwargs)
    return {"schema_name": "forward_composed_paper_observer", "non_mutating": True,
            "paper_only": True, "promotion_eligible": False,
            "candidate_universe": "recorded_control_stable_rows_not_all_pre_risk_picker_intents",
            "pairs": pairs, "reason_counts": dict(Counter(row["reason_code"] for row in pairs)),
            **wallets}


def observe_fresh_forward_snapshot(
    rows: list[Mapping[str, Any]], resolutions: list[Mapping[str, Any]],
    state: Mapping[str, Any], *, enabled: bool = False, now: Any = None,
    max_snapshot_age_seconds: int = 120, **wallet_policy: Any,
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
