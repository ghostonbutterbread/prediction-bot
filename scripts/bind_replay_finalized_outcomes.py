#!/usr/bin/env python3
"""Bind sanitized replay inputs to separate authoritative strict outcomes."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.replay_outcome_binding import bind_replay_finalized_outcomes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay-inputs", required=True, help="sanitized replay_decision_inputs.jsonl")
    parser.add_argument("--strict-resolutions", required=True, help="authoritative strict-resolution JSONL")
    parser.add_argument("--output-dir", required=True, help="new/empty directory beneath data/derived_reports")
    args = parser.parse_args()
    result = bind_replay_finalized_outcomes(
        replay_inputs_path=args.replay_inputs, strict_resolutions_path=args.strict_resolutions, output_dir=args.output_dir,
    )
    counts = result.metadata["binding_counts"]
    print(
        f"candidate={counts['candidate']} bound={counts['bound']} unbound={counts['unbound']} "
        f"ambiguous={counts['ambiguous']} invalid={counts['invalid']} output={result.output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
