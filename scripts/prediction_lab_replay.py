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
from bot.prediction_lab_replay import replay_from_paths


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
    parser.add_argument("--output", default=None, help="Optional JSONL path for comparison rows")
    parser.add_argument("--summary-output", default=None, help="Optional JSON path for summary")
    args = parser.parse_args()

    config = load_config(Path(args.config))
    result = replay_from_paths(
        args.inputs,
        config=config,
        limit=args.limit,
        bankroll_usd=args.bankroll_usd,
        live_source_policy=args.live_source_policy,
        require_recorded_source=args.require_recorded_source,
    )

    if args.output:
        rewrite_jsonl(Path(args.output), [row.to_dict() for row in result.rows])
    if args.summary_output:
        atomic_write_json(Path(args.summary_output), result.summary)

    print(json.dumps(result.summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
