#!/usr/bin/env python3
"""Safely partition JSONL files into monthly shards."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DATE_FIELDS = (
    "observed_at",
    "timestamp",
    "decided_at",
    "created_at",
    "recorded_at",
    "started_at",
    "finished_at",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_month(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return f"{parsed.year:04d}-{parsed.month:02d}"


def _row_month(row: dict[str, Any]) -> tuple[str, str | None]:
    for field in DATE_FIELDS:
        month = _parse_month(row.get(field))
        if month is not None:
            return month, field
    return "unknown", None


def _default_output_dir(source: Path) -> Path:
    return source.parent / "monthly" / source.stem


def _shard_filename(source: Path, shard: str, *, compress: bool) -> str:
    suffix = ".jsonl.gz" if compress else ".jsonl"
    return f"{source.stem}-{shard}{suffix}"


def _manifest_path(source: Path, output_dir: Path) -> Path:
    return output_dir / f"{source.stem}-partition-manifest.json"


def _atomic_write_text(path: Path, text: str, *, force: bool) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"refusing to overwrite existing output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


class _ShardWriter:
    """Incrementally write shard temp files, then atomically promote them."""

    def __init__(self, output_dir: Path, source_path: Path, *, compress: bool) -> None:
        self.output_dir = output_dir
        self.source_path = source_path
        self.compress = compress
        self._files: dict[str, Any] = {}
        self._raw_files: dict[str, Any] = {}
        self._tmp_paths: dict[str, Path] = {}

    def write(self, shard: str, line: str) -> None:
        if shard not in self._files:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            final_name = _shard_filename(self.source_path, shard, compress=self.compress)
            fd, tmp_path = tempfile.mkstemp(prefix=f".{final_name}.", dir=str(self.output_dir))
            self._tmp_paths[shard] = Path(tmp_path)
            if self.compress:
                raw_fh = os.fdopen(fd, "wb")
                self._raw_files[shard] = raw_fh
                self._files[shard] = gzip.GzipFile(fileobj=raw_fh, mode="wb")
            else:
                self._files[shard] = os.fdopen(fd, "w", encoding="utf-8")

        payload = line if line.endswith("\n") else line + "\n"
        if self.compress:
            self._files[shard].write(payload.encode("utf-8"))
        else:
            self._files[shard].write(payload)

    def close(self) -> None:
        for shard, fh in list(self._files.items()):
            fh.close()
            raw_fh = self._raw_files.get(shard)
            if raw_fh is not None:
                raw_fh.close()
        self._files.clear()
        self._raw_files.clear()

    def cleanup(self) -> None:
        self.close()
        for path in self._tmp_paths.values():
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def promote(self, *, force: bool) -> list[Path]:
        self.close()
        promoted: list[Path] = []
        try:
            for shard, tmp_path in sorted(self._tmp_paths.items()):
                final_path = self.output_dir / _shard_filename(self.source_path, shard, compress=self.compress)
                if final_path.exists() and not force:
                    raise FileExistsError(f"refusing to overwrite existing output: {final_path}")
                os.replace(tmp_path, final_path)
                promoted.append(final_path)
        except Exception:
            for path in promoted:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            raise
        return promoted


def partition_jsonl_by_month(
    source: str | Path,
    *,
    output_root: str | Path | None = None,
    write: bool = False,
    force: bool = False,
    max_rows: int | None = None,
    compress: bool = False,
) -> dict[str, Any]:
    source_path = Path(source)
    if max_rows is not None and max_rows < 0:
        raise ValueError("max_rows must be >= 0")
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    if not source_path.is_file():
        raise ValueError(f"source is not a file: {source_path}")

    output_dir = Path(output_root) if output_root is not None else _default_output_dir(source_path)
    stat = source_path.stat()
    started_at = _utc_now_iso()
    shard_counts: Counter[str] = Counter()
    date_field_usage_counts: Counter[str] = Counter()
    shard_writer = _ShardWriter(output_dir, source_path, compress=compress) if write else None
    lines_read = 0
    nonblank_rows_read = 0
    blank_lines = 0
    bad_json_rows = 0
    unknown_date_rows = 0
    truncated_by_max_rows = False

    try:
        with source_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if max_rows is not None and nonblank_rows_read >= max_rows:
                    truncated_by_max_rows = True
                    break
                lines_read += 1
                if not line.strip():
                    blank_lines += 1
                    continue
                nonblank_rows_read += 1
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    bad_json_rows += 1
                    continue
                if not isinstance(row, dict):
                    bad_json_rows += 1
                    continue

                shard, field = _row_month(row)
                if field is None:
                    unknown_date_rows += 1
                    date_field_usage_counts["unknown"] += 1
                else:
                    date_field_usage_counts[field] += 1
                shard_counts[shard] += 1
                if shard_writer is not None:
                    shard_writer.write(shard, line)
    except Exception:
        if shard_writer is not None:
            shard_writer.cleanup()
        raise

    shard_paths = {
        shard: str(output_dir / _shard_filename(source_path, shard, compress=compress))
        for shard in sorted(shard_counts)
    }
    manifest_path = _manifest_path(source_path, output_dir)
    manifest: dict[str, Any] = {
        "source_path": str(source_path),
        "source_size_bytes": stat.st_size,
        "source_mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_mtime_ns": stat.st_mtime_ns,
        "output_dir": str(output_dir),
        "manifest_path": str(manifest_path) if write else None,
        "dry_run": not write,
        "compress": compress,
        "force": force,
        "max_rows": max_rows,
        "truncated_by_max_rows": truncated_by_max_rows,
        "started_at": started_at,
        "finished_at": None,
        "lines_read": lines_read,
        "rows_read": nonblank_rows_read,
        "rows_partitioned": sum(shard_counts.values()),
        "blank_lines": blank_lines,
        "skipped_blank_lines": blank_lines,
        "bad_json_rows": bad_json_rows,
        "skipped_bad_json_rows": bad_json_rows,
        "unknown_date_rows": unknown_date_rows,
        "warnings_count": unknown_date_rows + bad_json_rows,
        "date_field_usage_counts": {
            field: date_field_usage_counts.get(field, 0)
            for field in (*DATE_FIELDS, "unknown")
        },
        "shard_counts": dict(sorted(shard_counts.items())),
        "shard_paths": shard_paths,
        "shards_written": False,
    }

    if write:
        existing_outputs = [Path(path) for path in shard_paths.values() if Path(path).exists()]
        if manifest_path.exists():
            existing_outputs.append(manifest_path)
        if existing_outputs and not force:
            if shard_writer is not None:
                shard_writer.cleanup()
            joined = ", ".join(str(path) for path in existing_outputs)
            raise FileExistsError(f"refusing to overwrite existing output(s): {joined}")

        written: list[Path] = []
        try:
            if shard_writer is not None:
                written = shard_writer.promote(force=force)
            manifest["finished_at"] = _utc_now_iso()
            manifest["shards_written"] = True
            _atomic_write_text(manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n", force=force)
        except Exception:
            if shard_writer is not None:
                shard_writer.cleanup()
            for path in written:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            raise
    else:
        manifest["finished_at"] = _utc_now_iso()

    return manifest


def partition_many(
    sources: list[str | Path],
    *,
    output_root: str | Path | None = None,
    write: bool = False,
    force: bool = False,
    max_rows: int | None = None,
    compress: bool = False,
) -> list[dict[str, Any]]:
    return [
        partition_jsonl_by_month(
            source,
            output_root=output_root,
            write=write,
            force=force,
            max_rows=max_rows,
            compress=compress,
        )
        for source in sources
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safely partition JSONL files into one-month shards")
    parser.add_argument("inputs", nargs="+", help="One or more JSONL files to scan or partition")
    parser.add_argument("--output-root", help="Directory for shard outputs; defaults to <file-parent>/monthly/<stem>/")
    parser.add_argument("--write", action="store_true", help="Write shard files and a manifest; default is dry-run")
    parser.add_argument("--force", action="store_true", help="Allow replacing existing shard outputs and manifests")
    parser.add_argument("--max-rows", type=int, default=None, help="Maximum nonblank input rows to scan")
    parser.add_argument("--compress", action="store_true", help="Write gzip-compressed .jsonl.gz shard files")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    reports = partition_many(
        args.inputs,
        output_root=args.output_root,
        write=args.write,
        force=args.force,
        max_rows=args.max_rows,
        compress=args.compress,
    )
    payload: dict[str, Any] | list[dict[str, Any]]
    payload = reports[0] if len(reports) == 1 else reports
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
