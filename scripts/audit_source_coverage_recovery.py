#!/usr/bin/env python3
"""Run the offline derived source coverage/backfill recovery audit."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.weather.source_coverage_recovery_audit import audit_source_coverage_recovery  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", action="append", required=True, help="Pending or unusable source-observation JSONL; repeatable.")
    parser.add_argument("--strict-resolutions", required=True, help="Exact-identity strict resolution JSONL; no network lookup is performed.")
    parser.add_argument("--output-dir", required=True, help="New empty derived output directory.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = audit_source_coverage_recovery(
        observation_paths=[ROOT / path if not Path(path).is_absolute() else Path(path) for path in args.observations],
        strict_resolution_path=ROOT / args.strict_resolutions if not Path(args.strict_resolutions).is_absolute() else args.strict_resolutions,
        output_dir=ROOT / args.output_dir if not Path(args.output_dir).is_absolute() else args.output_dir,
    )
    counts = result.metadata["coverage_counts"]
    print(
        "offline-source-coverage-audit "
        f"exact_resolved={counts['exact_resolved']} missing={counts['missing_exact_resolution']} "
        f"conflict={counts['exact_resolution_ambiguity_or_conflict']} invalid_settlement={counts['invalid_settlement_timestamp']} "
        f"output={result.output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
