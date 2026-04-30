#!/usr/bin/env python3
"""Read-only supervised live repeatability evidence report."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from bot.live_repeatability import (
    build_live_repeatability_report,
    data_dir_from_static_config,
    format_live_repeatability_report,
)


def _load_static_config(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.live_supervised.yaml", help="Static config to derive the live artifact directory.")
    parser.add_argument("--data-dir", help="Artifact directory to review, for example data/live.")
    parser.add_argument("--sessions", type=int, default=5, help="Number of recent startup-delimited sessions to review.")
    args = parser.parse_args()

    if args.data_dir:
        data_dir = Path(args.data_dir)
    else:
        data_dir = data_dir_from_static_config(_load_static_config(args.config))

    report = build_live_repeatability_report(data_dir, sessions=args.sessions)
    print(format_live_repeatability_report(report))
    return 0 if report.get("ready") else 1


if __name__ == "__main__":
    sys.exit(main())
