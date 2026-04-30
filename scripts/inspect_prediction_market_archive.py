#!/usr/bin/env python3
"""Inspect the external Jon-Becker prediction-market-analysis archive.

This is a read-only verifier for the imported Parquet archive under:
  data/external/prediction_market_analysis/data

It checks that the archive contains the fields we need for training/replay,
including market pricing, trade pricing, timestamps, and outcomes where available.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import duckdb
except ModuleNotFoundError as exc:  # pragma: no cover - exercised in environments without optional deps
    raise SystemExit("Archive inspection requires duckdb. Install project requirements first.") from exc

DEFAULT_ARCHIVE_DIR = ROOT / "data" / "external" / "prediction_market_analysis" / "data"


def _files(path: Path, pattern: str) -> list[Path]:
    """Return real parquet/data files, excluding macOS resource-fork sidecars."""
    return sorted(p for p in path.glob(pattern) if not p.name.startswith("._"))


def _glob(path: Path, pattern: str) -> str:
    # DuckDB globs would include macOS AppleDouble sidecars (._*.parquet) from the
    # upstream tarball, so pass an explicit file list instead.
    encoded = json.dumps([str(p) for p in _files(path, pattern)])
    return f"read_parquet({encoded})"


def _exists(path: Path, pattern: str) -> bool:
    return bool(_files(path, pattern))


def _query_one(con: duckdb.DuckDBPyConnection, sql: str) -> dict[str, Any]:
    rows = con.execute(sql).fetchdf().to_dict(orient="records")
    return rows[0] if rows else {}


def inspect_archive(archive_dir: Path = DEFAULT_ARCHIVE_DIR) -> dict[str, Any]:
    con = duckdb.connect()
    out: dict[str, Any] = {
        "archive_dir": str(archive_dir),
        "exists": archive_dir.exists(),
        "datasets": {},
    }

    datasets = {
        "kalshi_markets": (archive_dir / "kalshi" / "markets", "*.parquet"),
        "kalshi_trades": (archive_dir / "kalshi" / "trades", "*.parquet"),
        "polymarket_markets": (archive_dir / "polymarket" / "markets", "*.parquet"),
        "polymarket_trades": (archive_dir / "polymarket" / "trades", "*.parquet"),
        "polymarket_blocks": (archive_dir / "polymarket" / "blocks", "*.parquet"),
        "polymarket_fpmm_trades": (archive_dir / "polymarket" / "legacy_trades", "*.parquet"),
    }

    for name, (directory, pattern) in datasets.items():
        entry: dict[str, Any] = {
            "path": str(directory),
            "file_count": len(_files(directory, pattern)) if directory.exists() else 0,
            "exists": _exists(directory, pattern),
        }
        if not entry["exists"]:
            out["datasets"][name] = entry
            continue

        glob_path = _glob(directory, pattern)
        entry["columns"] = [row[1] for row in con.execute(f"DESCRIBE SELECT * FROM {glob_path} LIMIT 1").fetchall()]
        entry["row_count"] = int(con.execute(f"SELECT COUNT(*) FROM {glob_path}").fetchone()[0])

        if name == "kalshi_markets":
            entry.update(_query_one(con, f"""
                SELECT
                  COUNT(*) AS rows,
                  COUNT(*) FILTER (WHERE yes_bid IS NOT NULL OR yes_ask IS NOT NULL OR no_bid IS NOT NULL OR no_ask IS NOT NULL OR last_price IS NOT NULL) AS rows_with_quote_or_last,
                  COUNT(*) FILTER (WHERE result IN ('yes','no')) AS resolved_rows,
                  MIN(created_time) AS min_created_time,
                  MAX(created_time) AS max_created_time,
                  MIN(close_time) AS min_close_time,
                  MAX(close_time) AS max_close_time
                FROM {glob_path}
            """))
        elif name == "kalshi_trades":
            entry.update(_query_one(con, f"""
                SELECT
                  COUNT(*) AS rows,
                  COUNT(*) FILTER (WHERE yes_price BETWEEN 1 AND 99 AND no_price BETWEEN 1 AND 99) AS rows_with_trade_prices,
                  MIN(created_time) AS min_trade_time,
                  MAX(created_time) AS max_trade_time,
                  SUM(count) AS total_contracts
                FROM {glob_path}
            """))
        elif name == "polymarket_markets":
            entry.update(_query_one(con, f"""
                SELECT
                  COUNT(*) AS rows,
                  COUNT(*) FILTER (WHERE outcome_prices IS NOT NULL AND outcome_prices != '') AS rows_with_outcome_prices,
                  COUNT(*) FILTER (WHERE closed) AS closed_rows,
                  MIN(created_at) AS min_created_at,
                  MAX(created_at) AS max_created_at,
                  MIN(end_date) AS min_end_date,
                  MAX(end_date) AS max_end_date
                FROM {glob_path}
            """))
        elif name in {"polymarket_trades", "polymarket_fpmm_trades"}:
            # Polymarket CTF/FPMM prices are derivable from asset amounts, not stored as one price column.
            entry["pricing_note"] = "Prices are derivable from maker/taker or collateral/outcome token amounts."

        out["datasets"][name] = entry

    return out


def main() -> int:
    summary = inspect_archive()
    print(json.dumps(summary, indent=2, default=str))
    return 0 if summary.get("exists") else 1


if __name__ == "__main__":
    raise SystemExit(main())
