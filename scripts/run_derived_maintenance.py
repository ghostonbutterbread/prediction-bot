#!/usr/bin/env python3
"""Run one configuration-driven beta derived-maintenance pass."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.config import load_config
from bot.derived_maintenance import run_derived_maintenance_once


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="absolute or working-directory-relative cohort config")
    args = parser.parse_args()
    config_path = Path(args.config).resolve()
    if not config_path.is_file():
        parser.error(f"cohort config does not exist: {config_path}")
    print(run_derived_maintenance_once(load_config(config_path)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
