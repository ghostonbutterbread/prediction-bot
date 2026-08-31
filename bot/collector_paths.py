"""Canonical storage roots for collector-owned derived artifacts.

The code checkout may live on the host root filesystem while collector evidence
and derived artifacts belong on the collector volume.  Callers may override the
collector root for isolated tests only.
"""
from __future__ import annotations

import os
from pathlib import Path


COLLECTOR_ROOT_ENV = "PREDICTION_BOT_COLLECTOR_ROOT"
DEFAULT_COLLECTOR_ROOT = Path("/mnt/data-collection/prediction-bot")


def collector_root() -> Path:
    """Return the collector-owned repository root, never the code checkout."""
    return Path(os.environ.get(COLLECTOR_ROOT_ENV, str(DEFAULT_COLLECTOR_ROOT))).expanduser().resolve()


def derived_reports_root() -> Path:
    """Return the collector-owned root for derived reports."""
    return collector_root() / "data" / "derived_reports"


def auto_source_router_history_root() -> Path:
    """Return the canonical derived-only Source Router promotion root."""
    return derived_reports_root() / "auto_source_router_history"
