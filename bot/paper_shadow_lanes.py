"""Lightweight paper-only decision lanes over shared candidates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from bot.agent_decision_ledger import (
    build_agent_decision_id,
    build_agent_run_id,
    validate_agent_decision_row,
)
from bot.file_ops import append_jsonl
from bot.paper_wallets import BETA_PAPER_WALLET_ID, STABLE_PAPER_WALLET_ID

PAPER_LANE_AGENT_ID = "paper"
PAPER_LANE_RUNTIME = "paper"
PAPER_LANE_DECISION_ROLE = "paper_lane"
PAPER_LANE_SCHEMA_VERSION = 1
DEFAULT_CONFIDENCE_FLOOR = 0.58
DEFAULT_LANE_IDS = ("control_stable", "shadow_current_beta", "shadow_confidence_floor")


@dataclass(frozen=True, slots=True)
class PaperShadowLaneWriteResult:
    decision_path: str
    rows_written: int
    lane_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _LaneDefinition:
    lane_id: str
    source_wallet_id: str | None = None


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
    source_rows: Mapping[str, dict[str, Any] | None],
    decision_path: Path,
) -> dict[str, Any]:
    if lane.lane_id == "shadow_confidence_floor":
        decision = _confidence_floor_decision(config, signal, source_rows.get(STABLE_PAPER_WALLET_ID))
    elif lane.lane_id == "shadow_premium_city":
        decision = _premium_city_decision(config, signal, source_rows.get(STABLE_PAPER_WALLET_ID))
    else:
        decision = _passthrough_decision(lane, signal, source_rows.get(lane.source_wallet_id or ""))

    observed_at = _observed_at(decision.get("source_row"), signal)
    market_id = _text(
        (decision.get("source_row") or {}).get("market_id") if isinstance(decision.get("source_row"), dict) else None,
        signal.get("market_id"),
    )
    policy = lane.lane_id
    source_row = decision.get("source_row") if isinstance(decision.get("source_row"), dict) else None
    confidence_before = _number((source_row or {}).get("confidence"), signal.get("confidence"))
    confidence_after = _number(decision.get("confidence_after"), confidence_before)
    action = str(decision.get("action") or "SKIP")
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
        "candidate_dataset_identity": "shared_candidate_dataset",
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
        "confidence": confidence_after,
        "edge": _number((source_row or {}).get("edge"), signal.get("edge")),
        "model_probability": _number((source_row or {}).get("model_probability"), signal.get("model_probability")),
        "entry_price": _number((source_row or {}).get("entry_price"), signal.get("market_price")),
        "price": _number((source_row or {}).get("price"), (source_row or {}).get("entry_price"), signal.get("market_price")),
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
            "lane_version": PAPER_LANE_SCHEMA_VERSION,
            "confidence_before": confidence_before,
            "confidence_after": confidence_after,
            "source_decision_id": (source_row or {}).get("decision_id"),
            "source_policy": (source_row or {}).get("policy"),
            "source_decision_role": (source_row or {}).get("decision_role"),
            "source_wallet_id": (source_row or {}).get("wallet_id"),
            "decision_only": True,
        },
    }
    return validate_agent_decision_row(row)


def _passthrough_decision(
    lane: _LaneDefinition,
    signal: Mapping[str, Any],
    source_row: dict[str, Any] | None,
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
        "confidence_after": _number(source_row.get("confidence"), signal.get("confidence")),
        "requested_position_size_usd": _number(source_row.get("requested_position_size_usd")),
        "approved_position_size_usd": _number(source_row.get("approved_position_size_usd")),
    }


def _confidence_floor_decision(
    config: Mapping[str, Any],
    signal: Mapping[str, Any],
    source_row: dict[str, Any] | None,
) -> dict[str, Any]:
    baseline = _passthrough_decision(
        _LaneDefinition("control_stable", source_wallet_id=STABLE_PAPER_WALLET_ID),
        signal,
        source_row,
    )
    confidence = _number((source_row or {}).get("confidence"), signal.get("confidence"))
    floor = _confidence_floor(config)
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
                "reason": f"Confidence below paper lane floor {floor:.2f}",
                "approved_position_size_usd": 0.0,
            }
        )
        return baseline
    baseline.update(
        {
            "reason_code": "approved_confidence_floor",
            "reason": f"Confidence meets paper lane floor {floor:.2f}",
        }
    )
    return baseline


def _premium_city_decision(
    config: Mapping[str, Any],
    signal: Mapping[str, Any],
    source_row: dict[str, Any] | None,
) -> dict[str, Any]:
    baseline = _passthrough_decision(
        _LaneDefinition("control_stable", source_wallet_id=STABLE_PAPER_WALLET_ID),
        signal,
        source_row,
    )
    premium_cfg = _mapping(config.get("shadow_premium_city"))
    allowlist = {str(value).strip().lower() for value in premium_cfg.get("allowlist", []) if str(value).strip()}
    city = _candidate_city(signal)
    if allowlist and city in allowlist:
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


def _lane_definitions(config: Mapping[str, Any]) -> tuple[_LaneDefinition, ...]:
    requested = config.get("lanes")
    if requested in (None, ""):
        lane_ids = list(DEFAULT_LANE_IDS)
        premium_cfg = _mapping(config.get("shadow_premium_city"))
        if premium_cfg.get("enabled"):
            lane_ids.append("shadow_premium_city")
    elif isinstance(requested, Mapping):
        lane_ids = [str(lane_id) for lane_id, lane_cfg in requested.items() if _mapping(lane_cfg).get("enabled", True)]
    else:
        lane_ids = [str(lane_id) for lane_id in requested]

    definitions: list[_LaneDefinition] = []
    for lane_id in lane_ids:
        if lane_id == "control_stable":
            definitions.append(_LaneDefinition(lane_id, source_wallet_id=STABLE_PAPER_WALLET_ID))
        elif lane_id == "shadow_current_beta":
            definitions.append(_LaneDefinition(lane_id, source_wallet_id=BETA_PAPER_WALLET_ID))
        elif lane_id in {"shadow_confidence_floor", "shadow_premium_city"}:
            definitions.append(_LaneDefinition(lane_id))
        else:
            raise ValueError(f"unknown paper shadow lane: {lane_id}")
    return tuple(definitions)


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


def _observed_at(source_row: dict[str, Any] | None, signal: Mapping[str, Any]) -> str:
    value = (source_row or {}).get("observed_at") or signal.get("candidate_observed_at") or signal.get("observed_at")
    if value not in (None, ""):
        return str(value)
    return datetime.now(timezone.utc).isoformat()


def _confidence_floor(config: Mapping[str, Any]) -> float:
    for value in (config.get("confidence_floor"), config.get("min_confidence")):
        number = _number(value)
        if number is not None:
            return float(number)
    floor_cfg = _mapping(config.get("shadow_confidence_floor"))
    number = _number(floor_cfg.get("confidence_floor"), floor_cfg.get("min_confidence"))
    return float(number if number is not None else DEFAULT_CONFIDENCE_FLOOR)


def _candidate_city(signal: Mapping[str, Any]) -> str:
    for key in ("city", "weather_city", "station_city"):
        value = signal.get(key)
        if value not in (None, ""):
            return str(value).strip().lower()
    return ""


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
    "write_paper_shadow_lane_decisions",
]
