#!/usr/bin/env python3
"""Derive an outcome-blind independent strict Source Router history artifact."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.weather.source_history_manifest import materialize_strict_source_history_collapse


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settled-source-ledger", required=True, help="settled_source_correctness.jsonl from strict materialization")
    parser.add_argument("--output-dir", required=True, help="new or empty derived artifact directory")
    args = parser.parse_args()
    result = materialize_strict_source_history_collapse(args.settled_source_ledger, args.output_dir)
    counts = result.metadata["counts"]
    print(
        f"strict_seen={counts['strict_rows_seen']} independent={counts['independent_rows']} "
        f"collapsed={counts['collapsed_repeat_polls']} output={result.output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
