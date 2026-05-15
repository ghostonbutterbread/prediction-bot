#!/usr/bin/env python3
"""Export compact Prediction Lab shadow-delta review rows."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.prediction_lab_shadow_delta import write_shadow_delta_compact_review_jsonl  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", required=True, help="Prediction Lab predictions.jsonl path")
    parser.add_argument("--market-snapshots", required=True, help="Prediction Lab market_snapshots.jsonl path")
    parser.add_argument("--output", required=True, help="Compact review JSONL output path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary = write_shadow_delta_compact_review_jsonl(
            args.output,
            predictions_path=args.predictions,
            market_snapshots_path=args.market_snapshots,
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
