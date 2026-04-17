#!/usr/bin/env python3
"""Summarize repo-local Kalshi weather city coverage from historical CSV data."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.weather.historical import (  # noqa: E402
    build_historical_city_coverage,
    load_historical_weather_records,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        default="data/historical/kalshi.csv",
        help="Repo-local historical Kalshi CSV to inspect.",
    )
    parser.add_argument(
        "--output",
        default="data/summaries/weather_historical_city_coverage.json",
        help="Path to the JSON report to write.",
    )
    parser.add_argument(
        "--full-history",
        action="store_true",
        help="Keep every weather row instead of one representative record per series.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = PROJECT_ROOT / args.input
    output_path = PROJECT_ROOT / args.output

    records = load_historical_weather_records(input_path, one_per_series=not args.full_history)
    report = build_historical_city_coverage(records)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    summary = report["summary"]
    print(f"Wrote {output_path.relative_to(PROJECT_ROOT)}")
    print(
        "Historical weather coverage "
        f"records={summary['records_examined']} "
        f"cities={summary['unique_historical_cities']} "
        f"registry_covered={summary['registry_covered_cities']} "
        f"missing={summary['registry_missing_cities']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
