#!/usr/bin/env python3
"""Stream old Prediction Lab rows through the current beta-shadow replay logic.

This is an analysis runner: it avoids loading multi-GB JSONL files into memory,
writes compact comparison rows, and summarizes stable/original vs current shadow
logic action changes and resolved outcome effects.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import load_config
from bot.file_ops import load_jsonl, atomic_write_json
from bot.prediction_lab_replay import (
    ReplayArtifactInput,
    _legacy_artifact_from_row,
    _strip_artifact_outcomes,
    _strip_inline_outcomes,
    replay_recorded_artifacts,
)


def iter_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield line_no, json.loads(line)
            except json.JSONDecodeError:
                continue


def make_record(path: Path, line_no: int, row: dict[str, Any]) -> ReplayArtifactInput:
    replay_row = _strip_inline_outcomes(row)
    artifact = row.get("decision_artifact")
    if not isinstance(artifact, dict):
        artifact = _legacy_artifact_from_row(replay_row)
    return ReplayArtifactInput(
        row=replay_row,
        artifact=_strip_artifact_outcomes(artifact),
        source_path=str(path),
        line_number=line_no,
    )


def add_counter(dst: Counter, src: dict[str, Any] | None) -> None:
    if not isinstance(src, dict):
        return
    for k, v in src.items():
        if isinstance(v, (int, float)):
            dst[str(k)] += v


def compact_row(row: Any) -> dict[str, Any]:
    d = row.to_dict()
    return {
        "market_id": d.get("market_id"),
        "series": d.get("series"),
        "event_ticker": d.get("event_ticker"),
        "prediction_id": d.get("prediction_id"),
        "experiment_id": d.get("experiment_id"),
        "strategy_version": d.get("strategy_version"),
        "original_action": d.get("original_action"),
        "replayed_action": d.get("replayed_action"),
        "original_reason_code": d.get("original_reason_code"),
        "replayed_reason_code": d.get("replayed_reason_code"),
        "action_changed": d.get("action_changed"),
        "reason_changed": d.get("reason_changed"),
        "source_mode": d.get("source_mode"),
        "order_book_mode": d.get("order_book_mode"),
        "execution_snapshot_mode": d.get("execution_snapshot_mode"),
        "quality_category": d.get("category"),
        "include_in_strict": d.get("include_in_strict"),
        "outcome": d.get("outcome"),
        "missed_win": d.get("missed_win"),
        "bad_buy_removed": d.get("bad_buy_removed"),
        "bad_buy_added": d.get("bad_buy_added"),
        "source_path": d.get("source_path"),
        "line_number": d.get("line_number"),
    }


def summarize_compacts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    c = Counter()
    for r in rows:
        c["rows"] += 1
        c["action_changed"] += bool(r.get("action_changed"))
        c["reason_changed"] += bool(r.get("reason_changed"))
        c["strict_rows"] += bool(r.get("include_in_strict"))
        c["missed_wins"] += bool(r.get("missed_win"))
        c["bad_buys_removed"] += bool(r.get("bad_buy_removed"))
        c["bad_buys_added"] += bool(r.get("bad_buy_added"))
    return dict(c)


def run_dataset(
    *,
    name: str,
    path: Path,
    config: dict[str, Any],
    resolution_records: list[dict[str, Any]],
    output_dir: Path,
    batch_size: int,
    limit: int | None,
    live_source_policy: str,
    row_quality_policy: str,
) -> dict[str, Any]:
    output_rows = output_dir / f"{name}.compact_rows.jsonl"
    output_rows.parent.mkdir(parents=True, exist_ok=True)
    if output_rows.exists():
        output_rows.unlink()

    totals = Counter()
    quality_counts = Counter()
    source_modes = Counter()
    order_book_modes = Counter()
    outcomes = Counter()
    original_actions = Counter()
    replayed_actions = Counter()
    original_reasons = Counter()
    replayed_reasons = Counter()
    changed_pairs = Counter()
    compact_totals = Counter()
    errors: list[str] = []

    batch: list[ReplayArtifactInput] = []
    processed = 0

    def flush() -> None:
        nonlocal batch
        if not batch:
            return
        result = replay_recorded_artifacts(
            batch,
            config=config,
            resolution_records=resolution_records,
            live_source_policy=live_source_policy,
            row_quality_policy=row_quality_policy,
        )
        rows = [compact_row(r) for r in (result.all_rows or result.rows)]
        with output_rows.open("a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, separators=(",", ":")) + "\n")
        compact_totals.update(summarize_compacts(rows))
        for r in rows:
            quality_counts[str(r.get("quality_category"))] += 1
            source_modes[str(r.get("source_mode"))] += 1
            order_book_modes[str(r.get("order_book_mode"))] += 1
            outcomes[str(r.get("outcome"))] += 1
            original_actions[str(r.get("original_action"))] += 1
            replayed_actions[str(r.get("replayed_action"))] += 1
            original_reasons[str(r.get("original_reason_code"))] += 1
            replayed_reasons[str(r.get("replayed_reason_code"))] += 1
            if r.get("action_changed"):
                changed_pairs[f"{r.get('original_action')}->{r.get('replayed_action')}"] += 1
        batch = []

    for line_no, row in iter_jsonl(path):
        if limit is not None and processed >= limit:
            break
        try:
            batch.append(make_record(path, line_no, row))
            processed += 1
            if len(batch) >= batch_size:
                flush()
        except Exception as exc:  # keep sweeping other rows
            errors.append(f"{path}:{line_no}: {type(exc).__name__}: {exc}")
            if len(errors) > 50:
                errors = errors[-50:]
    flush()

    summary = {
        "dataset": name,
        "path": str(path),
        "processed_rows": processed,
        "compact_rows_path": str(output_rows),
        "compact_totals": dict(compact_totals),
        "quality_counts": dict(quality_counts),
        "source_modes": dict(source_modes),
        "order_book_modes": dict(order_book_modes),
        "outcomes": dict(outcomes),
        "original_actions": dict(original_actions),
        "replayed_actions": dict(replayed_actions),
        "original_reason_top": dict(original_reasons.most_common(20)),
        "replayed_reason_top": dict(replayed_reasons.most_common(20)),
        "changed_pairs": dict(changed_pairs),
        "errors_tail": errors[-20:],
    }
    atomic_write_json(output_dir / f"{name}.summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.paper_beta_shadow_weather.yaml")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--batch-size", type=int, default=250)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--live-source-policy", default="warn_skip", choices=["fail", "warn_skip", "allow"])
    parser.add_argument("--row-quality-policy", default="annotate", choices=["annotate", "include_all", "strict", "drop_incomplete", "strict_only"])
    parser.add_argument("--resolution", action="append", default=[])
    parser.add_argument("inputs", nargs="+")
    args = parser.parse_args()

    config = load_config(Path(args.config))
    output_dir = Path(args.output_dir or f"data/summaries/shadow_replay_sweep")
    output_dir.mkdir(parents=True, exist_ok=True)
    resolution_records: list[dict[str, Any]] = []
    for rp in args.resolution:
        resolution_records.extend(load_jsonl(Path(rp)))

    dataset_summaries = []
    for input_value in args.inputs:
        path = Path(input_value)
        if not path.exists():
            dataset_summaries.append({"dataset": path.stem, "path": str(path), "error": "missing"})
            continue
        safe_name = path.parent.as_posix().replace("/", "__").strip("_") + "__" + path.stem
        summary = run_dataset(
            name=safe_name,
            path=path,
            config=config,
            resolution_records=resolution_records,
            output_dir=output_dir,
            batch_size=args.batch_size,
            limit=args.limit,
            live_source_policy=args.live_source_policy,
            row_quality_policy=args.row_quality_policy,
        )
        dataset_summaries.append(summary)
        print(json.dumps({"finished": safe_name, "processed_rows": summary.get("processed_rows"), "compact_totals": summary.get("compact_totals")}, sort_keys=True))
        sys.stdout.flush()

    aggregate = {
        "config": args.config,
        "resolution_rows": len(resolution_records),
        "dataset_count": len(dataset_summaries),
        "datasets": dataset_summaries,
    }
    atomic_write_json(output_dir / "aggregate_summary.json", aggregate)
    print(json.dumps(aggregate, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
