"""Build blind, versioned replay inputs from collector snapshot rows only."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any, Mapping


from bot.shared_market_feed import shared_candidate_identity_mismatch

REPLAY_DECISION_INPUT_SCHEMA_NAME = "replay_decision_input"
REPLAY_DECISION_INPUT_SCHEMA_VERSION = 1

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_INPUT_TOKENS = frozenset(
    {
        "outcome", "outcomes", "settlement", "settled", "resolution", "resolved",
        "pnl", "payout", "profit", "winner", "won", "void", "future", "result",
    }
)
_SAFE_PROVENANCE_FIELD_NAMES = frozenset({"settlement_source", "station_resolution"})
_FORBIDDEN_RECORDED_DECISION_FIELDS = frozenset(
    {
        "main_decision", "normal_decision", "shadow_decision", "shared_core_decision",
        "final_action", "action", "decision_type", "model_probability", "probability",
        "position_size", "requested_position_size", "requested_size", "size", "stake",
        "notional", "kelly_fraction",
    }
)
_REQUIRED_DECISION_CONTEXT_HASHES = (
    "policy_config_sha256", "strategy_logic_sha256", "kelly_config_sha256",
    "risk_config_sha256", "execution_price_policy_sha256",
)
_SANITIZED_TOP_LEVEL_CONTRACT_FIELDS = frozenset(
    {
        "observed_at", "market_id", "question", "yes_price", "no_price",
        "decision_artifact", "shared_snapshot_id", "shared_candidate_id",
        "collector_provenance", "replay_derived_features", "replay_decision_context",
    }
)

# This is deliberately a narrow description of the current collector weather
# snapshot, rather than a generic JSON blob.  Each nested mapping/list is rebuilt
# from this v1 schema, so unreviewed fields cannot cross the replay boundary.
_SCALAR = object()
_OPTIONAL_WEATHER_PROVENANCE = object()
_JSON_SCALAR_TYPES = (str, int, float, bool, type(None))
_DATE_VALIDATION_V1 = {
    "ok": _SCALAR, "reason": _SCALAR, "market_date": _SCALAR, "weather_date": _SCALAR,
    "source": _SCALAR,
}
_STATION_RESOLUTION_V1 = {
    "station_id": _SCALAR, "station_cli": _SCALAR, "mapping": _SCALAR,
    "market_city": _SCALAR, "weather_city": _SCALAR, "confidence": _SCALAR,
    "city_code": _SCALAR, "city_id": _SCALAR, "city": _SCALAR, "state": _SCALAR,
    "source": _SCALAR, "reason": _SCALAR, "matched_from": _SCALAR,
}
_FORECAST_V1 = {
    "high": _SCALAR, "low": _SCALAR, "current": _SCALAR, "forecast_high": _SCALAR,
    "forecast_low": _SCALAR, "current_temp": _SCALAR, "actual_temp_used": _SCALAR,
    "predicted_temp": _SCALAR, "threshold": _SCALAR, "question_side": _SCALAR,
}
_PERIOD_REF_V1 = {
    "number": _SCALAR, "name": _SCALAR, "startTime": _SCALAR, "endTime": _SCALAR,
    "isDaytime": _SCALAR, "temperature": _SCALAR, "temperatureUnit": _SCALAR,
}
_FORECAST_TARGET_MAPPING_V1 = {
    "market_target_date": _SCALAR, "source_target_date": _SCALAR, "mapping": _SCALAR,
    "source_timezone": _SCALAR, "source_period_start": _SCALAR, "source_period_end": _SCALAR,
}
_SOURCE_METADATA_V1 = {
    "timezone": _SCALAR, "utc_offset_seconds": _SCALAR, "current_time": _SCALAR,
    "forecast_times_used": [_SCALAR], "office": _SCALAR, "grid_x": _SCALAR,
    "grid_y": _SCALAR, "station_id": _SCALAR, "period_name": _SCALAR,
    "period_start": _SCALAR, "period_end": _SCALAR, "period_number": _SCALAR,
    "is_daytime": _SCALAR, "periods_used": [_PERIOD_REF_V1], "high_period": _PERIOD_REF_V1,
    "low_period": _PERIOD_REF_V1, "source": _SCALAR, "city": _SCALAR,
    "observation_time": _SCALAR,
}
_WEATHER_SOURCE_V1 = {
    "source_id": _SCALAR, "source_name": _SCALAR, "source_family": _SCALAR,
    "source_location_basis": _SCALAR, "source_location_city": _SCALAR,
    "forecast_measurement_kind": _SCALAR, "contract_shape": _SCALAR, "question_side": _SCALAR,
    "role": _SCALAR, "forecast_target": _SCALAR,
    "source_evidence_version": _SCALAR, "evidence_type": _SCALAR,
    "forecast_availability": _SCALAR, "scoreable_forecast": _SCALAR,
    "availability_reason": _SCALAR, "market_target_date": _SCALAR,
    "source_target_date": _SCALAR, "target_mapping": _FORECAST_TARGET_MAPPING_V1,
    "weight": _SCALAR, "contribution": _SCALAR, "weight_note": _SCALAR,
    "forecast_high": _SCALAR, "forecast_low": _SCALAR, "current_forecast": _SCALAR,
    "current_temp": _SCALAR, "confidence": _SCALAR, "market_date": _SCALAR,
    "weather_date": _SCALAR, "forecast_date": _SCALAR, "target_forecast_date": _SCALAR,
    "target_date": _SCALAR, "fetched_at": _SCALAR, "as_of": _SCALAR,
    "observed_at": _SCALAR, "source_fetched_at": _SCALAR, "source_as_of": _SCALAR,
    "forecast_start": _SCALAR, "forecast_end": _SCALAR, "forecast_times": [_SCALAR],
    "forecast_period_name": _SCALAR, "forecast_period_start": _SCALAR,
    "forecast_period_end": _SCALAR, "period_name": _SCALAR, "period_start": _SCALAR,
    "period_end": _SCALAR, "period_number": _SCALAR, "is_daytime": _SCALAR,
    "periods_used": [_PERIOD_REF_V1], "high_period": _PERIOD_REF_V1,
    "low_period": _PERIOD_REF_V1, "station_id": _SCALAR, "station_cli": _SCALAR,
    "station_mapping": _SCALAR, "settlement_source": _SCALAR,
    "source_agreement_score": _SCALAR, "date_validation": _DATE_VALIDATION_V1,
    "source_metadata": _SOURCE_METADATA_V1,
}
_WEATHER_SIGNAL_DATA_V1 = {
    "forecast_high": _SCALAR, "forecast_low": _SCALAR, "current_temp": _SCALAR,
    "actual_temp_used": _SCALAR, "predicted_temp": _SCALAR, "threshold": _SCALAR,
    "city": _SCALAR, "sources": [_SCALAR], "agreement": _SCALAR,
    "settlement_source": _SCALAR, "nws_high": _SCALAR, "nws_low": _SCALAR,
    "nws_open_meteo_gap": _SCALAR, "weather_date": _SCALAR, "forecast_date": _SCALAR,
    "target_forecast_date": _SCALAR, "fetched_at": _SCALAR, "as_of": _SCALAR,
    "market_target_date": _SCALAR,
    "station_id": _SCALAR, "station_cli": _SCALAR, "station_mapping": _SCALAR,
    "station_resolution": _STATION_RESOLUTION_V1, "date_validation": _DATE_VALIDATION_V1,
    "source_details": [_WEATHER_SOURCE_V1],
}
_WEATHER_SOURCE_SIGNAL_V1 = {
    "signal_type": _SCALAR, "predicted_prob": _SCALAR, "confidence": _SCALAR,
    "source_timestamp": _SCALAR, "ttl_seconds": _SCALAR, "question_side": _SCALAR,
    "data": _WEATHER_SIGNAL_DATA_V1,
}
_WEATHER_PROVENANCE_V1 = {
    "source_mode": _SCALAR, "source_provenance": _SCALAR, "anti_hindsight": _SCALAR,
}
_WEATHER_SNAPSHOT_V1 = {
    "artifact_version": _SCALAR, "mode": _SCALAR, "source_provenance": _SCALAR,
    "provenance": _OPTIONAL_WEATHER_PROVENANCE,
    "source_name": _SCALAR, "signal_name": _SCALAR, "signal_role": _SCALAR,
    "signal_type": _SCALAR, "method": _SCALAR, "market_id": _SCALAR, "question": _SCALAR,
    "market_date": _SCALAR, "market_date_source": _SCALAR, "weather_date": _SCALAR,
    "forecast_date": _SCALAR, "target_forecast_date": _SCALAR, "target_date": _SCALAR,
    "date_validation": _DATE_VALIDATION_V1, "fetched_at": _SCALAR, "as_of": _SCALAR,
    "source_timestamp": _SCALAR, "ttl_seconds": _SCALAR, "source_fetched_at": _SCALAR,
    "source_as_of": _SCALAR, "predicted_prob": _SCALAR, "confidence": _SCALAR,
    "weather_confidence_score": _SCALAR, "source_agreement_score": _SCALAR,
    "settlement_source": _SCALAR, "station_id": _SCALAR, "station_cli": _SCALAR,
    "station_mapping": _SCALAR, "station_resolution": _STATION_RESOLUTION_V1,
    "forecast": _FORECAST_V1, "sources": [_WEATHER_SOURCE_V1],
    "gaps": {"nws_open_meteo_gap": _SCALAR}, "source_signal": _WEATHER_SOURCE_SIGNAL_V1,
}
_MARKET_ROUTE_V1 = {
    "family": _SCALAR, "group": _SCALAR, "series": _SCALAR, "series_ticker": _SCALAR,
    "event_ticker": _SCALAR, "category": _SCALAR, "market_kind": _SCALAR,
    "contract_shape": _SCALAR, "city_id": _SCALAR, "allowed": _SCALAR,
    "subcategory": _SCALAR, "handler_id": _SCALAR, "reason_code": _SCALAR,
    "evidence": {
        "series_ticker": _SCALAR, "event_ticker": _SCALAR, "market_ticker": _SCALAR,
        "market_id": _SCALAR, "question": _SCALAR, "category": _SCALAR,
        "classification_reason": _SCALAR, "market_group": _SCALAR,
        "market_family": _SCALAR, "prefix_match": _SCALAR, "prefix_value": _SCALAR,
        "temperature_semantics_match": _SCALAR, "shape": _SCALAR,
    },
}
_MARKET_METADATA_V1 = {
    "market_group": _SCALAR, "market_family": _SCALAR, "series": _SCALAR,
    "series_ticker": _SCALAR, "event_ticker": _SCALAR, "category": _SCALAR,
    "market_id": _SCALAR, "ticker": _SCALAR, "market_date": _SCALAR, "city_id": _SCALAR,
    "city": _SCALAR, "market_kind": _SCALAR, "contract_shape": _SCALAR,
    "market_route": _MARKET_ROUTE_V1,
}
_SOURCE_CONTEXT_DATA_V1 = {
    "market_metadata": _MARKET_METADATA_V1,
    "market_route": _MARKET_ROUTE_V1,
    "weather_source_snapshot": _WEATHER_SNAPSHOT_V1,
}
_SOURCE_SNAPSHOT_V1 = {
    "mode": _SCALAR, "source": _SCALAR, "source_provenance": _SCALAR,
    "provenance": _OPTIONAL_WEATHER_PROVENANCE, "method": _SCALAR, "signal_name": _SCALAR,
    "signal_role": _SCALAR, "as_of": _SCALAR, "fetched_at": _SCALAR,
    "snapshot_ref": _SCALAR, "market_date": _SCALAR, "market_date_source": _SCALAR,
    "weather_date": _SCALAR, "target_forecast_date": _SCALAR, "forecast_date": _SCALAR,
    "date_validation": _DATE_VALIDATION_V1,
}
_DERIVED_FEATURE_VALUES_V1 = {
    "forecast_high_f": _SCALAR, "forecast_low_f": _SCALAR, "current_temp_f": _SCALAR,
    "threshold_f": _SCALAR, "forecast_high": _SCALAR, "forecast_low": _SCALAR,
    "current_temp": _SCALAR, "threshold": _SCALAR, "question_side": _SCALAR,
    "source_agreement_score": _SCALAR, "weather_confidence_score": _SCALAR,
    "station_id": _SCALAR, "market_date": _SCALAR, "weather_date": _SCALAR,
}
_EXECUTION_SNAPSHOT_V1 = {
    "source": _SCALAR, "yes_price": _SCALAR, "no_price": _SCALAR,
    "best_yes_ask": _SCALAR, "best_no_ask": _SCALAR, "best_yes_bid": _SCALAR,
    "best_no_bid": _SCALAR, "estimated_fill_price": _SCALAR, "as_of": _SCALAR,
}


@dataclass(frozen=True, slots=True)
class ReplayDecisionInputError:
    code: str
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


@dataclass(frozen=True, slots=True)
class ReplayDecisionInputBuildResult:
    record: dict[str, Any] | None
    canonical_input_json: bytes | None = None
    errors: tuple[ReplayDecisionInputError, ...] = ()

    @property
    def ok(self) -> bool:
        return self.record is not None and self.canonical_input_json is not None and not self.errors


def build_replay_decision_input_v1(
    raw_snapshot_row: Mapping[str, Any] | Any, *, strict: bool = False,
) -> ReplayDecisionInputBuildResult:
    """Build a blind replay input from immutable collector evidence.

    The default accepts legacy collector rows after rebuilding only reviewed
    decision-time fields.  ``strict=True`` retains the original v1 contract
    for callers that need every v1 provenance and context field present.
    """
    mismatch = shared_candidate_identity_mismatch(raw_snapshot_row)
    if mismatch:
        return _failure(mismatch, mismatch.removesuffix("_mismatch"), "recorded root and shared candidate identities conflict")
    if strict:
        return _build_replay_decision_input_strict_v1(raw_snapshot_row)
    return _build_replay_decision_input_sanitized_v1(raw_snapshot_row)


def _build_replay_decision_input_strict_v1(raw_snapshot_row: Mapping[str, Any] | Any) -> ReplayDecisionInputBuildResult:
    """Build a fresh v1 input using only reviewed decision-time weather fields."""
    if not isinstance(raw_snapshot_row, Mapping):
        return _failure("invalid_snapshot_row", "$", "collector snapshot row must be a mapping")

    row = {str(key): value for key, value in raw_snapshot_row.items()}
    errors: list[ReplayDecisionInputError] = []
    is_collector_v2 = row.get("collector_artifact_schema_version") == 2
    raw_row_sha256 = _canonical_sha256(raw_snapshot_row, errors)
    if errors:
        return ReplayDecisionInputBuildResult(record=None, errors=tuple(errors))
    assert raw_row_sha256 is not None

    observed_at = _validated_utc_timestamp(row.get("observed_at"), path="observed_at", errors=errors)
    shared_snapshot_id = _required_text(row.get("shared_snapshot_id"), "shared_snapshot_id", errors)
    shared_candidate_id = _required_text(row.get("shared_candidate_id"), "shared_candidate_id", errors)
    market_id = _required_text(row.get("market_id"), "market_id", errors)
    question = _required_text(row.get("question"), "question", errors)
    yes_price = _validated_price(row.get("yes_price"), "yes_price", errors)
    no_price = _validated_price(row.get("no_price"), "no_price", errors)

    provenance = _required_mapping(row.get("collector_provenance"), "collector_provenance", errors)
    raw_payload_sha256 = _required_sha256(_field(provenance, "raw_payload_sha256"), "collector_provenance.raw_payload_sha256", errors)
    collector_index_entry_sha256 = _required_sha256(_field(provenance, "collector_index_entry_sha256"), "collector_provenance.collector_index_entry_sha256", errors)

    decision_context = _required_mapping(row.get("replay_decision_context"), "replay_decision_context", errors)
    strategy_input_schema_version = _required_text(_field(decision_context, "strategy_input_schema_version"), "replay_decision_context.strategy_input_schema_version", errors)
    for field in _REQUIRED_DECISION_CONTEXT_HASHES:
        _required_sha256(_field(decision_context, field), f"replay_decision_context.{field}", errors)

    derived = _required_mapping(row.get("replay_derived_features"), "replay_derived_features", errors)
    derived_schema_version = _required_text(_field(derived, "schema_version"), "replay_derived_features.schema_version", errors)
    derived_values = _required_mapping(_field(derived, "values"), "replay_derived_features.values", errors)

    artifact = _required_mapping(row.get("decision_artifact"), "decision_artifact", errors)
    source_context = _required_mapping(_field(artifact, "source_context"), "decision_artifact.source_context", errors)
    source_context_source = _required_text(
        _field(source_context, "source"), "decision_artifact.source_context.source", errors,
    )
    source_context_as_of = _validated_utc_timestamp(_field(source_context, "as_of"), path="decision_artifact.source_context.as_of", errors=errors)
    source_context_data = _required_mapping(_field(source_context, "data"), "decision_artifact.source_context.data", errors)
    market_metadata = _required_mapping(_field(source_context_data, "market_metadata"), "decision_artifact.source_context.data.market_metadata", errors)
    weather_source_snapshot = _required_mapping(
        _field(source_context_data, "weather_source_snapshot"),
        "decision_artifact.source_context.data.weather_source_snapshot",
        errors,
    )
    source_snapshots = _field(artifact, "source_snapshots")
    if not isinstance(source_snapshots, list):
        errors.append(ReplayDecisionInputError("missing_required_field", "decision_artifact.source_snapshots", "source snapshots must be a recorded list"))
    execution_snapshot = _required_mapping(_field(artifact, "execution_snapshot"), "decision_artifact.execution_snapshot", errors)
    best_yes_ask = _validated_price(_field(execution_snapshot, "best_yes_ask"), "decision_artifact.execution_snapshot.best_yes_ask", errors)
    best_no_ask = _validated_price(_field(execution_snapshot, "best_no_ask"), "decision_artifact.execution_snapshot.best_no_ask", errors)

    # Reject dangerous aliases only where data can cross the boundary.  Raw
    # collector rows may retain unrelated historical records for audit.
    for value, path in (
        (source_context_data, "decision_artifact.source_context.data"),
        (source_snapshots, "decision_artifact.source_snapshots"),
        (derived_values, "replay_derived_features.values"),
        (execution_snapshot, "decision_artifact.execution_snapshot"),
    ):
        errors.extend(_copied_subtree_forbidden_errors(value, path))

    allowed_source_data = _allowlisted_mapping(source_context_data, _SOURCE_CONTEXT_DATA_V1, "decision_artifact.source_context.data", errors)
    allowed_source_snapshots = _allowlisted_list(source_snapshots, _SOURCE_SNAPSHOT_V1, "decision_artifact.source_snapshots", errors)
    allowed_derived_values = _allowlisted_mapping(derived_values, _DERIVED_FEATURE_VALUES_V1, "replay_derived_features.values", errors)
    allowed_market_metadata = _allowlisted_mapping(market_metadata, _MARKET_METADATA_V1, "decision_artifact.source_context.data.market_metadata", errors)
    allowed_execution_snapshot = _allowlisted_mapping(execution_snapshot, _EXECUTION_SNAPSHOT_V1, "decision_artifact.execution_snapshot", errors)

    if isinstance(weather_source_snapshot, Mapping) and not _has_useful_source_data(
        allowed_source_data.get("weather_source_snapshot", {}),
    ):
        errors.append(ReplayDecisionInputError(
            "missing_required_field", "decision_artifact.source_context.data.weather_source_snapshot",
            "usable allowlisted weather source evidence is required",
        ))
    if is_collector_v2 and isinstance(weather_source_snapshot, Mapping):
        _validate_collector_v2_source_evidence(weather_source_snapshot, errors)

    if errors:
        return ReplayDecisionInputBuildResult(record=None, errors=tuple(errors))
    assert all(value is not None for value in (
        observed_at, shared_snapshot_id, shared_candidate_id, market_id, question, yes_price, no_price,
        provenance, raw_payload_sha256, collector_index_entry_sha256, decision_context,
        strategy_input_schema_version, derived_schema_version, source_context, source_context_source, source_context_as_of,
        allowed_source_data, allowed_source_snapshots, allowed_derived_values, allowed_market_metadata,
        allowed_execution_snapshot, best_yes_ask, best_no_ask,
    ))

    record = {
        "schema_name": REPLAY_DECISION_INPUT_SCHEMA_NAME,
        "schema_version": REPLAY_DECISION_INPUT_SCHEMA_VERSION,
        "decision_key": {
            "shared_snapshot_id": shared_snapshot_id, "shared_candidate_id": shared_candidate_id,
            "market_id": market_id, "observed_at_utc": observed_at, "raw_row_sha256": raw_row_sha256,
        },
        "shared_snapshot_id": shared_snapshot_id, "shared_candidate_id": shared_candidate_id,
        "market_id": market_id, "observed_at": observed_at,
        "snapshot_provenance": {
            "raw_row_sha256": raw_row_sha256, "raw_payload_sha256": raw_payload_sha256,
            "collector_index_entry_sha256": collector_index_entry_sha256,
        },
        "market": {
            "question": question, "market_metadata": allowed_market_metadata, "yes_price": yes_price,
            "no_price": no_price, "best_yes_ask": best_yes_ask, "best_no_ask": best_no_ask,
            "execution_snapshot": allowed_execution_snapshot,
        },
        "source_inputs": {
            "recorded_as_of": source_context_as_of,
            "source_context": {
                "source": source_context_source,
                "mode": _json_scalar(_field(source_context, "mode")), "as_of": source_context_as_of,
                "data": allowed_source_data,
            },
            "source_snapshots": allowed_source_snapshots,
        },
        "derived_features": {"schema_version": derived_schema_version, "values": allowed_derived_values},
        "decision_context": {
            "strategy_input_schema_version": strategy_input_schema_version,
            **{field: str(decision_context[field]) for field in _REQUIRED_DECISION_CONTEXT_HASHES},
        },
    }
    canonical_input_json = _canonical_json_bytes(record, errors)
    if errors:
        return ReplayDecisionInputBuildResult(record=None, errors=tuple(errors))
    assert canonical_input_json is not None
    record["canonical_input_sha256"] = hashlib.sha256(canonical_input_json).hexdigest()
    return ReplayDecisionInputBuildResult(record=record, canonical_input_json=canonical_input_json)


def _build_replay_decision_input_sanitized_v1(raw_snapshot_row: Mapping[str, Any] | Any) -> ReplayDecisionInputBuildResult:
    """Rebuild a usable legacy row without carrying historical outputs forward."""
    if not isinstance(raw_snapshot_row, Mapping):
        return _failure("invalid_snapshot_row", "$", "collector snapshot row must be a mapping")

    row = {str(key): value for key, value in raw_snapshot_row.items()}
    errors: list[ReplayDecisionInputError] = []
    is_collector_v2 = row.get("collector_artifact_schema_version") == 2
    raw_row_sha256 = _canonical_sha256(raw_snapshot_row, errors)
    if errors or raw_row_sha256 is None:
        return ReplayDecisionInputBuildResult(record=None, errors=tuple(errors))

    observed_at = _validated_utc_timestamp(row.get("observed_at"), path="observed_at", errors=errors)
    market_id = _required_text(row.get("market_id"), "market_id", errors)
    question = _required_text(row.get("question"), "question", errors)
    artifact = _required_mapping(row.get("decision_artifact"), "decision_artifact", errors)
    source_context = _required_mapping(_field(artifact, "source_context"), "decision_artifact.source_context", errors)
    source_context_source = _required_text(
        _field(source_context, "source"), "decision_artifact.source_context.source", errors,
    )
    source_context_data = _required_mapping(
        _field(source_context, "data"), "decision_artifact.source_context.data", errors,
    )
    if errors:
        return ReplayDecisionInputBuildResult(record=None, errors=tuple(errors))
    assert observed_at is not None and market_id is not None and question is not None
    assert source_context is not None and source_context_source is not None and source_context_data is not None

    omitted_fields: list[dict[str, str]] = []
    _record_legacy_output_omissions(row, omitted_fields)
    allowed_source_data = _sanitized_allowlisted_mapping(
        source_context_data, _SOURCE_CONTEXT_DATA_V1, "decision_artifact.source_context.data", omitted_fields,
    )
    source_snapshots = _field(artifact, "source_snapshots")
    allowed_source_snapshots = _sanitized_allowlisted_list(
        source_snapshots, _SOURCE_SNAPSHOT_V1, "decision_artifact.source_snapshots", omitted_fields,
    ) if isinstance(source_snapshots, list) else []
    if "source_snapshots" in artifact and not isinstance(source_snapshots, list):
        _add_omitted_field(omitted_fields, "decision_artifact.source_snapshots", "unallowlisted")
    execution_snapshot = _field(artifact, "execution_snapshot")
    allowed_execution_snapshot = _sanitized_allowlisted_mapping(
        execution_snapshot, _EXECUTION_SNAPSHOT_V1, "decision_artifact.execution_snapshot", omitted_fields,
    ) if isinstance(execution_snapshot, Mapping) else {}
    if "execution_snapshot" in artifact and not isinstance(execution_snapshot, Mapping):
        _add_omitted_field(omitted_fields, "decision_artifact.execution_snapshot", "unallowlisted")

    collector_provenance = row.get("collector_provenance")
    if "collector_provenance" in row and not isinstance(collector_provenance, Mapping):
        _add_omitted_field(omitted_fields, "collector_provenance", "unallowlisted")
    derived_features_value = row.get("replay_derived_features")
    if "replay_derived_features" in row and not isinstance(derived_features_value, Mapping):
        _add_omitted_field(omitted_fields, "replay_derived_features", "unallowlisted")
    decision_context_value = row.get("replay_decision_context")
    if "replay_decision_context" in row and not isinstance(decision_context_value, Mapping):
        _add_omitted_field(omitted_fields, "replay_decision_context", "unallowlisted")

    derived_features = _sanitized_derived_features(derived_features_value, omitted_fields)
    decision_context = _sanitized_decision_context(decision_context_value, omitted_fields)
    weather_source_snapshot = allowed_source_data.get("weather_source_snapshot")
    if not isinstance(weather_source_snapshot, Mapping) or not _has_useful_source_data(weather_source_snapshot):
        return _failure(
            "missing_required_field", "decision_artifact.source_context.data.weather_source_snapshot",
            "usable allowlisted weather source evidence is required",
        )
    if is_collector_v2:
        _validate_collector_v2_source_evidence(weather_source_snapshot, errors)
    if errors:
        return ReplayDecisionInputBuildResult(record=None, errors=tuple(errors))

    prices = _available_decision_time_prices(row, execution_snapshot)
    if not prices:
        return _failure(
            "invalid_decision_time_price", "yes_price",
            "a valid decision-time quote or executable ask is required",
        )

    shared_snapshot_id = _optional_identity_text(row.get("shared_snapshot_id"))
    if "shared_snapshot_id" in row and shared_snapshot_id is None:
        _add_omitted_field(omitted_fields, "shared_snapshot_id", "unallowlisted")
    shared_candidate_id = _optional_identity_text(row.get("shared_candidate_id"))
    if "shared_candidate_id" in row and shared_candidate_id is None:
        _add_omitted_field(omitted_fields, "shared_candidate_id", "unallowlisted")
    if is_collector_v2 and shared_snapshot_id is None:
        errors.append(ReplayDecisionInputError(
            "missing_required_field", "shared_snapshot_id",
            "collector artifact schema v2 requires its recorded shared snapshot identity",
        ))
    if is_collector_v2 and shared_candidate_id is None:
        errors.append(ReplayDecisionInputError(
            "missing_required_field", "shared_candidate_id",
            "collector artifact schema v2 requires its recorded shared candidate identity",
        ))
    if errors:
        return ReplayDecisionInputBuildResult(record=None, errors=tuple(errors))

    legacy_identity = not shared_snapshot_id or not shared_candidate_id
    if legacy_identity:
        shared_snapshot_id = f"legacy-snapshot-{raw_row_sha256}"
        shared_candidate_id = f"legacy-candidate-{raw_row_sha256}"

    source_context_as_of = _optional_utc_timestamp(_field(source_context, "as_of"))
    market_metadata = allowed_source_data.get("market_metadata", {})
    input_mode = (
        "legacy_sanitized_v1"
        if legacy_identity
        else "collector_v2_sanitized_v1"
        if is_collector_v2
        else "sanitized_v1"
    )
    record: dict[str, Any] = {
        "schema_name": REPLAY_DECISION_INPUT_SCHEMA_NAME,
        "schema_version": REPLAY_DECISION_INPUT_SCHEMA_VERSION,
        "input_mode": input_mode,
        "decision_key": {
            "shared_snapshot_id": shared_snapshot_id,
            "shared_candidate_id": shared_candidate_id,
            "market_id": market_id,
            "observed_at_utc": observed_at,
            "raw_row_sha256": raw_row_sha256,
        },
        "shared_snapshot_id": shared_snapshot_id,
        "shared_candidate_id": shared_candidate_id,
        "market_id": market_id,
        "observed_at": observed_at,
        "snapshot_provenance": _sanitized_provenance(
            collector_provenance, raw_row_sha256,
            input_mode=input_mode,
            omitted_fields=omitted_fields,
        ),
        "market": {
            "question": question,
            "market_metadata": market_metadata,
            **prices,
        },
        "source_inputs": {
            "source_context": {
                "source": source_context_source,
                "mode": _json_scalar(_field(source_context, "mode")),
                "data": allowed_source_data,
            },
            "source_snapshots": allowed_source_snapshots,
        },
        "sanitization": {"omitted_fields": _sorted_omitted_fields(omitted_fields)},
    }
    if source_context_as_of is not None:
        record["source_inputs"]["recorded_as_of"] = source_context_as_of
        record["source_inputs"]["source_context"]["as_of"] = source_context_as_of
    if allowed_execution_snapshot:
        record["market"]["execution_snapshot"] = allowed_execution_snapshot
    if derived_features is not None:
        record["derived_features"] = derived_features
    if decision_context:
        record["decision_context"] = decision_context

    canonical_input_json = _canonical_json_bytes(record, errors)
    if errors or canonical_input_json is None:
        return ReplayDecisionInputBuildResult(record=None, errors=tuple(errors))
    record["canonical_input_sha256"] = hashlib.sha256(canonical_input_json).hexdigest()
    return ReplayDecisionInputBuildResult(record=record, canonical_input_json=canonical_input_json)


def _sanitized_provenance(
    value: Any, raw_row_sha256: str, *, input_mode: str, omitted_fields: list[dict[str, str]],
) -> dict[str, str]:
    provenance = {"input_mode": input_mode, "raw_row_sha256": raw_row_sha256}
    if isinstance(value, Mapping):
        for key in ("raw_payload_sha256", "collector_index_entry_sha256"):
            candidate = _field(value, key)
            if isinstance(candidate, str) and _SHA256_RE.fullmatch(candidate):
                provenance[key] = candidate
            elif key in value:
                _add_omitted_field(omitted_fields, f"collector_provenance.{key}", "unallowlisted")
    return provenance


def _available_decision_time_prices(row: Mapping[str, Any], execution_snapshot: Any) -> dict[str, float]:
    values: dict[str, Any] = {"yes_price": row.get("yes_price"), "no_price": row.get("no_price")}
    if isinstance(execution_snapshot, Mapping):
        for key in ("best_yes_ask", "best_no_ask"):
            values[key] = _field(execution_snapshot, key)
    prices: dict[str, float] = {}
    for key, value in values.items():
        price = _valid_price_or_none(value)
        if price is not None:
            prices[key] = price
    return prices


def _validate_collector_v2_source_evidence(
    weather_source_snapshot: Mapping[str, Any], errors: list[ReplayDecisionInputError],
) -> None:
    """Require new collector rows to classify each retained source honestly."""
    sources = weather_source_snapshot.get("sources")
    base_path = "decision_artifact.source_context.data.weather_source_snapshot.sources"
    if not isinstance(sources, list) or not sources:
        errors.append(ReplayDecisionInputError(
            "missing_required_field", base_path,
            "collector artifact schema v2 requires recorded source evidence",
        ))
        return
    for index, source in enumerate(sources):
        path = f"{base_path}[{index}]"
        if not isinstance(source, Mapping):
            errors.append(ReplayDecisionInputError("invalid_source_evidence", path, "source evidence must be a mapping"))
            continue
        if source.get("source_evidence_version") != 1:
            errors.append(ReplayDecisionInputError(
                "missing_required_field", f"{path}.source_evidence_version",
                "collector artifact schema v2 requires source evidence version 1",
            ))
        evidence_type = source.get("evidence_type")
        if evidence_type not in {"forecast", "forecast_unavailable", "observation"}:
            errors.append(ReplayDecisionInputError(
                "invalid_source_evidence", f"{path}.evidence_type",
                "source evidence type must classify forecast, unavailable forecast, or observation",
            ))
        scoreable = source.get("scoreable_forecast")
        if not isinstance(scoreable, bool):
            errors.append(ReplayDecisionInputError(
                "missing_required_field", f"{path}.scoreable_forecast",
                "source evidence must state whether the record is a scoreable forecast",
            ))
        if evidence_type == "forecast" and scoreable is True:
            mapping = source.get("target_mapping")
            if not isinstance(mapping, Mapping) or not mapping.get("market_target_date") or not mapping.get("source_target_date"):
                errors.append(ReplayDecisionInputError(
                    "missing_required_field", f"{path}.target_mapping",
                    "scoreable forecasts require recorded market and source target dates",
                ))


def _has_useful_source_data(value: Mapping[str, Any]) -> bool:
    for nested_value in value.values():
        if isinstance(nested_value, Mapping) and _has_useful_source_data(nested_value):
            return True
        if isinstance(nested_value, list) and nested_value:
            return True
        if nested_value not in ({}, [], None, ""):
            return True
    return False


def _sanitized_derived_features(value: Any, omitted_fields: list[dict[str, str]]) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    schema_version = _safe_optional_mapping_text(_field(value, "schema_version"))
    if "schema_version" in value and schema_version is None:
        _add_omitted_field(omitted_fields, "replay_derived_features.schema_version", "unallowlisted")
    values = _field(value, "values")
    if not isinstance(values, Mapping):
        _add_omitted_field(omitted_fields, "replay_derived_features.values", "unallowlisted")
        return None
    copied = _sanitized_allowlisted_mapping(
        values, _DERIVED_FEATURE_VALUES_V1, "replay_derived_features.values", omitted_fields,
        reject_sensitive_string_values=True,
    )
    if schema_version is None:
        return {"values": copied}
    return {"schema_version": schema_version, "values": copied}


def _sanitized_decision_context(value: Any, omitted_fields: list[dict[str, str]]) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    copied: dict[str, str] = {}
    schema_version = _safe_optional_mapping_text(_field(value, "strategy_input_schema_version"))
    if "strategy_input_schema_version" in value and schema_version is None:
        _add_omitted_field(omitted_fields, "replay_decision_context.strategy_input_schema_version", "unallowlisted")
    if schema_version is not None:
        copied["strategy_input_schema_version"] = schema_version
    for field in _REQUIRED_DECISION_CONTEXT_HASHES:
        candidate = _field(value, field)
        if isinstance(candidate, str) and _SHA256_RE.fullmatch(candidate):
            copied[field] = candidate
        elif candidate is not None:
            _add_omitted_field(omitted_fields, f"replay_decision_context.{field}", "unallowlisted")
    for key, nested in value.items():
        if key not in {"strategy_input_schema_version", *_REQUIRED_DECISION_CONTEXT_HASHES}:
            _record_omitted_subtree(nested, f"replay_decision_context.{key}", omitted_fields)
    return copied


def _record_legacy_output_omissions(row: Mapping[str, Any], omitted_fields: list[dict[str, str]]) -> None:
    for key, value in row.items():
        key_text = str(key)
        if key_text in {
            "main_decision", "normal_decision", "shared_pipeline", "decision_type", "direction",
            "recorded_prediction", "weather_risk", "paper_lab", "opportunity_mode",
        } or _omission_category(key_text) != "unallowlisted":
            _record_omitted_subtree(
                value, key_text, omitted_fields,
                category="recorded_decision" if _omission_category(key_text) == "recorded_decision" else None,
            )
        elif key_text not in _SANITIZED_TOP_LEVEL_CONTRACT_FIELDS:
            _add_omitted_field(omitted_fields, key_text, "unallowlisted")
    artifact = _field(row, "decision_artifact")
    if not isinstance(artifact, Mapping):
        return
    retained = {"source_context", "source_snapshots", "execution_snapshot"}
    for key, value in artifact.items():
        if key not in retained:
            _record_omitted_subtree(value, f"decision_artifact.{key}", omitted_fields, category="recorded_decision")


def _sanitized_allowlisted_mapping(
    value: Mapping[str, Any], schema: Mapping[str, Any], path: str, omitted_fields: list[dict[str, str]],
    *, reject_sensitive_string_values: bool = False,
) -> dict[str, Any]:
    copied: dict[str, Any] = {}
    for key, nested_value in value.items():
        key_text = str(key)
        nested_path = f"{path}.{key_text}"
        spec = schema.get(key_text)
        if spec is None:
            _record_omitted_subtree(nested_value, nested_path, omitted_fields)
            continue
        copied_value = _sanitized_allowlisted_value(
            nested_value, spec, nested_path, omitted_fields,
            reject_sensitive_string_values=reject_sensitive_string_values,
        )
        if copied_value is not _UNSET:
            copied[key_text] = copied_value
    return copied


def _sanitized_allowlisted_list(
    value: list[Any], item_schema: Any, path: str, omitted_fields: list[dict[str, str]],
    *, reject_sensitive_string_values: bool = False,
) -> list[Any]:
    copied: list[Any] = []
    for index, item in enumerate(value):
        copied_value = _sanitized_allowlisted_value(
            item, item_schema, f"{path}[{index}]", omitted_fields,
            reject_sensitive_string_values=reject_sensitive_string_values,
        )
        if copied_value is not _UNSET:
            copied.append(copied_value)
    return copied


def _sanitized_allowlisted_value(
    value: Any, spec: Any, path: str, omitted_fields: list[dict[str, str]],
    *, reject_sensitive_string_values: bool = False,
) -> Any:
    if spec is _SCALAR:
        sensitive_category = _string_semantics_omission_category(value)
        if reject_sensitive_string_values and sensitive_category is not None:
            _add_omitted_field(omitted_fields, path, sensitive_category)
            return _UNSET
        if isinstance(value, _JSON_SCALAR_TYPES):
            return _json_scalar(value)
        _add_omitted_field(omitted_fields, path, "unallowlisted")
        return _UNSET
    if spec is _OPTIONAL_WEATHER_PROVENANCE:
        if value is None:
            return None
        if isinstance(value, Mapping):
            return _sanitized_allowlisted_mapping(
                value, _WEATHER_PROVENANCE_V1, path, omitted_fields,
                reject_sensitive_string_values=reject_sensitive_string_values,
            )
        _add_omitted_field(omitted_fields, path, "unallowlisted")
        return _UNSET
    if isinstance(spec, Mapping):
        if isinstance(value, Mapping):
            return _sanitized_allowlisted_mapping(
                value, spec, path, omitted_fields,
                reject_sensitive_string_values=reject_sensitive_string_values,
            )
        _add_omitted_field(omitted_fields, path, "unallowlisted")
        return _UNSET
    if isinstance(spec, list) and len(spec) == 1:
        if isinstance(value, list):
            return _sanitized_allowlisted_list(
                value, spec[0], path, omitted_fields,
                reject_sensitive_string_values=reject_sensitive_string_values,
            )
        _add_omitted_field(omitted_fields, path, "unallowlisted")
        return _UNSET
    raise AssertionError(f"invalid allowlist specification at {path}")


def _record_omitted_subtree(
    value: Any, path: str, omitted_fields: list[dict[str, str]], *, category: str | None = None,
) -> None:
    category = category or _omission_category(path.rsplit(".", 1)[-1])
    if category != "unallowlisted" or not isinstance(value, (Mapping, list)):
        _add_omitted_field(omitted_fields, path, category)
        return
    if isinstance(value, Mapping) and value and _subtree_has_sensitive_field(value):
        for key, nested_value in value.items():
            _record_omitted_subtree(nested_value, f"{path}.{key}", omitted_fields)
        return
    if isinstance(value, list) and value and _subtree_has_sensitive_field(value):
        for index, nested_value in enumerate(value):
            _record_omitted_subtree(nested_value, f"{path}[{index}]", omitted_fields)
        return
    _add_omitted_field(omitted_fields, path, category)


def _omission_category(name: str) -> str:
    normalized = _normalized_field_name(name)
    if _field_name_is_forbidden(name):
        return "outcome_or_future"
    if normalized in _FORBIDDEN_RECORDED_DECISION_FIELDS or normalized in {"direction", "side", "entry_price", "win_probability"}:
        return "recorded_decision"
    return "unallowlisted"


def _subtree_has_sensitive_field(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, nested_value in value.items():
            if _omission_category(str(key)) != "unallowlisted" or _subtree_has_sensitive_field(nested_value):
                return True
    elif isinstance(value, list):
        return any(_subtree_has_sensitive_field(item) for item in value)
    return False


def _add_omitted_field(omitted_fields: list[dict[str, str]], path: str, category: str) -> None:
    entry = {"path": path, "category": category}
    if entry not in omitted_fields:
        omitted_fields.append(entry)


def _sorted_omitted_fields(omitted_fields: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(omitted_fields, key=lambda entry: (entry["path"], entry["category"]))


def _optional_text(value: Any) -> str | None:
    return str(value) if value is not None and str(value).strip() else None


def _safe_optional_mapping_text(value: Any) -> str | None:
    """Accept only ordinary non-empty labels without replay-sensitive semantics."""
    if type(value) is not str or not value.strip() or _string_semantics_omission_category(value) is not None:
        return None
    return value


def _string_semantics_omission_category(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    category = _omission_category(value)
    return category if category != "unallowlisted" else None


def _optional_identity_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _optional_utc_timestamp(value: Any) -> str | None:
    errors: list[ReplayDecisionInputError] = []
    return _validated_utc_timestamp(value, path="optional", errors=errors) if _optional_text(value) else None


def _valid_price_or_none(value: Any) -> float | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if isfinite(price) and 0 <= price <= 1 else None


def verify_replay_decision_input_v1(record: Mapping[str, Any] | Any, canonical_input_json: bytes | bytearray | Any) -> bool:
    """Verify the immutable canonical bytes and advertised hash against a record."""
    if not isinstance(record, Mapping) or not isinstance(canonical_input_json, bytes):
        return False
    expected_hash = record.get("canonical_input_sha256")
    if not isinstance(expected_hash, str) or not _SHA256_RE.fullmatch(expected_hash):
        return False
    unsigned_record = {str(key): value for key, value in record.items() if key != "canonical_input_sha256"}
    errors: list[ReplayDecisionInputError] = []
    recomputed = _canonical_json_bytes(unsigned_record, errors)
    if errors or recomputed is None:
        return False
    return hmac.compare_digest(recomputed, canonical_input_json) and hmac.compare_digest(
        hashlib.sha256(canonical_input_json).hexdigest(), expected_hash
    )


def verify_replay_decision_input_record_v1(record: Mapping[str, Any] | Any) -> bool:
    """Verify the self-contained canonical record emitted by the replay exporter."""
    if not isinstance(record, Mapping):
        return False
    unsigned_record = {str(key): value for key, value in record.items() if key != "canonical_input_sha256"}
    errors: list[ReplayDecisionInputError] = []
    canonical_input_json = _canonical_json_bytes(unsigned_record, errors)
    return canonical_input_json is not None and not errors and verify_replay_decision_input_v1(record, canonical_input_json)


def _failure(code: str, path: str, message: str) -> ReplayDecisionInputBuildResult:
    return ReplayDecisionInputBuildResult(record=None, errors=(ReplayDecisionInputError(code, path, message),))


def _copied_subtree_forbidden_errors(value: Any, path: str) -> list[ReplayDecisionInputError]:
    errors: list[ReplayDecisionInputError] = []
    if isinstance(value, Mapping):
        for key, nested_value in value.items():
            key_text = str(key)
            nested_path = f"{path}.{key_text}"
            normalized = _normalized_field_name(key_text)
            if _field_name_is_forbidden(key_text):
                errors.append(ReplayDecisionInputError("forbidden_outcome_or_future_field", nested_path, "copied input contains outcome, settlement, or future-looking data"))
            if normalized in _FORBIDDEN_RECORDED_DECISION_FIELDS:
                errors.append(ReplayDecisionInputError("forbidden_recorded_decision_field", nested_path, "recorded decision outputs cannot reach a blind replay lane"))
            errors.extend(_copied_subtree_forbidden_errors(nested_value, nested_path))
    elif isinstance(value, list):
        for index, nested_value in enumerate(value):
            errors.extend(_copied_subtree_forbidden_errors(nested_value, f"{path}[{index}]"))
    return errors


def _field_name_is_forbidden(name: str) -> bool:
    normalized = _normalized_field_name(name)
    if normalized in _SAFE_PROVENANCE_FIELD_NAMES:
        return False
    return bool(set(_field_tokens(name)) & _FORBIDDEN_INPUT_TOKENS)


def _field_tokens(name: str) -> tuple[str, ...]:
    split = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name))
    return tuple(token for token in re.split(r"[^a-z0-9]+", split.lower()) if token)


def _normalized_field_name(name: str) -> str:
    return "_".join(_field_tokens(name))


def _allowlisted_mapping(value: Any, schema: Mapping[str, Any], path: str, errors: list[ReplayDecisionInputError]) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    copied: dict[str, Any] = {}
    for key, nested_value in value.items():
        key_text = str(key)
        nested_path = f"{path}.{key_text}"
        spec = schema.get(key_text)
        if spec is None:
            errors.append(ReplayDecisionInputError("unknown_unallowlisted_input_field", nested_path, "field is not permitted by replay decision input v1"))
            continue
        copied_value = _allowlisted_value(nested_value, spec, nested_path, errors)
        if copied_value is not _UNSET:
            copied[key_text] = copied_value
    return copied


_UNSET = object()


def _allowlisted_list(value: Any, item_schema: Any, path: str, errors: list[ReplayDecisionInputError]) -> list[Any] | None:
    if not isinstance(value, list):
        return None
    copied: list[Any] = []
    for index, item in enumerate(value):
        copied_value = _allowlisted_value(item, item_schema, f"{path}[{index}]", errors)
        if copied_value is not _UNSET:
            copied.append(copied_value)
    return copied


def _allowlisted_value(value: Any, spec: Any, path: str, errors: list[ReplayDecisionInputError]) -> Any:
    if spec is _SCALAR:
        if isinstance(value, _JSON_SCALAR_TYPES):
            return _json_scalar(value)
        errors.append(ReplayDecisionInputError("invalid_unallowlisted_input_type", path, "allowlisted input field must be a JSON scalar"))
        return _UNSET
    if spec is _OPTIONAL_WEATHER_PROVENANCE:
        if value is None:
            return None
        copied = _allowlisted_mapping(value, _WEATHER_PROVENANCE_V1, path, errors)
        if copied is None:
            errors.append(ReplayDecisionInputError("invalid_unallowlisted_input_type", path, "weather provenance must be a mapping or null"))
            return _UNSET
        return copied
    if isinstance(spec, Mapping):
        copied = _allowlisted_mapping(value, spec, path, errors)
        if copied is None:
            errors.append(ReplayDecisionInputError("invalid_unallowlisted_input_type", path, "allowlisted input field must be a mapping"))
            return _UNSET
        return copied
    if isinstance(spec, list) and len(spec) == 1:
        copied = _allowlisted_list(value, spec[0], path, errors)
        if copied is None:
            errors.append(ReplayDecisionInputError("invalid_unallowlisted_input_type", path, "allowlisted input field must be a list"))
            return _UNSET
        return copied
    raise AssertionError(f"invalid allowlist specification at {path}")


def _json_scalar(value: Any) -> Any:
    # Scalars are immutable; normalization avoids subclasses carrying mutable state.
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    return None


def _canonical_sha256(value: Any, errors: list[ReplayDecisionInputError]) -> str | None:
    canonical = _canonical_json_bytes(value, errors)
    return hashlib.sha256(canonical).hexdigest() if canonical is not None else None


def _canonical_json_bytes(value: Any, errors: list[ReplayDecisionInputError]) -> bytes | None:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        errors.append(ReplayDecisionInputError("non_canonical_snapshot", "$", f"collector snapshot cannot be canonically serialized: {exc}"))
        return None


def _field(mapping: Mapping[str, Any] | None, key: str) -> Any:
    return mapping.get(key) if mapping is not None else None


def _required_text(value: Any, path: str, errors: list[ReplayDecisionInputError]) -> str | None:
    if value is None or not str(value).strip():
        errors.append(ReplayDecisionInputError("missing_required_field", path, "required non-empty text is missing"))
        return None
    return str(value)


def _required_mapping(value: Any, path: str, errors: list[ReplayDecisionInputError]) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping):
        errors.append(ReplayDecisionInputError("missing_required_field", path, "required mapping is missing"))
        return None
    return value


def _required_sha256(value: Any, path: str, errors: list[ReplayDecisionInputError]) -> str | None:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        errors.append(ReplayDecisionInputError("invalid_sha256", path, "required lowercase SHA-256 digest is missing or invalid"))
        return None
    return value


def _validated_utc_timestamp(value: Any, *, path: str, errors: list[ReplayDecisionInputError]) -> str | None:
    if value is None or not str(value).strip():
        errors.append(ReplayDecisionInputError("missing_observed_at", path, "UTC timestamp is required"))
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        errors.append(ReplayDecisionInputError("invalid_observed_at", path, "timestamp must be ISO-8601"))
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        errors.append(ReplayDecisionInputError("observed_at_not_utc", path, "timestamp must be explicitly UTC"))
        return None
    return parsed.astimezone(timezone.utc).isoformat()


def _validated_price(value: Any, path: str, errors: list[ReplayDecisionInputError]) -> float | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        errors.append(ReplayDecisionInputError("invalid_decision_time_price", path, "finite price from zero to one is required"))
        return None
    if not isfinite(price) or not 0 <= price <= 1:
        errors.append(ReplayDecisionInputError("invalid_decision_time_price", path, "finite price from zero to one is required"))
        return None
    return price
