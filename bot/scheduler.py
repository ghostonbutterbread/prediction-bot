"""Scan scheduler — adjusts scan frequency based on time-to-event.

The scheduler determines how often to scan based on how close the
nearest market is to closing. Markets closing soon get rapid scans,
distant markets get slow scans.

Usage:
    scheduler = ScanScheduler(config)
    interval = scheduler.get_interval("sports", nearest_closes_in_hours=2.5)
    # returns 120 (seconds) because 2.5 hours = "active" phase

    # Or let it auto-detect from markets:
    interval = scheduler.auto_interval(markets, market_type="sports")
"""

import logging
from typing import Optional
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class ScanPhase:
    """A single scan phase definition."""
    def __init__(self, name: str, max_hours_to_close: float,
                 interval_seconds: int, researcher_enabled: bool):
        self.name = name
        self.max_hours_to_close = max_hours_to_close
        self.interval_seconds = interval_seconds
        self.researcher_enabled = researcher_enabled

    def __repr__(self):
        return f"Phase({self.name}, <{self.max_hours_to_close}h, {self.interval_seconds}s)"


class ScanScheduler:
    """
    Time-based scan interval scheduler.

    Phases are defined in config.yaml under `schedule`.
    Each market type can have its own phases or use defaults.

    The scheduler picks the phase based on the nearest market's
    time to close. Closer markets → faster scans → more opportunities.
    """

    def __init__(self, config: dict = None):
        config = config or {}
        schedule_config = config.get("schedule", {})

        # Load default phases
        self.default_phases = self._load_phases(
            schedule_config.get("default_phases", self._builtin_defaults())
        )

        # Load market-type-specific phases
        self.market_phases = {}
        for market_type, phases in schedule_config.items():
            if market_type != "default_phases" and isinstance(phases, dict):
                self.market_phases[market_type] = self._load_phases(phases)

        self.current_phase = self.default_phases[0].name
        logger.info(
            f"Scheduler initialized: {len(self.default_phases)} default phases, "
            f"{len(self.market_phases)} market-type overrides"
        )

    def _builtin_defaults(self) -> dict:
        """Built-in fallback phase definitions."""
        return {
            "quiet": {"max_hours_to_close": 999, "interval_seconds": 300, "researcher_enabled": False},
            "active": {"max_hours_to_close": 4, "interval_seconds": 120, "researcher_enabled": True},
            "hot": {"max_hours_to_close": 1, "interval_seconds": 30, "researcher_enabled": True},
            "live": {"max_hours_to_close": 0, "interval_seconds": 15, "researcher_enabled": False},
        }

    def _load_phases(self, phase_config: dict) -> list[ScanPhase]:
        """Convert config dict to sorted list of ScanPhases."""
        phases = []
        for name, cfg in phase_config.items():
            phases.append(ScanPhase(
                name=name,
                max_hours_to_close=cfg.get("max_hours_to_close", 999),
                interval_seconds=cfg.get("interval_seconds", 300),
                researcher_enabled=cfg.get("researcher_enabled", False),
            ))
        # Sort by max_hours_to_close ascending (live first, quiet last)
        phases.sort(key=lambda p: p.max_hours_to_close)
        return phases

    def get_phases(self, market_type: str = None) -> list[ScanPhase]:
        """Get phases for a market type (falls back to defaults)."""
        if market_type and market_type in self.market_phases:
            return self.market_phases[market_type]
        return self.default_phases

    def get_interval(self, market_type: str = None,
                     nearest_closes_in_hours: float = None) -> int:
        """
        Get the scan interval in seconds based on time to nearest close.

        Args:
            market_type: e.g. "sports", "politics" (uses type-specific phases if set)
            nearest_closes_in_hours: hours until the closest market closes

        Returns:
            Interval in seconds
        """
        if nearest_closes_in_hours is None:
            # No markets → quiet mode
            return self.default_phases[-1].interval_seconds

        phases = self.get_phases(market_type)

        for phase in phases:
            if nearest_closes_in_hours <= phase.max_hours_to_close:
                if self.current_phase != phase.name:
                    logger.info(
                        f"Phase change: {self.current_phase} → {phase.name} "
                        f"({nearest_closes_in_hours:.1f}h to close, "
                        f"scanning every {phase.interval_seconds}s)"
                    )
                    self.current_phase = phase.name
                return phase.interval_seconds

        # Shouldn't happen, but fallback to last phase (quiet)
        return phases[-1].interval_seconds

    def is_researcher_enabled(self, market_type: str = None,
                               nearest_closes_in_hours: float = None) -> bool:
        """Check if researcher should run in the current phase."""
        if nearest_closes_in_hours is None:
            return False

        phases = self.get_phases(market_type)

        for phase in phases:
            if nearest_closes_in_hours <= phase.max_hours_to_close:
                return phase.researcher_enabled

        return False

    def auto_interval(self, markets: list, market_type: str = None) -> tuple[int, bool]:
        """
        Auto-determine interval from a list of markets.

        Finds the nearest closing time among markets and returns
        the appropriate interval.

        Args:
            markets: List of market objects with closes_at attribute
            market_type: Market type for phase selection

        Returns:
            (interval_seconds, researcher_enabled)
        """
        nearest_hours = self._find_nearest_close(markets)
        interval = self.get_interval(market_type, nearest_hours)
        researcher = self.is_researcher_enabled(market_type, nearest_hours)
        return interval, researcher

    def _find_nearest_close(self, markets: list) -> Optional[float]:
        """Find the nearest closing time among markets (in hours from now)."""
        if not markets:
            return None

        now = datetime.now(timezone.utc)
        nearest = None

        for market in markets:
            closes_at = getattr(market, "closes_at", None)
            if closes_at is None:
                continue

            # Handle string timestamps
            if isinstance(closes_at, str):
                try:
                    closes_at = datetime.fromisoformat(closes_at.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    continue

            if closes_at.tzinfo is None:
                closes_at = closes_at.replace(tzinfo=timezone.utc)

            delta = (closes_at - now).total_seconds() / 3600  # hours
            if delta > 0 and (nearest is None or delta < nearest):
                nearest = delta

        return nearest

    def get_phase_info(self, market_type: str = None) -> dict:
        """Get current phase info for logging/display."""
        phases = self.get_phases(market_type)
        return {
            "current_phase": self.current_phase,
            "available_phases": [
                {
                    "name": p.name,
                    "max_hours": p.max_hours_to_close,
                    "interval": p.interval_seconds,
                    "researcher": p.researcher_enabled,
                }
                for p in phases
            ],
        }
