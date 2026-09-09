"""Compact, provenance-checked index for immutable collector JSONL archives.

The index deliberately stores only locator and replay-routing fields. The full
source/signal/quote payload remains in the raw collector archive and is loaded
on demand during a replay, avoiding a second large copy of collection data.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Collection, Iterator, Mapping

from bot.shared_market_feed import shared_candidate_identity_mismatch
from bot.file_ops import atomic_write_json, locked_file

INDEX_SCHEMA_NAME = "collector_replay_index"
INDEX_SCHEMA_VERSION = 1


def build_collector_replay_index(
    source_path: Path, index_path: Path, manifest_path: Path,
) -> dict[str, Any]:
    """Create a collector-owned committed-prefix index without copying raw rows."""
    with locked_file(Path(str(manifest_path) + ".lock"), "a+"):
        return _build_collector_replay_index(source_path, index_path, manifest_path)


def _build_collector_replay_index(
    source_path: Path, index_path: Path, manifest_path: Path,
) -> dict[str, Any]:
    source_path = Path(source_path).resolve()
    index_path, manifest_path = Path(index_path), Path(manifest_path)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if index_path.exists() or manifest_path.exists():
        raise FileExistsError("replay index outputs already exist")
    index_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with index_path.open("xb") as index:
        index.flush()
        os.fsync(index.fileno())
    empty_digest = hashlib.sha256(b"").hexdigest()
    source_stat = source_path.stat()
    manifest = {
        "schema_name": "collector_replay_index_manifest",
        "schema_version": 2,
        "index_schema_name": INDEX_SCHEMA_NAME,
        "index_schema_version": INDEX_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_path": str(source_path),
        "archive_identity": _archive_identity(source_stat),
        "source_size_bytes": source_stat.st_size,
        "indexed_source_bytes": 0,
        "unindexed_trailing_bytes": source_stat.st_size,
        "source_rows_seen": 0,
        "source_sha256": empty_digest,
        "append_chain_base_sha256": empty_digest,
        "append_chain_sha256": empty_digest,
        "index_path": str(index_path.resolve()),
        "committed_index_bytes": 0,
        "index_sha256": empty_digest,
        "indexed_rows": 0,
        "invalid_rows": 0,
        "non_mutating": True,
        "storage_contract": "raw_payload_remains_only_in_source_archive; index_contains_offsets_and_compact_replay_metadata",
    }
    try:
        # A failed first scan still has a valid empty checkpoint to retry from.
        atomic_write_json(manifest_path, manifest)
    except BaseException:
        # No bytes or published references exist yet; remove only our empty file.
        index_path.unlink()
        raise
    return _update_collector_replay_index(source_path, index_path, manifest_path)


def update_collector_replay_index(source_path: Path, index_path: Path, manifest_path: Path) -> dict[str, Any]:
    """Serialize collector-owned updates; readers consume committed prefixes."""
    with locked_file(Path(str(manifest_path) + ".lock"), "a+"):
        return _update_collector_replay_index(source_path, index_path, manifest_path)


def _update_collector_replay_index(source_path: Path, index_path: Path, manifest_path: Path) -> dict[str, Any]:
    """Append locators for newly appended raw JSONL rows without rescanning payloads.

    The raw archive is append-only. Each index entry has its own payload digest,
    so replay validates selected source rows without making a second full archive
    copy or re-hashing gigabytes on every collector pass.
    """
    source_path = Path(source_path).resolve()
    index_path = Path(index_path)
    manifest_path = Path(manifest_path)
    if not manifest_path.exists() and index_path.is_file() and index_path.stat().st_size == 0:
        if index_path.resolve() != source_path:
            # Process death before the first empty manifest was published.
            # No committed bytes exist; never remove a raw input or nonempty index.
            index_path.unlink()
    if not index_path.exists() or not manifest_path.exists():
        return _build_collector_replay_index(source_path, index_path, manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _validate_manifest(manifest)
    if manifest["schema_version"] != 2:
        raise ValueError("legacy replay index requires an explicit offline checkpoint migration")
    _verify_index_path(index_path, manifest)
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
    source_digest = hashlib.sha256() if start_offset == 0 else None
    with source_path.open("rb") as source, index_path.open("r+b") as index:
        _verify_archive(source, manifest)
        _verify_index_extent(index, manifest)
        index.seek(manifest["committed_index_bytes"])
        # Discard only uncommitted derived suffix from an interrupted publish.
        index.truncate()
        source.seek(start_offset)
        while source.tell() < source_size:
            byte_offset = source.tell()
            payload = source.readline(source_size - byte_offset)
            if not payload:
                break
            if not payload.endswith(b"\n"):
                # Leave an incomplete trailing record for the next pass.
                break
            last_complete_offset = source.tell()
            if source_digest is not None:
                source_digest.update(payload)
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
            index.write((json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n").encode())
            new_indexed += 1
        index.flush()
        os.fsync(index.fileno())
    final_source_size = source_path.stat().st_size
    if last_complete_offset == start_offset and final_source_size == manifest["source_size_bytes"]:
        return {**manifest, "new_indexed_rows": 0, "new_invalid_rows": 0, "manifest_path": str(manifest_path)}
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    manifest["source_size_bytes"] = final_source_size
    manifest["indexed_source_bytes"] = last_complete_offset
    manifest["source_rows_seen"] = row_number
    manifest["indexed_rows"] = int(manifest.get("indexed_rows") or 0) + new_indexed
    manifest["invalid_rows"] = int(manifest.get("invalid_rows") or 0) + new_invalid
    manifest["append_chain_sha256"] = chain.hex()
    manifest["unindexed_trailing_bytes"] = final_source_size - last_complete_offset
    if last_complete_offset != start_offset:
        manifest["source_sha256"] = source_digest.hexdigest() if source_digest is not None else None
    manifest["source_integrity"] = "per_indexed_row_payload_sha256"
    manifest["committed_index_bytes"] = index_path.stat().st_size
    manifest["index_sha256"] = _sha256_file(index_path)
    atomic_write_json(manifest_path, manifest)
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
    _validate_manifest(manifest)
    source_path = Path(str(manifest.get("source_path") or ""))
    if manifest["schema_version"] == 2:
        _verify_index_path(Path(index_path), manifest)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    yielded = 0
    with source_path.open("rb") as source, Path(index_path).open("rb") as index:
        extent = manifest.get("committed_index_bytes")
        if manifest.get("schema_version") == 2:
            _verify_archive(source, manifest)
            _verify_index_extent(index, manifest)
        while extent is None or index.tell() < extent:
            line = index.readline() if extent is None else index.readline(extent - index.tell())
            if not line:
                break
            entry = json.loads(line)
            if entry.get("schema_name") != INDEX_SCHEMA_NAME:
                raise ValueError("invalid replay index row")
            if manifest["schema_version"] == 2:
                _validate_locator(entry, manifest)
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
            expected = _index_entry(row, row_number=entry["row_number"], byte_offset=entry["byte_offset"], payload=payload)
            identity_fields = ("market_id", "shared_candidate_id", "shared_snapshot_id", "observed_at")
            if expected is None or any(expected[key] != entry.get(key) for key in identity_fields):
                raise ValueError("raw collector row does not match replay index row identity")
            yielded += 1
            yield dict(row)


def load_indexed_collector_rows_reverse(
    index_path: Path, manifest_path: Path, *, max_rows: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield newest committed raw rows first without materializing the archive.

    Archive row number/locator order, rather than a producer timestamp, defines
    newest.  The entire compact checkpoint is verified before the first raw
    hydration so a checksum-consistent malformed index cannot influence a
    bounded newest-row query.
    """
    if max_rows is not None and max_rows <= 0:
        raise ValueError("max_rows must be positive")
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    _validate_manifest(manifest)
    if manifest.get("schema_version") != 2:
        raise ValueError("legacy replay index does not support reverse committed reads")
    checked_index = Path(index_path)
    _verify_index_path(checked_index, manifest)
    source_path = Path(str(manifest.get("source_path") or ""))
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    yielded = 0
    with source_path.open("rb") as source, checked_index.open("rb") as index:
        _verify_archive(source, manifest)
        _verify_index_extent(index, manifest)
        for line in _iter_committed_index_lines_reverse(index, int(manifest["committed_index_bytes"])):
            entry = json.loads(line)
            # _verify_index_extent already performed structural validation; keep
            # this local guard for future callers/refactors of the iterator.
            _validate_locator(entry, manifest)
            source.seek(int(entry["byte_offset"]))
            payload = source.read(int(entry["byte_length"]))
            if hashlib.sha256(payload).hexdigest() != entry.get("payload_sha256"):
                raise ValueError("raw collector payload differs from replay index")
            row = json.loads(payload)
            expected = _index_entry(row, row_number=entry["row_number"], byte_offset=entry["byte_offset"], payload=payload)
            identity_fields = ("market_id", "shared_candidate_id", "shared_snapshot_id", "observed_at")
            if expected is None or any(expected[key] != entry.get(key) for key in identity_fields):
                raise ValueError("raw collector row does not match replay index row identity")
            yield dict(row)
            yielded += 1
            if max_rows is not None and yielded >= max_rows:
                return


def _iter_committed_index_lines_reverse(index: Any, extent: int) -> Iterator[bytes]:
    """Yield newline-terminated compact index rows backwards within an extent."""
    position, trailing = extent, b""
    while position:
        size = min(1024 * 1024, position)
        position -= size
        index.seek(position)
        block = index.read(size)
        parts = block.split(b"\n")
        if trailing:
            parts[-1] += trailing
        trailing = parts[0]
        for line in reversed(parts[1:]):
            if line:
                yield line + b"\n"
    if trailing:
        yield trailing + b"\n"


def _index_entry(row: Mapping[str, Any], *, row_number: int, byte_offset: int, payload: bytes) -> dict[str, Any] | None:
    if shared_candidate_identity_mismatch(row):
        return None
    market_id = str(row.get("market_id") or "")
    observed_at = str(row.get("observed_at") or row.get("timestamp") or "")
    snapshot_id = str(row.get("shared_snapshot_id") or row.get("run_id") or row.get("snapshot_key") or "")
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


def _verify_index_extent(index: Any, manifest: Mapping[str, Any]) -> None:
    """Verify only the committed compact prefix, never the raw archive."""
    extent = manifest.get("committed_index_bytes")
    if type(extent) is not int or extent < 0:
        raise ValueError("invalid committed index extent")
    index.seek(0)
    remaining = extent
    digest = hashlib.sha256()
    while remaining:
        chunk = index.read(min(1024 * 1024, remaining))
        if not chunk:
            raise ValueError("committed replay index is truncated")
        digest.update(chunk)
        remaining -= len(chunk)
    if digest.hexdigest() != manifest.get("index_sha256"):
        raise ValueError("committed replay index digest mismatch")
    # Validate the whole compact sequence before any filtering, early return,
    # hydration, or update can consume a checksum-consistent malformed index.
    index.seek(0)
    previous_number = previous_end = 0
    while index.tell() < extent:
        line = index.readline(extent - index.tell())
        if not line or not line.endswith(b"\n"):
            raise ValueError("committed replay index ends in an incomplete row")
        entry = json.loads(line)
        if not isinstance(entry, Mapping) or entry.get("schema_name") != INDEX_SCHEMA_NAME:
            raise ValueError("invalid replay index row")
        _validate_locator(entry, manifest)
        if entry["row_number"] <= previous_number or entry["byte_offset"] < previous_end:
            raise ValueError("replay index locator order conflicts with committed row sequence")
        previous_number = entry["row_number"]
        previous_end = entry["byte_offset"] + entry["byte_length"]
    index.seek(0)


def _archive_identity(stat: os.stat_result) -> dict[str, int]:
    return {"device": stat.st_dev, "inode": stat.st_ino}


def _verify_index_path(index_path: Path, manifest: Mapping[str, Any]) -> None:
    if index_path.resolve() != Path(str(manifest.get("index_path") or "")):
        raise ValueError("replay index path differs from committed checkpoint")


def _validate_manifest(manifest: Any) -> None:
    if (not isinstance(manifest, dict)
            or manifest.get("schema_name") != "collector_replay_index_manifest"
            or manifest.get("schema_version") not in (1, 2)
            or manifest.get("index_schema_name") != INDEX_SCHEMA_NAME
            or manifest.get("index_schema_version") != INDEX_SCHEMA_VERSION):
        raise ValueError("unsupported replay index manifest schema")
    if manifest["schema_version"] == 2:
        for field in ("committed_index_bytes", "indexed_source_bytes", "source_rows_seen", "indexed_rows", "invalid_rows"):
            if type(manifest.get(field)) is not int or manifest[field] < 0:
                raise ValueError(f"invalid committed replay index field: {field}")


def _validate_locator(entry: Mapping[str, Any], manifest: Mapping[str, Any]) -> None:
    offset, length, number = (entry.get(key) for key in ("byte_offset", "byte_length", "row_number"))
    if (entry.get("schema_version") != INDEX_SCHEMA_VERSION
            or type(offset) is not int or type(length) is not int or type(number) is not int
            or offset < 0 or length <= 0 or number <= 0
            or number > manifest["source_rows_seen"]
            or offset + length > manifest["indexed_source_bytes"]):
        raise ValueError("replay index locator is outside committed extent")


def _verify_archive(source: Any, manifest: Mapping[str, Any]) -> None:
    stat = os.fstat(source.fileno())
    if _archive_identity(stat) != manifest.get("archive_identity"):
        raise ValueError("raw collector archive identity differs from committed checkpoint")
    if stat.st_size < manifest["indexed_source_bytes"]:
        raise ValueError("raw collector archive was truncated below committed extent")
