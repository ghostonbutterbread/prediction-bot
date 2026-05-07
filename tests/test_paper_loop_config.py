import importlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest.mock import patch

import dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]


def import_paper_loop():
    return importlib.import_module("paper_loop")


class PaperLoopConfigTests(unittest.TestCase):
    def tearDown(self):
        sys.modules.pop("paper_loop", None)

    def test_import_does_not_load_dotenv_or_mutate_env_controls(self):
        sys.modules.pop("paper_loop", None)

        def fake_load_dotenv(*args, **kwargs):
            os.environ["KALSHI_USE_DEMO"] = "false"
            return True

        with patch.dict(os.environ, {}, clear=True):
            with patch.object(dotenv, "load_dotenv", side_effect=fake_load_dotenv) as load:
                import_paper_loop()

            load.assert_not_called()
            self.assertNotIn("KALSHI_USE_DEMO", os.environ)
            self.assertNotIn("PAPER_MODE", os.environ)

    def test_fresh_import_has_no_env_logging_file_or_sys_path_side_effects(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "logs" / "paper_loop.log"
            env = {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PAPER_LOG_FILE": str(log_path),
            }
            script = textwrap.dedent(
                """
                import json
                import logging
                import os
                import sys
                from pathlib import Path

                before_handlers = list(logging.getLogger().handlers)
                before_level = logging.getLogger().level
                before_path = list(sys.path)
                import paper_loop
                after_path = list(sys.path)
                print(json.dumps({
                    "paper_mode": os.environ.get("PAPER_MODE"),
                    "kalshi_use_demo": os.environ.get("KALSHI_USE_DEMO"),
                    "root_handlers_same": list(logging.getLogger().handlers) == before_handlers,
                    "root_level_same": logging.getLogger().level == before_level,
                    "log_exists": Path(os.environ["PAPER_LOG_FILE"]).exists(),
                    "log_parent_exists": Path(os.environ["PAPER_LOG_FILE"]).parent.exists(),
                    "sys_path_same": after_path == before_path,
                }))
                """
            )

            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            payload = json.loads(result.stdout)

        self.assertIsNone(payload["paper_mode"])
        self.assertIsNone(payload["kalshi_use_demo"])
        self.assertTrue(payload["root_handlers_same"])
        self.assertTrue(payload["root_level_same"])
        self.assertFalse(payload["log_exists"])
        self.assertFalse(payload["log_parent_exists"])
        self.assertTrue(payload["sys_path_same"])

    def test_import_does_not_configure_root_logging_or_open_log_file(self):
        sys.modules.pop("paper_loop", None)
        root_logger = logging.getLogger()
        original_handlers = list(root_logger.handlers)
        original_level = root_logger.level

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "logs" / "paper_loop.log"

            with patch.dict(os.environ, {"PAPER_LOG_FILE": str(log_path)}, clear=True):
                import_paper_loop()

            self.assertEqual(root_logger.handlers, original_handlers)
            self.assertEqual(root_logger.level, original_level)
            self.assertFalse(log_path.exists())
            self.assertFalse(log_path.parent.exists())

    def test_configure_logging_creates_runtime_handlers_and_log_file(self):
        paper_loop = import_paper_loop()
        root_logger = logging.getLogger()
        original_handlers = list(root_logger.handlers)
        original_level = root_logger.level
        named_loggers = ["paper-loop", *paper_loop._RUNTIME_LOGGER_NAMES]
        original_named_levels = {name: logging.getLogger(name).level for name in named_loggers}

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "logs" / "paper_loop.log"
            try:
                with patch.dict(os.environ, {"PAPER_LOG_FILE": str(log_path)}, clear=True):
                    paper_loop.configure_logging()

                self.assertTrue(log_path.exists())
                self.assertEqual(root_logger.level, logging.WARNING)
                self.assertTrue(any(isinstance(handler, RotatingFileHandler) for handler in root_logger.handlers))
                self.assertTrue(any(type(handler) is logging.StreamHandler for handler in root_logger.handlers))
                self.assertEqual(paper_loop.logger.level, logging.INFO)
            finally:
                for handler in list(root_logger.handlers):
                    if handler not in original_handlers:
                        root_logger.removeHandler(handler)
                        handler.close()
                root_logger.handlers[:] = original_handlers
                root_logger.setLevel(original_level)
                for name, level in original_named_levels.items():
                    logging.getLogger(name).setLevel(level)

    def test_load_runtime_env_intentionally_loads_dotenv_and_refreshes_settings(self):
        paper_loop = import_paper_loop()
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                """
KALSHI_USE_DEMO=false
PAPER_SCAN_INTERVAL=7
SIMULATE_ONLY=false
PAPER_SUMMARY_SCAN_INTERVAL=8
PAPER_SUMMARY_LOG_SECONDS=9
"""
            )

            with patch.dict(os.environ, {}, clear=True):
                paper_loop.load_runtime_env(env_path)

                self.assertEqual(os.environ["PAPER_MODE"], "true")
                self.assertEqual(os.environ["KALSHI_USE_DEMO"], "false")
                self.assertEqual(paper_loop.INTERVAL, 7)
                self.assertFalse(paper_loop.SIMULATE_ONLY)
                self.assertEqual(paper_loop.SUMMARY_SCAN_INTERVAL, 8)
                self.assertEqual(paper_loop.SUMMARY_LOG_SECONDS, 9)

    def test_get_config_loads_scan_limit_from_yaml(self):
        paper_loop = import_paper_loop()
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
        paper_loop = import_paper_loop()
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
        paper_loop = import_paper_loop()
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
        paper_loop = import_paper_loop()
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
        paper_loop = import_paper_loop()
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
        paper_loop = import_paper_loop()
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
