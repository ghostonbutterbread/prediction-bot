#!/usr/bin/env python3
"""Materialize paper-only source observations from sealed replay inputs."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.weather.source_observation_ledger import materialize_source_observation_ledger


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay-inputs", required=True, help="sanitized replay_decision_inputs.jsonl")
    parser.add_argument("--finalized-outcomes", required=True, help="exact finalized_replay_outcomes.jsonl from the binder")
    parser.add_argument("--output-dir", required=True, help="new/empty derived artifact directory")
    args = parser.parse_args()
    result = materialize_source_observation_ledger(
        replay_inputs_path=args.replay_inputs, finalized_outcomes_path=args.finalized_outcomes, output_dir=args.output_dir,
    )
    counts = result.metadata["counts"]
    print(
        f"pending={counts['pending']} settled={counts['settled']} "
        f"unsettled_or_unusable={counts['unsettled_or_unusable']} output={result.output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
