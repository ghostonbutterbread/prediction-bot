#!/usr/bin/env python3
"""Derive paper-only lane decisions from immutable collector snapshots.

The input collector JSONL is never changed.  Outputs are derived research
artifacts; no wallet, accounting, live order, or collector state is mutated.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.file_ops import load_jsonl
from bot.paper_shadow_lanes import (
    build_paper_shadow_lane_resolution_rows,
    summarize_paper_shadow_lane_resolution_rows,
    write_paper_shadow_lane_decisions,
)
from bot.paper_wallets import BETA_PAPER_WALLET_ID, STABLE_PAPER_WALLET_ID
from bot.resolution_feed import run_resolution_feed_once

DEFAULT_NOTIONAL_USD = 10.0
OUTCOME_LIKE_KEYS = frozenset(
    {
        "actual",
        "actual_outcome",
        "actual_source",
        "actual_temp_used",
        "known_after",
        "label_target",
        "resolved_at",
        "resolved_outcome",
        "settled_side",
        "settlement_source",
    }
)


@dataclass(frozen=True, slots=True)
class CollectorLaneReplayResult:
    lane_decision_path: Path
    buy_decision_path: Path
    resolved_row_path: Path
    summary_path: Path
    summary: dict[str, Any]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-path", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--lane", action="append", dest="lanes", required=True)
    parser.add_argument("--resolution-path", action="append", default=[])
    parser.add_argument("--default-notional-usd", type=float, default=DEFAULT_NOTIONAL_USD)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = build_collector_lane_replay(
        snapshot_path=_root_path(args.snapshot_path),
        output_dir=_safe_output_dir(_root_path(args.output_dir)),
        enabled_lanes=args.lanes,
        resolution_paths=[_root_path(path) for path in args.resolution_path],
        default_notional_usd=args.default_notional_usd,
    )
    if args.format == "json":
        print(json.dumps(result.summary, indent=2, sort_keys=True))
    else:
        print(
            "collector_rows={collector_rows} valid={valid_snapshot_rows} invalid={invalid_snapshot_rows} "
            "lane_rows={lane_rows} buys={buy_rows} pnl={pnl}".format(
                collector_rows=result.summary["collector_rows"],
                valid_snapshot_rows=result.summary["valid_snapshot_rows"],
                invalid_snapshot_rows=result.summary["invalid_snapshot_rows"],
                lane_rows=result.summary["lane_rows"],
                buy_rows=result.summary["buy_rows"],
                pnl=result.summary["pnl"]["total_pnl_usd"],
            )
        )
        print(f"output_dir={result.summary['output_dir']}")
    return 0


def build_collector_lane_replay(
    *,
    snapshot_path: Path,
    output_dir: Path,
    enabled_lanes: Iterable[str],
    resolution_paths: Iterable[Path] = (),
    default_notional_usd: float = DEFAULT_NOTIONAL_USD,
) -> CollectorLaneReplayResult:
    """Evaluate configured lanes over stored snapshots and optionally score known resolutions."""
    if default_notional_usd <= 0:
        raise ValueError("default_notional_usd must be positive")
    if not snapshot_path.exists():
        raise FileNotFoundError(snapshot_path)
    lane_ids = _normalized_lanes(enabled_lanes)
    output_dir.mkdir(parents=True, exist_ok=True)
    lane_decision_path = output_dir / "lane_decisions.jsonl"
    buy_decision_path = output_dir / "buy_decisions.jsonl"
    resolved_row_path = output_dir / "resolved_rows.jsonl"
    summary_path = output_dir / "summary.json"
    for path in (lane_decision_path, buy_decision_path, resolved_row_path, summary_path):
        if path.exists():
            raise FileExistsError(f"derived replay output already exists: {path}")

    rows = _load_jsonl(snapshot_path)
    source_digest = _file_sha256(snapshot_path)
    replay_run_id = f"collector_replay:{source_digest[:16]}"
    candidate_dataset_path = str(snapshot_path)
    inputs: dict[str, dict[str, Any]] = {}
    collector_identity: dict[str, dict[str, Any]] = {}
    stable_rows: list[dict[str, Any]] = []
    beta_rows: list[dict[str, Any]] = []
    invalid = 0
    duplicate = 0
    for row_index, raw_row in enumerate(rows, start=1):
        prepared = _prepare_snapshot(
            raw_row,
            candidate_dataset_path=candidate_dataset_path,
            replay_run_id=replay_run_id,
            default_notional_usd=default_notional_usd,
        )
        if prepared is None:
            invalid += 1
            continue
        candidate_id, wallet_input, stable, beta = prepared
        if candidate_id in inputs:
            duplicate += 1
            continue
        collector_identity[candidate_id] = {
            "collector_run_id": str(raw_row.get("run_id") or raw_row.get("snapshot_key")),
            "collector_snapshot_id": str(raw_row.get("run_id") or raw_row.get("snapshot_key")),
            "collector_source_row_index": row_index,
        }
        inputs[candidate_id] = {
            STABLE_PAPER_WALLET_ID: wallet_input,
            BETA_PAPER_WALLET_ID: wallet_input,
        }
        stable_rows.append(stable)
        beta_rows.append(beta)

    lane_config = {
        "paper_shadow_lanes": {
            "enabled": True,
            "enabled_lanes": lane_ids,
            "definitions_dir": str(ROOT / "lanes"),
            "decision_ledger_path": str(lane_decision_path),
        }
    }
    wallet_runs = {
        STABLE_PAPER_WALLET_ID: SimpleNamespace(session_id=replay_run_id),
        BETA_PAPER_WALLET_ID: SimpleNamespace(session_id=replay_run_id),
    }
    write_result = write_paper_shadow_lane_decisions(
        config=lane_config,
        candidate_dataset_path=candidate_dataset_path,
        inputs_by_shared_candidate_id=inputs,
        wallet_decision_rows={STABLE_PAPER_WALLET_ID: stable_rows, BETA_PAPER_WALLET_ID: beta_rows},
        wallet_runs=wallet_runs,
        ledger_root=output_dir,
    )
    lane_definition_digest = _digest_json(lane_config)
    lane_rows = _load_jsonl(lane_decision_path)
    for row in lane_rows:
        identity = collector_identity.get(str(row.get("shared_candidate_id") or ""))
        if identity is None:
            raise ValueError("derived lane row is missing immutable collector identity")
        row.update(identity)
        row["collector_dataset_path"] = candidate_dataset_path
        row["collector_dataset_sha256"] = source_digest
        row["derived_replay_run_id"] = replay_run_id
        row["lane_definition_digest"] = lane_definition_digest
    _write_jsonl(lane_decision_path, lane_rows)
    buy_rows = [row for row in lane_rows if str(row.get("action") or "") in {"BUY_YES", "BUY_NO"}]
    _write_jsonl(buy_decision_path, buy_rows)
    resolution_rows = [row for path in resolution_paths if path.exists() for row in _load_jsonl(path)]
    resolved_rows = build_paper_shadow_lane_resolution_rows(lane_rows=lane_rows, resolution_rows=resolution_rows)
    _write_jsonl(resolved_row_path, resolved_rows)
    pnl = summarize_paper_shadow_lane_resolution_rows(resolved_rows)
    summary = {
        "schema_name": "collector_lane_replay_summary",
        "schema_version": 1,
        "non_mutating": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_path": str(snapshot_path),
        "snapshot_sha256": _file_sha256(snapshot_path),
        "output_dir": str(output_dir),
        "replay_run_id": replay_run_id,
        "enabled_lanes": lane_ids,
        "collector_rows": len(rows),
        "valid_snapshot_rows": len(inputs),
        "invalid_snapshot_rows": invalid,
        "duplicate_snapshot_rows": duplicate,
        "lane_rows": len(lane_rows),
        "buy_rows": len(buy_rows),
        "resolution_rows_loaded": len(resolution_rows),
        "pnl": pnl,
        "lane_decision_path": str(write_result.decision_path),
        "buy_decision_path": str(buy_decision_path),
        "resolved_row_path": str(resolved_row_path),
    }
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return CollectorLaneReplayResult(lane_decision_path, buy_decision_path, resolved_row_path, summary_path, summary)


def auto_resolve_collector_lane_replay(*, output_dir: Path, fetch_market=None, force: bool = False) -> dict[str, Any]:
    """Refresh independent outcomes for a derived replay's BUY-only ledger."""
    output_dir = Path(output_dir)
    buy_path = output_dir / "buy_decisions.jsonl"
    lane_path = output_dir / "lane_decisions.jsonl"
    summary_path = output_dir / "summary.json"
    if not buy_path.exists() or not lane_path.exists() or not summary_path.exists():
        raise FileNotFoundError("derived replay requires lane_decisions.jsonl, buy_decisions.jsonl, and summary.json")
    feed = run_resolution_feed_once(
        {
            "resolution_feed": {
                "enabled": True,
                "decision_ledger_path": str(buy_path),
                "output_dir": str(output_dir / "resolution_feed"),
                "central_output_dir": str(output_dir / "resolution_feed"),
                "mode": "incremental_unresolved",
            }
        },
        fetch_market=fetch_market,
        force=force,
    )
    resolution_rows = _load_jsonl(feed.output_path) if feed.output_path and feed.output_path.exists() else []
    resolved_rows = build_paper_shadow_lane_resolution_rows(lane_rows=_load_jsonl(lane_path), resolution_rows=resolution_rows)
    _write_jsonl(output_dir / "resolved_rows.jsonl", resolved_rows)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["pnl"] = summarize_paper_shadow_lane_resolution_rows(resolved_rows)
    summary["auto_resolution"] = {
        "non_mutating": True,
        "decision_ledger_path": str(buy_path),
        "resolution_feed": {
            "status": feed.status,
            "resolved_market_count": feed.resolved_market_count,
            "unresolved_market_count": feed.unresolved_market_count,
            "fetch_error_count": feed.fetch_error_count,
            "latest_resolution_path": str(feed.output_path) if feed.output_path else None,
        },
    }
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"pnl": summary["pnl"], "resolution_feed": summary["auto_resolution"]["resolution_feed"]}


def _prepare_snapshot(
    raw_row: Mapping[str, Any], *, candidate_dataset_path: str, replay_run_id: str, default_notional_usd: float):
    if not isinstance(raw_row, Mapping):
        return None
    candidate_id = _text(raw_row.get("shared_candidate_id"), _mapping(raw_row.get("shared_candidate")).get("candidate_id"))
    market_id = _text(raw_row.get("market_id"), _mapping(raw_row.get("shared_candidate")).get("market_id"))
    snapshot_id = _text(raw_row.get("run_id"), raw_row.get("snapshot_key"))
    observed_at = _text(raw_row.get("observed_at"), raw_row.get("timestamp"))
    if not candidate_id or not market_id or not snapshot_id or not observed_at:
        return None
    shared_candidate = _scrub_outcome_like_fields(_mapping(raw_row.get("shared_candidate")))
    shared_candidate.update(
        {
            "candidate_id": candidate_id,
            "market_id": market_id,
            "observed_at": observed_at,
            "snapshot_id": snapshot_id,
            "shared_snapshot_id": snapshot_id,
            "source_runtime": "collector_snapshot_replay",
            "provenance": "collector_recorded_as_of",
        }
    )
    signal = _scrub_outcome_like_fields(
        {
            **_mapping(shared_candidate.get("decision")),
            "shared_candidate_id": candidate_id,
            "market_id": market_id,
            "observed_at": observed_at,
            "shared_snapshot_id": snapshot_id,
            "snapshot_id": snapshot_id,
            "question": _text(raw_row.get("question"), _mapping(shared_candidate.get("market")).get("question")),
            "confidence": raw_row.get("confidence", _mapping(shared_candidate.get("decision")).get("confidence")),
            "edge": raw_row.get("edge", _mapping(shared_candidate.get("decision")).get("edge")),
            "model_probability": _mapping(shared_candidate.get("decision")).get("model_probability"),
            "yes_price": raw_row.get("yes_price"),
            "no_price": raw_row.get("no_price"),
            "market_price": raw_row.get("yes_price"),
            "source_details": _mapping(shared_candidate.get("evidence")).get("source_details", []),
        }
    )
    action = _action(_mapping(raw_row.get("main_decision")).get("action"), _mapping(shared_candidate.get("decision")).get("final_action"))
    size = _number(_mapping(raw_row.get("main_decision")).get("size"))
    stable = _source_decision(
        wallet_id=STABLE_PAPER_WALLET_ID,
        candidate_id=candidate_id,
        market_id=market_id,
        observed_at=observed_at,
        action=action,
        size=size if action.startswith("BUY_") and size is not None else (default_notional_usd if action.startswith("BUY_") else 0.0),
        candidate_dataset_path=candidate_dataset_path,
        replay_run_id=replay_run_id,
        decision=_mapping(raw_row.get("main_decision")),
    )
    beta = _source_decision(
        wallet_id=BETA_PAPER_WALLET_ID,
        candidate_id=candidate_id,
        market_id=market_id,
        observed_at=observed_at,
        action="SKIP",
        size=0.0,
        candidate_dataset_path=candidate_dataset_path,
        replay_run_id=replay_run_id,
        decision={"reason_code": "beta_decision_not_recorded", "reason": "Collector snapshot did not include a beta decision"},
    )
    return candidate_id, SimpleNamespace(signal=signal, shared_candidate=shared_candidate), stable, beta


def _source_decision(*, wallet_id: str, candidate_id: str, market_id: str, observed_at: str, action: str, size: float, candidate_dataset_path: str, replay_run_id: str, decision: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "wallet_id": wallet_id,
        "run_id": replay_run_id,
        "candidate_dataset_path": candidate_dataset_path,
        "decision_role": "paper_shadow",
        "decision_id": f"{replay_run_id}:{wallet_id}:{candidate_id}",
        "policy": "collector_recorded_stable" if wallet_id == STABLE_PAPER_WALLET_ID else "collector_beta_unavailable",
        "market_id": market_id,
        "observed_at": observed_at,
        "action": action,
        "reason_code": decision.get("reason_code") or "collector_recorded_decision",
        "reason": decision.get("reason"),
        "confidence": decision.get("confidence"),
        "requested_position_size_usd": size,
        "approved_position_size_usd": size,
        "shared_candidate_id": candidate_id,
    }


def _scrub_outcome_like_fields(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _scrub_outcome_like_fields(item)
            for key, item in value.items()
            if str(key).lower() not in OUTCOME_LIKE_KEYS
        }
    if isinstance(value, list):
        return [_scrub_outcome_like_fields(item) for item in value]
    return copy.deepcopy(value)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [dict(row) for row in load_jsonl(path) if isinstance(row, Mapping)]


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.write_text("".join(json.dumps(dict(row), sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _safe_output_dir(path: Path) -> Path:
    resolved = path.resolve()
    allowed = ((ROOT / "data" / "derived_reports").resolve(), (ROOT / "data" / "summaries").resolve())
    if not any(resolved == root or root in resolved.parents for root in allowed):
        raise ValueError("output_dir must be under data/derived_reports or data/summaries")
    return resolved


def _root_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _normalized_lanes(values: Iterable[str]) -> list[str]:
    lane_ids: list[str] = []
    for value in values:
        lane_id = str(value).strip()
        if lane_id and lane_id not in lane_ids:
            lane_ids.append(lane_id)
    if not lane_ids:
        raise ValueError("at least one lane is required")
    return lane_ids


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(*values: Any) -> str:
    for value in values:
        if value not in (None, ""):
            return str(value)
    return ""


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _action(*values: Any) -> str:
    for value in values:
        action = str(value or "").upper()
        if action in {"BUY_YES", "BUY_NO", "SKIP"}:
            return action
    return "SKIP"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
