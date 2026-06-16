#!/usr/bin/env python3
"""Read-only paper shadow lane report with source-scoreboard readiness focus."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import load_config  # noqa: E402
from bot.paper_shadow_lanes import (  # noqa: E402
    build_paper_shadow_lane_resolution_rows,
    summarize_paper_shadow_lane_report,
    summarize_paper_shadow_lane_resolved_pnl,
    update_paper_shadow_lane_incremental_pnl,
)


DEFAULT_LANE_DECISION_PATH = "data/beta_shadow/paper/source_scoreboard/paper_shadow_lane_decisions.jsonl"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lane-decision-path",
        default=DEFAULT_LANE_DECISION_PATH,
        help="Path to the paper shadow lane decision JSONL to summarize.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Optional config path used to scope enabled lanes. Defaults to no config scoping.",
    )
    parser.add_argument(
        "--section",
        choices=["source_scoreboard_readiness", "resolved_pnl", "incremental_pnl", "full"],
        default="source_scoreboard_readiness",
        help="Which section of the report to print.",
    )
    parser.add_argument(
        "--resolution-path",
        default=None,
        help="Optional JSONL of finalized resolution rows for resolved_pnl section.",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format. Full reports print JSON when text is requested.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional path to write the selected payload as JSON.",
    )
    parser.add_argument(
        "--resolved-output-jsonl",
        default=None,
        help="Optional path to write per-row resolved lane artifacts as JSONL. Must be under a derived-report directory.",
    )
    parser.add_argument(
        "--incremental-state",
        default=None,
        help="State path for incremental_pnl. Must be under a derived-report directory.",
    )
    parser.add_argument(
        "--incremental-events-output",
        default=None,
        help="Optional JSONL event output for incremental_pnl. Must be under a derived-report directory.",
    )
    parser.add_argument("--starting-balance-usd", type=float, default=100.0)
    parser.add_argument(
        "--sizing-mode",
        choices=["recorded_notional", "balance_scaled", "balance_fraction"],
        default="recorded_notional",
        help="How incremental replay sizes buys against the synthetic balance.",
    )
    parser.add_argument("--balance-fraction", type=float, default=0.1)
    parser.add_argument("--max-new-rows", type=int, default=10000)
    parser.add_argument("--max-pending-rows", type=int, default=50000)
    parser.add_argument("--reset-incremental", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(ROOT / args.config) if args.config else None
    if args.section == "full":
        report = summarize_paper_shadow_lane_report(
            lane_decision_path=ROOT / args.lane_decision_path,
            config=config,
        )
        payload = report
    elif args.section == "resolved_pnl":
        payload = summarize_paper_shadow_lane_resolved_pnl(
            lane_decision_path=ROOT / args.lane_decision_path,
            resolution_path=ROOT / args.resolution_path if args.resolution_path else None,
        )
    elif args.section == "incremental_pnl":
        if not args.resolution_path:
            raise SystemExit("--resolution-path is required for incremental_pnl")
        if not args.incremental_state:
            raise SystemExit("--incremental-state is required for incremental_pnl")
        payload = update_paper_shadow_lane_incremental_pnl(
            lane_decision_path=ROOT / args.lane_decision_path,
            resolution_path=ROOT / args.resolution_path,
            state_path=_safe_derived_output_path(args.incremental_state),
            event_output_path=_safe_derived_output_path(args.incremental_events_output) if args.incremental_events_output else None,
            starting_balance_usd=args.starting_balance_usd,
            sizing_mode=args.sizing_mode,
            balance_fraction=args.balance_fraction,
            max_new_rows=args.max_new_rows,
            max_pending_rows=args.max_pending_rows,
            reset=args.reset_incremental,
        )
    else:
        report = summarize_paper_shadow_lane_report(
            lane_decision_path=ROOT / args.lane_decision_path,
            config=config,
        )
        payload = report["source_scoreboard_readiness"]

    if args.output:
        output_path = ROOT / args.output
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if args.resolved_output_jsonl:
        resolved_rows = build_paper_shadow_lane_resolution_rows(
            lane_decision_path=ROOT / args.lane_decision_path,
            resolution_path=ROOT / args.resolution_path if args.resolution_path else None,
        )
        resolved_output_path = _safe_derived_output_path(args.resolved_output_jsonl)
        resolved_output_path.parent.mkdir(parents=True, exist_ok=True)
        with resolved_output_path.open("w", encoding="utf-8") as fh:
            for row in resolved_rows:
                fh.write(json.dumps(row, sort_keys=True) + "\n")

    if args.format == "json" or args.section == "full":
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        if args.section == "resolved_pnl":
            print(_format_resolved_pnl(payload))
        elif args.section == "incremental_pnl":
            print(_format_incremental_pnl(payload))
        else:
            print(_format_source_scoreboard_readiness(payload))
    return 0


def _safe_derived_output_path(raw_path: str) -> Path:
    output_path = (ROOT / raw_path).resolve()
    allowed_roots = [
        (ROOT / "data" / "summaries").resolve(),
        (ROOT / "data" / "beta_shadow" / "summaries").resolve(),
        (ROOT / "data" / "beta_shadow" / "reports").resolve(),
        (ROOT / "data" / "derived_reports").resolve(),
    ]
    if not any(output_path == root or root in output_path.parents for root in allowed_roots):
        allowed = ", ".join(str(root.relative_to(ROOT)) for root in allowed_roots)
        raise ValueError(
            "--resolved-output-jsonl must be written under a derived report directory "
            f"({allowed}); refusing to overwrite wallet/accounting paths"
        )
    return output_path


def _format_source_scoreboard_readiness(payload: dict[str, object]) -> str:
    leak_risk = payload.get("leak_risk_indicators") if isinstance(payload.get("leak_risk_indicators"), dict) else {}
    blockers = payload.get("missing_field_blockers") if isinstance(payload.get("missing_field_blockers"), dict) else {}
    tier_counts = payload.get("reliability_tier_counts") if isinstance(payload.get("reliability_tier_counts"), dict) else {}
    label_counts = payload.get("label_source_counts") if isinstance(payload.get("label_source_counts"), dict) else {}
    return "\n".join(
        [
            "Source scoreboard readiness "
            f"rows={payload.get('evaluated_rows', 0)} "
            f"explicit_labels={payload.get('explicit_label_rows', 0)} "
            f"independent_labels={payload.get('independent_label_rows', 0)} "
            f"order_book={payload.get('order_book_quote_rows', 0)} "
            f"execution={payload.get('execution_snapshot_rows', 0)} "
            f"estimated_fill={payload.get('estimated_fill_price_rows', 0)}",
            f"reliability_tiers={json.dumps(tier_counts, sort_keys=True)}",
            f"label_sources={json.dumps(label_counts, sort_keys=True)}",
            f"leak_risk={json.dumps(leak_risk, sort_keys=True)}",
            f"blockers={json.dumps(blockers, sort_keys=True)}",
        ]
    )


def _format_resolved_pnl(payload: dict[str, object]) -> str:
    blockers = payload.get("blocker_counts") if isinstance(payload.get("blocker_counts"), dict) else {}
    by_lane = payload.get("by_lane") if isinstance(payload.get("by_lane"), dict) else {}
    source_router = payload.get("source_router") if isinstance(payload.get("source_router"), dict) else {}
    lane_summary = {
        str(lane): {
            "pnl": data.get("total_pnl_usd"),
            "stake": data.get("total_stake_usd"),
            "roi_pct": data.get("roi_pct"),
            "resolved": data.get("resolved_rows"),
            "blocked": data.get("blocker_counts"),
        }
        for lane, data in by_lane.items()
        if isinstance(data, dict)
    }
    return "\n".join(
        [
            "Resolved paper shadow lane PnL "
            f"rows={payload.get('evaluated_rows', 0)} "
            f"resolved={payload.get('resolved_rows', 0)} "
            f"calculable={payload.get('pnl_calculable_rows', 0)} "
            f"stake=${payload.get('total_stake_usd', 0)} "
            f"pnl=${payload.get('total_pnl_usd', 0)} "
            f"roi={payload.get('roi_pct')}%",
            f"blockers={json.dumps(blockers, sort_keys=True)}",
            f"by_lane={json.dumps(lane_summary, sort_keys=True)}",
            "source_router="
            + json.dumps(
                {
                    "raw_win_rate_pct": source_router.get("raw_router_win_rate_pct"),
                    "correct_side_rows": source_router.get("raw_router_correct_side_rows"),
                    "resolved_buy_rows": source_router.get("raw_router_resolved_buy_rows"),
                    "standardized_stake": source_router.get("standardized_hypothetical_stake_usd"),
                    "standardized_pnl": source_router.get("standardized_hypothetical_pnl_usd"),
                    "standardized_roi_pct": source_router.get("standardized_hypothetical_roi_pct"),
                    "actions": source_router.get("action_counts"),
                    "blockers": source_router.get("blocker_counts"),
                },
                sort_keys=True,
            ),
        ]
    )


def _format_incremental_pnl(payload: dict[str, object]) -> str:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    last_run = payload.get("last_run") if isinstance(payload.get("last_run"), dict) else {}
    lanes = summary.get("lanes") if isinstance(summary.get("lanes"), dict) else {}
    lane_summary = {
        lane_id: {
            "balance": lane.get("balance_usd"),
            "pnl": lane.get("total_pnl_usd"),
            "stake": lane.get("total_stake_usd"),
            "roi_pct": lane.get("roi_pct"),
            "balance_return_pct": lane.get("balance_return_pct"),
            "buys": lane.get("buy_rows"),
            "wins": lane.get("winning_buy_rows"),
            "losses": lane.get("losing_buy_rows"),
            "blockers": lane.get("blocker_counts"),
        }
        for lane_id, lane in lanes.items()
        if isinstance(lane, dict)
    }
    return "\n".join(
        [
            "Incremental paper shadow lane PnL "
            f"new_rows={last_run.get('new_rows_read', 0)} "
            f"applied={last_run.get('applied_rows', 0)} "
            f"pending={payload.get('pending_count', 0)} "
            f"total_pnl=${summary.get('total_pnl_usd', 0)}",
            f"cursor={payload.get('cursor_offset', 0)} / {payload.get('cursor_file_size', 0)}",
            f"by_lane={json.dumps(lane_summary, sort_keys=True)}",
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
