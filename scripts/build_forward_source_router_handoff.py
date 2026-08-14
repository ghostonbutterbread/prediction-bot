#!/usr/bin/env python3
"""Write an inert forward-paper source-router handoff manifest; starts nothing."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.weather.forward_source_router_handoff import build_forward_source_router_handoff  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort-id", required=True)
    parser.add_argument("--start-at", required=True)
    parser.add_argument("--shared-snapshot-manifest-id", required=True)
    parser.add_argument("--control-policy-version", required=True)
    parser.add_argument("--control-policy-sha256", required=True)
    parser.add_argument("--router-policy-version", required=True)
    parser.add_argument("--router-policy-sha256", required=True)
    parser.add_argument("--evaluator-version", default="source-router-forward-evaluator-v1")
    parser.add_argument("--output", required=True, help="Manifest file to create; no service is started.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    manifest = build_forward_source_router_handoff(
        cohort_id=args.cohort_id, start_at=args.start_at, shared_snapshot_manifest_id=args.shared_snapshot_manifest_id,
        evaluator_version=args.evaluator_version,
        policy_bundles={
            "control_stable": {"version": args.control_policy_version, "sha256": args.control_policy_sha256},
            "shadow_source_router_no_price_guard": {"version": args.router_policy_version, "sha256": args.router_policy_sha256},
        },
    )
    output = Path(args.output)
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing manifest: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"disabled-forward-source-router-handoff enabled=false output={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
