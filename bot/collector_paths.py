"""Canonical storage roots for collector evidence and runtime artifacts.

The code checkout may live on the host root filesystem while collector evidence
and runtime artifacts belong on the collector volume. Configured storage roots
and the environment override allow isolated tests and alternate data volumes.
"""
from __future__ import annotations

import os
from pathlib import Path


COLLECTOR_ROOT_ENV = "PREDICTION_BOT_COLLECTOR_ROOT"
DEFAULT_COLLECTOR_ROOT = Path("/mnt/data-collection/prediction-bot")


def collector_root(storage_root: str | Path | None = None) -> Path:
    """Select env > configured root > collector volume, at call time.

    Roots must be absolute (after expanding ~); accepting a relative root would
    reintroduce checkout/CWD-dependent storage. This does not create directories.
    """
    value = os.environ.get(COLLECTOR_ROOT_ENV) or storage_root or DEFAULT_COLLECTOR_ROOT
    root = Path(value).expanduser()
    if not root.is_absolute():
        raise ValueError("collector storage root must be absolute (runtime.storage_root or PREDICTION_BOT_COLLECTOR_ROOT)")
    return root.resolve()


def derived_reports_root(storage_root: str | Path | None = None) -> Path:
    """Return the collector-owned root for derived reports."""
    return collector_root(storage_root) / "data" / "derived_reports"


def auto_source_router_history_root(storage_root: str | Path | None = None) -> Path:
    """Return the canonical derived-only Source Router promotion root."""
    return derived_reports_root(storage_root) / "auto_source_router_history"
