import json
import tempfile
import time
import unittest
from pathlib import Path

from bot.runner import PredictionBot


class RunnerLiveReloadTests(unittest.TestCase):
    def _write_config(self, path: Path, enabled: bool, mode: str = "live"):
        path.write_text(
            "\n".join(
                [
                    "runtime:",
                    f"  base_dir: {path.parent}",
                    "trading:",
                    f"  mode: {mode}",
                    f"  enabled: {'true' if enabled else 'false'}",
                    "strategy:",
                    "  enable_news: false",
                    "  enable_social: false",
                    "  enable_ai: false",
                ]
            )
        )

    def test_reload_runtime_controls_emits_pause_resume_and_mode_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            self._write_config(config_path, enabled=True, mode="live")

            bot = PredictionBot(
                {
                    "log_dir": tmpdir,
                    "data_dir": tmpdir,
                    "_config_path": str(config_path),
                    "trading": {"mode": "live", "enabled": True},
                    "strategy": {
                        "enable_news": False,
                        "enable_social": False,
                        "enable_ai": False,
                    },
                }
            )
            bot.open_positions = []
            bot.open_orders = [{"order_id": "ord-1"}]

            time.sleep(0.02)
            self._write_config(config_path, enabled=False, mode="paper")
            changed = bot.reload_runtime_controls_if_needed()

            self.assertTrue(changed)
            self.assertEqual(bot.config["trading"]["mode"], "paper")
            self.assertFalse(bot.risk.trading_enabled)

            time.sleep(0.02)
            self._write_config(config_path, enabled=True, mode="paper")
            changed_again = bot.reload_runtime_controls_if_needed()

            self.assertTrue(changed_again)
            self.assertTrue(bot.risk.trading_enabled)

            with open(bot.log_dir / "lifecycle.jsonl") as f:
                events = [json.loads(line) for line in f if line.strip()]

            event_names = [event["event"] for event in events]
            self.assertIn("mode_changed", event_names)
            self.assertIn("trading_paused", event_names)
            self.assertIn("trading_resumed", event_names)

            pause_event = next(event for event in events if event["event"] == "trading_paused")
            self.assertEqual(pause_event["details"]["behavior"], "leave_resting_orders_untouched")
            self.assertEqual(pause_event["details"]["open_orders"], 1)


if __name__ == "__main__":
    unittest.main()
