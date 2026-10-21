"""Materialize paper-only source observations from sealed replay inputs.

Pending rows contain only decision-time source evidence.  Finalized replay
outcomes are joined later by the binder's complete sealed decision identity;
they are never copied back into the pending artifact.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bot.replay_decision_input import verify_replay_decision_input_record_v1
from bot.weather.source_scoreboard import extract_source_forecast_observations
from bot.weather.thresholds import (
    infer_direction_from_value, infer_predicted_outcome,
    extract_forecast_interval, infer_range_forecast_outcome,
)


SCHEMA_VERSION = 1
PENDING_FILENAME = "pending_source_observations.jsonl"
SETTLED_FILENAME = "settled_source_correctness.jsonl"
UNSETTLED_FILENAME = "unsettled_or_unusable_source_observations.jsonl"
VOID_FILENAME = "void_source_observations.jsonl"
METADATA_FILENAME = "run_metadata.json"
_IDENTITY_FIELDS = (
    "shared_snapshot_id", "shared_candidate_id", "market_id", "observed_at_utc", "raw_row_sha256",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_ELIGIBLE_TARGET_PROOF = "eligible_exact_target_proof"
_ELIGIBLE_STRICT_SOURCE_PROOF = "eligible_strict_source_proof"
_TARGET_ALIASES = ("source_target_date", "target_forecast_date", "forecast_target", "forecast_date")


class SourceObservationLedgerError(ValueError):
    """The source-observation evidence cannot be materialized fail-closed."""


@dataclass(frozen=True, slots=True)
class SourceObservationLedgerResult:
    output_dir: Path
    pending_path: Path
    settled_path: Path
    unsettled_path: Path
    void_path: Path
    metadata_path: Path
    metadata: dict[str, Any]


def materialize_source_observation_ledger(
    *, replay_inputs_path: str | Path, finalized_outcomes_path: str | Path, output_dir: str | Path,
    strict_resolutions_path: str | Path | None = None,
) -> SourceObservationLedgerResult:
    """Write versioned pending/settled/unusable source artifacts from derived inputs.

    This intentionally has no router, price, action, wallet, or service surface.
    Duplicate identical observations are collapsed; conflicting identities abort
    before output is written. Conflicting authoritative outcomes are retained as
    unusable observations rather than selecting an arbitrary result.
    """

    inputs_path, outcomes_path, target_dir = (Path(replay_inputs_path).resolve(), Path(finalized_outcomes_path).resolve(), Path(output_dir).resolve())
    if not inputs_path.is_file() or not outcomes_path.is_file():
        raise SourceObservationLedgerError("replay inputs and finalized outcomes must be readable files")
    _prepare_output_dir(target_dir)

    input_sha256, outcome_sha256 = _sha256_file(inputs_path), _sha256_file(outcomes_path)
    counters: Counter[str] = Counter()
    strict_rejection_counts: Counter[str] = Counter()
    pending_by_id: dict[str, dict[str, Any]] = {}
    for _line_number, row in _read_jsonl(inputs_path):
        counters["input_records_seen"] += 1
        if not verify_replay_decision_input_record_v1(row):
            counters["invalid_input_records"] += 1
            continue
        identity = _decision_identity(row)
        if identity is None:
            counters["invalid_input_records"] += 1
            continue
        pending_rows = _pending_rows_for_input(row, identity)
        if not pending_rows:
            counters["inputs_without_source_observations"] += 1
        for pending in pending_rows:
            counters["source_observations_seen"] += 1
            strict_proof = pending.get("strict_source_proof")
            if isinstance(strict_proof, Mapping) and strict_proof.get("status") != "eligible":
                for reason in strict_proof.get("reasons") or ():
                    strict_rejection_counts[str(reason)] += 1
            observation_id = pending["source_observation_id"]
            existing = pending_by_id.get(observation_id)
            if existing is None:
                pending_by_id[observation_id] = pending
            elif _canonical_bytes(existing) == _canonical_bytes(pending):
                counters["duplicate_input_source_observations"] += 1
            else:
                raise SourceObservationLedgerError(f"conflicting duplicate source observation ID: {observation_id}")

    outcome_index, conflicting_outcomes = _outcome_index(outcomes_path, counters)
    if strict_resolutions_path is not None:
        _attach_resolver_knowledge(outcome_index.values(), Path(strict_resolutions_path))
    pending_rows = list(pending_by_id.values())
    settled_rows: list[dict[str, Any]] = []
    unusable_rows: list[dict[str, Any]] = []
    void_rows: list[dict[str, Any]] = []
    for pending in pending_rows:
        eligibility = pending.get("source_correctness_eligibility")
        if eligibility not in {_ELIGIBLE_TARGET_PROOF, _ELIGIBLE_STRICT_SOURCE_PROOF}:
            reason = str(eligibility or "unusable_legacy_target_unproven")
            counters[reason] += 1
            unusable_rows.append(_unusable_row(pending, reason))
            continue
        identity_key = _identity_key(pending["canonical_input_sha256"], pending["decision_key"])
        if identity_key in conflicting_outcomes:
            unusable_rows.append(_unusable_row(pending, "conflicting_exact_authoritative_outcome"))
            continue
        outcome = outcome_index.get(identity_key)
        if outcome is None:
            unusable_rows.append(_unusable_row(pending, "missing_exact_authoritative_outcome"))
            continue
        if outcome["official_outcome"] == "VOID":
            counters["void_resolution"] += 1
            void_rows.append(_void_row(pending, outcome))
            continue
        observed_time = _parse_time(pending.get("input_observed_at"))
        source_time = _parse_time(pending.get("source_as_of"))
        fetched_time = _parse_time(pending.get("source_fetched_at"))
        settlement_time = _parse_time(outcome["settlement_ts"])
        chronology_problem = None
        if observed_time is None or settlement_time is None or observed_time >= settlement_time:
            chronology_problem = "input_observed_at_not_before_settlement"
        elif source_time is not None and source_time > observed_time:
            chronology_problem = "source_as_of_after_immutable_observed_at"
        elif fetched_time is not None and fetched_time > observed_time:
            chronology_problem = "source_fetched_at_after_immutable_observed_at"
        if chronology_problem:
            if chronology_problem not in pending["strict_source_proof"]["reasons"]:
                strict_rejection_counts[chronology_problem] += 1
            unusable_rows.append(_unusable_row(pending, chronology_problem, outcome=outcome))
            continue
        implied = _source_implied_outcome(pending)
        if implied is None:
            unusable_rows.append(_unusable_row(pending, pending.get("forecast_interval", {}).get("status")
                if pending.get("forecast_interval", {}).get("status") not in {None, "supported"}
                else "unavailable_source_implied_side", outcome=outcome))
            continue
        settled_rows.append({
            **pending,
            "schema_name": "settled_source_correctness",
            "schema_version": SCHEMA_VERSION,
            "source_implied_outcome": implied,
            "official_outcome": outcome["official_outcome"],
            "direction_correct": implied == outcome["official_outcome"],
            "eligible_for_reliability": True,
            "eligible_for_source_history": eligibility == _ELIGIBLE_STRICT_SOURCE_PROOF,
            "eligible_for_predictive_history": (
                pending["predictive_source_proof"]["status"] == "eligible_predictive_source_proof"
                and predictive_context_supported(pending)
                and outcome.get("resolver_knowledge_verified") is True
            ),
            "predictive_availability_field": "known_after",
            "availability_field": "settlement_ts",
            "settlement_ts": outcome["settlement_ts"],
            "known_after": outcome.get("known_after") if strict_resolutions_path is not None else outcome["settlement_ts"],
            "resolver_knowledge_verified": outcome.get("resolver_knowledge_verified", False),
            "predictive_history_reason": outcome.get("predictive_history_reason"),
            "resolution_id": outcome["resolution_id"],
            "resolution_resolved_at": outcome.get("resolution_resolved_at"),
            "resolution_retrieved_at": outcome.get("resolution_retrieved_at"),
            "resolution_provenance": outcome.get("provenance"),
        })

    pending_bytes = _jsonl_bytes(pending_rows)
    settled_bytes = _jsonl_bytes(settled_rows)
    unusable_bytes = _jsonl_bytes(unusable_rows)
    void_bytes = _jsonl_bytes(void_rows)
    pending_path, settled_path, unusable_path, void_path = (
        target_dir / PENDING_FILENAME, target_dir / SETTLED_FILENAME, target_dir / UNSETTLED_FILENAME, target_dir / VOID_FILENAME,
    )
    pending_path.write_bytes(pending_bytes)
    settled_path.write_bytes(settled_bytes)
    unusable_path.write_bytes(unusable_bytes)
    void_path.write_bytes(void_bytes)
    metadata = {
        "schema_name": "source_observation_ledger_materialization",
        "schema_version": SCHEMA_VERSION,
        "mode": "offline_derived_source_observation_materialization",
        "non_mutating": True,
        "network_access": False,
        "router_selection_used": False,
        "settlement_join": "exact canonical_input_sha256 plus complete decision_key",
        "history_availability_field": "settlement_ts",
        "history_eligibility": "source_as_of <= input_observed_at < settlement_ts < future_decision_time",
        "inputs": {
            "replay_inputs_path": str(inputs_path), "replay_inputs_sha256": input_sha256,
            "finalized_outcomes_path": str(outcomes_path), "finalized_outcomes_sha256": outcome_sha256,
        },
        "counts": {
            **{key: int(counters[key]) for key in (
                "input_records_seen", "invalid_input_records", "inputs_without_source_observations",
                "source_observations_seen", "duplicate_input_source_observations", "outcome_records_seen",
                "invalid_outcomes", "duplicate_outcomes", "conflicting_outcomes",
                "unusable_legacy_target_mismatch", "unusable_legacy_target_unproven",
                "unusable_v1_target_mismatch", "unusable_v1_target_unproven",
                "unusable_v1_forecast_not_scoreable",
            )},
            "pending": len(pending_rows), "settled": len(settled_rows), "unsettled_or_unusable": len(unusable_rows),
            "void_resolution": len(void_rows),
            "strict_proof_rejection_counts": dict(sorted(strict_rejection_counts.items())),
            "predictive_history_eligible": sum(row["eligible_for_predictive_history"] for row in settled_rows),
        },
        "output_artifacts": {
            PENDING_FILENAME: {"sha256": hashlib.sha256(pending_bytes).hexdigest(), "record_count": len(pending_rows)},
            SETTLED_FILENAME: {"sha256": hashlib.sha256(settled_bytes).hexdigest(), "record_count": len(settled_rows)},
            UNSETTLED_FILENAME: {"sha256": hashlib.sha256(unusable_bytes).hexdigest(), "record_count": len(unusable_rows)},
            VOID_FILENAME: {"sha256": hashlib.sha256(void_bytes).hexdigest(), "record_count": len(void_rows)},
        },
    }
    metadata_path = target_dir / METADATA_FILENAME
    metadata_path.write_bytes(_canonical_bytes(metadata))
    return SourceObservationLedgerResult(target_dir, pending_path, settled_path, unusable_path, void_path, metadata_path, metadata)


def is_eligible_for_future_history(row: Mapping[str, Any], future_decision_time: str) -> bool:
    """Return whether a source-correctness row was authoritative before a decision.

    Resolution retrieval timestamps are audit-only. Source evidence must exist
    at capture, capture must precede settlement, and settlement must precede the
    future decision, strictly.
    """

    if row.get("eligible_for_source_history") is not True or row.get("source_correctness_eligibility") != _ELIGIBLE_STRICT_SOURCE_PROOF:
        return False
    settlement, future = _parse_time(row.get("settlement_ts")), _parse_time(future_decision_time)
    source, observed = _parse_time(row.get("source_as_of")), _parse_time(row.get("input_observed_at"))
    fetched = _parse_time(row.get("source_fetched_at"))
    return (
        source is not None and observed is not None and settlement is not None and future is not None
        and source <= observed < settlement < future
        and (row.get("source_fetched_at") is None or (fetched is not None and fetched <= observed))
    )


def predictive_context_supported(row: Mapping[str, Any]) -> bool:
    """Unknown locations must not pool unrelated physical weather events."""
    return (row.get("city_id") not in {None, "", "unknown"}
            and row.get("market_kind") in {"high", "low"}
            and row.get("contract_shape") in {"tail", "range", "bucket"})


def is_eligible_for_predictive_history(row: Mapping[str, Any], future_decision_time: str) -> bool:
    """Admit only independently resolved predictors actually known before t."""
    proof = row.get("predictive_source_proof") or {}
    provenance = row.get("resolution_provenance") or {}
    if (row.get("eligible_for_predictive_history") is not True
            or row.get("resolver_knowledge_verified") is not True
            or proof.get("status") != "eligible_predictive_source_proof" or proof.get("reasons") != []
            or _decision_identity(row) is None
            or not _SHA256_RE.fullmatch(str(provenance.get("strict_resolution_source_sha256") or ""))
            or not _SHA256_RE.fullmatch(str(provenance.get("raw_resolution_row_sha256") or ""))
            or row.get("official_outcome") not in {"YES", "NO"}
            or not predictive_context_supported(row)):
        return False
    implied = _source_implied_outcome(row)
    if implied is None or row.get("source_implied_outcome") != implied or row.get("direction_correct") is not (implied == row["official_outcome"]):
        return False
    source, observed, settlement, known = (_parse_time(row.get(key)) for key in
        ("source_as_of", "input_observed_at", "settlement_ts", "known_after"))
    future = _parse_time(future_decision_time)
    fetched = _parse_time(row.get("source_fetched_at"))
    return (source is not None and observed is not None and settlement is not None and known is not None and future is not None
            and source <= observed < settlement <= known < future
            and (row.get("source_fetched_at") is None or (fetched is not None and fetched <= observed)))


def _pending_rows_for_input(row: Mapping[str, Any], identity: Mapping[str, Any]) -> list[dict[str, Any]]:
    snapshot = _weather_snapshot(row)
    if not snapshot:
        return []
    market = row.get("market") if isinstance(row.get("market"), Mapping) else {}
    metadata = market.get("market_metadata") if isinstance(market.get("market_metadata"), Mapping) else {}
    context_row = {
        "market_id": identity["market_id"],
        "question": market.get("question"),
        "observed_at": row.get("observed_at"),
        "market": dict(metadata),
        "weather_source_snapshot": snapshot,
    }
    source_records = _source_records(snapshot)
    rows: list[dict[str, Any]] = []
    for source_record in source_records:
        # Normalize each recorded source payload through the shared extractor,
        # one at a time. This preserves distinct target records that a
        # scoreboard aggregate would otherwise collapse for the same source.
        source_snapshot = _snapshot_with_only_source(snapshot, source_record)
        observations = extract_source_forecast_observations({**context_row, "weather_source_snapshot": source_snapshot})
        for observation in observations:
            target_proof = source_correctness_target_proof(
                source_record=source_record, snapshot=snapshot, market_date=observation.market.market_date,
            )
            strict_proof = strict_source_observation_proof(
                source_record=source_record,
                snapshot=snapshot,
                market_metadata=metadata,
                market_date=observation.market.market_date,
                market_kind=observation.market.market_kind,
                contract_shape=observation.market.contract_shape,
                question_side=observation.market.question_side,
                observed_at=identity["decision_key"]["observed_at_utc"],
            )
            predictive_proof = predictive_source_observation_proof(
                source_record=source_record, target_proof=target_proof, strict_proof=strict_proof,
                observed_at=identity["decision_key"]["observed_at_utc"],
                fetched_at=_first_text(_mapping_text(source_record, "source_fetched_at", "fetched_at"),
                                      _mapping_text(snapshot, "source_fetched_at", "fetched_at")),
            )
            eligibility = _ELIGIBLE_STRICT_SOURCE_PROOF if (
                target_proof["status"] == _ELIGIBLE_TARGET_PROOF and strict_proof["status"] == "eligible"
            ) else target_proof["status"]
            source_as_of = _first_text(
                _mapping_text(source_record, "source_as_of", "as_of", "observed_at"),
                _mapping_text(snapshot, "source_as_of", "as_of", "source_timestamp", "fetched_at"),
                _nested_text(row, "source_inputs", "recorded_as_of"),
            )
            source_fetched_at = _first_text(
                _mapping_text(source_record, "source_fetched_at", "fetched_at"),
                _mapping_text(snapshot, "source_fetched_at", "fetched_at"),
            )
            target_identity = {
                "market_date": observation.market.market_date,
                "source_target": _first_text(
                    _nested_text(source_record, "target_mapping", "source_target_date"),
                    _mapping_text(source_record, *_TARGET_ALIASES),
                ),
                "forecast_start": _mapping_text(source_record, "forecast_start", "forecast_period_start", "period_start"),
                "forecast_end": _mapping_text(source_record, "forecast_end", "forecast_period_end", "period_end"),
                "forecast_temp_f": observation.forecast_temp_f,
            }
            source_payload = {
                "source_id": observation.source_id,
                "source_name": observation.source_name,
                "source_as_of": source_as_of,
                "source_fetched_at": source_fetched_at,
                "target_identity": target_identity,
            }
            source_payload_sha256 = hashlib.sha256(_canonical_bytes(source_payload)).hexdigest()
            source_record_sha256 = hashlib.sha256(_canonical_bytes(source_record)).hexdigest()
            source_observation_id = "sha256:" + hashlib.sha256(_canonical_bytes({
                "canonical_input_sha256": identity["canonical_input_sha256"], "decision_key": identity["decision_key"],
                "source_id": observation.source_id, "target_identity": target_identity,
                "canonical_source_payload_sha256": source_payload_sha256,
                "source_record_sha256": source_record_sha256,
            })).hexdigest()
            rows.append({
                "schema_name": "pending_source_observation", "schema_version": SCHEMA_VERSION,
                "source_observation_id": source_observation_id,
                "canonical_input_sha256": identity["canonical_input_sha256"], "decision_key": identity["decision_key"],
                "market_id": identity["market_id"], "input_observed_at": identity["decision_key"]["observed_at_utc"],
                "observed_at": identity["decision_key"]["observed_at_utc"], "market_date": observation.market.market_date,
                "source_id": observation.source_id, "source_name": observation.source_name,
                "source_as_of": source_as_of, "source_fetched_at": source_fetched_at,
                "canonical_source_payload_sha256": source_payload_sha256, "target_identity": target_identity,
                "source_correctness_eligibility": eligibility,
                "source_target_proof": target_proof,
                "strict_source_proof": strict_proof,
                "retained_clock_evidence": _retained_forecast_clocks(
                    source_record, snapshot, identity["decision_key"]["observed_at_utc"])[0],
                "predictive_source_proof": predictive_proof,
                "forecast_semantics": {
                    "evidence_type": source_record.get("evidence_type"),
                    "forecast_type": source_record.get("forecast_type"),
                    "period_name": _first_text(_mapping_text(source_record, "forecast_period_name", "period_name"), _nested_text(source_record, "source_metadata", "period_name")),
                    "is_daytime": source_record.get("is_daytime", (source_record.get("source_metadata") or {}).get("is_daytime")),
                    "measurement_kind": source_record.get("forecast_measurement_kind"),
                    "period_start": _first_text(_nested_text(source_record, "target_mapping", "source_period_start"), target_identity["forecast_start"]),
                    "period_end": _first_text(_nested_text(source_record, "target_mapping", "source_period_end"), target_identity["forecast_end"]),
                    "target_mapping": source_record.get("target_mapping"),
                    "measurement_equivalence": "not_asserted_predictor_only",
                },
                "source_provenance": {
                    "source_record_sha256": source_record_sha256,
                    "canonical_input_sha256": identity["canonical_input_sha256"],
                },
                "forecast_temp_f": observation.forecast_temp_f, "threshold": observation.market.threshold,
                **({"forecast_interval": extract_forecast_interval(market.get("question"), metadata)}
                   if observation.market.contract_shape in {"range", "bucket"} else {}),
                "question_side": observation.market.question_side, "city_id": observation.market.city_id or "unknown",
                "market_kind": observation.market.market_kind or "unknown", "contract_shape": observation.market.contract_shape or "unknown",
                "event_ticker": _first_text(_mapping_text(metadata, "event_ticker"), _nested_text(metadata, "market_route", "evidence", "event_ticker")),
                "source_missing_reasons": list(observation.missing_reasons) or None,
            })
    return rows


def source_correctness_target_proof(
    *, source_record: Mapping[str, Any], snapshot: Mapping[str, Any], market_date: Any,
) -> dict[str, Any]:
    """Classify recorded forecast target proof without rejecting the raw record.

    Legacy aliases can establish a narrow exact-date proof, but any retained
    conflicting target fails closed.  V1 evidence additionally requires its
    explicit forecast typing and source-local target mapping.
    """

    normalized_market_date = _normalized_date(market_date)
    version = source_record.get("source_evidence_version")
    recorded_mapping = source_record.get("target_mapping")
    mapping = recorded_mapping if isinstance(recorded_mapping, Mapping) else {}
    period_problem = _source_period_target_problem(source_record, mapping, normalized_market_date)
    if str(version).strip() == "1":
        evidence_type = str(source_record.get("evidence_type") or "").strip().lower()
        if evidence_type != "forecast" or source_record.get("scoreable_forecast") is not True:
            return {"status": "unusable_v1_forecast_not_scoreable", "market_target_date": normalized_market_date, "source_target_date": None}
        market_targets = _normalized_targets(mapping.get("market_target_date"), source_record.get("market_target_date"))
        source_targets = _normalized_targets(
            mapping.get("source_target_date"), source_record.get("source_target_date"),
            source_record.get("target_forecast_date"), source_record.get("forecast_target"), source_record.get("forecast_date"),
        )
        mapped_market_date = market_targets[0] if market_targets else None
        source_target_date = source_targets[0] if source_targets else None
        if not mapping or not normalized_market_date or not market_targets or not source_targets:
            status = "unusable_v1_target_unproven"
        elif any(value != normalized_market_date for value in (*market_targets, *source_targets)):
            status = "unusable_v1_target_mismatch"
        elif period_problem:
            status = f"unusable_{period_problem}"
        else:
            status = _ELIGIBLE_TARGET_PROOF
        return {
            "status": status, "evidence_version": 1, "market_target_date": mapped_market_date,
            "source_target_date": source_target_date,
        }

    # Legacy records may prove only what the source itself attested.  In
    # particular, market-owned dates (including a snapshot's date and a
    # target_mapping.market_target_date) establish the market context but are
    # never evidence that a provider forecast targeted that date.
    del snapshot
    aliases = [mapping.get("source_target_date"), *[source_record.get(key) for key in _TARGET_ALIASES]]
    normalized_aliases = [value for value in (_normalized_date(alias) for alias in aliases) if value]
    source_target_date = normalized_aliases[0] if normalized_aliases else None
    if normalized_market_date and any(value != normalized_market_date for value in normalized_aliases):
        status = "unusable_legacy_target_mismatch"
    elif normalized_market_date and normalized_aliases:
        status = f"unusable_{period_problem}" if period_problem else _ELIGIBLE_TARGET_PROOF
    else:
        status = "unusable_legacy_target_unproven"
    return {
        "status": status, "evidence_version": "legacy", "market_target_date": normalized_market_date,
        "source_target_date": source_target_date,
    }



def _source_timezone(value: str | None) -> ZoneInfo | timezone | None:
    if not value:
        return None
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        # NWS retains fixed UTC offsets rather than an IANA zone.
        match = re.fullmatch(r"UTC([+-])(\d{2}):(\d{2})", value)
        if not match:
            return None
        hours, minutes = int(match[2]), int(match[3])
        if hours > 23 or minutes > 59:
            return None
        offset = timedelta(hours=hours, minutes=minutes)
        return timezone(offset if match[1] == "+" else -offset)


def _source_period_target_problem(
    source: Mapping[str, Any], mapping: Mapping[str, Any], market_date: str | None,
) -> str | None:
    """Check optional retained periods in source-local time, including aliases.

    An overnight period may end the following day. Missing bounds do not impose
    a new legacy requirement, and naive provider timestamps are source-local.
    """
    if not market_date:
        return None
    target = date.fromisoformat(market_date)
    timezone_name = _text(mapping.get("source_timezone"))
    source_timezone = _source_timezone(timezone_name)
    period_dates: dict[str, list[date]] = {"start": [], "end": []}
    period_times: dict[str, list[datetime]] = {"start": [], "end": []}
    alias_conflict = False
    for boundary in ("start", "end"):
        values = [mapping.get(f"source_period_{boundary}"), *(
            source.get(f"{prefix}_{boundary}") for prefix in ("forecast", "forecast_period", "period")
        )]
        for value in values:
            if value is None:
                continue
            try:
                period = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return "source_period_invalid"
            if source_timezone is not None:
                period = (period.astimezone(source_timezone) if period.tzinfo is not None
                          else period.replace(tzinfo=source_timezone))
            period_dates[boundary].append(period.date())
            period_times[boundary].append(period)
        # Distinct bounds on the same local date are not interchangeable
        # evidence. Resolve naive source-local aliases against the recorded IANA
        # zone; without a resolvable zone, mixed forms cannot prove equality.
        aware = {period.astimezone(timezone.utc) for period in period_times[boundary] if period.tzinfo is not None}
        naive = {period for period in period_times[boundary] if period.tzinfo is None}
        if len(aware) > 1 or len(naive) > 1 or (aware and naive):
            alias_conflict = True
    is_low = (_text(source.get("forecast_measurement_kind")) or "").lower() == "low"
    # A daytime high belongs to its starting date; a night low belongs to
    # the morning on which its retained nighttime period ends. Do not require
    # optional bounds on older source records.
    low_ends_on_target = bool(period_dates["end"]) and all(
        period_day == target for period_day in period_dates["end"]
    )
    local_prior_night = source.get("forecast_type") == "local_prior_night_low_predictor"
    if local_prior_night:
        # The local daily-row minimum predicts the following morning. Its
        # retained start is the prior day's row timestamp, not a fabricated
        # nighttime boundary; the producer does not supply a period end.
        # This explicit target mapping proves a predictor, never a daily low.
        if not (
            is_low and str(source.get("source_evidence_version")).strip() == "1"
            and source.get("evidence_type") == "forecast"
            and source.get("scoreable_forecast") is True
            and mapping.get("mapping") == "source_local_dated_predictor"
            and _normalized_date(mapping.get("market_target_date")) == market_date
            and _normalized_date(mapping.get("source_target_date")) == market_date
            and source_timezone is not None and period_dates["start"]
            and all(day == target - timedelta(days=1) for day in period_dates["start"])
            and (not period_dates["end"] or low_ends_on_target)
        ):
            return "source_period_target_mismatch"
    full_day_low = (is_low and bool(period_times["start"] and period_times["end"])
                    and all(period.date() == target and period.hour == 0 and period.minute == 0
                            and period.second == 0 and period.microsecond == 0
                            for period in period_times["start"])
                    and all(period.date() == target + timedelta(days=1)
                            and period.hour == 0 and period.minute == 0
                            and period.second == 0 and period.microsecond == 0
                            for period in period_times["end"]))
    for boundary, dates in period_dates.items():
        for period_day in dates:
            day_offset = (period_day - target).days
            if day_offset == 0:
                continue
            if boundary == "start" and day_offset == -1 and is_low and (low_ends_on_target or local_prior_night):
                continue
            if boundary == "end" and day_offset == 1 and (not is_low or full_day_low):
                continue
            return "source_period_target_mismatch"
    return "source_period_alias_conflict" if alias_conflict else None


def _complete_open_meteo_hourly_day_end(
    source: Mapping[str, Any], mapping: Mapping[str, Any], market_date: str | None,
) -> datetime | None:
    """A final hourly temperature timestamp is a sample, not a period end."""
    if (source.get("source_id") != "open_meteo"
            or mapping.get("mapping") != "exact_source_local_hourly_date"
            or not market_date or not isinstance(source.get("forecast_times"), list)):
        return None
    try:
        zone = ZoneInfo(str(mapping.get("source_timezone")))
        start = datetime.combine(date.fromisoformat(market_date), datetime.min.time(), tzinfo=zone)
        end = datetime.combine(start.date() + timedelta(days=1), datetime.min.time(), tzinfo=zone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return None
    start_utc, end_utc = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    expected = [start_utc + timedelta(hours=hour) for hour in range(int((end_utc - start_utc).total_seconds() // 3600))]
    times = [_source_period_time(value, str(zone)) for value in source["forecast_times"]]
    if (times != expected or not expected
            or any(_source_period_time(value, str(zone)) != expected[0] for value in (
                mapping.get("source_period_start"), source.get("forecast_start")) if value is not None)
            or any(_source_period_time(value, str(zone)) != expected[-1] for value in (
                mapping.get("source_period_end"), source.get("forecast_end")) if value is not None)):
        return None
    return end_utc


def strict_source_observation_proof(
    *,
    source_record: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    market_metadata: Mapping[str, Any],
    market_date: Any,
    market_kind: Any,
    contract_shape: Any,
    question_side: Any,
    observed_at: Any,
) -> dict[str, Any]:
    """Validate the full recorded source-to-contract proof without inference.

    This is deliberately separate from target-date correctness: incomplete v1
    observations may remain useful audit/reliability evidence, but they cannot
    become strict chronological Source Router history.
    """
    reasons: list[str] = []
    source_id = _text(source_record.get("source_id"))
    source_as_of = _text(source_record.get("source_as_of"))
    recorded_mapping = source_record.get("target_mapping")
    mapping = recorded_mapping if isinstance(recorded_mapping, Mapping) else {}
    if not source_id:
        reasons.append("missing_source_id")
    source_as_of_time, observed_at_time = _parse_time(source_as_of), _parse_time(observed_at)
    if not source_as_of or source_as_of_time is None:
        reasons.append("missing_or_unoffset_source_as_of")
    elif observed_at_time is None or source_as_of_time > observed_at_time:
        reasons.append("source_as_of_after_immutable_observed_at")
    _, clock_reasons = _retained_forecast_clocks(source_record, snapshot, observed_at)
    reasons.extend(reason for reason in clock_reasons if reason not in reasons)
    source_timezone_name = _text(mapping.get("source_timezone"))
    hourly_day_end = _complete_open_meteo_hourly_day_end(source_record, mapping, _normalized_date(market_date))
    if source_as_of_time is not None:
        period_ends = (
            mapping.get("source_period_end"),
            *(source_record.get(f"{prefix}_end") for prefix in ("forecast", "forecast_period", "period")),
        )
        end_times = [
            _source_period_time(end, source_timezone_name)
            for end in period_ends if end is not None
        ]
        if any(end_time is None for end_time in end_times):
            reasons.append("source_period_end_time_unproven")
        if any(source_as_of_time >= (hourly_day_end if hourly_day_end and end_time == hourly_day_end - timedelta(hours=1) else end_time)
               for end_time in end_times if end_time is not None):
            reasons.append("source_as_of_after_source_period_end")
    period_start = _source_period_time(_first_text(
        mapping.get("source_period_start"), source_record.get("forecast_start"),
        source_record.get("forecast_period_start"), source_record.get("period_start"),
    ), source_timezone_name)
    period_end = _source_period_time(_first_text(
        mapping.get("source_period_end"), source_record.get("forecast_end"),
        source_record.get("forecast_period_end"), source_record.get("period_end"),
    ), source_timezone_name)
    target_day = _normalized_date(market_date)
    kind = (_text(source_record.get("forecast_measurement_kind")) or "").lower()
    if target_day and kind in {"high", "low"}:
        # Bounded daytime/nighttime periods are not daily extrema, regardless
        # of provider (including city-specific forecasts).
        # A retained bounded period proves a daily target only when it covers
        # the entire source-local calendar day; older unbounded rows retain
        # their legacy treatment rather than inventing a missing bound.
        effective_period_end = hourly_day_end.astimezone(period_start.tzinfo) if hourly_day_end and period_start else period_end
        partial_day = bool((period_start or effective_period_end) and (
            not period_start or not effective_period_end
            or period_start.date().isoformat() != target_day
            or period_start.time().isoformat() != "00:00:00"
            or effective_period_end.date() != period_start.date() + timedelta(days=1)
            or effective_period_end.time().isoformat() != "00:00:00"
        ))
        metadata = source_record.get("source_metadata")
        # Calendar-label midnight bounds are not provider measurement-interval
        # proof. Preserve the producer's explicit non-equivalence at either
        # recorded alias, even when those bounds resemble an entire day.
        explicitly_non_full_day = (
            source_record.get("full_day_measurement_equivalent") is False
            or (isinstance(metadata, Mapping) and metadata.get("full_day_measurement_equivalent") is False)
        )
        if (partial_day or explicitly_non_full_day
                or (kind == "low" and _text(mapping.get("mapping")) == "source_local_nws_night_low_proxy")):
            reasons.append(f"partial_day_{kind}_proxy_not_full_day")
    if not source_timezone_name:
        reasons.append("missing_source_timezone")
    elif _source_timezone(source_timezone_name) is None:
        reasons.append("unresolvable_source_timezone")
    period_problem = _source_period_target_problem(source_record, mapping, _normalized_date(market_date))
    if period_problem:
        reasons.append(period_problem)

    source_city = _normalized_city(source_record.get("source_location_city"))
    recorded_city = _normalized_city(
        _first_text(_mapping_text(market_metadata, "city"), _nested_text(snapshot, "station_resolution", "city"))
    )
    if not source_city:
        reasons.append("missing_source_location_city")
    elif not recorded_city or source_city != recorded_city:
        reasons.append("source_city_conflicts_with_recorded_market_city")

    expected = {
        "forecast_measurement_kind": _text(market_kind),
        "contract_shape": _text(contract_shape),
        "question_side": _text(question_side),
    }
    for field, expected_value in expected.items():
        actual_value = _text(source_record.get(field))
        if not actual_value:
            reasons.append(f"missing_{field}")
        elif not expected_value or actual_value.lower() != expected_value.lower():
            reasons.append(f"{field}_conflicts_with_recorded_contract")
    if _normalized_date(market_date) is None:
        reasons.append("missing_recorded_market_date")
    return {
        "status": "eligible" if not reasons else "unusable_strict_source_proof",
        "reasons": reasons,
        "source_id": source_id,
        "source_as_of": source_as_of,
        "source_timezone": _text(mapping.get("source_timezone")),
        "source_location_city": source_city,
        "recorded_market_city": recorded_city,
        "market_kind": expected["forecast_measurement_kind"],
        "contract_shape": expected["contract_shape"],
        "question_side": expected["question_side"],
    }


def _retained_forecast_clocks(source: Mapping[str, Any], snapshot: Mapping[str, Any],
                              observed_at: Any) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Validate retained aliases at their source or aggregate clock boundary.

    Provider issue clocks cannot follow their own or enclosing capture. The
    completed snapshot's as-of clock can legitimately follow provider captures;
    compare it only with the snapshot capture, never with an earlier provider.
    """
    groups = {
        "source_issue": (source, ("source_as_of", "as_of", "observed_at")),
        "snapshot_issue": (snapshot, ("source_as_of", "as_of", "source_timestamp")),
        "source_capture": (source, ("source_fetched_at", "fetched_at")),
        "snapshot_capture": (snapshot, ("source_fetched_at", "fetched_at")),
    }
    evidence = {name: {key: record[key] for key in keys if key in record and record[key] is not None}
                for name, (record, keys) in groups.items()}
    reasons: list[str] = []
    observed = _parse_time(observed_at)
    clock_times: dict[str, list[datetime]] = {}
    for name, values in evidence.items():
        parsed = [_parse_time(value) for value in values.values()]
        if any(value is None for value in parsed):
            reasons.append("invalid_retained_source_clock")
        valid = [value for value in parsed if value is not None]
        if len(set(valid)) > 1:
            reasons.append("conflicting_retained_source_clock_aliases")
        if any(observed is None or value > observed for value in valid):
            reasons.append("retained_source_clock_after_immutable_observed_at")
        clock_times[name] = valid
    for issue_group, capture_groups in (
        ("source_issue", ("source_capture", "snapshot_capture")),
        ("snapshot_issue", ("snapshot_capture",)),
    ):
        issues = clock_times[issue_group]
        captures = [value for group in capture_groups for value in clock_times[group]]
        if issues and captures and max(issues) > min(captures):
            reasons.append("source_issue_after_declared_capture")
    return evidence, list(dict.fromkeys(reasons))

def predictive_source_observation_proof(
    *, source_record: Mapping[str, Any], target_proof: Mapping[str, Any], strict_proof: Mapping[str, Any],
    observed_at: str, fetched_at: str | None = None,
) -> dict[str, Any]:
    """Prove a labelled predictor, without claiming daily-extremum equivalence.

    Date, location, kind, contract and capture-clock checks remain identical.
    The only strict-proof exclusions relaxed here are partial day/night proxy
    semantics. These rows never gain the strict full-day marker.
    """
    reasons = [reason for reason in strict_proof.get("reasons", ())
               if reason not in {"partial_day_high_proxy_not_full_day", "partial_day_low_proxy_not_full_day"}]
    if target_proof.get("status") != _ELIGIBLE_TARGET_PROOF:
        reasons.append(str(target_proof.get("status") or "source_target_unproven"))
    if source_record.get("evidence_type") != "forecast" or source_record.get("scoreable_forecast") is not True:
        reasons.append("source_not_explicit_scoreable_forecast")
    if fetched_at is not None:
        fetched, observed = _parse_time(fetched_at), _parse_time(observed_at)
        if fetched is None:
            reasons.append("missing_or_unoffset_source_fetched_at")
        elif observed is None or fetched > observed:
            reasons.append("source_fetched_at_after_immutable_observed_at")
    return {**strict_proof, "status": "eligible_predictive_source_proof" if not reasons else "unusable_predictive_source_proof",
            "reasons": reasons, "measurement_equivalence": "not_asserted_predictor_only"}


def _attach_resolver_knowledge(outcomes: Iterable[dict[str, Any]], path: Path) -> None:
    """Recover clocks only from the exact independently bound resolver receipt.

    The legacy outcome binder projects away knowledge clocks. Re-read its
    hash-pinned original, never use the exchange settlement time as knowledge.
    """
    from bot.replay_outcome_binding import _normalize_strict_resolution

    raw_bytes = path.read_bytes()
    digest = hashlib.sha256(raw_bytes).hexdigest()
    lines = raw_bytes.splitlines()
    for outcome in outcomes:
        provenance = outcome.get("provenance") or {}
        line = provenance.get("strict_resolution_source_line_number")
        if (provenance.get("strict_resolution_source_sha256") != digest
                or type(line) is not int or line < 1 or line > len(lines)
                or hashlib.sha256(lines[line - 1]).hexdigest() != provenance.get("raw_resolution_row_sha256")):
            raise SourceObservationLedgerError("independent resolver receipt mismatch")
        raw = json.loads(lines[line - 1])
        normalized = _normalize_strict_resolution(raw, outcome["market_id"])
        if normalized is None or any(normalized[key] != outcome[key] for key in ("official_outcome", "settlement_ts", "resolution_id")):
            raise SourceObservationLedgerError("independent resolver identity/outcome mismatch")
        resolution = raw.get("resolution") if isinstance(raw.get("resolution"), Mapping) else {}
        clocks = [value for value in (raw.get("known_after"), raw.get("resolved_at"), raw.get("retrieved_at"),
                                      resolution.get("resolved_at"), resolution.get("retrieved_at")) if value is not None]
        parsed = [_parse_time(value) for value in clocks]
        valid_times = [value for value in parsed if value is not None]
        known = max(valid_times) if valid_times and len(valid_times) == len(parsed) else None
        settlement = _parse_time(outcome["settlement_ts"])
        verified = known is not None and settlement is not None and known >= settlement
        outcome.update(known_after=known.isoformat() if verified and known is not None else None,
                       resolver_knowledge_verified=verified,
                       predictive_history_reason=None if verified else "missing_or_invalid_resolver_knowledge",
                       resolution_resolved_at=resolution.get("resolved_at") or raw.get("resolved_at"),
                       resolution_retrieved_at=resolution.get("retrieved_at") or raw.get("retrieved_at"))


def _normalized_city(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    return re.sub(r"[^a-z0-9]+", "", text.lower()) or None


def _normalized_date(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10]).isoformat()
    except ValueError:
        return None


def _normalized_targets(*values: Any) -> list[str]:
    texts = [str(value).strip() for value in values if value is not None and str(value).strip()]
    normalized = [_normalized_date(value) for value in texts]
    return [] if len(normalized) != len(texts) or any(value is None for value in normalized) else normalized


def _outcome_index(path: Path, counters: Counter[str]) -> tuple[dict[tuple[str, ...], dict[str, Any]], set[tuple[str, ...]]]:
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for _, row in _read_jsonl(path):
        counters["outcome_records_seen"] += 1
        normalized = _normalized_outcome(row)
        if normalized is None:
            counters["invalid_outcomes"] += 1
            continue
        grouped[_identity_key(normalized["canonical_input_sha256"], normalized["decision_key"])].append(normalized)
    accepted: dict[tuple[str, ...], dict[str, Any]] = {}
    conflicts: set[tuple[str, ...]] = set()
    for key, rows in grouped.items():
        unique = {_canonical_bytes(row) for row in rows}
        if len(unique) == 1:
            accepted[key] = rows[0]
            counters["duplicate_outcomes"] += len(rows) - 1
        else:
            conflicts.add(key)
            counters["conflicting_outcomes"] += len(rows)
    return accepted, conflicts


def _normalized_outcome(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(row, Mapping):
        return None
    identity = _decision_identity(row)
    status = str(row.get("market_status") or "finalized").lower()
    if identity is None or status not in {"finalized", "void_resolution"}:
        return None
    official = str(row.get("official_outcome") or "").upper()
    settlement = _text(row.get("settlement_ts"))
    resolution_id = _text(row.get("resolution_id"))
    if official not in {"YES", "NO", "VOID"} or not settlement or _parse_time(settlement) is None or not resolution_id:
        return None
    if (status == "void_resolution") != (official == "VOID"):
        return None
    return {
        **identity, "official_outcome": official, "settlement_ts": settlement, "resolution_id": resolution_id,
        "resolution_resolved_at": _first_text(_text(row.get("resolved_at")), _nested_text(row, "resolution", "resolved_at")),
        "resolution_retrieved_at": _first_text(_text(row.get("retrieved_at")), _nested_text(row, "provenance", "retrieved_at")),
        "provenance": row.get("provenance") if isinstance(row.get("provenance"), Mapping) else None,
    }


def _unusable_row(pending: Mapping[str, Any], reason: str, *, outcome: Mapping[str, Any] | None = None) -> dict[str, Any]:
    row = {**pending, "schema_name": "unsettled_or_unusable_source_observation", "schema_version": SCHEMA_VERSION, "disposition_reason": reason}
    if outcome is not None:
        row.update({
            "authoritative_resolution_present": True, "settlement_ts": outcome["settlement_ts"],
            "resolution_id": outcome["resolution_id"], "resolution_resolved_at": outcome.get("resolution_resolved_at"),
        })
    return row


def _void_row(pending: Mapping[str, Any], outcome: Mapping[str, Any]) -> dict[str, Any]:
    """Retain a settled VOID receipt without scoring or source-history eligibility."""
    return {
        **pending,
        "schema_name": "void_source_observation",
        "schema_version": SCHEMA_VERSION,
        "disposition_reason": "void_resolution",
        "official_outcome": "VOID",
        "eligible_for_reliability": False,
        "eligible_for_source_history": False,
        "settlement_ts": outcome["settlement_ts"],
        "resolution_id": outcome["resolution_id"],
        "resolution_resolved_at": outcome.get("resolution_resolved_at"),
        "resolution_retrieved_at": outcome.get("resolution_retrieved_at"),
        "resolution_provenance": outcome.get("provenance"),
    }


def _source_implied_outcome(row: Mapping[str, Any]) -> str | None:
    if row.get("contract_shape") in {"range", "bucket"} or row.get("question_side") == "range":
        return infer_range_forecast_outcome(_number(row.get("forecast_temp_f")), row.get("forecast_interval"))
    direction = infer_direction_from_value(_number(row.get("forecast_temp_f")), _number(row.get("threshold")))
    return infer_predicted_outcome(str(row.get("question_side") or ""), direction)


def _decision_identity(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(row, Mapping):
        return None
    digest, key, market_id = row.get("canonical_input_sha256"), row.get("decision_key"), row.get("market_id")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest) or not isinstance(key, Mapping):
        return None
    normalized_key = {field: _text(key.get(field)) for field in _IDENTITY_FIELDS}
    if any(value is None for value in normalized_key.values()) or normalized_key["market_id"] != _text(market_id):
        return None
    return {"canonical_input_sha256": digest.lower(), "decision_key": normalized_key, "market_id": normalized_key["market_id"]}


def _identity_key(digest: str, key: Mapping[str, Any]) -> tuple[str, ...]:
    return (digest, *(str(key[field]) for field in _IDENTITY_FIELDS))


def _weather_snapshot(row: Mapping[str, Any]) -> Mapping[str, Any]:
    inputs = row.get("source_inputs") if isinstance(row.get("source_inputs"), Mapping) else {}
    context = inputs.get("source_context") if isinstance(inputs.get("source_context"), Mapping) else {}
    data = context.get("data") if isinstance(context.get("data"), Mapping) else {}
    snapshot = data.get("weather_source_snapshot")
    return snapshot if isinstance(snapshot, Mapping) else {}


def _source_records(snapshot: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    signal = snapshot.get("source_signal") if isinstance(snapshot.get("source_signal"), Mapping) else {}
    signal_data = signal.get("data") if isinstance(signal.get("data"), Mapping) else {}
    records: list[Mapping[str, Any]] = []
    for value in (snapshot.get("sources"), signal_data.get("source_details"), signal_data.get("sources")):
        if isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    records.append(item)
    return records


def _snapshot_with_only_source(snapshot: Mapping[str, Any], source_record: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(snapshot)
    result["sources"] = [dict(source_record)]
    signal = result.get("source_signal")
    if isinstance(signal, Mapping):
        signal = dict(signal)
        data = signal.get("data")
        if isinstance(data, Mapping):
            data = dict(data)
            data.pop("sources", None)
            data.pop("source_details", None)
            signal["data"] = data
        result["source_signal"] = signal
    return result


def _prepare_output_dir(path: Path) -> None:
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            raise SourceObservationLedgerError("output directory must be new or empty; refusing to overwrite artifacts")
    else:
        path.mkdir(parents=True, exist_ok=False)


def _read_jsonl(path: Path) -> Iterable[tuple[int, Mapping[str, Any] | None]]:
    with path.open(encoding="utf-8") as stream:
        for line_number, raw in enumerate(stream, start=1):
            if not raw.strip():
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = None
            yield line_number, payload if isinstance(payload, Mapping) else None


def _jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(_canonical_bytes(row) + b"\n" for row in rows)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _source_period_time(value: Any, source_timezone: str | None) -> datetime | None:
    """Resolve provider-local period bounds before comparing them to UTC instants."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    zone = _source_timezone(source_timezone)
    if parsed.tzinfo is not None:
        return parsed.astimezone(zone) if zone is not None else parsed
    return parsed.replace(tzinfo=zone) if zone is not None else None


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and parsed not in (float("inf"), float("-inf")) else None


def _text(value: Any) -> str | None:
    return str(value).strip() if value is not None and str(value).strip() else None


def _mapping_text(mapping: Mapping[str, Any], *keys: str) -> str | None:
    return _first_text(*(_text(mapping.get(key)) for key in keys))


def _nested_text(mapping: Mapping[str, Any], *keys: str) -> str | None:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return _text(current)


def _first_text(*values: str | None) -> str | None:
    return next((value for value in values if value), None)
