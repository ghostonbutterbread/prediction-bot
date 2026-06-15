#!/usr/bin/env python3
"""Compare gated source-router shadow lane configs against resolved replay rows.

This is a summary-only replay helper. It consumes flattened source-router rows
that already include resolution/PnL fields, applies composition gate/exposure
settings, and writes compact comparison artifacts. It does not mutate paper or
live state.
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

try:
    import yaml
except ImportError:  # pragma: no cover - requirements include PyYAML.
    yaml = None

DEFAULT_INPUT = "data/derived_reports/source_router_rule_discovery_current/joined_source_router_rows.jsonl"
DEFAULT_OUTPUT_DIR = "data/derived_reports/source_router_gate_cross_compare_current"
SAFE_OUTPUT_ROOTS = (
    ROOT / "data" / "derived_reports",
    ROOT / "data" / "summaries",
    ROOT / "data" / "beta_shadow" / "summaries",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--config", action="append", default=[], help="Lane composition YAML/JSON config. Repeatable.")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    rows = load_jsonl(_root_path(args.input))
    config_paths = [_root_path(path) for path in args.config]
    if not config_paths:
        raise SystemExit("At least one --config is required")

    result = compare_gate_configs(rows=rows, config_paths=config_paths)
    output_dir = _safe_output_dir(_root_path(args.output_dir))
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output_dir / "report.md").write_text(_markdown_report(result), encoding="utf-8")

    if args.format == "json":
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(_text_report(result))
        print(f"output_dir={_display_path(output_dir)}")
    return 0


def compare_gate_configs(*, rows: Iterable[Mapping[str, Any]], config_paths: Iterable[Path]) -> dict[str, Any]:
    source_rows = [dict(row) for row in rows]
    configs = [_load_composition(path) for path in config_paths]
    summaries = [_summarize_config(source_rows, config) for config in configs]
    return {
        "schema_name": "source_router_gate_replay_compare",
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "non_mutating": True,
        "input_rows": len(source_rows),
        "config_count": len(summaries),
        "configs": summaries,
        "best": {
            "all_snapshots_roi": _best(summaries, "all_snapshots", "roi_pct"),
            "latest_per_market_roi": _best(summaries, "latest_per_market", "roi_pct"),
            "latest_per_market_pnl": _best(summaries, "latest_per_market", "total_pnl_usd"),
        },
    }


def _summarize_config(rows: list[dict[str, Any]], config: Mapping[str, Any]) -> dict[str, Any]:
    composition = config["composition"]
    name = str(composition["name"])
    filtered, diagnostics = _filter_rows(rows, composition)
    exposure_rows, exposure_diagnostics = _apply_config_exposure(filtered, composition)
    diagnostics.update(exposure_diagnostics)
    latest = _select_one_per_market(filtered, selector="latest")
    earliest = _select_one_per_market(filtered, selector="earliest")
    return {
        "name": name,
        "config_path": config["config_path"],
        "gates": list(composition.get("gates") or []),
        "exposure": dict(composition.get("exposure") or {}),
        "diagnostics": dict(sorted(diagnostics.items())),
        "all_snapshots": _pnl_summary(filtered),
        "configured_exposure": _pnl_summary(exposure_rows),
        "latest_per_market": _pnl_summary(latest),
        "earliest_per_market": _pnl_summary(earliest),
    }


def _filter_rows(rows: list[dict[str, Any]], composition: Mapping[str, Any]) -> tuple[list[dict[str, Any]], Counter[str]]:
    diagnostics: Counter[str] = Counter()
    out: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("action") or "").upper() not in {"BUY_YES", "BUY_NO"}:
            diagnostics["skip_non_buy"] += 1
            continue
        if row.get("pnl_usd") is None or row.get("stake_usd") is None:
            diagnostics["skip_uncalculable"] += 1
            continue
        failed = _failed_gate(row, composition)
        if failed:
            diagnostics[failed] += 1
            continue
        out.append(row)

    return out, diagnostics


def _apply_config_exposure(rows: list[dict[str, Any]], composition: Mapping[str, Any]) -> tuple[list[dict[str, Any]], Counter[str]]:
    exposure = composition.get("exposure") if isinstance(composition.get("exposure"), Mapping) else {}
    if _number(exposure.get("max_rows_per_market")) == 1:
        selected = _select_one_per_market(rows, selector=str(exposure.get("selector") or "latest"))
        return selected, Counter({"exposure_dropped_rows": max(0, len(rows) - len(selected))})
    return rows, Counter()


def _failed_gate(row: Mapping[str, Any], composition: Mapping[str, Any]) -> str | None:
    for gate in composition.get("gates", []):
        if not isinstance(gate, Mapping):
            continue
        name = str(gate.get("name") or gate.get("field") or "gate")
        field = str(gate.get("field") or "")
        op = str(gate.get("op") or "eq")
        if not _gate_passes(row.get(field), op=op, expected=gate.get("value")):
            return f"gate_failed:{name}"
    return None


def _gate_passes(actual: Any, *, op: str, expected: Any) -> bool:
    if op in {"eq", "=="}:
        return str(actual) == str(expected)
    if op in {"ne", "!="}:
        return str(actual) != str(expected)
    if op == "in":
        values = expected if isinstance(expected, list) else [expected]
        return str(actual) in {str(value) for value in values}
    if op in {"gte", ">="}:
        actual_number = _number(actual)
        expected_number = _number(expected)
        return actual_number is not None and expected_number is not None and actual_number >= expected_number
    if op in {"gt", ">"}:
        actual_number = _number(actual)
        expected_number = _number(expected)
        return actual_number is not None and expected_number is not None and actual_number > expected_number
    if op in {"lte", "<="}:
        actual_number = _number(actual)
        expected_number = _number(expected)
        return actual_number is not None and expected_number is not None and actual_number <= expected_number
    if op in {"lt", "<"}:
        actual_number = _number(actual)
        expected_number = _number(expected)
        return actual_number is not None and expected_number is not None and actual_number < expected_number
    raise ValueError(f"unknown gate op: {op}")


def _select_one_per_market(rows: list[dict[str, Any]], *, selector: str) -> list[dict[str, Any]]:
    by_market: dict[str, dict[str, Any]] = {}
    for row in rows:
        market_id = str(row.get("market_id") or row.get("shared_candidate_id") or "")
        if not market_id:
            continue
        current = by_market.get(market_id)
        if current is None:
            by_market[market_id] = row
            continue
        observed = str(row.get("observed_at") or "")
        current_observed = str(current.get("observed_at") or "")
        if selector == "latest" and observed >= current_observed:
            by_market[market_id] = row
        elif selector == "earliest" and observed <= current_observed:
            by_market[market_id] = row
    return list(by_market.values())


def _pnl_summary(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    stake = sum(float(_number(row.get("stake_usd")) or 0.0) for row in rows)
    pnl = sum(float(_number(row.get("pnl_usd")) or 0.0) for row in rows)
    wins = sum(1 for row in rows if row.get("won") is True)
    losses = sum(1 for row in rows if row.get("won") is False)
    return {
        "rows": len(rows),
        "unique_markets": len({str(row.get("market_id") or "") for row in rows if row.get("market_id") not in (None, "")}),
        "winning_buy_rows": wins,
        "losing_buy_rows": losses,
        "win_rate_pct": round((wins / len(rows)) * 100, 2) if rows else None,
        "total_stake_usd": round(stake, 4),
        "total_pnl_usd": round(pnl, 4),
        "roi_pct": round((pnl / stake) * 100, 2) if stake else None,
        "side_mix": dict(sorted(Counter(str(row.get("side") or "") for row in rows).items())),
    }


def _best(summaries: list[Mapping[str, Any]], section: str, field: str) -> dict[str, Any] | None:
    candidates = []
    for summary in summaries:
        metrics = summary.get(section) if isinstance(summary.get(section), Mapping) else {}
        if isinstance(metrics.get(field), int | float):
            candidates.append((summary, metrics))
    if not candidates:
        return None
    summary, metrics = max(candidates, key=lambda item: float(item[1][field]))
    return {"name": summary["name"], section: metrics}


def _load_composition(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        loaded = json.loads(text)
    else:
        if yaml is None:
            raise RuntimeError("YAML configs require PyYAML")
        loaded = yaml.safe_load(text)
    if not isinstance(loaded, Mapping) or not isinstance(loaded.get("composition"), Mapping):
        raise ValueError(f"composition config must contain composition mapping: {path}")
    return {"config_path": _display_path(path), "composition": dict(loaded["composition"])}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            row = json.loads(stripped)
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _markdown_report(result: Mapping[str, Any]) -> str:
    return _text_report(result) + "\n"


def _text_report(result: Mapping[str, Any]) -> str:
    lines = [f"Source-router gate replay compare configs={result['config_count']} input_rows={result['input_rows']}"]
    for row in result["configs"]:
        latest = row["latest_per_market"]
        all_rows = row["all_snapshots"]
        lines.append(
            f"{row['name']}: latest rows={latest['rows']} markets={latest['unique_markets']} "
            f"pnl=${latest['total_pnl_usd']} roi={latest['roi_pct']}% win={latest['win_rate_pct']}% "
            f"| all rows={all_rows['rows']} pnl=${all_rows['total_pnl_usd']} roi={all_rows['roi_pct']}%"
        )
    return "\n".join(lines)


def _root_path(raw: str | Path) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def _safe_output_dir(path: Path) -> Path:
    output = path.resolve()
    safe_roots = [root.resolve() for root in SAFE_OUTPUT_ROOTS]
    if not any(output == root or root in output.parents for root in safe_roots):
        raise ValueError("Output directory must be under data/derived_reports, data/summaries, or data/beta_shadow/summaries")
    return output


def _display_path(path: Path) -> str:
    path = path.resolve()
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _number(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
