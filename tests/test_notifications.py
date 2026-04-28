import json
import tempfile
import unittest

from bot.notifications import build_notification, normalize_verbosity
from bot.runner import PredictionBot


class NotificationFormatterTests(unittest.TestCase):
    def test_normalize_verbosity_aliases(self):
        self.assertEqual(normalize_verbosity("-v"), "normal")
        self.assertEqual(normalize_verbosity("verbose"), "verbose")
        self.assertEqual(normalize_verbosity("double"), "double_verbose")

    def test_trade_placed_normal_message_contains_balance_and_confidence(self):
        msg = build_notification(
            "trade_placed",
            {
                "direction": "BUY_YES",
                "market_id": "m1",
                "size": 4,
                "price": 0.42,
                "confidence": 0.81,
                "balance_after": 96,
                "reserved_capital": 4,
            },
            verbosity="normal",
        )
        self.assertIn("Trade placed", msg)
        self.assertIn("Confidence: 81.0%", msg)
        self.assertIn("Balance: $96.00", msg)


class RunnerNotificationEmissionTests(unittest.TestCase):
    def test_single_trade_completed_written_to_notifications_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = PredictionBot(
                {
                    "log_dir": tmpdir,
                    "alerts": {
                        "enabled": True,
                        "single_trade_events": True,
                    },
                    "verbosity": {"level": "verbose"},
                    "trading": {"single_trade_mode": True},
                }
            )
            bot._log_lifecycle_event(
                "single_trade_completed",
                {"open_positions": 1, "open_orders": 0, "behavior": "continue_resolution_tracking"},
            )
            notifications = bot.log_dir / "notifications.jsonl"
            self.assertTrue(notifications.exists())
            entries = [json.loads(line) for line in notifications.read_text().splitlines() if line.strip()]
            self.assertEqual(entries[-1]["event"], "single_trade_completed")
            self.assertIn("No further new entries", entries[-1]["message"])

    def test_hourly_summary_written_to_notifications_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = PredictionBot(
                {
                    "log_dir": tmpdir,
                    "alerts": {
                        "enabled": True,
                        "status_events": True,
                    },
                    "verbosity": {"level": "normal"},
                }
            )
            bot._log_lifecycle_event(
                "hourly_summary",
                {
                    "mode": "paper",
                    "scans": 4,
                    "signals_considered": 7,
                    "trades_executed": 1,
                    "blocked_total": 2,
                    "open_positions": 1,
                    "errors": 0,
                },
            )
            notifications = bot.log_dir / "notifications.jsonl"
            entries = [json.loads(line) for line in notifications.read_text().splitlines() if line.strip()]
            self.assertEqual(entries[-1]["event"], "hourly_summary")
            self.assertIn("Hourly summary", entries[-1]["message"])


if __name__ == "__main__":
    unittest.main()
