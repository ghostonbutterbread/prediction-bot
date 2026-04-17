from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from .analysis import WeatherSampleRecord, compare_sample_records, load_historical_csv_samples
from .registry import WeatherRegistry
from .thresholds import extract_threshold_value, infer_question_side


SUPPORTED_MARKET_TYPES = {"high_temp", "low_temp", "temperature"}
SUPPORTED_OUTCOMES = {"YES": 1.0, "NO": 0.0}


@dataclass(frozen=True)
class WeatherTrainingExample:
    market_id: str
    series_ticker: str
    city_id: str
    source_id: str
    market_type: str
    question: str
    event_date: str
    observed_at: str | None
    resolved_at: str | None
    outcome: float
    threshold_value: float | None
    question_side: str
    sample_kind: str
    lead_time_hours: float | None
    evidence: dict[str, Any] = field(default_factory=dict)
    yes_price: float | None = None
    no_price: float | None = None
    volume: float | None = None
    liquidity: float | None = None
    spread: float | None = None
    time_to_close_hours: float | None = None
    fees: float | None = None
    model_probability: float | None = None
    model_edge: float | None = None
    action: str | None = None
    position_size: float | None = None
    realized_pnl: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "market_id": self.market_id,
            "series_ticker": self.series_ticker,
            "city_id": self.city_id,
            "source_id": self.source_id,
            "market_type": self.market_type,
            "question": self.question,
            "event_date": self.event_date,
            "observed_at": self.observed_at,
            "resolved_at": self.resolved_at,
            "outcome": self.outcome,
            "threshold_value": self.threshold_value,
            "question_side": self.question_side,
            "sample_kind": self.sample_kind,
            "lead_time_hours": self.lead_time_hours,
            "evidence": dict(self.evidence),
            "yes_price": self.yes_price,
            "no_price": self.no_price,
            "volume": self.volume,
            "liquidity": self.liquidity,
            "spread": self.spread,
            "time_to_close_hours": self.time_to_close_hours,
            "fees": self.fees,
            "model_probability": self.model_probability,
            "model_edge": self.model_edge,
            "action": self.action,
            "position_size": self.position_size,
            "realized_pnl": self.realized_pnl,
        }


@dataclass(frozen=True)
class StructuralTrainingPolicy:
    min_samples_per_city_source: int = 12
    min_unique_days: int = 4
    holdout_fraction: float = 0.25

    def as_dict(self) -> dict[str, float | int]:
        return {
            "min_samples_per_city_source": self.min_samples_per_city_source,
            "min_unique_days": self.min_unique_days,
            "holdout_fraction": self.holdout_fraction,
        }


@dataclass(frozen=True)
class TemperatureTrainingPolicy:
    min_samples_per_city_source: int = 12
    min_unique_days: int = 4
    holdout_fraction: float = 0.25
    max_trust_score_delta_per_run: float = 5.0
    min_trust_score_delta_to_emit: float = 1.0
    trust_score_step: float = 1.0

    def as_dict(self) -> dict[str, float | int]:
        return {
            "min_samples_per_city_source": self.min_samples_per_city_source,
            "min_unique_days": self.min_unique_days,
            "holdout_fraction": self.holdout_fraction,
            "max_trust_score_delta_per_run": self.max_trust_score_delta_per_run,
            "min_trust_score_delta_to_emit": self.min_trust_score_delta_to_emit,
            "trust_score_step": self.trust_score_step,
        }


@dataclass(frozen=True)
class TemperatureTrainingSample:
    market_id: str
    city_id: str
    source_id: str
    market_type: str
    event_date: str
    yes_price: float
    outcome: float
    observed_at: str | None
    resolved_at: str | None

    def as_dict(self) -> dict[str, str | float | None]:
        return {
            "market_id": self.market_id,
            "city_id": self.city_id,
            "source_id": self.source_id,
            "market_type": self.market_type,
            "event_date": self.event_date,
            "yes_price": self.yes_price,
            "outcome": self.outcome,
            "observed_at": self.observed_at,
            "resolved_at": self.resolved_at,
        }


def load_weather_training_examples_from_history(
    path: str,
    *,
    registry: WeatherRegistry | None = None,
) -> list[WeatherTrainingExample]:
    records = load_historical_csv_samples(path, one_per_series=False)
    return build_weather_training_examples(records, registry=registry)


def build_weather_training_examples(
    records: Iterable[WeatherSampleRecord],
    *,
    registry: WeatherRegistry | None = None,
) -> list[WeatherTrainingExample]:
    examples, _ = _build_weather_training_examples_with_stats(records, registry=registry)
    return examples


def load_temperature_training_samples_from_history(
    path: str,
    *,
    registry: WeatherRegistry | None = None,
) -> list[TemperatureTrainingSample]:
    records = load_historical_csv_samples(path, one_per_series=False)
    return build_temperature_training_samples(records, registry=registry)


def build_temperature_training_samples(
    records: Iterable[WeatherSampleRecord],
    *,
    registry: WeatherRegistry | None = None,
) -> list[TemperatureTrainingSample]:
    samples, _ = _build_temperature_training_samples_with_stats(records, registry=registry)
    return samples


def _build_weather_training_examples_with_stats(
    records: Iterable[WeatherSampleRecord],
    *,
    registry: WeatherRegistry | None = None,
) -> tuple[list[WeatherTrainingExample], dict[str, int]]:
    registry = registry or WeatherRegistry.from_file()
    record_list = list(records)
    comparisons = compare_sample_records(record_list, registry=registry)
    examples: list[WeatherTrainingExample] = []
    stats: Counter[str] = Counter()
    stats["input_records"] = len(record_list)
    stats["comparison_records"] = len(comparisons)

    for record in comparisons:
        city_id = _string_or_none(record.get("registry_city_id"))
        source_id = _string_or_none(record.get("registry_primary_source_id"))
        market_type = str(record.get("normalized_market_type") or "")
        outcome_value = SUPPORTED_OUTCOMES.get(str(record.get("outcome") or "").upper())
        event_date = _event_date(record.get("observed_at"), record.get("resolved_at"))
        if not city_id:
            stats["skipped_missing_city"] += 1
            continue
        if not source_id:
            stats["skipped_missing_source"] += 1
            continue
        if market_type not in SUPPORTED_MARKET_TYPES:
            stats["skipped_unsupported_market_type"] += 1
            continue
        if outcome_value is None:
            stats["skipped_missing_outcome"] += 1
            continue
        if event_date is None:
            stats["skipped_missing_event_date"] += 1
            continue

        question = _string_or_none(record.get("question")) or ""
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        yes_price = _price_or_none(record.get("yes_price"))
        no_price = _price_or_none(record.get("no_price"))
        if yes_price is None and no_price is not None:
            yes_price = round(1.0 - no_price, 6)
        if no_price is None and yes_price is not None:
            no_price = round(1.0 - yes_price, 6)

        examples.append(
            WeatherTrainingExample(
                market_id=str(record.get("market_id") or ""),
                series_ticker=str(record.get("category") or ""),
                city_id=city_id,
                source_id=source_id,
                market_type=market_type,
                question=question,
                event_date=event_date,
                observed_at=_string_or_none(record.get("observed_at")),
                resolved_at=_string_or_none(record.get("resolved_at")),
                outcome=outcome_value,
                threshold_value=extract_threshold_value(question, metadata),
                question_side=infer_question_side(question, metadata),
                sample_kind=str(record.get("sample_kind") or "unknown"),
                lead_time_hours=_lead_time_hours(record.get("observed_at"), record.get("resolved_at")),
                evidence=_build_structural_evidence(metadata),
                yes_price=yes_price,
                no_price=no_price,
                volume=_float_or_none(record.get("volume")),
            )
        )
        stats["training_examples"] += 1

    examples.sort(
        key=lambda example: (
            example.city_id,
            example.source_id,
            example.market_type,
            example.event_date,
            example.market_id,
        )
    )
    return examples, dict(sorted(stats.items()))


def _build_temperature_training_samples_with_stats(
    records: Iterable[WeatherSampleRecord],
    *,
    registry: WeatherRegistry | None = None,
) -> tuple[list[TemperatureTrainingSample], dict[str, int]]:
    examples, stats = _build_weather_training_examples_with_stats(records, registry=registry)
    samples: list[TemperatureTrainingSample] = []

    for example in examples:
        if example.yes_price is None:
            stats["skipped_missing_yes_price"] = int(stats.get("skipped_missing_yes_price", 0)) + 1
            continue

        samples.append(
            TemperatureTrainingSample(
                market_id=example.market_id,
                city_id=example.city_id,
                source_id=example.source_id,
                market_type=example.market_type,
                event_date=example.event_date,
                yes_price=example.yes_price,
                outcome=example.outcome,
                observed_at=example.observed_at,
                resolved_at=example.resolved_at,
            )
        )

    stats["temperature_samples"] = len(samples)
    return samples, dict(sorted(stats.items()))


def run_structural_training(
    records: Iterable[WeatherSampleRecord],
    *,
    registry: WeatherRegistry | None = None,
    policy: StructuralTrainingPolicy | None = None,
    generated_at: str | None = None,
) -> dict:
    registry = registry or WeatherRegistry.from_file()
    policy = policy or StructuralTrainingPolicy()
    examples, stats = _build_weather_training_examples_with_stats(records, registry=registry)
    report = run_structural_training_from_examples(
        examples,
        registry=registry,
        policy=policy,
        generated_at=generated_at,
    )
    report["summary"]["records"].update(stats)
    return report


def run_structural_training_from_examples(
    examples: Iterable[WeatherTrainingExample],
    *,
    registry: WeatherRegistry | None = None,
    policy: StructuralTrainingPolicy | None = None,
    generated_at: str | None = None,
) -> dict:
    registry = registry or WeatherRegistry.from_file()
    policy = policy or StructuralTrainingPolicy()
    example_list = sorted(
        examples,
        key=lambda example: (
            example.city_id,
            example.source_id,
            example.market_type,
            example.event_date,
            example.market_id,
        ),
    )
    grouped_examples: dict[tuple[str, str, str], list[WeatherTrainingExample]] = defaultdict(list)
    for example in example_list:
        grouped_examples[(example.city_id, example.source_id, example.market_type)].append(example)

    group_reports: list[dict] = []
    blocked_reasons: Counter[str] = Counter()

    for city_id, source_id, market_type in sorted(grouped_examples):
        report = _evaluate_structural_group(
            city_id=city_id,
            source_id=source_id,
            market_type=market_type,
            examples=grouped_examples[(city_id, source_id, market_type)],
            registry=registry,
            policy=policy,
        )
        group_reports.append(report)
        if report.get("decision_reason"):
            blocked_reasons[report["decision_reason"]] += 1

    summary = {
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "dry_run": True,
        "training_mode": "structural",
        "registry_mutated": False,
        "policy": policy.as_dict(),
        "records": {
            "training_examples": len(example_list),
            "groups_evaluated": len(group_reports),
            "groups_scored": sum(1 for report in group_reports if report["decision_reason"] is None),
            "blocked_groups": sum(1 for report in group_reports if report["decision_reason"] is not None),
            "candidate_updates": 0,
        },
        "blocked_reason_counts": dict(sorted(blocked_reasons.items())),
    }

    return {
        "summary": summary,
        "candidate_updates": [],
        "group_reports": group_reports,
    }


def run_price_aware_training(
    records: Iterable[WeatherSampleRecord],
    *,
    registry: WeatherRegistry | None = None,
    policy: TemperatureTrainingPolicy | None = None,
    generated_at: str | None = None,
) -> dict:
    registry = registry or WeatherRegistry.from_file()
    policy = policy or TemperatureTrainingPolicy()
    samples, stats = _build_temperature_training_samples_with_stats(records, registry=registry)
    report = run_price_aware_training_from_samples(
        samples,
        registry=registry,
        policy=policy,
        generated_at=generated_at,
    )
    report["summary"]["records"].update(stats)
    return report


def run_price_aware_training_from_samples(
    samples: Iterable[TemperatureTrainingSample],
    *,
    registry: WeatherRegistry | None = None,
    policy: TemperatureTrainingPolicy | None = None,
    generated_at: str | None = None,
) -> dict:
    registry = registry or WeatherRegistry.from_file()
    policy = policy or TemperatureTrainingPolicy()
    sample_list = sorted(samples, key=lambda sample: (sample.city_id, sample.source_id, sample.event_date, sample.market_id))
    grouped_samples: dict[tuple[str, str], list[TemperatureTrainingSample]] = defaultdict(list)
    for sample in sample_list:
        grouped_samples[(sample.city_id, sample.source_id)].append(sample)

    group_reports: list[dict] = []
    candidate_updates: list[dict] = []
    blocked_reasons: Counter[str] = Counter()

    for city_id, source_id in sorted(grouped_samples):
        report = _evaluate_price_aware_group(
            city_id=city_id,
            source_id=source_id,
            samples=grouped_samples[(city_id, source_id)],
            registry=registry,
            policy=policy,
        )
        group_reports.append(report)
        if report["candidate_update"]:
            candidate_updates.append(report["candidate_update"])
        if report.get("decision_reason"):
            blocked_reasons[report["decision_reason"]] += 1

    summary = {
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "dry_run": True,
        "training_mode": "price_aware",
        "temperature_only": True,
        "registry_mutated": False,
        "policy": policy.as_dict(),
        "records": {
            "temperature_samples": len(sample_list),
            "groups_evaluated": len(group_reports),
            "candidate_updates": len(candidate_updates),
            "blocked_groups": sum(1 for report in group_reports if not report["candidate_update"]),
        },
        "blocked_reason_counts": dict(sorted(blocked_reasons.items())),
    }

    return {
        "summary": summary,
        "candidate_updates": candidate_updates,
        "group_reports": group_reports,
    }


def run_temperature_training(
    records: Iterable[WeatherSampleRecord],
    *,
    registry: WeatherRegistry | None = None,
    policy: TemperatureTrainingPolicy | None = None,
    generated_at: str | None = None,
) -> dict:
    return run_price_aware_training(
        records,
        registry=registry,
        policy=policy,
        generated_at=generated_at,
    )


def run_temperature_training_from_samples(
    samples: Iterable[TemperatureTrainingSample],
    *,
    registry: WeatherRegistry | None = None,
    policy: TemperatureTrainingPolicy | None = None,
    generated_at: str | None = None,
) -> dict:
    return run_price_aware_training_from_samples(
        samples,
        registry=registry,
        policy=policy,
        generated_at=generated_at,
    )


def apply_price_aware_training_updates(
    report: dict[str, Any],
    *,
    registry: WeatherRegistry,
    reviewed_at: str | None = None,
    save: bool = False,
    save_path: str | None = None,
) -> dict[str, Any]:
    reviewed_timestamp = reviewed_at or datetime.now(timezone.utc).isoformat()
    applied_updates: list[dict[str, Any]] = []
    candidate_updates = report.get("candidate_updates", [])
    if not isinstance(candidate_updates, list):
        raise ValueError("report.candidate_updates must be a list")

    for candidate in candidate_updates:
        if not isinstance(candidate, dict):
            raise ValueError("candidate update entries must be objects")
        source_id = str(candidate.get("source_id") or "").strip()
        if not source_id:
            raise ValueError("candidate update missing source_id")
        candidate_score = candidate.get("candidate_trust_score")
        sample_size = candidate.get("sample_size")
        reason = candidate.get("reason") or "Applied from price-aware replay training outcomes."
        updated_source = registry.update_source_score(
            source_id,
            float(candidate_score),
            reviewed_at=reviewed_timestamp,
            sample_size=int(sample_size) if sample_size is not None else None,
            reason=reason,
        )
        applied_candidate = dict(candidate)
        applied_candidate["dry_run"] = False
        applied_candidate["applied"] = True
        applied_candidate["reviewed_at"] = reviewed_timestamp
        applied_candidate["updated_source"] = updated_source
        applied_updates.append(applied_candidate)

    report_summary = dict(report.get("summary", {}))
    report_records = dict(report_summary.get("records", {}))
    report_records["candidate_updates"] = len(candidate_updates)
    report_records["applied_updates"] = len(applied_updates)
    report_summary["records"] = report_records
    report_summary["dry_run"] = False
    report_summary["registry_mutated"] = bool(applied_updates)
    report_summary["applied_at"] = reviewed_timestamp

    updated_report = dict(report)
    updated_report["summary"] = report_summary
    updated_report["candidate_updates"] = applied_updates
    updated_report["applied_updates"] = applied_updates

    if save:
        registry.save(save_path)
        report_summary["registry_saved"] = True
        report_summary["registry_save_path"] = str(save_path or registry.source_path)
    else:
        report_summary["registry_saved"] = False

    return updated_report


def _evaluate_structural_group(
    *,
    city_id: str,
    source_id: str,
    market_type: str,
    examples: list[WeatherTrainingExample],
    registry: WeatherRegistry,
    policy: StructuralTrainingPolicy,
) -> dict:
    current_source = _lookup_source(registry, source_id)
    current_trust_score = float(current_source["trust_score"])
    unique_days = sorted({example.event_date for example in examples})
    holdout_days = _holdout_days(unique_days, policy.holdout_fraction)
    train_examples = [example for example in examples if example.event_date not in holdout_days]
    holdout_examples = [example for example in examples if example.event_date in holdout_days]

    metrics = _score_structural_split(train_examples, holdout_examples)
    gates = {
        "min_samples_per_city_source": len(examples) >= policy.min_samples_per_city_source,
        "min_unique_days": len(unique_days) >= policy.min_unique_days,
        "holdout_has_samples": bool(holdout_examples),
        "train_has_samples": bool(train_examples),
    }
    gates["all_passed"] = all(gates.values())

    decision_reason = None
    if not gates["all_passed"]:
        for gate_name, passed in gates.items():
            if gate_name != "all_passed" and not passed:
                decision_reason = gate_name
                break

    return {
        "city_id": city_id,
        "source_id": source_id,
        "market_type": market_type,
        "current_trust_score": round(current_trust_score, 3),
        "structural_probability": metrics["structural_probability"],
        "sample_size": len(examples),
        "train_sample_size": len(train_examples),
        "holdout_sample_size": len(holdout_examples),
        "unique_days": len(unique_days),
        "train_window": _date_window(train_examples),
        "holdout_window": _date_window(holdout_examples),
        "metrics": metrics,
        "gates": gates,
        "decision_reason": decision_reason,
        "candidate_update": None,
        "reason": "Structural-only scoring on resolved outcomes; no price context or trust-score mutation required.",
    }


def _evaluate_price_aware_group(
    *,
    city_id: str,
    source_id: str,
    samples: list[TemperatureTrainingSample],
    registry: WeatherRegistry,
    policy: TemperatureTrainingPolicy,
) -> dict:
    current_source = _lookup_source(registry, source_id)
    current_trust_score = float(current_source["trust_score"])
    unique_days = sorted({sample.event_date for sample in samples})
    holdout_days = _holdout_days(unique_days, policy.holdout_fraction)
    train_samples = [sample for sample in samples if sample.event_date not in holdout_days]
    holdout_samples = [sample for sample in samples if sample.event_date in holdout_days]

    candidate_trust_score = _fit_candidate_trust_score(train_samples, current_trust_score, policy)
    current_metrics = _score_price_aware_split(train_samples, holdout_samples, current_trust_score)
    candidate_metrics = _score_price_aware_split(train_samples, holdout_samples, candidate_trust_score)
    trust_score_delta = round(candidate_trust_score - current_trust_score, 3)

    gates = {
        "min_samples_per_city_source": len(samples) >= policy.min_samples_per_city_source,
        "min_unique_days": len(unique_days) >= policy.min_unique_days,
        "train_must_improve": candidate_metrics["train_brier"] < current_metrics["train_brier"],
        "holdout_must_not_degrade": candidate_metrics["holdout_brier"] <= current_metrics["holdout_brier"],
        "max_trust_score_delta_per_run": abs(trust_score_delta) <= policy.max_trust_score_delta_per_run,
    }
    gates["all_passed"] = all(gates.values())

    decision_reason = None
    candidate_update = None
    if gates["all_passed"] and abs(trust_score_delta) >= policy.min_trust_score_delta_to_emit:
        candidate_update = {
            "city_id": city_id,
            "source_id": source_id,
            "dry_run": True,
            "current_trust_score": round(current_trust_score, 3),
            "candidate_trust_score": round(candidate_trust_score, 3),
            "trust_score_delta": trust_score_delta,
            "sample_size": len(samples),
            "train_sample_size": len(train_samples),
            "holdout_sample_size": len(holdout_samples),
            "unique_days": len(unique_days),
            "market_types": sorted({sample.market_type for sample in samples}),
            "train_window": _date_window(train_samples),
            "holdout_window": _date_window(holdout_samples),
            "metrics": {
                "baseline_train_brier": current_metrics["train_brier"],
                "candidate_train_brier": candidate_metrics["train_brier"],
                "baseline_holdout_brier": current_metrics["holdout_brier"],
                "candidate_holdout_brier": candidate_metrics["holdout_brier"],
                "train_improvement": round(current_metrics["train_brier"] - candidate_metrics["train_brier"], 6),
                "holdout_improvement": round(current_metrics["holdout_brier"] - candidate_metrics["holdout_brier"], 6),
            },
            "gates": dict(gates),
            "reason": "Dry-run only; candidate trust score calibrated from resolved temperature-market prices without mutating the registry.",
        }
    elif gates["all_passed"]:
        decision_reason = "delta_below_emit_threshold"
    else:
        for gate_name, passed in gates.items():
            if gate_name != "all_passed" and not passed:
                decision_reason = gate_name
                break

    return {
        "city_id": city_id,
        "source_id": source_id,
        "current_trust_score": round(current_trust_score, 3),
        "candidate_trust_score": round(candidate_trust_score, 3),
        "trust_score_delta": trust_score_delta,
        "sample_size": len(samples),
        "train_sample_size": len(train_samples),
        "holdout_sample_size": len(holdout_samples),
        "unique_days": len(unique_days),
        "market_types": sorted({sample.market_type for sample in samples}),
        "train_window": _date_window(train_samples),
        "holdout_window": _date_window(holdout_samples),
        "metrics": {
            "baseline_train_brier": current_metrics["train_brier"],
            "candidate_train_brier": candidate_metrics["train_brier"],
            "baseline_holdout_brier": current_metrics["holdout_brier"],
            "candidate_holdout_brier": candidate_metrics["holdout_brier"],
        },
        "gates": gates,
        "decision_reason": decision_reason,
        "candidate_update": candidate_update,
    }


def _fit_candidate_trust_score(
    samples: list[TemperatureTrainingSample],
    current_trust_score: float,
    policy: TemperatureTrainingPolicy,
) -> float:
    if not samples:
        return current_trust_score

    min_score = max(0.0, current_trust_score - policy.max_trust_score_delta_per_run)
    max_score = min(100.0, current_trust_score + policy.max_trust_score_delta_per_run)
    candidate_scores = _candidate_score_range(min_score, max_score, policy.trust_score_step)
    scored = [
        (
            _brier_loss(samples, score),
            abs(score - current_trust_score),
            score,
        )
        for score in candidate_scores
    ]
    _, _, best_score = min(scored)
    return round(best_score, 3)


def _candidate_score_range(min_score: float, max_score: float, step: float) -> list[float]:
    if step <= 0:
        raise ValueError("trust_score_step must be positive")

    steps = int(math.floor((max_score - min_score) / step))
    values = [round(min_score + (index * step), 3) for index in range(steps + 1)]
    if not values or values[-1] != round(max_score, 3):
        values.append(round(max_score, 3))
    return sorted(set(values))


def _score_structural_split(
    train_examples: list[WeatherTrainingExample],
    holdout_examples: list[WeatherTrainingExample],
) -> dict[str, float | str | bool]:
    structural_probability = _empirical_probability(train_examples)
    neutral_probability = 0.5
    holdout_brier = _brier_from_probability(holdout_examples, structural_probability)
    neutral_holdout_brier = _brier_from_probability(holdout_examples, neutral_probability)
    usefulness = round(neutral_holdout_brier - holdout_brier, 6)
    usefulness_label = "positive" if usefulness > 0 else "negative" if usefulness < 0 else "neutral"

    return {
        "structural_probability": structural_probability,
        "train_direction_accuracy": _accuracy_from_probability(train_examples, structural_probability),
        "holdout_direction_accuracy": _accuracy_from_probability(holdout_examples, structural_probability),
        "train_brier": _brier_from_probability(train_examples, structural_probability),
        "holdout_brier": holdout_brier,
        "train_calibration_error": _calibration_error(train_examples, structural_probability),
        "holdout_calibration_error": _calibration_error(holdout_examples, structural_probability),
        "neutral_holdout_brier": neutral_holdout_brier,
        "holdout_outcome_base_rate": _empirical_probability(holdout_examples),
        "source_usefulness_score": usefulness,
        "source_usefulness_label": usefulness_label,
        "source_useful": usefulness >= 0,
    }


def _score_price_aware_split(
    train_samples: list[TemperatureTrainingSample],
    holdout_samples: list[TemperatureTrainingSample],
    trust_score: float,
) -> dict[str, float]:
    return {
        "train_brier": _brier_loss(train_samples, trust_score),
        "holdout_brier": _brier_loss(holdout_samples, trust_score),
    }


def _brier_loss(samples: list[TemperatureTrainingSample], trust_score: float) -> float:
    if not samples:
        return 0.0
    errors = []
    for sample in samples:
        predicted_yes = _trust_weighted_probability(sample.yes_price, trust_score)
        errors.append((predicted_yes - sample.outcome) ** 2)
    return round(sum(errors) / len(errors), 6)


def _trust_weighted_probability(yes_price: float, trust_score: float) -> float:
    centered_price = min(max(float(yes_price), 0.0), 1.0) - 0.5
    shrink = min(max(float(trust_score) / 100.0, 0.0), 1.0)
    return min(max(0.5 + (centered_price * shrink), 0.0), 1.0)


def _empirical_probability(examples: list[WeatherTrainingExample]) -> float:
    if not examples:
        return 0.5
    return round(sum(example.outcome for example in examples) / len(examples), 6)


def _accuracy_from_probability(examples: list[WeatherTrainingExample], probability: float) -> float:
    if not examples:
        return 0.0
    predicted = 1.0 if probability >= 0.5 else 0.0
    correct = sum(1 for example in examples if example.outcome == predicted)
    return round(correct / len(examples), 6)


def _brier_from_probability(examples: list[WeatherTrainingExample], probability: float) -> float:
    if not examples:
        return 0.0
    errors = [(probability - example.outcome) ** 2 for example in examples]
    return round(sum(errors) / len(errors), 6)


def _calibration_error(examples: list[WeatherTrainingExample], probability: float) -> float:
    if not examples:
        return 0.0
    errors = [abs(probability - example.outcome) for example in examples]
    return round(sum(errors) / len(errors), 6)


def _holdout_days(unique_days: list[str], holdout_fraction: float) -> set[str]:
    if not unique_days:
        return set()
    holdout_count = max(1, int(math.ceil(len(unique_days) * holdout_fraction)))
    holdout_count = min(holdout_count, max(1, len(unique_days) - 1))
    return set(unique_days[-holdout_count:])


def _date_window(samples: list[TemperatureTrainingSample] | list[WeatherTrainingExample]) -> dict[str, str | None]:
    if not samples:
        return {"start": None, "end": None}
    dates = [sample.event_date for sample in samples]
    return {"start": min(dates), "end": max(dates)}


def _lookup_source(registry: WeatherRegistry, source_id: str) -> dict:
    for source in registry.as_dict().get("sources", []):
        if isinstance(source, dict) and source.get("source_id") == source_id:
            return source
    raise KeyError(f"unknown source_id '{source_id}'")


def _event_date(observed_at: object, resolved_at: object) -> str | None:
    for value in (observed_at, resolved_at):
        parsed = _parse_timestamp(value)
        if parsed is not None:
            return parsed.date().isoformat()
    return None


def _lead_time_hours(observed_at: object, resolved_at: object) -> float | None:
    observed = _parse_timestamp(observed_at)
    resolved = _parse_timestamp(resolved_at)
    if observed is None or resolved is None:
        return None
    delta_hours = (resolved - observed).total_seconds() / 3600.0
    if delta_hours < 0:
        return None
    return round(delta_hours, 3)


def _build_structural_evidence(metadata: dict[str, Any]) -> dict[str, Any]:
    evidence = {
        key: metadata[key]
        for key in ("market_subtitle", "yes_subtitle", "no_subtitle")
        if key in metadata and metadata[key] is not None
    }
    return evidence


def _parse_timestamp(value: object) -> datetime | None:
    rendered = _string_or_none(value)
    if not rendered:
        return None
    normalized = rendered.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _price_or_none(value: object) -> float | None:
    numeric = _float_or_none(value)
    if numeric is None:
        return None
    return min(max(numeric, 0.0), 1.0)


def _float_or_none(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None
