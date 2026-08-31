"""Canonical storage roots for collector-owned derived artifacts.

The default collector root is `/mnt/data-collection/prediction-bot`; set
``PREDICTION_BOT_COLLECTOR_ROOT`` to select a different default.  Individual
calls may also supply an explicit output root when a one-off artifact belongs
elsewhere.
"""
from __future__ import annotations

import os
from pathlib import Path


COLLECTOR_ROOT_ENV = "PREDICTION_BOT_COLLECTOR_ROOT"
DEFAULT_COLLECTOR_ROOT = Path("/mnt/data-collection/prediction-bot")


def collector_root() -> Path:
    """Return the configured default root for collector-owned artifacts."""
    return Path(os.environ.get(COLLECTOR_ROOT_ENV, str(DEFAULT_COLLECTOR_ROOT))).expanduser().resolve()


def derived_reports_root() -> Path:
    """Return the collector-owned root for derived reports."""
    return collector_root() / "data" / "derived_reports"


def auto_source_router_history_root() -> Path:
    """Return the canonical derived-only Source Router promotion root."""
    return derived_reports_root() / "auto_source_router_history"
