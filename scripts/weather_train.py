#!/usr/bin/env python3
"""Run bounded weather trust training passes over historical temperature markets."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.weather import WeatherRegistry  # noqa: E402
from bot.weather.analysis import load_historical_csv_samples  # noqa: E402
from bot.weather.training import (  # noqa: E402
    StructuralTrainingPolicy,
    TemperatureTrainingPolicy,
    apply_price_aware_training_updates,
    run_price_aware_training,
    run_structural_training,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        default="data/historical/kalshi.csv",
        help="Historical Kalshi CSV to train against.",
    )
    parser.add_argument(
        "--registry",
        default="docs/weather/city_registry_starter.json",
        help="Weather registry JSON used for current trust scores and source mapping.",
    )
    parser.add_argument(
        "--mode",
        default="price-aware",
        choices=("price-aware", "structural"),
        help="Training mode: price-aware trading learning or structural no-price learning.",
    )
    parser.add_argument(
        "--runtime",
        default="codex-cli",
        choices=("codex-cli", "openrouter", "manual"),
        help="Intended evaluator runtime for downstream AI-assisted replay/training flows.",
    )
    parser.add_argument(
        "--model",
        default="gpt-5.4",
        help="Intended evaluator model identifier for downstream AI-assisted replay/training flows.",
    )
    parser.add_argument(
        "--codex-command",
        default="codex exec -m gpt-5.4",
        help="Preferred Codex CLI invocation for downstream evaluator runs when runtime=codex-cli.",
    )
    parser.add_argument(
        "--summary-output",
        default="",
        help="Optional JSON path for the compact dry-run summary.",
    )
    parser.add_argument(
        "--candidate-output",
        default="",
        help="Optional JSON path for candidate trust-score updates.",
    )
    parser.add_argument(
        "--apply-updates",
        action="store_true",
        help="Apply emitted candidate trust-score updates back into the registry and save it.",
    )
    parser.add_argument(
        "--max-records",
        type=int,
        default=0,
        help="Optional cap on total historical records processed across all batches (0 = no cap).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=0,
        help="Optional number of records per bounded training batch (0 = process all selected records in one batch).",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=0,
        help="Optional cap on the number of batches to execute after filtering/truncation (0 = no cap).",
    )
    parser.add_argument(
        "--min-samples-per-city-source",
        type=int,
        default=12,
        help="Minimum resolved temperature samples required per city/source.",
    )
    parser.add_argument(
        "--min-unique-days",
        type=int,
        default=4,
        help="Minimum distinct market dates required per city/source.",
    )
    parser.add_argument(
        "--holdout-fraction",
        type=float,
        default=0.25,
        help="Fraction of dates reserved for holdout scoring.",
    )
    parser.add_argument(
        "--max-trust-score-delta-per-run",
        type=float,
        default=5.0,
        help="Absolute cap on candidate trust-score movement per run.",
    )
    parser.add_argument(
        "--min-trust-score-delta-to-emit",
        type=float,
        default=1.0,
        help="Minimum trust-score movement required before emitting a candidate update.",
    )
    parser.add_argument(
        "--trust-score-step",
        type=float,
        default=1.0,
        help="Grid-search step size used when fitting dry-run trust scores.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    input_path = _resolve_path(args.input)
    registry_path = _resolve_path(args.registry)
    summary_output_path = _resolve_optional_path(args.summary_output)
    candidate_output_path = _resolve_optional_path(args.candidate_output)

    registry = WeatherRegistry.from_file(registry_path)
    all_records = load_historical_csv_samples(input_path, one_per_series=False)
    selected_records = _select_records(all_records, max_records=args.max_records)
    if not selected_records:
        raise SystemExit("no historical weather records matched the requested limits")

    batch_size = _normalize_positive_int(args.batch_size)
    batches = _build_batches(selected_records, batch_size=batch_size, max_batches=args.max_batches)

    evaluator = {
        "runtime": args.runtime,
        "model": args.model,
        "codex_command": args.codex_command if args.runtime == "codex-cli" else None,
    }

    if args.mode == "structural":
        policy = StructuralTrainingPolicy(
            min_samples_per_city_source=args.min_samples_per_city_source,
            min_unique_days=args.min_unique_days,
            holdout_fraction=args.holdout_fraction,
        )
        report = _run_batched_training(
            batches,
            registry=registry,
            mode=args.mode,
            policy=policy,
            apply_updates=args.apply_updates,
            registry_path=registry_path,
            runner=lambda batch_records, active_registry, active_policy: run_structural_training(
                batch_records,
                registry=active_registry,
                policy=active_policy,
            ),
        )
    else:
        policy = TemperatureTrainingPolicy(
            min_samples_per_city_source=args.min_samples_per_city_source,
            min_unique_days=args.min_unique_days,
            holdout_fraction=args.holdout_fraction,
            max_trust_score_delta_per_run=args.max_trust_score_delta_per_run,
            min_trust_score_delta_to_emit=args.min_trust_score_delta_to_emit,
            trust_score_step=args.trust_score_step,
        )
        report = _run_batched_training(
            batches,
            registry=registry,
            mode=args.mode,
            policy=policy,
            apply_updates=args.apply_updates,
            registry_path=registry_path,
            runner=lambda batch_records, active_registry, active_policy: run_price_aware_training(
                batch_records,
                registry=active_registry,
                policy=active_policy,
            ),
        )
    report["summary"]["evaluator"] = evaluator

    if summary_output_path:
        _write_json(summary_output_path, report["summary"])
    if candidate_output_path:
        _write_json(
            candidate_output_path,
            {
                "summary": report["summary"],
                "candidate_updates": report["candidate_updates"],
                "applied_updates": report.get("applied_updates", []),
                "batch_reports": report.get("batch_reports", []),
                "evaluator": evaluator,
            },
        )

    _print_summary(report, input_path, summary_output_path, candidate_output_path)
    return 0


def _run_batched_training(
    batches: list[list],
    *,
    registry: WeatherRegistry,
    mode: str,
    policy,
    apply_updates: bool,
    registry_path: Path,
    runner: Callable[[list, WeatherRegistry, object], dict],
) -> dict:
    if apply_updates and mode != "price-aware":
        raise SystemExit("--apply-updates is only supported in --mode price-aware")

    batch_reports: list[dict] = []
    candidate_updates: list[dict] = []
    applied_updates: list[dict] = []

    for index, batch_records in enumerate(batches, start=1):
        batch_report = runner(batch_records, registry, policy)
        batch_report["summary"]["batch"] = {
            "index": index,
            "records_in_batch": len(batch_records),
        }
        if apply_updates:
            batch_report = apply_price_aware_training_updates(
                batch_report,
                registry=registry,
                reviewed_at=batch_report["summary"].get("generated_at"),
                save=True,
                save_path=str(registry_path),
            )
        batch_reports.append(batch_report)
        candidate_updates.extend(batch_report.get("candidate_updates", []))
        applied_updates.extend(batch_report.get("applied_updates", []))

    return _aggregate_batch_reports(
        batch_reports,
        total_records=sum(len(batch) for batch in batches),
        batch_size=max(len(batch) for batch in batches) if batches else 0,
        max_batches=len(batches),
        apply_updates=apply_updates,
        mode=mode,
        policy=policy,
        registry=registry,
        candidate_updates=candidate_updates,
        applied_updates=applied_updates,
    )


def _aggregate_batch_reports(
    batch_reports: list[dict],
    *,
    total_records: int,
    batch_size: int,
    max_batches: int,
    apply_updates: bool,
    mode: str,
    policy,
    registry: WeatherRegistry,
    candidate_updates: list[dict],
    applied_updates: list[dict],
) -> dict:
    if not batch_reports:
        raise ValueError("at least one batch report is required")

    generated_at = batch_reports[-1]["summary"].get("generated_at")
    totals: dict[str, int] = {}
    for batch_report in batch_reports:
        for key, value in batch_report["summary"].get("records", {}).items():
            if isinstance(value, int):
                totals[key] = totals.get(key, 0) + value

    totals["input_records"] = total_records
    totals["candidate_updates"] = len(candidate_updates)
    totals["applied_updates"] = len(applied_updates)
    totals["batches_executed"] = len(batch_reports)

    dry_run = not apply_updates
    registry_mutated = bool(apply_updates and applied_updates)

    summary = {
        "generated_at": generated_at,
        "training_mode": mode,
        "dry_run": dry_run,
        "registry_mutated": registry_mutated,
        "registry_saved": registry_mutated,
        "policy": policy.as_dict(),
        "records": totals,
        "batching": {
            "enabled": len(batch_reports) > 1,
            "batches_executed": len(batch_reports),
            "batch_size": batch_size,
            "total_records_selected": total_records,
            "max_batches": max_batches,
        },
        "registry_source_count": len(registry.as_dict().get("sources", [])),
    }

    group_reports: list[dict] = []
    for batch_report in batch_reports:
        group_reports.extend(batch_report.get("group_reports", []))

    return {
        "summary": summary,
        "candidate_updates": candidate_updates,
        "applied_updates": applied_updates,
        "group_reports": group_reports,
        "batch_reports": [batch_report["summary"] for batch_report in batch_reports],
    }


def _select_records(records: list, *, max_records: int) -> list:
    limit = _normalize_positive_int(max_records)
    if limit is None:
        return list(records)
    return list(records[:limit])


def _build_batches(records: list, *, batch_size: int | None, max_batches: int) -> list[list]:
    if not records:
        return []
    normalized_max_batches = _normalize_positive_int(max_batches)
    effective_batch_size = len(records) if batch_size is None else batch_size
    batches = [records[index:index + effective_batch_size] for index in range(0, len(records), effective_batch_size)]
    if normalized_max_batches is not None:
        batches = batches[:normalized_max_batches]
    return batches


def _normalize_positive_int(value: int) -> int | None:
    if value is None or value <= 0:
        return None
    return int(value)


def _print_summary(report: dict, input_path: Path, summary_output_path: Path | None, candidate_output_path: Path | None) -> None:
    summary = report["summary"]
    counts = summary["records"]
    sample_count = counts.get("temperature_samples", counts.get("training_examples", 0))
    mode_label = "Applied weather training" if not summary.get("dry_run", True) else "Dry-run weather training"
    print(
        f"{mode_label} "
        f"input={_display_path(input_path)} "
        f"input_records={counts.get('input_records', sample_count)} "
        f"samples={sample_count} "
        f"groups={counts.get('groups_evaluated', counts.get('groups_scored', 0))} "
        f"candidates={counts['candidate_updates']} "
        f"blocked={counts['blocked_groups']}"
    )
    policy_line = (
        "Policy "
        f"min_samples={summary['policy']['min_samples_per_city_source']} "
        f"min_days={summary['policy']['min_unique_days']} "
        f"holdout_fraction={summary['policy']['holdout_fraction']}"
    )
    if "max_trust_score_delta_per_run" in summary["policy"]:
        policy_line += f" max_delta={summary['policy']['max_trust_score_delta_per_run']}"
    print(policy_line)
    batching = summary.get("batching", {})
    if batching:
        print(
            "Batches "
            f"executed={batching.get('batches_executed')} "
            f"size={batching.get('batch_size')} "
            f"selected_records={batching.get('total_records_selected')}"
        )
    evaluator = summary.get("evaluator", {})
    if evaluator:
        print(
            "Evaluator "
            f"runtime={evaluator.get('runtime')} "
            f"model={evaluator.get('model')}"
        )
    if counts.get("skipped_missing_yes_price"):
        print(f"Filtered missing_yes_price={counts['skipped_missing_yes_price']}")
    if report["candidate_updates"]:
        for candidate in report["candidate_updates"]:
            print(
                "Candidate "
                f"city={candidate['city_id']} "
                f"source={candidate['source_id']} "
                f"trust={candidate['current_trust_score']}->{candidate['candidate_trust_score']} "
                f"holdout_brier={candidate['metrics']['baseline_holdout_brier']}->{candidate['metrics']['candidate_holdout_brier']}"
            )
    else:
        print("Candidate none")

    if summary_output_path:
        print(f"Summary JSON {_display_path(summary_output_path)}")
    if candidate_output_path:
        print(f"Candidate JSON {_display_path(candidate_output_path)}")


def _resolve_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def _resolve_optional_path(value: str) -> Path | None:
    if not value:
        return None
    return _resolve_path(value)


def _display_path(path: Path) -> str:
    if path.is_relative_to(PROJECT_ROOT):
        return str(path.relative_to(PROJECT_ROOT))
    return str(path)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
