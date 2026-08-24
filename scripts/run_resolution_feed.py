#!/usr/bin/env python3
"""Run one read-only cohort resolution-feed pass from an explicit config path."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.config import load_config
from bot.resolution_feed import run_resolution_feed_once


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="absolute or working-directory-relative cohort config")
    parser.add_argument("--force", action="store_true", help="run even when the configured interval is not due")
    parser.add_argument("--enable", action="store_true", help="explicitly enable the separate resolution feed for this one pass")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    if not config_path.is_file():
        parser.error(f"cohort config does not exist: {config_path}")
    config = load_config(config_path)
    if args.enable:
        config.setdefault("resolution_feed", {})["enabled"] = True
    result = run_resolution_feed_once(config, force=args.force)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
