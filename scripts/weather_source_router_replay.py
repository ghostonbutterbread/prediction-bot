#!/usr/bin/env python3
"""Replay weather source-router decisions against stable weather decisions.

Offline/read-only: consumes source-outcome ledger rows and explicit finalized
outcome files, then writes head-to-head stable-baseline vs source-router
reports. It does not fetch network data or mutate paper/live state.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.weather.source_reliability import load_source_outcome_ledger_rows  # noqa: E402
from bot.weather.source_router import (  # noqa: E402
    build_joined_source_router_ledger_rows,
    build_source_router_replay_rows,
    summarize_source_router_replay_rows,
)
from bot.weather.source_scoreboard import load_jsonl_rows  # noqa: E402
from scripts.weather_source_edge_validate import _load_outcome_lookup  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger-input", action="append", default=None, help="Prebuilt source-outcome ledger JSON/JSONL path. Repeatable.")
    parser.add_argument("--history-ledger-input", action="append", default=None, help="Prior source-outcome ledger JSON/JSONL rows used only as selector history. Repeatable.")
    parser.add_argument("--source-input", action="append", default=None, help="Raw source snapshot / market snapshot JSONL path. Repeatable.")
    parser.add_argument("--decision-input", action="append", default=None, help="Stable/main decision JSONL path to join against --source-input. Repeatable.")
    parser.add_argument("--outcome-input", action="append", required=True, help="Finalized market outcome JSON/JSONL path. Repeatable.")
    parser.add_argument("--output-dir", required=True, help="Directory for source-router replay artifacts.")
    parser.add_argument("--min-sample-count", type=int, default=5, help="Minimum prior resolved rows required before routing.")
    parser.add_argument("--limit", type=int, default=None, help="Optional maximum ledger rows to replay.")
    parser.add_argument("--report-limit", type=int, default=25, help="Maximum slice rows in markdown report.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.min_sample_count < 1:
        raise SystemExit("--min-sample-count must be >= 1")
    if args.limit is not None and args.limit < 0:
        raise SystemExit("--limit must be non-negative")

    if not args.ledger_input and not args.source_input:
        raise SystemExit("pass --ledger-input, or pass --source-input with --decision-input")
    if args.source_input and not args.decision_input:
        raise SystemExit("--decision-input is required when --source-input is used")
    ledger_paths = [_resolve_path(path) for path in args.ledger_input or []]
    history_ledger_paths = [_resolve_path(path) for path in args.history_ledger_input or []]
    source_paths = [_resolve_path(path) for path in args.source_input or []]
    decision_paths = [_resolve_path(path) for path in args.decision_input or []]
    outcome_paths = [_resolve_path(path) for path in args.outcome_input]
    output_dir = _resolve_path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    ledger_rows, input_stats = _load_router_ledger_rows(
        ledger_paths,
        history_ledger_paths=history_ledger_paths,
        source_paths=source_paths,
        decision_paths=decision_paths,
        limit=args.limit,
    )
    outcome_lookup, outcome_stats = _load_outcome_lookup(outcome_paths)
    replay_rows = build_source_router_replay_rows(
        ledger_rows,
        outcome_lookup=outcome_lookup,
        min_sample_count=args.min_sample_count,
    )
    summary = summarize_source_router_replay_rows(replay_rows)
    metadata = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "offline_source_router_replay_only",
        "network_access": False,
        "ledger_inputs": [str(path) for path in ledger_paths],
        "history_ledger_inputs": [str(path) for path in history_ledger_paths],
        "source_inputs": [str(path) for path in source_paths],
        "decision_inputs": [str(path) for path in decision_paths],
        "outcome_inputs": [str(path) for path in outcome_paths],
        "output_dir": str(output_dir),
        "min_sample_count": args.min_sample_count,
        "limit": args.limit,
        "outcome_load_stats": outcome_stats,
        "input_load_stats": input_stats,
        "summary": summary["summary"],
        "artifacts": [
            "source_router_decisions.jsonl",
            "source_router_summary.json",
            "source_router_slices.jsonl",
            "source_router_vs_stable.md",
            "run_metadata.json",
        ],
    }

    _write_jsonl(output_dir / "source_router_decisions.jsonl", replay_rows)
    _write_json(output_dir / "source_router_summary.json", summary)
    _write_jsonl(output_dir / "source_router_slices.jsonl", summary["slices"])
    (output_dir / "source_router_vs_stable.md").write_text(
        render_source_router_report_markdown(summary, metadata=metadata, limit=args.report_limit),
        encoding="utf-8",
    )
    _write_json(output_dir / "run_metadata.json", metadata)

    counts = summary["summary"]
    print(
        "Weather source router replay "
        f"ledger_rows={len(ledger_rows)} "
        f"replay_rows={counts.get('input_rows', 0)} "
        f"routeable={counts.get('routeable_rows', 0)} "
        f"stable_pnl={counts.get('stable_pnl_usd', 0.0)} "
        f"source_router_buys={counts.get('source_router_buy_rows', 0)} "
        f"source_router_pnl={counts.get('source_router_pnl_usd', 0.0)} "
        f"source_filter_pnl={counts.get('source_filter_pnl_usd', 0.0)} "
        f"output={output_dir}"
    )
    return 0


def render_source_router_report_markdown(
    summary: Mapping[str, Any],
    *,
    metadata: Mapping[str, Any] | None = None,
    limit: int = 25,
) -> str:
    run = dict(metadata or {})
    counts = summary.get("summary") if isinstance(summary.get("summary"), Mapping) else {}
    slices = [row for row in summary.get("slices", []) if isinstance(row, Mapping)]
    ranked = slices[: max(0, limit)]
    lines = [
        "# Weather Source Router Replay",
        "",
        "Offline source-router replay. Source routing chooses its own source-implied BUY_YES/BUY_NO/SKIP from the shared market candidate/source rows, then compares that head-to-head against stable using stable-sized hypothetical notional.",
        "",
        "## Run Metadata",
        "",
        f"- generated_at: {_markdown_cell(run.get('generated_at') or summary.get('generated_at'))}",
        f"- mode: {_markdown_cell(run.get('mode'))}",
        f"- network_access: {_markdown_cell(run.get('network_access'))}",
        f"- min_sample_count: {_markdown_cell(run.get('min_sample_count'))}",
        "",
        "## Summary",
        "",
        "| rows | routeable | source_buys | stable_pnl | source_router_pnl | router_delta | source_filter_pnl | filter_delta |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
        f"| {counts.get('input_rows') or 0} | {counts.get('routeable_rows') or 0} | {counts.get('source_router_buy_rows') or 0} | {_metric(counts.get('stable_pnl_usd'))} | {_metric(counts.get('source_router_pnl_usd'))} | {_metric(counts.get('source_router_minus_stable_pnl_usd'))} | {_metric(counts.get('source_filter_pnl_usd'))} | {_metric(counts.get('source_filter_minus_stable_pnl_usd'))} |",
        "",
        "## Chosen Sources",
        "",
    ]
    source_counts = counts.get("chosen_source_counts") if isinstance(counts.get("chosen_source_counts"), Mapping) else {}
    if source_counts:
        for source_id, count in source_counts.items():
            lines.append(f"- {source_id}: {count}")
    else:
        lines.append("- none: 0")
    lines.extend([
        "",
        "## Blockers",
        "",
    ])
    blockers = counts.get("blocker_counts") if isinstance(counts.get("blocker_counts"), Mapping) else {}
    if blockers:
        for blocker, count in blockers.items():
            lines.append(f"- {blocker}: {count}")
    else:
        lines.append("- none: 0")
    lines.extend([
        "",
        "## Routeable Slices",
        "",
        "| city | kind | shape | side | rows | routeable | source_buys | stable_pnl | source_router_pnl | router_delta |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|",
    ])
    if not ranked:
        lines.append("| none | none | none | none | 0 | 0 | 0 | 0 | 0 | 0 |")
    for row in ranked:
        lines.append(
            "| "
            f"{_markdown_cell(row.get('city_id'))} | "
            f"{_markdown_cell(row.get('market_kind'))} | "
            f"{_markdown_cell(row.get('contract_shape'))} | "
            f"{_markdown_cell(row.get('question_side'))} | "
            f"{row.get('rows') or 0} | "
            f"{row.get('routeable_rows') or 0} | "
            f"{row.get('source_router_buy_rows') or 0} | "
            f"{_metric(row.get('stable_pnl_usd'))} | "
            f"{_metric(row.get('source_router_pnl_usd'))} | "
            f"{_metric(row.get('source_router_minus_stable_pnl_usd'))} |"
        )
    lines.append("")
    return "\n".join(lines)


def _load_router_ledger_rows(
    ledger_paths: Iterable[Path],
    *,
    history_ledger_paths: Iterable[Path],
    source_paths: Iterable[Path],
    decision_paths: Iterable[Path],
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    stats: dict[str, Any] = {"mode": "ledger_input"}
    replay_row_count = 0
    for path in history_ledger_paths:
        for row in load_source_outcome_ledger_rows(path):
            copied = dict(row)
            copied["source_router_history_only"] = True
            rows.append(copied)
    if rows:
        stats["history_ledger_rows"] = len(rows)
    for path in ledger_paths:
        for row in load_source_outcome_ledger_rows(path):
            if limit is not None and replay_row_count >= limit:
                stats["limit_reached"] = True
                stats["replay_ledger_rows"] = replay_row_count
                return rows, stats
            rows.append(row)
            replay_row_count += 1
    stats["replay_ledger_rows"] = replay_row_count
    source_paths = list(source_paths)
    decision_paths = list(decision_paths)
    if source_paths:
        source_rows, source_stats = load_jsonl_rows(source_paths, limit=limit)
        decision_rows, decision_stats = load_jsonl_rows(decision_paths)
        joined_rows, join_stats = build_joined_source_router_ledger_rows(source_rows, decision_rows)
        rows.extend(joined_rows)
        stats = {
            "mode": "joined_source_and_decision_inputs",
            "source_load_stats": source_stats,
            "decision_load_stats": decision_stats,
            "join_stats": join_stats,
        }
    return rows, stats


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _resolve_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def _markdown_cell(value: Any) -> str:
    return str(value if value is not None else "n/a").replace("|", "\\|")


def _metric(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return _markdown_cell(value)


if __name__ == "__main__":
    raise SystemExit(main())
