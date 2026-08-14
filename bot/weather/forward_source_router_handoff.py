"""Disabled, reusable forward-paper handoff manifest for the source router.

The manifest is a contract only.  It does not construct a runner, open a
wallet, query a resolver, or enable a lane.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_DECISION_KEY_FIELDS = (
    "shared_snapshot_id", "shared_candidate_id", "market_id", "observed_at_utc", "raw_row_sha256",
)
_FORBIDDEN_ROUTER_INPUT_FIELDS = {
    "official_outcome", "outcome", "settlement_ts", "resolution_id", "resolution_provenance", "resolution_resolved_at",
}
_FORBIDDEN_ROUTER_INPUT_TOKENS = ("wallet", "pnl", "payout")


class ForwardPaperHandoffError(ValueError):
    """The disabled forward-paper contract is invalid or being misused."""


def build_forward_source_router_handoff(
    *, cohort_id: str, start_at: str, shared_snapshot_manifest_id: str,
    policy_bundles: Mapping[str, Mapping[str, str]], evaluator_version: str = "source-router-forward-evaluator-v1",
    environment: str = "paper",
) -> dict[str, Any]:
    """Build an inert control-vs-router cohort manifest.

    ``environment='live'`` is allowed only to reserve a distinct *future*
    namespace; this builder always returns ``enabled: false``.
    """

    _required_text(cohort_id, "cohort_id")
    start = _timestamp(start_at, "start_at")
    _required_text(shared_snapshot_manifest_id, "shared_snapshot_manifest_id")
    _required_text(evaluator_version, "evaluator_version")
    if environment not in {"paper", "live"}:
        raise ForwardPaperHandoffError("environment must be paper or live")
    lane_roles = {"control_stable": "control", "shadow_source_router_no_price_guard": "candidate"}
    if set(policy_bundles) != set(lane_roles):
        raise ForwardPaperHandoffError("policy_bundles must define exactly control_stable and shadow_source_router_no_price_guard")
    lanes = []
    for lane_id, role in lane_roles.items():
        bundle = policy_bundles[lane_id]
        version, bundle_hash = _required_text(bundle.get("version"), f"policy_bundles.{lane_id}.version"), bundle.get("sha256")
        if not isinstance(bundle_hash, str) or not _SHA256_RE.fullmatch(bundle_hash):
            raise ForwardPaperHandoffError(f"policy_bundles.{lane_id}.sha256 must be a SHA-256 hex digest")
        namespace_base = f"{environment}/{cohort_id}/{lane_id}"
        lanes.append({
            "lane_id": lane_id,
            "decision_role": role,
            "policy_bundle": {"version": version, "sha256": bundle_hash.lower()},
            "consumer_cursor_namespace": f"{namespace_base}/cursor",
            "decision_namespace": f"{namespace_base}/decisions",
            "paper_wallet_namespace": f"{namespace_base}/paper-wallet",
            "execution_intent_namespace": f"{namespace_base}/execution-intents",
        })
    return {
        "schema_name": "forward_source_router_paper_handoff",
        "schema_version": 1,
        "enabled": False,
        "paper_only": True,
        "runtime_action": "none_manifest_only",
        "cohort_id": cohort_id,
        "environment": environment,
        "start_at": start,
        "shared_collector_snapshot_manifest": {"manifest_id": shared_snapshot_manifest_id},
        "eligible_lanes": lanes,
        "evaluator_version": evaluator_version,
        "decision_idempotency": {
            "fields": ["decision_key", "environment", "lane_id", "policy_bundle_hash", "evaluator_version", "decision_role"],
            "algorithm": "sha256_canonical_json_v1",
        },
        "router_input_contract": {
            "allowed_phase": "decision_time_collector_snapshot_only",
            "forbidden_categories": ["resolution", "wallet", "pnl"],
            "forbidden_fields": sorted(_FORBIDDEN_ROUTER_INPUT_FIELDS),
            "forbidden_field_tokens": list(_FORBIDDEN_ROUTER_INPUT_TOKENS),
        },
        "forward_paper_requirements": {
            "fresh_cohort_required": True,
            "shared_snapshot_identity_required": True,
            "decision_time_executable_quote_required": True,
            "capacity_status": "pending_recorded_executable_quotes",
            "kelly_and_live_capacity": "pending; no archived executable fills or quotes may be invented",
            "resolution_for_pnl": "independently_authoritative_exact_resolution_after_decision",
        },
    }


def validate_forward_snapshot(manifest: Mapping[str, Any], snapshot: Mapping[str, Any], *, lane_id: str) -> dict[str, Any]:
    """Validate one lane's fresh shared snapshot and produce its decision identity."""

    lane = _lane(manifest, lane_id)
    expected_manifest = _nested_text(manifest, "shared_collector_snapshot_manifest", "manifest_id")
    if _text(snapshot.get("collector_snapshot_manifest_id")) != expected_manifest:
        raise ForwardPaperHandoffError("shared collector snapshot manifest identity mismatch")
    observed = _timestamp(snapshot.get("observed_at_utc"), "snapshot.observed_at_utc")
    start = _timestamp(manifest.get("start_at"), "manifest.start_at")
    if observed < start:
        raise ForwardPaperHandoffError("pre-start snapshot is not eligible for a fresh forward cohort")
    _reject_forbidden_router_input(snapshot)
    decision_key = {field: _required_text(snapshot.get(field), f"snapshot.{field}") for field in _DECISION_KEY_FIELDS}
    if not _SHA256_RE.fullmatch(decision_key["raw_row_sha256"]):
        raise ForwardPaperHandoffError("snapshot.raw_row_sha256 must be a SHA-256 hex digest")
    return {
        "decision_key": decision_key,
        "shared_snapshot_id": decision_key["shared_snapshot_id"],
        "shared_candidate_id": decision_key["shared_candidate_id"],
        "decision_namespace": lane["decision_namespace"],
        "paper_wallet_namespace": lane["paper_wallet_namespace"],
        "decision_idempotency_id": decision_idempotency_id(
            decision_key=decision_key, environment=_required_text(manifest.get("environment"), "environment"), lane_id=lane_id,
            policy_bundle_hash=lane["policy_bundle"]["sha256"], evaluator_version=_required_text(manifest.get("evaluator_version"), "evaluator_version"),
            decision_role=lane["decision_role"],
        ),
    }


def validate_shared_lane_snapshots(manifest: Mapping[str, Any], snapshots: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Reject a cohort if control and candidate did not receive one exact snapshot."""

    expected_lane_ids = {str(lane["lane_id"]) for lane in manifest.get("eligible_lanes", []) if isinstance(lane, Mapping)}
    if set(snapshots) != expected_lane_ids or len(expected_lane_ids) != 2:
        raise ForwardPaperHandoffError("paired validation requires exactly control and candidate snapshots")
    validated = {lane_id: validate_forward_snapshot(manifest, snapshot, lane_id=lane_id) for lane_id, snapshot in snapshots.items()}
    identities = {tuple(value["decision_key"][field] for field in _DECISION_KEY_FIELDS) for value in validated.values()}
    if len(identities) != 1:
        raise ForwardPaperHandoffError("control and candidate shared snapshot identities must match exactly")
    return validated


def decision_idempotency_id(
    *, decision_key: Mapping[str, str], environment: str, lane_id: str, policy_bundle_hash: str,
    evaluator_version: str, decision_role: str,
) -> str:
    """Return the immutable decision idempotency key for a single lane role."""

    payload = {
        "decision_key": {field: _required_text(decision_key.get(field), f"decision_key.{field}") for field in _DECISION_KEY_FIELDS},
        "environment": _required_text(environment, "environment"), "lane_id": _required_text(lane_id, "lane_id"),
        "policy_bundle_hash": _required_text(policy_bundle_hash, "policy_bundle_hash"),
        "evaluator_version": _required_text(evaluator_version, "evaluator_version"),
        "decision_role": _required_text(decision_role, "decision_role"),
    }
    return "sha256:" + hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def execution_intent_id(decision_id: str, environment: str, wallet_namespace: str) -> str:
    """Keep execution/wallet identity distinct from the sealed decision identity."""

    payload = {"decision_id": _required_text(decision_id, "decision_id"), "environment": _required_text(environment, "environment"), "wallet_namespace": _required_text(wallet_namespace, "wallet_namespace")}
    return "sha256:" + hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _lane(manifest: Mapping[str, Any], lane_id: str) -> Mapping[str, Any]:
    for lane in manifest.get("eligible_lanes", []):
        if isinstance(lane, Mapping) and lane.get("lane_id") == lane_id:
            return lane
    raise ForwardPaperHandoffError(f"lane is not eligible: {lane_id}")


def _reject_forbidden_router_input(value: Any, *, path: str = "snapshot") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            key_text = str(key).lower()
            if key_text in _FORBIDDEN_ROUTER_INPUT_FIELDS or any(token in key_text for token in _FORBIDDEN_ROUTER_INPUT_TOKENS):
                raise ForwardPaperHandoffError(f"router input prohibits {path}.{key}")
            _reject_forbidden_router_input(nested, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_forbidden_router_input(nested, path=f"{path}[{index}]")


def _timestamp(value: Any, field: str) -> str:
    text = _required_text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ForwardPaperHandoffError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ForwardPaperHandoffError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _nested_text(mapping: Mapping[str, Any], *keys: str) -> str:
    value: Any = mapping
    for key in keys:
        value = value.get(key) if isinstance(value, Mapping) else None
    return _required_text(value, ".".join(keys))


def _required_text(value: Any, field: str) -> str:
    text = _text(value)
    if text is None:
        raise ForwardPaperHandoffError(f"{field} must be non-empty text")
    return text


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
