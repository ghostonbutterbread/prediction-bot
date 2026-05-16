#!/usr/bin/env python3
"""Build offline source-reliability replay evidence slices.

This is a shadow/report-only helper. It reads existing Prediction Lab
market_snapshots JSONL rows, selects replay-grade weather rows with buy interest,
and writes bounded evidence artifacts without changing runtime behavior.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.prediction_lab_replay import (  # noqa: E402
    ORDER_BOOK_RECORDED,
    ORDER_BOOK_SIGNAL_PRICE_FALLBACK,
    SOURCE_RECORDED_AS_OF,
    classify_execution_snapshot_mode,
    classify_order_book_mode,
    classify_replay_row_quality,
    classify_source_mode,
)
from bot.weather.source_reliability import build_source_outcome_ledger_rows  # noqa: E402
from bot.weather.source_scoreboard import extract_source_forecast_observations  # noqa: E402


SCHEMA_VERSION = 1
BUY_ACTIONS = {"BUY_YES", "BUY_NO"}
BUY_INTEREST_ACTIONS = BUY_ACTIONS | {"BUY"}


def parse_args() -> argparse.Namespace:
    return parse_args_from(sys.argv[1:])


def parse_args_from(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        action="append",
        required=True,
        help="Prediction Lab market_snapshots.jsonl input. May be repeated.",
    )
    parser.add_argument("--ledger-output", default=None, help="Optional source-outcome ledger JSONL output path.")
    parser.add_argument("--slice-output", required=True, help="JSONL output path for selected raw rows.")
    parser.add_argument("--summary-output", required=True, help="JSON summary output path.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum input rows to inspect across all inputs.")
    parser.add_argument(
        "--max-slice-rows",
        type=int,
        default=200,
        help="Maximum selected rows to write to the evidence slice.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args() if argv is None else parse_args_from(argv)
    if args.limit is not None and args.limit < 0:
        raise SystemExit("--limit must be non-negative")
    if args.max_slice_rows < 0:
        raise SystemExit("--max-slice-rows must be non-negative")

    input_paths = [_resolve_path(path) for path in args.input]
    slice_output = _resolve_path(args.slice_output)
    summary_output = _resolve_path(args.summary_output)
    ledger_output = _resolve_path(args.ledger_output) if args.ledger_output else None

    result = build_evidence_slice(
        input_paths,
        limit=args.limit,
        max_slice_rows=args.max_slice_rows,
    )

    _write_raw_jsonl(slice_output, [candidate.raw_line for candidate in result.selected])

    ledger_rows: list[dict[str, Any]] = []
    if ledger_output is not None:
        ledger_rows = build_source_outcome_ledger_rows(
            [candidate.row for candidate in result.selected],
            source_row_path=str(input_paths[0]) if len(input_paths) == 1 else None,
        )
        _write_jsonl(ledger_output, ledger_rows)

    summary = result.summary(
        inputs=input_paths,
        slice_output=slice_output,
        summary_output=summary_output,
        ledger_output=ledger_output,
        ledger_rows=len(ledger_rows),
        limit=args.limit,
        max_slice_rows=args.max_slice_rows,
    )
    _write_json(summary_output, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


class SelectedCandidate:
    def __init__(self, row: dict[str, Any], raw_line: str, line_number: int, input_path: Path, reason: str) -> None:
        self.row = row
        self.raw_line = raw_line
        self.line_number = line_number
        self.input_path = input_path
        self.reason = reason


class EvidenceSliceResult:
    def __init__(self) -> None:
        self.selected: list[SelectedCandidate] = []
        self.rows_read = 0
        self.lines_read = 0
        self.invalid_json_lines = 0
        self.non_object_lines = 0
        self.blank_lines = 0
        self.limit_reached = False
        self.slice_limit_reached = False
        self.reason_counts: Counter[str] = Counter()
        self.quality_counts: Counter[str] = Counter()
        self.action_counts: Counter[str] = Counter()
        self.selected_action_counts: Counter[str] = Counter()
        self.selected_quality_counts: Counter[str] = Counter()
        self.source_mode_counts: Counter[str] = Counter()
        self.order_book_mode_counts: Counter[str] = Counter()
        self.execution_snapshot_mode_counts: Counter[str] = Counter()
        self.buy_interest_counts: Counter[str] = Counter()
        self.weather_rows = 0
        self.source_observation_rows = 0

    def summary(
        self,
        *,
        inputs: Iterable[Path],
        slice_output: Path,
        summary_output: Path,
        ledger_output: Path | None,
        ledger_rows: int,
        limit: int | None,
        max_slice_rows: int,
    ) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "offline_shadow_report_only",
            "network_access": False,
            "runtime_behavior_changed": False,
            "inputs": [str(path) for path in inputs],
            "slice_output": str(slice_output),
            "summary_output": str(summary_output),
            "ledger_output": str(ledger_output) if ledger_output is not None else None,
            "limit": limit,
            "max_slice_rows": max_slice_rows,
            "lines_read": self.lines_read,
            "rows_read": self.rows_read,
            "blank_lines": self.blank_lines,
            "invalid_json_lines": self.invalid_json_lines,
            "non_object_lines": self.non_object_lines,
            "limit_reached": self.limit_reached,
            "slice_limit_reached": self.slice_limit_reached,
            "weather_rows": self.weather_rows,
            "source_observation_rows": self.source_observation_rows,
            "selected_rows": len(self.selected),
            "ledger_rows": ledger_rows,
            "quality_counts": _sorted_counter(self.quality_counts),
            "reason_counts": _sorted_counter(self.reason_counts),
            "action_counts": _sorted_counter(self.action_counts),
            "selected_action_counts": _sorted_counter(self.selected_action_counts),
            "selected_quality_counts": _sorted_counter(self.selected_quality_counts),
            "source_mode_counts": _sorted_counter(self.source_mode_counts),
            "order_book_mode_counts": _sorted_counter(self.order_book_mode_counts),
            "execution_snapshot_mode_counts": _sorted_counter(self.execution_snapshot_mode_counts),
            "buy_interest_counts": _sorted_counter(self.buy_interest_counts),
            "selected_rows_preview": [
                {
                    "input": str(candidate.input_path),
                    "line_number": candidate.line_number,
                    "market_id": candidate.row.get("market_id")
                    or _artifact(candidate.row).get("market_id")
                    or candidate.row.get("snapshot_key"),
                    "action": _primary_action(candidate.row),
                    "buy_interest": _buy_interest_label(candidate.row),
                    "reason": candidate.reason,
                }
                for candidate in self.selected[:25]
            ],
        }


def build_evidence_slice(
    input_paths: Iterable[str | Path],
    *,
    limit: int | None = None,
    max_slice_rows: int = 200,
) -> EvidenceSliceResult:
    result = EvidenceSliceResult()
    for input_path_value in input_paths:
        input_path = _resolve_path(input_path_value)
        with input_path.open(encoding="utf-8") as fh:
            for line_number, raw_line in enumerate(fh, start=1):
                if limit is not None and result.rows_read >= limit:
                    result.limit_reached = True
                    return result
                result.lines_read += 1
                if not raw_line.strip():
                    result.blank_lines += 1
                    continue
                try:
                    payload = json.loads(raw_line)
                except json.JSONDecodeError:
                    result.invalid_json_lines += 1
                    result.reason_counts["invalid_json"] += 1
                    continue
                if not isinstance(payload, dict):
                    result.non_object_lines += 1
                    result.reason_counts["non_object_json"] += 1
                    continue
                result.rows_read += 1
                verdict = classify_evidence_row(payload)
                result.reason_counts[verdict["reason"]] += 1
                result.quality_counts[str(verdict["quality_category"])] += 1
                result.action_counts[str(verdict["action"])] += 1
                result.source_mode_counts[str(verdict["source_mode"])] += 1
                result.order_book_mode_counts[str(verdict["order_book_mode"])] += 1
                result.execution_snapshot_mode_counts[str(verdict["execution_snapshot_mode"])] += 1
                result.buy_interest_counts[str(verdict["buy_interest"])] += 1
                if verdict["is_weather"]:
                    result.weather_rows += 1
                if verdict["source_observation_count"]:
                    result.source_observation_rows += 1
                if verdict["selected"] and len(result.selected) < max_slice_rows:
                    result.selected.append(
                        SelectedCandidate(
                            row=payload,
                            raw_line=raw_line,
                            line_number=line_number,
                            input_path=input_path,
                            reason=str(verdict["reason"]),
                        )
                    )
                    result.selected_action_counts[str(verdict["action"])] += 1
                    result.selected_quality_counts[str(verdict["quality_category"])] += 1
                elif verdict["selected"]:
                    result.slice_limit_reached = True
                    result.reason_counts["slice_limit_reached"] += 1
                    return result
    return result


def classify_evidence_row(row: Mapping[str, Any]) -> dict[str, Any]:
    artifact = _artifact(row)
    source_mode = classify_source_mode(artifact, dict(row))
    order_book_mode = classify_order_book_mode(artifact)
    execution_snapshot_mode = classify_execution_snapshot_mode(artifact)
    quality = classify_replay_row_quality(
        artifact,
        dict(row),
        source_mode=source_mode,
        order_book_mode=order_book_mode,
        execution_snapshot_mode=execution_snapshot_mode,
    )
    observations = extract_source_forecast_observations(dict(row))
    action = _primary_action(row)
    buy_interest = _has_buy_interest(row)
    is_weather = _is_weather_candidate(row, artifact, observations)
    timestamp = _row_timestamp(row, artifact)

    reason = "selected"
    selected = False
    if not is_weather:
        reason = "non_weather"
    elif timestamp is None:
        reason = "missing_timestamp"
    elif source_mode != SOURCE_RECORDED_AS_OF:
        reason = f"source_not_recorded_as_of:{source_mode}"
    elif not observations:
        reason = "missing_source_observations"
    elif order_book_mode != ORDER_BOOK_RECORDED or execution_snapshot_mode not in {
        ORDER_BOOK_RECORDED,
        ORDER_BOOK_SIGNAL_PRICE_FALLBACK,
    }:
        reason = f"missing_or_unusable_order_book:{order_book_mode}/{execution_snapshot_mode}"
    elif not buy_interest:
        reason = "no_buy_interest"
    elif not quality.include_in_strict:
        reason = f"not_replay_grade:{quality.category}"
    else:
        selected = True

    return {
        "selected": selected,
        "reason": reason,
        "quality_category": quality.category,
        "quality_reasons": list(quality.reasons),
        "action": action,
        "buy_interest": _buy_interest_label(row),
        "is_weather": is_weather,
        "source_observation_count": len(observations),
        "source_mode": source_mode,
        "order_book_mode": order_book_mode,
        "execution_snapshot_mode": execution_snapshot_mode,
        "timestamp": timestamp,
    }


def _resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def _write_raw_jsonl(path: Path, lines: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line if line.endswith("\n") else f"{line}\n")


def _artifact(row: Mapping[str, Any]) -> dict[str, Any]:
    artifact = row.get("decision_artifact")
    return dict(artifact) if isinstance(artifact, Mapping) else {}


def _is_weather_candidate(
    row: Mapping[str, Any],
    artifact: Mapping[str, Any],
    observations: list[Any],
) -> bool:
    if observations:
        return True
    route_values = [
        row.get("group"),
        _nested(row, "market_route", "group"),
        _nested(row, "market_route", "family"),
        _nested(artifact, "market_route", "group"),
        _nested(artifact, "market_route", "family"),
        _nested(row, "weather_risk", "shape"),
        row.get("series"),
        row.get("market_id"),
        row.get("question"),
        artifact.get("market_id"),
    ]
    snapshots = artifact.get("source_snapshots")
    if isinstance(snapshots, list):
        route_values.extend(
            snapshot.get("source")
            for snapshot in snapshots
            if isinstance(snapshot, Mapping)
        )
    joined = " ".join(str(value or "").lower() for value in route_values)
    return any(token in joined for token in ("weather", "daily_temperature", "temperature", "kxhigh", "kxlow"))


def _primary_action(row: Mapping[str, Any]) -> str:
    artifact = _artifact(row)
    return _normalize_action(
        artifact.get("final_action")
        or row.get("final_action")
        or row.get("direction")
        or row.get("decision_type")
        or _nested(artifact, "strategy_signal", "direction")
        or _nested(artifact, "strategy_trace", "ensemble_signal", "direction")
    )


def _has_buy_interest(row: Mapping[str, Any]) -> bool:
    artifact = _artifact(row)
    candidates = [
        artifact.get("final_action"),
        row.get("final_action"),
        row.get("direction"),
        row.get("decision_type"),
        _nested(artifact, "strategy_signal", "direction"),
        _nested(artifact, "strategy_trace", "ensemble_signal", "direction"),
        _nested(row, "strategy_signal", "direction"),
    ]
    return any(_normalize_action(value) in BUY_INTEREST_ACTIONS for value in candidates)


def _buy_interest_label(row: Mapping[str, Any]) -> str:
    artifact = _artifact(row)
    for label, value in (
        ("final_action", artifact.get("final_action") or row.get("final_action")),
        ("row_direction", row.get("direction")),
        ("decision_type", row.get("decision_type")),
        ("strategy_signal", _nested(artifact, "strategy_signal", "direction")),
        ("ensemble_signal", _nested(artifact, "strategy_trace", "ensemble_signal", "direction")),
        ("row_strategy_signal", _nested(row, "strategy_signal", "direction")),
    ):
        if _normalize_action(value) in BUY_INTEREST_ACTIONS:
            return label
    return "none"


def _row_timestamp(row: Mapping[str, Any], artifact: Mapping[str, Any]) -> str | None:
    for value in (
        row.get("observed_at"),
        row.get("timestamp"),
        row.get("created_at"),
        artifact.get("observed_at"),
        artifact.get("timestamp"),
        artifact.get("created_at"),
        artifact.get("as_of"),
        _nested(artifact, "source_context", "as_of"),
    ):
        if value not in (None, ""):
            return str(value)
    return None


def _nested(value: Mapping[str, Any], *path: str) -> Any:
    current: Any = value
    for part in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _normalize_action(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text in BUY_ACTIONS or text == "SKIP":
        return text
    if text in {"BUY_YES", "BUY YES", "YES", "BUYYES"}:
        return "BUY_YES"
    if text in {"BUY_NO", "BUY NO", "NO", "BUYNO"}:
        return "BUY_NO"
    if text in {"BUY", "BUY_INTEREST"}:
        return "BUY"
    if text.startswith("BUY_YES") or text == "BUY_Y":
        return "BUY_YES"
    if text.startswith("BUY_NO") or text == "BUY_N":
        return "BUY_NO"
    if text in {"BUY_YES".replace("_", ""), "BUY_NO".replace("_", "")}:
        return "BUY_YES" if text.endswith("YES") else "BUY_NO"
    return text or "UNKNOWN"


def _sorted_counter(counter: Counter[str]) -> dict[str, int]:
    return dict(sorted(counter.items()))


if __name__ == "__main__":
    raise SystemExit(main())
