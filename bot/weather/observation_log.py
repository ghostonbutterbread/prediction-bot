from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_IDENTICAL_COOLDOWN_SECONDS = 60 * 60


def _parse_ts(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


def _signature(record: dict[str, Any]) -> tuple[Any, ...]:
    return (
        record.get("kind"),
        record.get("market_id"),
        record.get("source_id"),
        record.get("city_id"),
    )


def observation_hash(record: dict[str, Any]) -> str:
    payload = {key: value for key, value in record.items() if key not in {"ts", "content_hash"}}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def normalize_observation(record: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(record)
    if not isinstance(normalized.get("ts"), str):
        raise ValueError("record.ts is required")
    if not isinstance(normalized.get("kind"), str):
        raise ValueError("record.kind is required")

    _parse_ts(normalized["ts"])
    normalized["content_hash"] = observation_hash(normalized)
    return normalized


def append_jsonl_compact(path: Path, record: dict[str, Any]) -> int:
    line = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.write("\n")
    return len(line) + 1


class ObservationLog:
    """
    Compact JSONL logger for market observations.

    The helper intentionally keeps dedupe state in memory and only scans the
    active log on startup, which avoids extra index files and keeps writes low.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        identical_cooldown_seconds: int = DEFAULT_IDENTICAL_COOLDOWN_SECONDS,
        archive_dir: str | Path | None = None,
    ):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.identical_cooldown_seconds = identical_cooldown_seconds
        self.archive_dir = Path(archive_dir) if archive_dir is not None else self.path.parent / "archive"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._last_seen: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._hydrate_state()

    def _hydrate_state(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        continue
                    if "content_hash" not in record:
                        record["content_hash"] = observation_hash(record)
                    self._last_seen[_signature(record)] = {
                        "ts": record.get("ts"),
                        "content_hash": record.get("content_hash"),
                    }
                except (ValueError, json.JSONDecodeError):
                    continue

    def should_log_observation(self, record: dict[str, Any]) -> bool:
        last_record = self._last_seen.get(_signature(record))
        if last_record is None:
            return True
        if last_record.get("content_hash") != record["content_hash"]:
            return True
        try:
            last_ts = _parse_ts(last_record["ts"])
            current_ts = _parse_ts(record["ts"])
        except (KeyError, TypeError, ValueError):
            return True
        elapsed = (current_ts - last_ts).total_seconds()
        return elapsed >= self.identical_cooldown_seconds

    def rotate_if_needed(self, incoming_bytes: int) -> Path | None:
        if not self.path.exists():
            return None
        current_size = self.path.stat().st_size
        if current_size + incoming_bytes <= self.max_bytes:
            return None

        self.archive_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive_path = self.archive_dir / f"{self.path.stem}_{timestamp}{self.path.suffix}"
        counter = 1
        while archive_path.exists():
            archive_path = self.archive_dir / f"{self.path.stem}_{timestamp}_{counter}{self.path.suffix}"
            counter += 1
        self.path.replace(archive_path)
        return archive_path

    def append(self, record: dict[str, Any]) -> bool:
        normalized = normalize_observation(record)
        if not self.should_log_observation(normalized):
            return False

        encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        self.rotate_if_needed(len(encoded) + 1)
        append_jsonl_compact(self.path, normalized)
        self._last_seen[_signature(normalized)] = {
            "ts": normalized["ts"],
            "content_hash": normalized["content_hash"],
        }
        return True
