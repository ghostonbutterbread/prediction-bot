#!/usr/bin/env python3
"""Run multiple read-only paper shadow lane composition replays.

The sweep CLI is an orchestration layer around
``scripts.paper_shadow_lane_compose_replay.compose_lane_replay``. It writes only
derived replay artifacts and never mutates paper/live wallets or accounting.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.file_ops import load_jsonl  # noqa: E402
from scripts.paper_shadow_lane_compose_replay import (  # noqa: E402
    DEFAULT_LANE_DECISION_PATH,
    DEFAULT_OUTPUT_ROOT,
    SAFE_OUTPUT_ROOTS,
    _load_config,
    _markdown_report,
    _root_path,
    _slug,
    _summary_payload,
    compose_lane_replay,
)

CONFIG_SUFFIXES = (".yaml", ".yml", ".json")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lane-decision-path", default=DEFAULT_LANE_DECISION_PATH)
    parser.add_argument("--resolution-path", default=None)
    parser.add_argument(
        "--composition-config",
        action="append",
        default=[],
        help="YAML/JSON composition config. Repeat to compare multiple compositions.",
    )
    parser.add_argument(
        "--composition-dir",
        action="append",
        default=[],
        help="Directory containing YAML/JSON composition configs. Repeat to load multiple directories.",
    )
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--format", choices=["text", "json"], default="text")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_paths = _composition_config_paths(args.composition_config, args.composition_dir)
    if not config_paths:
        raise SystemExit("At least one --composition-config or --composition-dir is required")

    lane_rows = load_jsonl(_root_path(args.lane_decision_path))
    resolution_rows = load_jsonl(_root_path(args.resolution_path)) if args.resolution_path else []
    output_dir = _sweep_output_dir(args.output_dir)

    result = run_composition_sweep(
        lane_rows=lane_rows,
        resolution_rows=resolution_rows,
        config_paths=config_paths,
        output_dir=output_dir,
    )

    if args.format == "json":
        print(json.dumps(result["summary"], indent=2, sort_keys=True))
    else:
        print(_text_report(result["summary"]))
        print(f"output_dir={output_dir.relative_to(ROOT)}")
    return 0


def run_composition_sweep(
    *,
    lane_rows: Iterable[Mapping[str, Any]],
    resolution_rows: Iterable[Mapping[str, Any]] = (),
    config_paths: Iterable[Path],
    output_dir: Path,
) -> dict[str, Any]:
    """Run each composition config and write per-composition plus aggregate artifacts."""

    lane_rows = list(lane_rows)
    resolution_rows = list(resolution_rows)
    output_dir = _ensure_safe_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    composition_summaries: list[dict[str, Any]] = []
    for config_path in config_paths:
        config = _load_config(config_path)
        result = compose_lane_replay(lane_rows=lane_rows, resolution_rows=resolution_rows, config=config)
        composition_name = str(result["composition"]["name"])
        composition_dir = _unique_child_dir(output_dir, _slug(composition_name))
        _write_composition_artifacts(composition_dir, result)
        composition_summaries.append(
            {
                "name": composition_name,
                "config_path": _display_path(config_path),
                "output_dir": _display_path(composition_dir),
                **_aggregate_fields(result["summary"]),
            }
        )

    summary = _aggregate_summary(composition_summaries, output_dir)
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output_dir / "report.md").write_text(_markdown_sweep_report(summary), encoding="utf-8")
    return {"summary": summary, "composition_summaries": composition_summaries, "output_dir": output_dir}


def _composition_config_paths(configs: Iterable[str], dirs: Iterable[str]) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()
    for raw in configs:
        path = _root_path(raw).resolve()
        if path not in seen:
            paths.append(path)
            seen.add(path)
    for raw_dir in dirs:
        directory = _root_path(raw_dir).resolve()
        if not directory.is_dir():
            raise ValueError(f"Composition directory does not exist: {directory}")
        for path in sorted(directory.iterdir()):
            if path.is_file() and path.suffix.lower() in CONFIG_SUFFIXES and path.resolve() not in seen:
                paths.append(path.resolve())
                seen.add(path.resolve())
    return paths


def _write_composition_artifacts(output_dir: Path, result: Mapping[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "composition_rows.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in result["composition_rows"]),
        encoding="utf-8",
    )
    (output_dir / "resolved_rows.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in result["resolved_rows"]),
        encoding="utf-8",
    )
    (output_dir / "summary.json").write_text(
        json.dumps(_summary_payload(result), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "report.md").write_text(_markdown_report(result), encoding="utf-8")


def _aggregate_summary(composition_summaries: list[dict[str, Any]], output_dir: Path) -> dict[str, Any]:
    diagnostics: Counter[str] = Counter()
    blockers: Counter[str] = Counter()
    for row in composition_summaries:
        diagnostics.update(row.get("diagnostics", {}))
        blockers.update(row.get("blocker_counts", {}))

    return {
        "schema_name": "paper_shadow_lane_composition_sweep_summary",
        "schema_version": 1,
        "non_mutating": True,
        "output_dir": _display_path(output_dir),
        "composition_count": len(composition_summaries),
        "compositions": composition_summaries,
        "aggregate": {
            "diagnostics": dict(sorted(diagnostics.items())),
            "blocker_counts": dict(sorted(blockers.items())),
            "best_total_pnl_usd": _best(composition_summaries, "total_pnl_usd"),
            "best_roi_pct": _best(composition_summaries, "roi_pct"),
        },
    }


def _aggregate_fields(summary: Mapping[str, Any]) -> dict[str, Any]:
    pnl = summary.get("pnl") if isinstance(summary.get("pnl"), Mapping) else {}
    return {
        "candidate_groups": summary.get("candidate_groups", 0),
        "composed_rows": summary.get("composed_rows", 0),
        "diagnostics": dict(summary.get("diagnostics", {}) or {}),
        "duplicate_lane_rows": dict(summary.get("duplicate_lane_rows", {}) or {}),
        "resolved_rows": pnl.get("resolved_rows", 0),
        "buy_rows": pnl.get("buy_rows", 0),
        "skip_rows": pnl.get("skip_rows", 0),
        "winning_buy_rows": pnl.get("winning_buy_rows", 0),
        "losing_buy_rows": pnl.get("losing_buy_rows", 0),
        "pnl_calculable_rows": pnl.get("pnl_calculable_rows", 0),
        "total_stake_usd": pnl.get("total_stake_usd", 0.0),
        "total_pnl_usd": pnl.get("total_pnl_usd", 0.0),
        "roi_pct": pnl.get("roi_pct"),
        "blocker_counts": dict(pnl.get("blocker_counts", {}) or {}),
    }


def _best(rows: list[dict[str, Any]], field: str) -> dict[str, Any] | None:
    eligible = [row for row in rows if isinstance(row.get(field), int | float)]
    if not eligible:
        return None
    best = max(eligible, key=lambda row: float(row[field]))
    return {
        "name": best["name"],
        "config_path": best.get("config_path"),
        "output_dir": best.get("output_dir"),
        field: best[field],
    }


def _sweep_output_dir(raw: str | None) -> Path:
    if raw:
        output = _root_path(raw).resolve()
    else:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = (ROOT / DEFAULT_OUTPUT_ROOT / f"composition_sweep_{timestamp}").resolve()
    return _ensure_safe_output_dir(output)


def _ensure_safe_output_dir(output: Path) -> Path:
    output = output.resolve()
    safe_roots = [root.resolve() for root in SAFE_OUTPUT_ROOTS]
    if not any(output == root or root in output.parents for root in safe_roots):
        raise ValueError("Output directory must be under data/summaries, data/beta_shadow/summaries, or data/derived_reports")
    return output


def _unique_child_dir(parent: Path, slug: str) -> Path:
    candidate = parent / slug
    if not candidate.exists():
        return candidate
    index = 2
    while True:
        candidate = parent / f"{slug}_{index}"
        if not candidate.exists():
            return candidate
        index += 1


def _display_path(path: Path) -> str:
    path = path.resolve()
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _text_report(summary: Mapping[str, Any]) -> str:
    lines = [
        f"Composition sweep compositions={summary['composition_count']}",
        f"aggregate_diagnostics={json.dumps(summary['aggregate']['diagnostics'], sort_keys=True)}",
        f"aggregate_blockers={json.dumps(summary['aggregate']['blocker_counts'], sort_keys=True)}",
    ]
    for row in summary["compositions"]:
        lines.append(
            f"{row['name']}: rows={row['composed_rows']} buys={row['buy_rows']} "
            f"pnl=${row['total_pnl_usd']} roi={row['roi_pct']}% diagnostics={json.dumps(row['diagnostics'], sort_keys=True)}"
        )
    return "\n".join(lines)


def _markdown_sweep_report(summary: Mapping[str, Any]) -> str:
    return _text_report(summary) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
