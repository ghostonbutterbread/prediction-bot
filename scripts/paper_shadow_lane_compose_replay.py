#!/usr/bin/env python3
"""Read-only replay composer for paper shadow lane decisions.

This script builds a derived lane from existing paper shadow lane rows. It does
not mutate wallets, accounting ledgers, source lane decisions, or live state.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.file_ops import load_jsonl  # noqa: E402
from bot.paper_shadow_lanes import (  # noqa: E402
    build_paper_shadow_lane_resolution_rows,
    summarize_paper_shadow_lane_resolution_rows,
)


DEFAULT_STABLE_LANE = "control_stable"
DEFAULT_LANE_DECISION_PATH = "data/beta_shadow/paper/source_scoreboard/paper_shadow_lane_decisions.jsonl"
DEFAULT_OUTPUT_ROOT = "data/summaries/lane_compositions"
SAFE_OUTPUT_ROOTS = (
    ROOT / "data" / "summaries",
    ROOT / "data" / "beta_shadow" / "summaries",
    ROOT / "data" / "derived_reports",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lane-decision-path", default=DEFAULT_LANE_DECISION_PATH)
    parser.add_argument("--resolution-path", default=None)
    parser.add_argument(
        "--composition-config",
        required=True,
        help="YAML/JSON composition file, for example lane_compositions/source_router_side_stable_size.yaml.",
    )
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--format", choices=["text", "json"], default="text")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    lane_rows = load_jsonl(_root_path(args.lane_decision_path))
    resolution_rows = load_jsonl(_root_path(args.resolution_path)) if args.resolution_path else []
    config = _load_config(_root_path(args.composition_config))
    result = compose_lane_replay(lane_rows=lane_rows, resolution_rows=resolution_rows, config=config)

    output_dir = _output_dir(args.output_dir, result["composition"]["name"])
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

    if args.format == "json":
        print(json.dumps(_summary_payload(result), indent=2, sort_keys=True))
    else:
        print(_text_report(result))
        print(f"output_dir={output_dir.relative_to(ROOT)}")
    return 0


def compose_lane_replay(
    *,
    lane_rows: Iterable[Mapping[str, Any]],
    resolution_rows: Iterable[Mapping[str, Any]] = (),
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Compose a derived lane from existing lane rows and score it read-only."""

    composition = _normalize_composition(config)
    grouped = _group_lane_rows(lane_rows)
    composed_rows: list[dict[str, Any]] = []
    diagnostics: Counter[str] = Counter()
    duplicate_lanes: Counter[str] = Counter()

    for candidate_key, lane_map in sorted(grouped.items()):
        for lane_id, rows in lane_map.items():
            if len(rows) > 1:
                duplicate_lanes[lane_id] += len(rows) - 1
        row, reason = _compose_candidate(candidate_key, lane_map, composition)
        diagnostics[reason] += 1
        if row is not None:
            composed_rows.append(row)

    composed_rows, exposure_diagnostics = _apply_exposure_controls(composed_rows, composition)
    diagnostics.update(exposure_diagnostics)

    resolved_rows = build_paper_shadow_lane_resolution_rows(
        lane_rows=composed_rows,
        resolution_rows=resolution_rows,
    )
    pnl_summary = summarize_paper_shadow_lane_resolution_rows(resolved_rows)
    summary = {
        "schema_name": "paper_shadow_lane_composition_summary",
        "schema_version": 1,
        "non_mutating": True,
        "composition": composition,
        "candidate_groups": len(grouped),
        "composed_rows": len(composed_rows),
        "diagnostics": dict(sorted(diagnostics.items())),
        "duplicate_lane_rows": dict(sorted(duplicate_lanes.items())),
        "pnl": pnl_summary,
    }
    return {
        "composition": composition,
        "composition_rows": composed_rows,
        "resolved_rows": resolved_rows,
        "summary": summary,
    }


def _compose_candidate(
    candidate_key: str,
    lane_map: Mapping[str, list[Mapping[str, Any]]],
    composition: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    base_lane = str(composition["base_lane"])
    action_lane = str(composition["action_lane"])
    sizing_lane = str(composition["sizing_lane"])
    price_lane = str(composition["price_lane"])
    fallback_to_base = bool(composition.get("fallback_to_base", True))

    base_row = _select_lane_row(lane_map, base_lane)
    if base_row is None:
        return None, "missing_base_lane"

    action_row = _select_lane_row(lane_map, action_lane) or (base_row if fallback_to_base else None)
    sizing_row = _select_lane_row(lane_map, sizing_lane) or (base_row if fallback_to_base else None)
    price_row = _select_lane_row(lane_map, price_lane) or action_row or base_row
    if action_row is None:
        return None, "missing_action_lane"
    if sizing_row is None:
        return None, "missing_sizing_lane"

    action = _action(action_row)
    side = _side_from_action(action)
    veto_reason = _veto_reason(lane_map, composition, selected_side=side)
    if veto_reason:
        action = "SKIP"
        side = None

    notional = 0.0 if action == "SKIP" else _composition_notional(composition, sizing_row)
    price = None if action == "SKIP" else _side_price(price_row, side)
    future_inputs = _merged_future_inputs(base_row=base_row, action_row=action_row, sizing_row=sizing_row, price_row=price_row)
    future_inputs.update(
        {
            "recommended_action": action,
            "side": side,
            "entry_price": price,
            "estimated_fill_price": price,
            "requested_position_size_usd": notional,
            "approved_position_size_usd": notional,
            "composition_name": composition["name"],
            "composition_base_lane": base_lane,
            "composition_action_lane": action_lane,
            "composition_sizing_lane": sizing_lane,
            "composition_price_lane": price_lane,
        }
    )
    gate_context = _gate_context(
        base_row=base_row,
        action_row=action_row,
        sizing_row=sizing_row,
        price_row=price_row,
        future_inputs=future_inputs,
        action=action,
        side=side,
    )
    failed_gate = _failed_gate_reason(gate_context, composition)
    if failed_gate:
        return None, failed_gate

    row = {
        "schema_name": "paper_shadow_lane_composition_decision",
        "schema_version": 1,
        "non_mutating": True,
        "policy": f"composition:{composition['name']}",
        "selected_lane": f"composition:{composition['name']}",
        "decision_role": "derived_composition_replay",
        "shared_candidate_id": _candidate_id(base_row) or candidate_key,
        "market_id": _first_text(_field(base_row, "market_id"), future_inputs.get("market_id")),
        "observed_at": _first_text(_field(action_row, "observed_at"), _field(base_row, "observed_at"), future_inputs.get("observed_at")),
        "action": action,
        "side": side,
        "entry_price": price,
        "price": price,
        "requested_position_size_usd": notional,
        "approved_position_size_usd": notional,
        "reason_code": veto_reason or "composition_selected",
        "reason": "Derived read-only lane composition replay",
        "provenance": {
            "composition": {
                **composition,
                "candidate_key": candidate_key,
                "selected_action": action,
                "selected_side": side,
                "veto_reason": veto_reason,
                "source_lane_ids": sorted(lane_map.keys()),
            },
            "future_pnl_inputs": {key: value for key, value in future_inputs.items() if value not in (None, "", {}, [])},
            "gate_context": {key: value for key, value in gate_context.items() if value not in (None, "", {}, [])},
        },
        "mutation_contract": {
            "mutates_balance": False,
            "mutates_accounting": False,
            "places_live_orders": False,
        },
    }
    return row, veto_reason or ("composed_buy" if action in {"BUY_YES", "BUY_NO"} else "composed_skip")


def _veto_reason(
    lane_map: Mapping[str, list[Mapping[str, Any]]],
    composition: Mapping[str, Any],
    *,
    selected_side: str | None,
) -> str | None:
    for veto in composition.get("vetoes", []):
        lane_id = str(veto.get("lane", ""))
        mode = str(veto.get("mode", "skip_on_conflict"))
        row = _select_lane_row(lane_map, lane_id)
        if row is None:
            if veto.get("required"):
                return f"missing_required_veto_lane:{lane_id}"
            continue
        action = _action(row)
        side = _side_from_action(action)
        if mode == "require_not_skip" and action == "SKIP":
            return f"veto_skip:{lane_id}"
        if mode == "require_buy" and action not in {"BUY_YES", "BUY_NO"}:
            return f"veto_not_buy:{lane_id}"
        if mode == "require_agreement":
            if action not in {"BUY_YES", "BUY_NO"}:
                return f"veto_not_buy:{lane_id}"
            if selected_side and side != selected_side:
                return f"side_conflict:{lane_id}"
        if mode == "skip_on_conflict" and action in {"BUY_YES", "BUY_NO"} and selected_side and side != selected_side:
            return f"side_conflict:{lane_id}"
    return None


def _normalize_composition(config: Mapping[str, Any]) -> dict[str, Any]:
    raw = config.get("composition") if isinstance(config.get("composition"), Mapping) else config
    name = str(raw.get("name") or "lane_composition")
    base_lane = str(raw.get("base_lane") or DEFAULT_STABLE_LANE)
    action_lane = str(raw.get("action_lane") or base_lane)
    sizing_lane = str(raw.get("sizing_lane") or base_lane)
    price_lane = str(raw.get("price_lane") or action_lane)
    return {
        "name": name,
        "base_lane": base_lane,
        "action_lane": action_lane,
        "sizing_lane": sizing_lane,
        "price_lane": price_lane,
        "fallback_to_base": bool(raw.get("fallback_to_base", True)),
        "fixed_notional_usd": _number(raw.get("fixed_notional_usd")),
        "vetoes": list(raw.get("vetoes") or []),
        "gates": list(raw.get("gates") or []),
        "exposure": dict(raw.get("exposure") or {}),
        "variable_ownership": {
            "action": action_lane,
            "sizing": sizing_lane,
            "price": price_lane,
            "fallback": base_lane,
        },
    }


def _group_lane_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, list[Mapping[str, Any]]]]:
    grouped: dict[str, dict[str, list[Mapping[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        lane_id = _lane_id(row)
        candidate_id = _candidate_id(row)
        market_id = _field(row, "market_id")
        key = candidate_id or market_id
        if not lane_id or not key:
            continue
        grouped[str(key)][lane_id].append(row)
    return grouped


def _select_lane_row(lane_map: Mapping[str, list[Mapping[str, Any]]], lane_id: str) -> Mapping[str, Any] | None:
    rows = list(lane_map.get(lane_id, []) or [])
    if not rows:
        return None
    return sorted(rows, key=lambda row: _first_text(_field(row, "decided_at"), _field(row, "observed_at")) or "")[-1]


def _composition_notional(composition: Mapping[str, Any], sizing_row: Mapping[str, Any]) -> float:
    fixed = _number(composition.get("fixed_notional_usd"))
    if fixed is not None:
        return float(fixed)
    future = _future_inputs(sizing_row)
    return float(
        _number(
            _field(sizing_row, "approved_position_size_usd"),
            _field(sizing_row, "requested_position_size_usd"),
            future.get("approved_position_size_usd"),
            future.get("requested_position_size_usd"),
            future.get("stable_approved_position_size_usd"),
            future.get("stable_requested_position_size_usd"),
        )
        or 0.0
    )


def _side_price(row: Mapping[str, Any] | None, side: str | None) -> float | None:
    if row is None:
        return None
    future = _future_inputs(row)
    if side == "YES":
        return _number(future.get("best_yes_ask"), future.get("estimated_fill_price"), future.get("entry_price"), _field(row, "entry_price"), _field(row, "price"))
    if side == "NO":
        return _number(future.get("best_no_ask"), future.get("estimated_fill_price"), future.get("entry_price"), _field(row, "entry_price"), _field(row, "price"))
    return None


def _merged_future_inputs(
    *,
    base_row: Mapping[str, Any],
    action_row: Mapping[str, Any],
    sizing_row: Mapping[str, Any],
    price_row: Mapping[str, Any],
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for row in (base_row, sizing_row, action_row, price_row):
        merged.update(_future_inputs(row))
    for key in ("shared_candidate_id", "market_id", "observed_at"):
        merged.setdefault(key, _field(base_row, key))
    return merged


def _future_inputs(row: Mapping[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {}
    provenance = row.get("provenance") if isinstance(row.get("provenance"), Mapping) else {}
    direct = provenance.get("future_pnl_inputs") if isinstance(provenance.get("future_pnl_inputs"), Mapping) else {}
    if direct:
        return dict(direct)
    for key in ("source_router", "source_scoreboard"):
        nested = provenance.get(key) if isinstance(provenance.get(key), Mapping) else {}
        future = nested.get("future_pnl_inputs") if isinstance(nested.get("future_pnl_inputs"), Mapping) else {}
        if future:
            return dict(future)
    return {}


def _gate_context(
    *,
    base_row: Mapping[str, Any],
    action_row: Mapping[str, Any],
    sizing_row: Mapping[str, Any],
    price_row: Mapping[str, Any],
    future_inputs: Mapping[str, Any],
    action: str,
    side: str | None,
) -> dict[str, Any]:
    rows = [base_row, sizing_row, action_row, price_row]
    router = _first_mapping(rows, ("provenance", "source_router"))
    data_quality = router.get("data_quality") if isinstance(router.get("data_quality"), Mapping) else {}
    sources_used = router.get("sources_used") if isinstance(router.get("sources_used"), list) else []
    source_ids = sorted(
        {
            str(source.get("source_id") or "").strip()
            for source in sources_used
            if isinstance(source, Mapping) and str(source.get("source_id") or "").strip()
        }
    )
    sample_counts = [
        _number(source.get("sample_count"))
        for source in sources_used
        if isinstance(source, Mapping) and _number(source.get("sample_count")) is not None
    ]
    source_tiers = sorted(
        {
            str(source.get("tier") or "").strip()
            for source in sources_used
            if isinstance(source, Mapping) and str(source.get("tier") or "").strip()
        }
    )
    return {
        "action": action,
        "side": side,
        "market_id": _first_text(future_inputs.get("market_id"), _field(action_row, "market_id"), _field(base_row, "market_id")),
        "shared_candidate_id": _first_text(future_inputs.get("shared_candidate_id"), _field(action_row, "shared_candidate_id"), _field(base_row, "shared_candidate_id")),
        "observed_at": _first_text(future_inputs.get("observed_at"), _field(action_row, "observed_at"), _field(base_row, "observed_at")),
        "contract_shape": _first_text(future_inputs.get("contract_shape")),
        "market_kind": _first_text(future_inputs.get("market_kind")),
        "question_side": _first_text(future_inputs.get("question_side")),
        "source_ids": "+".join(source_ids) if source_ids else None,
        "source_id_count": len(source_ids),
        "source_grade": _first_text(future_inputs.get("source_router_source_grade"), router.get("source_grade")),
        "source_direction": _first_text(future_inputs.get("source_router_source_direction"), router.get("source_direction")),
        "agreement_state": _first_text(router.get("agreement_state")),
        "source_confidence_score": _number(router.get("source_confidence_score")),
        "source_observation_count": _number(data_quality.get("source_observation_count")),
        "usable_forecast_count": _number(data_quality.get("usable_forecast_count")),
        "min_sample_count_met": data_quality.get("min_sample_count_met") if isinstance(data_quality.get("min_sample_count_met"), bool) else None,
        "known_at_time": data_quality.get("known_at_time") if isinstance(data_quality.get("known_at_time"), bool) else None,
        "min_source_sample_count": min(sample_counts) if sample_counts else None,
        "max_source_sample_count": max(sample_counts) if sample_counts else None,
        "source_tiers": "+".join(source_tiers) if source_tiers else None,
        "edge": _number(_field(action_row, "edge"), future_inputs.get("edge")),
        "entry_price": _number(future_inputs.get("estimated_fill_price"), future_inputs.get("entry_price"), _field(action_row, "entry_price"), _field(action_row, "price")),
    }


def _first_mapping(rows: Iterable[Mapping[str, Any]], path: tuple[str, ...]) -> dict[str, Any]:
    for row in rows:
        value: Any = row
        for key in path:
            value = value.get(key) if isinstance(value, Mapping) else None
        if isinstance(value, Mapping):
            return dict(value)
    return {}


def _failed_gate_reason(gate_context: Mapping[str, Any], composition: Mapping[str, Any]) -> str | None:
    for gate in composition.get("gates", []):
        if not isinstance(gate, Mapping):
            continue
        name = str(gate.get("name") or gate.get("field") or "gate")
        field = str(gate.get("field") or "")
        op = str(gate.get("op") or "eq")
        actual = gate_context.get(field)
        expected = gate.get("value")
        if not _gate_passes(actual, op=op, expected=expected):
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
    if op == "not_in":
        values = expected if isinstance(expected, list) else [expected]
        return str(actual) not in {str(value) for value in values}
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
    if op == "truthy":
        return bool(actual)
    if op == "falsey":
        return not bool(actual)
    raise ValueError(f"unknown composition gate op: {op}")


def _apply_exposure_controls(
    composed_rows: list[dict[str, Any]],
    composition: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], Counter[str]]:
    exposure = composition.get("exposure") if isinstance(composition.get("exposure"), Mapping) else {}
    max_rows = _number(exposure.get("max_rows_per_market"))
    if max_rows is None:
        return composed_rows, Counter()
    max_rows_i = int(max_rows)
    if max_rows_i <= 0:
        return [], Counter({"exposure_removed_all": len(composed_rows)})

    per_side = bool(exposure.get("per_side", False))
    selector = str(exposure.get("selector") or "latest")
    groups: dict[tuple[str, str | None], list[dict[str, Any]]] = {}
    for row in composed_rows:
        market_id = str(row.get("market_id") or "")
        side = str(row.get("side") or "") if per_side else None
        if not market_id:
            groups.setdefault((str(row.get("shared_candidate_id") or ""), side), []).append(row)
        else:
            groups.setdefault((market_id, side), []).append(row)

    kept: list[dict[str, Any]] = []
    removed = 0
    for rows in groups.values():
        selected = _select_exposure_rows(rows, max_rows=max_rows_i, selector=selector)
        kept.extend(selected)
        removed += max(0, len(rows) - len(selected))
    return sorted(kept, key=lambda row: (_first_text(row.get("observed_at"), row.get("shared_candidate_id")) or "")), Counter({"exposure_dropped_rows": removed} if removed else {})


def _select_exposure_rows(rows: list[dict[str, Any]], *, max_rows: int, selector: str) -> list[dict[str, Any]]:
    def sort_key(row: Mapping[str, Any]) -> str:
        return _first_text(row.get("observed_at"), row.get("shared_candidate_id")) or ""

    ordered = sorted(rows, key=sort_key)
    if selector == "earliest":
        return ordered[:max_rows]
    if selector == "latest":
        return ordered[-max_rows:]
    if selector == "highest_edge":
        return sorted(rows, key=lambda row: _number(_mapping(_mapping(row.get("provenance")).get("gate_context")).get("edge")) or float("-inf"), reverse=True)[:max_rows]
    raise ValueError(f"unknown exposure selector: {selector}")


def _lane_id(row: Mapping[str, Any]) -> str | None:
    return _first_text(_field(row, "policy"), _field(row, "selected_lane"), _field(row, "lane_id"), _future_inputs(row).get("lane_id"))


def _candidate_id(row: Mapping[str, Any]) -> str | None:
    shared = row.get("shared_candidate") if isinstance(row.get("shared_candidate"), Mapping) else {}
    return _first_text(_field(row, "shared_candidate_id"), shared.get("candidate_id"), shared.get("shared_candidate_id"), _future_inputs(row).get("shared_candidate_id"))


def _field(row: Mapping[str, Any], key: str) -> Any:
    return row.get(key)


def _action(row: Mapping[str, Any] | None) -> str:
    if row is None:
        return "SKIP"
    future = _future_inputs(row)
    return str(_first_text(future.get("recommended_action"), row.get("action"), row.get("side")) or "SKIP").upper()


def _side_from_action(action: str | None) -> str | None:
    text = str(action or "").upper()
    if "YES" in text:
        return "YES"
    if "NO" in text:
        return "NO"
    return None


def _number(*values: Any) -> float | None:
    for value in values:
        if value in (None, "") or isinstance(value, bool):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _first_text(*values: Any) -> str | None:
    for value in values:
        if value not in (None, ""):
            return str(value)
    return None


def _load_config(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        payload = json.loads(text)
    else:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - yaml is expected in runtime.
            raise RuntimeError("YAML composition configs require PyYAML; use JSON instead") from exc
        payload = yaml.safe_load(text)
    if not isinstance(payload, Mapping):
        raise ValueError("Composition config must be a mapping")
    return dict(payload)


def _root_path(raw: str | None) -> Path:
    if raw in (None, ""):
        return Path()
    path = Path(str(raw))
    return path if path.is_absolute() else ROOT / path


def _output_dir(raw: str | None, name: str) -> Path:
    if raw:
        output = _root_path(raw).resolve()
    else:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = (ROOT / DEFAULT_OUTPUT_ROOT / f"{_slug(name)}_{timestamp}").resolve()
    safe_roots = [root.resolve() for root in SAFE_OUTPUT_ROOTS]
    if not any(output == root or root in output.parents for root in safe_roots):
        raise ValueError("Output directory must be under data/summaries, data/beta_shadow/summaries, or data/derived_reports")
    return output


def _summary_payload(result: Mapping[str, Any]) -> dict[str, Any]:
    return dict(result["summary"])


def _text_report(result: Mapping[str, Any]) -> str:
    summary = result["summary"]
    pnl = summary["pnl"]
    return "\n".join(
        [
            f"Composition {summary['composition']['name']} rows={summary['composed_rows']} candidates={summary['candidate_groups']}",
            f"diagnostics={json.dumps(summary['diagnostics'], sort_keys=True)}",
            f"duplicates={json.dumps(summary['duplicate_lane_rows'], sort_keys=True)}",
            "pnl "
            f"resolved={pnl.get('resolved_rows')} buys={pnl.get('buy_rows')} "
            f"wins={pnl.get('winning_buy_rows')} losses={pnl.get('losing_buy_rows')} "
            f"stake=${pnl.get('total_stake_usd')} pnl=${pnl.get('total_pnl_usd')} roi={pnl.get('roi_pct')}%",
        ]
    )


def _markdown_report(result: Mapping[str, Any]) -> str:
    return _text_report(result) + "\n"


def _slug(value: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in value.lower()).strip("_") or "composition"


if __name__ == "__main__":
    raise SystemExit(main())
