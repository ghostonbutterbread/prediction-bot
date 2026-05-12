#!/usr/bin/env python3
"""Build read-only agent decision sidecars from legacy Prediction Lab JSONL rows."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.agent_decision_backfill import backfill_legacy_agent_decisions


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill unified agent decision sidecars from legacy Prediction Lab rows")
    parser.add_argument("inputs", nargs="+", help="Prediction Lab market_snapshots.jsonl or prediction-like JSONL inputs")
    parser.add_argument("--output-dir", required=True, help="Explicit directory for agent_runs.jsonl and agent_decisions.jsonl")
    args = parser.parse_args()

    result = backfill_legacy_agent_decisions(args.inputs, output_dir=args.output_dir)
    print(json.dumps(result.report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
