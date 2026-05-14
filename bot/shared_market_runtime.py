from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Mapping

from bot.file_ops import atomic_write_json, locked_file

STATE_SCHEMA_NAME = "shared_market_runtime_state"
STATE_SCHEMA_VERSION = 1
SNAPSHOT_SCHEMA_NAME = "shared_market_snapshot_metadata"
SNAPSHOT_SCHEMA_VERSION = 1

DEFAULT_SHARED_MARKET_CONFIG: dict[str, Any] = {
    "enabled": True,
    "default_interval_seconds": 900,
    "min_interval_seconds": 300,
    "publisher_lease_timeout_seconds": 120,
    "consumer_timeout_seconds": 300,
    "stop_when_idle": True,
    "snapshot_ttl_seconds": 1200,
    "publisher_priority": {
        "collector": 30,
        "paper": 20,
        "live": 10,
    },
    "live": {
        "allow_direct_bypass": True,
        "max_snapshot_age_seconds": 30,
    },
}


def normalize_shared_market_config(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    shared_market = {}
    if isinstance(config, Mapping):
        raw = config.get("shared_market")
        if isinstance(raw, Mapping):
            shared_market = dict(raw)
    normalized = deepcopy(DEFAULT_SHARED_MARKET_CONFIG)
    normalized.update({key: value for key, value in shared_market.items() if key not in {"publisher_priority", "live"}})
    if isinstance(shared_market.get("publisher_priority"), Mapping):
        normalized["publisher_priority"].update(dict(shared_market["publisher_priority"]))
    if isinstance(shared_market.get("live"), Mapping):
        normalized["live"].update(dict(shared_market["live"]))

    normalized["default_interval_seconds"] = max(1, int(normalized.get("default_interval_seconds", 900) or 900))
    normalized["min_interval_seconds"] = max(1, int(normalized.get("min_interval_seconds", 300) or 300))
    if normalized["min_interval_seconds"] > normalized["default_interval_seconds"]:
        normalized["min_interval_seconds"] = normalized["default_interval_seconds"]
    normalized["publisher_lease_timeout_seconds"] = max(
        1,
        int(normalized.get("publisher_lease_timeout_seconds", 120) or 120),
    )
    normalized["consumer_timeout_seconds"] = max(1, int(normalized.get("consumer_timeout_seconds", 300) or 300))
    normalized["stop_when_idle"] = bool(normalized.get("stop_when_idle", True))
    normalized["snapshot_ttl_seconds"] = max(1, int(normalized.get("snapshot_ttl_seconds", 1200) or 1200))
    live = dict(normalized.get("live") or {})
    live["allow_direct_bypass"] = bool(live.get("allow_direct_bypass", True))
    live["max_snapshot_age_seconds"] = max(1, int(live.get("max_snapshot_age_seconds", 30) or 30))
    normalized["live"] = live
    normalized["publisher_priority"] = {
        str(runtime_kind): int(priority)
        for runtime_kind, priority in dict(normalized.get("publisher_priority") or {}).items()
    }
    return normalized


def shared_market_runtime_root(config: Mapping[str, Any] | None = None) -> Path:
    normalized = normalize_shared_market_config(config)
    configured_root = normalized.get("runtime_root")
    if configured_root not in (None, ""):
        return Path(str(configured_root))

    if isinstance(config, Mapping):
        runtime = config.get("runtime")
        if isinstance(runtime, Mapping) and runtime.get("base_dir") not in (None, ""):
            return Path(str(runtime["base_dir"])) / "shared_market_runtime"

        data_dir = config.get("data_dir")
        if data_dir not in (None, ""):
            base_path = Path(str(data_dir))
            if base_path.name in {"paper", "live"}:
                base_path = base_path.parent
            return base_path / "shared_market_runtime"

    return Path("data") / "shared_market_runtime"


def build_shared_market_snapshot_metadata(
    *,
    snapshot_id: str,
    observed_at: str | datetime,
    publisher_runtime: str,
    publisher_instance_id: str,
    candidate_count: int,
    ttl_seconds: int | None = None,
    published_at: str | datetime | None = None,
    source_exchange: str | None = None,
    market_count: int | None = None,
) -> dict[str, Any]:
    observed_dt = _coerce_datetime(observed_at)
    if observed_dt is None:
        raise ValueError("observed_at is required for shared snapshot metadata")
    published_dt = _coerce_datetime(published_at) or observed_dt
    ttl_value = None if ttl_seconds in (None, "") else max(1, int(ttl_seconds))
    metadata = {
        "schema_name": SNAPSHOT_SCHEMA_NAME,
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "snapshot_id": str(snapshot_id or ""),
        "observed_at": _isoformat(observed_dt),
        "published_at": _isoformat(published_dt),
        "publisher_runtime": str(publisher_runtime or ""),
        "publisher_instance_id": str(publisher_instance_id or ""),
        "candidate_count": max(0, int(candidate_count or 0)),
        "market_count": max(0, int(market_count if market_count is not None else candidate_count or 0)),
        "ttl_seconds": ttl_value,
        "source_exchange": str(source_exchange) if source_exchange not in (None, "") else None,
    }
    if ttl_value is not None:
        metadata["expires_at"] = _isoformat(observed_dt + timedelta(seconds=ttl_value))
    return metadata


def snapshot_age_seconds(snapshot_metadata: Mapping[str, Any] | None, *, now: str | datetime | None = None) -> float | None:
    if not isinstance(snapshot_metadata, Mapping):
        return None
    reference_dt = _coerce_datetime(snapshot_metadata.get("observed_at")) or _coerce_datetime(snapshot_metadata.get("published_at"))
    if reference_dt is None:
        return None
    current_dt = _coerce_datetime(now) or datetime.now(timezone.utc)
    return max(0.0, (current_dt - reference_dt).total_seconds())


def shared_snapshot_is_fresh(
    snapshot_metadata: Mapping[str, Any] | None,
    *,
    max_snapshot_age_seconds: int,
    now: str | datetime | None = None,
) -> bool:
    current_dt = _coerce_datetime(now) or datetime.now(timezone.utc)
    expires_dt = _snapshot_expires_at(snapshot_metadata)
    if expires_dt is not None and current_dt > expires_dt:
        return False
    age_seconds = snapshot_age_seconds(snapshot_metadata, now=now)
    if age_seconds is None:
        return False
    return age_seconds <= max(1, int(max_snapshot_age_seconds))


def should_bypass_shared_snapshot(
    snapshot_metadata: Mapping[str, Any] | None,
    *,
    max_snapshot_age_seconds: int,
    allow_direct_bypass: bool = True,
    now: str | datetime | None = None,
) -> bool:
    if not allow_direct_bypass:
        return False
    return not shared_snapshot_is_fresh(
        snapshot_metadata,
        max_snapshot_age_seconds=max_snapshot_age_seconds,
        now=now,
    )


class SharedMarketRuntimeManager:
    def __init__(
        self,
        *,
        runtime_root: str | Path | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> None:
        self.config = normalize_shared_market_config(config)
        self.runtime_root = Path(runtime_root) if runtime_root is not None else shared_market_runtime_root(config)
        self.state_path = self.runtime_root / "runtime_state.json"
        self.lock_path = self.runtime_root / "runtime_state.lock"
        self.latest_snapshot_path = self.runtime_root / "latest_snapshot.json"

    @property
    def default_interval_seconds(self) -> int:
        return int(self.config["default_interval_seconds"])

    @property
    def min_interval_seconds(self) -> int:
        return int(self.config["min_interval_seconds"])

    @property
    def publisher_lease_timeout_seconds(self) -> int:
        return int(self.config["publisher_lease_timeout_seconds"])

    @property
    def consumer_timeout_seconds(self) -> int:
        return int(self.config["consumer_timeout_seconds"])

    @property
    def stop_when_idle(self) -> bool:
        return bool(self.config["stop_when_idle"])

    @property
    def snapshot_ttl_seconds(self) -> int:
        return int(self.config["snapshot_ttl_seconds"])

    def read_state(self, *, now: str | datetime | None = None) -> dict[str, Any]:
        now_dt = _coerce_datetime(now) or datetime.now(timezone.utc)
        with locked_file(self.lock_path, "a+"):
            state = self._finalize_locked(self._cleanup_expired_locked(self._load_state_locked(), now_dt), now_dt)
            self._write_state_locked(state)
            return state

    def attach(
        self,
        *,
        runtime_kind: str,
        instance_id: str,
        can_publish: bool,
        can_consume: bool,
        desired_interval_seconds: int | None = None,
        max_snapshot_age_seconds: int | None = None,
        latency_sensitive: bool = False,
        publisher_priority: int | None = None,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        now_dt = _coerce_datetime(now) or datetime.now(timezone.utc)
        consumer_key = _consumer_key(runtime_kind, instance_id)
        with locked_file(self.lock_path, "a+"):
            state = self._cleanup_expired_locked(self._load_state_locked(), now_dt)
            existing = dict((state.get("consumers") or {}).get(consumer_key) or {})
            attached_at = existing.get("attached_at") or _isoformat(now_dt)
            reacquire_required_at = existing.get("publisher_reacquire_required_at")
            if reacquire_required_at in (None, ""):
                reacquire_required_at = dict(state.get("publisher_reacquire_guards") or {}).get(consumer_key)
            consumer = {
                "runtime_kind": str(runtime_kind or ""),
                "instance_id": str(instance_id or ""),
                "can_publish": bool(can_publish),
                "can_consume": bool(can_consume),
                "desired_interval_seconds": self._normalize_desired_interval(desired_interval_seconds),
                "max_snapshot_age_seconds": _optional_positive_int(max_snapshot_age_seconds),
                "latency_sensitive": bool(latency_sensitive),
                "publisher_priority": self._publisher_priority(runtime_kind, publisher_priority),
                "attached_at": attached_at,
                "last_heartbeat_at": _isoformat(now_dt),
            }
            if reacquire_required_at not in (None, ""):
                consumer["publisher_reacquire_required_at"] = str(reacquire_required_at)
            consumers = dict(state.get("consumers") or {})
            consumers[consumer_key] = consumer
            state["consumers"] = consumers
            state = self._finalize_locked(state, now_dt)
            self._write_state_locked(state)
            return state

    def heartbeat(
        self,
        *,
        runtime_kind: str,
        instance_id: str,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        now_dt = _coerce_datetime(now) or datetime.now(timezone.utc)
        consumer_key = _consumer_key(runtime_kind, instance_id)
        with locked_file(self.lock_path, "a+"):
            state = self._cleanup_expired_locked(self._load_state_locked(), now_dt)
            consumers = dict(state.get("consumers") or {})
            consumer = dict(consumers.get(consumer_key) or {})
            if not consumer:
                raise KeyError(f"shared market consumer is not attached: {consumer_key}")
            consumer["last_heartbeat_at"] = _isoformat(now_dt)
            consumers[consumer_key] = consumer
            state["consumers"] = consumers
            if self._publisher_matches(state.get("publisher"), runtime_kind, instance_id):
                publisher = dict(state.get("publisher") or {})
                publisher["last_heartbeat_at"] = _isoformat(now_dt)
                state["publisher"] = publisher
            state = self._finalize_locked(state, now_dt)
            self._write_state_locked(state)
            return state

    def detach(
        self,
        *,
        runtime_kind: str,
        instance_id: str,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        now_dt = _coerce_datetime(now) or datetime.now(timezone.utc)
        consumer_key = _consumer_key(runtime_kind, instance_id)
        with locked_file(self.lock_path, "a+"):
            state = self._cleanup_expired_locked(self._load_state_locked(), now_dt)
            consumers = dict(state.get("consumers") or {})
            consumers.pop(consumer_key, None)
            state["consumers"] = consumers
            if self._publisher_matches(state.get("publisher"), runtime_kind, instance_id):
                state["publisher"] = None
            state = self._finalize_locked(state, now_dt)
            self._write_state_locked(state)
            return state

    def acquire_publisher_lease(
        self,
        *,
        runtime_kind: str,
        instance_id: str,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        now_dt = _coerce_datetime(now) or datetime.now(timezone.utc)
        consumer_key = _consumer_key(runtime_kind, instance_id)
        with locked_file(self.lock_path, "a+"):
            state = self._cleanup_expired_locked(self._load_state_locked(), now_dt)
            consumers = dict(state.get("consumers") or {})
            consumer = dict(consumers.get(consumer_key) or {})
            if not consumer:
                raise KeyError(f"shared market consumer is not attached: {consumer_key}")
            consumer["last_heartbeat_at"] = _isoformat(now_dt)
            current_publisher = state.get("publisher")
            if self._publisher_matches(current_publisher, runtime_kind, instance_id) or not self._healthy_publisher(
                current_publisher,
                consumers,
                now_dt,
            ):
                state = self._clear_reacquire_requirement(consumer, state)
            consumers[consumer_key] = consumer
            state["consumers"] = consumers
            if self._publisher_matches(state.get("publisher"), runtime_kind, instance_id):
                publisher = dict(state.get("publisher") or {})
                publisher["last_heartbeat_at"] = _isoformat(now_dt)
                state["publisher"] = publisher
            state = self._finalize_locked(state, now_dt)
            self._write_state_locked(state)
            return state

    def renew_publisher_lease(
        self,
        *,
        runtime_kind: str,
        instance_id: str,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        now_dt = _coerce_datetime(now) or datetime.now(timezone.utc)
        consumer_key = _consumer_key(runtime_kind, instance_id)
        with locked_file(self.lock_path, "a+"):
            state = self._cleanup_expired_locked(self._load_state_locked(), now_dt)
            if not self._publisher_matches(state.get("publisher"), runtime_kind, instance_id):
                state = self._finalize_locked(state, now_dt)
                self._write_state_locked(state)
                return state
            consumers = dict(state.get("consumers") or {})
            consumer = dict(consumers.get(consumer_key) or {})
            if not consumer:
                state["publisher"] = None
                state = self._finalize_locked(state, now_dt)
                self._write_state_locked(state)
                return state
            consumer["last_heartbeat_at"] = _isoformat(now_dt)
            consumers[consumer_key] = consumer
            state["consumers"] = consumers
            publisher = dict(state.get("publisher") or {})
            publisher["last_heartbeat_at"] = _isoformat(now_dt)
            state["publisher"] = publisher
            state = self._finalize_locked(state, now_dt)
            self._write_state_locked(state)
            return state

    def release_publisher_lease(
        self,
        *,
        runtime_kind: str,
        instance_id: str,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        now_dt = _coerce_datetime(now) or datetime.now(timezone.utc)
        with locked_file(self.lock_path, "a+"):
            state = self._cleanup_expired_locked(self._load_state_locked(), now_dt)
            if self._publisher_matches(state.get("publisher"), runtime_kind, instance_id):
                state = self._mark_consumer_requires_reacquire(state, runtime_kind, instance_id, now_dt)
                state["publisher"] = None
            state = self._finalize_locked(state, now_dt)
            self._write_state_locked(state)
            return state

    def cleanup_expired(self, *, now: str | datetime | None = None) -> dict[str, Any]:
        now_dt = _coerce_datetime(now) or datetime.now(timezone.utc)
        with locked_file(self.lock_path, "a+"):
            state = self._finalize_locked(self._cleanup_expired_locked(self._load_state_locked(), now_dt), now_dt)
            self._write_state_locked(state)
            return state

    def record_snapshot_metadata(
        self,
        snapshot_metadata: Mapping[str, Any],
        *,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        now_dt = _coerce_datetime(now) or datetime.now(timezone.utc)
        metadata = self._normalize_snapshot_metadata(snapshot_metadata)
        with locked_file(self.lock_path, "a+"):
            state = self._finalize_locked(self._cleanup_expired_locked(self._load_state_locked(), now_dt), now_dt)
            publisher = state.get("publisher")
            if not isinstance(publisher, Mapping):
                raise RuntimeError("cannot record snapshot metadata without an active publisher lease")
            if not self._publisher_matches(
                publisher,
                metadata.get("publisher_runtime"),
                metadata.get("publisher_instance_id"),
            ):
                raise ValueError(
                    "snapshot metadata publisher does not match active publisher lease: "
                    f"expected {_consumer_key(publisher.get('runtime_kind'), publisher.get('instance_id'))}, "
                    f"got {_consumer_key(metadata.get('publisher_runtime'), metadata.get('publisher_instance_id'))}"
                )
            state["latest_snapshot"] = metadata
            self._write_state_locked(state)
            atomic_write_json(self.latest_snapshot_path, metadata)
            return state

    def publisher_snapshot_due_for(
        self,
        *,
        runtime_kind: str,
        instance_id: str,
        now: str | datetime | None = None,
    ) -> bool:
        now_dt = _coerce_datetime(now) or datetime.now(timezone.utc)
        state = self.read_state(now=now_dt)
        if not self._publisher_matches(state.get("publisher"), runtime_kind, instance_id):
            return False
        return self._snapshot_due_for_state(state, now_dt)

    def _snapshot_due_for_state(self, state: Mapping[str, Any], now_dt: datetime) -> bool:
        snapshot = state.get("latest_snapshot")
        if not isinstance(snapshot, Mapping):
            return True
        publisher = state.get("publisher")
        if isinstance(publisher, Mapping) and (
            str(snapshot.get("publisher_runtime") or "") != str(publisher.get("runtime_kind") or "")
            or str(snapshot.get("publisher_instance_id") or "") != str(publisher.get("instance_id") or "")
        ):
            return True
        expires_dt = _snapshot_expires_at(snapshot)
        if expires_dt is not None and now_dt > expires_dt:
            return True
        published_dt = _coerce_datetime(snapshot.get("published_at")) or _coerce_datetime(snapshot.get("observed_at"))
        if published_dt is None:
            return True
        due_after = int(state.get("effective_interval_seconds") or self.default_interval_seconds)
        return (now_dt - published_dt).total_seconds() >= due_after

    def _normalize_desired_interval(self, desired_interval_seconds: int | None) -> int:
        desired = self.default_interval_seconds if desired_interval_seconds in (None, "") else int(desired_interval_seconds)
        desired = max(1, desired)
        return max(self.min_interval_seconds, desired)

    def _publisher_priority(self, runtime_kind: str, override: int | None) -> int:
        if override not in (None, ""):
            return int(override)
        priorities = dict(self.config.get("publisher_priority") or {})
        return int(priorities.get(str(runtime_kind or ""), 0))

    def _load_state_locked(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._new_state()
        try:
            payload = self.state_path.read_text(encoding="utf-8")
        except OSError:
            return self._new_state()
        if not payload.strip():
            return self._new_state()
        try:
            state = json.loads(payload)
        except Exception:
            return self._new_state()
        return self._normalize_state(state)

    def _write_state_locked(self, state: Mapping[str, Any]) -> None:
        atomic_write_json(self.state_path, state)

    def _new_state(self) -> dict[str, Any]:
        return {
            "schema_name": STATE_SCHEMA_NAME,
            "schema_version": STATE_SCHEMA_VERSION,
            "runtime_root": str(self.runtime_root),
            "default_interval_seconds": self.default_interval_seconds,
            "min_interval_seconds": self.min_interval_seconds,
            "publisher_lease_timeout_seconds": self.publisher_lease_timeout_seconds,
            "consumer_timeout_seconds": self.consumer_timeout_seconds,
            "stop_when_idle": self.stop_when_idle,
            "snapshot_ttl_seconds": self.snapshot_ttl_seconds,
            "effective_interval_seconds": self.default_interval_seconds,
            "idle": True,
            "consumers": {},
            "publisher_reacquire_guards": {},
            "publisher": None,
            "latest_snapshot": None,
            "updated_at": "",
        }

    def _normalize_state(self, state: Mapping[str, Any] | None) -> dict[str, Any]:
        normalized = self._new_state()
        if isinstance(state, Mapping):
            normalized.update({key: value for key, value in state.items() if key in normalized})
        consumers = normalized.get("consumers")
        normalized["consumers"] = dict(consumers) if isinstance(consumers, Mapping) else {}
        publisher_reacquire_guards = normalized.get("publisher_reacquire_guards")
        normalized["publisher_reacquire_guards"] = (
            {str(key): str(value) for key, value in publisher_reacquire_guards.items() if value not in (None, "")}
            if isinstance(publisher_reacquire_guards, Mapping)
            else {}
        )
        latest_snapshot = normalized.get("latest_snapshot")
        normalized["latest_snapshot"] = dict(latest_snapshot) if isinstance(latest_snapshot, Mapping) else None
        publisher = normalized.get("publisher")
        normalized["publisher"] = dict(publisher) if isinstance(publisher, Mapping) else None
        normalized["runtime_root"] = str(self.runtime_root)
        normalized["default_interval_seconds"] = self.default_interval_seconds
        normalized["min_interval_seconds"] = self.min_interval_seconds
        normalized["publisher_lease_timeout_seconds"] = self.publisher_lease_timeout_seconds
        normalized["consumer_timeout_seconds"] = self.consumer_timeout_seconds
        normalized["stop_when_idle"] = self.stop_when_idle
        normalized["snapshot_ttl_seconds"] = self.snapshot_ttl_seconds
        normalized["effective_interval_seconds"] = self._calculate_effective_interval_seconds(normalized)
        normalized["idle"] = not bool(normalized["consumers"])
        return normalized

    def _cleanup_expired_locked(self, state: Mapping[str, Any], now_dt: datetime) -> dict[str, Any]:
        normalized = self._normalize_state(state)
        consumers: dict[str, dict[str, Any]] = {}
        for consumer_key, consumer in dict(normalized.get("consumers") or {}).items():
            last_heartbeat_dt = _coerce_datetime(consumer.get("last_heartbeat_at"))
            if last_heartbeat_dt is None:
                continue
            if (now_dt - last_heartbeat_dt).total_seconds() > self.consumer_timeout_seconds:
                continue
            consumers[consumer_key] = dict(consumer)
        normalized["consumers"] = consumers
        publisher = normalized.get("publisher")
        if not self._healthy_publisher(publisher, consumers, now_dt):
            if isinstance(publisher, Mapping):
                normalized = self._mark_consumer_requires_reacquire(
                    normalized,
                    publisher.get("runtime_kind"),
                    publisher.get("instance_id"),
                    now_dt,
                )
            normalized["publisher"] = None
        return normalized

    def _finalize_locked(self, state: Mapping[str, Any], now_dt: datetime) -> dict[str, Any]:
        normalized = self._normalize_state(state)
        normalized["effective_interval_seconds"] = self._calculate_effective_interval_seconds(normalized)
        normalized["idle"] = not bool(normalized["consumers"])
        normalized["publisher"] = self._elect_publisher(normalized, now_dt)
        normalized["updated_at"] = _isoformat(now_dt)
        return normalized

    def _healthy_publisher(
        self,
        publisher: Mapping[str, Any] | None,
        consumers: Mapping[str, Mapping[str, Any]],
        now_dt: datetime,
    ) -> bool:
        if not isinstance(publisher, Mapping):
            return False
        consumer_key = _consumer_key(publisher.get("runtime_kind"), publisher.get("instance_id"))
        consumer = consumers.get(consumer_key)
        if not isinstance(consumer, Mapping):
            return False
        if not bool(consumer.get("can_publish")):
            return False
        last_heartbeat_dt = _coerce_datetime(publisher.get("last_heartbeat_at"))
        if last_heartbeat_dt is None:
            return False
        return (now_dt - last_heartbeat_dt).total_seconds() <= self.publisher_lease_timeout_seconds

    def _elect_publisher(self, state: Mapping[str, Any], now_dt: datetime) -> dict[str, Any] | None:
        consumers = dict(state.get("consumers") or {})
        current_publisher = state.get("publisher")
        if self.stop_when_idle and not consumers:
            return None
        if self._healthy_publisher(current_publisher, consumers, now_dt):
            return dict(current_publisher)

        eligible: list[dict[str, Any]] = []
        for consumer_key, consumer in consumers.items():
            if not self._consumer_can_acquire_publisher(consumer):
                continue
            eligible.append(
                {
                    "consumer_key": consumer_key,
                    "runtime_kind": str(consumer.get("runtime_kind") or ""),
                    "instance_id": str(consumer.get("instance_id") or ""),
                    "publisher_priority": int(consumer.get("publisher_priority") or 0),
                    "attached_at": str(consumer.get("attached_at") or ""),
                }
            )
        if not eligible:
            return None

        winner = sorted(
            eligible,
            key=lambda item: (
                -int(item["publisher_priority"]),
                item["attached_at"],
                item["instance_id"],
                item["runtime_kind"],
            ),
        )[0]
        return {
            "runtime_kind": winner["runtime_kind"],
            "instance_id": winner["instance_id"],
            "publisher_priority": winner["publisher_priority"],
            "lease_acquired_at": _isoformat(now_dt),
            "last_heartbeat_at": _isoformat(now_dt),
            "lease_timeout_seconds": self.publisher_lease_timeout_seconds,
        }

    def _consumer_can_acquire_publisher(self, consumer: Mapping[str, Any] | None) -> bool:
        if not isinstance(consumer, Mapping):
            return False
        if not bool(consumer.get("can_publish")):
            return False
        return consumer.get("publisher_reacquire_required_at") in (None, "")

    def _calculate_effective_interval_seconds(self, state: Mapping[str, Any]) -> int:
        intervals = [self.default_interval_seconds]
        for consumer in dict(state.get("consumers") or {}).values():
            desired_interval = _optional_positive_int(consumer.get("desired_interval_seconds"))
            if desired_interval is not None:
                intervals.append(desired_interval)
        return max(self.min_interval_seconds, min(intervals))

    def _publisher_matches(self, publisher: Mapping[str, Any] | None, runtime_kind: Any, instance_id: Any) -> bool:
        if not isinstance(publisher, Mapping):
            return False
        return (
            str(publisher.get("runtime_kind") or "") == str(runtime_kind or "")
            and str(publisher.get("instance_id") or "") == str(instance_id or "")
        )

    def _normalize_snapshot_metadata(self, snapshot_metadata: Mapping[str, Any]) -> dict[str, Any]:
        observed_at = snapshot_metadata.get("observed_at")
        publisher_runtime = snapshot_metadata.get("publisher_runtime")
        publisher_instance_id = snapshot_metadata.get("publisher_instance_id")
        if observed_at in (None, ""):
            raise ValueError("snapshot metadata missing observed_at")
        if publisher_runtime in (None, "") or publisher_instance_id in (None, ""):
            raise ValueError("snapshot metadata missing publisher identity")
        ttl_seconds = snapshot_metadata.get("ttl_seconds", self.snapshot_ttl_seconds)
        return build_shared_market_snapshot_metadata(
            snapshot_id=str(snapshot_metadata.get("snapshot_id") or ""),
            observed_at=observed_at,
            published_at=snapshot_metadata.get("published_at"),
            publisher_runtime=str(publisher_runtime),
            publisher_instance_id=str(publisher_instance_id),
            candidate_count=int(snapshot_metadata.get("candidate_count") or 0),
            ttl_seconds=int(ttl_seconds) if ttl_seconds not in (None, "") else None,
            source_exchange=snapshot_metadata.get("source_exchange"),
            market_count=int(snapshot_metadata.get("market_count") or snapshot_metadata.get("candidate_count") or 0),
        )

    def _mark_consumer_requires_reacquire(
        self,
        state: Mapping[str, Any],
        runtime_kind: Any,
        instance_id: Any,
        now_dt: datetime,
    ) -> dict[str, Any]:
        normalized = self._normalize_state(state)
        consumer_key = _consumer_key(runtime_kind, instance_id)
        reacquire_required_at = _isoformat(now_dt)
        publisher_reacquire_guards = dict(normalized.get("publisher_reacquire_guards") or {})
        publisher_reacquire_guards[consumer_key] = reacquire_required_at
        normalized["publisher_reacquire_guards"] = publisher_reacquire_guards
        consumers = dict(normalized.get("consumers") or {})
        consumer = dict(consumers.get(consumer_key) or {})
        if consumer:
            consumer["publisher_reacquire_required_at"] = reacquire_required_at
            consumers[consumer_key] = consumer
            normalized["consumers"] = consumers
        return normalized

    def _clear_reacquire_requirement(self, consumer: dict[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
        consumer.pop("publisher_reacquire_required_at", None)
        normalized = self._normalize_state(state)
        consumer_key = _consumer_key(consumer.get("runtime_kind"), consumer.get("instance_id"))
        publisher_reacquire_guards = dict(normalized.get("publisher_reacquire_guards") or {})
        publisher_reacquire_guards.pop(consumer_key, None)
        normalized["publisher_reacquire_guards"] = publisher_reacquire_guards
        return normalized


def _consumer_key(runtime_kind: Any, instance_id: Any) -> str:
    return f"{str(runtime_kind or '')}:{str(instance_id or '')}"


def _coerce_datetime(value: str | datetime | Any | None) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _isoformat(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return normalized.isoformat()


def _optional_positive_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return max(1, int(value))


def _snapshot_expires_at(snapshot_metadata: Mapping[str, Any] | None) -> datetime | None:
    if not isinstance(snapshot_metadata, Mapping):
        return None
    expires_dt = _coerce_datetime(snapshot_metadata.get("expires_at"))
    if expires_dt is not None:
        return expires_dt
    ttl_seconds = snapshot_metadata.get("ttl_seconds")
    observed_dt = _coerce_datetime(snapshot_metadata.get("observed_at")) or _coerce_datetime(snapshot_metadata.get("published_at"))
    if ttl_seconds in (None, "") or observed_dt is None:
        return None
    return observed_dt + timedelta(seconds=max(1, int(ttl_seconds)))
