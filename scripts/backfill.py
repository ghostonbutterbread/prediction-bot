#!/usr/bin/env python3
"""Unified read-only backfill entrypoint for Prediction Bot derived artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.agent_decision_backfill import backfill_legacy_agent_decisions  # noqa: E402
from bot.prediction_lab_backfill import (  # noqa: E402
    DEFAULT_ANALYSIS_DIR,
    DEFAULT_PREDICTION_LAB_DIR,
    run_prediction_lab_backfill,
    run_prediction_lab_canonical_analysis,
)
from bot.scoreboard_resolution_backfill import backfill_scoreboard_resolutions  # noqa: E402


DEFAULT_SOURCE_SCOREBOARD_INPUT = "data/beta_shadow/paper/source_scoreboard/paper_shadow_lane_decisions.jsonl"
LANE_DEFAULT_INPUTS = {
    "shadow_source_scoreboard": DEFAULT_SOURCE_SCOREBOARD_INPUT,
    "source_scoreboard": DEFAULT_SOURCE_SCOREBOARD_INPUT,
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--kind",
        required=True,
        choices=[
            "prediction-lab",
            "agent-decisions",
            "scoreboard-resolutions",
            "source-scoreboard-resolutions",
        ],
        help="Backfill family to run.",
    )
    parser.add_argument(
        "--lane",
        default="shadow_source_scoreboard",
        help="Lane/profile selector for lane-specific backfills. Used by scoreboard-resolutions.",
    )
    parser.add_argument("inputs", nargs="*", help="Input JSONL paths. Defaults depend on --kind/--lane.")

    parser.add_argument("--input", dest="single_input", default=None, help="Single input path for prediction-lab mode.")
    parser.add_argument("--output-dir", default=None, help="Output directory for prediction-lab or agent-decisions modes.")
    parser.add_argument("--output", default=None, help="Output JSONL path for resolution backfills.")
    parser.add_argument("--report-output", default=None, help="Optional JSON report path for resolution backfills.")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Console output format.")

    parser.add_argument("--limit", type=int, default=None, help="Maximum input rows or markets to process, depending on kind.")
    parser.add_argument("--tail", action="store_true", help="Use latest valid Prediction Lab input rows instead of first rows.")
    parser.add_argument("--inventory-only", action="store_true", help="Prediction Lab: only write backfill_report.json.")
    parser.add_argument("--artifact-recovery", action="store_true", help="Prediction Lab: recover fields already present in artifacts.")
    parser.add_argument("--resolutions", action="append", default=None, help="Prediction Lab: optional resolutions.jsonl join path.")
    parser.add_argument("--canonical-analysis", action="store_true", help="Prediction Lab: build canonical analysis outputs.")
    parser.add_argument("--analysis-dir", default=str(DEFAULT_ANALYSIS_DIR), help="Prediction Lab canonical analysis output directory.")
    parser.add_argument(
        "--prediction-lab-dir",
        default=str(DEFAULT_PREDICTION_LAB_DIR),
        help="Prediction Lab directory containing raw ledgers.",
    )
    parser.add_argument("--market-snapshots", default=None, help="Prediction Lab canonical analysis market snapshots override.")
    parser.add_argument("--predictions", default=None, help="Prediction Lab canonical analysis predictions override.")
    parser.add_argument("--include-predictions", action="store_true", help="Prediction Lab: include predictions.jsonl.")
    parser.add_argument("--validate-output", dest="validate_output", action="store_true", help="Prediction Lab: validate output.")
    parser.add_argument("--no-validate-output", dest="validate_output", action="store_false", help="Prediction Lab: skip validation.")
    parser.set_defaults(validate_output=None)

    parser.add_argument(
        "--include-unresolved",
        action="store_true",
        help="Resolution backfills: also write unresolved market rows with null outcomes.",
    )
    parser.add_argument(
        "--max-markets",
        type=int,
        default=None,
        help="Resolution backfills: optional cap for smoke/backfill slices. Overrides --limit.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.kind == "prediction-lab":
        return _run_prediction_lab(args)
    if args.kind == "agent-decisions":
        return _run_agent_decisions(args)
    if args.kind in {"scoreboard-resolutions", "source-scoreboard-resolutions"}:
        return _run_scoreboard_resolutions(args)
    raise SystemExit(f"unsupported --kind: {args.kind}")


def _run_prediction_lab(args: argparse.Namespace) -> int:
    if args.canonical_analysis:
        result = run_prediction_lab_canonical_analysis(
            prediction_lab_dir=args.prediction_lab_dir,
            analysis_dir=args.analysis_dir,
            market_snapshots_path=args.market_snapshots,
            predictions_path=args.predictions,
            include_predictions=args.include_predictions,
            limit=args.limit,
            tail=args.tail,
            validate_output=True if args.validate_output is None else args.validate_output,
            resolution_paths=args.resolutions,
        )
    else:
        input_path = args.single_input or _single_positional_input(args)
        if not input_path or not args.output_dir:
            raise SystemExit("--input/positional input and --output-dir are required for --kind prediction-lab")
        result = run_prediction_lab_backfill(
            input_path,
            args.output_dir,
            limit=args.limit,
            tail=args.tail,
            inventory_only=args.inventory_only,
            artifact_recovery=args.artifact_recovery,
            resolution_paths=args.resolutions,
        )
    print(json.dumps(result.report, indent=2, sort_keys=True))
    if result.validation and not result.validation.get("skipped", False) and not result.validation.get("ok", False):
        return 2
    return 0


def _run_agent_decisions(args: argparse.Namespace) -> int:
    inputs = _input_paths(args)
    if not inputs or not args.output_dir:
        raise SystemExit("positional inputs and --output-dir are required for --kind agent-decisions")
    result = backfill_legacy_agent_decisions(inputs, output_dir=args.output_dir)
    print(json.dumps(result.report, indent=2, sort_keys=True))
    return 0


def _run_scoreboard_resolutions(args: argparse.Namespace) -> int:
    inputs = _input_paths(args) or [_default_lane_input(args.lane)]
    output_path = _safe_derived_output_path(args.output or _default_resolution_output_path(args.lane))
    report_path = (
        _safe_derived_output_path(args.report_output)
        if args.report_output
        else output_path.with_suffix(output_path.suffix + ".report.json")
    )
    result = backfill_scoreboard_resolutions(
        [_repo_path(path) for path in inputs],
        output_path=output_path,
        report_path=report_path,
        include_unresolved=args.include_unresolved,
        max_markets=args.max_markets if args.max_markets is not None else args.limit,
    )
    if args.format == "json":
        print(json.dumps(result.report, indent=2, sort_keys=True))
    else:
        print(_format_scoreboard_resolution_report(result.report, lane=args.lane))
    return 0


def _input_paths(args: argparse.Namespace) -> list[str]:
    values = list(args.inputs or [])
    if args.single_input:
        values.insert(0, args.single_input)
    return values


def _single_positional_input(args: argparse.Namespace) -> str | None:
    inputs = _input_paths(args)
    if not inputs:
        return None
    if len(inputs) > 1:
        raise SystemExit("--kind prediction-lab accepts one input unless --canonical-analysis is set")
    return inputs[0]


def _default_lane_input(lane: str) -> str:
    try:
        return LANE_DEFAULT_INPUTS[lane]
    except KeyError as exc:
        known = ", ".join(sorted(LANE_DEFAULT_INPUTS))
        raise SystemExit(f"no default input for --lane {lane!r}; pass an explicit input path. Known defaults: {known}") from exc


def _default_resolution_output_path(lane: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_lane = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in lane).strip("_") or "lane"
    return f"data/summaries/{safe_lane}_resolution_backfill_{stamp}.jsonl"


def _repo_path(path: str | Path) -> Path:
    raw = Path(path)
    return raw if raw.is_absolute() else ROOT / raw


def _safe_derived_output_path(raw_path: str | Path) -> Path:
    path = _repo_path(raw_path).resolve()
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


def _format_scoreboard_resolution_report(report: dict[str, object], *, lane: str) -> str:
    return "\n".join(
        [
            f"Backfill kind=scoreboard-resolutions lane={lane} "
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
