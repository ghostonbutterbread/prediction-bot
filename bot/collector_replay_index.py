"""Compact, provenance-checked index for immutable collector JSONL archives.

The index deliberately stores only locator and replay-routing fields. The full
source/signal/quote payload remains in the raw collector archive and is loaded
on demand during a replay, avoiding a second large copy of collection data.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

INDEX_SCHEMA_NAME = "collector_replay_index"
INDEX_SCHEMA_VERSION = 1


def build_collector_replay_index(
    source_path: Path,
    index_path: Path,
    manifest_path: Path,
) -> dict[str, Any]:
    """Stream a raw JSONL archive into a compact byte-offset replay index.

    Neither the source archive nor its rows are changed. Existing outputs are
    rejected so a replay index is always attributable to one source digest.
    """
    source_path = Path(source_path)
    index_path = Path(index_path)
    manifest_path = Path(manifest_path)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if index_path.exists() or manifest_path.exists():
        raise FileExistsError("replay index outputs already exist")
    index_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    source_digest = hashlib.sha256()
    indexed_rows = invalid_rows = 0
    with source_path.open("rb") as source, index_path.open("w", encoding="utf-8") as index:
        row_number = 0
        while True:
            byte_offset = source.tell()
            payload = source.readline()
            if not payload:
                break
            source_digest.update(payload)
            row_number += 1
            try:
                row = json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError):
                invalid_rows += 1
                continue
            if not isinstance(row, Mapping):
                invalid_rows += 1
                continue
            entry = _index_entry(row, row_number=row_number, byte_offset=byte_offset, byte_length=len(payload))
            if entry is None:
                invalid_rows += 1
                continue
            index.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")
            indexed_rows += 1

    manifest = {
        "schema_name": "collector_replay_index_manifest",
        "schema_version": 1,
        "index_schema_name": INDEX_SCHEMA_NAME,
        "index_schema_version": INDEX_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_path": str(source_path),
        "source_size_bytes": source_path.stat().st_size,
        "source_sha256": source_digest.hexdigest(),
        "index_path": str(index_path),
        "indexed_rows": indexed_rows,
        "invalid_rows": invalid_rows,
        "non_mutating": True,
        "storage_contract": "raw_payload_remains_only_in_source_archive; index_contains_offsets_and_compact_replay_metadata",
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {**manifest, "manifest_path": str(manifest_path)}


def load_indexed_collector_rows(index_path: Path, manifest_path: Path) -> Iterator[dict[str, Any]]:
    """Yield original raw rows referenced by an index after provenance checks."""
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if manifest.get("index_schema_name") != INDEX_SCHEMA_NAME:
        raise ValueError("unsupported replay index schema")
    source_path = Path(str(manifest.get("source_path") or ""))
    if _sha256_file(source_path) != manifest.get("source_sha256"):
        raise ValueError("raw collector archive digest differs from replay-index manifest")
    with source_path.open("rb") as source, Path(index_path).open(encoding="utf-8") as index:
        for line in index:
            entry = json.loads(line)
            if entry.get("schema_name") != INDEX_SCHEMA_NAME:
                raise ValueError("invalid replay index row")
            source.seek(int(entry["byte_offset"]))
            payload = source.read(int(entry["byte_length"]))
            row = json.loads(payload)
            if str(row.get("market_id") or "") != entry.get("market_id"):
                raise ValueError("raw collector row does not match replay index market identity")
            yield dict(row)


def _index_entry(row: Mapping[str, Any], *, row_number: int, byte_offset: int, byte_length: int) -> dict[str, Any] | None:
    market_id = str(row.get("market_id") or "")
    observed_at = str(row.get("observed_at") or row.get("timestamp") or "")
    snapshot_id = str(row.get("run_id") or row.get("snapshot_key") or "")
    if not market_id or not observed_at or not snapshot_id:
        return None
    shared_value = row.get("shared_candidate")
    shared: dict[str, Any] = {str(key): value for key, value in shared_value.items()} if isinstance(shared_value, Mapping) else {}
    candidate_id = str(row.get("shared_candidate_id") or shared.get("candidate_id") or f"{snapshot_id}:{market_id}")
    return {
        "schema_name": INDEX_SCHEMA_NAME,
        "schema_version": INDEX_SCHEMA_VERSION,
        "row_number": row_number,
        "byte_offset": byte_offset,
        "byte_length": byte_length,
        "shared_candidate_id": candidate_id,
        "shared_snapshot_id": snapshot_id,
        "market_id": market_id,
        "observed_at": observed_at,
        "yes_price": row.get("yes_price"),
        "no_price": row.get("no_price"),
        "confidence": row.get("confidence"),
        "edge": row.get("edge"),
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
