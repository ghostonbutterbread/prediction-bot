#!/usr/bin/env python3
"""Explicit, disabled-by-default, read-only forward paper composition observer.

Consumes the full forward paper lane ledger and shared publisher state. There is
no exchange polling, collector, resolver invocation, account write, or order API.
Redirect stdout into a derived report location if a durable receipt is needed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.forward_composed_paper import observe_fresh_forward_snapshot  # noqa: E402
from bot.risk import RiskManager  # noqa: E402
from bot.strategies.enhanced import KellySizer  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lane-decision-path", required=True)
    parser.add_argument("--shared-market-state-path", required=True)
    parser.add_argument("--resolution-path", help="Optional separate exact-authority resolution JSONL")
    parser.add_argument("--enable-paper-observer", action="store_true", default=False)
    parser.add_argument("--max-snapshot-age-seconds", type=int, default=120)
    parser.add_argument("--starting-balance-usd", type=float, default=100.0)
    parser.add_argument("--fee-rate", type=float, default=0.07)
    parser.add_argument("--event-cap-pct", type=float, default=0.10)
    args = parser.parse_args(argv)
    if not args.enable_paper_observer:
        print(json.dumps({"status": "skipped", "reason_code": "observer_disabled"}))
        return 0
    if args.max_snapshot_age_seconds <= 0:
        parser.error("max snapshot age must be positive")
    rows = _jsonl(Path(args.lane_decision_path))
    resolutions = _jsonl(Path(args.resolution_path)) if args.resolution_path else []
    state = json.loads(Path(args.shared_market_state_path).read_text(encoding="utf-8"))
    risk = RiskManager({"trading": {"mode": "paper"}})
    kelly = KellySizer(kelly_fraction=risk.kelly_fraction, max_bet_pct=risk.max_bet_pct,
                        fee_rate=args.fee_rate)
    result = observe_fresh_forward_snapshot(
        rows, resolutions, state, enabled=True,
        max_snapshot_age_seconds=args.max_snapshot_age_seconds,
        starting_balance_usd=args.starting_balance_usd, kelly_sizer=kelly,
        risk_policy=risk, min_edge=0.01, min_confidence=0.50, max_entry_price=0.70,
        fee_rate=args.fee_rate, event_cap_pct=args.event_cap_pct,
    )
    print(json.dumps(result, sort_keys=True, default=str))
    return 0


def _jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as source:
        return [json.loads(line) for line in source if line.strip()]


if __name__ == "__main__":
    raise SystemExit(main())
