from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from bot.agent_decision_ledger import (
    build_agent_decision_id,
    build_agent_decision_rows_from_source_row,
    build_agent_run_id,
    build_agent_run_row,
    validate_agent_decision_row_with_legacy_identity,
)
from bot.file_ops import atomic_write_json, rewrite_jsonl
from bot.shared_market_feed import shared_candidate_id_from_row

BACKFILL_AGENT_ID = "prediction_lab_legacy_backfill"
BACKFILL_RUNTIME = "backfill"
BACKFILL_POLICY = "legacy_compatibility"
BACKFILL_MODE = "legacy_compatibility_readonly"
REPORT_NAME = "agent_decision_backfill_report.json"

NON_MUTATING_CONTRACT = {
    "mutates_shared_candidate": False,
    "mutates_accounting": False,
    "places_orders": False,
}


@dataclass(frozen=True)
class LoadedLegacyDecisionRow:
    row: dict[str, Any]
    source_path: str
    line_number: int


@dataclass(frozen=True)
class AgentDecisionBackfillResult:
    agent_runs_path: Path
    agent_decisions_path: Path
    report_path: Path
    agent_run_row: dict[str, Any]
    decision_rows: list[dict[str, Any]]
    report: dict[str, Any]


def backfill_legacy_agent_decisions(
    input_paths: Iterable[str | Path],
    *,
    output_dir: str | Path,
    started_at: str | datetime | None = None,
    finished_at: str | datetime | None = None,
) -> AgentDecisionBackfillResult:
    """Build read-only agent decision sidecars from legacy Prediction Lab JSONL rows."""
    normalized_input_paths = [Path(path) for path in input_paths]
    if not normalized_input_paths:
        raise ValueError("at least one input path is required")

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    agent_runs_path = output_path / "agent_runs.jsonl"
    agent_decisions_path = output_path / "agent_decisions.jsonl"
    report_path = output_path / REPORT_NAME

    started_iso = _iso_timestamp(started_at or datetime.now(timezone.utc))
    finished_iso = _iso_timestamp(finished_at or datetime.now(timezone.utc))
    backfill_run_id = _stable_backfill_run_id(normalized_input_paths)
    agent_run_id = build_agent_run_id(agent_id=BACKFILL_AGENT_ID, run_id=backfill_run_id)

    loaded_rows, load_report = _load_legacy_rows(normalized_input_paths)
    decision_rows: list[dict[str, Any]] = []
    skipped_unusable = 0
    validation_errors: list[dict[str, Any]] = []
    rows_with_shared_candidate = 0
    rows_with_legacy_identity = 0
    source_rows_with_decisions = 0

    for loaded in loaded_rows:
        source_row = loaded.row
        if not _row_has_useful_decision(source_row):
            skipped_unusable += 1
            continue

        try:
            shared_candidate_id = shared_candidate_id_from_row(source_row)
            if shared_candidate_id not in (None, ""):
                rows_with_shared_candidate += 1
                emitted = _shared_candidate_decisions(
                    loaded,
                    agent_run_id=agent_run_id,
                    backfill_run_id=backfill_run_id,
                )
            else:
                emitted = _legacy_identity_decisions(
                    loaded,
                    agent_run_id=agent_run_id,
                    backfill_run_id=backfill_run_id,
                )
                if emitted:
                    rows_with_legacy_identity += 1

            if not emitted:
                skipped_unusable += 1
                continue
            source_rows_with_decisions += 1
            decision_rows.extend(emitted)
        except Exception as exc:
            validation_errors.append(
                {
                    "source_path": loaded.source_path,
                    "line_number": loaded.line_number,
                    "error": str(exc),
                }
            )

    status = "partial" if validation_errors or load_report["missing_input_paths"] else "completed"
    run_row = build_agent_run_row(
        agent_id=BACKFILL_AGENT_ID,
        runtime=BACKFILL_RUNTIME,
        policy=BACKFILL_POLICY,
        mode=BACKFILL_MODE,
        run_id=backfill_run_id,
        started_at=started_iso,
        finished_at=finished_iso,
        status=status,
        candidate_dataset_path=_run_candidate_dataset_path(normalized_input_paths),
        decision_ledger_path=agent_decisions_path,
        mutates_accounting=False,
        notes="Read-only legacy Prediction Lab decision sidecar backfill.",
    )
    run_row["metadata"] = {
        "read_only": True,
        "mutates_input_ledgers": False,
        "mutates_shared_candidates": False,
        "mutates_accounting": False,
        "input_paths": [str(path) for path in normalized_input_paths],
        "report_path": str(report_path),
    }

    report = {
        "schema_name": "agent_decision_legacy_backfill_report",
        "schema_version": 1,
        "agent_run_id": agent_run_id,
        "run_id": backfill_run_id,
        "agent_id": BACKFILL_AGENT_ID,
        "runtime": BACKFILL_RUNTIME,
        "mode": BACKFILL_MODE,
        "input_paths": [str(path) for path in normalized_input_paths],
        "output_dir": str(output_path),
        "agent_runs_path": str(agent_runs_path),
        "agent_decisions_path": str(agent_decisions_path),
        "rows_read": len(loaded_rows),
        "source_rows_with_decisions": source_rows_with_decisions,
        "decision_rows_written": len(decision_rows),
        "rows_with_shared_candidate_id": rows_with_shared_candidate,
        "rows_with_legacy_candidate_identity": rows_with_legacy_identity,
        "skipped_unusable_rows": skipped_unusable,
        "invalid_json_rows": load_report["invalid_json_rows"],
        "non_object_rows": load_report["non_object_rows"],
        "missing_input_paths": load_report["missing_input_paths"],
        "validation_error_rows": len(validation_errors),
        "validation_error_samples": validation_errors[:10],
        "by_source_path": _counts(row["candidate_dataset_path"] for row in decision_rows),
        "by_decision_role": _counts(row["decision_role"] for row in decision_rows),
        "by_policy": _counts(row["policy"] for row in decision_rows),
    }

    rewrite_jsonl(agent_runs_path, [run_row])
    rewrite_jsonl(agent_decisions_path, decision_rows)
    atomic_write_json(report_path, report)

    return AgentDecisionBackfillResult(
        agent_runs_path=agent_runs_path,
        agent_decisions_path=agent_decisions_path,
        report_path=report_path,
        agent_run_row=run_row,
        decision_rows=decision_rows,
        report=report,
    )


def _shared_candidate_decisions(
    loaded: LoadedLegacyDecisionRow,
    *,
    agent_run_id: str,
    backfill_run_id: str,
) -> list[dict[str, Any]]:
    source_row = _source_row_with_backfill_defaults(loaded.row)
    rows = build_agent_decision_rows_from_source_row(
        source_row,
        agent_run_id=agent_run_id,
        agent_id=BACKFILL_AGENT_ID,
        runtime=BACKFILL_RUNTIME,
        candidate_dataset_path=loaded.source_path,
        decided_at=_observed_at(source_row),
    )
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not _decision_row_is_useful(row):
            continue
        normalized.append(
            _normalize_backfill_decision_row(
                row,
                source_row=source_row,
                loaded=loaded,
                backfill_run_id=backfill_run_id,
            )
        )
    return normalized


def _legacy_identity_decisions(
    loaded: LoadedLegacyDecisionRow,
    *,
    agent_run_id: str,
    backfill_run_id: str,
) -> list[dict[str, Any]]:
    source_row = _source_row_with_backfill_defaults(loaded.row)
    observed_at = _observed_at(source_row)
    run_id = str(source_row.get("run_id") or "legacy_unknown_run")
    market_id = str(source_row.get("market_id") or source_row.get("snapshot_key") or "unknown")
    row_fingerprint = _row_fingerprint(loaded.row)
    rows: list[dict[str, Any]] = []

    for decision_role, decision in _legacy_decision_role_rows(source_row):
        policy = str(
            _first_present(
                decision.get("policy"),
                decision.get("strategy_policy"),
                source_row.get("strategy_policy_normalized"),
                source_row.get("strategy_policy"),
                source_row.get("policy"),
                "normal",
            )
        )
        legacy_identity = {
            "identity_type": "legacy_prediction_lab_market_snapshot",
            "source_path": loaded.source_path,
            "line_number": loaded.line_number,
            "run_id": run_id,
            "market_id": market_id,
            "observed_at": observed_at,
            "timestamp": _first_present(source_row.get("timestamp"), source_row.get("observed_at"), observed_at),
            "decision_role": decision_role,
            "policy": policy,
            "row_fingerprint_sha256": row_fingerprint,
        }
        decision_row = validate_agent_decision_row_with_legacy_identity(
            {
                "schema_name": "agent_decision",
                "schema_version": 1,
                "decision_id": build_agent_decision_id(
                    agent_run_id=agent_run_id,
                    agent_id=BACKFILL_AGENT_ID,
                    runtime=BACKFILL_RUNTIME,
                    policy=policy,
                    decision_role=decision_role,
                    shared_candidate_id="",
                    run_id=run_id,
                    market_id=market_id,
                    observed_at=observed_at,
                    legacy_candidate_identity=legacy_identity,
                ),
                "agent_run_id": agent_run_id,
                "agent_id": BACKFILL_AGENT_ID,
                "runtime": BACKFILL_RUNTIME,
                "policy": policy,
                "decision_role": decision_role,
                "legacy_candidate_identity": legacy_identity,
                "candidate_dataset_path": loaded.source_path,
                "candidate_dataset_identity": f"legacy_prediction_lab:{loaded.source_path}:L{loaded.line_number}:{row_fingerprint[:16]}",
                "run_id": run_id,
                "market_id": market_id,
                "observed_at": observed_at,
                "decided_at": observed_at,
                "action": decision.get("action"),
                "side": decision.get("side") or _side_from_action(decision.get("action")),
                "requested_position_size_usd": _coerce_number(
                    decision.get("requested_position_size"),
                    decision.get("requested_position_size_usd"),
                    decision.get("size"),
                ),
                "approved_position_size_usd": _coerce_number(
                    decision.get("approved_position_size"),
                    decision.get("approved_position_size_usd"),
                ),
                "reason_code": decision.get("reason_code"),
                "reason": decision.get("reason"),
                "selected_lane": decision.get("selected_lane"),
                "confidence": _coerce_number(decision.get("confidence"), source_row.get("confidence")),
                "edge": _coerce_number(decision.get("edge"), source_row.get("edge")),
                "model_probability": _coerce_number(decision.get("model_probability"), source_row.get("model_probability")),
                "entry_price": _entry_price(source_row, decision),
                "price": _entry_price(source_row, decision),
                "mutation_contract": dict(NON_MUTATING_CONTRACT),
                "provenance": _backfill_provenance(loaded=loaded, backfill_run_id=backfill_run_id),
            }
        )
        if _decision_row_is_useful(decision_row):
            rows.append(decision_row)
    return rows


def _legacy_decision_role_rows(source_row: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    rows: list[tuple[str, dict[str, Any]]] = []
    for decision_role, key in (("main", "main_decision"), ("normal", "normal_decision"), ("shadow", "shadow_decision")):
        value = source_row.get(key)
        if isinstance(value, dict):
            decision = _decision_from_sources(source_row, value)
            if _decision_is_useful(decision):
                rows.append((decision_role, decision))
    if rows:
        return rows

    fallback = _decision_from_sources(source_row, {})
    return [("main", fallback)] if _decision_is_useful(fallback) else []


def _decision_from_sources(source_row: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    artifact = source_row.get("decision_artifact") if isinstance(source_row.get("decision_artifact"), dict) else {}
    shared_pipeline = source_row.get("shared_pipeline") if isinstance(source_row.get("shared_pipeline"), dict) else {}
    shared_core = artifact.get("shared_core_decision") if isinstance(artifact.get("shared_core_decision"), dict) else {}
    signal = artifact.get("strategy_signal") if isinstance(artifact.get("strategy_signal"), dict) else {}
    action = _first_present(
        decision.get("action"),
        decision.get("direction"),
        artifact.get("final_action"),
        shared_pipeline.get("final_action"),
        shared_core.get("action"),
        source_row.get("direction"),
        _action_from_decision_type(source_row.get("decision_type")),
    )
    reason_code = _first_present(
        decision.get("reason_code"),
        decision.get("skip_reason_code"),
        artifact.get("final_reason_code"),
        shared_pipeline.get("final_reason_code"),
        shared_core.get("reason_code"),
        source_row.get("skip_reason_code"),
        source_row.get("reason_code"),
    )
    reason = _first_present(
        decision.get("reason"),
        artifact.get("final_reason"),
        shared_core.get("reason"),
        source_row.get("reason"),
        source_row.get("skip_reason"),
    )
    return {
        "policy": _first_present(decision.get("policy"), source_row.get("policy")),
        "action": action,
        "side": _first_present(decision.get("side"), _side_from_action(action)),
        "reason_code": reason_code,
        "reason": reason,
        "requested_position_size": _first_present(
            decision.get("requested_position_size"),
            decision.get("requested_position_size_usd"),
            shared_core.get("requested_position_size"),
            shared_pipeline.get("requested_position_size_usd"),
            decision.get("size"),
        ),
        "approved_position_size": _first_present(
            decision.get("approved_position_size"),
            decision.get("approved_position_size_usd"),
            shared_core.get("approved_position_size"),
            shared_core.get("position_size"),
            shared_pipeline.get("approved_position_size_usd"),
        ),
        "selected_lane": _first_present(decision.get("selected_lane"), shared_core.get("selected_lane")),
        "entry_price": _first_present(decision.get("entry_price"), shared_core.get("entry_price")),
        "market_price": _first_present(decision.get("market_price"), source_row.get("market_price")),
        "price": _first_present(decision.get("price"), shared_core.get("price")),
        "confidence": _first_present(decision.get("confidence"), signal.get("confidence")),
        "edge": _first_present(decision.get("edge"), signal.get("edge")),
        "model_probability": _first_present(decision.get("model_probability"), signal.get("model_probability")),
    }


def _normalize_backfill_decision_row(
    row: dict[str, Any],
    *,
    source_row: dict[str, Any],
    loaded: LoadedLegacyDecisionRow,
    backfill_run_id: str,
) -> dict[str, Any]:
    normalized = dict(row)
    _fill_missing_decision_fields(normalized, source_row=source_row)
    normalized["mutation_contract"] = dict(NON_MUTATING_CONTRACT)
    accounting_ref = normalized.get("accounting_ref")
    if isinstance(accounting_ref, dict):
        normalized["accounting_ref"] = {
            **accounting_ref,
            "mutates_balance": False,
            "mutates_accounting": False,
            "places_orders": False,
        }
    normalized["provenance"] = {
        **(normalized.get("provenance") if isinstance(normalized.get("provenance"), dict) else {}),
        **_backfill_provenance(loaded=loaded, backfill_run_id=backfill_run_id),
    }
    return normalized


def _fill_missing_decision_fields(row: dict[str, Any], *, source_row: dict[str, Any]) -> None:
    decision_role = str(row.get("decision_role") or "")
    decision_key = f"{decision_role}_decision"
    has_explicit_role_decision = isinstance(source_row.get(decision_key), dict)
    decision = source_row.get(decision_key) if has_explicit_role_decision else {}
    extracted = _decision_from_sources(source_row, decision)
    # Legacy shared-candidate rows may only have decision_artifact.final_action.
    # The normal ledger builder can synthesize a fallback SKIP for those rows;
    # backfill should prefer the historical artifact fields unless an explicit
    # role-specific decision object exists.
    for key in ("action", "reason_code", "reason", "selected_lane"):
        if extracted.get(key) not in (None, "") and (row.get(key) in (None, "") or not has_explicit_role_decision):
            row[key] = extracted.get(key)
    if row.get("side") in (None, "") or not has_explicit_role_decision:
        row["side"] = extracted.get("side") or _side_from_action(row.get("action"))
    for key in ("confidence", "edge", "model_probability"):
        if row.get(key) in (None, ""):
            row[key] = _coerce_number(extracted.get(key), source_row.get(key))
    if row.get("entry_price") in (None, ""):
        row["entry_price"] = _entry_price(source_row, extracted)
    if row.get("price") in (None, ""):
        row["price"] = row.get("entry_price")


def _source_row_with_backfill_defaults(row: dict[str, Any]) -> dict[str, Any]:
    copied = dict(row)
    copied.setdefault("run_id", "legacy_unknown_run")
    copied.setdefault("market_id", copied.get("snapshot_key") or "unknown")
    copied.setdefault("observed_at", copied.get("timestamp") or "unknown")
    copied.setdefault("timestamp", copied.get("observed_at") or "unknown")
    return copied


def _row_has_useful_decision(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    if _legacy_decision_role_rows(_source_row_with_backfill_defaults(row)):
        return True
    return False


def _decision_is_useful(decision: dict[str, Any]) -> bool:
    return any(decision.get(key) not in (None, "") for key in ("action", "reason", "reason_code"))


def _decision_row_is_useful(row: dict[str, Any]) -> bool:
    return any(row.get(key) not in (None, "") for key in ("action", "reason", "reason_code"))


def _backfill_provenance(*, loaded: LoadedLegacyDecisionRow, backfill_run_id: str) -> dict[str, Any]:
    return {
        "known_at_time": False,
        "source": "historical_post_facto",
        "backfill": "legacy_prediction_lab_agent_decision_sidecar",
        "backfill_run_id": backfill_run_id,
        "input_path": loaded.source_path,
        "input_line_number": loaded.line_number,
        "row_fingerprint_sha256": _row_fingerprint(loaded.row),
    }


def _load_legacy_rows(paths: list[Path]) -> tuple[list[LoadedLegacyDecisionRow], dict[str, Any]]:
    rows: list[LoadedLegacyDecisionRow] = []
    invalid_json_rows = 0
    non_object_rows = 0
    missing_input_paths: list[str] = []
    for path in paths:
        if not path.exists():
            missing_input_paths.append(str(path))
            continue
        with path.open("r", encoding="utf-8") as fh:
            for line_number, line in enumerate(fh, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    value = json.loads(stripped)
                except json.JSONDecodeError:
                    invalid_json_rows += 1
                    continue
                if not isinstance(value, dict):
                    non_object_rows += 1
                    continue
                rows.append(LoadedLegacyDecisionRow(row=value, source_path=str(path), line_number=line_number))
    return rows, {
        "invalid_json_rows": invalid_json_rows,
        "non_object_rows": non_object_rows,
        "missing_input_paths": missing_input_paths,
    }


def _stable_backfill_run_id(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path).encode("utf-8"))
        digest.update(b"\0")
        if path.exists():
            digest.update(_file_sha256(path).encode("ascii"))
        else:
            digest.update(b"missing")
        digest.update(b"\0")
    return f"legacy_backfill_{digest.hexdigest()[:20]}"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _row_fingerprint(row: dict[str, Any]) -> str:
    payload = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _run_candidate_dataset_path(paths: list[Path]) -> str:
    if len(paths) == 1:
        return str(paths[0])
    digest = hashlib.sha256("\n".join(str(path) for path in paths).encode("utf-8")).hexdigest()[:16]
    return f"multiple_prediction_lab_inputs:{digest}"


def _observed_at(row: dict[str, Any]) -> str:
    return str(_first_present(row.get("observed_at"), row.get("timestamp"), "unknown"))


def _entry_price(source_row: dict[str, Any], decision: dict[str, Any]) -> int | float | None:
    explicit = _coerce_number(decision.get("entry_price"), decision.get("market_price"), decision.get("price"))
    if explicit is not None:
        return explicit
    side = str(decision.get("side") or _side_from_action(decision.get("action")) or "").upper()
    if side == "YES":
        return _coerce_number(source_row.get("yes_market_price"), source_row.get("yes_price"), source_row.get("market_price"))
    if side == "NO":
        return _coerce_number(source_row.get("no_market_price"), source_row.get("no_price"), source_row.get("market_price"))
    return _coerce_number(source_row.get("market_price"), source_row.get("price"), source_row.get("yes_price"))


def _action_from_decision_type(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if text == "buy_yes":
        return "BUY_YES"
    if text == "buy_no":
        return "BUY_NO"
    if text == "skip":
        return "SKIP"
    return None


def _side_from_action(value: Any) -> str | None:
    text = str(value or "").upper()
    if "YES" in text:
        return "YES"
    if "NO" in text:
        return "NO"
    return None


def _first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _coerce_number(*values: Any) -> float | int | None:
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return value
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _iso_timestamp(value: str | datetime | None) -> str:
    if isinstance(value, datetime):
        dt = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    text = str(value or "").strip()
    return text or datetime.now(timezone.utc).isoformat()


def _counts(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts
