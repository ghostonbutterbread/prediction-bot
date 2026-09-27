#!/usr/bin/env python3
"""Bounded fixed-locator paired loss-guard recorded-decision diagnostic.

Only sealed decision-time fields reach the fee-aware evaluator. Independent
strict resolutions are opened after all decisions are sealed. No strategy,
wallet, fill, or accounting replay is claimed by this fee-free $10 proxy.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.collector_paths import derived_reports_root
from bot.paper_shadow_lanes import _LaneDefinition, _fee_aware_loss_guard_decision
from bot.replay_outcome_binding import _normalize_strict_resolution

MAX_PROBE_ROWS = 512
MAX_RAW_ROW_BYTES = 2 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_KEYS = ("market_id", "shared_snapshot_id", "shared_candidate_id", "observed_at")


def _object(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.astimezone(timezone.utc) if stamp.tzinfo else None
    except ValueError:
        return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_fixed_probe(path: Path) -> tuple[Path, str | None, int | None, list[dict[str, Any]]]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("invalid probe manifest schema")
    recorded = manifest.get("status") == "completed_bounded_read_only_probe"
    legacy = manifest.get("schema_name") == "paired_loss_guard_probe" and manifest.get("schema_version") == 1
    if not (recorded or legacy):
        raise ValueError("invalid probe manifest schema")
    archive = _object(manifest.get("manifest")) if recorded else manifest
    source_name, source_sha, locators = archive.get("source_path"), archive.get("source_sha256"), manifest.get("locators")
    indexed_bytes = archive.get("indexed_source_bytes") if recorded else None
    if not _text(source_name) or not Path(source_name).is_absolute():
        raise ValueError("probe requires absolute source path")
    if recorded:
        snapshot = _object(manifest.get("snapshot"))
        if (type(indexed_bytes) is not int or indexed_bytes < 1 or
                type(snapshot.get("candidate_count")) is not int or
                not isinstance(locators, list) or snapshot["candidate_count"] != len(locators)):
            raise ValueError("invalid recorded probe extent or candidate count")
    elif not _text(source_sha) or not _SHA.fullmatch(source_sha):
        raise ValueError("probe requires source sha256")
    if not isinstance(locators, list) or not 0 < len(locators) <= MAX_PROBE_ROWS:
        raise ValueError("fixed probe locator count must be 1..512")
    source = Path(source_name).resolve(strict=True)
    if not source.is_file():
        raise ValueError("probe source must be a file")
    if indexed_bytes is not None and source.stat().st_size < indexed_bytes:
        raise ValueError("source shorter than recorded indexed extent")
    seen_markets: set[str] = set()
    seen_offsets: set[int] = set()
    checked: list[dict[str, Any]] = []
    for entry in locators:
        if not isinstance(entry, dict):
            raise ValueError("invalid locator")
        row_number, offset, length = (entry.get(key) for key in ("row_number", "byte_offset", "byte_length"))
        digest = entry.get("payload_sha256")
        if (type(row_number) is not int or row_number < 1 or type(offset) is not int or offset < 0
                or type(length) is not int or not 0 < length <= MAX_RAW_ROW_BYTES
                or not isinstance(digest, str) or not _SHA.fullmatch(digest)
                or any(not _text(entry.get(key)) for key in _KEYS)):
            raise ValueError("invalid locator fields")
        if entry["market_id"] in seen_markets:
            raise ValueError("duplicate market in fixed probe")
        if offset in seen_offsets:
            raise ValueError("duplicate locator offset")
        if recorded and (offset + length > indexed_bytes or entry["shared_snapshot_id"] != snapshot.get("snapshot_id")):
            raise ValueError("locator exceeds indexed extent or mismatches snapshot")
        seen_markets.add(entry["market_id"])
        seen_offsets.add(offset)
        checked.append(entry)
    if recorded:
        # The original probe verified row ordinals/index integrity. Verify only
        # selected raw lines here; an append-only 57-GB source has no full hash.
        return source, None, indexed_bytes, checked
    actual_sha = _sha256_file(source)
    if actual_sha != source_sha:
        raise ValueError("source sha256 mismatch")
    positions = {entry["byte_offset"]: (entry["row_number"], entry["byte_length"]) for entry in checked}
    found = set()
    offset = 0
    with source.open("rb") as stream:
        for number, line in enumerate(stream, 1):
            if offset in positions:
                if positions[offset] != (number, len(line)):
                    raise ValueError("locator row_number or byte_length mismatch")
                found.add(offset)
            offset += len(line)
    if found != set(positions):
        raise ValueError("locator does not identify a raw line boundary")
    return source, actual_sha, None, checked


def _seal(entry: Mapping[str, Any], raw: Mapping[str, Any], *, source: Path) -> dict[str, Any]:
    if any(raw.get(key) != entry[key] for key in _KEYS):
        raise ValueError("locator identity mismatch")
    shared = _object(raw.get("shared_candidate"))
    for key, nested_key in (("market_id", "market_id"), ("shared_candidate_id", "candidate_id"), ("shared_snapshot_id", "shared_snapshot_id"), ("observed_at", "observed_at")):
        if shared.get(nested_key) not in (None, "", entry[key]):
            raise ValueError("shared candidate identity mismatch")
    if _timestamp(entry["observed_at"]) is None:
        raise ValueError("invalid locator observed_at")
    artifact = _object(raw.get("decision_artifact"))
    main = _object(raw.get("main_decision"))
    final_action = artifact.get("final_action")
    if main.get("action") in ("BUY_YES", "BUY_NO", "SKIP") and final_action in ("BUY_YES", "BUY_NO", "SKIP") and main["action"] != final_action:
        raise ValueError("conflicting recorded actions")
    recorded_action = main.get("action") if "action" in main else final_action
    if recorded_action is not None and recorded_action not in ("BUY_YES", "BUY_NO", "SKIP"):
        raise ValueError("unsupported recorded baseline action")
    action = recorded_action or "SKIP"
    signal = _object(artifact.get("strategy_signal"))
    shared_decision = _object(shared.get("decision"))
    probability = _number(signal.get("model_probability"))
    if probability is None:
        probability = _number(shared_decision.get("model_probability"))
    artifact_book = _object(artifact.get("order_book_snapshot"))
    artifact_data = _object(artifact_book.get("data"))
    book = _object(_object(shared.get("prices")).get("order_book_snapshot"))
    book_data = _object(book.get("data"))
    side = action.removeprefix("BUY_") if action in ("BUY_YES", "BUY_NO") else None
    ask_key = "best_yes_ask" if side == "YES" else "best_no_ask"
    book_time, candidate_time, observed_time = (_timestamp(book.get("observed_at")),
                                                 _timestamp(shared.get("observed_at")),
                                                 _timestamp(entry["observed_at"]))
    valid_book = (
        side is not None and artifact_book.get("source") == "book" and book.get("source") == "book"
        and book.get("market_id") == entry["market_id"]
        and book_time is not None and candidate_time is not None and observed_time is not None
        and book_time <= candidate_time <= observed_time
    )
    recorded_ask = _number(book_data.get(ask_key)) if valid_book else None
    ask = recorded_ask if recorded_ask == _number(artifact_data.get(ask_key)) else None
    fill = _number(_object(artifact.get("execution_snapshot")).get("estimated_fill_price"))
    market = _object(shared.get("market"))
    return {
        **{key: entry[key] for key in _KEYS},
        "source_path": str(source), "row_number": entry["row_number"],
        "byte_offset": entry["byte_offset"], "byte_length": entry["byte_length"],
        "raw_row_sha256": entry["payload_sha256"],
        "baseline_action": action, "side": side, "yes_probability": probability,
        "side_order_book_ask": ask, "estimated_fill_price": fill,
        "book_market_id": book.get("market_id") if valid_book else None,
        "book_observed_at": book.get("observed_at") if valid_book else None,
        "event_ticker": _text(market.get("event_ticker")) or _text(shared.get("event_ticker")),
    }


def _seal_all(source: Path, locators: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sealed = []
    with source.open("rb") as stream:
        for entry in locators:
            if entry["byte_offset"]:
                stream.seek(entry["byte_offset"] - 1)
                if stream.read(1) != b"\n":
                    raise ValueError("locator does not start at a raw line boundary")
            stream.seek(entry["byte_offset"])
            raw = stream.read(entry["byte_length"])
            if hashlib.sha256(raw).hexdigest() != entry["payload_sha256"] or not raw.endswith(b"\n"):
                raise ValueError("locator raw line sha256/newline mismatch")
            try:
                parsed = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("locator invalid raw JSON") from exc
            if not isinstance(parsed, dict):
                raise ValueError("locator raw row must be an object")
            sealed.append(_seal(entry, parsed, source=source))
    return sealed


def _apply_guard(sealed: list[dict[str, Any]], *, min_side_ask: float, min_fee_aware_net_edge: float, fee_rate: float) -> None:
    lane = _LaneDefinition("shadow_fee_aware_loss_guard", lane_type="fee_aware_loss_guard", parameters={
        "min_executable_ask": min_side_ask, "min_fee_aware_net_edge": min_fee_aware_net_edge, "fee_rate": fee_rate,
    })
    for decision in sealed:
        action = decision["baseline_action"]
        ask = decision["side_order_book_ask"]
        signal = {"model_probability": decision["yes_probability"],
                  "estimated_fill_price": decision["estimated_fill_price"]}
        if action in ("BUY_YES", "BUY_NO") and ask is not None:
            signal["best_yes_ask" if action == "BUY_YES" else "best_no_ask"] = ask
        source_row = {"action": action, "market_id": decision["market_id"],
                      "observed_at": decision["observed_at"]}
        shared_candidate = {
            "market_id": decision["market_id"], "observed_at": decision["observed_at"],
            "prices": {"order_book_snapshot": {
                "source": "book", "market_id": decision["book_market_id"],
                "observed_at": decision["book_observed_at"],
                "data": {"best_yes_ask" if action == "BUY_YES" else "best_no_ask": ask},
            }} if ask is not None else {},
        }
        evaluated = _fee_aware_loss_guard_decision(lane, signal, source_row, shared_candidate)
        decision["candidate_action"] = evaluated["action"]
        decision["candidate_reason_code"] = evaluated.get("reason_code")
        decision["payout_aware"] = evaluated.get("payout_aware")


def _strict_resolutions(path: Path, markets: set[str]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    receipts: dict[str, dict[str, Any]] = {}
    ambiguous: set[str] = set()
    with path.open("rb") as stream:
        for number, raw in enumerate(stream, 1):
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid strict resolution JSONL line {number}") from exc
            if not isinstance(row, dict):
                raise ValueError("strict resolution row must be object")
            market_id = row.get("market_id")
            if market_id not in markets:
                continue
            if market_id in receipts:
                ambiguous.add(market_id)
                continue
            normalized = (
                _normalize_strict_resolution(row, market_id)
                if row.get("saved_market_id", market_id) == market_id else None
            )
            receipts[market_id] = {
                "normalized": normalized, "source_path": str(path), "source_line_number": number,
                "raw_row_sha256": hashlib.sha256(raw).hexdigest(),
            }
    return receipts, ambiguous


def _summarize(sealed: list[dict[str, Any]], receipts: Mapping[str, Mapping[str, Any]], ambiguous: set[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    paired = []
    counts: Counter[str] = Counter()
    metrics = {name: {"winning_buys": 0, "losing_buys": 0, "proxy_pnl_usd": 0.0, "proxy_priced_buys": 0} for name in ("control", "candidate")}
    events: dict[str, Counter[str]] = {}
    for decision in sealed:
        market = decision["market_id"]
        event = decision["event_ticker"] or f"contract_only:{market}"
        events.setdefault(event, Counter())["markets"] += 1
        receipt = receipts.get(market)
        normalized = receipt.get("normalized") if receipt else None
        resolution = {key: receipt[key] for key in ("source_path", "source_line_number", "raw_row_sha256")} if receipt else None
        if market in ambiguous:
            blocker = "ambiguous_resolution"
        elif receipt is None:
            blocker = "missing_resolution"
        elif normalized is None:
            blocker = "invalid_resolution"
        elif _timestamp(normalized["settlement_ts"]) <= _timestamp(decision["observed_at"]):
            blocker = "settlement_not_after_decision"
        elif normalized["official_outcome"] == "VOID":
            blocker = "void_resolution"
        else:
            blocker = None
        outcome = normalized["official_outcome"] if blocker in (None, "void_resolution") else None
        if blocker == "void_resolution":
            counts["void"] += 1
        elif blocker:
            counts["unresolved"] += 1
            counts[blocker] += 1
        if decision["baseline_action"].startswith("BUY_"):
            if decision["yes_probability"] is None:
                counts["missing_probability"] += 1
            if decision["side_order_book_ask"] is None or not 0 < decision["side_order_book_ask"] < 1:
                counts["missing_quote"] += 1
        for label, action_key in (("control", "baseline_action"), ("candidate", "candidate_action")):
            if blocker is not None or decision[action_key] not in ("BUY_YES", "BUY_NO"):
                continue
            won = decision["side"] == outcome
            metric = metrics[label]
            metric["winning_buys" if won else "losing_buys"] += 1
            events[event][f"{label}_{'winning' if won else 'losing'}_buys"] += 1
            ask = decision["side_order_book_ask"]
            if ask is not None and 0 < ask < 1:
                metric["proxy_priced_buys"] += 1
                metric["proxy_pnl_usd"] += 10.0 / ask - 10.0 if won else -10.0
        paired.append({
            "market_id": market, "shared_snapshot_id": decision["shared_snapshot_id"],
            "shared_candidate_id": decision["shared_candidate_id"], "observed_at": decision["observed_at"],
            "event_ticker": decision["event_ticker"], "baseline_action": decision["baseline_action"],
            "candidate_action": decision["candidate_action"], "blocker": blocker,
            "resolution": {
                **resolution,
                "outcome": outcome,
                "settlement_ts": normalized["settlement_ts"] if normalized and blocker in (None, "void_resolution") else None,
            } if resolution else None,
        })
    counts["rows"] = len(sealed)
    for metric in metrics.values():
        metric["proxy_pnl_usd"] = round(metric["proxy_pnl_usd"], 4)
    wins = metrics["control"]["winning_buys"]
    return paired, {
        "counts": {key: counts[key] for key in ("rows", "unresolved", "void", "missing_quote", "missing_probability", "missing_resolution", "invalid_resolution", "ambiguous_resolution", "settlement_not_after_decision")},
        **metrics, "win_retention": metrics["candidate"]["winning_buys"] / wins if wins else None,
        "events": {event: dict(sorted(count.items())) for event, count in sorted(events.items())},
    }


def build_paired_loss_guard_probe(*, probe_manifest_path: str | Path, strict_resolutions_path: str | Path, output_dir: str | Path, min_side_ask: float = .20, min_fee_aware_net_edge: float = .03, fee_rate: float = .07) -> dict[str, Any]:
    """Seal fixed decisions, then independently score exact finalized markets."""
    for label, value in (("min_side_ask", min_side_ask), ("min_fee_aware_net_edge", min_fee_aware_net_edge), ("fee_rate", fee_rate)):
        number = _number(value)
        if number is None or not 0 <= number < 1 or (label == "min_side_ask" and number <= 0):
            raise ValueError(f"invalid {label}")
    probe = Path(probe_manifest_path).resolve(strict=True)
    strict = Path(strict_resolutions_path).resolve(strict=True)
    target = Path(output_dir).resolve()
    allowed_roots = ((ROOT / "data" / "derived_reports").resolve(), derived_reports_root().resolve())
    if target.exists() or not any(root in target.parents for root in allowed_roots):
        raise ValueError("output_dir must be a NEW directory below data/derived_reports")
    if probe == strict:
        raise ValueError("probe manifest and strict resolution must be separate")
    source, source_sha, indexed_bytes, locators = _read_fixed_probe(probe)
    if source == strict or source == probe:
        raise ValueError("source, probe, resolution artifacts must be distinct")
    sealed = _seal_all(source, locators)
    _apply_guard(sealed, min_side_ask=min_side_ask, min_fee_aware_net_edge=min_fee_aware_net_edge, fee_rate=fee_rate)
    # Nothing outcome-bearing was read or passed to the gate above this line.
    receipts, ambiguous = _strict_resolutions(strict, {decision["market_id"] for decision in sealed})
    paired, summary = _summarize(sealed, receipts, ambiguous)
    report = {
        "schema_name": "paired_loss_guard_recorded_decision_diagnostic", "schema_version": 1,
        "non_mutating": True, "promotion_eligible": False, "paper_parity_claim": False,
        "method": "same_market_fixed_locator_recorded_decision_fee_free_10_usd_quote_proxy",
        "price_basis": "selected_side_decision_time_order_book_ask_not_executable_fill_or_realized_pnl",
        "settings": {"min_side_ask": min_side_ask, "min_fee_aware_net_edge": min_fee_aware_net_edge, "fee_rate": fee_rate, "proxy_stake_usd": 10.0},
        "inputs": {"source_path": str(source), "source_sha256": source_sha,
                   "source_indexed_bytes": indexed_bytes, "verified_locator_count": len(locators),
                   "probe_path": str(probe), "probe_sha256": _sha256_file(probe),
                   "resolution_path": str(strict), "resolution_sha256": _sha256_file(strict)},
        **summary,
    }
    sealed_bytes = b"".join((json.dumps(row, sort_keys=True) + "\n").encode() for row in sealed)
    paired_bytes = b"".join((json.dumps(row, sort_keys=True) + "\n").encode() for row in paired)
    report["outputs"] = {name: {"sha256": hashlib.sha256(payload).hexdigest(), "rows": len(sealed)} for name, payload in (("sealed_decisions.jsonl", sealed_bytes), ("paired_rows.jsonl", paired_bytes))}
    # The collector archive can append after this probe was captured. For its
    # hash-verified fixed locators, re-read only the selected lines; never hash
    # or scan the entire growing source to publish a bounded diagnostic.
    if source_sha is not None and _sha256_file(source) != source_sha:
        raise ValueError("input changed before publication: source_sha256")
    if indexed_bytes is not None and source.stat().st_size < indexed_bytes:
        raise ValueError("input changed before publication: source_indexed_bytes")
    if _seal_all(source, locators) != [
        {key: value for key, value in row.items() if key not in ("candidate_action", "candidate_reason_code", "payout_aware")}
        for row in sealed
    ]:
        raise ValueError("selected raw rows changed before publication")
    for key, path in (("probe_sha256", probe), ("resolution_sha256", strict)):
        if _sha256_file(path) != report["inputs"][key]:
            raise ValueError(f"input changed before publication: {key}")
    target.mkdir(parents=True, exist_ok=False)
    (target / "sealed_decisions.jsonl").write_bytes(sealed_bytes)
    (target / "paired_rows.jsonl").write_bytes(paired_bytes)
    (target / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-manifest", required=True)
    parser.add_argument("--strict-resolutions", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--min-side-ask", type=float, default=.20)
    parser.add_argument("--min-fee-aware-net-edge", type=float, default=.03)
    parser.add_argument("--fee-rate", type=float, default=.07)
    args = parser.parse_args(argv)
    result = build_paired_loss_guard_probe(
        probe_manifest_path=args.probe_manifest, strict_resolutions_path=args.strict_resolutions,
        output_dir=args.output_dir, min_side_ask=args.min_side_ask,
        min_fee_aware_net_edge=args.min_fee_aware_net_edge, fee_rate=args.fee_rate,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
