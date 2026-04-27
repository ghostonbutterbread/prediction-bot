import json
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from bot.runner import PredictionBot


class RunnerLifecycleSummaryTests(unittest.TestCase):
    def _make_bot(self, tmpdir):
        config = {
            "log_dir": tmpdir,
            "data_dir": tmpdir,
            "trading_enabled": True,
            "trading": {"mode": "paper", "trading_enabled": True},
            "strategy": {
                "min_edge": 0.05,
                "min_confidence": 0.5,
                "enable_news": False,
                "enable_social": False,
                "enable_ai": False,
            },
        }
        return PredictionBot(config)

    def test_build_status_snapshot_includes_lifecycle_counters(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.lifecycle_counters["signals_considered"] = 12
            bot.lifecycle_counters["trades_executed"] = 3
            bot.lifecycle_counters["blocked_total"] = 4
            bot.lifecycle_counters["errors"] = 1
            bot.live_failure_streaks["kalshi"] = {
                "count": 2,
                "last_reason": "execution_failed",
                "issues": ["execution_failed"],
            }
            bot.reconciliation_gate["kalshi"] = {"verdict": "blocked", "issues": ["repeated_live_failures_threshold_reached"]}

            snapshot = bot.build_status_snapshot(reason="manual")

            self.assertEqual(snapshot.extra["signals_considered"], 12)
            self.assertEqual(snapshot.extra["trades_executed"], 3)
            self.assertEqual(snapshot.extra["blocked_total"], 4)
            self.assertEqual(snapshot.extra["runner_errors"], 1)
            self.assertEqual(snapshot.extra["live_failure_streaks"]["kalshi"]["count"], 2)
            self.assertEqual(snapshot.extra["reconciliation_gate"]["kalshi"]["verdict"], "blocked")

    def test_emit_hourly_summary_writes_once_per_hour(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.stats["scans"] = 5
            bot.lifecycle_counters["signals_considered"] = 9
            bot.lifecycle_counters["trades_executed"] = 2
            bot.lifecycle_counters["blocked_total"] = 3
            bot.lifecycle_counters["errors"] = 1
            bot.lifecycle_block_reasons = {"risk_max_drawdown_hit": 2, "risk_position_cap": 1}
            bot.live_failure_streaks["kalshi"] = {"count": 2, "last_reason": "execution_failed", "issues": ["execution_failed"]}
            bot.reconciliation_gate["kalshi"] = {"verdict": "blocked", "issues": ["repeated_live_failures_threshold_reached"]}

            fixed_now = datetime(2026, 4, 20, 17, 0, 0, tzinfo=timezone.utc)
            summary_path = str(bot.log_dir / "hourly_summary.jsonl")

            class FixedDateTime(datetime):
                @classmethod
                def now(cls, tz=None):
                    return fixed_now if tz else fixed_now.replace(tzinfo=None)

            with patch("bot.runner.datetime", FixedDateTime):
                bot._emit_hourly_summary_if_due()
                bot._emit_hourly_summary_if_due()

            with open(summary_path) as f:
                lines = [json.loads(line) for line in f if line.strip()]

            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["scans"], 5)
            self.assertEqual(lines[0]["signals_considered"], 9)
            self.assertEqual(lines[0]["top_blockers"]["risk_max_drawdown_hit"], 2)
            self.assertEqual(lines[0]["live_failure_streaks"]["kalshi"], 2)
            self.assertEqual(lines[0]["reconciliation_gate"]["kalshi"]["verdict"], "blocked")


if __name__ == "__main__":
    unittest.main()
