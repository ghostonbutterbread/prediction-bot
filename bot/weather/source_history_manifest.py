"""Validated, read-only registry entries for historical source evidence."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class SourceHistoryManifestError(ValueError):
    """A history manifest is malformed, unsafe, or no longer matches its evidence."""


@dataclass(frozen=True, slots=True)
class SourceHistoryManifest:
    manifest_path: Path
    source_ledger_path: Path
    strict_resolution_path: Path
    raw_archive_path: Path
    replay_index_path: Path
    replay_manifest_path: Path
    historical_counterfactual_only: bool
    non_mutating: bool
    join_key: str
    eligibility_filter: str
    availability_field: str
    sha256: Mapping[str, str]


_REQUIRED_PATHS = {
    "source_ledger": "source_ledger_path",
    "strict_resolution": "strict_resolution_path",
    "raw_archive": "raw_archive_path",
    "replay_index": "replay_index_path",
    "replay_manifest": "replay_manifest_path",
}


def load_source_history_manifest(path: str | Path) -> SourceHistoryManifest:
    """Load a paper-only historical evidence manifest after hash verification."""
    manifest_path = Path(path).expanduser().resolve()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SourceHistoryManifestError(f"history manifest does not exist: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise SourceHistoryManifestError(f"history manifest is not valid JSON: {manifest_path}") from exc
    if not isinstance(payload, Mapping):
        raise SourceHistoryManifestError("history manifest must be a JSON object")
    if payload.get("schema_name") != "source_history_manifest" or payload.get("schema_version") != 1:
        raise SourceHistoryManifestError("unsupported source history manifest schema")
    if payload.get("historical_counterfactual_only") is not True:
        raise SourceHistoryManifestError("history manifest must be historical_counterfactual_only")
    if payload.get("non_mutating") is not True:
        raise SourceHistoryManifestError("history manifest must be non_mutating")
    if payload.get("join_key") != "market_id":
        raise SourceHistoryManifestError("history manifest join_key must be market_id")
    if payload.get("availability_field") != "settlement_ts":
        raise SourceHistoryManifestError("history manifest availability_field must be settlement_ts")
    if not isinstance(payload.get("eligibility_filter"), str) or not payload["eligibility_filter"].strip():
        raise SourceHistoryManifestError("history manifest must declare an eligibility_filter")
    digests = payload.get("sha256")
    if not isinstance(digests, Mapping):
        raise SourceHistoryManifestError("history manifest sha256 must be an object")

    resolved_paths: dict[str, Path] = {}
    for digest_key, path_key in _REQUIRED_PATHS.items():
        raw_path = payload.get(path_key)
        expected = digests.get(digest_key)
        if not isinstance(raw_path, str) or not raw_path:
            raise SourceHistoryManifestError(f"history manifest missing {path_key}")
        if not isinstance(expected, str) or len(expected) != 64:
            raise SourceHistoryManifestError(f"history manifest missing sha256 for {digest_key}")
        source_path = Path(raw_path).expanduser()
        if not source_path.is_absolute():
            source_path = manifest_path.parent / source_path
        source_path = source_path.resolve()
        if not source_path.is_file():
            raise SourceHistoryManifestError(f"history evidence file does not exist: {digest_key}")
        actual = _sha256(source_path)
        if actual != expected.lower():
            raise SourceHistoryManifestError(f"sha256 mismatch for {digest_key}: expected {expected}, got {actual}")
        resolved_paths[digest_key] = source_path

    return SourceHistoryManifest(
        manifest_path=manifest_path,
        source_ledger_path=resolved_paths["source_ledger"],
        strict_resolution_path=resolved_paths["strict_resolution"],
        raw_archive_path=resolved_paths["raw_archive"],
        replay_index_path=resolved_paths["replay_index"],
        replay_manifest_path=resolved_paths["replay_manifest"],
        historical_counterfactual_only=True,
        non_mutating=True,
        join_key="market_id",
        eligibility_filter=payload["eligibility_filter"],
        availability_field="settlement_ts",
        sha256={key: str(value).lower() for key, value in digests.items()},
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
