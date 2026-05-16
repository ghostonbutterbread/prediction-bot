"""Replay Prediction Lab collector artifacts through the shared evaluator."""

from __future__ import annotations

import logging
import gzip
import json
import re
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

from bot.agent_decision_ledger import (
    load_agent_decision_rows,
    summarize_agent_decision_coverage,
    summarize_agent_decision_reporting,
)
from bot.decision_pipeline import (
    DecisionPipelineEvaluator,
    build_fixed_opportunity_account_state,
    build_fixed_opportunity_risk_policy,
)
from bot.file_ops import load_jsonl
from bot.hidden_gem_evidence import (
    extract_hidden_gem_evidence_card,
    summarize_hidden_gem_evidence_cards,
)
from bot.prediction_lab_shadow_delta import summarize_shadow_delta_rows
from bot.shared_market_feed import (
    shared_candidate_from_market_snapshot_row,
    shared_candidate_id_from_row,
    summarize_dual_policy_pnl_snapshot_rows,
    summarize_dual_policy_snapshot_rows,
)
from bot.strategy_lane_reporting import summarize_strategy_lanes
from bot.weather.source_reliability import (
    SourceReliabilityTable,
    build_rolling_source_reliability_table,
    build_source_reliability_shadow_row,
    evaluate_source_reliability_candidate,
    load_source_outcome_ledger_rows,
    summarize_source_reliability_shadow_rows,
)

logger = logging.getLogger(__name__)


SOURCE_RECORDED_AS_OF = "recorded_as_of"
SOURCE_HISTORICAL_POST_FACTO = "historical_post_facto"
SOURCE_LIVE_CURRENT_FORBIDDEN = "live_current_forbidden"
SOURCE_SYNTHETIC = "synthetic"
SOURCE_MISSING = "missing"

ORDER_BOOK_RECORDED = "recorded_book"
ORDER_BOOK_SIGNAL_PRICE_FALLBACK = "signal_price_fallback"
ORDER_BOOK_SYNTHETIC = "synthetic"
ORDER_BOOK_MISSING = "missing"

QUALITY_REPLAY_GRADE_ORIGINAL = "replay_grade_original"
QUALITY_REPLAY_GRADE_BACKFILLED = "replay_grade_backfilled"
QUALITY_COVERAGE_ONLY = "coverage_only"
QUALITY_MISSING_WEATHER_SNAPSHOT = "missing_weather_snapshot"
QUALITY_MISSING_ORDER_BOOK = "missing_order_book"
QUALITY_DATE_UNVERIFIED = "date_unverified"
QUALITY_LIVE_SOURCE_FORBIDDEN = "live_source_forbidden"
QUALITY_SYNTHETIC_SOURCE = "synthetic_source"
QUALITY_HISTORICAL_POST_FACTO = "historical_post_facto"
QUALITY_MISSING_SOURCE = "missing_source"

SHADOW_DELTA_COMPACT_REVIEW_ROW_TYPE = "prediction_lab_shadow_delta_compact_review"

LIVE_CURRENT_SOURCE_METHODS = (
    "_live_data_signal",
    "_news_signal",
    "_social_signal",
    "_ai_signal",
    "live_data_signal",
    "news_signal",
    "social_signal",
    "ai_signal",
    "get_live_data_signal",
    "get_news_signal",
    "get_social_signal",
    "get_ai_signal",
)

SOURCE_SIGNAL_METHODS = {
    "live": "_live_data_signal",
    "live_data": "_live_data_signal",
    "weather": "_live_data_signal",
    "crypto": "_live_data_signal",
    "forex": "_live_data_signal",
    "news": "_news_signal",
    "social": "_social_signal",
    "twitter": "_social_signal",
    "x": "_social_signal",
    "ai": "_ai_signal",
    "llm": "_ai_signal",
}

SOURCE_CONTEXT_METADATA_KEYS = {
    "market_metadata",
    "metadata",
    "market",
    "market_id",
    "event_ticker",
    "series",
    "group",
    "category",
}

WEATHER_HIDDEN_GEM_HOTFIX_BRIDGE_REASON_CODES = {
    "weather_bucket_hidden_gem_missing_distribution_probability",
    "weather_tail_hidden_gem_live_probability_mismatch",
}


class LiveCurrentSourceForbiddenError(RuntimeError):
    """Raised when historical replay would touch current live source data."""


@dataclass(slots=True)
class ReplayArtifactInput:
    row: dict[str, Any]
    artifact: dict[str, Any]
    source_path: str | None = None
    line_number: int | None = None


@dataclass(slots=True)
class ReplayRowQuality:
    category: str
    reasons: list[str] = field(default_factory=list)
    is_replay_grade_strict: bool = False
    include_in_strict: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ReplayComparisonRow:
    market_id: str
    series: str | None
    event_ticker: str | None
    prediction_id: str | None
    run_id: str | None
    experiment_id: str | None
    strategy_version: str | None
    shared_candidate_id: str | None
    original_action: str
    replayed_action: str
    original_reason_code: str | None
    replayed_reason_code: str | None
    action_changed: bool
    reason_changed: bool
    source_mode: str
    order_book_mode: str
    execution_snapshot_mode: str
    quality: dict[str, Any]
    category: str
    reasons: list[str]
    is_replay_grade_strict: bool
    include_in_strict: bool
    warnings: list[str] = field(default_factory=list)
    source_path: str | None = None
    line_number: int | None = None
    original_artifact: dict[str, Any] | None = None
    replayed_artifact: dict[str, Any] | None = None
    outcome: str | None = None
    missed_win: bool = False
    bad_buy_removed: bool = False
    bad_buy_added: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PredictionLabValidationIssue:
    severity: str
    code: str
    message: str
    source_path: str | None = None
    line_number: int | None = None
    market_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PredictionLabValidationResult:
    total_rows: int
    checked_paths: list[str]
    issues: list[PredictionLabValidationIssue]

    @property
    def ok(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "total_rows": self.total_rows,
            "checked_paths": list(self.checked_paths),
            "issue_counts": _counts(issue.code for issue in self.issues),
            "severity_counts": _counts(issue.severity for issue in self.issues),
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(slots=True)
class PredictionLabReplayResult:
    rows: list[ReplayComparisonRow]
    summary: dict[str, Any]
    all_rows: list[ReplayComparisonRow] = field(default_factory=list)
    source_reliability_shadow_rows: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        coverage_rows = self.all_rows or self.rows
        return {
            "summary": dict(self.summary),
            "rows": [row.to_dict() for row in self.rows],
            "all_rows": [row.to_dict() for row in coverage_rows],
            "source_reliability_shadow_rows": list(self.source_reliability_shadow_rows),
        }


@dataclass(frozen=True, slots=True)
class PredictionLabReplayDataset:
    dataset_id: str
    path: str
    kind: str
    min_observed_at: str | None
    max_observed_at: str | None
    months: tuple[str, ...]
    row_count: int
    quality: str
    exclusion_reason: str | None = None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "path": self.path,
            "kind": self.kind,
            "min_observed_at": self.min_observed_at,
            "max_observed_at": self.max_observed_at,
            "months": list(self.months),
            "row_count": self.row_count,
            "quality": self.quality,
            "exclusion_reason": self.exclusion_reason,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class PredictionLabReplayWindow:
    selection_mode: str
    requested_months: int | str | None
    selected_months: int
    available_months: int
    anchor_month: str | None
    fallback_reason: str | None
    datasets: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    backfill_recommendation: str | None = None
    selected_month_list: tuple[str, ...] = ()
    dataset_details: tuple[PredictionLabReplayDataset, ...] = ()
    available_dataset_details: tuple[PredictionLabReplayDataset, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "selection_mode": self.selection_mode,
            "requested_months": self.requested_months,
            "selected_months": self.selected_months,
            "available_months": self.available_months,
            "anchor_month": self.anchor_month,
            "fallback_reason": self.fallback_reason,
            "datasets": list(self.datasets),
            "warnings": list(self.warnings),
        }
        if self.backfill_recommendation:
            payload["backfill_recommendation"] = self.backfill_recommendation
        if self.selected_month_list:
            payload["selected_month_list"] = list(self.selected_month_list)
        if self.dataset_details:
            payload["dataset_details"] = [dataset.to_dict() for dataset in self.dataset_details]
        if self.available_dataset_details:
            payload["available_dataset_details"] = [dataset.to_dict() for dataset in self.available_dataset_details]
        return payload


REPLAY_DATASET_FILENAMES = ("replay_rows.jsonl", "market_snapshots.jsonl", "predictions.jsonl")
REPLAY_DATASET_STEMS = tuple(filename.removesuffix(".jsonl") for filename in REPLAY_DATASET_FILENAMES)


def explicit_prediction_lab_replay_window(paths: Iterable[str | Path]) -> PredictionLabReplayWindow:
    dataset_paths = tuple(str(Path(path)) for path in paths)
    return PredictionLabReplayWindow(
        selection_mode="explicit_path",
        requested_months=None,
        selected_months=0,
        available_months=0,
        anchor_month=None,
        fallback_reason=None,
        datasets=dataset_paths,
    )


def discover_prediction_lab_replay_datasets(roots: Iterable[str | Path]) -> list[PredictionLabReplayDataset]:
    """Discover local Prediction Lab replay datasets and derive their covered months."""

    candidates: list[PredictionLabReplayDataset] = []
    seen: set[str] = set()
    for root_value in roots:
        root = Path(root_value)
        for path in _iter_replay_dataset_paths(root):
            key = str(path.expanduser().resolve(strict=False))
            if key in seen:
                continue
            seen.add(key)
            candidates.append(_inspect_replay_dataset(path))
    return sorted(candidates, key=lambda dataset: (dataset.months[-1] if dataset.months else "", dataset.path))


def select_prediction_lab_replay_window(
    roots: Iterable[str | Path],
    *,
    months: int | str,
) -> PredictionLabReplayWindow:
    _normalize_requested_months(months)
    datasets = discover_prediction_lab_replay_datasets(roots)
    return select_prediction_lab_replay_window_from_datasets(datasets, months=months)


def select_prediction_lab_replay_window_from_datasets(
    datasets: Iterable[PredictionLabReplayDataset],
    *,
    months: int | str,
) -> PredictionLabReplayWindow:
    dataset_list = list(datasets)
    requested = _normalize_requested_months(months)
    mode = "months_all" if requested == "all" else "months"
    warnings: list[str] = []
    needs_backfill = False
    selectable_by_month: dict[str, list[PredictionLabReplayDataset]] = {}

    for dataset in dataset_list:
        if dataset.warnings:
            warnings.extend(f"{Path(dataset.path).name}: {warning}" for warning in dataset.warnings)
        if dataset.quality in {"invalid", "partial"}:
            needs_backfill = True
        if dataset.exclusion_reason:
            warnings.append(f"{Path(dataset.path).name}: {dataset.exclusion_reason}")
        if dataset.quality == "invalid":
            continue
        for month in dataset.months:
            selectable_by_month.setdefault(month, []).append(dataset)

    available_months = sorted(selectable_by_month)
    if not available_months:
        raise ValueError("no valid replay datasets found")

    anchor_month = available_months[-1]
    fallback_reason = None
    if requested == "all":
        selected_months = available_months
    else:
        requested_count = int(requested)
        if requested_count > len(available_months):
            fallback_reason = "requested_window_exceeds_available_data"
            selected_months = available_months
        else:
            selected_months = available_months[-requested_count:]

    selected_datasets_by_path: dict[str, PredictionLabReplayDataset] = {}
    for month in selected_months:
        month_datasets = sorted(selectable_by_month[month], key=_dataset_selection_key, reverse=True)
        if len(month_datasets) > 1:
            warnings.append(
                "overlapping_replay_datasets_for_month:"
                f"{month}:"
                f"{','.join(dataset.path for dataset in month_datasets)}"
            )
        selected = month_datasets[0]
        selected_datasets_by_path.setdefault(selected.path, selected)
        if selected.quality == "partial":
            warnings.append(f"{Path(selected.path).name}: selected_partial_dataset")

    selected_datasets = tuple(selected_datasets_by_path)
    selected_details = tuple(selected_datasets_by_path.values())
    return PredictionLabReplayWindow(
        selection_mode=mode,
        requested_months=requested,
        selected_months=len(selected_months),
        available_months=len(available_months),
        anchor_month=anchor_month,
        fallback_reason=fallback_reason,
        datasets=selected_datasets,
        warnings=tuple(dict.fromkeys(warnings)),
        backfill_recommendation=(
            "Run Prediction Lab backfill to populate missing replay-grade data before relying on coverage."
            if needs_backfill
            else None
        ),
        selected_month_list=tuple(selected_months),
        dataset_details=selected_details,
        available_dataset_details=tuple(dataset_list),
    )


def _iter_jsonl_dict_rows(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    if not path.exists():
        return
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rt", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    yield line_number, row
    except OSError:
        return


def load_replay_artifacts(paths: Iterable[str | Path], *, limit: int | None = None) -> list[ReplayArtifactInput]:
    """Load collector prediction/snapshot JSONL rows that contain replayable artifacts."""

    records: list[ReplayArtifactInput] = []
    for path_value in paths:
        path = Path(path_value)
        for index, row in _iter_jsonl_dict_rows(path):
            if _is_shadow_delta_compact_review_row(row):
                raise ValueError(f"compact shadow-delta review rows are not replay inputs: {path}:{index}")
            replay_row = _strip_inline_outcomes(row)
            artifact = row.get("decision_artifact")
            if not isinstance(artifact, dict):
                artifact = _legacy_artifact_from_row(replay_row)
            records.append(
                ReplayArtifactInput(
                    row=replay_row,
                    artifact=_strip_artifact_outcomes(artifact),
                    source_path=str(path),
                    line_number=index,
                )
            )
            if limit is not None and len(records) >= limit:
                return records
    return records


def replay_recorded_artifacts(
    records: Iterable[ReplayArtifactInput | dict[str, Any]],
    *,
    config: dict[str, Any] | None = None,
    evaluator: DecisionPipelineEvaluator | None = None,
    bankroll_usd: float = 100.0,
    live_source_policy: str = "fail",
    require_recorded_source: bool = False,
    row_quality_policy: str = "annotate",
    resolution_records: Iterable[dict[str, Any]] | None = None,
    resolution_paths: Iterable[str | Path] | None = None,
    decision_records: Iterable[dict[str, Any]] | None = None,
    decision_paths: Iterable[str | Path] | None = None,
    source_reliability_scoreboard: str | Path | None = None,
    source_reliability_ledger: str | Path | None = None,
) -> PredictionLabReplayResult:
    """Replay recorded artifacts and compare original vs replayed decisions.

    ``live_source_policy`` controls attempts to call current source feeds during
    historical replay:

    - ``fail`` raises ``LiveCurrentSourceForbiddenError``.
    - ``warn_skip`` replaces the live call with ``None`` and records a warning.
    - ``allow`` leaves the evaluator untouched and labels the run as unsafe.
    """

    replay_config = _replay_safe_config(config or {})
    replay_evaluator = evaluator or DecisionPipelineEvaluator(
        replay_config,
        risk_policy=build_fixed_opportunity_risk_policy(replay_config, bankroll_usd=bankroll_usd),
    )
    rows: list[ReplayComparisonRow] = []
    all_rows: list[ReplayComparisonRow] = []
    shadow_delta_rows: list[dict[str, Any]] = []
    dual_policy_rows: list[dict[str, Any]] = []
    source_reliability_shadow_rows: list[dict[str, Any]] = []
    if source_reliability_scoreboard not in (None, "") and source_reliability_ledger not in (None, ""):
        raise ValueError("provide either source_reliability_scoreboard or source_reliability_ledger, not both")
    source_reliability_table = (
        SourceReliabilityTable.from_path(source_reliability_scoreboard)
        if source_reliability_scoreboard not in (None, "")
        else None
    )
    source_reliability_ledger_rows = (
        load_source_outcome_ledger_rows(source_reliability_ledger)
        if source_reliability_ledger not in (None, "")
        else None
    )
    policy = str(live_source_policy or "fail").lower()
    if policy not in {"fail", "warn_skip", "allow"}:
        raise ValueError("live_source_policy must be one of: fail, warn_skip, allow")
    quality_policy = str(row_quality_policy or "annotate").lower()
    if quality_policy not in {"annotate", "include_all", "strict", "drop_incomplete", "strict_only"}:
        raise ValueError("row_quality_policy must be one of: annotate, include_all, strict, drop_incomplete, strict_only")

    for raw_record in records:
        record = _coerce_record(raw_record)
        if isinstance(record.row.get("shadow_delta"), dict):
            shadow_row = dict(record.row)
            if record.source_path:
                shadow_row["_source_path"] = record.source_path
            shadow_delta_rows.append(shadow_row)
        if _has_dual_policy_metadata(record.row):
            dual_policy_rows.append(dict(record.row))
        original_artifact = record.artifact
        original_action = _normalize_action(original_artifact.get("final_action") or _stored_action(record.row))
        original_reason = _coerce_reason(original_artifact.get("final_reason_code") or _shared_pipeline_reason(record.row))
        market = _market_from_record(record)
        source_mode = classify_source_mode(original_artifact, record.row)
        order_book_mode = classify_order_book_mode(original_artifact)
        execution_mode = classify_execution_snapshot_mode(original_artifact)
        row_warnings = _source_mode_warnings(source_mode, order_book_mode)
        quality = classify_replay_row_quality(
            original_artifact,
            record.row,
            source_mode=source_mode,
            order_book_mode=order_book_mode,
            execution_snapshot_mode=execution_mode,
            warnings=row_warnings,
        )

        if source_mode == SOURCE_LIVE_CURRENT_FORBIDDEN and policy == "fail":
            raise LiveCurrentSourceForbiddenError(
                f"replay input {record.source_path or '<memory>'}:{record.line_number or '?'} "
                f"for {getattr(market, 'id', '')} is labeled live_current_forbidden"
            )
        if require_recorded_source and source_mode != SOURCE_RECORDED_AS_OF:
            raise LiveCurrentSourceForbiddenError(
                f"replay input {record.source_path or '<memory>'}:{record.line_number or '?'} "
                f"for {getattr(market, 'id', '')} has source_mode={source_mode}; recorded_as_of required"
            )

        order_book = _recorded_order_book(original_artifact)
        source_context = _recorded_source_context(original_artifact)
        execution_snapshot = _recorded_execution_snapshot(original_artifact)
        source_signals = _recorded_source_signals(original_artifact)
        if policy == "allow":
            row_warnings.append("live_current_source_policy_allow_is_unsafe_for_historical_replay")

        with _patch_recorded_source_methods(replay_evaluator, source_signals) as replayed_source_methods:
            with _guard_live_current_source(
                replay_evaluator,
                policy=policy,
                warnings=row_warnings,
                market_id=str(getattr(market, "id", "")),
                skip_methods=replayed_source_methods,
            ):
                replayed = replay_evaluator.evaluate(
                    market,
                    account_state=build_fixed_opportunity_account_state(bankroll_usd),
                    order_book=order_book,
                    source_context=source_context,
                    execution_snapshot=execution_snapshot,
                    mode="prediction_lab_replay",
                    config_snapshot=replay_config,
                    as_of=_record_as_of(original_artifact, record.row),
                ).to_dict()

        replayed_action = _normalize_action(replayed.get("final_action"))
        replayed_reason = _coerce_reason(replayed.get("final_reason_code"))
        row = ReplayComparisonRow(
            market_id=str(getattr(market, "id", "") or ""),
            series=_record_series(record, market),
            event_ticker=_record_event_ticker(record, market),
            prediction_id=_record_prediction_id(record),
            run_id=_record_run_id(record),
            experiment_id=_record_experiment_id(record),
            strategy_version=_record_strategy_version(record),
            shared_candidate_id=shared_candidate_id_from_row(record.row),
            original_action=original_action,
            replayed_action=replayed_action,
            original_reason_code=original_reason,
            replayed_reason_code=replayed_reason,
            action_changed=original_action != replayed_action,
            reason_changed=original_reason != replayed_reason,
            source_mode=source_mode,
            order_book_mode=order_book_mode,
            execution_snapshot_mode=execution_mode,
            quality=quality.to_dict(),
            category=quality.category,
            reasons=list(quality.reasons),
            is_replay_grade_strict=quality.is_replay_grade_strict,
            include_in_strict=quality.include_in_strict,
            warnings=row_warnings,
            source_path=record.source_path,
            line_number=record.line_number,
            original_artifact=original_artifact,
            replayed_artifact=replayed,
            outcome=None,
        )
        all_rows.append(row)
        if source_reliability_ledger_rows is not None:
            source_reliability_shadow_rows.append(
                _build_rolling_source_reliability_shadow_row(
                    record,
                    original_artifact=original_artifact,
                    stable_action=original_action,
                    replayed_action=replayed_action,
                    ledger_rows=source_reliability_ledger_rows,
                )
            )
        elif source_reliability_table is not None:
            reliability_input = dict(record.row)
            reliability_input["decision_artifact"] = original_artifact
            reliability_input["replayed_action"] = replayed_action
            evaluation = evaluate_source_reliability_candidate(
                reliability_input,
                source_reliability_table,
                action=replayed_action,
            )
            source_reliability_shadow_rows.append(
                build_source_reliability_shadow_row(
                    reliability_input,
                    evaluation,
                    stable_action=original_action,
                    replayed_action=replayed_action,
                )
            )
        if quality_policy in {"strict", "drop_incomplete", "strict_only"} and not row.include_in_strict:
            continue
        rows.append(row)

    loaded_resolutions = _load_resolution_records(resolution_records=resolution_records, resolution_paths=resolution_paths)
    loaded_decisions = _load_agent_decision_records(decision_records=decision_records, decision_paths=decision_paths)
    _apply_resolution_scoring(all_rows, resolution_records=loaded_resolutions)
    _apply_resolution_outcomes(dual_policy_rows, resolution_records=loaded_resolutions)
    return PredictionLabReplayResult(
        rows=rows,
        summary=_summarize(
            rows,
            all_rows=all_rows,
            shadow_delta_rows=shadow_delta_rows,
            dual_policy_rows=dual_policy_rows,
            agent_decision_rows=loaded_decisions,
            source_reliability_shadow_rows=source_reliability_shadow_rows,
        ),
        all_rows=all_rows,
        source_reliability_shadow_rows=source_reliability_shadow_rows,
    )


def replay_from_paths(
    paths: Iterable[str | Path],
    *,
    config: dict[str, Any] | None = None,
    evaluator: DecisionPipelineEvaluator | None = None,
    limit: int | None = None,
    bankroll_usd: float = 100.0,
    live_source_policy: str = "fail",
    require_recorded_source: bool = False,
    row_quality_policy: str = "annotate",
    resolution_paths: Iterable[str | Path] | None = None,
    decision_paths: Iterable[str | Path] | None = None,
    replay_window: PredictionLabReplayWindow | dict[str, Any] | None = None,
    source_reliability_scoreboard: str | Path | None = None,
    source_reliability_ledger: str | Path | None = None,
) -> PredictionLabReplayResult:
    records = load_replay_artifacts(paths, limit=limit)
    result = replay_recorded_artifacts(
        records,
        config=config,
        evaluator=evaluator,
        bankroll_usd=bankroll_usd,
        live_source_policy=live_source_policy,
        require_recorded_source=require_recorded_source,
        row_quality_policy=row_quality_policy,
        resolution_paths=resolution_paths,
        decision_paths=decision_paths,
        source_reliability_scoreboard=source_reliability_scoreboard,
        source_reliability_ledger=source_reliability_ledger,
    )
    if replay_window is not None:
        result.summary["replay_window"] = replay_window.to_dict() if hasattr(replay_window, "to_dict") else dict(replay_window)
    return result


def _build_rolling_source_reliability_shadow_row(
    record: ReplayArtifactInput,
    *,
    original_artifact: dict[str, Any],
    stable_action: str,
    replayed_action: str,
    ledger_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    reliability_input = dict(record.row)
    reliability_input["decision_artifact"] = original_artifact
    reliability_input["replayed_action"] = replayed_action
    as_of = _source_reliability_candidate_as_of(record)
    total_rows = len(ledger_rows)
    eligible_rows = sum(1 for row in ledger_rows if isinstance(row, dict) and row.get("eligible_for_reliability") is True)
    base_metadata = {
        "reliability_mode": "rolling_as_of",
        "as_of": _dt_iso_utc(as_of),
        "ledger_total_rows": total_rows,
        "ledger_eligible_rows": eligible_rows,
    }
    if as_of is None:
        market_id = reliability_input.get("market_id") or reliability_input.get("snapshot_key") or original_artifact.get("market_id")
        return {
            "row_type": "source_reliability_shadow",
            "schema_version": 1,
            "market_id": str(market_id or ""),
            "shared_candidate_id": reliability_input.get("shared_candidate_id"),
            "stable_action": stable_action,
            "replayed_action": replayed_action,
            "reliability_recommended_action": "SKIP",
            "reliability_effect": "unavailable",
            "reason_code": "missing_candidate_observed_at",
            "trusted_support_count": 0,
            "excluded_dissent_count": 0,
            "weighted_support": 0.0,
            "weighted_dissent": 0.0,
            "tier_counts": {},
            "source_votes": [],
            "ledger_considered_rows": 0,
            **base_metadata,
        }

    table = build_rolling_source_reliability_table(ledger_rows, as_of=as_of)
    evaluation = evaluate_source_reliability_candidate(
        reliability_input,
        table,
        action=replayed_action,
    )
    shadow_row = build_source_reliability_shadow_row(
        reliability_input,
        evaluation,
        stable_action=stable_action,
        replayed_action=replayed_action,
    )
    shadow_row.update(base_metadata)
    shadow_row["ledger_considered_rows"] = _count_source_reliability_ledger_rows_known_before(ledger_rows, as_of)
    shadow_row["source_votes"] = list(evaluation.source_votes)
    return shadow_row


def _source_reliability_candidate_as_of(record: ReplayArtifactInput) -> datetime | None:
    artifact = record.artifact if isinstance(record.artifact, dict) else {}
    candidates = (
        record.row.get("observed_at"),
        record.row.get("timestamp"),
        artifact.get("observed_at"),
        artifact.get("timestamp"),
    )
    for value in candidates:
        parsed = _parse_dt_utc(value)
        if parsed is not None:
            return parsed
    return None


def _count_source_reliability_ledger_rows_known_before(
    ledger_rows: Iterable[dict[str, Any]],
    as_of: datetime,
) -> int:
    count = 0
    for row in ledger_rows:
        if not isinstance(row, dict) or row.get("eligible_for_reliability") is not True:
            continue
        known_after = _parse_dt_utc(row.get("known_after"))
        if known_after is not None and known_after < as_of:
            count += 1
    return count


def _parse_dt_utc(value: Any) -> datetime | None:
    parsed = _parse_dt(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _dt_iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _parse_dt_utc(value).isoformat() if _parse_dt_utc(value) is not None else None


def _replay_safe_config(config: dict[str, Any]) -> dict[str, Any]:
    replay_config = dict(config or {})
    prediction_lab_cfg = replay_config.get("prediction_lab") if isinstance(replay_config.get("prediction_lab"), dict) else {}
    strategy_cfg = dict(replay_config.get("strategy") or {})
    for lab_key, strategy_key in (
        ("disable_news", "enable_news"),
        ("disable_social", "enable_social"),
        ("disable_ai", "enable_ai"),
    ):
        if bool(prediction_lab_cfg.get(lab_key, False)):
            strategy_cfg[strategy_key] = False
    if strategy_cfg or "strategy" in replay_config:
        replay_config["strategy"] = strategy_cfg
    return replay_config


def classify_source_mode(artifact: dict[str, Any], row: dict[str, Any] | None = None) -> str:
    snapshots = artifact.get("source_snapshots")
    if isinstance(snapshots, list) and snapshots:
        modes = {str(snapshot.get("mode") or snapshot.get("source_mode") or "").lower() for snapshot in snapshots if isinstance(snapshot, dict)}
        snapshot_sources = {
            str(snapshot.get("source") or snapshot.get("source_name") or "").lower()
            for snapshot in snapshots
            if isinstance(snapshot, dict)
        }
        if SOURCE_LIVE_CURRENT_FORBIDDEN in modes or (modes & {"live_current", "current", "live"}) or (snapshot_sources & {"live_current", "current"}):
            return SOURCE_LIVE_CURRENT_FORBIDDEN
        has_evidence = any(_snapshot_has_source_evidence(snapshot) for snapshot in snapshots if isinstance(snapshot, dict))
        has_post_facto_evidence = any(
            _snapshot_has_historical_post_facto_provenance(artifact, snapshot)
            for snapshot in snapshots
            if isinstance(snapshot, dict)
        )
        if has_post_facto_evidence and (has_evidence or _source_context_has_evidence((artifact.get("source_context") or {}).get("data") if isinstance(artifact.get("source_context"), dict) else None)):
            return SOURCE_HISTORICAL_POST_FACTO
        if SOURCE_HISTORICAL_POST_FACTO in modes and has_evidence:
            return SOURCE_HISTORICAL_POST_FACTO
        if SOURCE_RECORDED_AS_OF in modes and has_evidence:
            return SOURCE_RECORDED_AS_OF
        if SOURCE_SYNTHETIC in modes and has_evidence:
            return SOURCE_SYNTHETIC
        if has_evidence:
            return SOURCE_RECORDED_AS_OF

    source_context = artifact.get("source_context") if isinstance(artifact.get("source_context"), dict) else {}
    mode = str(source_context.get("source_mode") or source_context.get("mode") or "").lower()
    data = source_context.get("data")
    has_context_evidence = _source_context_has_evidence(data)
    source = str(source_context.get("source") or "").lower()
    if mode == SOURCE_LIVE_CURRENT_FORBIDDEN or mode in {"live", "live_current", "current"} or source in {"live", "live_current", "current"}:
        return SOURCE_LIVE_CURRENT_FORBIDDEN
    if has_context_evidence and _has_historical_post_facto_provenance(data):
        return SOURCE_HISTORICAL_POST_FACTO
    if mode in {SOURCE_RECORDED_AS_OF, SOURCE_HISTORICAL_POST_FACTO, SOURCE_LIVE_CURRENT_FORBIDDEN, SOURCE_SYNTHETIC, SOURCE_MISSING}:
        if mode in {SOURCE_RECORDED_AS_OF, SOURCE_HISTORICAL_POST_FACTO, SOURCE_SYNTHETIC} and not has_context_evidence:
            return SOURCE_MISSING
        return mode
    if mode in {"historical", "historical_replay", "post_facto"}:
        if not has_context_evidence:
            return SOURCE_MISSING
        return SOURCE_HISTORICAL_POST_FACTO

    if source in {"historical", "post_facto"}:
        if not has_context_evidence:
            return SOURCE_MISSING
        return SOURCE_HISTORICAL_POST_FACTO
    if source == "synthetic":
        if not has_context_evidence:
            return SOURCE_MISSING
        return SOURCE_SYNTHETIC
    if source == "provided" and has_context_evidence:
        return SOURCE_RECORDED_AS_OF
    if row and row.get("decision_artifact"):
        return SOURCE_MISSING
    return SOURCE_MISSING


def classify_order_book_mode(artifact: dict[str, Any]) -> str:
    snapshot = artifact.get("order_book_snapshot") if isinstance(artifact.get("order_book_snapshot"), dict) else {}
    if not isinstance(snapshot, dict) or not snapshot:
        snapshot = artifact.get("pre_logic_order_book_snapshot") if isinstance(artifact.get("pre_logic_order_book_snapshot"), dict) else {}
    source = str(snapshot.get("source") or "").lower()
    data = snapshot.get("data")
    has_recorded_book_asks = _has_executable_ask_fields(data)
    has_execution_asks = _has_usable_execution_prices(artifact)
    if source in {"book", "recorded_book"} and has_recorded_book_asks:
        return ORDER_BOOK_RECORDED
    if source in {"fallback", "signal_price_fallback"} and has_execution_asks:
        return ORDER_BOOK_SIGNAL_PRICE_FALLBACK
    if source == "synthetic" and has_recorded_book_asks:
        return ORDER_BOOK_SYNTHETIC

    execution_source = str(artifact.get("execution_snapshot_source") or "").lower()
    if execution_source in {"book", "recorded_book"} and has_execution_asks:
        return ORDER_BOOK_RECORDED
    if execution_source in {"fallback", "signal_price_fallback"} and has_execution_asks:
        return ORDER_BOOK_SIGNAL_PRICE_FALLBACK
    if execution_source == "synthetic" and has_execution_asks:
        return ORDER_BOOK_SYNTHETIC
    return ORDER_BOOK_MISSING


def classify_execution_snapshot_mode(artifact: dict[str, Any]) -> str:
    snapshot = artifact.get("execution_snapshot") if isinstance(artifact.get("execution_snapshot"), dict) else {}
    source = str(snapshot.get("source") or artifact.get("execution_snapshot_source") or "").lower()
    has_prices = _has_executable_ask_fields(snapshot) or _has_executable_ask_fields(
        (artifact.get("order_book_snapshot") or {}).get("data") if isinstance(artifact.get("order_book_snapshot"), dict) else None
    )
    if source == "book" and has_prices:
        return ORDER_BOOK_RECORDED
    if source == "fallback" and has_prices:
        return ORDER_BOOK_SIGNAL_PRICE_FALLBACK
    if source == "synthetic" and has_prices:
        return ORDER_BOOK_SYNTHETIC
    return ORDER_BOOK_MISSING


def classify_replay_row_quality(
    artifact: dict[str, Any],
    row: dict[str, Any] | None = None,
    *,
    source_mode: str | None = None,
    order_book_mode: str | None = None,
    execution_snapshot_mode: str | None = None,
    warnings: Iterable[str] | None = None,
) -> ReplayRowQuality:
    """Classify whether a recorded row is strict replay-grade without fetching live data."""

    row = row or {}
    source_mode = source_mode or classify_source_mode(artifact, row)
    order_book_mode = order_book_mode or classify_order_book_mode(artifact)
    execution_snapshot_mode = execution_snapshot_mode or classify_execution_snapshot_mode(artifact)
    warning_values = _artifact_warning_values(artifact, row, warnings)
    weather_snapshot = _weather_source_snapshot(artifact)
    weather_like = _is_weather_replay_row(artifact, row)
    date_validation = _date_validation_for_quality(artifact, weather_snapshot)
    original_action = _normalize_action(artifact.get("final_action") or _stored_action(row))
    reasons: list[str] = []

    if source_mode == SOURCE_MISSING:
        reasons.append("missing_source")
    elif source_mode == SOURCE_LIVE_CURRENT_FORBIDDEN:
        reasons.append("live_source_forbidden")
    elif source_mode == SOURCE_SYNTHETIC:
        reasons.append("synthetic_source")
    elif source_mode == SOURCE_HISTORICAL_POST_FACTO:
        reasons.append("historical_post_facto")

    if _warnings_match(warning_values, ("live_current_source_forbidden", "live source", "forbidden")):
        _append_unique(reasons, "live_source_forbidden")
    if _warnings_match(warning_values, ("synthetic",)):
        _append_unique(reasons, "synthetic_source")
    if _warnings_match(warning_values, ("historical_post_facto", "post_facto")):
        _append_unique(reasons, "historical_post_facto")
    if _warnings_match(warning_values, ("missing_source", "source_mode_missing")):
        _append_unique(reasons, "missing_source")

    if weather_like and not weather_snapshot:
        _append_unique(reasons, "missing_weather_snapshot")

    if order_book_mode != ORDER_BOOK_RECORDED or execution_snapshot_mode not in {ORDER_BOOK_RECORDED, ORDER_BOOK_SIGNAL_PRICE_FALLBACK}:
        _append_unique(reasons, f"order_book_not_recorded:{order_book_mode}/{execution_snapshot_mode}")
    if order_book_mode == ORDER_BOOK_MISSING:
        _append_unique(reasons, "missing_order_book")
    elif order_book_mode == ORDER_BOOK_SYNTHETIC or execution_snapshot_mode == ORDER_BOOK_SYNTHETIC:
        _append_unique(reasons, "synthetic_source")

    if original_action in {"BUY_YES", "BUY_NO"}:
        feasibility = _execution_feasibility_block(artifact)
        if feasibility is None:
            _append_unique(reasons, "missing_execution_feasibility")
        elif not _execution_feasibility_is_strict(feasibility):
            status = str(feasibility.get("status") or "infeasible").lower()
            failed = feasibility.get("failed_checks")
            if isinstance(failed, list) and failed:
                status = f"{status}:{','.join(str(item) for item in failed)}"
            _append_unique(reasons, f"execution_feasibility_failed:{status}")

    if weather_like and weather_snapshot and not _date_validation_is_strict(date_validation):
        reason = str((date_validation or {}).get("reason") or "missing_date_validation")
        _append_unique(reasons, f"date_unverified:{reason}")
    if _warnings_match(warning_values, ("date_unverified", "missing_weather_date", "unit_mismatch", "date_validation")):
        _append_unique(reasons, "date_unverified")

    category = _quality_category_from_reasons(reasons)
    if category is None:
        category = QUALITY_REPLAY_GRADE_BACKFILLED if _has_backfill_provenance(artifact, row) else QUALITY_REPLAY_GRADE_ORIGINAL
        strict = True
    else:
        strict = False
    return ReplayRowQuality(
        category=category,
        reasons=reasons,
        is_replay_grade_strict=strict,
        include_in_strict=strict,
    )


def build_replay_series_grid(rows_or_result: Iterable[ReplayComparisonRow] | PredictionLabReplayResult) -> list[dict[str, Any]]:
    """Group replay rows by series/event_ticker for strict-vs-coverage analysis."""

    rows = (rows_or_result.all_rows or rows_or_result.rows) if isinstance(rows_or_result, PredictionLabReplayResult) else list(rows_or_result)
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        series = str(row.series or _artifact_metadata(row.original_artifact or {}).get("series") or "unknown")
        event_ticker = str(row.event_ticker or _artifact_metadata(row.original_artifact or {}).get("event_ticker") or "")
        key = (series, event_ticker)
        group = groups.setdefault(
            key,
            {
                "series": series,
                "event_ticker": event_ticker or None,
                "total_rows": 0,
                "strict_rows": 0,
                "excluded_rows": 0,
                "excluded_counts": {},
                "quality_counts": {},
                "warning_counts": {},
                "action_changed": 0,
                "reason_changed": 0,
                "strict_action_changed": 0,
                "strict_reason_changed": 0,
                "action_change_counts": {},
                "reason_change_counts": {},
            },
        )
        group["total_rows"] += 1
        if row.include_in_strict:
            group["strict_rows"] += 1
            if row.action_changed:
                group["strict_action_changed"] += 1
            if row.reason_changed:
                group["strict_reason_changed"] += 1
        else:
            group["excluded_rows"] += 1
            for reason in row.reasons or [row.category]:
                _increment(group["excluded_counts"], reason)
        _increment(group["quality_counts"], row.category)
        for warning in row.warnings:
            _increment(group["warning_counts"], warning)
        if row.action_changed:
            group["action_changed"] += 1
            _increment(group["action_change_counts"], f"{row.original_action}->{row.replayed_action}")
        if row.reason_changed:
            group["reason_changed"] += 1
            _increment(group["reason_change_counts"], f"{row.original_reason_code}->{row.replayed_reason_code}")
    return [groups[key] for key in sorted(groups)]


def validate_prediction_lab_tables(
    input_paths: Iterable[str | Path],
    *,
    resolution_paths: Iterable[str | Path] | None = None,
) -> PredictionLabValidationResult:
    """Validate Prediction Lab replay-input rows and optional resolution ledgers."""

    issues: list[PredictionLabValidationIssue] = []
    checked_paths: list[str] = []
    total_rows = 0
    seen_inputs: dict[tuple[str, ...], tuple[str, int]] = {}
    seen_resolutions: dict[tuple[str, ...], tuple[str, int]] = {}
    resolution_path_set = {_validation_path_key(path_value) for path_value in resolution_paths or []}

    for path_value in input_paths:
        path = Path(path_value)
        if _validation_path_key(path) in resolution_path_set:
            continue
        checked_paths.append(str(path))
        for line_number, row in _iter_jsonl_dict_rows(path):
            total_rows += 1
            _validate_replay_input_row(row, path=str(path), line_number=line_number, seen=seen_inputs, issues=issues)

    for path_value in resolution_paths or []:
        path = Path(path_value)
        checked_paths.append(str(path))
        for line_number, row in _iter_jsonl_dict_rows(path):
            total_rows += 1
            _validate_resolution_row(row, path=str(path), line_number=line_number, seen=seen_resolutions, issues=issues)

    return PredictionLabValidationResult(total_rows=total_rows, checked_paths=checked_paths, issues=issues)


def _validation_path_key(path_value: str | Path) -> str:
    return str(Path(path_value).expanduser().resolve(strict=False))


def _coerce_record(value: ReplayArtifactInput | dict[str, Any]) -> ReplayArtifactInput:
    if isinstance(value, ReplayArtifactInput):
        row = _strip_inline_outcomes(value.row)
        return ReplayArtifactInput(row=row, artifact=_strip_artifact_outcomes(value.artifact), source_path=value.source_path, line_number=value.line_number)
    if _is_shadow_delta_compact_review_row(value):
        raise ValueError("compact shadow-delta review rows are not replay inputs")
    row = _strip_inline_outcomes(value)
    artifact = row.get("decision_artifact")
    if not isinstance(artifact, dict):
        artifact = _legacy_artifact_from_row(row)
    return ReplayArtifactInput(row=row, artifact=_strip_artifact_outcomes(artifact))


def _is_shadow_delta_compact_review_row(row: dict[str, Any]) -> bool:
    return row.get("row_type") == SHADOW_DELTA_COMPACT_REVIEW_ROW_TYPE


def _validate_replay_input_row(
    row: dict[str, Any],
    *,
    path: str,
    line_number: int,
    seen: dict[tuple[str, ...], tuple[str, int]],
    issues: list[PredictionLabValidationIssue],
) -> None:
    market_id = str(row.get("market_id") or "")
    if _is_shadow_delta_compact_review_row(row):
        _add_validation_issue(
            issues,
            "error",
            "shadow_delta_review_not_replay_input",
            "compact shadow-delta review rows are not valid replay input",
            path,
            line_number,
            market_id,
        )
        return
    for key in ("market_id", "decision_type"):
        if row.get(key) in (None, ""):
            _add_validation_issue(issues, "error", "schema_missing_field", f"missing required field {key}", path, line_number, market_id)
    if row.get("timestamp") in (None, "") and row.get("observed_at") in (None, ""):
        _add_validation_issue(issues, "error", "schema_missing_timestamp", "missing timestamp/observed_at", path, line_number, market_id)
    for key in ("timestamp", "observed_at"):
        if row.get(key) not in (None, "") and _parse_dt(row.get(key)) is None:
            _add_validation_issue(issues, "error", "timestamp_invalid", f"invalid {key}", path, line_number, market_id)

    identity = _validation_identity(row)
    if identity in seen:
        first_path, first_line = seen[identity]
        _add_validation_issue(
            issues,
            "error",
            "duplicate_identity",
            f"duplicate identity first seen at {first_path}:{first_line}",
            path,
            line_number,
            market_id,
        )
    else:
        seen[identity] = (path, line_number)

    if _has_outcome_leakage(row):
        _add_validation_issue(issues, "error", "outcome_leakage", "replay input row contains resolved outcome data", path, line_number, market_id)

    artifact = row.get("decision_artifact")
    if not isinstance(artifact, dict):
        _add_validation_issue(issues, "warning", "schema_missing_decision_artifact", "missing decision_artifact; replay will use legacy fallback", path, line_number, market_id)
        artifact = _legacy_artifact_from_row(row)

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
    if source_mode != SOURCE_RECORDED_AS_OF:
        _add_validation_issue(issues, "warning", "source_snapshot_incomplete", f"source_mode={source_mode}", path, line_number, market_id)
    if _is_weather_replay_row(artifact, row) and not _weather_source_snapshot(artifact):
        _add_validation_issue(issues, "warning", "weather_snapshot_incomplete", "weather row is missing weather_source_snapshot", path, line_number, market_id)
    if order_book_mode != ORDER_BOOK_RECORDED or execution_mode not in {ORDER_BOOK_RECORDED, ORDER_BOOK_SIGNAL_PRICE_FALLBACK}:
        _add_validation_issue(
            issues,
            "warning",
            "order_book_execution_not_strict",
            f"order_book_mode={order_book_mode} execution_snapshot_mode={execution_mode}",
            path,
            line_number,
            market_id,
        )
    original_action = _normalize_action(artifact.get("final_action") or _stored_action(row))
    if original_action in {"BUY_YES", "BUY_NO"} and not _execution_feasibility_is_strict(_execution_feasibility_block(artifact)):
        _add_validation_issue(
            issues,
            "warning",
            "execution_feasibility_not_strict",
            "buy row is missing passing execution_feasibility evidence",
            path,
            line_number,
            market_id,
        )
    if not quality.include_in_strict:
        _add_validation_issue(issues, "warning", f"row_quality_{quality.category}", ", ".join(quality.reasons), path, line_number, market_id)


def _validate_resolution_row(
    row: dict[str, Any],
    *,
    path: str,
    line_number: int,
    seen: dict[tuple[str, ...], tuple[str, int]],
    issues: list[PredictionLabValidationIssue],
) -> None:
    market_id = str(row.get("market_id") or "")
    if not market_id:
        _add_validation_issue(issues, "error", "resolution_schema_missing_market_id", "resolution row missing market_id", path, line_number, market_id)
    resolution = row.get("resolution") if isinstance(row.get("resolution"), dict) else {}
    outcome = str(resolution.get("outcome") or row.get("outcome") or "").upper()
    if outcome not in {"YES", "NO", "VOID"}:
        _add_validation_issue(issues, "error", "resolution_schema_missing_outcome", "resolution row missing YES/NO/VOID outcome", path, line_number, market_id)
    resolved_at = resolution.get("resolved_at") or row.get("resolved_at")
    if _parse_dt(resolved_at) is None:
        _add_validation_issue(issues, "error", "resolution_timestamp_invalid", "resolution row missing/invalid resolved_at", path, line_number, market_id)
    identity = _validation_identity(row)
    if identity in seen:
        first_path, first_line = seen[identity]
        _add_validation_issue(
            issues,
            "error",
            "duplicate_resolution_identity",
            f"duplicate resolution identity first seen at {first_path}:{first_line}",
            path,
            line_number,
            market_id,
        )
    else:
        seen[identity] = (path, line_number)


def _add_validation_issue(
    issues: list[PredictionLabValidationIssue],
    severity: str,
    code: str,
    message: str,
    source_path: str,
    line_number: int,
    market_id: str | None,
) -> None:
    issues.append(
        PredictionLabValidationIssue(
            severity=severity,
            code=code,
            message=message,
            source_path=source_path,
            line_number=line_number,
            market_id=market_id or None,
        )
    )


def _validation_identity(row: dict[str, Any]) -> tuple[str, ...]:
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


def _has_outcome_leakage(value: Any) -> bool:
    leakage_keys = {"resolution", "outcome", "actual_outcome", "settled_outcome", "market_result"}
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in leakage_keys and _outcome_leakage_value_present(item):
                return True
            if _has_outcome_leakage(item):
                return True
    elif isinstance(value, list):
        return any(_has_outcome_leakage(item) for item in value)
    return False


def _outcome_leakage_value_present(value: Any) -> bool:
    if value in (None, "", [], {}):
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_outcome_leakage_value_present(item) for item in value.values())
    if isinstance(value, list):
        return any(_outcome_leakage_value_present(item) for item in value)
    return True


def _strip_inline_outcomes(row: dict[str, Any]) -> dict[str, Any]:
    replay_row = dict(row)
    for key in ("resolution", "outcome", "actual_outcome", "settled_outcome", "market_result", "result"):
        replay_row.pop(key, None)
    return replay_row


def _strip_artifact_outcomes(value: Any) -> dict[str, Any]:
    stripped = _strip_artifact_outcomes_inner(value)
    return stripped if isinstance(stripped, dict) else {}


def _strip_artifact_outcomes_inner(value: Any) -> Any:
    leakage_keys = {"resolution", "outcome", "actual_outcome", "settled_outcome", "market_result"}
    if isinstance(value, dict):
        return {
            key: _strip_artifact_outcomes_inner(item)
            for key, item in value.items()
            if str(key).lower() not in leakage_keys
        }
    if isinstance(value, list):
        return [_strip_artifact_outcomes_inner(item) for item in value]
    return value


def _legacy_artifact_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "market_id": row.get("market_id"),
        "final_action": _stored_action(row),
        "final_reason_code": _shared_pipeline_reason(row),
        "source_context": {"source": "missing", "mode": "legacy", "data": {}},
        "order_book_snapshot": {"source": "missing", "data": None},
        "execution_snapshot_source": "missing",
    }


def _market_from_record(record: ReplayArtifactInput) -> Any:
    row = record.row
    artifact = record.artifact
    signal = artifact.get("strategy_signal") if isinstance(artifact.get("strategy_signal"), dict) else {}
    source_context = artifact.get("source_context") if isinstance(artifact.get("source_context"), dict) else {}
    source_data = source_context.get("data") if isinstance(source_context.get("data"), dict) else {}
    metadata = dict(source_data.get("market_metadata") or {}) if isinstance(source_data.get("market_metadata"), dict) else {}
    shared_candidate = shared_candidate_from_market_snapshot_row(row) if isinstance(row, dict) else {}
    shared_market = shared_candidate.get("market") if isinstance(shared_candidate.get("market"), dict) else {}
    shared_prices = shared_candidate.get("prices") if isinstance(shared_candidate.get("prices"), dict) else {}
    for key, value in {
        "market_group": row.get("group") or shared_market.get("group"),
        "series": row.get("series") or shared_market.get("series"),
        "event_ticker": row.get("event_ticker") or shared_market.get("event_ticker"),
        "series_ticker": shared_market.get("series_ticker"),
        "market_route": shared_market.get("route"),
        "market_family": shared_market.get("family"),
    }.items():
        if value is not None:
            metadata.setdefault(key, value)

    return SimpleNamespace(
        id=str(artifact.get("market_id") or row.get("market_id") or shared_candidate.get("market_id") or shared_market.get("id") or signal.get("market_id") or ""),
        exchange=str(signal.get("exchange") or row.get("exchange") or shared_market.get("exchange") or "kalshi"),
        question=str(signal.get("question") or row.get("question") or shared_market.get("question") or ""),
        yes_price=_first_number(signal.get("yes_market_price"), signal.get("yes_price"), row.get("yes_market_price"), row.get("yes_price"), row.get("market_price"), shared_prices.get("yes_price"), shared_prices.get("yes_market_price"), default=0.0),
        no_price=_first_number(signal.get("no_market_price"), signal.get("no_price"), row.get("no_market_price"), row.get("no_price"), shared_prices.get("no_price"), shared_prices.get("no_market_price"), default=None),
        volume=_first_number(row.get("volume"), signal.get("market_volume"), shared_market.get("volume"), default=0.0),
        category=str(metadata.get("series") or shared_market.get("category") or row.get("series") or row.get("group") or "unknown"),
        metadata=metadata,
        closes_at=shared_market.get("closes_at"),
    )


def _record_series(record: ReplayArtifactInput, market: Any) -> str | None:
    metadata = getattr(market, "metadata", {}) if isinstance(getattr(market, "metadata", {}), dict) else {}
    value = metadata.get("series") or record.row.get("series") or metadata.get("market_group") or record.row.get("group")
    return str(value) if value not in (None, "") else None


def _record_event_ticker(record: ReplayArtifactInput, market: Any) -> str | None:
    metadata = getattr(market, "metadata", {}) if isinstance(getattr(market, "metadata", {}), dict) else {}
    value = metadata.get("event_ticker") or record.row.get("event_ticker")
    return str(value) if value not in (None, "") else None


def _record_prediction_id(record: ReplayArtifactInput) -> str | None:
    value = record.row.get("prediction_id")
    return str(value) if value not in (None, "") else None


def _record_run_id(record: ReplayArtifactInput) -> str | None:
    value = record.row.get("run_id")
    return str(value) if value not in (None, "") else None


def _record_experiment_id(record: ReplayArtifactInput) -> str | None:
    value = record.row.get("experiment_id")
    return str(value) if value not in (None, "") else None


def _record_strategy_version(record: ReplayArtifactInput) -> str | None:
    value = record.row.get("strategy_version")
    return str(value) if value not in (None, "") else None


def _recorded_order_book(artifact: dict[str, Any]) -> dict[str, Any] | None:
    pre_logic_snapshot = artifact.get("pre_logic_order_book_snapshot") if isinstance(artifact.get("pre_logic_order_book_snapshot"), dict) else {}
    pre_logic_data = pre_logic_snapshot.get("data")
    if _has_executable_ask_fields(pre_logic_data):
        return dict(pre_logic_data)
    snapshot = artifact.get("order_book_snapshot") if isinstance(artifact.get("order_book_snapshot"), dict) else {}
    data = snapshot.get("data")
    if _has_executable_ask_fields(data):
        return dict(data)
    order_book = artifact.get("order_book")
    if _has_executable_ask_fields(order_book):
        return dict(order_book)
    execution_snapshot = artifact.get("execution_snapshot")
    if _has_executable_ask_fields(execution_snapshot):
        return {
            key: execution_snapshot.get(key)
            for key in ("best_yes_ask", "best_no_ask", "best_yes_bid", "best_no_bid")
            if execution_snapshot.get(key) is not None
        }
    return None


def _recorded_source_context(artifact: dict[str, Any]) -> dict[str, Any]:
    source_context = artifact.get("source_context") if isinstance(artifact.get("source_context"), dict) else {}
    data = source_context.get("data")
    context = dict(data) if isinstance(data, dict) else {}
    snapshots = artifact.get("source_snapshots")
    if isinstance(snapshots, list):
        context["source_snapshots"] = [dict(snapshot) for snapshot in snapshots if isinstance(snapshot, dict)]
    return context


def _artifact_metadata(artifact: dict[str, Any]) -> dict[str, Any]:
    source_context = artifact.get("source_context") if isinstance(artifact.get("source_context"), dict) else {}
    data = source_context.get("data") if isinstance(source_context.get("data"), dict) else {}
    metadata = data.get("market_metadata") if isinstance(data.get("market_metadata"), dict) else {}
    return dict(metadata)


def _weather_source_snapshot(artifact: dict[str, Any]) -> dict[str, Any] | None:
    source_context = artifact.get("source_context") if isinstance(artifact.get("source_context"), dict) else {}
    data = source_context.get("data") if isinstance(source_context.get("data"), dict) else {}
    snapshot = data.get("weather_source_snapshot")
    if isinstance(snapshot, dict) and snapshot:
        return snapshot
    snapshots = artifact.get("source_snapshots")
    if isinstance(snapshots, list):
        for source_snapshot in snapshots:
            if not isinstance(source_snapshot, dict):
                continue
            resolved = _resolve_snapshot_ref(artifact, source_snapshot)
            candidate = resolved if isinstance(resolved, dict) else source_snapshot
            source_name = str(
                candidate.get("source_name")
                or candidate.get("source")
                or candidate.get("signal_type")
                or source_snapshot.get("source_name")
                or source_snapshot.get("source")
                or ""
            ).lower()
            is_rich_weather_snapshot = any(
                key in candidate
                for key in (
                    "forecast",
                    "date_validation",
                    "sources",
                    "source_signal",
                    "market_date",
                    "target_forecast_date",
                    "station_id",
                )
            )
            if source_name == "weather" and candidate and (resolved is not None or is_rich_weather_snapshot):
                return candidate
    return None


def _is_weather_replay_row(artifact: dict[str, Any], row: dict[str, Any]) -> bool:
    metadata = _artifact_metadata(artifact)
    values = [
        row.get("group"),
        row.get("series"),
        row.get("event_ticker"),
        row.get("market_id"),
        row.get("question"),
        metadata.get("market_group"),
        metadata.get("series"),
        metadata.get("event_ticker"),
        artifact.get("market_id"),
    ]
    joined = " ".join(str(value or "").lower() for value in values)
    return any(token in joined for token in ("weather", "temperature", "daily_temperature", "kxhigh", "kxlow", "weather_source_snapshot"))


def _date_validation_for_quality(artifact: dict[str, Any], weather_snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    candidates: list[Any] = []
    if isinstance(weather_snapshot, dict):
        candidates.append(weather_snapshot.get("date_validation"))
    source_context = artifact.get("source_context") if isinstance(artifact.get("source_context"), dict) else {}
    data = source_context.get("data") if isinstance(source_context.get("data"), dict) else {}
    candidates.append(data.get("date_validation"))
    candidates.append(artifact.get("date_validation"))
    snapshots = artifact.get("source_snapshots")
    if isinstance(snapshots, list):
        for snapshot in snapshots:
            if not isinstance(snapshot, dict):
                continue
            resolved = _resolve_snapshot_ref(artifact, snapshot)
            if isinstance(resolved, dict):
                candidates.append(resolved.get("date_validation"))
            candidates.append(snapshot.get("date_validation"))
    for candidate in candidates:
        if isinstance(candidate, dict):
            return candidate
    return None


def _date_validation_is_strict(date_validation: dict[str, Any] | None) -> bool:
    if not isinstance(date_validation, dict):
        return False
    if date_validation.get("ok") is not True:
        return False
    market_date = _normalize_iso_date(date_validation.get("market_date"))
    weather_date = _normalize_iso_date(date_validation.get("weather_date"))
    return bool(market_date and weather_date and market_date == weather_date)


def _normalize_iso_date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        value = isoformat()
    text = str(value).strip()
    if not text:
        return None
    if len(text) >= 10:
        candidate = text[:10]
        try:
            datetime.fromisoformat(candidate)
            return candidate
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def _has_usable_execution_prices(artifact: dict[str, Any]) -> bool:
    snapshot = artifact.get("execution_snapshot")
    return _has_executable_ask_fields(snapshot)


def _execution_feasibility_block(artifact: dict[str, Any]) -> dict[str, Any] | None:
    value = artifact.get("execution_feasibility")
    return value if isinstance(value, dict) and value else None


def _execution_feasibility_is_strict(value: dict[str, Any] | None) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("feasible") is not True:
        return False
    checks = (
        "same_market_open",
        "same_side_ask_present",
        "ask_within_slippage",
        "elapsed_within_threshold",
    )
    if not all(value.get(check) is True for check in checks):
        return False
    sufficient_quantity = value.get("sufficient_quantity")
    return sufficient_quantity is not False


def _has_executable_ask_fields(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    for field_name in ("best_yes_ask", "best_no_ask"):
        if _usable_price(value.get(field_name)):
            return True
    return False


def _usable_price(value: Any) -> bool:
    if value in (None, ""):
        return False
    try:
        price = float(value)
    except (TypeError, ValueError):
        return False
    return 0.0 < price < 1.0


def _artifact_warning_values(artifact: dict[str, Any], row: dict[str, Any], warnings: Iterable[str] | None) -> list[str]:
    values: list[str] = []
    for source in (warnings, artifact.get("warnings"), row.get("warnings")):
        if isinstance(source, str):
            values.append(source)
        elif isinstance(source, (list, tuple, set)):
            values.extend(str(value) for value in source if value not in (None, ""))
    return values


def _warnings_match(warnings: Iterable[str], tokens: Iterable[str]) -> bool:
    lowered = [str(warning or "").lower() for warning in warnings]
    return any(token in warning for warning in lowered for token in tokens)


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _quality_category_from_reasons(reasons: list[str]) -> str | None:
    joined = " ".join(reasons)
    if "live_source_forbidden" in reasons:
        return QUALITY_LIVE_SOURCE_FORBIDDEN
    if "synthetic_source" in reasons:
        return QUALITY_SYNTHETIC_SOURCE
    if "historical_post_facto" in reasons:
        return QUALITY_HISTORICAL_POST_FACTO
    if "missing_weather_snapshot" in reasons:
        return QUALITY_MISSING_WEATHER_SNAPSHOT
    if "missing_source" in reasons:
        return QUALITY_MISSING_SOURCE
    if any(reason.startswith("date_unverified") for reason in reasons):
        return QUALITY_DATE_UNVERIFIED
    if "missing_order_book" in reasons or "order_book_not_recorded" in joined:
        return QUALITY_MISSING_ORDER_BOOK
    if "missing_execution_feasibility" in reasons or any(reason.startswith("execution_feasibility_failed") for reason in reasons):
        return QUALITY_COVERAGE_ONLY
    return None


def _has_backfill_provenance(artifact: dict[str, Any], row: dict[str, Any]) -> bool:
    tokens = ("historical_replay", "backfilled", "backfill")
    values: list[Any] = [
        artifact.get("provenance"),
        artifact.get("source_provenance"),
        artifact.get("collection_source"),
        artifact.get("replay_source"),
        artifact.get("mode"),
        row.get("provenance"),
        row.get("source_provenance"),
        row.get("collection_source"),
    ]
    shared_pipeline = row.get("shared_pipeline") if isinstance(row.get("shared_pipeline"), dict) else {}
    values.extend((shared_pipeline.get("provenance"), shared_pipeline.get("source_provenance")))
    source_context = artifact.get("source_context") if isinstance(artifact.get("source_context"), dict) else {}
    values.extend((source_context.get("provenance"), source_context.get("source_provenance"), source_context.get("source_mode")))
    data = source_context.get("data") if isinstance(source_context.get("data"), dict) else {}
    weather_snapshot = data.get("weather_source_snapshot") if isinstance(data.get("weather_source_snapshot"), dict) else {}
    values.extend(
        (
            weather_snapshot.get("provenance"),
            weather_snapshot.get("source_provenance"),
            weather_snapshot.get("mode"),
            weather_snapshot.get("source_quality"),
        )
    )
    snapshots = artifact.get("source_snapshots")
    if isinstance(snapshots, list):
        for snapshot in snapshots:
            if isinstance(snapshot, dict):
                resolved = _resolve_snapshot_ref(artifact, snapshot)
                values.extend((snapshot.get("provenance"), snapshot.get("source_provenance"), snapshot.get("mode"), snapshot.get("source_quality")))
                if isinstance(resolved, dict) and _nested_backfill_signal(resolved):
                    return True
    if any(any(token in str(value or "").lower() for token in tokens) for value in values):
        return True
    return _nested_backfill_signal(weather_snapshot)


def _snapshot_has_historical_post_facto_provenance(artifact: dict[str, Any], snapshot: dict[str, Any]) -> bool:
    if _has_historical_post_facto_provenance(snapshot):
        return True
    resolved = _resolve_snapshot_ref(artifact, snapshot)
    return isinstance(resolved, dict) and _has_historical_post_facto_provenance(resolved)


def _has_historical_post_facto_provenance(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key or "").lower()
            item_text = str(item or "").lower()
            if key_text == "historical_replay" and bool(item):
                return True
            if key_text in {
                "mode",
                "source_mode",
                "source",
                "source_provenance",
                "provenance",
                "collection_source",
                "replay_source",
            } and any(token in item_text for token in ("historical_post_facto", "post_facto", "historical_replay")):
                return True
            if _has_historical_post_facto_provenance(item):
                return True
    elif isinstance(value, (list, tuple, set)):
        return any(_has_historical_post_facto_provenance(item) for item in value)
    return False


def _nested_backfill_signal(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key or "").lower()
            item_text = str(item or "").lower()
            if key_text == "historical_replay" and bool(item):
                return True
            if key_text == "source_quality" and ("settlement" in item_text or "historical" in item_text):
                return True
            if any(token in item_text for token in ("historical_replay", "backfilled", "backfill")):
                return True
            if _nested_backfill_signal(item):
                return True
    elif isinstance(value, (list, tuple, set)):
        return any(_nested_backfill_signal(item) for item in value)
    return False


def _recorded_execution_snapshot(artifact: dict[str, Any]) -> dict[str, Any] | None:
    snapshot = artifact.get("execution_snapshot")
    if not isinstance(snapshot, dict) or not snapshot:
        return None
    recorded = dict(snapshot)
    if not recorded.get("source") and artifact.get("execution_snapshot_source"):
        recorded["source"] = artifact.get("execution_snapshot_source")
    return recorded


def _record_as_of(artifact: dict[str, Any], row: dict[str, Any]) -> datetime | None:
    values = [artifact.get("as_of")]
    source_context = artifact.get("source_context") if isinstance(artifact.get("source_context"), dict) else {}
    values.append(source_context.get("as_of"))
    source_data = source_context.get("data") if isinstance(source_context.get("data"), dict) else {}
    weather_snapshot = source_data.get("weather_source_snapshot") if isinstance(source_data.get("weather_source_snapshot"), dict) else {}
    values.append(weather_snapshot.get("as_of"))
    snapshots = artifact.get("source_snapshots")
    if isinstance(snapshots, list):
        values.extend(snapshot.get("as_of") for snapshot in snapshots if isinstance(snapshot, dict))
    values.extend((artifact.get("observed_at"), row.get("observed_at"), row.get("timestamp")))
    for value in values:
        parsed = _parse_dt(value)
        if parsed is not None:
            return parsed
    return None


@contextmanager
def _patch_recorded_source_methods(
    evaluator: DecisionPipelineEvaluator,
    source_signals: dict[str, dict[str, Any]],
):
    strategy = getattr(evaluator, "strategy", None)
    if strategy is None or not source_signals:
        yield set()
        return

    originals: dict[str, Any] = {}
    for method_name, signal in source_signals.items():
        original = getattr(strategy, method_name, None)
        if not callable(original):
            continue
        originals[method_name] = original

        def recorded_source_signal(*args, _signal=signal, **kwargs):
            return dict(_signal) if isinstance(_signal, dict) else None

        setattr(strategy, method_name, recorded_source_signal)

    try:
        yield set(originals)
    finally:
        for method_name, original in originals.items():
            setattr(strategy, method_name, original)


@contextmanager
def _guard_live_current_source(
    evaluator: DecisionPipelineEvaluator,
    *,
    policy: str,
    warnings: list[str],
    market_id: str,
    skip_methods: set[str] | None = None,
):
    if policy == "allow":
        yield
        return

    strategy = getattr(evaluator, "strategy", None)
    skip_methods = skip_methods or set()
    originals: dict[str, Any] = {}
    if strategy is None:
        yield
        return

    for method_name in LIVE_CURRENT_SOURCE_METHODS:
        if method_name in skip_methods:
            continue
        original = getattr(strategy, method_name, None)
        if callable(original):
            originals[method_name] = original

    if not originals:
        yield
        return

    def guarded_live_current_source(*args, **kwargs):
        call_market_id = ""
        if args:
            call_market_id = str(getattr(args[0], "id", "") or "")
        message = f"live_current_source_forbidden_for_historical_replay:{market_id or call_market_id}"
        if policy == "fail":
            raise LiveCurrentSourceForbiddenError(message)
        warnings.append(message)
        logger.warning(message)
        return None

    for method_name in originals:
        setattr(strategy, method_name, guarded_live_current_source)
    try:
        yield
    finally:
        for method_name, original in originals.items():
            setattr(strategy, method_name, original)


def _recorded_source_signals(artifact: dict[str, Any]) -> dict[str, dict[str, Any]]:
    signals: dict[str, dict[str, Any]] = {}
    ref_backed_methods: set[str] = set()
    snapshots = artifact.get("source_snapshots")
    if isinstance(snapshots, list):
        for snapshot in snapshots:
            if not isinstance(snapshot, dict):
                continue
            method_name = _source_method_for_snapshot(snapshot)
            snapshot_payload = _resolve_snapshot_ref(artifact, snapshot) or snapshot
            if isinstance(snapshot_payload, dict):
                method_name = method_name or _source_method_for_snapshot(snapshot_payload)
            signal = _signal_from_replay_snapshot_payload(snapshot_payload, snapshot)
            if method_name and signal:
                signals[method_name] = signal
                if snapshot_payload is not snapshot:
                    ref_backed_methods.add(method_name)

    source_context = artifact.get("source_context") if isinstance(artifact.get("source_context"), dict) else {}
    data = source_context.get("data")
    if isinstance(data, dict):
        for source_name, value in data.items():
            if source_name in SOURCE_CONTEXT_METADATA_KEYS:
                continue
            if not isinstance(value, dict):
                continue
            if str(source_name).lower() == "weather_source_snapshot":
                method_name = SOURCE_SIGNAL_METHODS["weather"]
                signal = _signal_from_weather_source_snapshot(value)
            else:
                method_name = SOURCE_SIGNAL_METHODS.get(str(source_name).lower())
                signal = _normalize_recorded_source_signal(value, source_name=str(source_name))
            if method_name and signal:
                if method_name not in signals or method_name in ref_backed_methods:
                    signals[method_name] = signal

    return signals


def _resolve_snapshot_ref(artifact: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any] | None:
    ref = str(snapshot.get("snapshot_ref") or "").strip()
    if not ref:
        return None
    if ref == "source_context.data.weather_source_snapshot":
        source_context = artifact.get("source_context") if isinstance(artifact.get("source_context"), dict) else {}
        data = source_context.get("data") if isinstance(source_context.get("data"), dict) else {}
        value = data.get("weather_source_snapshot")
        return value if isinstance(value, dict) else None
    current: Any = artifact
    for part in ref.split("."):
        if isinstance(current, dict):
            current = current.get(part)
            continue
        return None
    return current if isinstance(current, dict) else None


def _signal_from_replay_snapshot_payload(payload: Any, snapshot: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    source_name = str(
        payload.get("source_name")
        or payload.get("source")
        or snapshot.get("source_name")
        or snapshot.get("source")
        or snapshot.get("name")
        or ""
    ).lower()
    if any(key in payload for key in ("signal", "raw_signal", "accepted_signal", "data")):
        signal = _signal_from_source_snapshot(payload)
        if signal:
            return signal
    if source_name == "weather" or payload.get("forecast") or str(payload.get("signal_type") or "").lower() == "weather":
        signal = _signal_from_weather_source_snapshot(payload)
        if signal:
            return signal
    return _signal_from_source_snapshot(payload)


def _source_method_for_snapshot(snapshot: dict[str, Any]) -> str | None:
    method = str(snapshot.get("method") or "").strip()
    if method in LIVE_CURRENT_SOURCE_METHODS:
        return method
    source_name = str(
        snapshot.get("name")
        or snapshot.get("source_name")
        or snapshot.get("source")
        or snapshot.get("signal_name")
        or snapshot.get("signal_type")
        or ""
    ).lower()
    data = snapshot.get("data")
    if not source_name and isinstance(data, dict):
        source_name = str(data.get("signal_type") or data.get("source") or "").lower()
    signal = snapshot.get("signal")
    if not source_name and isinstance(signal, dict):
        source_name = str(signal.get("signal_type") or signal.get("source") or "").lower()
    return SOURCE_SIGNAL_METHODS.get(source_name)


def _signal_from_source_snapshot(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("signal", "raw_signal", "accepted_signal", "data"):
        value = snapshot.get(key)
        signal = _normalize_recorded_source_signal(value, source_name=str(snapshot.get("source") or snapshot.get("name") or ""))
        if signal:
            return signal
    return _normalize_recorded_source_signal(snapshot, source_name=str(snapshot.get("source") or snapshot.get("name") or ""))


def _normalize_recorded_source_signal(value: Any, *, source_name: str = "") -> dict[str, Any] | None:
    if not isinstance(value, dict) or not _dict_has_signal_fields(value):
        return None
    signal = dict(value)
    if "predicted_prob" not in signal and signal.get("model_probability") is not None:
        signal["predicted_prob"] = signal.get("model_probability")
    if "confidence" not in signal:
        signal["confidence"] = 0.5
    if "signal_type" not in signal and source_name:
        signal["signal_type"] = str(source_name).lower()
    return signal


def _signal_from_weather_source_snapshot(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    source_signal = snapshot.get("source_signal")
    normalized = _normalize_recorded_source_signal(source_signal, source_name="weather")
    if normalized:
        return normalized
    forecast = snapshot.get("forecast") if isinstance(snapshot.get("forecast"), dict) else {}
    sources = snapshot.get("sources") if isinstance(snapshot.get("sources"), list) else []
    source_names = [
        source.get("source_name")
        for source in sources
        if isinstance(source, dict) and source.get("source_name") not in (None, "")
    ]
    source_details = [dict(source) for source in sources if isinstance(source, dict)]
    date_validation = snapshot.get("date_validation") if isinstance(snapshot.get("date_validation"), dict) else None
    weather_date = _weather_date_from_snapshot(snapshot, date_validation)
    source_timestamp = snapshot.get("source_timestamp") or snapshot.get("as_of") or snapshot.get("fetched_at")
    data = {
        "forecast_high": forecast.get("forecast_high", forecast.get("high")),
        "forecast_low": forecast.get("forecast_low", forecast.get("low")),
        "current_temp": forecast.get("current_temp", forecast.get("current")),
        "actual_temp_used": forecast.get("actual_temp_used"),
        "predicted_temp": forecast.get("predicted_temp"),
        "threshold": forecast.get("threshold"),
        "sources": source_names,
        "source_details": source_details,
        "agreement": snapshot.get("source_agreement_score"),
        "settlement_source": snapshot.get("settlement_source"),
        "station_id": snapshot.get("station_id"),
        "station_cli": snapshot.get("station_cli"),
        "station_mapping": snapshot.get("station_mapping"),
        "station_resolution": snapshot.get("station_resolution"),
        "date_validation": date_validation,
        "weather_date": weather_date,
        "forecast_date": snapshot.get("forecast_date"),
        "target_forecast_date": snapshot.get("target_forecast_date"),
        "fetched_at": snapshot.get("source_fetched_at") or snapshot.get("fetched_at"),
        "as_of": snapshot.get("source_as_of") or snapshot.get("as_of"),
    }
    gaps = snapshot.get("gaps") if isinstance(snapshot.get("gaps"), dict) else {}
    if gaps.get("nws_open_meteo_gap") is not None:
        data["nws_open_meteo_gap"] = gaps.get("nws_open_meteo_gap")
    candidate = {
        "signal_type": "weather",
        "predicted_prob": snapshot.get("predicted_prob"),
        "confidence": snapshot.get("confidence"),
        "source_timestamp": source_timestamp,
        "ttl_seconds": snapshot.get("ttl_seconds"),
        "question_side": forecast.get("question_side"),
        "data": {key: value for key, value in data.items() if value not in (None, "", [])},
    }
    return _normalize_recorded_source_signal(candidate, source_name="weather")


def _weather_date_from_snapshot(snapshot: dict[str, Any], date_validation: dict[str, Any] | None) -> Any:
    if snapshot.get("weather_date") not in (None, ""):
        return snapshot.get("weather_date")
    if snapshot.get("forecast_date") not in (None, ""):
        return snapshot.get("forecast_date")
    if date_validation and date_validation.get("ok") and date_validation.get("weather_date") not in (None, ""):
        return date_validation.get("weather_date")
    return None


def _dict_has_signal_fields(value: dict[str, Any]) -> bool:
    return any(key in value for key in ("predicted_prob", "model_probability", "confidence", "signal_type"))


def _snapshot_has_source_evidence(snapshot: dict[str, Any]) -> bool:
    if _signal_from_source_snapshot(snapshot):
        return True
    for key in ("evidence", "items", "documents", "records", "raw"):
        value = snapshot.get(key)
        if isinstance(value, (list, tuple, set)) and len(value) > 0:
            return True
        if isinstance(value, dict) and bool(value):
            return True
        if isinstance(value, str) and value.strip():
            return True
    return False


def _source_context_has_evidence(data: Any) -> bool:
    if not isinstance(data, dict) or not data:
        return False
    for key, value in data.items():
        if key in SOURCE_CONTEXT_METADATA_KEYS:
            continue
        if isinstance(value, dict):
            if _dict_has_signal_fields(value):
                return True
            if any(item not in SOURCE_CONTEXT_METADATA_KEYS for item in value):
                return True
        elif isinstance(value, list) and value:
            return True
        elif value not in (None, ""):
            return True
    return False


def _source_mode_warnings(source_mode: str, order_book_mode: str) -> list[str]:
    warnings: list[str] = []
    if source_mode == SOURCE_MISSING:
        warnings.append("source_mode_missing_for_historical_replay")
    elif source_mode == SOURCE_HISTORICAL_POST_FACTO:
        warnings.append("source_mode_historical_post_facto_not_as_of")
    elif source_mode == SOURCE_SYNTHETIC:
        warnings.append("source_mode_synthetic")
    elif source_mode == SOURCE_LIVE_CURRENT_FORBIDDEN:
        warnings.append("source_mode_live_current_forbidden")
    if order_book_mode == ORDER_BOOK_MISSING:
        warnings.append("order_book_mode_missing_for_historical_replay")
    elif order_book_mode == ORDER_BOOK_SIGNAL_PRICE_FALLBACK:
        warnings.append("order_book_mode_signal_price_fallback")
    elif order_book_mode == ORDER_BOOK_SYNTHETIC:
        warnings.append("order_book_mode_synthetic")
    return warnings


def _summarize(
    rows: list[ReplayComparisonRow],
    *,
    all_rows: list[ReplayComparisonRow] | None = None,
    shadow_delta_rows: Iterable[dict[str, Any]] | None = None,
    dual_policy_rows: Iterable[dict[str, Any]] | None = None,
    agent_decision_rows: list[dict[str, Any]] | None = None,
    source_reliability_shadow_rows: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    all_rows = all_rows or rows
    strict_rows = [row for row in all_rows if row.include_in_strict]
    excluded_rows = [row for row in all_rows if not row.include_in_strict]
    summary = {
        "total": len(rows),
        "input_total": len(all_rows),
        "action_changed": sum(1 for row in rows if row.action_changed),
        "reason_changed": sum(1 for row in rows if row.reason_changed),
        "missed_wins": sum(1 for row in rows if row.missed_win),
        "over_filtered_wins": sum(1 for row in rows if row.missed_win),
        "bad_buys_removed": sum(1 for row in rows if row.bad_buy_removed),
        "bad_buys_added": sum(1 for row in rows if row.bad_buy_added),
        "outcomes": _counts(row.outcome or "unknown" for row in rows),
        "source_modes": _counts(row.source_mode for row in rows),
        "order_book_modes": _counts(row.order_book_mode for row in rows),
        "quality_counts": _counts(row.category for row in all_rows),
        "strict_row_count": len(strict_rows),
        "excluded_row_count": len(excluded_rows),
        "excluded_reason_counts": _counts(reason for row in excluded_rows for reason in (row.reasons or [row.category])),
        "strict_metrics": _summary_metrics(strict_rows),
        "strategy_lanes": {
            "original": summarize_strategy_lanes(_strategy_lane_summary_rows(rows, artifact_attr="original_artifact")),
            "replayed": summarize_strategy_lanes(_strategy_lane_summary_rows(rows, artifact_attr="replayed_artifact")),
        },
        "weather_hidden_gem_comparison": _weather_hidden_gem_replay_comparison(all_rows),
        "dual_policy_replay_comparison": _summarize_dual_policy_replay_rows(dual_policy_rows or ()),
        "shadow_delta": summarize_shadow_delta_rows(shadow_delta_rows or ()),
        "source_reliability_shadow": summarize_source_reliability_shadow_rows(source_reliability_shadow_rows or ()),
        "warning_count": sum(len(row.warnings) for row in rows),
    }
    if agent_decision_rows is not None:
        replay_candidate_ids = [row.shared_candidate_id for row in all_rows if row.shared_candidate_id not in (None, "")]
        summary["agent_decision_coverage"] = summarize_agent_decision_coverage(
            agent_decision_rows,
            shared_candidate_ids=replay_candidate_ids,
        )
        summary["agent_decision_report"] = summarize_agent_decision_reporting(
            agent_decision_rows,
            replay_rows=all_rows,
            shared_candidate_ids=replay_candidate_ids,
        )
    return summary


def _strategy_lane_summary_rows(rows: list[ReplayComparisonRow], *, artifact_attr: str) -> list[dict[str, Any]]:
    summary_rows = []
    for row in rows:
        artifact = getattr(row, artifact_attr, None)
        if isinstance(artifact, dict):
            summary_rows.append({"decision_artifact": artifact})
    return summary_rows


def _weather_hidden_gem_replay_comparison(rows: list[ReplayComparisonRow]) -> dict[str, Any]:
    rows = [row for row in rows if _is_weather_hidden_gem_scope(row)]
    strict_rows = [row for row in rows if row.include_in_strict]
    return {
        "schema_version": 1,
        "basis": "artifact_derived_conservative",
        "basis_note": (
            "Uses recorded/replayed artifacts and hidden_gem_evidence_card fields when present; "
            "pre-hotfix and hotfix-bridge labels are conservative artifact-derived comparators, not exact "
            "historical logic reconstruction."
        ),
        "strict": _weather_hidden_gem_slice(strict_rows),
        "coverage": _weather_hidden_gem_slice(rows),
    }


def _is_weather_hidden_gem_scope(row: ReplayComparisonRow) -> bool:
    if _comparison_hidden_gem_card(row) is not None:
        return True
    if str(row.series or "") == "daily_temperature":
        return True
    for artifact in (row.replayed_artifact, row.original_artifact):
        if not isinstance(artifact, dict):
            continue
        source_context = artifact.get("source_context") if isinstance(artifact.get("source_context"), dict) else {}
        data = source_context.get("data") if isinstance(source_context.get("data"), dict) else {}
        metadata = data.get("market_metadata") if isinstance(data.get("market_metadata"), dict) else {}
        route = artifact.get("market_route") if isinstance(artifact.get("market_route"), dict) else {}
        if metadata.get("market_group") == "weather" or route.get("group") == "weather":
            return True
    return False


def _weather_hidden_gem_slice(rows: list[ReplayComparisonRow]) -> dict[str, Any]:
    card_rows = [_row_hidden_gem_card_payload(row, _comparison_hidden_gem_card(row)) for row in rows]
    bucket_rows = [
        (row, card)
        for row in rows
        for card in [_comparison_hidden_gem_card(row)]
        if isinstance(card, dict) and _clean_label(card.get("weather_shape")) == "bucket"
    ]
    hotfix_bridge_rows = [
        (row, card)
        for row in rows
        for card in [_comparison_hidden_gem_card(row)]
        if isinstance(card, dict) and _has_hotfix_bridge_reason(card, row)
    ]
    evidence_rejected_rows = [
        (row, card)
        for row in rows
        for card in [_comparison_hidden_gem_card(row)]
        if isinstance(card, dict) and _card_rejection_reason(card, row) is not None
    ]
    evidence_approved_rows = [
        (row, card)
        for row in rows
        for card in [_comparison_hidden_gem_card(row)]
        if isinstance(card, dict) and _card_rejection_reason(card, row) is None
    ]
    no_card_rows = sum(1 for row in rows if _comparison_hidden_gem_card(row) is None)
    return {
        "rows": len(rows),
        "strict_rows": sum(1 for row in rows if row.include_in_strict),
        "coverage_only_rows": sum(1 for row in rows if not row.include_in_strict),
        "card_rows": len(rows) - no_card_rows,
        "no_card_rows": no_card_rows,
        "hidden_gem_evidence_cards": summarize_hidden_gem_evidence_cards(card_rows),
        "bucket_distribution": _bucket_distribution_summary(bucket_rows),
        "comparators": {
            "recorded_or_pre_hotfix_proxy": {
                "basis": "recorded_final_action",
                "buy_rows": sum(1 for row in rows if _normalize_action(row.original_action) in {"BUY_YES", "BUY_NO"}),
                "skip_rows": sum(1 for row in rows if _is_skip(row.original_action)),
            },
            "hotfix_bridge": {
                "basis": "artifact_reason_code",
                "reason_codes": sorted(WEATHER_HIDDEN_GEM_HOTFIX_BRIDGE_REASON_CODES),
                "inferred_rejections": len(hotfix_bridge_rows),
                "winners_skipped": sum(
                    1
                    for row, _card in hotfix_bridge_rows
                    if _normalize_action(row.original_action) in {"BUY_YES", "BUY_NO"}
                    and _is_correct_buy(row.original_action, row.outcome)
                ),
                "bad_bucket_buys_removed": sum(
                    1
                    for row, card in hotfix_bridge_rows
                    if _clean_label(card.get("weather_shape")) == "bucket"
                    and _is_incorrect_buy(row.original_action, row.outcome)
                ),
            },
            "evidence_card": {
                "basis": "hidden_gem_evidence_card",
                "approvals": len(evidence_approved_rows),
                "rejections": len(evidence_rejected_rows),
                "bad_bucket_buys_removed": sum(
                    1
                    for row, card in evidence_rejected_rows
                    if _clean_label(card.get("weather_shape")) == "bucket"
                    and _is_incorrect_buy(row.original_action, row.outcome)
                ),
            },
        },
        "outcomes": _counts(row.outcome or "unknown" for row in rows),
        "quality_counts": _counts(row.category for row in rows),
    }


def _row_hidden_gem_card_payload(row: ReplayComparisonRow, card: dict[str, Any] | None = None) -> dict[str, Any]:
    card_rejection = _card_rejection_reason(card, row) if isinstance(card, dict) else None
    evidence_action = "SKIP" if card_rejection else _card_approved_action(row)
    evidence_reason = card_rejection or "approved"
    payload: dict[str, Any] = {
        "market_id": row.market_id,
        "direction": evidence_action,
        "final_action": evidence_action,
        "final_reason_code": evidence_reason,
        "decision_reason_code": evidence_reason,
        "status": "rejected" if card_rejection else "approved",
    }
    if isinstance(card, dict):
        payload["hidden_gem_evidence_card"] = card
    return payload


def _card_approved_action(row: ReplayComparisonRow) -> str:
    for action in (row.original_action, row.replayed_action):
        normalized = _normalize_action(action)
        if normalized in {"BUY_YES", "BUY_NO"}:
            return normalized
    return "BUY_YES"


def _comparison_hidden_gem_card(row: ReplayComparisonRow) -> dict[str, Any] | None:
    for artifact in (row.replayed_artifact, row.original_artifact):
        if isinstance(artifact, dict):
            card = extract_hidden_gem_evidence_card({"decision_artifact": artifact})
            if isinstance(card, dict):
                return card
    return None


def _bucket_distribution_summary(bucket_rows: list[tuple[ReplayComparisonRow, dict[str, Any]]]) -> dict[str, Any]:
    with_distribution = 0
    without_distribution = 0
    insufficient_threshold_data = 0
    threshold_counts = {
        "distribution_probability_gte_entry_plus_0_05": {"pass": 0, "fail": 0},
        "distribution_probability_gte_3x_entry": {"pass": 0, "fail": 0},
        "combined_gate": {"pass": 0, "fail": 0},
    }
    for row, card in bucket_rows:
        distribution_probability = _card_distribution_probability(card)
        entry_price = _card_entry_price(card, row)
        if distribution_probability is None:
            without_distribution += 1
            insufficient_threshold_data += 1
            continue
        with_distribution += 1
        if entry_price is None:
            insufficient_threshold_data += 1
            continue
        entry_plus = distribution_probability + 1e-9 >= entry_price + 0.05
        three_x = distribution_probability + 1e-9 >= 3 * entry_price
        _threshold_increment(threshold_counts["distribution_probability_gte_entry_plus_0_05"], entry_plus)
        _threshold_increment(threshold_counts["distribution_probability_gte_3x_entry"], three_x)
        _threshold_increment(threshold_counts["combined_gate"], entry_plus and three_x)
    return {
        "bucket_rows": len(bucket_rows),
        "with_distribution_probability": with_distribution,
        "without_distribution_probability": without_distribution,
        "threshold_insufficient_data_rows": insufficient_threshold_data,
        "threshold_slices": threshold_counts,
    }


def _threshold_increment(counts: dict[str, int], passed: bool) -> None:
    counts["pass" if passed else "fail"] += 1


def _card_distribution_probability(card: dict[str, Any]) -> float | None:
    bucket = card.get("bucket") if isinstance(card.get("bucket"), dict) else {}
    return _first_number(bucket.get("distribution_probability"), card.get("distribution_probability"), default=None)


def _card_entry_price(card: dict[str, Any], row: ReplayComparisonRow) -> float | None:
    original_signal = (row.original_artifact or {}).get("strategy_signal") if isinstance(row.original_artifact, dict) else {}
    replayed_signal = (row.replayed_artifact or {}).get("strategy_signal") if isinstance(row.replayed_artifact, dict) else {}
    return _first_number(
        card.get("entry_price"),
        original_signal.get("market_price") if isinstance(original_signal, dict) else None,
        replayed_signal.get("market_price") if isinstance(replayed_signal, dict) else None,
        default=None,
    )


def _has_hotfix_bridge_reason(card: dict[str, Any], row: ReplayComparisonRow) -> bool:
    return any(reason in WEATHER_HIDDEN_GEM_HOTFIX_BRIDGE_REASON_CODES for reason in _card_reason_codes(card, row))


def _card_rejection_reason(card: dict[str, Any], _row: ReplayComparisonRow) -> str | None:
    reason_codes = card.get("reason_codes") if isinstance(card.get("reason_codes"), dict) else {}
    for value in (reason_codes.get("beta_reject"), reason_codes.get("weather_reject")):
        reason = _clean_optional_label(value)
        if reason:
            return reason
    return None


def _card_reason_codes(card: dict[str, Any], row: ReplayComparisonRow) -> list[str]:
    reason_codes = card.get("reason_codes") if isinstance(card.get("reason_codes"), dict) else {}
    values = [
        reason_codes.get("beta_reject"),
        reason_codes.get("weather_reject"),
        row.replayed_reason_code,
        row.original_reason_code,
    ]
    cleaned: list[str] = []
    for value in values:
        label = _clean_optional_label(value)
        if label and label not in cleaned:
            cleaned.append(label)
    return cleaned


def _clean_label(value: Any) -> str:
    return _clean_optional_label(value) or "unknown"


def _clean_optional_label(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _apply_resolution_scoring(
    rows: list[ReplayComparisonRow],
    *,
    resolution_records: Iterable[dict[str, Any]] | None = None,
    resolution_paths: Iterable[str | Path] | None = None,
) -> None:
    resolutions = _load_resolution_records(resolution_records=resolution_records, resolution_paths=resolution_paths)
    if not resolutions:
        return

    exact, by_run_market, by_shared_candidate, by_market = _build_resolution_outcome_maps(resolutions)
    for row in rows:
        outcome = (
            (by_shared_candidate.get(row.shared_candidate_id) if row.shared_candidate_id not in (None, "") else None)
            or by_run_market.get(_comparison_run_market_identity(row))
            or exact.get(_comparison_identity(row))
            or by_market.get(row.market_id)
        )
        if outcome is None:
            continue
        row.outcome = outcome
        row.missed_win = _is_skip(row.original_action) and _is_correct_buy(row.replayed_action, outcome)
        row.bad_buy_removed = _is_incorrect_buy(row.original_action, outcome) and _is_skip(row.replayed_action)
        row.bad_buy_added = _is_skip(row.original_action) and _is_incorrect_buy(row.replayed_action, outcome)


def _resolution_identity(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("market_id") or ""),
        str(row.get("experiment_id") or ""),
        str(row.get("strategy_version") or ""),
    )


def _comparison_identity(row: ReplayComparisonRow) -> tuple[str, str, str]:
    return (
        row.market_id,
        str(row.experiment_id or ""),
        str(row.strategy_version or ""),
    )


def _resolution_run_market_identity(row: dict[str, Any]) -> tuple[str, str]:
    return (
        str(row.get("run_id") or ""),
        str(row.get("market_id") or ""),
    )


def _comparison_run_market_identity(row: ReplayComparisonRow) -> tuple[str, str]:
    return (
        str(row.run_id or ""),
        row.market_id,
    )


def _load_resolution_records(
    *,
    resolution_records: Iterable[dict[str, Any]] | None = None,
    resolution_paths: Iterable[str | Path] | None = None,
) -> list[dict[str, Any]]:
    resolutions = list(resolution_records or [])
    for path_value in resolution_paths or []:
        resolutions.extend(load_jsonl(Path(path_value)))
    return resolutions


def _load_agent_decision_records(
    *,
    decision_records: Iterable[dict[str, Any]] | None = None,
    decision_paths: Iterable[str | Path] | None = None,
) -> list[dict[str, Any]] | None:
    if decision_records is None and not decision_paths:
        return None
    rows = list(decision_records or [])
    if decision_paths:
        rows.extend(load_agent_decision_rows(decision_paths))
    return rows


def _build_resolution_outcome_maps(
    resolutions: Iterable[dict[str, Any]],
) -> tuple[dict[tuple[str, str, str], str], dict[tuple[str, str], str], dict[str, str], dict[str, str]]:
    exact: dict[tuple[str, str, str], str] = {}
    by_run_market: dict[tuple[str, str], str] = {}
    by_shared_candidate: dict[str, str] = {}
    by_market: dict[str, str] = {}
    for resolution_row in resolutions:
        outcome = _row_outcome(resolution_row)
        if outcome is None:
            continue
        market_id = str(resolution_row.get("market_id") or "")
        if not market_id:
            continue
        exact[_resolution_identity(resolution_row)] = outcome
        by_run_market.setdefault(_resolution_run_market_identity(resolution_row), outcome)
        shared_candidate_id = shared_candidate_id_from_row(resolution_row)
        if shared_candidate_id not in (None, ""):
            by_shared_candidate.setdefault(shared_candidate_id, outcome)
        by_market.setdefault(market_id, outcome)
    return exact, by_run_market, by_shared_candidate, by_market


def _apply_resolution_outcomes(
    rows: list[dict[str, Any]],
    *,
    resolution_records: Iterable[dict[str, Any]] | None = None,
    resolution_paths: Iterable[str | Path] | None = None,
) -> None:
    resolutions = _load_resolution_records(resolution_records=resolution_records, resolution_paths=resolution_paths)
    if not resolutions:
        return
    exact, by_run_market, by_shared_candidate, by_market = _build_resolution_outcome_maps(resolutions)
    for row in rows:
        if not isinstance(row, dict):
            continue
        shared_candidate_id = shared_candidate_id_from_row(row)
        outcome = (
            (by_shared_candidate.get(shared_candidate_id) if shared_candidate_id not in (None, "") else None)
            or by_run_market.get(_resolution_run_market_identity(row))
            or exact.get(_resolution_identity(row))
            or by_market.get(str(row.get("market_id") or ""))
        )
        if outcome is None:
            continue
        resolution = row.get("resolution") if isinstance(row.get("resolution"), dict) else {}
        row["resolution"] = {**resolution, "outcome": outcome}


def _summary_metrics(rows: list[ReplayComparisonRow]) -> dict[str, Any]:
    return {
        "total": len(rows),
        "action_changed": sum(1 for row in rows if row.action_changed),
        "reason_changed": sum(1 for row in rows if row.reason_changed),
        "missed_wins": sum(1 for row in rows if row.missed_win),
        "over_filtered_wins": sum(1 for row in rows if row.missed_win),
        "bad_buys_removed": sum(1 for row in rows if row.bad_buy_removed),
        "bad_buys_added": sum(1 for row in rows if row.bad_buy_added),
        "outcomes": _counts(row.outcome or "unknown" for row in rows),
    }


def _summarize_dual_policy_replay_rows(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    eligible_rows = [dict(row) for row in rows if _has_dual_policy_metadata(row)]
    return {
        "schema_version": 1,
        "eligible_rows": len(eligible_rows),
        "authoritative_main": {
            "decision_path": "main_decision",
            "normal_policy_path": "normal_decision",
            "runtime_counts": _counts(_dual_policy_main_runtime(row) or "unknown" for row in eligible_rows),
            "authoritative": True,
        },
        "hypothetical_shadow": {
            "decision_path": "shadow_decision",
            "non_mutating": True,
        },
        "decision_columns": summarize_dual_policy_snapshot_rows(eligible_rows),
        "pnl_comparison": summarize_dual_policy_pnl_snapshot_rows(eligible_rows),
    }


def _has_dual_policy_metadata(row: dict[str, Any] | None) -> bool:
    if not isinstance(row, dict):
        return False
    if any(isinstance(row.get(key), dict) for key in ("main_decision", "normal_decision", "shadow_decision", "decision_delta")):
        return True
    shared = row.get("shared_candidate")
    return isinstance(shared, dict) and any(isinstance(shared.get(key), dict) for key in ("main_decision", "normal_decision", "shadow_decision", "decision_delta"))


def _dual_policy_main_runtime(row: dict[str, Any]) -> str | None:
    if not isinstance(row, dict):
        return None
    value = row.get("main_runtime")
    if value not in (None, ""):
        return str(value)
    shared = row.get("shared_candidate")
    if isinstance(shared, dict) and shared.get("main_runtime") not in (None, ""):
        return str(shared.get("main_runtime"))
    main_decision = row.get("main_decision")
    if isinstance(main_decision, dict) and main_decision.get("runtime") not in (None, ""):
        return str(main_decision.get("runtime"))
    if isinstance(shared, dict):
        shared_main_decision = shared.get("main_decision")
        if isinstance(shared_main_decision, dict) and shared_main_decision.get("runtime") not in (None, ""):
            return str(shared_main_decision.get("runtime"))
    return None


def _counts(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def _increment(counts: dict[str, int], value: Any) -> None:
    key = str(value)
    counts[key] = counts.get(key, 0) + 1


def _stored_action(row: dict[str, Any]) -> str:
    decision_type = str(row.get("decision_type") or "").lower()
    if decision_type == "skip":
        return "SKIP"
    direction = str(row.get("direction") or "SKIP").upper()
    if direction in {"BUY_YES", "BUY_NO"}:
        return direction
    return "SKIP"


def _shared_pipeline_reason(row: dict[str, Any]) -> str | None:
    shared_pipeline = row.get("shared_pipeline")
    if isinstance(shared_pipeline, dict):
        return _coerce_reason(shared_pipeline.get("final_reason_code"))
    return None


def _normalize_action(value: Any) -> str:
    action = str(value or "SKIP").upper()
    if action in {"BUY_YES", "BUY_NO"}:
        return action
    return "SKIP"


def _coerce_reason(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _first_number(*values: Any, default: float | None = None) -> float | None:
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return default


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _iter_replay_dataset_paths(root: Path) -> Iterable[Path]:
    if root.is_file():
        if _is_replay_dataset_path(root):
            yield root
        return
    if not root.exists():
        yield root
        return
    for filename in REPLAY_DATASET_FILENAMES:
        direct = root / filename
        if direct.is_file():
            yield direct
    for pattern in ("*.jsonl", "*.jsonl.gz"):
        for path in sorted(root.rglob(pattern)):
            if _is_replay_dataset_path(path):
                yield path


def _is_replay_dataset_path(path: Path) -> bool:
    if path.name in REPLAY_DATASET_FILENAMES:
        return True
    stem = _jsonl_stem(path)
    if stem is None:
        return False
    for dataset_stem in REPLAY_DATASET_STEMS:
        if stem == dataset_stem or stem.startswith(f"{dataset_stem}-"):
            return True
    return False


def _jsonl_stem(path: Path) -> str | None:
    name = path.name
    if name.endswith(".jsonl.gz"):
        return name.removesuffix(".jsonl.gz")
    if name.endswith(".jsonl"):
        return name.removesuffix(".jsonl")
    return None


def _inspect_replay_dataset(path: Path) -> PredictionLabReplayDataset:
    kind = _dataset_kind(path)
    resolved_path = str(path)
    if not path.exists():
        return PredictionLabReplayDataset(
            dataset_id=resolved_path,
            path=resolved_path,
            kind=kind,
            min_observed_at=None,
            max_observed_at=None,
            months=(),
            row_count=0,
            quality="invalid",
            exclusion_reason="configured_root_or_dataset_missing",
            warnings=("configured_root_or_dataset_missing",),
        )

    filename_month = _partitioned_month_from_path(path)
    manifest = _dataset_partition_manifest(path)
    if filename_month and manifest:
        shard_counts = manifest.get("shard_counts") if isinstance(manifest.get("shard_counts"), dict) else {}
        row_count = int(shard_counts.get(filename_month) or 0)
        if row_count > 0:
            return PredictionLabReplayDataset(
                dataset_id=resolved_path,
                path=resolved_path,
                kind=kind,
                min_observed_at=f"{filename_month}-01T00:00:00+00:00",
                max_observed_at=f"{filename_month}-01T00:00:00+00:00",
                months=(filename_month,),
                row_count=row_count,
                quality="valid",
                warnings=(),
            )

    row_count = 0
    invalid_json_lines = 0
    missing_timestamp_rows = 0
    min_observed: datetime | None = None
    max_observed: datetime | None = None
    observed_months: set[str] = set()
    try:
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    invalid_json_lines += 1
                    continue
                if not isinstance(row, dict):
                    invalid_json_lines += 1
                    continue
                row_count += 1
                observed_at = _row_observed_at(row)
                if observed_at is None:
                    missing_timestamp_rows += 1
                    continue
                min_observed = observed_at if min_observed is None else min(min_observed, observed_at)
                max_observed = observed_at if max_observed is None else max(max_observed, observed_at)
                month = _month_key(observed_at)
                if month:
                    observed_months.add(month)
    except OSError as exc:
        return PredictionLabReplayDataset(
            dataset_id=resolved_path,
            path=resolved_path,
            kind=kind,
            min_observed_at=None,
            max_observed_at=None,
            months=(),
            row_count=0,
            quality="invalid",
            exclusion_reason=f"dataset_unreadable:{exc.__class__.__name__}",
            warnings=(f"dataset_unreadable:{exc.__class__.__name__}",),
        )

    metadata_min, metadata_max = _dataset_metadata_range(path)
    warnings: list[str] = []
    if invalid_json_lines:
        warnings.append("invalid_json_lines")
    if missing_timestamp_rows:
        warnings.append("rows_missing_observed_timestamp")

    min_dt = metadata_min or min_observed
    max_dt = metadata_max or max_observed
    months = sorted(observed_months)
    if not months and min_dt and max_dt:
        months = sorted({_month_key(min_dt), _month_key(max_dt)} - {None})
    if len(months) > 1:
        warnings.append("dataset_spans_multiple_months")

    quality = "valid"
    exclusion_reason = None
    if not row_count:
        quality = "invalid"
        exclusion_reason = "dataset_has_no_rows"
        warnings.append("dataset_has_no_rows")
    elif not months or min_dt is None or max_dt is None:
        quality = "invalid"
        exclusion_reason = "dataset_has_no_observed_month"
        warnings.append("dataset_has_no_observed_month")
    elif invalid_json_lines or missing_timestamp_rows:
        quality = "partial"

    return PredictionLabReplayDataset(
        dataset_id=resolved_path,
        path=resolved_path,
        kind=kind,
        min_observed_at=_dt_iso(min_dt),
        max_observed_at=_dt_iso(max_dt),
        months=tuple(months),
        row_count=row_count,
        quality=quality,
        exclusion_reason=exclusion_reason,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _partitioned_month_from_path(path: Path) -> str | None:
    stem = _jsonl_stem(path)
    if stem is None:
        return None
    for dataset_stem in REPLAY_DATASET_STEMS:
        match = re.fullmatch(rf"{re.escape(dataset_stem)}-(\d{{4}}-\d{{2}})", stem)
        if match:
            return match.group(1)
    return None


def _dataset_partition_manifest(path: Path) -> dict[str, Any] | None:
    stem = _jsonl_stem(path)
    if stem is None:
        return None
    source_stem = None
    for dataset_stem in REPLAY_DATASET_STEMS:
        if stem == dataset_stem or stem.startswith(f"{dataset_stem}-"):
            source_stem = dataset_stem
            break
    if source_stem is None:
        return None
    manifest_path = path.parent / f"{source_stem}-partition-manifest.json"
    if not manifest_path.exists():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _dataset_metadata_range(path: Path) -> tuple[datetime | None, datetime | None]:
    partition_manifest = _dataset_partition_manifest(path)
    if partition_manifest:
        min_value = _first_nested_value(partition_manifest, ("min_observed_at", "min_timestamp", "start_observed_at", "start_at"))
        max_value = _first_nested_value(partition_manifest, ("max_observed_at", "max_timestamp", "end_observed_at", "end_at"))
        min_dt = _parse_dt(min_value)
        max_dt = _parse_dt(max_value)
        if min_dt or max_dt:
            return min_dt, max_dt
    for metadata_name in ("summary.json", "manifest.json", "metadata.json"):
        metadata_path = path.parent / metadata_name
        if not metadata_path.exists():
            continue
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        min_value = _first_nested_value(payload, ("min_observed_at", "min_timestamp", "start_observed_at", "start_at"))
        max_value = _first_nested_value(payload, ("max_observed_at", "max_timestamp", "end_observed_at", "end_at"))
        min_dt = _parse_dt(min_value)
        max_dt = _parse_dt(max_value)
        if min_dt or max_dt:
            return min_dt, max_dt
    return None, None


def _first_nested_value(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    pending: list[Any] = [payload]
    while pending:
        value = pending.pop(0)
        if not isinstance(value, dict):
            continue
        for key in keys:
            if value.get(key) not in (None, ""):
                return value.get(key)
        pending.extend(item for item in value.values() if isinstance(item, dict))
    return None


def _row_observed_at(row: dict[str, Any]) -> datetime | None:
    artifact = row.get("decision_artifact")
    if not isinstance(artifact, dict):
        artifact = row.get("original_artifact") if isinstance(row.get("original_artifact"), dict) else {}
    candidates = (
        row.get("observed_at"),
        row.get("timestamp"),
        row.get("recorded_at"),
        row.get("created_at"),
        row.get("collected_at"),
        row.get("_fetched_at"),
        row.get("as_of"),
        row.get("decided_at"),
        row.get("started_at"),
        row.get("finished_at"),
        artifact.get("observed_at") if isinstance(artifact, dict) else None,
        artifact.get("as_of") if isinstance(artifact, dict) else None,
        artifact.get("timestamp") if isinstance(artifact, dict) else None,
        artifact.get("decided_at") if isinstance(artifact, dict) else None,
        artifact.get("created_at") if isinstance(artifact, dict) else None,
    )
    for candidate in candidates:
        parsed = _parse_dt(candidate)
        if parsed is not None:
            return parsed
    return None


def _dataset_kind(path: Path) -> str:
    stem = _jsonl_stem(path) or path.stem
    if stem == "replay_rows" or stem.startswith("replay_rows-"):
        return "replay_rows"
    if stem == "market_snapshots" or stem.startswith("market_snapshots-"):
        return "market_snapshots"
    if stem == "predictions" or stem.startswith("predictions-"):
        return "predictions"
    return "unknown"


def _month_key(value: datetime | None) -> str | None:
    if value is None:
        return None
    return f"{value.year:04d}-{value.month:02d}"


def _dt_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _normalize_requested_months(months: int | str) -> int | str:
    if isinstance(months, str):
        text = months.strip().lower()
        if text == "all":
            return "all"
        try:
            months = int(text)
        except ValueError as exc:
            raise ValueError("--months must be a positive integer or 'all'") from exc
    if isinstance(months, bool) or int(months) < 1:
        raise ValueError("--months must be a positive integer or 'all'")
    return int(months)


def _dataset_selection_key(dataset: PredictionLabReplayDataset) -> tuple[int, int, int, str]:
    quality_rank = {"valid": 2, "partial": 1, "invalid": 0}.get(dataset.quality, 0)
    kind_rank = {"market_snapshots": 3, "replay_rows": 2, "predictions": 1, "unknown": 0}.get(dataset.kind, 0)
    return (quality_rank, dataset.row_count, kind_rank, dataset.path)


def _row_outcome(row: dict[str, Any]) -> str | None:
    resolution = row.get("resolution") if isinstance(row.get("resolution"), dict) else {}
    outcome = str(resolution.get("outcome") or row.get("outcome") or "").upper()
    if outcome in {"YES", "NO", "VOID"}:
        return outcome
    return None


def _is_skip(action: str) -> bool:
    return _normalize_action(action) == "SKIP"


def _is_correct_buy(action: str, outcome: str | None) -> bool:
    normalized = _normalize_action(action)
    return (normalized == "BUY_YES" and outcome == "YES") or (normalized == "BUY_NO" and outcome == "NO")


def _is_incorrect_buy(action: str, outcome: str | None) -> bool:
    normalized = _normalize_action(action)
    if normalized not in {"BUY_YES", "BUY_NO"} or outcome not in {"YES", "NO"}:
        return False
    return not _is_correct_buy(normalized, outcome)
