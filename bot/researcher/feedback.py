"""Trade outcome feedback loop — feeds results back to the researcher.

Tracks which researcher recommendations led to wins/losses, and uses
that data to improve future analysis quality over time.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class FeedbackTracker:
    """
    Tracks researcher recommendation outcomes.

    When the researcher analyzes a market and recommends a trade,
    we record that recommendation. When the trade resolves, we
    record the outcome. Over time, this shows which types of
    analysis are accurate and which aren't.
    """

    def __init__(self, data_dir: str = "data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.recommendations_file = self.data_dir / "researcher_recommendations.jsonl"
        self.feedback_file = self.data_dir / "researcher_feedback.json"

    def record_recommendation(
        self,
        market_id: str,
        market_type: str,
        researcher_response: dict,
        trade_taken: bool,
        trade_details: dict = None,
    ):
        """Record a researcher's recommendation for a market."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "market_id": market_id,
            "market_type": market_type,
            "assessment": researcher_response.get("assessment"),
            "true_probability": researcher_response.get("true_probability"),
            "edge": researcher_response.get("edge"),
            "confidence": researcher_response.get("confidence"),
            "direction": researcher_response.get("direction"),
            "reasoning": researcher_response.get("reasoning"),
            "trade_taken": trade_taken,
            "trade_details": trade_details or {},
            "outcome": None,  # filled in later
            "pnl": None,
        }
        with open(self.recommendations_file, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def record_outcome(self, market_id: str, outcome: str, pnl: float):
        """Record the outcome of a previously recommended trade."""
        entries = []
        found = False
        if self.recommendations_file.exists():
            with open(self.recommendations_file) as f:
                for line in f:
                    entry = json.loads(line)
                    if entry["market_id"] == market_id and entry["outcome"] is None:
                        entry["outcome"] = outcome
                        entry["pnl"] = pnl
                        entry["resolved_at"] = datetime.now(timezone.utc).isoformat()
                        found = True
                    entries.append(entry)

        if found:
            with open(self.recommendations_file, "w") as f:
                for entry in entries:
                    f.write(json.dumps(entry, default=str) + "\n")

    def get_accuracy_stats(self, market_type: str = None, last_n: int = 50) -> dict:
        """Calculate researcher accuracy stats."""
        entries = []
        if self.recommendations_file.exists():
            with open(self.recommendations_file) as f:
                for line in f:
                    entry = json.loads(line)
                    if market_type and entry.get("market_type") != market_type:
                        continue
                    if entry.get("outcome"):
                        entries.append(entry)

        entries = entries[-last_n:]

        if not entries:
            return {"total": 0, "resolved": 0, "accuracy": None, "avg_pnl": None}

        wins = sum(1 for e in entries if e["pnl"] and e["pnl"] > 0)
        resolved = sum(1 for e in entries if e["outcome"])
        total_pnl = sum(e.get("pnl", 0) for e in entries if e.get("pnl"))

        return {
            "total": len(entries),
            "resolved": resolved,
            "wins": wins,
            "losses": resolved - wins,
            "accuracy": wins / resolved if resolved > 0 else None,
            "avg_pnl": total_pnl / resolved if resolved > 0 else None,
            "total_pnl": total_pnl,
        }

    def get_pattern_insights(self, last_n: int = 100) -> dict:
        """Analyze patterns in researcher recommendations vs outcomes."""
        entries = []
        if self.recommendations_file.exists():
            with open(self.recommendations_file) as f:
                for line in f:
                    entry = json.loads(line)
                    if entry.get("outcome"):
                        entries.append(entry)

        entries = entries[-last_n:]

        if not entries:
            return {"message": "No resolved recommendations yet"}

        # Analyze by confidence range
        by_confidence = {"high": [], "medium": [], "low": []}
        for e in entries:
            conf = e.get("confidence", 0)
            if conf >= 0.7:
                by_confidence["high"].append(e)
            elif conf >= 0.5:
                by_confidence["medium"].append(e)
            else:
                by_confidence["low"].append(e)

        insights = {}
        for level, recs in by_confidence.items():
            if recs:
                wins = sum(1 for r in recs if r.get("pnl", 0) > 0)
                insights[f"confidence_{level}"] = {
                    "count": len(recs),
                    "accuracy": wins / len(recs),
                    "avg_pnl": sum(r.get("pnl", 0) for r in recs) / len(recs),
                }

        # Analyze by assessment
        by_assessment = {}
        for e in entries:
            assessment = e.get("assessment", "unknown")
            if assessment not in by_assessment:
                by_assessment[assessment] = []
            by_assessment[assessment].append(e)

        for assessment, recs in by_assessment.items():
            wins = sum(1 for r in recs if r.get("pnl", 0) > 0)
            insights[f"assessment_{assessment}"] = {
                "count": len(recs),
                "accuracy": wins / len(recs),
            }

        return insights
