#!/usr/bin/env python3
"""Run the non-mutating Collector Lane Replay Kelly diagnostic.

This is intentionally distinct from ``collector_lane_replay.py``: it applies
chronological Kelly sizing and a synthetic reserved-capital wallet to recorded
collector-lane decisions.  It does not re-execute strategy logic and is not a
paper-wallet parity or promotion result.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.collector_lane_replay_kelly import write_collector_lane_replay_kelly


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decision-path", required=True, help="derived collector-lane decision JSONL")
    parser.add_argument("--resolution-path", action="append", default=[], help="independent resolution JSONL; may be repeated")
    parser.add_argument("--output-dir", required=True, help="new derived Kelly replay output directory")
    parser.add_argument("--starting-balance-usd", type=float, default=100.0)
    parser.add_argument("--kelly-fraction", type=float, default=0.5)
    parser.add_argument("--max-bet-pct", type=float, default=0.10)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = write_collector_lane_replay_kelly(
        decision_path=_root_path(args.decision_path),
        resolution_paths=[_root_path(path) for path in args.resolution_path],
        output_dir=_safe_output_dir(_root_path(args.output_dir)),
        starting_balance_usd=args.starting_balance_usd,
        kelly_fraction=args.kelly_fraction,
        max_bet_pct=args.max_bet_pct,
    )
    if args.format == "json":
        print(json.dumps(result["summary"], indent=2, sort_keys=True))
    else:
        summary = result["summary"]
        print(
            "methodology={methodology} opened={opened_positions} settled={settled_positions} "
            "open={open_positions} final_balance_usd={final_balance_usd}".format(**summary)
        )
        print(f"output_dir={result['summary_path'].parent}")
    return 0


def _root_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _safe_output_dir(path: Path) -> Path:
    resolved = path.resolve()
    allowed_roots = ((ROOT / "data" / "derived_reports").resolve(), (ROOT / "data" / "summaries").resolve())
    if not any(resolved == root or root in resolved.parents for root in allowed_roots):
        raise ValueError("output_dir must be under data/derived_reports or data/summaries")
    return resolved


if __name__ == "__main__":
    raise SystemExit(main())
