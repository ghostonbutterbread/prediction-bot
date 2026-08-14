#!/usr/bin/env python3
"""Export immutable collector JSONL into offline sanitized replay inputs."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.collector_replay_inputs import export_collector_replay_inputs  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collector-archive", required=True, help="Immutable collector JSONL archive to read.")
    parser.add_argument("--output-dir", required=True, help="New or empty directory under data/derived_reports.")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--max-rows", type=int, default=None, help="Optional maximum non-empty collector rows to select.")
    selection.add_argument(
        "--accepted-limit",
        type=int,
        default=None,
        help="Select this many newest successfully sanitized rows, skipping rejected rows.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = export_collector_replay_inputs(
            source_archive=args.collector_archive,
            output_dir=args.output_dir,
            max_rows=args.max_rows,
            accepted_limit=args.accepted_limit,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error
    metadata = result.metadata
    print(
        "Collector replay-input export "
        f"selected={metadata['selected_row_count']} "
        f"accepted={metadata['accepted_row_count']} "
        f"rejected={metadata['rejected_row_count']} "
        f"selection_mode={metadata['selection_mode']} "
        f"output={result.output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
