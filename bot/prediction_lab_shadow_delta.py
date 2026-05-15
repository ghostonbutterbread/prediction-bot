from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from math import isfinite
from typing import Any, Iterable

from bot.strategy_policy import coerce_strategy_policy, strategy_policy_status


def build_shadow_delta(
    decision_artifact: dict[str, Any],
    market_id: str,
    run_id: str,
    fallback_strategy_policy: Any = None,
) -> dict[str, Any] | None:
    """Build compact beta/shadow comparison metadata from a decision artifact."""
    if not isinstance(decision_artifact, dict):
        return None

    policy_status = _artifact_strategy_policy_status(
        decision_artifact,
        fallback_strategy_policy=fallback_strategy_policy,
    )
    if not (
        policy_status.get("version") == "beta"
        and policy_status.get("mode") == "shadow"
        and policy_status.get("shadow") is True
    ):
        return None

    shared_decision = (
        decision_artifact.get("shared_core_decision")
        if isinstance(decision_artifact.get("shared_core_decision"), dict)
        else {}
    )
    reasoning = shared_decision.get("reasoning") if isinstance(shared_decision.get("reasoning"), dict) else {}
    stable = _shadow_delta_side(
        action=decision_artifact.get("final_action"),
        reason_code=decision_artifact.get("final_reason_code"),
        requested_position_size=shared_decision.get("requested_position_size"),
        selected_lane=_stable_selected_lane(reasoning),
    )
    shadow = dict(stable)
    evidence_sources: list[str] = []
    status = "complete"
    comparison_complete = True
    action_comparison_available = True

    beta_lane_gate = _beta_lane_gate(reasoning)
    if beta_lane_gate and (
        beta_lane_gate.get("allowed") is False
        or beta_lane_gate.get("differs_from_final") is True
    ):
        evidence_sources.append("beta_lane_gate")
        shadow["selected_lane"] = beta_lane_gate.get("lane_id") or shadow.get("selected_lane")
        if beta_lane_gate.get("allowed") is False:
            shadow.update(
                _shadow_delta_side(
                    action="SKIP",
                    reason_code=beta_lane_gate.get("reason_code") or "strategy_lane_disabled",
                    requested_position_size=None,
                    selected_lane=shadow.get("selected_lane"),
                )
            )
        elif stable.get("action") == "SKIP" and beta_lane_gate.get("allowed") is True:
            status = "partial_beta_evidence"
            comparison_complete = False
            action_comparison_available = False
            shadow = _partial_shadow_delta_side(
                reason_code=beta_lane_gate.get("reason_code"),
                requested_position_size=None,
                selected_lane=shadow.get("selected_lane"),
            )

    weather_beta_gate = _weather_beta_gate(reasoning, "beta_gate")
    if weather_beta_gate:
        evidence_sources.append("weather_risk.beta_gate")
        if weather_beta_gate.get("would_reject"):
            status = "complete"
            comparison_complete = True
            action_comparison_available = True
            shadow.update(
                _shadow_delta_side(
                    action="SKIP",
                    reason_code=weather_beta_gate.get("reason_code") or "weather_risk_rejected",
                    requested_position_size=None,
                    selected_lane=shadow.get("selected_lane"),
                )
            )

    if action_comparison_available and shadow.get("action") != "SKIP":
        lane_sizing = reasoning.get("lane_sizing") if isinstance(reasoning.get("lane_sizing"), dict) else {}
        if _active_shadow_gate(lane_sizing) and lane_sizing.get("beta_adjusted_size") is not None:
            evidence_sources.append("lane_sizing")
            shadow["requested_position_size"] = _coerce_finite_number(lane_sizing.get("beta_adjusted_size"))

        weather_sizing_gate = _weather_beta_gate(reasoning, "beta_sizing_gate")
        if weather_sizing_gate and weather_sizing_gate.get("beta_adjusted_size") is not None:
            evidence_sources.append("weather_risk.beta_sizing_gate")
            shadow["requested_position_size"] = _coerce_finite_number(weather_sizing_gate.get("beta_adjusted_size"))

    if not evidence_sources:
        return None

    if action_comparison_available:
        action_changed: bool | None = stable.get("action") != shadow.get("action")
        side_changed: bool | None = stable.get("direction") != shadow.get("direction")
        buy_decision_changed: bool | None = (stable.get("action") != "SKIP") != (shadow.get("action") != "SKIP")
        reason_changed: bool | None = stable.get("reason_code") != shadow.get("reason_code")
        size_changed: bool | None = stable.get("requested_position_size") != shadow.get("requested_position_size")
    else:
        action_changed = None
        side_changed = None
        buy_decision_changed = None
        reason_changed = None
        size_changed = None
    lane_changed = stable.get("selected_lane") != shadow.get("selected_lane")
    known_changes = [
        value
        for value in (action_changed, side_changed, buy_decision_changed, reason_changed, size_changed, lane_changed)
        if value is not None
    ]
    if action_comparison_available:
        changed = any(known_changes) if known_changes else False
    else:
        changed = True if any(known_changes) else None

    return {
        "schema_version": 1,
        "mode": "beta_shadow_delta",
        "status": status,
        "comparison_complete": comparison_complete,
        "action_comparison_available": action_comparison_available,
        "policy": {
            "version": policy_status.get("version"),
            "mode": policy_status.get("mode"),
            "enabled_features": [
                name
                for name, enabled in sorted((policy_status.get("enabled_features") or {}).items())
                if enabled is True
            ],
        },
        "stable": stable,
        "shadow": shadow,
        "changed": changed,
        "action_changed": action_changed,
        "side_changed": side_changed,
        "buy_decision_changed": buy_decision_changed,
        "reason_changed": reason_changed,
        "size_changed": size_changed,
        "lane_changed": lane_changed,
        "dedupe_key": f"{market_id}|{run_id}|beta-shadow",
        "evidence_sources": evidence_sources,
    }


def summarize_shadow_delta_rows(
    rows: Iterable[dict[str, Any]],
    *,
    prediction_lab_rows: bool = False,
) -> dict[str, Any]:
    """Summarize top-level row shadow deltas without creating synthetic rows."""
    total_rows = 0
    keyed: dict[str, dict[str, Any]] = {}
    unkeyed: list[dict[str, Any]] = []

    for row in rows or []:
        if not isinstance(row, dict):
            continue
        shadow_delta = row.get("shadow_delta")
        if not isinstance(shadow_delta, dict) or not shadow_delta:
            continue
        total_rows += 1
        key = _shadow_delta_summary_key(row, shadow_delta, prediction_lab_rows=prediction_lab_rows)
        if key is None:
            unkeyed.append(row)
            continue
        existing = keyed.get(key)
        if existing is None or _prefer_shadow_delta_summary_row(row, existing):
            keyed[key] = row

    opportunity_rows = list(keyed.values()) + unkeyed
    action_available_rows = [
        row
        for row in opportunity_rows
        if _shadow_delta(row).get("action_comparison_available") is True
    ]
    unavailable_action_rows = len(opportunity_rows) - len(action_available_rows)
    changed_counts = _shadow_delta_changed_counts(opportunity_rows)
    summary = {
        "schema_version": 1,
        "basis": "row_level_shadow_delta_deduped",
        "total_shadow_delta_rows": total_rows,
        "shadow_delta_rows": total_rows,
        "total_shadow_delta_opportunities": len(opportunity_rows),
        "shadow_delta_opportunities": len(opportunity_rows),
        "keyed_opportunities": len(keyed),
        "unkeyed_opportunities": len(unkeyed),
        "deduped_duplicate_rows": total_rows - len(opportunity_rows),
        "status_counts": _counts(_coerce_label(_shadow_delta(row).get("status"), "unknown") for row in opportunity_rows),
        "changed_counts": changed_counts,
        "changed_rows": changed_counts.get("changed", 0),
        "unchanged_rows": changed_counts.get("unchanged", 0),
        "unknown_changed_rows": changed_counts.get("unknown", 0),
        "action_comparison_available_rows": len(action_available_rows),
        "unavailable_action_comparisons": unavailable_action_rows,
        "action_comparison_unavailable_rows": unavailable_action_rows,
        "action_changed": _bool_count(opportunity_rows, "action_changed", available_only=True),
        "action_unchanged": _bool_count(opportunity_rows, "action_changed", false_only=True, available_only=True),
        "side_changed": _bool_count(opportunity_rows, "side_changed", available_only=True),
        "side_unchanged": _bool_count(opportunity_rows, "side_changed", false_only=True, available_only=True),
        "buy_decision_changed": _bool_count(opportunity_rows, "buy_decision_changed", available_only=True),
        "buy_decision_unchanged": _bool_count(opportunity_rows, "buy_decision_changed", false_only=True, available_only=True),
        "reason_changed": _bool_count(opportunity_rows, "reason_changed"),
        "reason_unchanged": _bool_count(opportunity_rows, "reason_changed", false_only=True),
        "size_changed": _bool_count(opportunity_rows, "size_changed"),
        "size_unchanged": _bool_count(opportunity_rows, "size_changed", false_only=True),
        "lane_changed": _bool_count(opportunity_rows, "lane_changed"),
        "lane_unchanged": _bool_count(opportunity_rows, "lane_changed", false_only=True),
        "stable_action_counts": _counts(
            _coerce_label((_shadow_delta(row).get("stable") or {}).get("action"), "unknown")
            for row in opportunity_rows
            if isinstance(_shadow_delta(row).get("stable"), dict)
        ),
        "shadow_action_counts": _counts(
            _coerce_label((_shadow_delta(row).get("shadow") or {}).get("action"), "unknown")
            for row in opportunity_rows
            if isinstance(_shadow_delta(row).get("shadow"), dict)
        ),
        "evidence_source_counts": _counts(
            source
            for row in opportunity_rows
            for source in _shadow_delta_evidence_sources(_shadow_delta(row))
        ),
    }
    return summary


def format_shadow_delta_summary(summary: dict[str, Any] | None) -> str | None:
    if not isinstance(summary, dict):
        return None
    opportunities = int(summary.get("total_shadow_delta_opportunities") or summary.get("shadow_delta_opportunities") or 0)
    if opportunities <= 0:
        return None
    raw_rows = int(summary.get("total_shadow_delta_rows") or summary.get("shadow_delta_rows") or opportunities)
    return (
        "Shadow delta: "
        f"{opportunities} opportunities"
        f" ({raw_rows} rows, deduped {int(summary.get('deduped_duplicate_rows') or 0)}) | "
        f"changed {int(summary.get('changed_rows') or 0)} | "
        f"action {int(summary.get('action_changed') or 0)} | "
        f"side {int(summary.get('side_changed') or 0)} | "
        f"buy {int(summary.get('buy_decision_changed') or 0)} | "
        f"reason {int(summary.get('reason_changed') or 0)} | "
        f"size {int(summary.get('size_changed') or 0)} | "
        f"lane {int(summary.get('lane_changed') or 0)} | "
        f"action unavailable {int(summary.get('unavailable_action_comparisons') or 0)}"
    )


def build_shadow_delta_compact_review(
    *,
    predictions_path: str | Path,
    market_snapshots_path: str | Path,
) -> dict[str, Any]:
    """Build deduped compact review rows for meaningful Prediction Lab shadow deltas.

    The export is a read-only comparison view. It intentionally does not create
    prediction, replay, or trade-shaped rows.
    """
    predictions = _require_jsonl_input_path(predictions_path, label="predictions_path")
    market_snapshots = _require_jsonl_input_path(market_snapshots_path, label="market_snapshots_path")
    inputs = [
        ("prediction", predictions),
        ("market_snapshot", market_snapshots),
    ]
    total_input_rows = 0
    total_shadow_delta_rows = 0
    keyed: dict[str, dict[str, Any]] = {}
    keyed_availability: dict[str, dict[str, bool]] = {}
    unkeyed: list[dict[str, Any]] = []

    for source_kind, path in inputs:
        for line_number, raw_row in _iter_jsonl_dict_rows(path):
            total_input_rows += 1
            shadow_delta = raw_row.get("shadow_delta")
            if not isinstance(shadow_delta, dict) or not shadow_delta:
                continue
            total_shadow_delta_rows += 1
            row = dict(raw_row)
            row["_source_path"] = str(path)
            row["_source_kind"] = source_kind
            row["_source_line_number"] = line_number
            key = _shadow_delta_summary_key(row, shadow_delta, prediction_lab_rows=True)
            if key is None:
                unkeyed.append(
                    {
                        "row": row,
                        "source_kind": source_kind,
                        "source_path": str(path),
                        "line_number": line_number,
                        "availability": _review_source_availability(source_kind),
                    }
                )
                continue

            availability = keyed_availability.setdefault(
                key,
                {"prediction_row_available": False, "market_snapshot_row_available": False},
            )
            if source_kind == "prediction":
                availability["prediction_row_available"] = True
            elif source_kind == "market_snapshot":
                availability["market_snapshot_row_available"] = True

            existing = keyed.get(key)
            candidate = {
                "row": row,
                "source_kind": source_kind,
                "source_path": str(path),
                "line_number": line_number,
                "dedupe_key": key,
            }
            if existing is None or _prefer_shadow_delta_summary_row(row, existing["row"]):
                keyed[key] = candidate

    opportunity_items: list[dict[str, Any]] = []
    for key, item in keyed.items():
        opportunity_items.append({**item, "availability": dict(keyed_availability.get(key) or {})})
    opportunity_items.extend(unkeyed)

    exported_rows = [
        _build_shadow_delta_review_row(item)
        for item in opportunity_items
        if _meaningful_shadow_delta(_shadow_delta(item["row"]))
    ]
    exported_rows.sort(key=_shadow_delta_review_sort_key)

    return {
        "schema_version": 1,
        "basis": "prediction_lab_shadow_delta_compact_review",
        "inputs": {
            "predictions_path": str(Path(predictions_path)),
            "market_snapshots_path": str(Path(market_snapshots_path)),
        },
        "total_input_rows": total_input_rows,
        "total_shadow_delta_rows": total_shadow_delta_rows,
        "total_shadow_delta_opportunities": len(opportunity_items),
        "deduped_duplicate_rows": total_shadow_delta_rows - len(opportunity_items),
        "exported_rows": len(exported_rows),
        "excluded_unmeaningful_rows": len(opportunity_items) - len(exported_rows),
        "rows": exported_rows,
    }


def write_shadow_delta_compact_review_jsonl(
    output_path: str | Path,
    *,
    predictions_path: str | Path,
    market_snapshots_path: str | Path,
) -> dict[str, Any]:
    """Write compact review rows as JSONL and return the export summary."""
    path = Path(output_path)
    _reject_output_input_alias(path, [predictions_path, market_snapshots_path])
    result = build_shadow_delta_compact_review(
        predictions_path=predictions_path,
        market_snapshots_path=market_snapshots_path,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in result["rows"]:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    return {key: value for key, value in result.items() if key != "rows"}


def _shadow_delta(row: dict[str, Any]) -> dict[str, Any]:
    shadow_delta = row.get("shadow_delta")
    return shadow_delta if isinstance(shadow_delta, dict) else {}


def _require_jsonl_input_path(path_value: str | Path, *, label: str) -> Path:
    path = Path(path_value)
    if not path.exists():
        raise FileNotFoundError(f"{label} does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"{label} is not a file: {path}")
    return path


def _reject_output_input_alias(output_path: str | Path, input_paths: Iterable[str | Path]) -> None:
    output_key = _path_alias_key(output_path)
    for input_path in input_paths:
        if output_key == _path_alias_key(input_path):
            raise ValueError("shadow-delta review output must be separate from predictions.jsonl and market_snapshots.jsonl inputs")


def _path_alias_key(path_value: str | Path) -> Path:
    return Path(path_value).expanduser().resolve(strict=False)


def _iter_jsonl_dict_rows(path: Path):
    with path.open("r", encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield line_number, row


def _review_source_availability(source_kind: str) -> dict[str, bool]:
    return {
        "prediction_row_available": source_kind == "prediction",
        "market_snapshot_row_available": source_kind == "market_snapshot",
    }


def _meaningful_shadow_delta(shadow_delta: dict[str, Any]) -> bool:
    if not isinstance(shadow_delta, dict) or not shadow_delta:
        return False
    if shadow_delta.get("changed") is True:
        return True
    if shadow_delta.get("status") == "partial_beta_evidence":
        return True
    if shadow_delta.get("action_comparison_available") is False:
        return True
    return any(
        shadow_delta.get(key) is True
        for key in (
            "action_changed",
            "side_changed",
            "buy_decision_changed",
            "reason_changed",
            "size_changed",
            "lane_changed",
        )
    )


def _build_shadow_delta_review_row(item: dict[str, Any]) -> dict[str, Any]:
    row = item["row"]
    shadow_delta = _compact_shadow_delta(_shadow_delta(row))
    artifact = row.get("decision_artifact") if isinstance(row.get("decision_artifact"), dict) else {}
    source_kind = str(item.get("source_kind") or row.get("_source_kind") or "unknown")
    source_path = str(item.get("source_path") or row.get("_source_path") or "")
    availability = dict(item.get("availability") or _review_source_availability(source_kind))
    review_row = {
        "schema_version": 1,
        "row_type": "prediction_lab_shadow_delta_compact_review",
        "market_id": row.get("market_id") or artifact.get("market_id"),
        "run_id": row.get("run_id"),
        "prediction_id": row.get("prediction_id"),
        "observed_at": row.get("observed_at") or artifact.get("observed_at") or row.get("timestamp"),
        "snapshot_key": row.get("snapshot_key"),
        "recorded_prediction": row.get("recorded_prediction"),
        "source_kind": source_kind,
        "source_path": source_path,
        "source_line_number": item.get("line_number") or row.get("_source_line_number"),
        "prediction_row_available": availability.get("prediction_row_available") is True,
        "market_snapshot_row_available": availability.get("market_snapshot_row_available") is True,
        "decision_artifact_available": bool(artifact),
        "decision_artifact_pointer": "decision_artifact" if artifact else None,
        "decision_artifact_mode": artifact.get("mode") if artifact else None,
        "decision_artifact_final_action": artifact.get("final_action") if artifact else None,
        "decision_artifact_final_reason_code": artifact.get("final_reason_code") if artifact else None,
        "shadow_delta": shadow_delta,
    }
    for field_name, value in (
        ("route_metadata", _compact_route_metadata(row, artifact)),
        ("weather_metadata", _compact_weather_metadata(row, artifact)),
        ("source_metadata", _compact_source_metadata(artifact)),
        ("order_book_metadata", _compact_order_book_metadata(artifact)),
    ):
        if value:
            review_row[field_name] = value
    return review_row


def _compact_shadow_delta(shadow_delta: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "schema_version",
        "mode",
        "status",
        "comparison_complete",
        "action_comparison_available",
        "policy",
        "stable",
        "shadow",
        "changed",
        "action_changed",
        "side_changed",
        "buy_decision_changed",
        "reason_changed",
        "size_changed",
        "lane_changed",
        "dedupe_key",
        "evidence_sources",
    )
    return {key: shadow_delta.get(key) for key in keys if key in shadow_delta}


def _compact_route_metadata(row: dict[str, Any], artifact: dict[str, Any]) -> dict[str, Any]:
    source_data = _artifact_source_data(artifact)
    market_metadata = source_data.get("market_metadata") if isinstance(source_data.get("market_metadata"), dict) else {}
    route = row.get("market_route") or artifact.get("market_route") or market_metadata.get("market_route")
    compact = {
        "market_route": route,
        "group": row.get("group") or market_metadata.get("market_group"),
        "series": row.get("series") or market_metadata.get("series"),
        "event_ticker": row.get("event_ticker") or market_metadata.get("event_ticker"),
    }
    return _drop_empty(compact)


def _compact_weather_metadata(row: dict[str, Any], artifact: dict[str, Any]) -> dict[str, Any]:
    source_data = _artifact_source_data(artifact)
    weather_snapshot = (
        source_data.get("weather_source_snapshot")
        if isinstance(source_data.get("weather_source_snapshot"), dict)
        else {}
    )
    weather_risk = row.get("weather_risk") if isinstance(row.get("weather_risk"), dict) else {}
    date_validation = (
        weather_snapshot.get("date_validation")
        if isinstance(weather_snapshot.get("date_validation"), dict)
        else {}
    )
    compact = {
        "weather_source_snapshot_available": bool(weather_snapshot),
        "weather_risk_available": bool(weather_risk),
        "source_name": weather_snapshot.get("source_name"),
        "source_mode": weather_snapshot.get("mode") or weather_snapshot.get("source_mode"),
        "as_of": weather_snapshot.get("as_of") or weather_snapshot.get("source_as_of"),
        "weather_date": weather_snapshot.get("weather_date") or date_validation.get("weather_date"),
        "date_validation": date_validation or None,
        "station_id": weather_snapshot.get("station_id"),
        "station_cli": weather_snapshot.get("station_cli"),
    }
    return _drop_empty(compact, keep_false_keys={"weather_source_snapshot_available", "weather_risk_available"})


def _compact_source_metadata(artifact: dict[str, Any]) -> dict[str, Any]:
    source_context = artifact.get("source_context") if isinstance(artifact.get("source_context"), dict) else {}
    snapshots = artifact.get("source_snapshots") if isinstance(artifact.get("source_snapshots"), list) else []
    compact_snapshots = []
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            continue
        compact_snapshots.append(
            _drop_empty(
                {
                    "mode": snapshot.get("mode"),
                    "source": snapshot.get("source") or snapshot.get("source_name"),
                    "method": snapshot.get("method"),
                    "snapshot_ref": snapshot.get("snapshot_ref"),
                }
            )
        )
    compact = {
        "source_context_available": bool(source_context),
        "source": source_context.get("source"),
        "source_mode": source_context.get("source_mode") or source_context.get("mode"),
        "as_of": source_context.get("as_of"),
        "source_snapshots": [snapshot for snapshot in compact_snapshots if snapshot],
    }
    return _drop_empty(compact, keep_false_keys={"source_context_available"})


def _compact_order_book_metadata(artifact: dict[str, Any]) -> dict[str, Any]:
    compact = {
        "execution_snapshot_source": artifact.get("execution_snapshot_source"),
        "order_book_snapshot_source": _snapshot_envelope_source(artifact, "order_book_snapshot"),
        "order_book_snapshot_available": _snapshot_envelope_available(artifact, "order_book_snapshot"),
        "pre_logic_order_book_snapshot_source": _snapshot_envelope_source(artifact, "pre_logic_order_book_snapshot"),
        "pre_logic_order_book_snapshot_available": _snapshot_envelope_available(artifact, "pre_logic_order_book_snapshot"),
        "post_logic_order_book_snapshot_source": _snapshot_envelope_source(artifact, "post_logic_order_book_snapshot"),
        "post_logic_order_book_snapshot_available": _snapshot_envelope_available(artifact, "post_logic_order_book_snapshot"),
    }
    return _drop_empty(
        compact,
        keep_false_keys={
            "order_book_snapshot_available",
            "pre_logic_order_book_snapshot_available",
            "post_logic_order_book_snapshot_available",
        },
    )


def _artifact_source_data(artifact: dict[str, Any]) -> dict[str, Any]:
    source_context = artifact.get("source_context") if isinstance(artifact.get("source_context"), dict) else {}
    data = source_context.get("data") if isinstance(source_context.get("data"), dict) else {}
    return data


def _snapshot_envelope_source(artifact: dict[str, Any], key: str) -> Any:
    envelope = artifact.get(key) if isinstance(artifact.get(key), dict) else {}
    return envelope.get("source") or envelope.get("mode")


def _snapshot_envelope_available(artifact: dict[str, Any], key: str) -> bool:
    envelope = artifact.get(key) if isinstance(artifact.get(key), dict) else {}
    data = envelope.get("data")
    return isinstance(data, dict) and bool(data)


def _drop_empty(value: dict[str, Any], *, keep_false_keys: set[str] | None = None) -> dict[str, Any]:
    keep_false_keys = keep_false_keys or set()
    return {
        key: item
        for key, item in value.items()
        if item not in (None, "", [])
        and (item is not False or key in keep_false_keys)
        and (item != {} or key in keep_false_keys)
    }


def _shadow_delta_review_sort_key(row: dict[str, Any]) -> tuple[str, str, str, str, str, str, int]:
    return (
        str(row.get("observed_at") or ""),
        str(row.get("market_id") or ""),
        str(row.get("run_id") or ""),
        str(row.get("prediction_id") or ""),
        str(row.get("source_kind") or ""),
        str(row.get("source_path") or ""),
        int(row.get("source_line_number") or 0),
    )


def _shadow_delta_summary_key(
    row: dict[str, Any],
    shadow_delta: dict[str, Any],
    *,
    prediction_lab_rows: bool,
) -> str | None:
    dedupe_key = shadow_delta.get("dedupe_key")
    if dedupe_key not in (None, ""):
        return str(dedupe_key)
    if prediction_lab_rows or _looks_like_prediction_lab_row(row):
        market_id = row.get("market_id")
        run_id = row.get("run_id")
        if market_id not in (None, "") and run_id not in (None, ""):
            return f"{market_id}|{run_id}|beta-shadow"
    return None


def _looks_like_prediction_lab_row(row: dict[str, Any]) -> bool:
    source_path = row.get("_source_path") or row.get("source_path")
    if source_path not in (None, ""):
        name = Path(str(source_path)).name
        if name in {"predictions.jsonl", "market_snapshots.jsonl"}:
            return True
    if row.get("prediction_id") not in (None, ""):
        return True
    return False


def _prefer_shadow_delta_summary_row(candidate: dict[str, Any], existing: dict[str, Any]) -> bool:
    candidate_recorded = candidate.get("recorded_prediction") is True
    existing_recorded = existing.get("recorded_prediction") is True
    if candidate_recorded != existing_recorded:
        return candidate_recorded
    candidate_artifact = _has_populated_decision_artifact(candidate)
    existing_artifact = _has_populated_decision_artifact(existing)
    if candidate_artifact != existing_artifact:
        return candidate_artifact
    return False


def _has_populated_decision_artifact(row: dict[str, Any]) -> bool:
    artifact = row.get("decision_artifact")
    return isinstance(artifact, dict) and bool(artifact)


def _shadow_delta_changed_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        value = _shadow_delta(row).get("changed")
        if value is True:
            counts["changed"] += 1
        elif value is False:
            counts["unchanged"] += 1
        else:
            counts["unknown"] += 1
    return dict(sorted(counts.items()))


def _bool_count(
    rows: list[dict[str, Any]],
    key: str,
    *,
    false_only: bool = False,
    available_only: bool = False,
) -> int:
    count = 0
    for row in rows:
        shadow_delta = _shadow_delta(row)
        if available_only and shadow_delta.get("action_comparison_available") is not True:
            continue
        value = shadow_delta.get(key)
        if false_only:
            count += value is False
        else:
            count += value is True
    return count


def _shadow_delta_evidence_sources(shadow_delta: dict[str, Any]) -> list[str]:
    sources = shadow_delta.get("evidence_sources")
    if not isinstance(sources, list):
        return []
    return [str(source) for source in sources if source not in (None, "")]


def _coerce_label(value: Any, default: str) -> str:
    if value in (None, ""):
        return default
    return str(value)


def _counts(values: Iterable[Any]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for value in values:
        counter[str(value)] += 1
    return dict(sorted(counter.items()))


def _artifact_strategy_policy_status(
    decision_artifact: dict[str, Any],
    *,
    fallback_strategy_policy: Any = None,
) -> dict[str, Any]:
    shared_decision = (
        decision_artifact.get("shared_core_decision")
        if isinstance(decision_artifact.get("shared_core_decision"), dict)
        else {}
    )
    reasoning = shared_decision.get("reasoning") if isinstance(shared_decision.get("reasoning"), dict) else {}
    for value in (
        reasoning.get("strategy_policy_status"),
        _nested_get(reasoning, ("strategy_lane", "evidence", "strategy_policy")),
        _nested_get(reasoning, ("lane_sizing", "policy")),
        _nested_get(reasoning, ("weather_risk", "beta_gate", "policy")),
        _nested_get(reasoning, ("weather_risk", "beta_sizing_gate", "policy")),
    ):
        if isinstance(value, dict) and value:
            return _normalize_policy_status(value)
    return strategy_policy_status(coerce_strategy_policy(fallback_strategy_policy))


def _normalize_policy_status(value: dict[str, Any]) -> dict[str, Any]:
    status = strategy_policy_status(coerce_strategy_policy(value))
    enabled_features = value.get("enabled_features")
    if isinstance(enabled_features, dict):
        status["enabled_features"] = {str(key): enabled is True for key, enabled in enabled_features.items()}
    return status


def _nested_get(root: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = root
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _stable_selected_lane(reasoning: dict[str, Any]) -> str | None:
    strategy_lane = reasoning.get("strategy_lane") if isinstance(reasoning.get("strategy_lane"), dict) else {}
    lane_id = strategy_lane.get("lane_id")
    return str(lane_id) if lane_id not in (None, "") else None


def _beta_lane_gate(reasoning: dict[str, Any]) -> dict[str, Any] | None:
    gate = _nested_get(reasoning, ("strategy_lane", "evidence", "beta_lane_gate"))
    if not isinstance(gate, dict):
        return None
    if _active_shadow_gate(gate):
        return gate
    if gate.get("beta_behavior_enabled") is True and gate.get("beta_behavior_enforced") is not True:
        return gate
    return None


def _weather_beta_gate(reasoning: dict[str, Any], name: str) -> dict[str, Any] | None:
    gate = _nested_get(reasoning, ("weather_risk", name))
    if isinstance(gate, dict) and _active_shadow_gate(gate):
        return gate
    return None


def _active_shadow_gate(gate: dict[str, Any]) -> bool:
    return bool(gate.get("active") is True and gate.get("shadow") is True and gate.get("enforced") is not True)


def _shadow_delta_side(
    *,
    action: Any,
    reason_code: Any,
    requested_position_size: Any,
    selected_lane: Any,
) -> dict[str, Any]:
    normalized_action = str(action or "SKIP").upper()
    if normalized_action not in {"BUY_YES", "BUY_NO"}:
        normalized_action = "SKIP"
    requested_size = _coerce_finite_number(requested_position_size)
    if normalized_action == "SKIP":
        requested_size = None
    return {
        "action": normalized_action,
        "reason_code": str(reason_code) if reason_code not in (None, "") else None,
        "direction": normalized_action if normalized_action in {"BUY_YES", "BUY_NO"} else "SKIP",
        "decision_type": _decision_type_for_action(normalized_action),
        "requested_position_size": requested_size,
        "selected_lane": str(selected_lane) if selected_lane not in (None, "") else None,
    }


def _partial_shadow_delta_side(
    *,
    reason_code: Any,
    requested_position_size: Any,
    selected_lane: Any,
) -> dict[str, Any]:
    return {
        "action": None,
        "reason_code": str(reason_code) if reason_code not in (None, "") else None,
        "direction": None,
        "decision_type": "unknown",
        "requested_position_size": _coerce_finite_number(requested_position_size),
        "selected_lane": str(selected_lane) if selected_lane not in (None, "") else None,
    }


def _decision_type_for_action(action: str) -> str:
    if action == "BUY_YES":
        return "buy_yes"
    if action == "BUY_NO":
        return "buy_no"
    return "skip"


def _coerce_finite_number(value: Any) -> float | None:
    try:
        if value is None:
            return None
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(numeric):
        return None
    return round(numeric, 4)


__all__ = [
    "build_shadow_delta",
    "build_shadow_delta_compact_review",
    "format_shadow_delta_summary",
    "summarize_shadow_delta_rows",
    "write_shadow_delta_compact_review_jsonl",
]
