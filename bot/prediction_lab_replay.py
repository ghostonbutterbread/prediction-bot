"""Replay Prediction Lab collector artifacts through the shared evaluator."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

from bot.decision_pipeline import DecisionPipelineEvaluator, build_fixed_opportunity_account_state
from bot.file_ops import load_jsonl

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


class LiveCurrentSourceForbiddenError(RuntimeError):
    """Raised when historical replay would touch current live source data."""


@dataclass(slots=True)
class ReplayArtifactInput:
    row: dict[str, Any]
    artifact: dict[str, Any]
    source_path: str | None = None
    line_number: int | None = None


@dataclass(slots=True)
class ReplayComparisonRow:
    market_id: str
    original_action: str
    replayed_action: str
    original_reason_code: str | None
    replayed_reason_code: str | None
    action_changed: bool
    reason_changed: bool
    source_mode: str
    order_book_mode: str
    execution_snapshot_mode: str
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
class PredictionLabReplayResult:
    rows: list[ReplayComparisonRow]
    summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": dict(self.summary),
            "rows": [row.to_dict() for row in self.rows],
        }


def load_replay_artifacts(paths: Iterable[str | Path], *, limit: int | None = None) -> list[ReplayArtifactInput]:
    """Load collector prediction/snapshot JSONL rows that contain replayable artifacts."""

    records: list[ReplayArtifactInput] = []
    for path_value in paths:
        path = Path(path_value)
        for index, row in enumerate(load_jsonl(path), start=1):
            artifact = row.get("decision_artifact")
            if not isinstance(artifact, dict):
                artifact = _legacy_artifact_from_row(row)
            records.append(
                ReplayArtifactInput(
                    row=dict(row),
                    artifact=dict(artifact),
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
) -> PredictionLabReplayResult:
    """Replay recorded artifacts and compare original vs replayed decisions.

    ``live_source_policy`` controls attempts to call current source feeds during
    historical replay:

    - ``fail`` raises ``LiveCurrentSourceForbiddenError``.
    - ``warn_skip`` replaces the live call with ``None`` and records a warning.
    - ``allow`` leaves the evaluator untouched and labels the run as unsafe.
    """

    replay_config = dict(config or {})
    replay_evaluator = evaluator or DecisionPipelineEvaluator(replay_config)
    rows: list[ReplayComparisonRow] = []
    policy = str(live_source_policy or "fail").lower()
    if policy not in {"fail", "warn_skip", "allow"}:
        raise ValueError("live_source_policy must be one of: fail, warn_skip, allow")

    for raw_record in records:
        record = _coerce_record(raw_record)
        original_artifact = record.artifact
        original_action = _normalize_action(original_artifact.get("final_action") or _stored_action(record.row))
        original_reason = _coerce_reason(original_artifact.get("final_reason_code") or _shared_pipeline_reason(record.row))
        market = _market_from_record(record)
        source_mode = classify_source_mode(original_artifact, record.row)
        order_book_mode = classify_order_book_mode(original_artifact)
        execution_mode = classify_execution_snapshot_mode(original_artifact)
        row_warnings = _source_mode_warnings(source_mode, order_book_mode)

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
        outcome = _row_outcome(record.row)
        row = ReplayComparisonRow(
            market_id=str(getattr(market, "id", "") or ""),
            original_action=original_action,
            replayed_action=replayed_action,
            original_reason_code=original_reason,
            replayed_reason_code=replayed_reason,
            action_changed=original_action != replayed_action,
            reason_changed=original_reason != replayed_reason,
            source_mode=source_mode,
            order_book_mode=order_book_mode,
            execution_snapshot_mode=execution_mode,
            warnings=row_warnings,
            source_path=record.source_path,
            line_number=record.line_number,
            original_artifact=original_artifact,
            replayed_artifact=replayed,
            outcome=outcome,
            missed_win=_is_skip(original_action) and _is_correct_buy(replayed_action, outcome),
            bad_buy_removed=_is_incorrect_buy(original_action, outcome) and _is_skip(replayed_action),
            bad_buy_added=_is_skip(original_action) and _is_incorrect_buy(replayed_action, outcome),
        )
        rows.append(row)

    return PredictionLabReplayResult(rows=rows, summary=_summarize(rows))


def replay_from_paths(
    paths: Iterable[str | Path],
    *,
    config: dict[str, Any] | None = None,
    evaluator: DecisionPipelineEvaluator | None = None,
    limit: int | None = None,
    bankroll_usd: float = 100.0,
    live_source_policy: str = "fail",
    require_recorded_source: bool = False,
) -> PredictionLabReplayResult:
    records = load_replay_artifacts(paths, limit=limit)
    return replay_recorded_artifacts(
        records,
        config=config,
        evaluator=evaluator,
        bankroll_usd=bankroll_usd,
        live_source_policy=live_source_policy,
        require_recorded_source=require_recorded_source,
    )


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
        if SOURCE_RECORDED_AS_OF in modes and has_evidence:
            return SOURCE_RECORDED_AS_OF
        if SOURCE_HISTORICAL_POST_FACTO in modes and has_evidence:
            return SOURCE_HISTORICAL_POST_FACTO
        if SOURCE_SYNTHETIC in modes and has_evidence:
            return SOURCE_SYNTHETIC
        if has_evidence:
            return SOURCE_RECORDED_AS_OF

    source_context = artifact.get("source_context") if isinstance(artifact.get("source_context"), dict) else {}
    mode = str(source_context.get("source_mode") or source_context.get("mode") or "").lower()
    data = source_context.get("data")
    has_context_evidence = _source_context_has_evidence(data)
    if mode in {SOURCE_RECORDED_AS_OF, SOURCE_HISTORICAL_POST_FACTO, SOURCE_LIVE_CURRENT_FORBIDDEN, SOURCE_SYNTHETIC, SOURCE_MISSING}:
        if mode in {SOURCE_RECORDED_AS_OF, SOURCE_HISTORICAL_POST_FACTO, SOURCE_SYNTHETIC} and not has_context_evidence:
            return SOURCE_MISSING
        return mode
    if mode in {"live", "live_current", "current"}:
        return SOURCE_LIVE_CURRENT_FORBIDDEN
    if mode in {"historical", "post_facto"}:
        if not has_context_evidence:
            return SOURCE_MISSING
        return SOURCE_HISTORICAL_POST_FACTO

    source = str(source_context.get("source") or "").lower()
    if source in {"live", "live_current", "current"}:
        return SOURCE_LIVE_CURRENT_FORBIDDEN
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
    source = str(snapshot.get("source") or "").lower()
    data = snapshot.get("data")
    if source in {"book", "recorded_book"} and isinstance(data, dict) and data:
        return ORDER_BOOK_RECORDED
    if source in {"fallback", "signal_price_fallback"}:
        return ORDER_BOOK_SIGNAL_PRICE_FALLBACK
    if source == "synthetic":
        return ORDER_BOOK_SYNTHETIC

    execution_source = str(artifact.get("execution_snapshot_source") or "").lower()
    if execution_source in {"fallback", "signal_price_fallback"}:
        return ORDER_BOOK_SIGNAL_PRICE_FALLBACK
    if execution_source == "synthetic":
        return ORDER_BOOK_SYNTHETIC
    return ORDER_BOOK_MISSING


def classify_execution_snapshot_mode(artifact: dict[str, Any]) -> str:
    snapshot = artifact.get("execution_snapshot") if isinstance(artifact.get("execution_snapshot"), dict) else {}
    source = str(snapshot.get("source") or artifact.get("execution_snapshot_source") or "").lower()
    if source == "book":
        return ORDER_BOOK_RECORDED
    if source == "fallback":
        return ORDER_BOOK_SIGNAL_PRICE_FALLBACK
    if source == "synthetic":
        return ORDER_BOOK_SYNTHETIC
    return ORDER_BOOK_MISSING


def _coerce_record(value: ReplayArtifactInput | dict[str, Any]) -> ReplayArtifactInput:
    if isinstance(value, ReplayArtifactInput):
        return value
    row = dict(value)
    artifact = row.get("decision_artifact")
    if not isinstance(artifact, dict):
        artifact = _legacy_artifact_from_row(row)
    return ReplayArtifactInput(row=row, artifact=dict(artifact))


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
    for key, value in {
        "market_group": row.get("group"),
        "series": row.get("series"),
        "event_ticker": row.get("event_ticker"),
    }.items():
        if value is not None:
            metadata.setdefault(key, value)

    return SimpleNamespace(
        id=str(artifact.get("market_id") or row.get("market_id") or signal.get("market_id") or ""),
        exchange=str(signal.get("exchange") or row.get("exchange") or "kalshi"),
        question=str(signal.get("question") or row.get("question") or ""),
        yes_price=_first_number(signal.get("yes_market_price"), signal.get("yes_price"), row.get("yes_market_price"), row.get("yes_price"), row.get("market_price"), default=0.0),
        no_price=_first_number(signal.get("no_market_price"), signal.get("no_price"), row.get("no_market_price"), row.get("no_price"), default=None),
        volume=_first_number(row.get("volume"), signal.get("market_volume"), default=0.0),
        category=str(metadata.get("series") or row.get("series") or row.get("group") or "unknown"),
        metadata=metadata,
        closes_at=None,
    )


def _recorded_order_book(artifact: dict[str, Any]) -> dict[str, Any] | None:
    snapshot = artifact.get("order_book_snapshot") if isinstance(artifact.get("order_book_snapshot"), dict) else {}
    data = snapshot.get("data")
    if isinstance(data, dict) and data:
        return dict(data)
    order_book = artifact.get("order_book")
    if isinstance(order_book, dict) and order_book:
        return dict(order_book)
    return None


def _recorded_source_context(artifact: dict[str, Any]) -> dict[str, Any]:
    source_context = artifact.get("source_context") if isinstance(artifact.get("source_context"), dict) else {}
    data = source_context.get("data")
    context = dict(data) if isinstance(data, dict) else {}
    snapshots = artifact.get("source_snapshots")
    if isinstance(snapshots, list):
        context["source_snapshots"] = [dict(snapshot) for snapshot in snapshots if isinstance(snapshot, dict)]
    return context


def _recorded_execution_snapshot(artifact: dict[str, Any]) -> dict[str, Any] | None:
    snapshot = artifact.get("execution_snapshot")
    if not isinstance(snapshot, dict) or not snapshot:
        return None
    recorded = dict(snapshot)
    if not recorded.get("source") and artifact.get("execution_snapshot_source"):
        recorded["source"] = artifact.get("execution_snapshot_source")
    return recorded


def _record_as_of(artifact: dict[str, Any], row: dict[str, Any]) -> datetime | None:
    for value in (
        artifact.get("as_of"),
        artifact.get("observed_at"),
        row.get("observed_at"),
        row.get("timestamp"),
    ):
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
    snapshots = artifact.get("source_snapshots")
    if isinstance(snapshots, list):
        for snapshot in snapshots:
            if not isinstance(snapshot, dict):
                continue
            method_name = _source_method_for_snapshot(snapshot)
            signal = _signal_from_source_snapshot(snapshot)
            if method_name and signal:
                signals[method_name] = signal

    source_context = artifact.get("source_context") if isinstance(artifact.get("source_context"), dict) else {}
    data = source_context.get("data")
    if isinstance(data, dict):
        for source_name, value in data.items():
            if source_name in SOURCE_CONTEXT_METADATA_KEYS:
                continue
            if not isinstance(value, dict):
                continue
            method_name = SOURCE_SIGNAL_METHODS.get(str(source_name).lower())
            signal = _normalize_recorded_source_signal(value, source_name=str(source_name))
            if method_name and signal:
                signals.setdefault(method_name, signal)

    return signals


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


def _summarize(rows: list[ReplayComparisonRow]) -> dict[str, Any]:
    return {
        "total": len(rows),
        "action_changed": sum(1 for row in rows if row.action_changed),
        "reason_changed": sum(1 for row in rows if row.reason_changed),
        "missed_wins": sum(1 for row in rows if row.missed_win),
        "over_filtered_wins": sum(1 for row in rows if row.missed_win),
        "bad_buys_removed": sum(1 for row in rows if row.bad_buy_removed),
        "bad_buys_added": sum(1 for row in rows if row.bad_buy_added),
        "outcomes": _counts(row.outcome or "unknown" for row in rows),
        "source_modes": _counts(row.source_mode for row in rows),
        "order_book_modes": _counts(row.order_book_mode for row in rows),
        "warning_count": sum(len(row.warnings) for row in rows),
    }


def _counts(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


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


def _row_outcome(row: dict[str, Any]) -> str | None:
    resolution = row.get("resolution") if isinstance(row.get("resolution"), dict) else {}
    outcome = str(resolution.get("outcome") or row.get("outcome") or "").upper()
    if outcome in {"YES", "NO"}:
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
