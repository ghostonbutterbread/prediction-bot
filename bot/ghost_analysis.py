"""Ghost's market analysis tools.

Reads market snapshots and generates trading recommendations.
This is what Ghost uses during heartbeats to analyze markets.
"""

import json
import sys
from pathlib import Path

# Add parent dir to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from bot.feeds.ai_signal import write_ai_signals, AISignal, SNAPSHOT_PATH


def load_snapshot(path: str = SNAPSHOT_PATH) -> dict:
    """Load the latest market snapshot."""
    with open(path) as f:
        return json.load(f)


def analyze_snapshot(snapshot: dict) -> list[AISignal]:
    """Analyze a market snapshot and generate signals.

    This is the main function Ghost calls.
    Returns list of AISignal objects.
    """
    signals = []
    markets = snapshot.get("markets", [])

    for m in markets:
        signal = analyze_single_market(m)
        if signal:
            signals.append(signal)

    return signals


def analyze_single_market(market: dict) -> AISignal | None:
    """Analyze a single market and return a signal.

    Ghost can modify this logic based on research and pattern recognition.
    """
    question = market.get("question", "")
    yes_price = market.get("yes_price", 0)
    volume = market.get("volume", 0)
    category = market.get("category", "")

    if yes_price <= 0 or yes_price >= 1:
        return None

    # === Ghost's Analysis Logic ===
    # This is where Ghost adds human-like reasoning
    # Modify these heuristics based on research

    reasoning_parts = []
    direction = "SKIP"
    confidence = 0.5
    edge = 0.0

    # Bias 1: Longshot bias
    if yes_price < 0.10:
        reasoning_parts.append("Longshot bias: low-probability markets often overpriced")
        direction = "BUY_NO"
        confidence = 0.55
        edge = 0.03
    elif yes_price > 0.90:
        reasoning_parts.append("Near-certainty bias: high-probability markets often underpriced")
        direction = "BUY_YES"
        confidence = 0.55
        edge = 0.02

    # Bias 2: Volume efficiency
    if volume > 50000 and direction != "SKIP":
        confidence += 0.05
        reasoning_parts.append(f"High volume (${volume:,.0f}): more efficient pricing")
    elif volume < 1000:
        confidence -= 0.05
        reasoning_parts.append(f"Low volume (${volume:,.0f}): less efficient, more potential edge")

    # Bias 3: Category adjustments
    if "sports" in category.lower():
        confidence -= 0.03
        reasoning_parts.append("Sports: emotional betting, less efficient")

    if direction == "SKIP":
        return None

    return AISignal(
        market_id=market.get("id", ""),
        question=question,
        direction=direction,
        confidence=round(confidence, 3),
        reasoning=" | ".join(reasoning_parts) if reasoning_parts else "Standard analysis",
        edge_estimate=round(edge, 4),
    )


if __name__ == "__main__":
    # CLI: analyze snapshot and write signals
    try:
        snapshot = load_snapshot()
        print(f"Loaded snapshot: {len(snapshot.get('markets', []))} markets")
        signals = analyze_snapshot(snapshot)
        write_ai_signals(signals, summary=f"Analyzed {len(snapshot.get('markets', []))} markets, {len(signals)} signals generated")
        print(f"Generated {len(signals)} signals")
        for s in signals:
            print(f"  {s.direction} | {s.question[:40]} | conf={s.confidence:.1%} | {s.reasoning[:50]}")
    except FileNotFoundError:
        print("No snapshot found. Run simulation first to generate one.")
