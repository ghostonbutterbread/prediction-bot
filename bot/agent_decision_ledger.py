from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from bot.file_ops import load_jsonl
from bot.shared_market_feed import shared_candidate_id_from_row

AGENT_RUN_SCHEMA_NAME = "agent_run"
AGENT_DECISION_SCHEMA_NAME = "agent_decision"
SCHEMA_VERSION = 1


def build_agent_run_id(
    *,
    agent_id: str,
    run_id: str,
) -> str:
    return f"{str(agent_id or 'unknown')}:{str(run_id or 'unknown')}"


def build_agent_run_row(
    *,
    agent_id: str,
    runtime: str,
    policy: str,
    mode: str,
    run_id: str,
    started_at: str | datetime,
    finished_at: str | datetime | None,
    status: str,
    candidate_dataset_path: str | Path | None,
    decision_ledger_path: str | Path | None,
    mutates_accounting: bool,
    accounting_namespace: str | Path | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    row = {
        "schema_name": AGENT_RUN_SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "agent_run_id": build_agent_run_id(agent_id=agent_id, run_id=run_id),
        "run_id": str(run_id or "unknown"),
        "agent_id": str(agent_id or "unknown"),
        "runtime": str(runtime or "unknown"),
        "policy": str(policy or "unknown"),
        "mode": str(mode or "unknown"),
        "started_at": _iso_timestamp(started_at),
        "finished_at": _iso_timestamp(finished_at),
        "status": str(status or "unknown"),
        "candidate_dataset_path": _optional_path(candidate_dataset_path),
        "decision_ledger_path": _optional_path(decision_ledger_path),
        "accounting_namespace": _optional_path(accounting_namespace),
        "mutates_accounting": bool(mutates_accounting),
        "notes": str(notes) if notes not in (None, "") else None,
    }
    return validate_agent_run_row(row)


def validate_agent_run_row(row: dict[str, Any]) -> dict[str, Any]:
    validated = dict(row or {})
    validated["schema_name"] = AGENT_RUN_SCHEMA_NAME
    validated["schema_version"] = SCHEMA_VERSION
    for key in ("agent_run_id", "run_id", "agent_id", "runtime", "policy", "mode", "started_at", "status"):
        if validated.get(key) in (None, ""):
            raise ValueError(f"agent run row missing required field: {key}")
    return _json_safe(validated)


def build_agent_decision_rows_from_source_row(
    source_row: dict[str, Any],
    *,
    agent_run_id: str,
    agent_id: str,
    runtime: str,
    candidate_dataset_path: str | Path | None,
    decided_at: str | datetime | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(source_row, dict):
        raise ValueError("source_row must be a dict")
    link = shared_candidate_link_from_row(source_row, candidate_dataset_path=candidate_dataset_path)
    if link["shared_candidate_id"] in (None, ""):
        raise ValueError("source_row is missing shared_candidate_id")

    role_rows = _decision_role_rows(source_row, candidate_dataset_path=candidate_dataset_path)
    if not role_rows:
        role_rows = [("main", _fallback_decision_from_row(source_row))]

    decided_iso = _iso_timestamp(decided_at or source_row.get("observed_at") or source_row.get("timestamp"))
    observed_at = _iso_timestamp(source_row.get("observed_at") or source_row.get("timestamp"))
    mutation_contract = _mutation_contract_from_row(source_row)
    provenance = _provenance_from_row(source_row)
    rows: list[dict[str, Any]] = []
    for decision_role, decision in role_rows:
        decision_mutation_contract = decision.get("mutation_contract") if isinstance(decision.get("mutation_contract"), dict) else mutation_contract
        entry_price = _decision_entry_price(source_row, decision)
        rows.append(
            validate_agent_decision_row(
                {
                    "schema_name": AGENT_DECISION_SCHEMA_NAME,
                    "schema_version": SCHEMA_VERSION,
                    "decision_id": build_agent_decision_id(
                        agent_run_id=agent_run_id,
                        agent_id=agent_id,
                        runtime=runtime,
                        policy=str(decision.get("policy") or "unknown"),
                        decision_role=decision_role,
                        shared_candidate_id=link["shared_candidate_id"],
                        run_id=link["run_id"],
                        market_id=link["market_id"],
                        observed_at=observed_at,
                    ),
                    "agent_run_id": agent_run_id,
                    "agent_id": str(agent_id or "unknown"),
                    "runtime": str(runtime or "unknown"),
                    "policy": str(decision.get("policy") or "unknown"),
                    "decision_role": decision_role,
                    "shared_candidate_id": link["shared_candidate_id"],
                    "candidate_dataset_path": link["candidate_dataset_path"],
                    "run_id": link["run_id"],
                    "market_id": link["market_id"],
                    "observed_at": observed_at,
                    "decided_at": decided_iso,
                    "action": decision.get("action"),
                    "side": decision.get("side") or _side_from_action(decision.get("action")),
                    "requested_position_size_usd": _coerce_number(decision.get("requested_position_size"), decision.get("size")),
                    "approved_position_size_usd": _coerce_number(decision.get("approved_position_size")),
                    "reason_code": decision.get("reason_code"),
                    "reason": decision.get("reason"),
                    "selected_lane": decision.get("selected_lane"),
                    "confidence": source_row.get("confidence"),
                    "edge": source_row.get("edge"),
                    "model_probability": source_row.get("model_probability"),
                    "entry_price": entry_price,
                    "price": entry_price,
                    "accounting_ref": decision.get("accounting_ref") if isinstance(decision.get("accounting_ref"), dict) else None,
                    "mutation_contract": decision_mutation_contract,
                    "provenance": provenance,
                }
            )
        )
    return rows


def validate_agent_decision_row(row: dict[str, Any]) -> dict[str, Any]:
    return _validate_agent_decision_row(row, allow_legacy_identity=False)


def validate_agent_decision_row_with_legacy_identity(row: dict[str, Any]) -> dict[str, Any]:
    return _validate_agent_decision_row(row, allow_legacy_identity=True)


def _validate_agent_decision_row(row: dict[str, Any], *, allow_legacy_identity: bool) -> dict[str, Any]:
    validated = dict(row or {})
    validated["schema_name"] = AGENT_DECISION_SCHEMA_NAME
    validated["schema_version"] = SCHEMA_VERSION
    for key in (
        "decision_id",
        "agent_run_id",
        "agent_id",
        "runtime",
        "policy",
        "decision_role",
        "run_id",
        "market_id",
        "observed_at",
        "decided_at",
        "candidate_dataset_path",
    ):
        if validated.get(key) in (None, ""):
            raise ValueError(f"agent decision row missing required field: {key}")
    if validated.get("shared_candidate_id") in (None, ""):
        legacy_identity = validated.get("legacy_candidate_identity")
        if not allow_legacy_identity or not isinstance(legacy_identity, dict) or not legacy_identity:
            raise ValueError("agent decision row missing shared_candidate_id")
    return _json_safe(validated)


def build_agent_decision_id(
    *,
    agent_run_id: str,
    agent_id: str,
    runtime: str,
    policy: str,
    decision_role: str,
    shared_candidate_id: str,
    run_id: str,
    market_id: str,
    observed_at: str | None,
    legacy_candidate_identity: dict[str, Any] | None = None,
) -> str:
    stable_payload = {
        "agent_run_id": str(agent_run_id or ""),
        "agent_id": str(agent_id or ""),
        "runtime": str(runtime or ""),
        "policy": str(policy or ""),
        "decision_role": str(decision_role or ""),
        "shared_candidate_id": str(shared_candidate_id or ""),
        "run_id": str(run_id or ""),
        "market_id": str(market_id or ""),
        "observed_at": str(observed_at or ""),
        "legacy_candidate_identity": legacy_candidate_identity or {},
    }
    digest = hashlib.sha256(json.dumps(stable_payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return f"{str(agent_id or 'unknown')}:{str(run_id or 'unknown')}:{str(decision_role or 'unknown')}:{digest}"


def shared_candidate_link_from_row(
    row: dict[str, Any],
    *,
    candidate_dataset_path: str | Path | None = None,
) -> dict[str, str | None]:
    return {
        "shared_candidate_id": shared_candidate_id_from_row(row),
        "run_id": str(row.get("run_id") or ""),
        "market_id": str(row.get("market_id") or row.get("snapshot_key") or ""),
        "candidate_dataset_path": _optional_path(candidate_dataset_path),
    }


def load_agent_decision_rows(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path_value in paths:
        path = Path(path_value)
        if not path.exists():
            continue
        rows.extend(load_jsonl(path))
    return rows


def summarize_agent_decision_coverage(
    rows: Iterable[dict[str, Any]],
    *,
    shared_candidate_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    requested_ids = {str(value) for value in (shared_candidate_ids or []) if str(value)}
    filter_by_candidate = shared_candidate_ids is not None
    filtered_rows: list[dict[str, Any]] = []
    unmatched_input_rows = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        shared_candidate_id = shared_candidate_id_from_row(row)
        if filter_by_candidate and shared_candidate_id not in requested_ids:
            unmatched_input_rows += 1
            continue
        filtered_rows.append(row)

    by_shared_candidate_id = _counts(shared_candidate_id_from_row(row) or "missing" for row in filtered_rows)
    matched_candidate_ids = {key for key in by_shared_candidate_id if key != "missing"}
    return {
        "schema_version": SCHEMA_VERSION,
        "total_rows": len(filtered_rows),
        "distinct_shared_candidate_ids": len(matched_candidate_ids),
        "requested_shared_candidate_ids": len(requested_ids) if filter_by_candidate else None,
        "matched_shared_candidate_ids": len(matched_candidate_ids) if filter_by_candidate else None,
        "missing_shared_candidate_ids": sorted(requested_ids - matched_candidate_ids) if filter_by_candidate else [],
        "unmatched_input_rows": unmatched_input_rows if filter_by_candidate else 0,
        "by_shared_candidate_id": by_shared_candidate_id,
        "by_agent_id": _counts(str(row.get("agent_id") or "unknown") for row in filtered_rows),
        "by_runtime": _counts(str(row.get("runtime") or "unknown") for row in filtered_rows),
        "by_policy": _counts(str(row.get("policy") or "unknown") for row in filtered_rows),
        "by_decision_role": _counts(str(row.get("decision_role") or "unknown") for row in filtered_rows),
    }


def _decision_role_rows(source_row: dict[str, Any], *, candidate_dataset_path: str | Path | None = None) -> list[tuple[str, dict[str, Any]]]:
    role_rows: list[tuple[str, dict[str, Any]]] = []
    for decision_role, key in (("main", "main_decision"), ("normal", "normal_decision"), ("shadow", "shadow_decision")):
        value = source_row.get(key)
        if isinstance(value, dict):
            role_rows.append((decision_role, dict(value)))
    prediction_lab_paper = _prediction_lab_paper_decision_from_row(source_row, candidate_dataset_path=candidate_dataset_path)
    if prediction_lab_paper is not None:
        role_rows.append(("prediction_lab_paper", prediction_lab_paper))
    return role_rows


def _prediction_lab_paper_decision_from_row(source_row: dict[str, Any], *, candidate_dataset_path: str | Path | None = None) -> dict[str, Any] | None:
    artifact = _dict_value(source_row.get("decision_artifact"))
    shared_pipeline = _dict_value(source_row.get("shared_pipeline"))
    if not artifact and not shared_pipeline:
        return None

    explicit_paper_lab, explicit_opportunity = _explicit_prediction_lab_paper_metadata(
        artifact=artifact,
        shared_pipeline=shared_pipeline,
    )
    if not _is_prediction_lab_paper_metadata(paper_lab=explicit_paper_lab, opportunity=explicit_opportunity):
        return None

    paper_lab = {**explicit_paper_lab, **_dict_value(source_row.get("paper_lab"))}
    opportunity = {**explicit_opportunity, **_dict_value(source_row.get("opportunity_mode"))}
    shared_decision = _dict_value(artifact.get("shared_core_decision"))
    normal_decision = _dict_value(source_row.get("normal_decision"))
    signal = _dict_value(artifact.get("strategy_signal"))
    action = _first_present(
        artifact.get("final_action"),
        shared_pipeline.get("final_action"),
        shared_decision.get("action"),
        normal_decision.get("action"),
        signal.get("direction"),
        source_row.get("direction"),
    )
    reason_code = _first_present(
        artifact.get("final_reason_code"),
        shared_pipeline.get("final_reason_code"),
        shared_decision.get("reason_code"),
        normal_decision.get("reason_code"),
        source_row.get("skip_reason_code"),
    )
    if action in (None, "") and reason_code in (None, ""):
        return None

    requested_size = _first_present(
        shared_decision.get("requested_position_size"),
        shared_pipeline.get("requested_position_size_usd"),
        normal_decision.get("requested_position_size"),
        normal_decision.get("size"),
        _dict_value(opportunity.get("kelly")).get("requested_position_size_usd"),
        _dict_value(opportunity.get("kelly")).get("requested_size"),
    )
    approved_size = _first_present(
        shared_decision.get("position_size"),
        shared_decision.get("approved_position_size"),
        shared_pipeline.get("approved_position_size_usd"),
        normal_decision.get("approved_position_size"),
        normal_decision.get("size"),
        _dict_value(opportunity.get("kelly")).get("approved_position_size_usd"),
    )
    entry_price = _first_present(shared_decision.get("entry_price"), shared_decision.get("price"))
    return {
        "policy": _prediction_lab_paper_policy(paper_lab=paper_lab, opportunity=opportunity),
        "action": action,
        "side": _side_from_action(action),
        "reason_code": reason_code,
        "reason": _first_present(artifact.get("final_reason"), shared_decision.get("reason"), normal_decision.get("reason")),
        "requested_position_size": requested_size,
        "approved_position_size": approved_size,
        "selected_lane": _first_present(normal_decision.get("selected_lane"), _selected_lane_from_shared_decision(shared_decision)),
        "entry_price": entry_price,
        "price": entry_price,
        "accounting_ref": _prediction_lab_paper_accounting_ref(
            candidate_dataset_path=candidate_dataset_path,
            source_row=source_row,
            paper_lab=paper_lab,
            opportunity=opportunity,
        ),
        "mutation_contract": {
            "mutates_shared_candidate": False,
            "mutates_accounting": False,
            "places_orders": False,
        },
    }


def _explicit_prediction_lab_paper_metadata(
    *,
    artifact: dict[str, Any],
    shared_pipeline: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    paper_lab = _dict_value(artifact.get("paper_lab"))
    opportunity = _dict_value(artifact.get("opportunity_mode"))
    if shared_pipeline:
        opportunity = {
            **{
                key: shared_pipeline.get(key)
                for key in (
                    "account_state_provider",
                    "bankroll_usd",
                    "kelly",
                    "mutates_portfolio_account",
                )
                if shared_pipeline.get(key) not in (None, "")
            },
            **({"mode": shared_pipeline.get("opportunity_mode")} if shared_pipeline.get("opportunity_mode") not in (None, "") else {}),
            **opportunity,
        }
    return paper_lab, opportunity


def _is_prediction_lab_paper_metadata(*, paper_lab: dict[str, Any], opportunity: dict[str, Any]) -> bool:
    mutates_portfolio = _first_present(opportunity.get("mutates_portfolio_account"), paper_lab.get("mutates_portfolio_account"))
    if _truthy_metadata_flag(mutates_portfolio):
        return False
    if mutates_portfolio is None:
        return False
    paper_mode = str(_first_present(paper_lab.get("mode"), paper_lab.get("paper_lab_mode"), opportunity.get("paper_lab_mode")) or "").lower()
    opportunity_mode = str(_first_present(opportunity.get("mode"), paper_lab.get("paper_lab_mode")) or "").lower()
    provider = str(_first_present(opportunity.get("account_state_provider"), paper_lab.get("account_state_provider")) or "").lower()
    return (
        paper_mode in {"opportunity", "fixed_opportunity"}
        or opportunity_mode in {"opportunity", "fresh_kelly", "fixed_opportunity"}
        or provider == "fixed_opportunity"
    )


def _prediction_lab_paper_policy(*, paper_lab: dict[str, Any], opportunity: dict[str, Any]) -> str:
    return str(
        _first_present(
            opportunity.get("mode"),
            paper_lab.get("paper_lab_mode"),
            paper_lab.get("mode"),
            "prediction_lab_paper",
        )
    )


def _prediction_lab_paper_accounting_ref(
    *,
    candidate_dataset_path: Any,
    source_row: dict[str, Any],
    paper_lab: dict[str, Any],
    opportunity: dict[str, Any],
) -> dict[str, Any]:
    dataset_path = candidate_dataset_path or source_row.get("candidate_dataset_path")
    namespace = _prediction_lab_paper_accounting_namespace(dataset_path)
    return {
        "namespace": str(namespace),
        "mutates_balance": False,
        "mutates_accounting": False,
        "places_orders": False,
        "balance_model": _prediction_lab_paper_balance_model(paper_lab=paper_lab, opportunity=opportunity),
        "account_state_provider": _first_present(opportunity.get("account_state_provider"), paper_lab.get("account_state_provider")),
    }


def _prediction_lab_paper_accounting_namespace(candidate_dataset_path: Any) -> Path:
    if candidate_dataset_path not in (None, ""):
        path = Path(candidate_dataset_path)
        parent = path.parent if path.name else path
        return parent / "paper_accounting"
    return Path("prediction_lab") / "paper_accounting"


def _prediction_lab_paper_balance_model(*, paper_lab: dict[str, Any], opportunity: dict[str, Any]) -> str:
    provider = str(_first_present(opportunity.get("account_state_provider"), paper_lab.get("account_state_provider")) or "").lower()
    opportunity_mode = str(opportunity.get("mode") or "").lower()
    if provider == "fixed_opportunity" or opportunity_mode == "opportunity":
        return "fixed_opportunity"
    notional_mode = str(_first_present(paper_lab.get("hypothetical_notional_mode"), paper_lab.get("paper_lab_mode")) or "").lower()
    if notional_mode in {"flat", "fixed", "fixed_notional"}:
        return "fixed_notional"
    return "infinite_hypothetical"


def _selected_lane_from_shared_decision(shared_decision: dict[str, Any]) -> Any:
    reasoning = _dict_value(shared_decision.get("reasoning"))
    if reasoning.get("selected_lane") not in (None, ""):
        return reasoning.get("selected_lane")
    lane_sizing = _dict_value(reasoning.get("lane_sizing"))
    return _first_present(lane_sizing.get("selected_lane"), lane_sizing.get("lane"))


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _truthy_metadata_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _fallback_decision_from_row(source_row: dict[str, Any]) -> dict[str, Any]:
    return {
        "policy": "normal",
        "action": source_row.get("direction") or _direction_from_decision_type(source_row.get("decision_type")),
        "side": _side_from_action(source_row.get("direction")),
        "reason_code": _source_row_reason_code(source_row),
        "reason": _source_row_reason(source_row),
        "requested_position_size": None,
        "selected_lane": None,
    }


def _mutation_contract_from_row(source_row: dict[str, Any]) -> dict[str, Any]:
    return {
        "mutates_shared_candidate": False,
        "mutates_accounting": bool(_paper_lab_flag(source_row, "mutates_portfolio_account")),
        "places_orders": bool(source_row.get("order_execution_enabled")),
    }


def _provenance_from_row(source_row: dict[str, Any]) -> dict[str, Any]:
    source = "live_known_at_time"
    shared = source_row.get("shared_candidate")
    if isinstance(shared, dict) and shared.get("provenance") not in (None, ""):
        source = str(shared.get("provenance"))
    return {
        "known_at_time": source != "historical_post_facto",
        "source": source,
    }


def _decision_entry_price(source_row: dict[str, Any], decision: dict[str, Any]) -> float | int | None:
    for key in ("entry_price", "market_price", "price"):
        value = _coerce_number(decision.get(key))
        if value is not None:
            return value
    action = str(decision.get("action") or source_row.get("direction") or "").upper()
    side = str(decision.get("side") or _side_from_action(action) or "").upper()
    if side == "YES" or action == "BUY_YES":
        return _coerce_number(source_row.get("yes_market_price"), source_row.get("yes_price"), source_row.get("market_price"))
    if side == "NO" or action == "BUY_NO":
        return _coerce_number(source_row.get("no_market_price"), source_row.get("no_price"), source_row.get("market_price"))
    return _coerce_number(source_row.get("market_price"))


def _source_row_reason_code(source_row: dict[str, Any]) -> Any:
    shared_pipeline = source_row.get("shared_pipeline")
    if isinstance(shared_pipeline, dict) and shared_pipeline.get("final_reason_code") not in (None, ""):
        return shared_pipeline.get("final_reason_code")
    artifact = source_row.get("decision_artifact")
    if isinstance(artifact, dict) and artifact.get("final_reason_code") not in (None, ""):
        return artifact.get("final_reason_code")
    return source_row.get("skip_reason_code")


def _source_row_reason(source_row: dict[str, Any]) -> Any:
    artifact = source_row.get("decision_artifact")
    if isinstance(artifact, dict) and artifact.get("final_reason") not in (None, ""):
        return artifact.get("final_reason")
    return None


def _paper_lab_flag(source_row: dict[str, Any], key: str) -> Any:
    paper_lab = source_row.get("paper_lab")
    if isinstance(paper_lab, dict):
        return paper_lab.get(key)
    return None


def _direction_from_decision_type(value: Any) -> str:
    text = str(value or "").lower()
    if text == "buy_yes":
        return "BUY_YES"
    if text == "buy_no":
        return "BUY_NO"
    return "SKIP"


def _side_from_action(value: Any) -> str | None:
    text = str(value or "").upper()
    if "YES" in text:
        return "YES"
    if "NO" in text:
        return "NO"
    return None


def _iso_timestamp(value: str | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    text = str(value).strip()
    return text or None


def _optional_path(value: str | Path | None) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _coerce_number(*values: Any) -> float | int | None:
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


def _counts(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_safe(inner) for key, inner in value.items() if inner is not None}
    if isinstance(value, list):
        return [_json_safe(inner) for inner in value]
    if isinstance(value, tuple):
        return [_json_safe(inner) for inner in value]
    return value
