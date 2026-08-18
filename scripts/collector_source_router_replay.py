#!/usr/bin/env python3
"""Run the offline source-only control/source-router replay slice."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.weather.collector_source_router_replay import run_collector_source_router_replay


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay-inputs", required=True, help="sanitized replay_decision_inputs.jsonl")
    parser.add_argument("--finalized-outcomes", required=True, help="separate explicit finalized-outcome JSONL")
    parser.add_argument("--output-dir", required=True, help="new/empty directory beneath data/derived_reports")
    parser.add_argument("--min-sample-count", type=int, default=5)
    parser.add_argument("--max-rows", type=int, default=None)
    parser.add_argument("--history-manifest", default=None, help="hash-verified source_history_manifest JSON for selector-only history")
    parser.add_argument("--history-ledger", default=None, help="must match the source ledger declared by --history-manifest")
    parser.add_argument("--cohort-per-shape-target", type=int, default=30, help="outcome-blind event cohort target per contract shape")
    args = parser.parse_args()
    result = run_collector_source_router_replay(
        replay_inputs_path=args.replay_inputs, finalized_outcomes_path=args.finalized_outcomes,
        output_dir=args.output_dir, min_sample_count=args.min_sample_count, max_rows=args.max_rows,
        history_manifest_path=args.history_manifest, history_ledger_path=args.history_ledger,
        cohort_per_shape_target=args.cohort_per_shape_target,
    )
    summary = result.metadata["resolution_summary"]
    print(
        f"control={result.metadata['control_lane_id']} candidate={result.metadata['candidate_lane_id']} "
        f"sealed={result.metadata['decision_stats']['sealed_control_decisions']} "
        f"resolved={summary['exactly_resolved_decisions']} cohort={result.cohort_report_path.name} output={result.output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
