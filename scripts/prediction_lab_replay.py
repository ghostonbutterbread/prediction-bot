#!/usr/bin/env python3
"""Replay Prediction Lab collector artifacts and compare decisions."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import load_config
from bot.file_ops import atomic_write_json, rewrite_jsonl
from bot.prediction_lab_replay import (
    build_replay_series_grid,
    explicit_prediction_lab_replay_window,
    replay_from_paths,
    select_prediction_lab_replay_window,
    validate_prediction_lab_tables,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay recorded Prediction Lab artifacts")
    parser.add_argument("inputs", nargs="*", help="Prediction Lab predictions.jsonl or market_snapshots.jsonl files")
    parser.add_argument("--input", action="append", default=[], help="Explicit Prediction Lab input path; may be repeated")
    parser.add_argument("--months", default=None, help="Select newest N available monthly datasets, or 'all'. Defaults to prediction_lab.replay_default_months when no explicit input is provided.")
    parser.add_argument(
        "--data-root",
        action="append",
        default=[],
        help="Dataset discovery root for --months; may be repeated. Defaults to config data_dir.",
    )
    parser.add_argument("--config", default="config.yaml", help="Replay config path")
    parser.add_argument("--limit", type=int, default=None, help="Maximum artifact rows to replay")
    parser.add_argument("--bankroll-usd", type=float, default=100.0, help="Fixed replay opportunity bankroll")
    parser.add_argument(
        "--live-source-policy",
        choices=["fail", "warn_skip", "allow"],
        default="fail",
        help="How to handle attempted current live source calls during historical replay",
    )
    parser.add_argument(
        "--require-recorded-source",
        action="store_true",
        help="Fail unless each artifact has recorded_as_of source data",
    )
    parser.add_argument(
        "--row-quality-policy",
        choices=["annotate", "include_all", "strict", "drop_incomplete", "strict_only"],
        default="annotate",
        help="Annotate all rows or return only strict replay-grade rows",
    )
    parser.add_argument("--output", default=None, help="Optional JSONL path for comparison rows")
    parser.add_argument("--summary-output", default=None, help="Optional JSON path for summary")
    parser.add_argument("--grid-output", default=None, help="Optional JSON path for replay series coverage grid")
    parser.add_argument(
        "--source-reliability-scoreboard",
        default=None,
        help="Optional source_scoreboard.json or source_scoreboard_by_slice.jsonl for shadow-only reliability evaluation",
    )
    parser.add_argument(
        "--source-reliability-ledger",
        default=None,
        help="Optional source-outcome ledger JSONL/JSON for rolling as-of shadow-only reliability evaluation",
    )
    parser.add_argument(
        "--source-reliability-shadow-output",
        default=None,
        help="Optional JSONL path for compact source reliability shadow rows",
    )
    parser.add_argument(
        "--resolution-input",
        action="append",
        default=[],
        help="Optional Prediction Lab resolutions.jsonl path; joined after replay for scoring only",
    )
    parser.add_argument(
        "--decision-input",
        action="append",
        default=[],
        help="Optional agent_decisions.jsonl path; joined by shared_candidate_id for coverage summaries",
    )
    parser.add_argument("--validate-only", action="store_true", help="Validate Prediction Lab input/resolution table quality and exit")
    parser.add_argument("--validation-output", default=None, help="Optional JSON path for validation result")
    parser.add_argument("--fail-on-validation-errors", action="store_true", help="Exit nonzero if validation reports errors")
    args = parser.parse_args()
    if args.source_reliability_scoreboard and args.source_reliability_ledger:
        parser.error("provide either --source-reliability-scoreboard or --source-reliability-ledger, not both")

    config = load_config(Path(args.config))
    explicit_inputs = [*args.inputs, *args.input]
    replay_window = None
    replay_inputs = explicit_inputs
    requested_months = args.months
    if requested_months is None and not explicit_inputs:
        requested_months = _default_replay_months(config)

    if requested_months is not None:
        discovery_roots = args.data_root or explicit_inputs or _default_replay_roots(config)
        try:
            replay_window = select_prediction_lab_replay_window(discovery_roots, months=requested_months)
        except ValueError as exc:
            parser.error(str(exc))
        replay_inputs = list(replay_window.datasets)
    elif explicit_inputs:
        replay_window = explicit_prediction_lab_replay_window(explicit_inputs)
    else:
        parser.error("provide one or more inputs, --input, or --months N|all")

    if args.validate_only:
        validation = validate_prediction_lab_tables(replay_inputs, resolution_paths=args.resolution_input)
        payload = validation.to_dict()
        if replay_window is not None:
            payload["replay_window"] = replay_window.to_dict()
        if args.validation_output:
            atomic_write_json(Path(args.validation_output), payload)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 2 if args.fail_on_validation_errors and not validation.ok else 0

    result = replay_from_paths(
        replay_inputs,
        config=config,
        limit=args.limit,
        bankroll_usd=args.bankroll_usd,
        live_source_policy=args.live_source_policy,
        require_recorded_source=args.require_recorded_source,
        row_quality_policy=args.row_quality_policy,
        resolution_paths=args.resolution_input,
        decision_paths=args.decision_input,
        replay_window=replay_window,
        source_reliability_scoreboard=args.source_reliability_scoreboard,
        source_reliability_ledger=args.source_reliability_ledger,
    )

    if args.output:
        rewrite_jsonl(Path(args.output), [row.to_dict() for row in result.rows])
    if args.summary_output:
        atomic_write_json(Path(args.summary_output), result.summary)
    if args.grid_output:
        atomic_write_json(Path(args.grid_output), build_replay_series_grid(result))
    if args.source_reliability_shadow_output:
        rewrite_jsonl(Path(args.source_reliability_shadow_output), result.source_reliability_shadow_rows)

    print(json.dumps(result.summary, indent=2, sort_keys=True))
    return 0


def _default_replay_months(config: dict) -> int | str:
    lab_cfg = config.get("prediction_lab", {}) if isinstance(config.get("prediction_lab"), dict) else {}
    configured = (
        lab_cfg.get("replay_default_months")
        or lab_cfg.get("monthly_audit_months")
        or lab_cfg.get("default_audit_months")
    )
    return configured if configured not in (None, "") else 2


def _default_replay_roots(config: dict) -> list[Path]:
    lab_cfg = config.get("prediction_lab", {}) if isinstance(config.get("prediction_lab"), dict) else {}
    configured = lab_cfg.get("replay_dataset_roots") or lab_cfg.get("replay_data_roots")
    if isinstance(configured, list) and configured:
        return [Path(value) for value in configured]
    if isinstance(configured, str) and configured:
        return [Path(configured)]

    runtime_cfg = config.get("runtime", {}) if isinstance(config.get("runtime"), dict) else {}
    base_dir = Path(runtime_cfg.get("base_dir") or config.get("data_dir", "data"))
    lab_root = base_dir / "paper" / "prediction_lab"
    monthly_root = lab_root / "monthly" / "market_snapshots"
    if monthly_root.exists():
        return [monthly_root]
    return [lab_root / "market_snapshots.jsonl"]


if __name__ == "__main__":
    raise SystemExit(main())
