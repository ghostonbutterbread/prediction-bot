#!/usr/bin/env python3
"""Validate weather source scoreboard rows against finalized Kalshi outcomes.

Offline/read-only evaluator: consumes a source-outcome ledger plus an explicit
market outcome lookup, then writes per-observation edge rows and source/city
summary artifacts. It does not fetch network data or mutate paper/live state.
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

from bot.weather.source_reliability import (  # noqa: E402
    build_source_edge_evaluation_rows,
    summarize_source_edge_evaluation_rows,
)


def parse_args() -> argparse.Namespace:
    return parse_args_from(sys.argv[1:])


def parse_args_from(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ledger-input",
        action="append",
        required=True,
        help="Source-outcome ledger JSONL path. Repeat for multiple files.",
    )
    parser.add_argument(
        "--outcome-input",
        action="append",
        required=True,
        help="Finalized market outcome lookup JSON/JSONL path. Repeat for multiple files.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory where edge validation artifacts will be written.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Optional max ledger rows to load.")
    parser.add_argument("--report-limit", type=int, default=25, help="Markdown slice row limit.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args() if argv is None else parse_args_from(argv)
    if args.limit is not None and args.limit < 0:
        raise SystemExit("--limit must be non-negative")
    if args.report_limit < 0:
        raise SystemExit("--report-limit must be non-negative")

    ledger_paths = [_resolve_path(path) for path in args.ledger_input]
    outcome_paths = [_resolve_path(path) for path in args.outcome_input]
    output_dir = _resolve_path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    ledger_rows, ledger_stats = _load_jsonl_rows(ledger_paths, limit=args.limit)
    outcome_lookup, outcome_stats = _load_outcome_lookup(outcome_paths)
    edge_rows = build_source_edge_evaluation_rows(ledger_rows, outcome_lookup=outcome_lookup)
    summary = summarize_source_edge_evaluation_rows(edge_rows)

    metadata = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "offline_edge_validation_only",
        "network_access": False,
        "ledger_inputs": [str(path) for path in ledger_paths],
        "outcome_inputs": [str(path) for path in outcome_paths],
        "output_dir": str(output_dir),
        "limit": args.limit,
        "report_limit": args.report_limit,
        "ledger_load_stats": ledger_stats,
        "outcome_load_stats": outcome_stats,
        "summary": summary["summary"],
        "artifacts": [
            "source_edge_evaluation_rows.jsonl",
            "source_edge_summary.json",
            "source_edge_slices.jsonl",
            "source_edge_report.md",
            "run_metadata.json",
        ],
    }

    _write_jsonl(output_dir / "source_edge_evaluation_rows.jsonl", edge_rows)
    _write_json(output_dir / "source_edge_summary.json", summary)
    _write_jsonl(output_dir / "source_edge_slices.jsonl", summary["slices"])
    (output_dir / "source_edge_report.md").write_text(
        render_source_edge_report_markdown(summary, metadata=metadata, limit=args.report_limit),
        encoding="utf-8",
    )
    _write_json(output_dir / "run_metadata.json", metadata)

    print(
        "Weather source edge validation "
        f"ledger_rows={ledger_stats['rows_loaded']} "
        f"outcomes={outcome_stats['outcomes_loaded']} "
        f"eligible={summary['summary'].get('eligible_rows', 0)} "
        f"blocked={summary['summary'].get('blocked_rows', 0)} "
        f"slices={summary['summary'].get('source_slice_count', 0)} "
        f"output={output_dir}"
    )
    return 0


def render_source_edge_report_markdown(
    summary: Mapping[str, Any],
    *,
    metadata: Mapping[str, Any] | None = None,
    limit: int = 25,
) -> str:
    run = dict(metadata or {})
    summary_counts = summary.get("summary") if isinstance(summary.get("summary"), Mapping) else {}
    slices = [row for row in summary.get("slices", []) if isinstance(row, Mapping)]
    ranked = sorted(
        slices,
        key=lambda row: (row.get("eligible_count") or 0, row.get("avg_binary_edge_realized") if row.get("avg_binary_edge_realized") is not None else -999),
        reverse=True,
    )[: max(0, limit)]
    lines = [
        "# Weather Source Edge Validation Report",
        "",
        "Offline scoreboard evaluator. This report compares source-implied sides against explicit finalized market outcomes and observed source-side prices. It is not a trading lane and does not mutate paper/live accounting.",
        "",
        "## Run Metadata",
        "",
        f"- generated_at: {_markdown_cell(run.get('generated_at') or summary.get('generated_at'))}",
        f"- mode: {_markdown_cell(run.get('mode'))}",
        f"- network_access: {_markdown_cell(run.get('network_access'))}",
        f"- ledger_inputs: {len(run.get('ledger_inputs') or [])}",
        f"- outcome_inputs: {len(run.get('outcome_inputs') or [])}",
        "",
        "## Summary",
        "",
        "| input_rows | eligible | blocked | slices |",
        "|---:|---:|---:|---:|",
        f"| {summary_counts.get('input_rows') or 0} | {summary_counts.get('eligible_rows') or 0} | {summary_counts.get('blocked_rows') or 0} | {summary_counts.get('source_slice_count') or 0} |",
        "",
        "## Blockers",
        "",
    ]
    reason_counts = summary_counts.get("reason_counts") if isinstance(summary_counts.get("reason_counts"), Mapping) else {}
    if reason_counts:
        for key, count in sorted(reason_counts.items()):
            lines.append(f"- {key}: {count}")
    else:
        lines.append("- none: 0")
    lines.extend([
        "",
        "## Source Edge Slices",
        "",
        "| source | city | kind | shape | eligible | blocked | win_rate | avg_price | avg_edge | flat_1usd_pnl |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|",
    ])
    if not ranked:
        lines.append("| none | none | none | none | 0 | 0 | n/a | n/a | n/a | n/a |")
    for row in ranked:
        lines.append(
            "| "
            f"{_markdown_cell(row.get('source_name') or row.get('source_id'))} | "
            f"{_markdown_cell(row.get('city_id'))} | "
            f"{_markdown_cell(row.get('market_kind'))} | "
            f"{_markdown_cell(row.get('contract_shape'))} | "
            f"{row.get('eligible_count') or 0} | "
            f"{row.get('blocked_count') or 0} | "
            f"{_format_metric(row.get('win_rate'))} | "
            f"{_format_metric(row.get('avg_source_side_price'))} | "
            f"{_format_metric(row.get('avg_binary_edge_realized'))} | "
            f"{_format_metric(row.get('flat_1usd_pnl'))} |"
        )
    lines.append("")
    return "\n".join(lines)


def _load_jsonl_rows(paths: Iterable[Path], *, limit: int | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    files_loaded = 0
    invalid_rows = 0
    for path in paths:
        files_loaded += 1
        with path.open(encoding="utf-8") as fh:
            for line_number, line in enumerate(fh, start=1):
                if limit is not None and len(rows) >= limit:
                    break
                text = line.strip()
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSONL row {path}:{line_number}: {exc}") from exc
                if isinstance(payload, dict):
                    rows.append(payload)
                else:
                    invalid_rows += 1
            if limit is not None and len(rows) >= limit:
                break
    return rows, {"files_loaded": files_loaded, "rows_loaded": len(rows), "invalid_rows": invalid_rows, "limit_applied": limit}


def _load_outcome_lookup(paths: Iterable[Path]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    files_loaded = 0
    rows_seen = 0
    rows_without_market_id = 0
    for path in paths:
        files_loaded += 1
        payloads = _read_outcome_payloads(path)
        for payload in payloads:
            rows_seen += 1
            if not isinstance(payload, Mapping):
                continue
            market_id = _market_id_from_payload(payload)
            if not market_id:
                rows_without_market_id += 1
                continue
            lookup[market_id] = dict(payload)
    return lookup, {
        "files_loaded": files_loaded,
        "rows_seen": rows_seen,
        "outcomes_loaded": len(lookup),
        "rows_without_market_id": rows_without_market_id,
    }


def _read_outcome_payloads(path: Path) -> list[Any]:
    if path.suffix.lower() == ".jsonl":
        rows: list[Any] = []
        with path.open(encoding="utf-8") as fh:
            for line_number, line in enumerate(fh, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    rows.append(json.loads(text))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSONL row {path}:{line_number}: {exc}") from exc
        return rows
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        for key in ("outcomes", "markets", "rows", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return [payload]
    return []


def _market_id_from_payload(payload: Mapping[str, Any]) -> str | None:
    for key in ("market_id", "ticker", "market_ticker", "id"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    market = payload.get("market")
    if isinstance(market, Mapping):
        for key in ("market_id", "ticker", "id"):
            value = market.get(key)
            if value not in (None, ""):
                return str(value)
    return None


def _resolve_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        json.dump(dict(payload), fh, indent=2, sort_keys=True)
        fh.write("\n")


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(dict(row), sort_keys=True) + "\n")


def _markdown_cell(value: Any) -> str:
    text = "n/a" if value in (None, "") else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _format_metric(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _markdown_cell(value)
    return f"{number:.4f}".rstrip("0").rstrip(".")


if __name__ == "__main__":
    raise SystemExit(main())
