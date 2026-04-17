#!/usr/bin/env python3
"""Build a small historical weather market mapping report from repo-local files."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.weather.analysis import (  # noqa: E402
    build_report,
    compare_sample_records,
    load_historical_csv_samples,
    load_scan_samples,
    load_simulation_samples,
    load_snapshot_samples,
    select_sample_records,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default="data/summaries/weather_market_mapping_sample.json",
        help="Path to the JSON report to write.",
    )
    parser.add_argument("--max-records", type=int, default=24, help="Max total sampled records.")
    parser.add_argument("--max-per-kind", type=int, default=8, help="Max sampled records per source kind.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project_root = PROJECT_ROOT
    output_path = project_root / args.output

    records = []
    input_counts = []

    resolved_sim_path = project_root / "data/archive/reset_20260411_210417/sim_20260321_193703.json"
    if resolved_sim_path.exists():
        loaded = load_simulation_samples(resolved_sim_path, resolved_only=True)
        records.extend(loaded)
        input_counts.append({"path": str(resolved_sim_path.relative_to(project_root)), "loaded": len(loaded)})

    historical_csv_path = project_root / "data/historical/kalshi.csv"
    if historical_csv_path.exists():
        loaded = load_historical_csv_samples(historical_csv_path, one_per_series=True)
        records.extend(loaded)
        input_counts.append({"path": "data/historical/kalshi.csv", "loaded": len(loaded)})

    for snapshot_name in ("data/market_snapshot_old.json", "data/market_snapshot.json"):
        snapshot_path = project_root / snapshot_name
        if not snapshot_path.exists():
            continue
        loaded = load_snapshot_samples(snapshot_path)
        records.extend(loaded)
        input_counts.append({"path": snapshot_name, "loaded": len(loaded)})

    scan_total = 0
    for scan_path in sorted((project_root / "data").glob("scans_*.jsonl")):
        loaded = load_scan_samples(scan_path)
        records.extend(loaded)
        scan_total += len(loaded)
    if scan_total:
        input_counts.append({"path": "data/scans_*.jsonl", "loaded": scan_total})

    sampled = select_sample_records(records, max_records=args.max_records, max_per_kind=args.max_per_kind)
    comparisons = compare_sample_records(sampled)
    report = build_report(comparisons)
    report["inputs"] = input_counts

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    summary = report["summary"]
    print(f"Wrote {output_path.relative_to(project_root)}")
    print(
        "Sampled "
        f"{summary['records_sampled']} records "
        f"({summary['by_sample_kind']}) | "
        f"city_fit={summary['by_city_fit']}"
    )
    print(
        "Registry mapped "
        f"{summary['registry_mapped_records']} records; "
        f"type misses={summary['baseline_market_type_misses']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
