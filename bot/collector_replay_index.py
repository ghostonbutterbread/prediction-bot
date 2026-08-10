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
from typing import Any, Collection, Iterator, Mapping

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
    source_path = Path(source_path).resolve()
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
            entry = _index_entry(row, row_number=row_number, byte_offset=byte_offset, payload=payload)
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
        "indexed_source_bytes": source_path.stat().st_size,
        "source_rows_seen": row_number,
        "source_sha256": source_digest.hexdigest(),
        "append_chain_base_sha256": source_digest.hexdigest(),
        "append_chain_sha256": source_digest.hexdigest(),
        "index_path": str(index_path),
        "indexed_rows": indexed_rows,
        "invalid_rows": invalid_rows,
        "non_mutating": True,
        "storage_contract": "raw_payload_remains_only_in_source_archive; index_contains_offsets_and_compact_replay_metadata",
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {**manifest, "manifest_path": str(manifest_path)}


def update_collector_replay_index(source_path: Path, index_path: Path, manifest_path: Path) -> dict[str, Any]:
    """Append locators for newly appended raw JSONL rows without rescanning payloads.

    The raw archive is append-only. Each index entry has its own payload digest,
    so replay validates selected source rows without making a second full archive
    copy or re-hashing gigabytes on every collector pass.
    """
    source_path = Path(source_path).resolve()
    index_path = Path(index_path)
    manifest_path = Path(manifest_path)
    if not index_path.exists() or not manifest_path.exists():
        return build_collector_replay_index(source_path, index_path, manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if Path(str(manifest.get("source_path") or "")) != source_path:
        raise ValueError("replay-index manifest refers to a different raw archive")
    start_offset = int(manifest.get("indexed_source_bytes") or 0)
    source_size = source_path.stat().st_size
    if source_size < start_offset:
        raise ValueError("raw collector archive was truncated; rebuild the replay index")
    row_number = int(manifest.get("source_rows_seen") or 0)
    last_complete_offset = start_offset
    chain = bytes.fromhex(str(manifest.get("append_chain_sha256") or manifest.get("source_sha256") or "00" * 32))
    new_indexed = new_invalid = 0
    with source_path.open("rb") as source, index_path.open("a", encoding="utf-8") as index:
        source.seek(start_offset)
        while True:
            byte_offset = source.tell()
            payload = source.readline()
            if not payload:
                break
            if not payload.endswith(b"\n"):
                # Leave an incomplete trailing record for the next pass.
                break
            last_complete_offset = source.tell()
            chain = hashlib.sha256(chain + hashlib.sha256(payload).digest()).digest()
            row_number += 1
            try:
                row = json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError):
                new_invalid += 1
                continue
            if not isinstance(row, Mapping):
                new_invalid += 1
                continue
            entry = _index_entry(row, row_number=row_number, byte_offset=byte_offset, payload=payload)
            if entry is None:
                new_invalid += 1
                continue
            index.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")
            new_indexed += 1
    final_source_size = source_path.stat().st_size
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    manifest["source_size_bytes"] = final_source_size
    manifest["indexed_source_bytes"] = last_complete_offset
    manifest["source_rows_seen"] = row_number
    manifest["indexed_rows"] = int(manifest.get("indexed_rows") or 0) + new_indexed
    manifest["invalid_rows"] = int(manifest.get("invalid_rows") or 0) + new_invalid
    manifest["append_chain_sha256"] = chain.hex()
    manifest["unindexed_trailing_bytes"] = source_size - last_complete_offset
    manifest["source_sha256"] = None
    manifest["source_integrity"] = "per_indexed_row_payload_sha256"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {**manifest, "new_indexed_rows": new_indexed, "new_invalid_rows": new_invalid, "manifest_path": str(manifest_path)}


def load_indexed_collector_rows(
    index_path: Path,
    manifest_path: Path,
    *,
    market_ids: Collection[str] | None = None,
    row_numbers: Collection[int] | None = None,
    max_rows: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield selected original rows after compact-index provenance checks.

    Filtering occurs on compact index metadata before the corresponding raw
    payload is read, so callers can hydrate a bounded replay without copying or
    scanning a second raw archive.
    """
    if max_rows is not None and max_rows <= 0:
        raise ValueError("max_rows must be positive")
    selected_market_ids = {str(market_id) for market_id in market_ids or () if str(market_id)}
    selected_row_numbers = {int(row_number) for row_number in row_numbers or () if int(row_number) > 0}
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if manifest.get("index_schema_name") != INDEX_SCHEMA_NAME:
        raise ValueError("unsupported replay index schema")
    source_path = Path(str(manifest.get("source_path") or ""))
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    yielded = 0
    with source_path.open("rb") as source, Path(index_path).open(encoding="utf-8") as index:
        for line in index:
            entry = json.loads(line)
            if entry.get("schema_name") != INDEX_SCHEMA_NAME:
                raise ValueError("invalid replay index row")
            if selected_market_ids and str(entry.get("market_id") or "") not in selected_market_ids:
                continue
            if selected_row_numbers and int(entry.get("row_number") or 0) not in selected_row_numbers:
                continue
            if max_rows is not None and yielded >= max_rows:
                break
            source.seek(int(entry["byte_offset"]))
            payload = source.read(int(entry["byte_length"]))
            if hashlib.sha256(payload).hexdigest() != entry.get("payload_sha256"):
                raise ValueError("raw collector payload differs from replay index")
            row = json.loads(payload)
            if str(row.get("market_id") or "") != entry.get("market_id"):
                raise ValueError("raw collector row does not match replay index market identity")
            yielded += 1
            yield dict(row)


def _index_entry(row: Mapping[str, Any], *, row_number: int, byte_offset: int, payload: bytes) -> dict[str, Any] | None:
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
        "byte_length": len(payload),
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
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
