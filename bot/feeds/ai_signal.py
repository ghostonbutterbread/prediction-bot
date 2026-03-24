"""AI signal bridge — connects Ghost's analysis to the trading bot.

Architecture:
- Bot writes market snapshots to data/market_snapshot.json every N scans
- Ghost reads snapshots during heartbeats, analyzes patterns
- Ghost writes recommendations to data/ai_signals.json
- Strategy engine reads AI signals as 6th signal weight
"""

import json
import logging
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SNAPSHOT_PATH = "data/market_snapshot.json"
SIGNALS_PATH = "data/ai_signals.json"


@dataclass
class AISignal:
    """A single AI recommendation."""
    market_id: str
    question: str
    direction: str          # BUY_YES, BUY_NO, SKIP, STRONG_YES, STRONG_NO
    confidence: float       # 0.0 to 1.0
    reasoning: str          # Why this recommendation
    edge_estimate: float    # Estimated edge
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


def write_snapshot(markets: list, session_id: str = ""):
    """Write market snapshot for Ghost to analyze.

    Called by the simulator every N scans.
    """
    snapshot = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session": session_id,
        "markets": [],
    }

    for m in markets:
        snapshot["markets"].append({
            "id": m.id,
            "question": m.question,
            "yes_price": m.yes_price,
            "no_price": m.no_price,
            "volume": m.volume,
            "closes_at": str(m.closes_at) if hasattr(m, 'closes_at') and m.closes_at else None,
            "category": getattr(m, 'category', ''),
        })

    Path("data").mkdir(exist_ok=True)
    with open(SNAPSHOT_PATH, "w") as f:
        json.dump(snapshot, f, indent=2)

    logger.info(f"Market snapshot written: {len(markets)} markets")


def read_ai_signals() -> list[dict]:
    """Read Ghost's recommendations.

    Called by the strategy engine to get AI signals.
    """
    try:
        with open(SIGNALS_PATH) as f:
            data = json.load(f)

        # Check freshness (signals older than 30 min are stale)
        ts = data.get("timestamp", "")
        if ts:
            signal_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            age_min = (datetime.now(timezone.utc) - signal_time).total_seconds() / 60
            if age_min > 30:
                logger.debug(f"AI signals are {age_min:.0f} min old, ignoring")
                return []

        signals = data.get("signals", [])
        logger.info(f"Loaded {len(signals)} AI signals (age: {age_min:.0f} min)" if ts else f"Loaded {len(signals)} AI signals")
        return signals

    except (FileNotFoundError, json.JSONDecodeError):
        return []


def write_ai_signals(signals: list[AISignal], summary: str = ""):
    """Write Ghost's recommendations to disk.

    This is what Ghost calls after analyzing a market snapshot.
    """
    data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "signals": [asdict(s) for s in signals],
        "total_markets_analyzed": len(signals),
    }

    Path("data").mkdir(exist_ok=True)
    with open(SIGNALS_PATH, "w") as f:
        json.dump(data, f, indent=2)

    logger.info(f"AI signals written: {len(signals)} recommendations")


class AISignalFeed:
    """Strategy engine integration for AI signals."""

    def __init__(self, config: dict = None):
        config = config or {}
        self.enabled = config.get("enable_ai", True)
        self.weight = config.get("ai_weight", 0.20)
        self._cache = []
        self._cache_time = 0
        self._cache_ttl = 60  # Re-read every 60 seconds

    def get_signal(self, market_id: str) -> Optional[dict]:
        """Get AI signal for a specific market."""
        if not self.enabled:
            return None

        # Refresh cache
        now = time.time()
        if now - self._cache_time > self._cache_ttl:
            self._cache = read_ai_signals()
            self._cache_time = now

        # Find signal for this market
        for sig in self._cache:
            if sig.get("market_id") == market_id:
                # Convert direction to probability
                direction = sig.get("direction", "SKIP")
                confidence = sig.get("confidence", 0.5)

                if direction in ("STRONG_YES", "BUY_YES"):
                    predicted = 0.5 + confidence * 0.3
                elif direction in ("STRONG_NO", "BUY_NO"):
                    predicted = 0.5 - confidence * 0.3
                else:
                    return None  # SKIP

                return {
                    "signal_type": "ai",
                    "predicted_prob": max(0.01, min(0.99, predicted)),
                    "confidence": confidence,
                    "source_timestamp": sig.get("timestamp"),
                    "ttl_seconds": 1800,
                }

        return None

    def get_summary(self) -> str:
        """Get the latest AI analysis summary."""
        signals = read_ai_signals()
        if not signals:
            return "No AI analysis available"
        # Return the summary from the data file
        try:
            with open(SIGNALS_PATH) as f:
                data = json.load(f)
            return data.get("summary", "No summary")
        except:
            return "Error reading AI signals"
