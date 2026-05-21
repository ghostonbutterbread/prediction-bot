#!/usr/bin/env python3
"""Build standalone weather source-confidence rows from JSONL input."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.file_ops import rewrite_jsonl  # noqa: E402
from bot.weather.source_confidence import build_source_confidence_row, summarize_source_confidence_rows  # noqa: E402
from bot.weather.source_reliability import (  # noqa: E402
    build_rolling_source_reliability_rows,
    load_scoreboard_rows,
    load_source_outcome_ledger_rows,
)


DEFAULT_OUTPUT = "data/beta_shadow/paper/source_confidence/weather_source_confidence.jsonl"


def parse_args() -> argparse.Namespace:
    return parse_args_from(sys.argv[1:])


def parse_args_from(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Prediction Lab/shared candidate JSONL input path.")
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"JSONL output path. Default: {DEFAULT_OUTPUT}",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional maximum number of JSON object rows to load.",
    )
    parser.add_argument(
        "--reliability",
        default=None,
        help="Optional JSON or JSONL scoreboard/reliability rows for source-only directional scoring.",
    )
    parser.add_argument(
        "--source-outcome-ledger",
        default=None,
        help=(
            "Optional JSON/JSONL resolved source-outcome ledger. When provided without --reliability, "
            "the CLI builds an as-of reliability table for each input row from rows known before that candidate."
        ),
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="Optional fixed as-of timestamp for --source-outcome-ledger reliability rows; defaults to each candidate observed_at.",
    )
    parser.add_argument("--min-samples", type=int, default=100, help="Minimum samples for rolling reliability tiers.")
    parser.add_argument("--trusted-samples", type=int, default=200, help="Sample threshold for strong_trusted rolling tiers.")
    parser.add_argument("--max-window", type=int, default=200, help="Maximum recent ledger rows per source slice.")
    parser.add_argument(
        "--summary-output",
        default=None,
        help="Optional JSON summary output path. Summary is source-only and omits full row payloads.",
    )
    parser.add_argument(
        "--report-output",
        default=None,
        help="Optional Markdown source-only report path with grade/blocker/source count rollups.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args() if argv is None else parse_args_from(argv)
    if args.limit is not None and args.limit < 0:
        raise SystemExit("--limit must be non-negative")

    input_path = _resolve_path(args.input)
    output_path = _resolve_path(args.output)
    if args.reliability and args.source_outcome_ledger:
        raise SystemExit("Use either --reliability or --source-outcome-ledger, not both")
    if args.min_samples < 1 or args.trusted_samples < 1 or args.max_window < 1:
        raise SystemExit("--min-samples, --trusted-samples, and --max-window must be positive")

    reliability_rows = load_scoreboard_rows(_resolve_path(args.reliability)) if args.reliability else None
    source_outcome_rows = (
        load_source_outcome_ledger_rows(_resolve_path(args.source_outcome_ledger)) if args.source_outcome_ledger else None
    )
    rows = _load_jsonl(input_path, limit=args.limit)
    confidence_rows = [
        build_source_confidence_row(
            row,
            reliability_table=_reliability_rows_for_candidate(
                row,
                static_reliability_rows=reliability_rows,
                source_outcome_rows=source_outcome_rows,
                fixed_as_of=args.as_of,
                max_window=args.max_window,
                min_samples=args.min_samples,
                trusted_samples=args.trusted_samples,
            ),
        )
        for row in rows
    ]
    rewrite_jsonl(output_path, confidence_rows)

    summary = summarize_source_confidence_rows(confidence_rows)
    if args.summary_output:
        summary_output_path = _resolve_path(args.summary_output)
        summary_output_path.parent.mkdir(parents=True, exist_ok=True)
        summary_payload = _summary_output_payload(summary, args=args)
        summary_output_path.write_text(json.dumps(summary_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.report_output:
        report_output_path = _resolve_path(args.report_output)
        report_output_path.parent.mkdir(parents=True, exist_ok=True)
        report_output_path.write_text(_markdown_report(summary, args=args), encoding="utf-8")
    print(
        " ".join(
            [
                "Weather source confidence",
                f"rows={len(confidence_rows)}",
                f"grade_counts={summary['grade_counts']}",
                f"observations={len(summary['source_observation_rows'])}",
                f"actions={summary['recommended_action_counts']}",
                f"reasons={summary['reason_counts']}",
                f"sources_used={summary['sources_used_count']}",
                f"sources_excluded={summary['sources_excluded_count']}",
                f"no_source_data={summary['no_source_data']}",
                f"insufficient_history={summary['insufficient_history']}",
                f"per_source_counts={summary['per_source_counts']}",
                f"reliability_mode={_reliability_mode(args)}",
                f"output={output_path}",
            ]
        )
    )
    return 0


def _reliability_rows_for_candidate(
    row: dict[str, Any],
    *,
    static_reliability_rows: list[dict[str, Any]] | None,
    source_outcome_rows: list[dict[str, Any]] | None,
    fixed_as_of: str | None,
    max_window: int,
    min_samples: int,
    trusted_samples: int,
) -> list[dict[str, Any]] | None:
    if static_reliability_rows is not None:
        return static_reliability_rows
    if source_outcome_rows is None:
        return None
    as_of = fixed_as_of or _candidate_as_of(row)
    if not as_of:
        return []
    return build_rolling_source_reliability_rows(
        source_outcome_rows,
        as_of,
        max_window=max_window,
        min_samples=min_samples,
        trusted_samples=trusted_samples,
    )


def _candidate_as_of(row: dict[str, Any]) -> str | None:
    candidates = [
        row.get("observed_at"),
        row.get("timestamp"),
        row.get("created_at"),
    ]
    shared_candidate = row.get("shared_candidate") if isinstance(row.get("shared_candidate"), dict) else {}
    decision_artifact = row.get("decision_artifact") if isinstance(row.get("decision_artifact"), dict) else {}
    strategy_signal = decision_artifact.get("strategy_signal") if isinstance(decision_artifact.get("strategy_signal"), dict) else {}
    strategy_data = strategy_signal.get("data") if isinstance(strategy_signal.get("data"), dict) else {}
    candidates.extend(
        [
            shared_candidate.get("observed_at"),
            shared_candidate.get("created_at"),
            decision_artifact.get("observed_at"),
            decision_artifact.get("timestamp"),
            strategy_signal.get("observed_at"),
            strategy_data.get("observed_at"),
        ]
    )
    for candidate in candidates:
        if candidate is None:
            continue
        text = str(candidate).strip()
        if text:
            return text
    return None


def _reliability_mode(args: argparse.Namespace) -> str:
    if args.reliability:
        return "static_reliability_rows"
    if args.source_outcome_ledger:
        return "rolling_source_outcome_ledger"
    return "none"


def _summary_output_payload(summary: dict[str, Any], *, args: argparse.Namespace | None = None) -> dict[str, Any]:
    keys = [
        "schema",
        "row_count",
        "grade_counts",
        "reason_counts",
        "recommended_action_counts",
        "agreement_state_counts",
        "source_direction_counts",
        "confidence_type_counts",
        "confidence_score_range",
        "sources_used_count",
        "sources_excluded_count",
        "no_source_data",
        "insufficient_history",
        "per_source_counts",
        "per_source_used_counts",
        "source_exclusion_reason_counts",
        "source_used_vote_counts",
        "source_used_tier_counts",
        "source_excluded_tier_counts",
    ]
    payload = {key: summary.get(key) for key in keys if key != "schema"}
    run_config = {
        "reliability_mode": _reliability_mode(args) if args is not None else "unknown",
    }
    if args is not None:
        run_config.update(
            {
                "limit": args.limit,
                "min_samples": args.min_samples,
                "trusted_samples": args.trusted_samples,
                "max_window": args.max_window,
                "fixed_as_of": args.as_of,
                "has_static_reliability": bool(args.reliability),
                "has_source_outcome_ledger": bool(args.source_outcome_ledger),
            }
        )
    return {"schema": "weather_source_confidence_summary_v1", "run_config": run_config, **payload}



def _markdown_report(summary: dict[str, Any], *, args: argparse.Namespace | None = None) -> str:
    run_config = _summary_output_payload(summary, args=args)["run_config"]
    lines = [
        "# Weather Source Confidence Report",
        "",
        "Source-only beta/shadow audit report. This report is not a trading, Kelly, PnL, wallet, or execution input.",
        "",
        "## Run config",
        "",
    ]
    for key in (
        "reliability_mode",
        "limit",
        "min_samples",
        "trusted_samples",
        "max_window",
        "fixed_as_of",
        "has_static_reliability",
        "has_source_outcome_ledger",
    ):
        lines.append(f"- {key}: {_markdown_value(run_config.get(key))}")
    lines.extend(
        [
            "",
            "## Source confidence overview",
            "",
            f"- rows: {_markdown_value(summary.get('row_count'))}",
            f"- sources used: {_markdown_value(summary.get('sources_used_count'))}",
            f"- sources excluded: {_markdown_value(summary.get('sources_excluded_count'))}",
            f"- no source data rows: {_markdown_value(summary.get('no_source_data'))}",
            f"- insufficient history rows: {_markdown_value(summary.get('insufficient_history'))}",
            "",
            "## Grade counts",
            "",
        ]
    )
    lines.extend(_markdown_bullets(summary.get("grade_counts")))
    lines.extend(["", "## Recommended action counts", ""])
    lines.extend(_markdown_bullets(summary.get("recommended_action_counts")))
    lines.extend(["", "## Blocker/reason counts", ""])
    lines.extend(_markdown_bullets(summary.get("reason_counts")))
    lines.extend(["", "## Source exclusion reason counts", ""])
    lines.extend(_markdown_bullets(summary.get("source_exclusion_reason_counts")))
    lines.extend(["", "## Source used vote counts", ""])
    lines.extend(_markdown_bullets(summary.get("source_used_vote_counts")))
    lines.extend(["", "## Source used tier counts", ""])
    lines.extend(_markdown_bullets(summary.get("source_used_tier_counts")))
    lines.extend(["", "## Source excluded tier counts", ""])
    lines.extend(_markdown_bullets(summary.get("source_excluded_tier_counts")))
    lines.extend(["", "## Per-source observation counts", ""])
    lines.extend(_markdown_bullets(summary.get("per_source_counts")))
    lines.extend(["", "## Per-source used counts", ""])
    lines.extend(_markdown_bullets(summary.get("per_source_used_counts")))
    lines.append("")
    return "\n".join(lines)


def _markdown_bullets(value: Any) -> list[str]:
    if not isinstance(value, dict) or not value:
        return ["- none"]
    return [f"- {key}: {_markdown_value(count)}" for key, count in sorted(value.items(), key=lambda item: str(item[0]))]


def _markdown_value(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)

def _resolve_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def _load_jsonl(path: Path, *, limit: int | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if limit is not None and len(rows) >= limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
