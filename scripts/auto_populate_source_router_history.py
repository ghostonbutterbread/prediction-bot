#!/usr/bin/env python3
"""Auto populate Source Router history from finalized collector evidence."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.auto_source_router_promotion import auto_populate_source_router_history


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collector-snapshots", required=True, help="immutable beta collector JSONL snapshots to read")
    parser.add_argument("--strict-resolutions", required=True, help="independently finalized authoritative strict-resolution JSONL")
    parser.add_argument(
        "--output-root",
        help="collector-derived output root; defaults to the canonical collector Source Router history root",
    )
    args = parser.parse_args()
    try:
        result = auto_populate_source_router_history(
            collector_snapshots_path=args.collector_snapshots,
            strict_resolutions_path=args.strict_resolutions,
            output_root=args.output_root,
        )
    except ValueError as error:
        parser.error(str(error))
    print(
        f"status={result.status} reused={str(result.reused).lower()} "
        f"pending={result.counts['pending']} unresolved={result.counts['unresolved']} "
        f"invalid={result.counts['invalid']} eligible={result.counts['eligible']} "
        f"collapsed={result.counts['collapsed']} history={result.history_path} "
        f"scoreboard={result.scoreboard_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
