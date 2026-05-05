#!/usr/bin/env python3
"""Inventory and derive safe upgraded Prediction Lab backfill ledgers."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.prediction_lab_backfill import (  # noqa: E402
    DEFAULT_ANALYSIS_DIR,
    DEFAULT_PREDICTION_LAB_DIR,
    run_prediction_lab_backfill,
    run_prediction_lab_canonical_analysis,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safely inventory/backfill Prediction Lab replay rows")
    parser.add_argument("--input", help="Prediction Lab market_snapshots.jsonl or predictions.jsonl input")
    parser.add_argument("--output-dir", help="Directory for derived backfill outputs")
    parser.add_argument("--limit", type=int, default=None, help="Maximum input rows to process")
    parser.add_argument("--tail", action="store_true", help="Use the latest valid input rows instead of the first rows; scans inputs")
    parser.add_argument("--inventory-only", action="store_true", help="Only write backfill_report.json")
    parser.add_argument("--artifact-recovery", action="store_true", help="Recover fields already present in row artifacts")
    parser.add_argument("--resolutions", action="append", default=None, help="Optional resolutions.jsonl path for inventory join coverage")
    parser.add_argument("--canonical-analysis", action="store_true", help="Build stable analysis outputs under --analysis-dir")
    parser.add_argument("--analysis-dir", default=str(DEFAULT_ANALYSIS_DIR), help="Canonical analysis output directory")
    parser.add_argument("--prediction-lab-dir", default=str(DEFAULT_PREDICTION_LAB_DIR), help="Directory containing raw Prediction Lab ledgers")
    parser.add_argument("--market-snapshots", default=None, help="Override raw market_snapshots.jsonl path for canonical analysis")
    parser.add_argument("--predictions", default=None, help="Override raw predictions.jsonl path for canonical analysis")
    parser.add_argument("--include-predictions", action="store_true", help="Include predictions.jsonl in canonical analysis input")
    parser.add_argument("--validate-output", dest="validate_output", action="store_true", help="Run validator and write validation_report.json")
    parser.add_argument("--no-validate-output", dest="validate_output", action="store_false", help="Write validation_report.json with validation marked skipped")
    parser.set_defaults(validate_output=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.canonical_analysis:
        result = run_prediction_lab_canonical_analysis(
            prediction_lab_dir=args.prediction_lab_dir,
            analysis_dir=args.analysis_dir,
            market_snapshots_path=args.market_snapshots,
            predictions_path=args.predictions,
            include_predictions=args.include_predictions,
            limit=args.limit,
            tail=args.tail,
            validate_output=True if args.validate_output is None else args.validate_output,
            resolution_paths=args.resolutions,
        )
    else:
        if not args.input or not args.output_dir:
            raise SystemExit("--input and --output-dir are required unless --canonical-analysis is set")
        result = run_prediction_lab_backfill(
            args.input,
            args.output_dir,
            limit=args.limit,
            tail=args.tail,
            inventory_only=args.inventory_only,
            artifact_recovery=args.artifact_recovery,
            resolution_paths=args.resolutions,
        )
    print(json.dumps(result.report, indent=2, sort_keys=True))
    if result.validation and not result.validation.get("skipped", False) and not result.validation.get("ok", False):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
