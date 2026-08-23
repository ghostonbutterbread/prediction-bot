"""Chronological, non-mutating Kelly capacity diagnostics for collector decisions.

This deliberately reuses recorded actions as a comparison against the legacy
fixed-notional collector-lane diagnostic. It is **not** a paper-wallet or
strategy replay: it models only sizing, capital reservation, and later exact
settlement receipts, and it cannot claim policy/paper-wallet parity.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping


_BUY_ACTIONS = frozenset({"BUY_YES", "BUY_NO"})


def replay_recorded_collector_lane_with_kelly(
    *,
    decisions: Iterable[Mapping[str, Any]],
    resolutions: Iterable[Mapping[str, Any]],
    starting_balance_usd: float = 100.0,
    kelly_fraction: float = 0.5,
    max_bet_pct: float = 0.10,
) -> dict[str, Any]:
    """Run recorded BUY/SKIP actions oldest-to-newest in a synthetic wallet.

    The `model_probability` field means the recorded probability that the
    recorded action's side wins.  Missing probabilities or decision-time prices
    are explicit skips; no price, fill, outcome, or size is invented.
    """
    if starting_balance_usd <= 0:
        raise ValueError("starting_balance_usd must be positive")
    if not 0 < kelly_fraction <= 1:
        raise ValueError("kelly_fraction must be in (0, 1]")
    if not 0 < max_bet_pct <= 1:
        raise ValueError("max_bet_pct must be in (0, 1]")

    ordered_decisions = sorted((dict(row) for row in decisions), key=lambda row: _timestamp(row, "observed_at"))
    resolutions_by_decision = _resolution_map(resolutions)
    cash = float(starting_balance_usd)
    positions: list[dict[str, Any]] = []
    decision_rows: list[dict[str, Any]] = []
    settlement_rows: list[dict[str, Any]] = []

    for ordinal, decision in enumerate(ordered_decisions, start=1):
        observed_at = _timestamp(decision, "observed_at")
        cash, settled = _settle_due_positions(
            positions=positions,
            resolutions_by_decision=resolutions_by_decision,
            cash=cash,
            before_or_at=observed_at,
        )
        settlement_rows.extend(settled)
        action = str(decision.get("action") or "SKIP").upper()
        row = {
            "ordinal": ordinal,
            "decision_id": str(decision.get("decision_id") or f"collector-kelly:{ordinal}"),
            "market_id": str(decision.get("market_id") or ""),
            "observed_at": decision.get("observed_at"),
            "action": action,
            "available_cash_before_usd": _round(cash),
            "reserved_capital_before_usd": _round(_reserved_capital(positions)),
            "wallet_equity_before_usd": _round(cash + _reserved_capital(positions)),
            "status": "skipped",
            "reason_code": None,
            "requested_kelly_fraction": None,
            "approved_stake_usd": 0.0,
        }
        if action not in _BUY_ACTIONS:
            row["reason_code"] = "recorded_skip"
            decision_rows.append(row)
            continue
        probability = _probability(decision.get("model_probability"))
        if probability is None:
            row["reason_code"] = "missing_model_probability"
            decision_rows.append(row)
            continue
        price = _price(
            decision.get("yes_price") if action == "BUY_YES" else decision.get("no_price"),
            decision.get("entry_price"),
            decision.get("price"),
        )
        if price is None:
            row["reason_code"] = "missing_decision_time_price"
            decision_rows.append(row)
            continue
        side_probability = probability if action == "BUY_YES" else 1.0 - probability
        kelly_fraction_raw = max(0.0, (side_probability - price) / (1.0 - price))
        requested_fraction = kelly_fraction * kelly_fraction_raw
        approved_fraction = min(requested_fraction, max_bet_pct)
        stake = min(cash, cash * approved_fraction)
        row.update({
            "model_probability": probability,
            "side_probability": side_probability,
            "entry_price": price,
            "requested_kelly_fraction": _round(requested_fraction),
            "approved_kelly_fraction": _round(approved_fraction),
            "approved_stake_usd": _round(stake),
        })
        if stake <= 0:
            row["reason_code"] = "kelly_nonpositive_or_no_available_cash"
            decision_rows.append(row)
            continue
        market_id = row["market_id"]
        if not market_id:
            row["reason_code"] = "missing_market_id"
            decision_rows.append(row)
            continue
        cash -= stake
        positions.append({
            "decision_id": row["decision_id"],
            "shared_candidate_id": str(decision.get("shared_candidate_id") or ""),
            "run_id": str(decision.get("run_id") or ""),
            "market_id": market_id,
            "action": action,
            "observed_at": observed_at,
            "stake_usd": stake,
            "entry_price": price,
        })
        row["status"] = "opened"
        row["reason_code"] = "kelly_position_opened"
        row["available_cash_after_usd"] = _round(cash)
        row["reserved_capital_after_usd"] = _round(_reserved_capital(positions))
        decision_rows.append(row)

    cash, settled = _settle_due_positions(
        positions=positions,
        resolutions_by_decision=resolutions_by_decision,
        cash=cash,
        before_or_at=None,
    )
    settlement_rows.extend(settled)
    reserved = _reserved_capital(positions)
    summary = {
        "starting_balance_usd": _round(starting_balance_usd),
        "final_balance_usd": _round(cash + reserved),
        "available_cash_usd": _round(cash),
        "reserved_capital_usd": _round(reserved),
        "opened_positions": sum(row["status"] == "opened" for row in decision_rows),
        "settled_positions": len(settlement_rows),
        "void_positions": sum(row["void_resolution"] for row in settlement_rows),
        "open_positions": len(positions),
        "skipped_decisions": sum(row["status"] == "skipped" for row in decision_rows),
        "kelly_policy": {
            "policy_name": "bounded_fractional_kelly_capacity_diagnostic",
            "policy_version": 1,
            "kelly_fraction": kelly_fraction,
            "max_bet_pct": max_bet_pct,
        },
    }
    return {
        "schema_name": "collector_lane_replay_kelly",
        "schema_version": 1,
        "methodology": "recorded_decision_sequential_synthetic_kelly_capacity_diagnostic",
        "non_mutating": True,
        "paper_parity_claim": False,
        "promotion_eligible": False,
        "decision_rows": decision_rows,
        "settlement_rows": settlement_rows,
        "open_positions": [dict(position) for position in positions],
        "summary": summary,
    }


def write_collector_lane_replay_kelly(
    *,
    decision_path: Path,
    resolution_paths: Iterable[Path],
    output_dir: Path,
    starting_balance_usd: float = 100.0,
    kelly_fraction: float = 0.5,
    max_bet_pct: float = 0.10,
) -> dict[str, Any]:
    """Write distinct derived Kelly-wallet artifacts from immutable ledgers."""
    decision_path = Path(decision_path)
    output_dir = Path(output_dir)
    if not decision_path.is_file():
        raise FileNotFoundError(decision_path)
    if output_dir.exists():
        raise FileExistsError(f"derived output already exists: {output_dir}")
    decisions = _load_jsonl(decision_path)
    resolution_paths = tuple(Path(path) for path in resolution_paths)
    resolutions = []
    for path in resolution_paths:
        for row in _load_jsonl(path):
            receipt_row = dict(row)
            receipt_row["resolution_source_path"] = str(path)
            receipt_row["source_row_sha256"] = _canonical_row_sha256(row)
            resolutions.append(receipt_row)
    result = replay_recorded_collector_lane_with_kelly(
        decisions=decisions,
        resolutions=resolutions,
        starting_balance_usd=starting_balance_usd,
        kelly_fraction=kelly_fraction,
        max_bet_pct=max_bet_pct,
    )
    output_dir.mkdir(parents=True)
    decision_rows_path = output_dir / "kelly_wallet_decisions.jsonl"
    settlement_rows_path = output_dir / "kelly_wallet_settlements.jsonl"
    summary_path = output_dir / "summary.json"
    _write_jsonl(decision_rows_path, result["decision_rows"])
    _write_jsonl(settlement_rows_path, result["settlement_rows"])
    summary = {
        "schema_name": result["schema_name"],
        "schema_version": result["schema_version"],
        "methodology": result["methodology"],
        "non_mutating": True,
        "paper_parity_claim": False,
        "promotion_eligible": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "decision_input_path": str(decision_path),
        "resolution_input_paths": [str(Path(path)) for path in resolution_paths],
        "starting_balance_usd": starting_balance_usd,
        "kelly_fraction": kelly_fraction,
        "max_bet_pct": max_bet_pct,
        **result["summary"],
    }
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "decision_rows_path": decision_rows_path,
        "settlement_rows_path": settlement_rows_path,
        "summary_path": summary_path,
        "summary": summary,
    }


def _resolution_map(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw_row in rows:
        row = dict(raw_row)
        nested = dict(row.get("resolution") or {}) if isinstance(row.get("resolution"), Mapping) else {}
        decision_id = str(row.get("decision_id") or row.get("lane_decision_id") or "")
        market_id = str(row.get("market_id") or nested.get("market_id") or "")
        outcome = str(nested.get("outcome") or row.get("outcome") or "").upper()
        settlement_value = nested.get("settlement_ts") or nested.get("outcome_known_at") or row.get("settlement_ts") or row.get("outcome_known_at")
        if not decision_id or not market_id or not outcome or settlement_value in (None, ""):
            continue
        shared_candidate_id = str(row.get("shared_candidate_id") or nested.get("shared_candidate_id") or "")
        run_id = str(row.get("run_id") or nested.get("run_id") or "")
        if nested and (
            nested.get("matched") is not True
            or nested.get("matched_by") in (None, "", "market_id")
            or not nested.get("resolution_row_id")
            or not shared_candidate_id
            or not run_id
        ):
            continue
        try:
            settlement_ts = _parse_timestamp(settlement_value)
        except ValueError:
            continue
        existing = result.get(decision_id)
        candidate = {
            "decision_id": decision_id,
            "market_id": market_id,
            "shared_candidate_id": shared_candidate_id,
            "run_id": run_id,
            "outcome": outcome,
            "settlement_ts": settlement_ts,
            "resolution_receipt": {
                "resolution_row_id": nested.get("resolution_row_id") or row.get("resolution_id") or row.get("decision_id"),
                "matched_by": nested.get("matched_by") or "exact_decision_id_market_id",
                "resolution_source_path": nested.get("resolution_source_path") or row.get("resolution_source_path"),
                "source_row_sha256": row.get("source_row_sha256"),
            },
        }
        if existing is None or settlement_ts < existing["settlement_ts"]:
            result[decision_id] = candidate
    return result


def _settle_due_positions(*, positions: list[dict[str, Any]], resolutions_by_decision: Mapping[str, Mapping[str, Any]], cash: float, before_or_at: datetime | None) -> tuple[float, list[dict[str, Any]]]:
    settled: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []
    for position in positions:
        resolution = resolutions_by_decision.get(position["decision_id"])
        if (
            resolution is None
            or resolution["market_id"] != position["market_id"]
            or (position["shared_candidate_id"] and resolution["shared_candidate_id"] != position["shared_candidate_id"])
            or (position["run_id"] and resolution["run_id"] != position["run_id"])
            or resolution["settlement_ts"] <= position["observed_at"]
        ):
            remaining.append(position)
            continue
        if before_or_at is not None and resolution["settlement_ts"] > before_or_at:
            remaining.append(position)
            continue
        is_void = resolution["outcome"] == "VOID"
        won = resolution["outcome"] == position["action"].removeprefix("BUY_")
        payout = position["stake_usd"] if is_void else (position["stake_usd"] / position["entry_price"] if won else 0.0)
        cash += payout
        settled.append({
            "decision_id": position["decision_id"],
            "market_id": position["market_id"],
            "action": position["action"],
            "settlement_ts": resolution["settlement_ts"].isoformat(),
            "outcome": resolution["outcome"],
            "void_resolution": is_void,
            "stake_usd": _round(position["stake_usd"]),
            "payout_usd": _round(payout),
            "pnl_usd": None if is_void else _round(payout - position["stake_usd"]),
            "resolution_receipt": dict(resolution["resolution_receipt"]),
        })
    positions[:] = remaining
    return cash, settled


def _timestamp(row: Mapping[str, Any], field: str) -> datetime:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} is required and must be an ISO timestamp")
    return _parse_timestamp(value)


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp is required and must be an ISO timestamp")
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return timestamp


def _probability(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if 0 < numeric < 1 else None


def _price(*values: Any) -> float | None:
    for value in values:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if 0 < numeric < 1:
            return numeric
    return None


def _reserved_capital(positions: Iterable[Mapping[str, Any]]) -> float:
    return sum(float(position["stake_usd"]) for position in positions)


def _round(value: float) -> float:
    return round(float(value), 6)


def _canonical_row_sha256(row: Mapping[str, Any]) -> str:
    payload = json.dumps(dict(row), sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if isinstance(row, Mapping):
                rows.append(dict(row))
    return rows


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(dict(row), sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
