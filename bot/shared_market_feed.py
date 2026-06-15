from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from bot.exchanges.base import Market

SCHEMA_NAME = "shared_market_candidate"
SCHEMA_VERSION = 1

SHARED_INPUT_STATUS_RECONSTRUCTABLE = "reconstructable"
SHARED_INPUT_STATUS_PARTIAL = "partial"
SHARED_INPUT_STATUS_MISSING_REQUIRED_FIELDS = "missing_required_fields"
SHARED_INPUT_STATUS_UNSUPPORTED_SCHEMA = "unsupported_schema"


@dataclass(frozen=True, slots=True)
class SharedCandidateInput:
    """Read-only analysis input reconstructed from a shared candidate row."""

    ok: bool
    reconstructable: bool
    status: str
    reason_code: str
    shared_candidate_id: str | None
    market_id: str | None
    market: Market | None
    signal: dict[str, Any]
    provenance: dict[str, Any]
    shared_candidate: dict[str, Any] | None
    source_schema: str | None
    missing_fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SharedCandidateMarketResult:
    """Structured result for conservative Market reconstruction."""

    ok: bool
    status: str
    reason_code: str
    market: Market | None
    missing_fields: tuple[str, ...] = ()


def build_shared_market_candidate_row(
    *,
    run_id: str,
    market: Any,
    signal: dict[str, Any] | None = None,
    decision_artifact: dict[str, Any] | None = None,
    shadow_delta: dict[str, Any] | None = None,
    source_runtime: str,
    provenance: str = "unknown",
    observed_at: str | datetime | None = None,
    snapshot_as_of: str | datetime | None = None,
    snapshot_ttl_seconds: int | float | None = None,
    weather_risk: dict[str, Any] | None = None,
    main_runtime: str | None = None,
    main_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical shared market candidate object.

    The helper is intentionally read-only and deterministic when callers pass the
    same observation timestamps. It copies only JSON-safe values and does not
    mutate market, signal, or decision artifact inputs.
    """
    signal = dict(signal or {})
    metadata = dict(getattr(market, "metadata", {}) or {})
    market_id = str(getattr(market, "id", signal.get("market_id") or ""))
    observed_iso = _iso_timestamp(observed_at)
    snapshot_iso = _iso_timestamp(snapshot_as_of) or _derive_snapshot_as_of(signal, decision_artifact) or observed_iso
    prices = _build_prices(market, signal, decision_artifact)
    evidence = _build_evidence(signal, decision_artifact, weather_risk)
    decision = _build_decision_summary(signal, decision_artifact)
    dual_policy = build_dual_policy_decision_metadata(
        decision_artifact,
        shadow_delta=shadow_delta,
        fallback_signal=signal,
        source_runtime=source_runtime,
        main_runtime=main_runtime,
        main_decision=main_decision,
    )
    row = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "candidate_id": _candidate_id(
            run_id=str(run_id or ""),
            market_id=market_id,
            observed_at=observed_iso,
            source_runtime=str(source_runtime or "unknown"),
        ),
        "run_id": str(run_id or ""),
        "market_id": market_id,
        "observed_at": observed_iso,
        "snapshot_as_of": snapshot_iso,
        "source_runtime": str(source_runtime or "unknown"),
        "provenance": str(provenance or "unknown"),
        "snapshot_ttl_seconds": _json_safe(snapshot_ttl_seconds),
        "market": _build_market_summary(market, metadata),
        "prices": prices,
        "evidence": evidence,
        "decision": decision,
    }
    if dual_policy:
        row.update(dual_policy)
    return _json_safe(row)


def build_dual_policy_decision_metadata(
    decision_artifact: dict[str, Any] | None,
    *,
    shadow_delta: dict[str, Any] | None = None,
    fallback_signal: dict[str, Any] | None = None,
    source_runtime: str | None = None,
    main_runtime: str | None = None,
    main_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build stable-safe side-by-side policy decision metadata.

    The output is derived only from already-recorded decision artifacts and the
    compact shadow delta. It is read-only table/schema metadata: no balances,
    exposure, PnL, order placement, or policy enforcement are changed here.
    """
    fallback_signal = fallback_signal if isinstance(fallback_signal, dict) else {}
    if not isinstance(decision_artifact, dict) and not isinstance(shadow_delta, dict) and not fallback_signal:
        return {}
    normal_decision = _normal_policy_decision(decision_artifact, fallback_signal=fallback_signal)
    if isinstance(shadow_delta, dict) and isinstance(shadow_delta.get("stable"), dict):
        normal_decision = _policy_decision_from_shadow_side(
            shadow_delta.get("stable") or {},
            policy="stable",
            source="shadow_delta.stable",
        )

    active_runtime = str(main_runtime or source_runtime or "unknown")
    metadata: dict[str, Any] = {
        "main_runtime": active_runtime,
        "main_decision": _main_policy_decision(
            main_decision,
            fallback_decision=normal_decision,
            main_runtime=active_runtime,
        ),
        "normal_decision": normal_decision,
    }
    if isinstance(shadow_delta, dict) and isinstance(shadow_delta.get("shadow"), dict):
        metadata["shadow_decision"] = _policy_decision_from_shadow_side(
            shadow_delta.get("shadow") or {},
            policy=_shadow_policy_label(shadow_delta),
            source="shadow_delta.shadow",
        )
        metadata["decision_delta"] = _decision_delta_from_shadow_delta(shadow_delta)
    return _json_safe(metadata)


def shared_candidate_from_market_snapshot_row(row: dict[str, Any]) -> dict[str, Any]:
    """Return an embedded shared candidate or convert a legacy snapshot row.

    This gives future consumers a stable read path while keeping old
    market_snapshots.jsonl rows valid.
    """
    existing = row.get("shared_candidate") if isinstance(row, dict) else None
    if isinstance(existing, dict) and existing.get("schema_name") == SCHEMA_NAME:
        return existing

    market = _LegacyMarket(row)
    signal = {
        "direction": row.get("direction"),
        "confidence": row.get("confidence"),
        "edge": row.get("edge"),
        "yes_market_price": row.get("yes_price"),
        "no_market_price": row.get("no_price"),
    }
    return build_shared_market_candidate_row(
        run_id=str(row.get("run_id") or ""),
        market=market,
        signal=signal,
        decision_artifact=row.get("decision_artifact") if isinstance(row.get("decision_artifact"), dict) else None,
        shadow_delta=row.get("shadow_delta") if isinstance(row.get("shadow_delta"), dict) else None,
        source_runtime=str(row.get("source_runtime") or "prediction_lab"),
        provenance=str(row.get("provenance") or row.get("observation_provenance") or "unknown"),
        observed_at=row.get("observed_at") or row.get("timestamp"),
        snapshot_as_of=row.get("snapshot_as_of") or row.get("observed_at") or row.get("timestamp"),
        snapshot_ttl_seconds=row.get("snapshot_ttl_seconds") or row.get("collector_interval_seconds"),
        weather_risk=row.get("weather_risk") if isinstance(row.get("weather_risk"), dict) else None,
        main_runtime=row.get("main_runtime"),
        main_decision=row.get("main_decision") if isinstance(row.get("main_decision"), dict) else None,
    )


def shared_candidate_id_from_row(row: dict[str, Any] | None) -> str | None:
    """Return a directly stored shared-candidate identifier when present."""
    if not isinstance(row, dict):
        return None
    direct = row.get("shared_candidate_id")
    if direct not in (None, ""):
        return str(direct)
    shared = row.get("shared_candidate")
    if isinstance(shared, dict):
        candidate_id = shared.get("candidate_id")
        if candidate_id not in (None, ""):
            return str(candidate_id)
    return None


def normalize_shared_candidate_input(row: Any) -> SharedCandidateInput:
    """Normalize a shared candidate or legacy snapshot row into analysis inputs.

    This helper is deliberately read-only. It reconstructs a Market and a compact
    signal only when the row has enough already-recorded data; callers must treat
    non-reconstructable results as provenance/reference rows.
    """
    shared_candidate, source_schema = _shared_candidate_for_normalization(row)
    if shared_candidate is None:
        return SharedCandidateInput(
            ok=False,
            reconstructable=False,
            status=SHARED_INPUT_STATUS_UNSUPPORTED_SCHEMA,
            reason_code="unsupported_shared_candidate_schema",
            shared_candidate_id=shared_candidate_id_from_row(row) if isinstance(row, dict) else None,
            market_id=_candidate_market_id_from_any(row),
            market=None,
            signal={},
            provenance={},
            shared_candidate=None,
            source_schema=source_schema,
        )

    source_row = row if isinstance(row, dict) else {}
    provenance = _shared_input_provenance(shared_candidate, source_row, source_schema=source_schema)
    market_result = shared_candidate_market_from_row(row)
    market_id = _optional_text(
        shared_candidate.get("market_id")
        or _mapping(shared_candidate.get("market")).get("id")
        or source_row.get("market_id")
        or source_row.get("snapshot_key")
    )
    signal = _shared_input_signal(shared_candidate, source_row, provenance=provenance)
    if not market_result.ok:
        signal["shared_candidate_reconstructable"] = False
        signal["shared_candidate_input_status"] = market_result.status
        signal["shared_candidate_input_reason_code"] = market_result.reason_code
        return SharedCandidateInput(
            ok=False,
            reconstructable=False,
            status=market_result.status,
            reason_code=market_result.reason_code,
            shared_candidate_id=_optional_text(shared_candidate.get("candidate_id")),
            market_id=market_id,
            market=None,
            signal=signal,
            provenance=provenance,
            shared_candidate=dict(shared_candidate),
            source_schema=source_schema,
            missing_fields=market_result.missing_fields,
        )

    if _optional_text(signal.get("direction")) is None:
        signal["shared_candidate_reconstructable"] = False
        signal["shared_candidate_input_status"] = SHARED_INPUT_STATUS_PARTIAL
        signal["shared_candidate_input_reason_code"] = "missing_signal_direction"
        return SharedCandidateInput(
            ok=False,
            reconstructable=False,
            status=SHARED_INPUT_STATUS_PARTIAL,
            reason_code="missing_signal_direction",
            shared_candidate_id=_optional_text(shared_candidate.get("candidate_id")),
            market_id=market_id,
            market=market_result.market,
            signal=signal,
            provenance=provenance,
            shared_candidate=dict(shared_candidate),
            source_schema=source_schema,
            missing_fields=("signal.direction",),
        )

    signal["shared_candidate_reconstructable"] = True
    signal["shared_candidate_input_status"] = SHARED_INPUT_STATUS_RECONSTRUCTABLE
    signal["shared_candidate_input_reason_code"] = "reconstructable"
    return SharedCandidateInput(
        ok=True,
        reconstructable=True,
        status=SHARED_INPUT_STATUS_RECONSTRUCTABLE,
        reason_code="reconstructable",
        shared_candidate_id=_optional_text(shared_candidate.get("candidate_id")),
        market_id=market_id,
        market=market_result.market,
        signal=signal,
        provenance=provenance,
        shared_candidate=dict(shared_candidate),
        source_schema=source_schema,
    )


def shared_candidate_market_from_row(row: Any) -> SharedCandidateMarketResult:
    """Build a Market from shared-row data only when required fields are sane."""
    shared_candidate, source_schema = _shared_candidate_for_normalization(row)
    if shared_candidate is None:
        return SharedCandidateMarketResult(
            ok=False,
            status=SHARED_INPUT_STATUS_UNSUPPORTED_SCHEMA,
            reason_code="unsupported_shared_candidate_schema",
            market=None,
        )

    source_row = row if isinstance(row, dict) else {}
    shared_market = _mapping(shared_candidate.get("market"))
    prices = _mapping(shared_candidate.get("prices"))
    source_signal = _mapping(source_row.get("signal"))
    artifact = _mapping(source_row.get("decision_artifact"))
    artifact_signal = _mapping(artifact.get("strategy_signal"))
    route = _mapping(shared_market.get("route")) or _mapping(source_row.get("market_route"))
    market_id = _optional_text(
        shared_market.get("id")
        or shared_candidate.get("market_id")
        or source_row.get("market_id")
        or source_row.get("snapshot_key")
        or source_signal.get("market_id")
        or artifact_signal.get("market_id")
    )
    exchange = _optional_text(
        shared_market.get("exchange")
        or source_row.get("exchange")
        or source_signal.get("exchange")
        or artifact_signal.get("exchange")
        or "unknown"
    )
    question = _optional_text(
        shared_market.get("question")
        or source_row.get("question")
        or source_signal.get("question")
        or artifact_signal.get("question")
    )
    category = _optional_text(
        shared_market.get("category")
        or shared_market.get("series")
        or source_row.get("series")
        or source_row.get("group")
        or source_signal.get("category")
        or artifact_signal.get("category")
    )
    yes_price = _coerce_probability(
        prices.get("yes_price"),
        prices.get("yes_market_price"),
        source_row.get("yes_price"),
        source_row.get("yes_market_price"),
        source_signal.get("yes_price"),
        source_signal.get("yes_market_price"),
        artifact_signal.get("yes_price"),
        artifact_signal.get("yes_market_price"),
    )
    no_price = _coerce_probability(
        prices.get("no_price"),
        prices.get("no_market_price"),
        source_row.get("no_price"),
        source_row.get("no_market_price"),
        source_signal.get("no_price"),
        source_signal.get("no_market_price"),
        artifact_signal.get("no_price"),
        artifact_signal.get("no_market_price"),
    )

    missing_fields: list[str] = []
    if market_id is None:
        missing_fields.append("market.id")
    if question is None:
        missing_fields.append("market.question")
    if category is None:
        missing_fields.append("market.category")
    if yes_price is None:
        missing_fields.append("prices.yes_price")
    if no_price is None:
        missing_fields.append("prices.no_price")
    if missing_fields:
        price_fields = {"prices.yes_price", "prices.no_price"}
        reason_code = "invalid_price_fields" if price_fields.intersection(missing_fields) else "missing_market_fields"
        return SharedCandidateMarketResult(
            ok=False,
            status=SHARED_INPUT_STATUS_MISSING_REQUIRED_FIELDS,
            reason_code=reason_code,
            market=None,
            missing_fields=tuple(missing_fields),
        )

    volume = _non_negative_number(shared_market.get("volume"), source_row.get("volume"), source_signal.get("market_volume"), default=0.0)
    liquidity = _non_negative_number(
        shared_market.get("liquidity"),
        source_row.get("liquidity"),
        source_signal.get("liquidity"),
        volume,
        default=volume,
    )
    provenance = _shared_input_provenance(shared_candidate, source_row, source_schema=source_schema)
    metadata = {
        "market_group": shared_market.get("group") or source_row.get("group"),
        "market_family": shared_market.get("family") or route.get("family"),
        "series": shared_market.get("series") or source_row.get("series"),
        "series_ticker": shared_market.get("series_ticker"),
        "event_ticker": shared_market.get("event_ticker") or source_row.get("event_ticker"),
        "market_route": route or None,
        "shared_market": provenance,
    }
    metadata = {key: value for key, value in metadata.items() if value is not None}

    return SharedCandidateMarketResult(
        ok=True,
        status=SHARED_INPUT_STATUS_RECONSTRUCTABLE,
        reason_code="reconstructable",
        market=Market(
            id=str(market_id),
            exchange=str(exchange or "unknown"),
            question=str(question),
            yes_price=float(yes_price),
            no_price=float(no_price),
            volume=float(volume if volume is not None else 0.0),
            liquidity=float(liquidity if liquidity is not None else 0.0),
            closes_at=_coerce_optional_datetime(shared_market.get("closes_at") or source_row.get("closes_at")),
            category=str(category),
            metadata=metadata,
            yes_bid=_coerce_probability(prices.get("best_yes_bid")),
            no_bid=_coerce_probability(prices.get("best_no_bid")),
        ),
    )


def summarize_dual_policy_snapshot_rows(rows: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> dict[str, Any]:
    """Summarize normal/stable vs shadow/beta policy columns from snapshot rows.

    This is intentionally counts-only for the first replay/reporting slice. It
    consumes already-recorded market snapshot rows or embedded shared candidate
    rows and does not attempt PnL/accounting.
    """
    summary = {
        "total_rows": 0,
        "rows_with_normal_decision": 0,
        "rows_with_shadow_decision": 0,
        "normal_buys": 0,
        "normal_skips": 0,
        "shadow_buys": 0,
        "shadow_skips": 0,
        "action_changes": 0,
        "size_changes": 0,
        "skipped_by_shadow": 0,
        "shadow_only_buys": 0,
        "missing_shadow_decision": 0,
    }
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        summary["total_rows"] += 1
        normal = _policy_column(row, "normal_decision")
        shadow = _policy_column(row, "shadow_decision")
        delta = _policy_column(row, "decision_delta")
        normal_buys = _is_buy_action(normal.get("action") if normal else None)
        shadow_buys = _is_buy_action(shadow.get("action") if shadow else None)
        if normal:
            summary["rows_with_normal_decision"] += 1
            if normal_buys:
                summary["normal_buys"] += 1
            elif _is_skip_action(normal.get("action")):
                summary["normal_skips"] += 1
        if shadow:
            summary["rows_with_shadow_decision"] += 1
            if shadow_buys:
                summary["shadow_buys"] += 1
            elif _is_skip_action(shadow.get("action")):
                summary["shadow_skips"] += 1
        else:
            summary["missing_shadow_decision"] += 1
        if _changed(delta, "action_changed", normal, shadow, "action"):
            summary["action_changes"] += 1
        if _changed(delta, "size_changed", normal, shadow, "size"):
            summary["size_changes"] += 1
        if normal_buys and shadow and _is_skip_action(shadow.get("action")):
            summary["skipped_by_shadow"] += 1
        if (normal is not None) and not normal_buys and shadow_buys:
            summary["shadow_only_buys"] += 1
    return summary



def summarize_dual_policy_pnl_snapshot_rows(rows: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> dict[str, Any]:
    """Compare simple hypothetical normal-vs-shadow PnL from dual-policy rows.

    Assumptions are intentionally small and explicit:
    - decision ``size`` / ``requested_position_size`` is treated as USD notional.
    - entry price is taken from the decision first, then row/shared market prices.
    - BUY_YES wins on YES outcomes; BUY_NO wins on NO outcomes; SKIP has zero PnL.
    - no fees, slippage, partial fills, cash ledger, or compounding are modeled.
    Rows without a resolved YES/NO outcome, buy size, or entry price are counted as
    unresolved/insufficient instead of guessed.
    """
    summary = {
        "total_rows": 0,
        "resolved_rows": 0,
        "unresolved_rows": 0,
        "insufficient_rows": 0,
        "normal_hypothetical_pnl": 0.0,
        "shadow_hypothetical_pnl": 0.0,
        "pnl_delta_shadow_minus_normal": 0.0,
        "normal_buy_shadow_skip": {
            "count": 0,
            "resolved_count": 0,
            "avoided_exposure_usd": 0.0,
            "avoided_loss_count": 0,
            "avoided_loss_usd": 0.0,
            "missed_win_count": 0,
            "missed_win_usd": 0.0,
        },
        "normal_buy_shadow_smaller_buy": {
            "count": 0,
            "resolved_count": 0,
            "size_reduction_usd": 0.0,
            "pnl_delta_shadow_minus_normal": 0.0,
        },
        "normal_skip_shadow_buy": {
            "count": 0,
            "resolved_count": 0,
            "shadow_pnl_usd": 0.0,
            "shadow_win_count": 0,
            "shadow_loss_count": 0,
        },
        "assumptions": {
            "size_basis": "decision size/requested_position_size is treated as USD notional",
            "entry_price_precedence": "decision entry/price fields, then row/shared market yes/no price by side",
            "fees_slippage_partial_fills": "not modeled",
        },
    }
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        summary["total_rows"] += 1
        normal = _policy_column(row, "normal_decision")
        shadow = _policy_column(row, "shadow_decision")
        if not normal or not shadow:
            continue
        normal_buy = _is_buy_action(normal.get("action"))
        shadow_buy = _is_buy_action(shadow.get("action"))
        shadow_skip = _is_skip_action(shadow.get("action"))
        normal_skip = _is_skip_action(normal.get("action"))
        normal_size = _decision_size(normal)
        shadow_size = _decision_size(shadow)

        bucket: dict[str, Any] | None = None
        if normal_buy and shadow_skip:
            bucket = summary["normal_buy_shadow_skip"]
            bucket["count"] += 1
            if normal_size is not None:
                bucket["avoided_exposure_usd"] = _round_money(bucket["avoided_exposure_usd"] + normal_size)
        elif normal_buy and shadow_buy and normal_size is not None and shadow_size is not None and shadow_size < normal_size:
            bucket = summary["normal_buy_shadow_smaller_buy"]
            bucket["count"] += 1
            bucket["size_reduction_usd"] = _round_money(bucket["size_reduction_usd"] + (normal_size - shadow_size))
        elif normal_skip and shadow_buy:
            bucket = summary["normal_skip_shadow_buy"]
            bucket["count"] += 1
        else:
            continue

        outcome = _row_outcome(row)
        if outcome not in {"YES", "NO"}:
            summary["unresolved_rows"] += 1
            continue
        summary["resolved_rows"] += 1
        normal_pnl = _decision_hypothetical_pnl(normal, row, outcome)
        shadow_pnl = _decision_hypothetical_pnl(shadow, row, outcome)
        if normal_pnl is None or shadow_pnl is None:
            summary["insufficient_rows"] += 1
            continue
        bucket["resolved_count"] += 1
        summary["normal_hypothetical_pnl"] = _round_money(summary["normal_hypothetical_pnl"] + normal_pnl)
        summary["shadow_hypothetical_pnl"] = _round_money(summary["shadow_hypothetical_pnl"] + shadow_pnl)
        delta = _round_money(shadow_pnl - normal_pnl)
        summary["pnl_delta_shadow_minus_normal"] = _round_money(summary["pnl_delta_shadow_minus_normal"] + delta)
        if bucket is summary["normal_buy_shadow_skip"]:
            if normal_pnl < 0:
                bucket["avoided_loss_count"] += 1
                bucket["avoided_loss_usd"] = _round_money(bucket["avoided_loss_usd"] + abs(normal_pnl))
            elif normal_pnl > 0:
                bucket["missed_win_count"] += 1
                bucket["missed_win_usd"] = _round_money(bucket["missed_win_usd"] + normal_pnl)
        elif bucket is summary["normal_buy_shadow_smaller_buy"]:
            bucket["pnl_delta_shadow_minus_normal"] = _round_money(bucket["pnl_delta_shadow_minus_normal"] + delta)
        elif bucket is summary["normal_skip_shadow_buy"]:
            bucket["shadow_pnl_usd"] = _round_money(bucket["shadow_pnl_usd"] + shadow_pnl)
            if shadow_pnl > 0:
                bucket["shadow_win_count"] += 1
            elif shadow_pnl < 0:
                bucket["shadow_loss_count"] += 1
    return summary


class _LegacyMarket:
    def __init__(self, row: dict[str, Any]):
        self.id = row.get("market_id") or row.get("snapshot_key") or ""
        self.exchange = row.get("exchange") or "unknown"
        self.question = row.get("question") or ""
        self.category = row.get("series") or "unknown"
        self.yes_price = row.get("yes_price")
        self.no_price = row.get("no_price")
        self.volume = row.get("volume")
        self.metadata = {
            "market_group": row.get("group"),
            "series": row.get("series"),
            "market_route": row.get("market_route"),
        }


def _shared_candidate_for_normalization(row: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(row, dict):
        return None, "non_mapping"

    schema_name = row.get("schema_name")
    if schema_name not in (None, "", SCHEMA_NAME):
        return None, str(schema_name)
    if schema_name == SCHEMA_NAME:
        if row.get("schema_version") != SCHEMA_VERSION:
            return None, f"{SCHEMA_NAME}:v{row.get('schema_version')}"
        return dict(row), "shared_candidate_row"

    embedded = row.get("shared_candidate")
    if isinstance(embedded, dict):
        if embedded.get("schema_name") == SCHEMA_NAME:
            if embedded.get("schema_version") != SCHEMA_VERSION:
                return None, f"{SCHEMA_NAME}:v{embedded.get('schema_version')}"
            return dict(embedded), "embedded_shared_candidate"
        return None, str(embedded.get("schema_name") or "unsupported_embedded_shared_candidate")

    if _looks_like_legacy_market_snapshot(row):
        return shared_candidate_from_market_snapshot_row(row), "legacy_market_snapshot"
    return None, "unknown"


def _looks_like_legacy_market_snapshot(row: dict[str, Any]) -> bool:
    market_id = _optional_text(row.get("market_id") or row.get("snapshot_key"))
    observed_at = _optional_text(row.get("observed_at") or row.get("timestamp"))
    has_market_payload = any(row.get(key) not in (None, "") for key in ("question", "yes_price", "no_price", "direction"))
    return market_id is not None and observed_at is not None and has_market_payload


def _candidate_market_id_from_any(row: Any) -> str | None:
    if not isinstance(row, dict):
        return None
    shared = row.get("shared_candidate") if isinstance(row.get("shared_candidate"), dict) else {}
    shared_market = shared.get("market") if isinstance(shared.get("market"), dict) else {}
    return _optional_text(row.get("market_id") or row.get("snapshot_key") or shared.get("market_id") or shared_market.get("id"))


def _shared_input_signal(
    shared_candidate: dict[str, Any],
    source_row: dict[str, Any],
    *,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    shared_market = _mapping(shared_candidate.get("market"))
    prices = _mapping(shared_candidate.get("prices"))
    decision = _mapping(shared_candidate.get("decision"))
    evidence = _mapping(shared_candidate.get("evidence"))
    artifact = _mapping(source_row.get("decision_artifact"))
    artifact_signal = _mapping(artifact.get("strategy_signal"))
    route = _mapping(shared_market.get("route")) or _mapping(source_row.get("market_route"))
    direction = _optional_text(
        artifact_signal.get("direction")
        or source_row.get("direction")
        or decision.get("direction")
        or _mapping(shared_candidate.get("main_decision")).get("action")
        or _mapping(shared_candidate.get("normal_decision")).get("action")
    )
    signal = {
        "shared_candidate_id": _optional_text(shared_candidate.get("candidate_id")),
        "candidate_feed_read_only": True,
        "candidate_source_runtime": _optional_text(shared_candidate.get("source_runtime")),
        "candidate_provenance": _optional_text(shared_candidate.get("provenance")),
        "candidate_observed_at": _optional_text(shared_candidate.get("observed_at")),
        "snapshot_as_of": _optional_text(shared_candidate.get("snapshot_as_of")),
        "snapshot_ttl_seconds": shared_candidate.get("snapshot_ttl_seconds"),
        "market_id": _optional_text(shared_candidate.get("market_id") or shared_market.get("id") or source_row.get("market_id")),
        "question": _optional_text(shared_market.get("question") or source_row.get("question") or artifact_signal.get("question")),
        "exchange": _optional_text(shared_market.get("exchange") or source_row.get("exchange") or artifact_signal.get("exchange") or "unknown"),
        "category": _optional_text(shared_market.get("category") or shared_market.get("series") or source_row.get("series") or source_row.get("group")),
        "group": shared_market.get("group") or source_row.get("group"),
        "series": shared_market.get("series") or source_row.get("series"),
        "event_ticker": shared_market.get("event_ticker") or source_row.get("event_ticker"),
        "series_ticker": shared_market.get("series_ticker"),
        "market_route": route or None,
        "market_family": shared_market.get("family") or route.get("family"),
        "direction": direction,
        "model_probability": source_row.get("model_probability") or decision.get("model_probability") or artifact_signal.get("model_probability"),
        "market_price": source_row.get("market_price") or prices.get("market_price") or _market_price_for_direction(direction, prices),
        "yes_market_price": source_row.get("yes_market_price") or prices.get("yes_market_price") or prices.get("yes_price"),
        "no_market_price": source_row.get("no_market_price") or prices.get("no_market_price") or prices.get("no_price"),
        "yes_price": prices.get("yes_price") or source_row.get("yes_price") or source_row.get("yes_market_price"),
        "no_price": prices.get("no_price") or source_row.get("no_price") or source_row.get("no_market_price"),
        "best_yes_bid": prices.get("best_yes_bid"),
        "best_yes_ask": prices.get("best_yes_ask"),
        "best_no_bid": prices.get("best_no_bid"),
        "best_no_ask": prices.get("best_no_ask"),
        "confidence": source_row.get("confidence") or decision.get("confidence") or artifact_signal.get("confidence"),
        "edge": source_row.get("edge") or decision.get("edge") or artifact_signal.get("edge"),
        "signals": source_row.get("signals") if isinstance(source_row.get("signals"), dict) else evidence.get("signals") or {},
        "source_as_of": artifact_signal.get("source_as_of") or shared_candidate.get("snapshot_as_of"),
        "source_mode": artifact_signal.get("source_mode") or evidence.get("source_mode"),
        "station_id": artifact_signal.get("station_id") or evidence.get("station_id"),
        "source_agreement_score": artifact_signal.get("source_agreement_score") or evidence.get("source_agreement_score"),
        "weather_confidence_score": artifact_signal.get("weather_confidence_score") or evidence.get("weather_confidence_score"),
        "distribution_probability": artifact_signal.get("distribution_probability") or evidence.get("distribution_probability"),
        "shared_market": provenance,
        "shared_market_provenance": provenance,
    }
    return {key: value for key, value in signal.items() if value is not None}


def _shared_input_provenance(
    shared_candidate: dict[str, Any],
    source_row: dict[str, Any],
    *,
    source_schema: str | None,
) -> dict[str, Any]:
    shared_market = _first_mapping(
        source_row.get("shared_market"),
        source_row.get("shared_market_provenance"),
        shared_candidate.get("shared_market"),
        shared_candidate.get("shared_market_provenance"),
    )
    provenance = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "source_schema": source_schema,
        "shared_candidate_id": _optional_text(shared_candidate.get("candidate_id") or source_row.get("shared_candidate_id")),
        "market_id": _optional_text(shared_candidate.get("market_id") or source_row.get("market_id") or source_row.get("snapshot_key")),
        "run_id": _optional_text(shared_candidate.get("run_id") or source_row.get("run_id")),
        "source_runtime": _optional_text(shared_candidate.get("source_runtime") or source_row.get("source_runtime")),
        "source_runtime_instance_id": _optional_text(
            shared_market.get("publisher_instance_id")
            or shared_market.get("shared_feed_instance_id")
            or source_row.get("source_runtime_instance_id")
        ),
        "provenance": _optional_text(shared_candidate.get("provenance") or source_row.get("provenance")),
        "observed_at": _optional_text(shared_candidate.get("observed_at") or source_row.get("observed_at") or source_row.get("timestamp")),
        "snapshot_as_of": _optional_text(shared_candidate.get("snapshot_as_of") or shared_market.get("snapshot_as_of")),
        "snapshot_ttl_seconds": shared_candidate.get("snapshot_ttl_seconds") or source_row.get("snapshot_ttl_seconds"),
        "shared_snapshot_id": _optional_text(shared_market.get("shared_snapshot_id") or shared_market.get("snapshot_id") or source_row.get("shared_snapshot_id")),
        "shared_snapshot_observed_at": _optional_text(shared_market.get("shared_snapshot_observed_at")),
        "shared_snapshot_as_of": _optional_text(shared_market.get("shared_snapshot_as_of") or shared_market.get("snapshot_as_of")),
        "shared_feed_source": _optional_text(shared_market.get("shared_feed_source") or shared_market.get("source")),
        "shared_feed_mode": _optional_text(shared_market.get("shared_feed_mode") or shared_market.get("mode")),
        "publisher_runtime": _optional_text(shared_market.get("publisher_runtime")),
        "publisher_instance_id": _optional_text(shared_market.get("publisher_instance_id")),
        "freshness_status": _optional_text(shared_market.get("freshness_status")),
        "read_only": True,
    }
    return {key: _json_safe(value) for key, value in provenance.items() if value not in (None, "")}


def _build_market_summary(market: Any, metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(getattr(market, "id", "")),
        "exchange": getattr(market, "exchange", None),
        "question": getattr(market, "question", ""),
        "series": metadata.get("series") or getattr(market, "category", None),
        "group": metadata.get("market_group"),
        "family": metadata.get("market_family"),
        "route": metadata.get("market_route"),
        "category": getattr(market, "category", None),
        "event_ticker": metadata.get("event_ticker"),
        "series_ticker": metadata.get("series_ticker"),
        "volume": getattr(market, "volume", None),
        "closes_at": getattr(market, "closes_at", None),
    }


def _build_prices(market: Any, signal: dict[str, Any], decision_artifact: dict[str, Any] | None) -> dict[str, Any]:
    order_book = _first_dict(
        decision_artifact,
        [
            "pre_logic_order_book_snapshot",
            "post_logic_order_book_snapshot",
            "order_book_snapshot",
            "execution_snapshot",
        ],
    )
    data = order_book.get("data") if isinstance(order_book.get("data"), dict) else order_book
    return {
        "yes_price": getattr(market, "yes_price", signal.get("yes_market_price")),
        "no_price": getattr(market, "no_price", signal.get("no_market_price")),
        "market_price": signal.get("market_price"),
        "yes_market_price": signal.get("yes_market_price"),
        "no_market_price": signal.get("no_market_price"),
        "best_yes_bid": data.get("best_yes_bid") if isinstance(data, dict) else None,
        "best_yes_ask": data.get("best_yes_ask") if isinstance(data, dict) else None,
        "best_no_bid": data.get("best_no_bid") if isinstance(data, dict) else None,
        "best_no_ask": data.get("best_no_ask") if isinstance(data, dict) else None,
        "order_book_snapshot": order_book or None,
    }


def _build_evidence(
    signal: dict[str, Any],
    decision_artifact: dict[str, Any] | None,
    weather_risk: dict[str, Any] | None,
) -> dict[str, Any]:
    evidence = {
        "signals": signal.get("signals") if isinstance(signal.get("signals"), dict) else {},
        "weather_risk": weather_risk,
        "source_mode": signal.get("source_mode"),
        "station_id": signal.get("station_id"),
        "source_agreement_score": signal.get("source_agreement_score"),
        "weather_confidence_score": signal.get("weather_confidence_score"),
        "distribution_probability": signal.get("distribution_probability"),
    }
    if isinstance(decision_artifact, dict):
        for key in ("source_snapshots", "weather_source_snapshot", "pre_logic_order_book_snapshot", "post_logic_order_book_snapshot"):
            if key in decision_artifact:
                evidence[key] = decision_artifact.get(key)
        source_context = decision_artifact.get("source_context") if isinstance(decision_artifact.get("source_context"), dict) else {}
        source_data = source_context.get("data") if isinstance(source_context.get("data"), dict) else {}
        weather_snapshot = source_data.get("weather_source_snapshot")
        if isinstance(weather_snapshot, dict) and "weather_source_snapshot" not in evidence:
            evidence["weather_source_snapshot"] = weather_snapshot
    return evidence


def _build_decision_summary(signal: dict[str, Any], decision_artifact: dict[str, Any] | None) -> dict[str, Any]:
    summary = {
        "direction": signal.get("direction"),
        "confidence": signal.get("confidence"),
        "edge": signal.get("edge"),
        "model_probability": signal.get("model_probability"),
        "skip_reason_code": signal.get("skip_reason_code"),
    }
    if isinstance(decision_artifact, dict):
        summary.update(
            {
                "final_action": decision_artifact.get("final_action"),
                "final_reason_code": decision_artifact.get("final_reason_code"),
                "final_reason": decision_artifact.get("final_reason"),
                "decision_latency_ms": decision_artifact.get("decision_latency_ms"),
                "paper_lab": decision_artifact.get("paper_lab"),
                "opportunity_mode": decision_artifact.get("opportunity_mode"),
                "shared_core_decision": decision_artifact.get("shared_core_decision"),
            }
        )
    return summary


def _derive_snapshot_as_of(signal: dict[str, Any], decision_artifact: dict[str, Any] | None) -> str | None:
    for value in (signal.get("source_as_of"), signal.get("source_fetched_at")):
        iso = _iso_timestamp(value)
        if iso:
            return iso
    if isinstance(decision_artifact, dict):
        for snapshot in decision_artifact.get("source_snapshots") or []:
            if isinstance(snapshot, dict):
                for key in ("source_as_of", "fetched_at", "as_of"):
                    iso = _iso_timestamp(snapshot.get(key))
                    if iso:
                        return iso
    return None


def _normal_policy_decision(
    decision_artifact: dict[str, Any] | None,
    *,
    fallback_signal: dict[str, Any],
) -> dict[str, Any]:
    artifact = decision_artifact if isinstance(decision_artifact, dict) else {}
    shared_decision = artifact.get("shared_core_decision") if isinstance(artifact.get("shared_core_decision"), dict) else {}
    signal = artifact.get("strategy_signal") if isinstance(artifact.get("strategy_signal"), dict) else fallback_signal
    reason_code = artifact.get("final_reason_code") or shared_decision.get("reason_code") or signal.get("skip_reason_code")
    return {
        "policy": "stable",
        "source": "decision_artifact",
        "action": artifact.get("final_action") or signal.get("direction"),
        "direction": signal.get("direction"),
        "side": _side_from_action_or_direction(artifact.get("final_action"), signal.get("direction")),
        "reason_code": reason_code,
        "reason": artifact.get("final_reason") or shared_decision.get("reason"),
        "requested_position_size": _coerce_number(shared_decision.get("requested_position_size")),
        "size": _coerce_number(shared_decision.get("requested_position_size")),
        "selected_lane": _selected_lane_from_reasoning(shared_decision.get("reasoning")),
    }


def _main_policy_decision(
    explicit: dict[str, Any] | None,
    *,
    fallback_decision: dict[str, Any],
    main_runtime: str,
) -> dict[str, Any]:
    decision = dict(explicit) if isinstance(explicit, dict) else dict(fallback_decision or {})
    decision["runtime"] = str(decision.get("runtime") or main_runtime or "unknown")
    decision["policy"] = str(decision.get("policy") or fallback_decision.get("policy") or "stable")
    decision["authoritative"] = bool(decision.get("authoritative", True))
    return decision


def _policy_decision_from_shadow_side(side: dict[str, Any], *, policy: str, source: str) -> dict[str, Any]:
    action = side.get("action")
    direction = side.get("direction")
    requested_size = _coerce_number(side.get("requested_position_size"))
    return {
        "policy": policy,
        "source": source,
        "action": action,
        "direction": direction,
        "side": _side_from_action_or_direction(action, direction),
        "reason_code": side.get("reason_code"),
        "reason": side.get("reason"),
        "requested_position_size": requested_size,
        "size": requested_size,
        "selected_lane": side.get("selected_lane"),
    }


def _decision_delta_from_shadow_delta(shadow_delta: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "changed",
        "action_changed",
        "side_changed",
        "buy_decision_changed",
        "reason_changed",
        "size_changed",
        "lane_changed",
        "comparison_complete",
        "action_comparison_available",
        "status",
        "evidence_sources",
    )
    return {key: shadow_delta.get(key) for key in keys if key in shadow_delta}


def _policy_column(row: dict[str, Any], key: str) -> dict[str, Any] | None:
    value = row.get(key)
    if isinstance(value, dict):
        return value
    shared = row.get("shared_candidate")
    if isinstance(shared, dict) and isinstance(shared.get(key), dict):
        return shared.get(key)
    return None


def _is_buy_action(action: Any) -> bool:
    text = str(action or "").upper()
    return text.startswith("BUY_") or text in {"BUY", "YES", "NO"}


def _is_skip_action(action: Any) -> bool:
    text = str(action or "").upper()
    return text in {"SKIP", "HOLD", "NO_TRADE", "REJECT"}


def _changed(delta: dict[str, Any] | None, key: str, normal: dict[str, Any] | None, shadow: dict[str, Any] | None, value_key: str) -> bool:
    if isinstance(delta, dict) and key in delta:
        return bool(delta.get(key))
    if normal is None or shadow is None:
        return False
    return normal.get(value_key) != shadow.get(value_key)


def _shadow_policy_label(shadow_delta: dict[str, Any]) -> str:
    policy = shadow_delta.get("policy") if isinstance(shadow_delta.get("policy"), dict) else {}
    version = policy.get("version") or "beta"
    mode = policy.get("mode") or "shadow"
    return f"{version}_{mode}"


def _side_from_action_or_direction(action: Any, direction: Any) -> str | None:
    text = str(action or direction or "").upper()
    if "YES" in text:
        return "YES"
    if "NO" in text:
        return "NO"
    return None


def _selected_lane_from_reasoning(reasoning: Any) -> Any:
    if not isinstance(reasoning, dict):
        return None
    for key in ("selected_lane", "lane_id"):
        if reasoning.get(key) is not None:
            return reasoning.get(key)
    for section_key in ("lane_sizing", "strategy_lane", "lane_gate"):
        section = reasoning.get(section_key)
        if isinstance(section, dict):
            for key in ("selected_lane", "lane_id", "lane"):
                if section.get(key) is not None:
                    return section.get(key)
    return None


def _coerce_number(value: Any) -> float | int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_probability(*values: Any) -> float | None:
    for value in values:
        number = _coerce_number(value)
        if number is None:
            continue
        price = float(number)
        if 0.0 < price < 1.0:
            return price
    return None


def _non_negative_number(*values: Any, default: float | int | None = None) -> float | int | None:
    for value in values:
        number = _coerce_number(value)
        if number is not None and number >= 0:
            return number
    return default


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _first_mapping(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict):
            return dict(value)
    return {}


def _optional_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _market_price_for_direction(direction: Any, prices: dict[str, Any]) -> Any:
    normalized = str(direction or "").upper()
    if normalized == "BUY_NO":
        return prices.get("no_market_price") or prices.get("no_price")
    return prices.get("yes_market_price") or prices.get("yes_price")


def _coerce_optional_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None
    return None


def _decision_size(decision: dict[str, Any]) -> float | None:
    for key in ("size", "requested_position_size", "position_size", "notional_usd"):
        value = _coerce_number(decision.get(key))
        if value is not None and value > 0:
            return float(value)
    return None


def _row_outcome(row: dict[str, Any]) -> str | None:
    candidates: list[Any] = [row.get("outcome")]
    resolution = row.get("resolution")
    if isinstance(resolution, dict):
        candidates.extend([resolution.get("outcome"), resolution.get("result")])
    shared = row.get("shared_candidate")
    if isinstance(shared, dict):
        candidates.append(shared.get("outcome"))
        shared_resolution = shared.get("resolution")
        if isinstance(shared_resolution, dict):
            candidates.extend([shared_resolution.get("outcome"), shared_resolution.get("result")])
    for value in candidates:
        text = str(value or "").strip().upper()
        if text in {"YES", "NO"}:
            return text
    return None


def _decision_hypothetical_pnl(decision: dict[str, Any], row: dict[str, Any], outcome: str) -> float | None:
    action = str(decision.get("action") or "").upper()
    if _is_skip_action(action):
        return 0.0
    if not _is_buy_action(action):
        return None
    side = _side_from_action_or_direction(action, decision.get("direction"))
    if side not in {"YES", "NO"}:
        return None
    size = _decision_size(decision)
    entry_price = _decision_entry_price(decision, row, side)
    if size is None or entry_price is None or not 0 < entry_price < 1:
        return None
    contracts = size / entry_price
    if side == outcome:
        return _round_money((1 - entry_price) * contracts)
    return _round_money(-size)


def _decision_entry_price(decision: dict[str, Any], row: dict[str, Any], side: str) -> float | None:
    for key in ("entry_price", "quoted_entry_price", "market_price", "price"):
        value = _coerce_number(decision.get(key))
        if value is not None:
            return float(value)
    price_keys = ("yes_market_price", "yes_price") if side == "YES" else ("no_market_price", "no_price")
    for key in price_keys:
        value = _coerce_number(row.get(key))
        if value is not None:
            return float(value)
    prices = row.get("prices") if isinstance(row.get("prices"), dict) else None
    shared = row.get("shared_candidate") if isinstance(row.get("shared_candidate"), dict) else None
    if prices is None and shared is not None and isinstance(shared.get("prices"), dict):
        prices = shared.get("prices")
    if prices is not None:
        for key in price_keys:
            value = _coerce_number(prices.get(key))
            if value is not None:
                return float(value)
    return None


def _round_money(value: float | int) -> float:
    return round(float(value), 4)


def _first_dict(parent: dict[str, Any] | None, keys: list[str]) -> dict[str, Any]:
    if not isinstance(parent, dict):
        return {}
    for key in keys:
        value = parent.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _candidate_id(*, run_id: str, market_id: str, observed_at: str | None, source_runtime: str) -> str:
    payload = json.dumps(
        {
            "schema_name": SCHEMA_NAME,
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "market_id": market_id,
            "observed_at": observed_at,
            "source_runtime": source_runtime,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{source_runtime}:{market_id}:{digest}"


def _iso_timestamp(value: str | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    text = str(value)
    return text or None


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, datetime):
        return _iso_timestamp(value)
    return str(value)
