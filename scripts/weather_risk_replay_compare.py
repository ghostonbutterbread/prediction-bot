#!/usr/bin/env python3
"""Replay archived weather trades through the current pure weather-risk logic."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.shared_core.weather_risk import build_weather_source_confidence_evidence  # noqa: E402
from bot.weather_market_risk import apply_weather_size_limits, assess_weather_market_risk  # noqa: E402


DEFAULT_SESSION = "data/paper/sim_20260420_194414.json"
DEFAULT_BASELINE = "data/paper/audits/baselines/weather_risk_20260427_shared_brain_v1/baseline_summary.json"
DEFAULT_SUMMARY_OUTPUT = "data/paper/audits/weather_risk_replay_compare_summary.json"
DEFAULT_CSV_OUTPUT = "data/paper/audits/weather_risk_replay_compare_rows.csv"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", default=DEFAULT_SESSION, help="Archived paper session JSON to replay.")
    parser.add_argument("--baseline", default=DEFAULT_BASELINE, help="Baseline summary JSON to compare against.")
    parser.add_argument("--summary-output", default=DEFAULT_SUMMARY_OUTPUT, help="Path to write the replay summary JSON.")
    parser.add_argument("--csv-output", default=DEFAULT_CSV_OUTPUT, help="Path to write per-trade replay rows.")
    return parser.parse_args()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def build_trade_row(trade: dict[str, Any]) -> dict[str, Any]:
    signal = {
        "market_id": trade.get("market_id"),
        "ticker": trade.get("market_id"),
        "category": trade.get("category"),
        "question": trade.get("question"),
        "confidence": trade.get("confidence"),
        "signals": trade.get("signals") or {},
    }
    evidence = build_weather_source_confidence_evidence(signal)
    direction = str(trade.get("direction") or "BUY_YES").upper()
    model_probability = _float(trade.get("model_probability"), default=0.0) or None
    win_probability = None
    if model_probability is not None:
        win_probability = 1 - model_probability if direction == "BUY_NO" else model_probability
    assessment = assess_weather_market_risk(
        {**signal, **evidence},
        entry_price=_float(trade.get("entry_price") or trade.get("market_price"), default=0.0) or None,
        win_probability=win_probability,
    )

    old_size = _float(trade.get("position_size") or trade.get("placed_size"))
    old_pnl = _float(trade.get("net_pnl", trade.get("pnl")))
    # Keep replay methodology stable against the archived baseline.  This is a
    # static per-trade risk replay, not an alternate account timeline.
    current_balance = 100.0
    new_size = (
        0.0
        if assessment.should_skip
        else apply_weather_size_limits(old_size, assessment, current_balance=current_balance)
    )
    if assessment.should_skip:
        outcome = "skip"
    elif abs(new_size - old_size) < 1e-9:
        outcome = "unchanged"
    else:
        outcome = "resize"

    replayed_pnl = 0.0
    if old_size > 0 and new_size > 0:
        replayed_pnl = round(old_pnl * (new_size / old_size), 4)

    return {
        "id": trade.get("id") or trade.get("trade_id"),
        "market_id": trade.get("market_id"),
        "question": trade.get("question"),
        "direction": direction,
        "shape": assessment.shape,
        "entry_price": _float(trade.get("entry_price") or trade.get("market_price"), default=0.0),
        "win_probability": round(win_probability, 6) if win_probability is not None else None,
        "probability_multiple": round(assessment.probability_multiple, 6) if assessment.probability_multiple is not None else None,
        "hidden_gem_tier": assessment.hidden_gem_tier,
        "weather_station_mapping": evidence.get("weather_station_mapping"),
        "weather_station_city_code": (evidence.get("weather_station_resolution") or {}).get("city_code"),
        "weather_station_id": (evidence.get("weather_station_resolution") or {}).get("station_id"),
        "weather_confidence_score": evidence.get("weather_confidence_score"),
        "source_agreement_score": evidence.get("source_agreement_score"),
        "distribution_probability": evidence.get("distribution_probability"),
        "volume_known": evidence.get("volume_known"),
        "flags": "|".join(assessment.flags),
        "reason_code": assessment.reason_code or outcome,
        "current_logic_outcome": outcome,
        "old_size": round(old_size, 4),
        "new_size": round(new_size, 4),
        "old_pnl": round(old_pnl, 4),
        "replayed_pnl": round(replayed_pnl, 4),
        "pnl_saved_vs_actual": round(replayed_pnl - old_pnl, 4),
        "resolution_type": trade.get("resolution_type"),
        "outcome": trade.get("outcome"),
    }


def build_summary(rows: list[dict[str, Any]], session_path: Path) -> dict[str, Any]:
    overall_outcomes = Counter(row["current_logic_outcome"] for row in rows)
    shape_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        shape_rows[row["shape"]].append(row)

    shape_metrics: dict[str, Any] = {}
    for shape, group in sorted(shape_rows.items()):
        shape_metrics[shape] = {
            "count": len(group),
            "actual_pnl": round(sum(_float(row["old_pnl"]) for row in group), 4),
            "replayed_current_logic_pnl": round(sum(_float(row["replayed_pnl"]) for row in group), 4),
            "improvement_vs_actual": round(sum(_float(row["replayed_pnl"]) - _float(row["old_pnl"]) for row in group), 4),
            "actual_total_size": round(sum(_float(row["old_size"]) for row in group), 4),
            "replayed_total_size": round(sum(_float(row["new_size"]) for row in group), 4),
            "outcomes": dict(Counter(row["current_logic_outcome"] for row in group)),
            "hidden_gem_tiers": dict(Counter(row["hidden_gem_tier"] for row in group)),
        }

    actual_pnl = round(sum(_float(row["old_pnl"]) for row in rows), 4)
    replayed_pnl = round(sum(_float(row["replayed_pnl"]) for row in rows), 4)
    return {
        "source_session": str(session_path),
        "overall": {
            "trade_count": len(rows),
            "actual_pnl": actual_pnl,
            "replayed_current_logic_pnl": replayed_pnl,
            "improvement_vs_actual": round(replayed_pnl - actual_pnl, 4),
            "actual_total_size": round(sum(_float(row["old_size"]) for row in rows), 4),
            "replayed_total_size": round(sum(_float(row["new_size"]) for row in rows), 4),
            "outcomes": dict(overall_outcomes),
        },
        "shape_metrics": shape_metrics,
    }


def compare_to_baseline(summary: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    def _metric(path: tuple[str, ...], current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
        current_value: Any = current
        previous_value: Any = previous
        for key in path:
            current_value = current_value[key]
            previous_value = previous_value[key]
        if isinstance(current_value, (int, float)) and isinstance(previous_value, (int, float)):
            delta = round(float(current_value) - float(previous_value), 4)
        else:
            delta = None if current_value == previous_value else {"current": current_value, "baseline": previous_value}
        return {"path": ".".join(path), "current": current_value, "baseline": previous_value, "delta": delta}

    metric_paths = (
        ("overall", "trade_count"),
        ("overall", "actual_pnl"),
        ("overall", "replayed_current_logic_pnl"),
        ("overall", "improvement_vs_actual"),
        ("overall", "actual_total_size"),
        ("overall", "replayed_total_size"),
        ("shape_metrics", "bucket", "replayed_current_logic_pnl"),
        ("shape_metrics", "tail_high", "replayed_current_logic_pnl"),
        ("shape_metrics", "tail_low", "replayed_current_logic_pnl"),
    )
    comparisons = [_metric(path, summary, baseline) for path in metric_paths]
    return {
        "matches_baseline": all(item["delta"] in (0, 0.0, None) for item in comparisons),
        "metrics": comparisons,
    }


def write_rows_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else []
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    session_path = PROJECT_ROOT / args.session
    baseline_path = PROJECT_ROOT / args.baseline
    summary_output = PROJECT_ROOT / args.summary_output
    csv_output = PROJECT_ROOT / args.csv_output

    session = json.loads(session_path.read_text(encoding="utf-8"))
    trades = [trade for trade in session.get("trades", []) if str(trade.get("market_id") or "").startswith("KX")]
    rows = [build_trade_row(trade) for trade in trades]
    summary = build_summary(rows, session_path)

    if baseline_path.exists():
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        summary["baseline_compare"] = compare_to_baseline(summary, baseline)

    summary_output.parent.mkdir(parents=True, exist_ok=True)
    summary_output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    write_rows_csv(rows, csv_output)

    print(f"Wrote {summary_output.relative_to(PROJECT_ROOT)}")
    print(f"Wrote {csv_output.relative_to(PROJECT_ROOT)}")
    print(
        "Replay totals: "
        f"actual_pnl={summary['overall']['actual_pnl']}, "
        f"replayed_pnl={summary['overall']['replayed_current_logic_pnl']}, "
        f"delta={summary['overall']['improvement_vs_actual']}"
    )
    if "baseline_compare" in summary:
        print(f"Baseline match: {summary['baseline_compare']['matches_baseline']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
