import json
import tempfile
import unittest

from bot.runner import PredictionBot


class RunnerLifecycleEventTests(unittest.TestCase):
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

    def test_startup_event_logged_on_init(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._make_bot(tmpdir)
            with open(f"{tmpdir}/lifecycle.jsonl") as f:
                entries = [json.loads(line) for line in f if line.strip()]

            self.assertEqual(entries[0]["event"], "startup")
            self.assertEqual(entries[0]["details"]["mode"], "paper")

    def test_stop_and_shutdown_events_logged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.stats["scans"] = 4
            bot.stats["signals"] = 7
            bot.stats["trades"] = 2
            bot.stop()
            bot.close()

            with open(f"{tmpdir}/lifecycle.jsonl") as f:
                entries = [json.loads(line) for line in f if line.strip()]

            events = [entry["event"] for entry in entries]
            self.assertEqual(events, ["startup", "stop_requested", "shutdown"])
            self.assertEqual(entries[-1]["details"]["trades"], 2)
            self.assertEqual(entries[-1]["details"]["signals"], 7)


if __name__ == "__main__":
    unittest.main()
