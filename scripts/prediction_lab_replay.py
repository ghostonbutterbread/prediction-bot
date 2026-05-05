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
from bot.prediction_lab_replay import build_replay_series_grid, replay_from_paths, validate_prediction_lab_tables


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay recorded Prediction Lab artifacts")
    parser.add_argument("inputs", nargs="+", help="Prediction Lab predictions.jsonl or market_snapshots.jsonl files")
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
        "--resolution-input",
        action="append",
        default=[],
        help="Optional Prediction Lab resolutions.jsonl path; joined after replay for scoring only",
    )
    parser.add_argument("--validate-only", action="store_true", help="Validate Prediction Lab input/resolution table quality and exit")
    parser.add_argument("--validation-output", default=None, help="Optional JSON path for validation result")
    parser.add_argument("--fail-on-validation-errors", action="store_true", help="Exit nonzero if validation reports errors")
    args = parser.parse_args()

    config = load_config(Path(args.config))
    if args.validate_only:
        validation = validate_prediction_lab_tables(args.inputs, resolution_paths=args.resolution_input)
        payload = validation.to_dict()
        if args.validation_output:
            atomic_write_json(Path(args.validation_output), payload)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 2 if args.fail_on_validation_errors and not validation.ok else 0

    result = replay_from_paths(
        args.inputs,
        config=config,
        limit=args.limit,
        bankroll_usd=args.bankroll_usd,
        live_source_policy=args.live_source_policy,
        require_recorded_source=args.require_recorded_source,
        row_quality_policy=args.row_quality_policy,
        resolution_paths=args.resolution_input,
    )

    if args.output:
        rewrite_jsonl(Path(args.output), [row.to_dict() for row in result.rows])
    if args.summary_output:
        atomic_write_json(Path(args.summary_output), result.summary)
    if args.grid_output:
        atomic_write_json(Path(args.grid_output), build_replay_series_grid(result))

    print(json.dumps(result.summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
