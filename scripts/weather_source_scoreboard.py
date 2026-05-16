#!/usr/bin/env python3
"""Build offline weather source reliability scoreboard artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.weather.source_reliability import build_source_outcome_ledger_rows  # noqa: E402
from bot.weather.source_scoreboard import build_source_scoreboard, load_jsonl_rows  # noqa: E402


def parse_args() -> argparse.Namespace:
    return parse_args_from(sys.argv[1:])


def parse_args_from(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        action="append",
        required=True,
        help="Explicit Prediction Lab/Paper JSONL input path. Repeat for multiple files.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory where source scoreboard artifacts will be written.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional maximum number of JSON object rows to load across all inputs.",
    )
    parser.add_argument(
        "--ledger-output",
        default=None,
        help="Optional JSONL path where source-outcome ledger rows will be written.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args() if argv is None else parse_args_from(argv)
    if args.limit is not None and args.limit < 0:
        raise SystemExit("--limit must be non-negative")

    input_paths = [_resolve_path(path) for path in args.input]
    output_dir = _resolve_path(args.output_dir)
    ledger_output = _resolve_path(args.ledger_output) if args.ledger_output else None
    output_dir.mkdir(parents=True, exist_ok=True)

    rows, load_stats = load_jsonl_rows(input_paths, limit=args.limit)
    scoreboard = build_source_scoreboard(rows)
    ledger_rows: list[dict[str, Any]] | None = None
    if ledger_output is not None:
        ledger_output.parent.mkdir(parents=True, exist_ok=True)
        ledger_rows = build_source_outcome_ledger_rows(
            rows,
            source_row_path=str(input_paths[0]) if len(input_paths) == 1 else None,
        )
        _write_jsonl(ledger_output, ledger_rows)

    metadata = {
        "schema_version": scoreboard["schema_version"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inputs": [str(path) for path in input_paths],
        "output_dir": str(output_dir),
        "limit": args.limit,
        "load_stats": load_stats,
        "scoreboard_summary": scoreboard["summary"],
        "mode": "offline_report_only",
        "network_access": False,
    }
    if ledger_output is not None:
        metadata["ledger_output"] = str(ledger_output)
        metadata["ledger_rows"] = len(ledger_rows) if ledger_rows is not None else 0

    _write_json(output_dir / "source_scoreboard.json", scoreboard)
    _write_jsonl(output_dir / "source_scoreboard_by_slice.jsonl", scoreboard["slices"])
    (output_dir / "best_slices.md").write_text(
        _render_slices_markdown("Best Slices", _best_slices(scoreboard["slices"])),
        encoding="utf-8",
    )
    (output_dir / "worst_slices.md").write_text(
        _render_slices_markdown("Worst Slices", _worst_slices(scoreboard["slices"])),
        encoding="utf-8",
    )
    _write_json(output_dir / "run_metadata.json", metadata)

    summary_parts = [
        "Weather source scoreboard",
        f"rows={load_stats.get('rows_loaded', 0)}",
        f"observations={scoreboard['summary'].get('observations_extracted', 0)}",
        f"scored={scoreboard['summary'].get('observations_scored', 0)}",
        f"slices={scoreboard['summary'].get('slice_count', 0)}",
    ]
    if ledger_rows is not None:
        summary_parts.append(f"ledger_rows={len(ledger_rows)}")
    summary_parts.append(f"output={output_dir}")
    print(" ".join(summary_parts))
    return 0


def _resolve_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def _best_slices(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            row.get("threshold_direction_accuracy") is not None,
            row.get("threshold_direction_accuracy") or -1,
            row.get("within_3f_rate") or -1,
            -(row.get("mae") or 999999),
            row.get("sample_count") or 0,
        ),
        reverse=True,
    )[:25]


def _worst_slices(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scored = [row for row in rows if row.get("sample_count")]
    return sorted(
        scored,
        key=lambda row: (
            row.get("threshold_direction_accuracy") is None,
            row.get("threshold_direction_accuracy") if row.get("threshold_direction_accuracy") is not None else 2,
            -(row.get("mae") or 0),
            -(row.get("sample_count") or 0),
        ),
    )[:25]


def _render_slices_markdown(title: str, rows: list[dict[str, Any]]) -> str:
    lines = [
        f"# {title}",
        "",
        "| source | city | kind | shape | samples | mae | bias | dir_acc | within_3f |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    if not rows:
        lines.append("| none | none | none | none | 0 | n/a | n/a | n/a | n/a |")
    for row in rows:
        lines.append(
            "| "
            f"{_cell(row.get('source_name'))} | "
            f"{_cell(row.get('city_id'))} | "
            f"{_cell(row.get('market_kind'))} | "
            f"{_cell(row.get('contract_shape'))} | "
            f"{row.get('sample_count') or 0} | "
            f"{_metric(row.get('mae'))} | "
            f"{_metric(row.get('mean_bias'))} | "
            f"{_metric(row.get('threshold_direction_accuracy'))} | "
            f"{_metric(row.get('within_3f_rate'))} |"
        )
    lines.append("")
    return "\n".join(lines)


def _cell(value: Any) -> str:
    return str(value if value not in (None, "") else "unknown").replace("|", "/")


def _metric(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
