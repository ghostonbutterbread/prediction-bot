"""Offline export of immutable collector JSONL into sanitized replay inputs.

This is deliberately a narrow collector-to-replay bridge.  It does not select
lanes, calculate a strategy, size a position, resolve outcomes, or contact any
network service.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from bot.collector_paths import derived_reports_root
from bot.replay_decision_input import (
    REPLAY_DECISION_INPUT_SCHEMA_NAME,
    REPLAY_DECISION_INPUT_SCHEMA_VERSION,
    build_replay_decision_input_v1,
)
RECORDS_FILENAME = "replay_decision_inputs.jsonl"
METADATA_FILENAME = "run_metadata.json"
_REVERSE_READ_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class CollectorReplayInputExportResult:
    """Locations and summary for one immutable-archive export."""

    output_dir: Path
    records_path: Path
    metadata_path: Path
    metadata: dict[str, Any]


def export_collector_replay_inputs(
    *,
    source_archive: str | Path,
    output_dir: str | Path,
    max_rows: int | None = None,
    accepted_limit: int | None = None,
) -> CollectorReplayInputExportResult:
    """Export successful default-sanitized replay inputs from collector JSONL.

    ``max_rows`` selects that many non-empty archive rows from the beginning.
    ``accepted_limit`` instead scans backwards from the newest non-empty row
    until that many rows have been successfully sanitized.  Every inspected
    JSON row is passed once to ``build_replay_decision_input_v1`` using its
    default legacy-compatible sanitized mode.  Source order and reobservations
    are retained exactly; no rows are deduplicated.
    """
    if max_rows is not None and max_rows < 0:
        raise ValueError("max_rows must be non-negative")
    if accepted_limit is not None and accepted_limit <= 0:
        raise ValueError("accepted_limit must be positive")
    if max_rows is not None and accepted_limit is not None:
        raise ValueError("max_rows and accepted_limit are mutually exclusive")

    archive_path = Path(source_archive).resolve()
    if not archive_path.is_file():
        raise ValueError(f"collector archive must be a readable file: {archive_path}")
    target_dir = _prepare_output_dir(output_dir)

    selected_row_count = 0
    accepted_records: list[dict[str, Any]] = []
    rejection_counts: Counter[tuple[str, str]] = Counter()
    input_modes: Counter[str] = Counter()
    selection_mode = "newest_accepted" if accepted_limit is not None else "archive_order"
    line_iterator = (
        _iter_nonempty_lines_reverse(archive_path)
        if accepted_limit is not None
        else _iter_nonempty_lines(archive_path)
    )
    for line_number, raw_line in line_iterator:
        if max_rows is not None and selected_row_count >= max_rows:
            break
        selected_row_count += 1
        try:
            row = json.loads(raw_line)
        except json.JSONDecodeError:
            rejection_counts[("invalid_json", f"line[{line_number}]")] += 1
            continue

        # Keep this as the only builder call for each selected collector row.
        result = build_replay_decision_input_v1(row)
        if result.ok:
            assert result.record is not None
            accepted_records.append(result.record)
            mode = result.record.get("input_mode")
            if isinstance(mode, str):
                input_modes[mode] += 1
            if accepted_limit is not None and len(accepted_records) >= accepted_limit:
                break
            continue
        if result.errors:
            for error in result.errors:
                rejection_counts[(error.code, error.path)] += 1
        else:  # Defensive accounting if a future builder returns an empty failure.
            rejection_counts[("unknown_replay_input_rejection", "$")] += 1

    if accepted_limit is not None:
        accepted_records.reverse()

    records_path = target_dir / RECORDS_FILENAME
    records_bytes = b"".join(_canonical_json_line(record) for record in accepted_records)
    records_path.write_bytes(records_bytes)

    rejected_row_count = selected_row_count - len(accepted_records)
    metadata = {
        "schema_name": "collector_replay_input_export",
        "schema_version": 1,
        "mode": "offline_collector_to_sanitized_replay_inputs",
        "offline": True,
        "non_mutating": True,
        "network_access": False,
        "research_status": "research_only",
        "for_forward_paper_validation_only": False,
        "source_archive": {
            "path": str(archive_path),
            "sha256": _sha256_file(archive_path),
        },
        "max_rows": max_rows,
        "accepted_limit": accepted_limit,
        "selection_mode": selection_mode,
        "selected_row_count": selected_row_count,
        "inspected_row_count": selected_row_count,
        "accepted_row_count": len(accepted_records),
        "rejected_row_count": rejected_row_count,
        "rejection_counts": [
            {"code": code, "path": path, "count": count}
            for (code, path), count in sorted(rejection_counts.items())
        ],
        "replay_input": {
            "schema_name": REPLAY_DECISION_INPUT_SCHEMA_NAME,
            "schema_version": REPLAY_DECISION_INPUT_SCHEMA_VERSION,
            "input_modes": dict(sorted(input_modes.items())),
        },
        "output_artifacts": {
            RECORDS_FILENAME: {
                "sha256": hashlib.sha256(records_bytes).hexdigest(),
                "record_count": len(accepted_records),
            },
        },
    }
    metadata_path = target_dir / METADATA_FILENAME
    metadata_path.write_bytes(_canonical_json_line(metadata))
    return CollectorReplayInputExportResult(
        output_dir=target_dir,
        records_path=records_path,
        metadata_path=metadata_path,
        metadata=metadata,
    )


def _prepare_output_dir(value: str | Path) -> Path:
    target_dir = Path(value).resolve()
    derived_root = derived_reports_root().resolve()
    if target_dir == derived_root or derived_root not in target_dir.parents:
        raise ValueError(f"output directory must be under {derived_root}")
    if target_dir.exists():
        if not target_dir.is_dir() or any(target_dir.iterdir()):
            raise ValueError("output directory must be new or empty; refusing to overwrite artifacts")
    else:
        target_dir.mkdir(parents=True, exist_ok=False)
    return target_dir


def _iter_nonempty_lines(path: Path) -> Iterator[tuple[int, str]]:
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if line.strip():
                yield line_number, line


def _iter_nonempty_lines_reverse(path: Path) -> Iterator[tuple[int, str]]:
    """Yield non-empty JSONL rows from newest to oldest without whole-file reads."""
    line_number = _physical_line_count(path)
    if line_number == 0:
        return

    with path.open("rb") as stream:
        stream.seek(0, 2)
        position = stream.tell()
        ends_with_newline = False
        if position:
            stream.seek(-1, 2)
            ends_with_newline = stream.read(1) == b"\n"

        pending = b""
        first_chunk = True
        while position:
            chunk_size = min(_REVERSE_READ_CHUNK_SIZE, position)
            position -= chunk_size
            stream.seek(position)
            pending = stream.read(chunk_size) + pending
            pieces = pending.split(b"\n")
            pending = pieces[0]
            complete_lines = pieces[1:]
            if first_chunk and ends_with_newline:
                complete_lines.pop()
            first_chunk = False
            for raw_line in reversed(complete_lines):
                if raw_line.strip():
                    yield line_number, raw_line.decode("utf-8")
                line_number -= 1

        if pending.strip():
            yield line_number, pending.decode("utf-8")


def _physical_line_count(path: Path) -> int:
    """Count physical lines in bounded chunks for reverse JSONL line numbers."""
    newline_count = 0
    last_byte = b""
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_REVERSE_READ_CHUNK_SIZE), b""):
            newline_count += chunk.count(b"\n")
            last_byte = chunk[-1:]
    if last_byte and last_byte != b"\n":
        return newline_count + 1
    return newline_count


def _canonical_json_line(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
