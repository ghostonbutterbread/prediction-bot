#!/usr/bin/env python3
"""Chunked source-router/stable-size composition replay.

Builds the derived `source_router_side_stable_size` lane in chunks so the large
shadow lane ledger can be replayed without loading the full file into memory.
The output is read-only derived reporting and does not mutate wallets or
accounting state.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.resolution_feed import run_resolution_feed_once  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lane-decision-path",
        default="data/beta_shadow/paper/source_router_low_sample/paper_shadow_lane_decisions.jsonl",
    )
    parser.add_argument(
        "--resolution-path",
        default="data/beta_shadow/resolution_feed/source_router_low_sample/latest_resolutions.jsonl",
    )
    parser.add_argument("--composition-config", default="lane_compositions/source_router_side_stable_size.yaml")
    parser.add_argument("--output-dir", default="data/summaries/lane_compositions/source_router_side_stable_size_latest_chunked")
    parser.add_argument("--chunk-size", type=int, default=5000)
    parser.add_argument(
        "--refresh-resolution",
        action="store_true",
        help="Fetch a fresh derived finalized-outcome JSONL before replaying.",
    )
    parser.add_argument(
        "--resolution-output",
        default=None,
        help="Output path for --refresh-resolution. Defaults under the replay output directory.",
    )
    parser.add_argument("--max-fetch-attempts", type=int, default=4)
    parser.add_argument("--retry-delay-seconds", type=float, default=3.0)
    parser.add_argument("--max-resolution-markets", type=int, default=None, help="Optional cap for resolution-refresh smoke runs.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_root = _root_path(args.output_dir)
    input_path = _root_path(args.lane_decision_path)
    output_root.mkdir(parents=True, exist_ok=True)
    resolution_path = _refresh_resolution_path(args, input_path=input_path, output_root=output_root)
    status_path = output_root / "chunk_status.jsonl"
    status_path.write_text("", encoding="utf-8")

    summaries: list[dict[str, Any]] = []
    rows: list[str] = []
    idx = 0
    with input_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            rows.append(line)
            if len(rows) >= args.chunk_size:
                idx += 1
                summaries.append(
                    _run_chunk(
                        idx=idx,
                        rows=rows,
                        output_root=output_root,
                        status_path=status_path,
                        resolution_path=resolution_path,
                        composition_config=args.composition_config,
                    )
                )
                rows = []
    if rows:
        idx += 1
        summaries.append(
            _run_chunk(
                idx=idx,
                rows=rows,
                output_root=output_root,
                status_path=status_path,
                resolution_path=resolution_path,
                composition_config=args.composition_config,
            )
        )

    aggregate = _aggregate(summaries)
    (output_root / "aggregate_summary.json").write_text(json.dumps(aggregate, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output_root / "aggregate_report.md").write_text(_report(aggregate), encoding="utf-8")
    print(json.dumps(aggregate, indent=2, sort_keys=True))
    return 0


def _run_chunk(
    *,
    idx: int,
    rows: list[str],
    output_root: Path,
    status_path: Path,
    resolution_path: Path,
    composition_config: str,
) -> dict[str, Any]:
    chunk_path = output_root / f"chunk_{idx:04d}_rows.jsonl"
    out_dir = output_root / f"chunk_{idx:04d}"
    chunk_path.write_text("".join(rows), encoding="utf-8")
    cmd = [
        sys.executable,
        "scripts/paper_shadow_lane_composition_sweep.py",
        "--lane-decision-path",
        str(chunk_path),
        "--resolution-path",
        str(resolution_path),
        "--composition-config",
        composition_config,
        "--output-dir",
        str(out_dir),
        "--format",
        "json",
    ]
    env = {**os.environ, "PYTHONPATH": "."}
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, env=env)
    (output_root / f"chunk_{idx:04d}.stdout.json").write_text(proc.stdout, encoding="utf-8")
    (output_root / f"chunk_{idx:04d}.stderr.log").write_text(proc.stderr, encoding="utf-8")
    if proc.returncode:
        raise SystemExit(f"chunk {idx} failed rc={proc.returncode}: {proc.stderr[-1000:]}")

    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    composition = summary.get("compositions", [{}])[0]
    with status_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"chunk": idx, "rows": len(rows), "ok": True, "summary": composition}, sort_keys=True) + "\n")
    chunk_path.unlink(missing_ok=True)
    return composition


def _refresh_resolution_path(args: argparse.Namespace, *, input_path: Path, output_root: Path) -> Path:
    if not args.refresh_resolution:
        return _root_path(args.resolution_path)

    resolution_result = run_resolution_feed_once(
        {
            "resolution_feed": {
                "enabled": True,
                "decision_ledger_path": str(input_path),
                "output_dir": str(_root_path(args.resolution_output).parent if args.resolution_output else output_root),
                "interval_seconds": 1,
                "max_markets": args.max_resolution_markets,
                "max_fetch_attempts": args.max_fetch_attempts,
                "retry_delay_seconds": args.retry_delay_seconds,
            }
        },
        force=True,
    )
    latest_resolution = resolution_result.output_path
    if latest_resolution is None:
        raise SystemExit(f"resolution refresh failed status={resolution_result.status} reason={resolution_result.reason}")
    print(
        json.dumps(
            {
                "resolution_refresh": {
                    "output_path": _display_path(resolution_result.output_path),
                    "report_path": _display_path(resolution_result.report_path),
                    "resolved_market_count": resolution_result.resolved_market_count,
                    "unresolved_market_count": resolution_result.unresolved_market_count,
                    "fetch_error_count": resolution_result.fetch_error_count,
                    "market_ref_path": _display_path(resolution_result.market_ref_path),
                }
            },
            sort_keys=True,
        )
    )
    return latest_resolution


def _aggregate(summaries: list[Mapping[str, Any]]) -> dict[str, Any]:
    diagnostics: Counter[str] = Counter()
    blockers: Counter[str] = Counter()
    aggregate: dict[str, Any] = {
        "schema_name": "paper_shadow_lane_composition_chunked_aggregate",
        "schema_version": 1,
        "non_mutating": True,
        "composition": "source_router_side_stable_size",
        "chunk_count": len(summaries),
        "candidate_groups": 0,
        "composed_rows": 0,
        "buy_rows": 0,
        "skip_rows": 0,
        "resolved_rows": 0,
        "pnl_calculable_rows": 0,
        "winning_buy_rows": 0,
        "losing_buy_rows": 0,
        "total_stake_usd": 0.0,
        "total_pnl_usd": 0.0,
    }
    for summary in summaries:
        diagnostics.update(summary.get("diagnostics") or {})
        blockers.update(summary.get("blocker_counts") or {})
        for key in ("candidate_groups", "composed_rows", "buy_rows", "skip_rows", "resolved_rows", "pnl_calculable_rows", "winning_buy_rows", "losing_buy_rows"):
            aggregate[key] += int(summary.get(key) or 0)
        for key in ("total_stake_usd", "total_pnl_usd"):
            aggregate[key] += float(summary.get(key) or 0.0)
    aggregate["total_stake_usd"] = round(aggregate["total_stake_usd"], 4)
    aggregate["total_pnl_usd"] = round(aggregate["total_pnl_usd"], 4)
    aggregate["roi_pct"] = round((aggregate["total_pnl_usd"] / aggregate["total_stake_usd"]) * 100, 2) if aggregate["total_stake_usd"] else None
    aggregate["diagnostics"] = dict(sorted(diagnostics.items()))
    aggregate["blocker_counts"] = dict(sorted(blockers.items()))
    return aggregate


def _report(aggregate: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            "# source_router_side_stable_size Latest Chunked Replay",
            "",
            f"chunks={aggregate['chunk_count']} candidates={aggregate['candidate_groups']} rows={aggregate['composed_rows']}",
            f"buys={aggregate['buy_rows']} skips={aggregate['skip_rows']} resolved={aggregate['resolved_rows']} calculable={aggregate['pnl_calculable_rows']}",
            f"wins={aggregate['winning_buy_rows']} losses={aggregate['losing_buy_rows']} stake=${aggregate['total_stake_usd']} pnl=${aggregate['total_pnl_usd']} roi={aggregate['roi_pct']}%",
            f"blockers={aggregate['blocker_counts']}",
            "",
        ]
    )


def _root_path(raw: str | Path) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def _display_path(path: str | Path | None) -> str | None:
    if path is None:
        return None
    value = Path(path)
    try:
        return str(value.relative_to(ROOT))
    except ValueError:
        return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
