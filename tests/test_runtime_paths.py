import tempfile
import unittest
from pathlib import Path

from bot.config import load_config
from bot.paper_adapters import SimulatorPaperSessionStore
from bot.runner import PredictionBot
from bot.simulator import Simulator


class RuntimePathTests(unittest.TestCase):
    def test_load_config_scopes_paths_by_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.yaml"
            cfg_path.write_text(
                """
runtime:
  base_dir: runtime_data
trading:
  mode: live
strategy:
  weather_observation_log_path: data/weather_observations.jsonl
"""
            )
            cfg = load_config(cfg_path)
            self.assertTrue(cfg["data_dir"].endswith("runtime_data/live"))
            self.assertTrue(cfg["log_dir"].endswith("runtime_data/live"))
            self.assertTrue(cfg["strategy"]["weather_observation_log_path"].endswith("runtime_data/live/weather_observations.jsonl"))

    def test_runner_uses_mode_scoped_log_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = PredictionBot(
                {
                    "runtime": {"base_dir": tmpdir},
                    "trading": {"mode": "live"},
                    "log_dir": tmpdir,
                    "data_dir": tmpdir,
                }
            )
            self.assertTrue(str(bot.log_dir).endswith("/live"))

    def test_simulator_uses_mode_scoped_data_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sim = Simulator(
                {
                    "runtime": {"base_dir": tmpdir},
                    "trading": {"mode": "paper"},
                    "data_dir": tmpdir,
                }
            )
            self.assertTrue(str(sim.data_dir).endswith("/paper"))

    def test_paper_session_store_falls_back_to_legacy_root_layout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            legacy = Path(tmpdir) / "sim_20260423_000000.json"
            legacy.write_text('{"session_id":"20260423_000000","trades":[]}')

            sim = Simulator(
                {
                    "trading": {"mode": "paper"},
                    "data_dir": tmpdir,
                    "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
                }
            )
            store = SimulatorPaperSessionStore(sim, sim.state_adapter)
            resolved = store._resolve_session_file("20260423_000000")

            self.assertEqual(resolved, legacy)


if __name__ == "__main__":
    unittest.main()
