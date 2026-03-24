"""AI Analyzer — standalone script for subagent analysis.

Called by the simulator every N scans via subagent spawn.
Reads market snapshot, analyzes patterns, writes AI signals.

Usage: python3 -m bot.ai_analyzer [snapshot_path] [signals_path]
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Default paths
SNAPSHOT_PATH = "data/market_snapshot.json"
SIGNALS_PATH = "data/ai_signals.json"


def load_snapshot(path: str = SNAPSHOT_PATH) -> dict:
    """Load market snapshot."""
    with open(path) as f:
        return json.load(f)


def analyze_markets(snapshot: dict) -> list[dict]:
    """Analyze markets and generate signals.

    This is the core analysis logic — Ghost's reasoning encoded as heuristics.
    As patterns are discovered, this gets updated.
    """
    signals = []
    markets = snapshot.get("markets", [])

    for m in markets:
        signal = analyze_single(m)
        if signal:
            signals.append(signal)

    return signals


def analyze_single(market: dict) -> dict | None:
    """Analyze a single market."""
    question = market.get("question", "")
    yes_price = market.get("yes_price", 0)
    volume = market.get("volume", 0)
    category = market.get("category", "")
    market_id = market.get("id", "")

    if yes_price <= 0 or yes_price >= 1:
        return None

    reasoning = []
    direction = "SKIP"
    confidence = 0.5
    edge = 0.0

    # === Ghost's Analysis Heuristics ===
    # Updated as we learn patterns from simulation data

    # 1. Longshot bias (strongest signal so far)
    if yes_price < 0.08:
        reasoning.append("Deep longshot — likely overpriced by emotional bettors")
        direction = "BUY_NO"
        confidence = 0.60
        edge = 0.04
    elif yes_price < 0.15:
        reasoning.append("Longshot range — slight overpricing expected")
        direction = "BUY_NO"
        confidence = 0.55
        edge = 0.02
    elif yes_price > 0.92:
        reasoning.append("Near-certainty — slight underpricing possible")
        direction = "BUY_YES"
        confidence = 0.55
        edge = 0.02
    elif yes_price > 0.80:
        reasoning.append("High probability — check if underpriced")
        direction = "BUY_YES"
        confidence = 0.52
        edge = 0.015

    # 2. Volume adjustment
    if volume > 100000:
        confidence += 0.08
        reasoning.append(f"Very high volume (${volume:,.0f}) — liquid market")
    elif volume > 20000:
        confidence += 0.04
        reasoning.append(f"Good volume (${volume:,.0f})")
    elif volume < 5000:
        confidence -= 0.05
        reasoning.append(f"Low volume (${volume:,.0f}) — wider spreads likely")

    # 3. Category signals
    cat_lower = category.lower()
    if "politics" in cat_lower or "election" in cat_lower:
        confidence += 0.03
        reasoning.append("Political market — polling data available")
    elif "sports" in cat_lower:
        confidence -= 0.02
        reasoning.append("Sports — emotional betting, less predictable")
    elif "crypto" in cat_lower or "finance" in cat_lower:
        confidence += 0.02
        reasoning.append("Financial market — data-driven")

    # 4. Question pattern matching
    q_lower = question.lower()

    # Pope markets — multiple candidates, one resolves YES
    if "pope" in q_lower and direction == "BUY_NO":
        # Each individual candidate is unlikely, but SOMEONE will be pope
        # So BUY_NO on low-probability candidates is solid
        edge += 0.01
        reasoning.append("Pope market — individual candidates unlikely")

    # Climate/environmental — long timeframes, hard to predict
    if any(w in q_lower for w in ["climate", "degrees", "celsius", "temperature"]):
        confidence -= 0.05
        reasoning.append("Long-term climate market — high uncertainty")

    # SpaceX/Mars — Elon time is optimistic
    if any(w in q_lower for w in ["mars", "spacex", "elon"]):
        if direction == "BUY_YES":
            confidence -= 0.05
            reasoning.append("Elon timeline — historically optimistic")

    if direction == "SKIP":
        return None

    return {
        "market_id": market_id,
        "category": category,
        "question": question,
        "direction": direction,
        "confidence": round(min(0.95, max(0.1, confidence)), 3),
        "edge_estimate": round(edge, 4),
        "reasoning": " | ".join(reasoning),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def write_signals(signals: list[dict], path: str = SIGNALS_PATH, summary: str = ""):
    """Write AI signals to disk."""
    Path("data").mkdir(exist_ok=True)

    data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": summary or f"Analyzed markets, {len(signals)} signals",
        "signals": signals,
        "total_markets_analyzed": len(signals),
    }

    with open(path, "w") as f:
        json.dump(data, f, indent=2)

    print(f"Written {len(signals)} signals to {path}")


def main():
    """Main entry point for subagent execution."""
    snapshot_path = sys.argv[1] if len(sys.argv) > 1 else SNAPSHOT_PATH
    signals_path = sys.argv[2] if len(sys.argv) > 2 else SIGNALS_PATH

    try:
        snapshot = load_snapshot(snapshot_path)
        total = len(snapshot.get("markets", []))
        print(f"Loaded snapshot: {total} markets")

        signals = analyze_markets(snapshot)
        print(f"Generated {len(signals)} signals:")

        for s in signals:
            print(f"  {s['direction']:10} | {s['question'][:40]:40} | edge={s['edge_estimate']:.2%} conf={s['confidence']:.1%}")

        summary = f"Analyzed {total} markets, {len(signals)} signals. Avg edge: {sum(s['edge_estimate'] for s in signals)/max(len(signals),1):.2%}"
        write_signals(signals, signals_path, summary)

    except FileNotFoundError:
        print(f"No snapshot at {snapshot_path}")
        sys.exit(1)


if __name__ == "__main__":
    main()
