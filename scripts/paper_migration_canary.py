#!/usr/bin/env python3
"""Read-only migration/canary preview for existing dual paper wallet state."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import load_config  # noqa: E402
from bot.paper_migration_canary import (  # noqa: E402
    build_paper_migration_canary_plan,
    format_paper_migration_canary_plan,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview paper-state migration/canary mapping without mutating data")
    parser.add_argument("--config", default=None, help="Optional config path for wallet/root resolution")
    parser.add_argument("--data-dir", default=None, help="Optional runtime data dir override for wallet/root resolution")
    parser.add_argument(
        "--shared-candidates-root",
        default=None,
        help="Optional explicit shared-candidate destination root for copy/backfill previews",
    )
    parser.add_argument(
        "--deep-scan",
        action="store_true",
        help="Count JSONL rows in detected candidate datasets; default is stat-only and does not read dataset contents",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(args.config) if args.config else load_config()
    plan = build_paper_migration_canary_plan(
        config,
        data_dir=args.data_dir,
        shared_candidates_root=args.shared_candidates_root,
        deep_scan=args.deep_scan,
    )
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
    else:
        print(format_paper_migration_canary_plan(plan))
    return 0 if plan.get("status") == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
