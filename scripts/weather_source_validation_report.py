#!/usr/bin/env python3
"""Build a compact city/source validation report from archive weather markets."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.weather.source_validation import build_source_validation_report  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-dir",
        default="data/weather/sources",
        help="Directory of per-city source validation pilot JSON files.",
    )
    parser.add_argument(
        "--history",
        default="data/historical/kalshi.csv",
        help="Historical weather market CSV used as the threshold-resolution archive.",
    )
    parser.add_argument(
        "--output",
        default="data/summaries/weather_source_validation_pilot.json",
        help="Path to the JSON report to write.",
    )
    parser.add_argument(
        "--city",
        action="append",
        default=[],
        help="Optional city_id filter. Repeat to include multiple pilot cities.",
    )
    return parser.parse_args()


def main(argv: list[str] | None = None) -> int:
    args = parse_args() if argv is None else parse_args_from(argv)
    report = build_source_validation_report(
        source_dir=PROJECT_ROOT / args.input_dir,
        history_path=PROJECT_ROOT / args.history,
        city_ids=args.city or None,
    )
    output_path = PROJECT_ROOT / args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    summary = report["summary"]
    try:
        rendered_output = str(output_path.relative_to(PROJECT_ROOT))
    except ValueError:
        rendered_output = str(output_path)
    print(f"Wrote {rendered_output}")
    print(
        "Pilot source validation "
        f"cities={summary['cities']} "
        f"sources={summary['sources']} "
        f"archive_markets={summary['archive_threshold_markets_available']} "
        f"matched_validations={summary['matched_validation_entries']} "
        f"accuracy={summary['accuracy']}"
    )
    return 0


def parse_args_from(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", default="data/weather/sources")
    parser.add_argument("--history", default="data/historical/kalshi.csv")
    parser.add_argument("--output", default="data/summaries/weather_source_validation_pilot.json")
    parser.add_argument("--city", action="append", default=[])
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
