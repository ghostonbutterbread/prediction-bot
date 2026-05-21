#!/usr/bin/env python3
"""Backfill finalized market outcomes for scoreboard/lane P&L replay.

This writes derived resolution artifacts only. It does not mutate paper wallets,
Prediction Lab ledgers, risk state, or accounting files.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.scoreboard_resolution_backfill import backfill_scoreboard_resolutions  # noqa: E402


DEFAULT_INPUT = "data/beta_shadow/paper/source_scoreboard/paper_shadow_lane_decisions.jsonl"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "inputs",
        nargs="*",
        default=[DEFAULT_INPUT],
        help="JSONL lane/candidate/decision files to scan for market ids.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Derived resolution JSONL output path. Defaults under data/summaries/.",
    )
    parser.add_argument(
        "--report-output",
        default=None,
        help="Optional JSON report output path. Defaults beside --output.",
    )
    parser.add_argument(
        "--include-unresolved",
        action="store_true",
        help="Also write unresolved market rows with null outcomes. Defaults to finalized outcomes only.",
    )
    parser.add_argument("--max-markets", type=int, default=None, help="Optional cap for smoke/backfill slices.")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Console output format.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_path = _safe_derived_output_path(args.output or _default_output_path())
    report_path = _safe_derived_output_path(args.report_output) if args.report_output else output_path.with_suffix(
        output_path.suffix + ".report.json"
    )
    result = backfill_scoreboard_resolutions(
        [ROOT / item for item in args.inputs],
        output_path=output_path,
        report_path=report_path,
        include_unresolved=args.include_unresolved,
        max_markets=args.max_markets,
    )
    if args.format == "json":
        print(json.dumps(result.report, indent=2, sort_keys=True))
    else:
        print(_format_report(result.report))
    return 0


def _default_output_path() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"data/summaries/scoreboard_resolution_backfill_{stamp}.jsonl"


def _safe_derived_output_path(raw_path: str | Path) -> Path:
    path = (ROOT / raw_path).resolve() if not Path(raw_path).is_absolute() else Path(raw_path).resolve()
    allowed_roots = [
        (ROOT / "data" / "summaries").resolve(),
        (ROOT / "data" / "beta_shadow" / "summaries").resolve(),
        (ROOT / "data" / "beta_shadow" / "reports").resolve(),
        (ROOT / "data" / "derived_reports").resolve(),
    ]
    if not any(path == root or root in path.parents for root in allowed_roots):
        allowed = ", ".join(str(root.relative_to(ROOT)) for root in allowed_roots)
        raise ValueError(f"derived backfill output must be under one of: {allowed}")
    return path


def _format_report(report: dict[str, object]) -> str:
    return "\n".join(
        [
            "Scoreboard resolution backfill "
            f"inputs={len(report.get('input_paths') or [])} "
            f"rows={report.get('input_rows_read', 0)} "
            f"refs={report.get('market_refs_found', 0)} "
            f"unique_markets={report.get('unique_markets_found', 0)} "
            f"requested={report.get('markets_requested', 0)} "
            f"written={report.get('resolution_rows_written', 0)} "
            f"resolved={report.get('resolved_market_count', 0)} "
            f"unresolved={report.get('unresolved_market_count', 0)} "
            f"errors={report.get('fetch_error_count', 0)}",
            f"outcomes={json.dumps(report.get('by_outcome') or {}, sort_keys=True)}",
            f"statuses={json.dumps(report.get('by_status') or {}, sort_keys=True)}",
            f"output={report.get('output_path')}",
            f"report={report.get('report_path')}",
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
