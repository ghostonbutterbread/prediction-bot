"""Safe derived backfill helpers for Prediction Lab replay ledgers."""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from bot.file_ops import atomic_write_json, load_jsonl, rewrite_jsonl
from bot.prediction_lab_replay import (
    ORDER_BOOK_MISSING,
    ORDER_BOOK_RECORDED,
    ORDER_BOOK_SIGNAL_PRICE_FALLBACK,
    SOURCE_HISTORICAL_POST_FACTO,
    SOURCE_RECORDED_AS_OF,
    classify_execution_snapshot_mode,
    classify_order_book_mode,
    classify_replay_row_quality,
    classify_source_mode,
    validate_prediction_lab_tables,
)


BACKFILL_VERSION = "prediction_lab_backfill_phase2"
DEFAULT_PREDICTION_LAB_DIR = Path("data/paper/prediction_lab")
DEFAULT_ANALYSIS_DIR = DEFAULT_PREDICTION_LAB_DIR / "analysis"
CANONICAL_ANALYSIS_LEDGER_NAME = "market_snapshots_upgraded.jsonl"

TIER_REPLAY_GRADE_ORIGINAL = "replay_grade_original"
TIER_REPLAY_GRADE_BACKFILLED_FROM_ARTIFACT = "replay_grade_backfilled_from_artifact"
TIER_COVERAGE_ONLY = "coverage_only"
TIER_UNUSABLE = "unusable"

OUTCOME_LEAKAGE_KEYS = {"resolution", "outcome", "actual_outcome", "settled_outcome", "market_result", "result"}
TOP_LEVEL_OUTCOME_KEYS = OUTCOME_LEAKAGE_KEYS


@dataclass(slots=True)
class FieldProvenance:
    field: str
    method: str
    path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class BackfilledRow:
    row: dict[str, Any]
    tier: str
    reasons: list[str] = field(default_factory=list)
    recovered_fields: list[FieldProvenance] = field(default_factory=list)
    original_had_outcome_leakage: bool = False

    def to_report_dict(self) -> dict[str, Any]:
        return {
            "market_id": self.row.get("market_id"),
            "snapshot_key": self.row.get("snapshot_key"),
            "tier": self.tier,
            "reasons": list(self.reasons),
            "recovered_fields": [item.to_dict() for item in self.recovered_fields],
            "original_had_outcome_leakage": self.original_had_outcome_leakage,
        }


@dataclass(slots=True)
class BackfillResult:
    rows: list[BackfilledRow]
    report: dict[str, Any]
    manifest: dict[str, Any]
    validation: dict[str, Any] | None = None
    output_path: Path | None = None


@dataclass(slots=True)
class LoadedBackfillRow:
    row: dict[str, Any]
    source_path: str
    line_number: int


def run_prediction_lab_backfill(
    input_path: str | Path,
    output_dir: str | Path,
    *,
    limit: int | None = None,
    tail: bool = False,
    inventory_only: bool = False,
    artifact_recovery: bool = False,
    resolution_paths: Iterable[str | Path] | None = None,
) -> BackfillResult:
    """Inventory and optionally write a derived upgraded Prediction Lab ledger."""

    input_path = Path(input_path)
    output_dir = Path(output_dir)
    loaded_rows = _load_jsonl_rows([input_path], limit=limit, tail=tail)

    backfilled = [
        backfill_prediction_lab_row(
            item.row,
            artifact_recovery=artifact_recovery,
            line_number=item.line_number,
            input_path=item.source_path,
        )
        for item in loaded_rows
    ]
    _mark_duplicate_identities(backfilled)
    resolution_join = _resolution_join_report(backfilled, resolution_paths=resolution_paths)
    report = build_backfill_report(
        backfilled,
        input_path=input_path,
        limit=limit,
        inventory_only=inventory_only,
        artifact_recovery=artifact_recovery,
        resolution_join=resolution_join,
    )
    manifest = build_provenance_manifest(
        backfilled,
        input_path=input_path,
        output_dir=output_dir,
        inventory_only=inventory_only,
        artifact_recovery=artifact_recovery,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output_dir / "backfill_report.json", report)
    validation: dict[str, Any] | None = None
    if not inventory_only:
        upgraded_path = output_dir / "upgraded_market_snapshots.jsonl"
        upgraded_rows = _upgraded_ledger_rows(backfilled)
        rewrite_jsonl(upgraded_path, upgraded_rows)
        validation = validate_prediction_lab_tables([upgraded_path]).to_dict()
        report["validation"] = validation
        report["written_rows"] = len(upgraded_rows)
        report["excluded_from_upgraded_counts"] = {
            "unusable": sum(1 for item in backfilled if item.tier == TIER_UNUSABLE),
            "duplicate_identity": _duplicate_exclusion_count(backfilled),
        }
        manifest["validation"] = {
            "ok": validation["ok"],
            "issue_counts": validation.get("issue_counts", {}),
            "severity_counts": validation.get("severity_counts", {}),
        }
        manifest["written_rows"] = len(upgraded_rows)
        atomic_write_json(output_dir / "backfill_report.json", report)
        atomic_write_json(output_dir / "provenance_manifest.json", manifest)

    return BackfillResult(rows=backfilled, report=report, manifest=manifest, validation=validation, output_path=None if inventory_only else upgraded_path)


def run_prediction_lab_canonical_analysis(
    *,
    prediction_lab_dir: str | Path = DEFAULT_PREDICTION_LAB_DIR,
    analysis_dir: str | Path = DEFAULT_ANALYSIS_DIR,
    market_snapshots_path: str | Path | None = None,
    predictions_path: str | Path | None = None,
    include_predictions: bool = False,
    limit: int | None = None,
    tail: bool = False,
    validate_output: bool = True,
    resolution_paths: Iterable[str | Path] | None = None,
) -> BackfillResult:
    """Build the stable canonical analysis ledger from raw Prediction Lab ledgers."""

    prediction_lab_dir = Path(prediction_lab_dir)
    analysis_dir = Path(analysis_dir)
    market_snapshots_path = Path(market_snapshots_path) if market_snapshots_path is not None else prediction_lab_dir / "market_snapshots.jsonl"
    source_paths = [market_snapshots_path]
    if include_predictions:
        predictions_path = Path(predictions_path) if predictions_path is not None else prediction_lab_dir / "predictions.jsonl"
        source_paths.append(predictions_path)

    loaded_rows: list[LoadedBackfillRow] = []
    for source_path in source_paths:
        loaded_rows.extend(_load_jsonl_rows([source_path], limit=limit, tail=tail))
    backfilled = [
        backfill_prediction_lab_row(
            item.row,
            artifact_recovery=True,
            line_number=item.line_number,
            input_path=item.source_path,
        )
        for item in loaded_rows
    ]
    _mark_duplicate_identities(backfilled)
    resolution_join = _resolution_join_report(backfilled, resolution_paths=resolution_paths)
    source_row_counts = _counts(item.source_path for item in loaded_rows)
    output_path = analysis_dir / CANONICAL_ANALYSIS_LEDGER_NAME

    report = build_backfill_report(
        backfilled,
        input_path=market_snapshots_path,
        limit=limit,
        inventory_only=False,
        artifact_recovery=True,
        resolution_join=resolution_join,
    )
    report.update(
        {
            "canonical_analysis": True,
            "analysis_dir": str(analysis_dir),
            "input_paths": [str(path) for path in source_paths],
            "source_row_counts": source_row_counts,
            "tail": tail,
            "output_ledger": str(output_path),
            "output_ledger_name": CANONICAL_ANALYSIS_LEDGER_NAME,
        }
    )
    manifest = build_provenance_manifest(
        backfilled,
        input_path=market_snapshots_path,
        output_dir=analysis_dir,
        inventory_only=False,
        artifact_recovery=True,
    )
    manifest.update(
        {
            "canonical_analysis": True,
            "analysis_dir": str(analysis_dir),
            "input_paths": [str(path) for path in source_paths],
            "source_row_counts": source_row_counts,
            "output_ledger": str(output_path),
            "output_files": [
                CANONICAL_ANALYSIS_LEDGER_NAME,
                "backfill_report.json",
                "provenance_manifest.json",
                "validation_report.json",
                "latest_metadata.json",
            ],
            "modes": {
                **manifest.get("modes", {}),
                "canonical_analysis": True,
            },
        }
    )

    analysis_dir.mkdir(parents=True, exist_ok=True)
    upgraded_rows = _upgraded_ledger_rows(backfilled)
    rewrite_jsonl(output_path, upgraded_rows)
    report["written_rows"] = len(upgraded_rows)
    report["excluded_from_upgraded_counts"] = {
        "unusable": sum(1 for item in backfilled if item.tier == TIER_UNUSABLE),
        "duplicate_identity": _duplicate_exclusion_count(backfilled),
    }
    manifest["written_rows"] = len(upgraded_rows)

    validation = _validation_payload(
        validate_output=validate_output,
        output_path=output_path,
        resolution_paths=resolution_paths,
    )
    report["validation"] = _validation_summary(validation)
    manifest["validation"] = _validation_summary(validation)

    metadata = {
        "version": BACKFILL_VERSION,
        "generated_at": _now_iso(),
        "canonical_analysis": True,
        "analysis_dir": str(analysis_dir),
        "output_ledger": str(output_path),
        "input_paths": [str(path) for path in source_paths],
        "limit": limit,
        "tail": tail,
        "include_predictions": include_predictions,
        "tier_counts": report["tier_counts"],
        "written_rows": len(upgraded_rows),
        "validation": _validation_summary(validation),
    }
    atomic_write_json(analysis_dir / "backfill_report.json", report)
    atomic_write_json(analysis_dir / "provenance_manifest.json", manifest)
    atomic_write_json(analysis_dir / "validation_report.json", validation)
    atomic_write_json(analysis_dir / "latest_metadata.json", metadata)

    return BackfillResult(rows=backfilled, report=report, manifest=manifest, validation=validation, output_path=output_path)


def backfill_prediction_lab_row(
    raw_row: dict[str, Any],
    *,
    artifact_recovery: bool = False,
    line_number: int | None = None,
    input_path: str | None = None,
) -> BackfilledRow:
    """Return a sanitized derived row with artifact-only recovery and provenance."""

    original = copy.deepcopy(raw_row)
    had_decision_artifact = isinstance(original.get("decision_artifact"), dict)
    had_outcome_leakage = _has_outcome_leakage(original)
    row = _strip_outcome_leakage(original)
    recovered: list[FieldProvenance] = []

    artifact = row.get("decision_artifact")
    if not isinstance(artifact, dict):
        artifact = _legacy_artifact_from_row(row)
        row["decision_artifact"] = artifact
    else:
        artifact = copy.deepcopy(artifact)
        row["decision_artifact"] = artifact

    if artifact_recovery:
        recovered.extend(_recover_artifact_fields(row, artifact))

    source_mode = classify_source_mode(artifact, row)
    order_book_mode = classify_order_book_mode(artifact)
    execution_mode = classify_execution_snapshot_mode(artifact)
    quality = classify_replay_row_quality(
        artifact,
        row,
        source_mode=source_mode,
        order_book_mode=order_book_mode,
        execution_snapshot_mode=execution_mode,
    )

    reasons = _inventory_reasons(
        row,
        artifact,
        source_mode=source_mode,
        order_book_mode=order_book_mode,
        execution_mode=execution_mode,
        quality_reasons=quality.reasons,
        had_decision_artifact=had_decision_artifact,
        had_outcome_leakage=had_outcome_leakage,
    )
    tier = _tier_for_row(
        row,
        artifact,
        recovered=recovered,
        source_mode=source_mode,
        order_book_mode=order_book_mode,
        execution_mode=execution_mode,
        reasons=reasons,
        had_outcome_leakage=had_outcome_leakage,
    )
    _attach_provenance(row, artifact, tier=tier, recovered=recovered, reasons=reasons, line_number=line_number, input_path=input_path)

    return BackfilledRow(
        row=row,
        tier=tier,
        reasons=reasons,
        recovered_fields=recovered,
        original_had_outcome_leakage=had_outcome_leakage,
    )


def build_backfill_report(
    rows: Iterable[BackfilledRow],
    *,
    input_path: str | Path,
    limit: int | None = None,
    inventory_only: bool = False,
    artifact_recovery: bool = False,
    resolution_join: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows = list(rows)
    return {
        "version": BACKFILL_VERSION,
        "generated_at": _now_iso(),
        "input_path": str(input_path),
        "limit": limit,
        "inventory_only": inventory_only,
        "artifact_recovery": artifact_recovery,
        "total_rows": len(rows),
        "tier_counts": _counts(item.tier for item in rows),
        "reason_counts": _counts(reason for item in rows for reason in item.reasons),
        "market_group_counts": _counts(_row_group(item.row) for item in rows),
        "series_counts": _counts(_row_series(item.row) for item in rows),
        "event_ticker_counts": _counts(_row_event_ticker(item.row) for item in rows),
        "date_counts": _counts(_row_date(item.row) for item in rows),
        "recovered_field_counts": _counts(source.field for item in rows for source in item.recovered_fields),
        "rows_with_outcome_leakage": sum(1 for item in rows if item.original_had_outcome_leakage),
        "resolution_join": resolution_join or {"checked": False, "missing_resolution_join": None},
    }


def build_provenance_manifest(
    rows: Iterable[BackfilledRow],
    *,
    input_path: str | Path,
    output_dir: str | Path,
    inventory_only: bool = False,
    artifact_recovery: bool = False,
) -> dict[str, Any]:
    rows = list(rows)
    output_files = ["backfill_report.json"]
    if not inventory_only:
        output_files.extend(["upgraded_market_snapshots.jsonl", "provenance_manifest.json"])
    return {
        "version": BACKFILL_VERSION,
        "generated_at": _now_iso(),
        "input_path": str(input_path),
        "output_dir": str(output_dir),
        "output_files": output_files,
        "raw_ledgers_mutated": False,
        "modes": {
            "inventory_only": inventory_only,
            "artifact_recovery": artifact_recovery,
            "log_recovery": False,
            "historical_weather": False,
        },
        "tier_counts": _counts(item.tier for item in rows),
        "source_methods": _counts(source.method for item in rows for source in item.recovered_fields),
        "field_sources": [source.to_dict() for item in rows for source in item.recovered_fields],
    }


def _recover_artifact_fields(row: dict[str, Any], artifact: dict[str, Any]) -> list[FieldProvenance]:
    recovered: list[FieldProvenance] = []
    recovered.extend(_recover_source_context(row, artifact))
    recovered.extend(_recover_weather_snapshot(artifact))
    recovered.extend(_recover_source_snapshots(artifact))
    recovered.extend(_recover_order_book(artifact))
    recovered.extend(_recover_execution_snapshot(artifact))
    return recovered


def _recover_source_context(row: dict[str, Any], artifact: dict[str, Any]) -> list[FieldProvenance]:
    if isinstance(artifact.get("source_context"), dict):
        return []
    for row_key in ("source_context", "recorded_source_context"):
        value = row.get(row_key)
        if isinstance(value, dict) and value:
            artifact["source_context"] = copy.deepcopy(value)
            return [
                FieldProvenance(
                    "source_context",
                    "nested_artifact_recovery",
                    row_key,
                )
            ]
    artifact["source_context"] = {"source": "missing", "mode": "legacy", "data": {}}
    return []


def _recover_weather_snapshot(artifact: dict[str, Any]) -> list[FieldProvenance]:
    source_context = artifact.setdefault("source_context", {"source": "missing", "mode": "legacy", "data": {}})
    if not isinstance(source_context, dict):
        return []
    data = source_context.setdefault("data", {})
    if not isinstance(data, dict):
        data = {}
        source_context["data"] = data
    if isinstance(data.get("weather_source_snapshot"), dict) and data["weather_source_snapshot"]:
        _normalize_weather_snapshot_provenance(data["weather_source_snapshot"], source_context)
        return []

    candidate, path = _weather_snapshot_from_source_snapshots(artifact)
    method = "nested_artifact_recovery"
    if candidate is None:
        candidate, path = _weather_snapshot_from_recorded_signal(artifact)
        method = "recorded_signal_weather_snapshot_reconstruction"
    if candidate is None:
        return []
    data["weather_source_snapshot"] = copy.deepcopy(candidate)
    _normalize_weather_snapshot_provenance(data["weather_source_snapshot"], source_context)
    return [FieldProvenance("weather_source_snapshot", method, path)]


def _recover_source_snapshots(artifact: dict[str, Any]) -> list[FieldProvenance]:
    snapshots = artifact.get("source_snapshots")
    if isinstance(snapshots, list) and snapshots:
        return []
    source_context = artifact.get("source_context") if isinstance(artifact.get("source_context"), dict) else {}
    data = source_context.get("data") if isinstance(source_context.get("data"), dict) else {}
    weather_snapshot = data.get("weather_source_snapshot") if isinstance(data.get("weather_source_snapshot"), dict) else None
    if weather_snapshot:
        mode = _weather_snapshot_mode(weather_snapshot)
        artifact["source_snapshots"] = [
            {
                "mode": mode,
                "source": "weather",
                "source_provenance": weather_snapshot.get("source_provenance"),
                "provenance": weather_snapshot.get("provenance"),
                "method": "_live_data_signal",
                "snapshot_ref": "source_context.data.weather_source_snapshot",
            }
        ]
        return [
            FieldProvenance(
                "source_snapshots",
                "nested_artifact_recovery",
                "decision_artifact.source_context.data.weather_source_snapshot",
            )
        ]
    return []


def _recover_order_book(artifact: dict[str, Any]) -> list[FieldProvenance]:
    if _has_executable_asks((artifact.get("order_book_snapshot") or {}).get("data") if isinstance(artifact.get("order_book_snapshot"), dict) else None):
        return []
    for key in ("order_book", "book", "recorded_order_book"):
        value = artifact.get(key)
        if _has_executable_asks(value):
            artifact["order_book_snapshot"] = {"source": "book", "data": copy.deepcopy(value)}
            return [FieldProvenance("order_book_snapshot", "nested_artifact_recovery", f"decision_artifact.{key}")]
    execution_snapshot = artifact.get("execution_snapshot")
    if _has_executable_asks(execution_snapshot):
        artifact["order_book_snapshot"] = {
            "source": str(execution_snapshot.get("source") or artifact.get("execution_snapshot_source") or "book"),
            "data": {
                key: execution_snapshot.get(key)
                for key in ("best_yes_ask", "best_no_ask", "best_yes_bid", "best_no_bid")
                if execution_snapshot.get(key) is not None
            },
        }
        return [FieldProvenance("order_book_snapshot", "nested_artifact_recovery", "decision_artifact.execution_snapshot")]
    return []


def _recover_execution_snapshot(artifact: dict[str, Any]) -> list[FieldProvenance]:
    if _has_executable_asks(artifact.get("execution_snapshot")):
        if not artifact.get("execution_snapshot_source"):
            source = artifact["execution_snapshot"].get("source") if isinstance(artifact.get("execution_snapshot"), dict) else None
            artifact["execution_snapshot_source"] = source or "book"
        return []
    snapshot = artifact.get("order_book_snapshot")
    data = snapshot.get("data") if isinstance(snapshot, dict) else None
    if not _has_executable_asks(data):
        return []
    artifact["execution_snapshot"] = {
        "source": str(snapshot.get("source") or "book"),
        **{
            key: data.get(key)
            for key in ("best_yes_ask", "best_no_ask", "best_yes_bid", "best_no_bid")
            if data.get(key) is not None
        },
    }
    artifact["execution_snapshot_source"] = str(snapshot.get("source") or "book")
    return [
        FieldProvenance(
            "execution_snapshot",
            "nested_artifact_recovery",
            "decision_artifact.order_book_snapshot.data",
        )
    ]


def _normalize_weather_snapshot_provenance(snapshot: dict[str, Any], source_context: dict[str, Any]) -> None:
    mode = _weather_snapshot_mode(snapshot)
    snapshot["mode"] = mode
    if mode == SOURCE_HISTORICAL_POST_FACTO:
        snapshot.setdefault("source_provenance", "historical_post_facto_backfill")
        snapshot.setdefault(
            "provenance",
            {
                "source_mode": SOURCE_HISTORICAL_POST_FACTO,
                "source_provenance": snapshot.get("source_provenance"),
                "anti_hindsight": "post_facto_weather_not_recorded_as_of",
            },
        )
        source_context["source"] = SOURCE_HISTORICAL_POST_FACTO
        source_context["source_mode"] = SOURCE_HISTORICAL_POST_FACTO
        source_context.setdefault("source_provenance", snapshot.get("source_provenance"))
        source_context.setdefault("provenance", snapshot.get("provenance"))
        return
    if not source_context.get("source_mode"):
        source_context["source_mode"] = SOURCE_RECORDED_AS_OF
    if not source_context.get("source"):
        source_context["source"] = "provided"


def _weather_snapshot_mode(snapshot: dict[str, Any]) -> str:
    values = [
        snapshot.get("mode"),
        snapshot.get("source_mode"),
        snapshot.get("source_provenance"),
        snapshot.get("provenance"),
    ]
    source_signal = snapshot.get("source_signal") if isinstance(snapshot.get("source_signal"), dict) else {}
    source_data = source_signal.get("data") if isinstance(source_signal.get("data"), dict) else {}
    values.extend(
        (
            source_signal.get("mode"),
            source_signal.get("source_mode"),
            source_signal.get("source_provenance"),
            source_data.get("mode"),
            source_data.get("source_mode"),
            source_data.get("source_provenance"),
        )
    )
    if snapshot.get("historical_replay") is True or source_signal.get("historical_replay") is True or source_data.get("historical_replay") is True:
        return SOURCE_HISTORICAL_POST_FACTO
    if any(_text_has_post_facto_token(value) for value in values):
        return SOURCE_HISTORICAL_POST_FACTO
    return SOURCE_RECORDED_AS_OF


def _text_has_post_facto_token(value: Any) -> bool:
    text = str(value or "").lower()
    return any(token in text for token in ("historical_post_facto", "post_facto", "historical_replay"))


def _weather_snapshot_from_source_snapshots(artifact: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    snapshots = artifact.get("source_snapshots")
    if not isinstance(snapshots, list):
        return None, ""
    for index, snapshot in enumerate(snapshots):
        if not isinstance(snapshot, dict):
            continue
        resolved = _resolve_snapshot_ref(artifact, snapshot)
        candidate = resolved if isinstance(resolved, dict) else snapshot
        source_name = str(candidate.get("source_name") or candidate.get("source") or candidate.get("signal_type") or "").lower()
        is_weather = source_name == "weather" or any(
            key in candidate
            for key in ("forecast", "date_validation", "market_date", "target_forecast_date", "station_id")
        )
        if is_weather and candidate:
            path = snapshot.get("snapshot_ref") if resolved is not None else f"decision_artifact.source_snapshots[{index}]"
            return candidate, str(path)
    return None, ""


def _weather_snapshot_from_recorded_signal(artifact: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    for path, signal in _recorded_weather_signal_candidates(artifact):
        data = signal.get("data") if isinstance(signal.get("data"), dict) else {}
        if not _looks_like_weather_signal(signal, data):
            continue
        mode = SOURCE_HISTORICAL_POST_FACTO if _recorded_signal_is_post_facto(signal, data) else SOURCE_RECORDED_AS_OF
        source_timestamp = signal.get("source_timestamp") or data.get("as_of") or data.get("fetched_at") or artifact.get("as_of") or artifact.get("observed_at")
        date_validation = data.get("date_validation") if isinstance(data.get("date_validation"), dict) else None
        snapshot = {
            "artifact_version": 1,
            "mode": mode,
            "source_name": "weather",
            "signal_type": "weather",
            "method": "_live_data_signal",
            "as_of": source_timestamp,
            "fetched_at": data.get("fetched_at") or source_timestamp,
            "source_timestamp": signal.get("source_timestamp"),
            "ttl_seconds": signal.get("ttl_seconds"),
            "predicted_prob": signal.get("predicted_prob"),
            "confidence": signal.get("confidence"),
            "source_agreement_score": data.get("agreement"),
            "settlement_source": data.get("settlement_source"),
            "station_id": data.get("station_id"),
            "station_cli": data.get("station_cli"),
            "station_mapping": data.get("station_mapping"),
            "station_resolution": data.get("station_resolution"),
            "weather_date": data.get("weather_date"),
            "forecast_date": data.get("forecast_date"),
            "target_forecast_date": data.get("target_forecast_date"),
            "date_validation": date_validation,
            "forecast": {
                "high": data.get("forecast_high"),
                "low": data.get("forecast_low"),
                "current": data.get("current_temp"),
                "actual_temp_used": data.get("actual_temp_used"),
                "predicted_temp": data.get("predicted_temp"),
                "threshold": data.get("threshold"),
                "question_side": signal.get("question_side"),
            },
            "sources": _weather_snapshot_sources_from_signal_data(data, source_timestamp),
            "source_signal": {
                "signal_type": "weather",
                "predicted_prob": signal.get("predicted_prob"),
                "confidence": signal.get("confidence"),
                "source_timestamp": signal.get("source_timestamp"),
                "ttl_seconds": signal.get("ttl_seconds"),
                "question_side": signal.get("question_side"),
                "edge": signal.get("edge"),
                "data": copy.deepcopy(data),
            },
        }
        if mode == SOURCE_HISTORICAL_POST_FACTO:
            snapshot["source_provenance"] = "historical_post_facto_backfill"
            snapshot["provenance"] = {
                "source_mode": SOURCE_HISTORICAL_POST_FACTO,
                "source_provenance": "historical_post_facto_backfill",
                "anti_hindsight": "post_facto_weather_not_recorded_as_of",
                "reconstructed_from": path,
            }
        return _drop_empty_values(snapshot), path
    return None, ""


def _recorded_weather_signal_candidates(artifact: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    strategy_signal = artifact.get("strategy_signal") if isinstance(artifact.get("strategy_signal"), dict) else {}
    signal_details = strategy_signal.get("signal_details") if isinstance(strategy_signal.get("signal_details"), dict) else {}
    for name, value in signal_details.items():
        if isinstance(value, dict):
            yield f"decision_artifact.strategy_signal.signal_details.{name}", value
    if isinstance(strategy_signal, dict):
        yield "decision_artifact.strategy_signal", strategy_signal
    trace = artifact.get("strategy_trace") if isinstance(artifact.get("strategy_trace"), dict) else {}
    for container_name in ("accepted_signals", "rejected_signals", "raw_signals"):
        container = trace.get(container_name)
        if not isinstance(container, dict):
            continue
        for name, value in container.items():
            if isinstance(value, dict):
                yield f"decision_artifact.strategy_trace.{container_name}.{name}", value


def _looks_like_weather_signal(signal: dict[str, Any], data: dict[str, Any]) -> bool:
    if str(signal.get("signal_type") or "").lower() == "weather":
        return True
    return any(key in data for key in ("forecast_high", "forecast_low", "current_temp", "actual_temp_used", "historical_high", "historical_low"))


def _recorded_signal_is_post_facto(signal: dict[str, Any], data: dict[str, Any]) -> bool:
    if signal.get("historical_replay") is True or data.get("historical_replay") is True:
        return True
    values = (
        signal.get("mode"),
        signal.get("source_mode"),
        signal.get("source_provenance"),
        data.get("mode"),
        data.get("source_mode"),
        data.get("source_provenance"),
    )
    return any(_text_has_post_facto_token(value) for value in values)


def _weather_snapshot_sources_from_signal_data(data: dict[str, Any], as_of: Any) -> list[dict[str, Any]]:
    source_details = data.get("source_details")
    if isinstance(source_details, list) and source_details:
        return [copy.deepcopy(item) for item in source_details if isinstance(item, dict)]
    raw_sources = data.get("sources") or []
    if not isinstance(raw_sources, list):
        raw_sources = [raw_sources]
    sources = []
    for source in raw_sources:
        if source in (None, ""):
            continue
        sources.append(
            {
                "source_name": str(source),
                "fetched_at": data.get("fetched_at") or as_of,
                "as_of": data.get("as_of") or data.get("fetched_at") or as_of,
                "weather_date": data.get("weather_date"),
                "station_id": data.get("station_id"),
                "station_cli": data.get("station_cli"),
            }
        )
    return sources


def _drop_empty_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: cleaned for key, item in value.items() if (cleaned := _drop_empty_values(item)) not in (None, "", [], {})}
    if isinstance(value, list):
        return [cleaned for item in value if (cleaned := _drop_empty_values(item)) not in (None, "", [], {})]
    return value


def _resolve_snapshot_ref(artifact: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any] | None:
    ref = str(snapshot.get("snapshot_ref") or "").strip()
    if not ref:
        return None
    current: Any = artifact
    parts = ref.split(".")
    if parts and parts[0] == "decision_artifact":
        parts = parts[1:]
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current if isinstance(current, dict) else None


def _inventory_reasons(
    row: dict[str, Any],
    artifact: dict[str, Any],
    *,
    source_mode: str,
    order_book_mode: str,
    execution_mode: str,
    quality_reasons: Iterable[str],
    had_decision_artifact: bool,
    had_outcome_leakage: bool,
) -> list[str]:
    reasons: list[str] = []
    if not row.get("market_id"):
        reasons.append("missing_market_id")
    if not (row.get("observed_at") or row.get("timestamp")):
        reasons.append("missing_timestamp")
    if not had_decision_artifact:
        reasons.append("missing_decision_artifact")
    if source_mode != SOURCE_RECORDED_AS_OF:
        reasons.append("missing_source_snapshot" if source_mode == "missing" else f"source_mode_{source_mode}")
    if _is_weather_row(row, artifact) and not _weather_snapshot(artifact):
        reasons.append("missing_weather_snapshot")
    if _is_weather_row(row, artifact) and _weather_snapshot(artifact) and not _has_strict_weather_date_validation(artifact):
        reasons.append("missing_weather_date_validation")
    if order_book_mode == ORDER_BOOK_MISSING:
        reasons.append("missing_order_book_snapshot")
    if execution_mode not in {ORDER_BOOK_RECORDED, ORDER_BOOK_SIGNAL_PRICE_FALLBACK}:
        reasons.append("missing_execution_snapshot")
    if had_outcome_leakage:
        reasons.append("possible_outcome_leakage")
    for reason in quality_reasons:
        if reason not in reasons:
            reasons.append(reason)
    return reasons


def _tier_for_row(
    row: dict[str, Any],
    artifact: dict[str, Any],
    *,
    recovered: list[FieldProvenance],
    source_mode: str,
    order_book_mode: str,
    execution_mode: str,
    reasons: list[str],
    had_outcome_leakage: bool,
) -> str:
    if not row.get("market_id") or not (row.get("observed_at") or row.get("timestamp")) or not isinstance(artifact, dict):
        return TIER_UNUSABLE
    strict = (
        source_mode == SOURCE_RECORDED_AS_OF
        and order_book_mode == ORDER_BOOK_RECORDED
        and execution_mode in {ORDER_BOOK_RECORDED, ORDER_BOOK_SIGNAL_PRICE_FALLBACK}
        and not had_outcome_leakage
        and not any(reason.startswith("missing_") or reason == "possible_outcome_leakage" for reason in reasons)
    )
    if strict and recovered:
        return TIER_REPLAY_GRADE_BACKFILLED_FROM_ARTIFACT
    if strict:
        return TIER_REPLAY_GRADE_ORIGINAL
    execution_feasibility_non_strict = "missing_execution_feasibility" in reasons or any(
        reason.startswith("execution_feasibility_failed") for reason in reasons
    )
    if (
        recovered
        and source_mode == SOURCE_RECORDED_AS_OF
        and order_book_mode == ORDER_BOOK_RECORDED
        and execution_mode in {ORDER_BOOK_RECORDED, ORDER_BOOK_SIGNAL_PRICE_FALLBACK}
        and not had_outcome_leakage
        and not execution_feasibility_non_strict
    ):
        return TIER_REPLAY_GRADE_BACKFILLED_FROM_ARTIFACT
    return TIER_COVERAGE_ONLY


def _mark_duplicate_identities(rows: list[BackfilledRow]) -> None:
    seen: dict[tuple[str, ...], BackfilledRow] = {}
    for item in rows:
        identity = _identity(item.row)
        first = seen.get(identity)
        if first is None:
            seen[identity] = item
            continue
        if "duplicate_identity" not in first.reasons:
            first.reasons.append("duplicate_identity")
        if "duplicate_identity" not in item.reasons:
            item.reasons.append("duplicate_identity")
        if first.tier in {TIER_REPLAY_GRADE_ORIGINAL, TIER_REPLAY_GRADE_BACKFILLED_FROM_ARTIFACT}:
            first.tier = TIER_COVERAGE_ONLY
            _attach_provenance(
                first.row,
                first.row["decision_artifact"],
                tier=first.tier,
                recovered=first.recovered_fields,
                reasons=first.reasons,
                line_number=None,
                input_path=first.row.get("provenance", {}).get("input_path"),
            )
        if item.tier in {TIER_REPLAY_GRADE_ORIGINAL, TIER_REPLAY_GRADE_BACKFILLED_FROM_ARTIFACT}:
            item.tier = TIER_COVERAGE_ONLY
            _attach_provenance(
                item.row,
                item.row["decision_artifact"],
                tier=item.tier,
                recovered=item.recovered_fields,
                reasons=item.reasons,
                line_number=None,
                input_path=item.row.get("provenance", {}).get("input_path"),
            )


def _identity(row: dict[str, Any]) -> tuple[str, ...]:
    if row.get("prediction_id") not in (None, ""):
        return ("prediction", str(row.get("prediction_id")))
    return (
        "row",
        str(row.get("market_id") or ""),
        str(row.get("experiment_id") or ""),
        str(row.get("strategy_version") or ""),
        str(row.get("run_id") or ""),
        str(row.get("observed_at") or row.get("timestamp") or ""),
        str(row.get("snapshot_key") or ""),
    )


def _upgraded_ledger_rows(rows: list[BackfilledRow]) -> list[dict[str, Any]]:
    upgraded: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for item in rows:
        if item.tier == TIER_UNUSABLE:
            continue
        identity = _identity(item.row)
        if identity in seen:
            continue
        seen.add(identity)
        upgraded.append(item.row)
    return upgraded


def _duplicate_exclusion_count(rows: list[BackfilledRow]) -> int:
    seen: set[tuple[str, ...]] = set()
    excluded = 0
    for item in rows:
        identity = _identity(item.row)
        if item.tier == TIER_UNUSABLE:
            continue
        if identity in seen:
            excluded += 1
        else:
            seen.add(identity)
    return excluded


def _resolution_join_report(
    rows: list[BackfilledRow],
    *,
    resolution_paths: Iterable[str | Path] | None,
) -> dict[str, Any]:
    paths = [Path(path) for path in resolution_paths or []]
    if not paths:
        return {"checked": False, "missing_resolution_join": None, "resolution_paths": []}
    resolved_market_ids: set[str] = set()
    for path in paths:
        for row in load_jsonl(path):
            market_id = row.get("market_id")
            if market_id not in (None, ""):
                resolved_market_ids.add(str(market_id))
    missing = [
        str(item.row.get("market_id"))
        for item in rows
        if item.row.get("market_id") not in (None, "") and str(item.row.get("market_id")) not in resolved_market_ids
    ]
    return {
        "checked": True,
        "resolution_paths": [str(path) for path in paths],
        "resolved_market_count": len(resolved_market_ids),
        "missing_resolution_join": len(missing),
        "missing_market_ids_sample": missing[:25],
    }


def _attach_provenance(
    row: dict[str, Any],
    artifact: dict[str, Any],
    *,
    tier: str,
    recovered: list[FieldProvenance],
    reasons: list[str],
    line_number: int | None,
    input_path: str | None = None,
) -> None:
    payload = {
        "tier": tier,
        "sources": [item.to_dict() for item in recovered],
        "backfill_version": BACKFILL_VERSION,
        "reasons": list(reasons),
    }
    if line_number is not None:
        payload["input_line_number"] = line_number
    if input_path:
        payload["input_path"] = input_path
    row["provenance"] = payload
    artifact["provenance"] = payload


def _load_jsonl_rows(paths: Iterable[str | Path], *, limit: int | None = None, tail: bool = False) -> list[LoadedBackfillRow]:
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")
    if limit == 0:
        return []
    if tail:
        return _load_jsonl_rows_tail(paths, limit=limit)

    rows: list[LoadedBackfillRow] = []
    for path_value in paths:
        path = Path(path_value)
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as fh:
            for line_number, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                rows.append(LoadedBackfillRow(row=row, source_path=str(path), line_number=line_number))
                if limit is not None and len(rows) >= limit:
                    return rows
    return rows


def _load_jsonl_rows_tail(paths: Iterable[str | Path], *, limit: int | None = None) -> list[LoadedBackfillRow]:
    from collections import deque

    maxlen = limit
    if maxlen is None:
        return _load_jsonl_rows(paths, limit=None, tail=False)
    rows: deque[LoadedBackfillRow] = deque(maxlen=maxlen)
    for path_value in paths:
        path = Path(path_value)
        if not path.exists():
            continue
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
                    rows.append(LoadedBackfillRow(row=row, source_path=str(path), line_number=line_number))
    return list(rows)


def _validation_payload(
    *,
    validate_output: bool,
    output_path: Path,
    resolution_paths: Iterable[str | Path] | None,
) -> dict[str, Any]:
    if not validate_output:
        return {
            "ok": None,
            "skipped": True,
            "reason": "validate_output_disabled",
            "checked_paths": [str(output_path)],
            "total_rows": None,
            "issue_counts": {},
            "severity_counts": {},
            "issues": [],
        }
    payload = validate_prediction_lab_tables([output_path], resolution_paths=resolution_paths).to_dict()
    payload["skipped"] = False
    return payload


def _validation_summary(validation: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": validation.get("ok"),
        "skipped": validation.get("skipped", False),
        "issue_counts": validation.get("issue_counts", {}),
        "severity_counts": validation.get("severity_counts", {}),
        "total_rows": validation.get("total_rows"),
    }


def _legacy_artifact_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "market_id": row.get("market_id"),
        "final_action": row.get("direction") or "SKIP",
        "final_reason_code": row.get("decision_reason_code") or row.get("reason_code"),
        "source_context": {"source": "missing", "mode": "legacy", "data": {}},
        "order_book_snapshot": {"source": "missing", "data": None},
        "execution_snapshot_source": "missing",
    }


def _strip_outcome_leakage(value: Any, *, top_level: bool = True) -> Any:
    if isinstance(value, dict):
        stripped: dict[str, Any] = {}
        keys = TOP_LEVEL_OUTCOME_KEYS if top_level else OUTCOME_LEAKAGE_KEYS
        for key, item in value.items():
            if str(key).lower() in keys:
                continue
            stripped[key] = _strip_outcome_leakage(item, top_level=False)
        return stripped
    if isinstance(value, list):
        return [_strip_outcome_leakage(item, top_level=False) for item in value]
    return value


def _has_outcome_leakage(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key).lower()
            if key_text in TOP_LEVEL_OUTCOME_KEYS and _leakage_value_present(item):
                return True
            if key_text in OUTCOME_LEAKAGE_KEYS and _leakage_value_present(item):
                return True
            if _has_outcome_leakage(item):
                return True
    if isinstance(value, list):
        return any(_has_outcome_leakage(item) for item in value)
    return False


def _leakage_value_present(value: Any) -> bool:
    if value in (None, "", [], {}):
        return False
    if isinstance(value, dict):
        return any(_leakage_value_present(item) for item in value.values())
    if isinstance(value, list):
        return any(_leakage_value_present(item) for item in value)
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _weather_snapshot(artifact: dict[str, Any]) -> dict[str, Any] | None:
    source_context = artifact.get("source_context") if isinstance(artifact.get("source_context"), dict) else {}
    data = source_context.get("data") if isinstance(source_context.get("data"), dict) else {}
    snapshot = data.get("weather_source_snapshot")
    return snapshot if isinstance(snapshot, dict) and snapshot else None


def _has_strict_weather_date_validation(artifact: dict[str, Any]) -> bool:
    snapshot = _weather_snapshot(artifact) or {}
    validation = snapshot.get("date_validation")
    if not isinstance(validation, dict) or validation.get("ok") is not True:
        return False
    market_date = _date_text(validation.get("market_date"))
    weather_date = _date_text(validation.get("weather_date"))
    return bool(market_date and weather_date and market_date == weather_date)


def _date_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    return text[:10] if len(text) >= 10 else text


def _has_executable_asks(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return _usable_price(value.get("best_yes_ask")) or _usable_price(value.get("best_no_ask"))


def _usable_price(value: Any) -> bool:
    if value in (None, ""):
        return False
    try:
        price = float(value)
    except (TypeError, ValueError):
        return False
    return 0.0 < price < 1.0


def _is_weather_row(row: dict[str, Any], artifact: dict[str, Any]) -> bool:
    values = [
        row.get("group"),
        row.get("series"),
        row.get("event_ticker"),
        row.get("market_id"),
        row.get("question"),
        artifact.get("market_id"),
    ]
    source_context = artifact.get("source_context") if isinstance(artifact.get("source_context"), dict) else {}
    data = source_context.get("data") if isinstance(source_context.get("data"), dict) else {}
    metadata = data.get("market_metadata") if isinstance(data.get("market_metadata"), dict) else {}
    values.extend((metadata.get("market_group"), metadata.get("series"), metadata.get("event_ticker")))
    joined = " ".join(str(value or "").lower() for value in values)
    return any(token in joined for token in ("weather", "temperature", "daily_temperature", "kxhigh", "kxlow"))


def _row_group(row: dict[str, Any]) -> str:
    return str(row.get("group") or row.get("market_group") or "unknown")


def _row_series(row: dict[str, Any]) -> str:
    return str(row.get("series") or "unknown")


def _row_event_ticker(row: dict[str, Any]) -> str:
    return str(row.get("event_ticker") or "unknown")


def _row_date(row: dict[str, Any]) -> str:
    value = row.get("observed_at") or row.get("timestamp") or ""
    text = str(value)
    return text[:10] if len(text) >= 10 else "unknown"


def _counts(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
