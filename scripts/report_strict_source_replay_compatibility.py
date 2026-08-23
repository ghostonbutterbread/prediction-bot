#!/usr/bin/env python3
"""Report strict source producer-to-replay field coverage from sealed inputs."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.weather.strict_source_compatibility import summarize_strict_source_replay_compatibility


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay-inputs", required=True)
    parser.add_argument("--max-records", type=int, default=1000)
    parser.add_argument("--output", required=True, help="new report JSON path")
    args = parser.parse_args()
    if args.max_records <= 0:
        parser.error("--max-records must be positive")
    source = Path(args.replay_inputs).resolve()
    output = Path(args.output).resolve()
    if not source.is_file():
        parser.error(f"replay inputs do not exist: {source}")
    if output.exists():
        parser.error(f"refusing to overwrite report: {output}")
    records = []
    for line in source.open(encoding="utf-8"):
        if line.strip():
            records.append(json.loads(line))
            if len(records) >= args.max_records:
                break
    report = {
        "schema_name": "strict_source_replay_compatibility_report",
        "schema_version": 1,
        "mode": "offline_bounded_compatibility_report",
        "non_mutating": True,
        "replay_inputs_path": str(source),
        "max_records": args.max_records,
        "coverage": summarize_strict_source_replay_compatibility(records),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    print(json.dumps(report["coverage"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
