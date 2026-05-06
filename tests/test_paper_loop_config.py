import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import paper_loop


class PaperLoopConfigTests(unittest.TestCase):
    def test_get_config_loads_scan_limit_from_yaml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.yaml"
            cfg_path.write_text(
                f"""
runtime:
  base_dir: {Path(tmpdir) / "data"}
trading:
  mode: paper
scan:
  markets_per_exchange: 321
"""
            )

            with patch.dict(os.environ, {"PAPER_MODE": "true"}, clear=False):
                os.environ.pop("MARKETS_PER_EXCHANGE", None)
                cfg = paper_loop.get_config(cfg_path)

        self.assertEqual(cfg["scan"]["markets_per_exchange"], 321)

    def test_get_config_allows_markets_per_exchange_env_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.yaml"
            cfg_path.write_text(
                f"""
runtime:
  base_dir: {Path(tmpdir) / "data"}
trading:
  mode: paper
scan:
  markets_per_exchange: 321
"""
            )

            with patch.dict(os.environ, {"PAPER_MODE": "true", "MARKETS_PER_EXCHANGE": "222"}, clear=False):
                cfg = paper_loop.get_config(cfg_path)

        self.assertEqual(cfg["scan"]["markets_per_exchange"], 222)

    def test_paper_mode_forces_stable_hidden_gem_guard_but_not_directional_mismatch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.yaml"
            cfg_path.write_text(
                f"""
runtime:
  base_dir: {Path(tmpdir) / "data"}
strategy:
  enable_weather_hidden_gem_safety_guard: false
  enable_weather_directional_mismatch_guard: false
"""
            )

            with patch.dict(os.environ, {"PAPER_MODE": "true"}, clear=False):
                os.environ.pop("ENABLE_WEATHER_HIDDEN_GEM_SAFETY_GUARD", None)
                os.environ.pop("ENABLE_WEATHER_DIRECTIONAL_MISMATCH_GUARD", None)
                cfg = paper_loop.get_config(cfg_path)

        self.assertTrue(cfg["strategy"]["enable_weather_hidden_gem_safety_guard"])
        self.assertFalse(cfg["strategy"]["enable_weather_directional_mismatch_guard"])
        self.assertFalse(cfg["strategy"]["weather_directional_mismatch_guard_explicit_override"])

    def test_paper_mode_enables_directional_mismatch_for_beta_enforce_policy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.yaml"
            cfg_path.write_text(
                f"""
runtime:
  base_dir: {Path(tmpdir) / "data"}
strategy_policy:
  version: beta
  beta:
    mode: enforce
    features:
      hidden_gem_lane_gates: true
strategy:
  enable_weather_directional_mismatch_guard: false
"""
            )

            with patch.dict(os.environ, {"PAPER_MODE": "true"}, clear=False):
                os.environ.pop("ENABLE_WEATHER_DIRECTIONAL_MISMATCH_GUARD", None)
                cfg = paper_loop.get_config(cfg_path)

        self.assertTrue(cfg["strategy"]["enable_weather_directional_mismatch_guard"])
        self.assertFalse(cfg["strategy"]["weather_directional_mismatch_guard_explicit_override"])

    def test_paper_mode_allows_env_to_disable_safety_guards(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.yaml"
            cfg_path.write_text(
                f"""
runtime:
  base_dir: {Path(tmpdir) / "data"}
strategy:
  enable_weather_hidden_gem_safety_guard: true
  enable_weather_directional_mismatch_guard: true
"""
            )

            with patch.dict(
                os.environ,
                {
                    "PAPER_MODE": "true",
                    "ENABLE_WEATHER_HIDDEN_GEM_SAFETY_GUARD": "false",
                    "ENABLE_WEATHER_DIRECTIONAL_MISMATCH_GUARD": "false",
                },
                clear=False,
            ):
                cfg = paper_loop.get_config(cfg_path)

        self.assertFalse(cfg["strategy"]["enable_weather_hidden_gem_safety_guard"])
        self.assertFalse(cfg["strategy"]["enable_weather_directional_mismatch_guard"])

    def test_paper_mode_allows_env_to_explicitly_enable_directional_mismatch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.yaml"
            cfg_path.write_text(
                f"""
runtime:
  base_dir: {Path(tmpdir) / "data"}
strategy:
  enable_weather_directional_mismatch_guard: false
"""
            )

            with patch.dict(
                os.environ,
                {
                    "PAPER_MODE": "true",
                    "ENABLE_WEATHER_DIRECTIONAL_MISMATCH_GUARD": "true",
                },
                clear=False,
            ):
                cfg = paper_loop.get_config(cfg_path)

        self.assertTrue(cfg["strategy"]["enable_weather_directional_mismatch_guard"])
        self.assertTrue(cfg["strategy"]["weather_directional_mismatch_guard_explicit_override"])


if __name__ == "__main__":
    unittest.main()
