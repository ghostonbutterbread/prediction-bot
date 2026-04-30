import importlib
import io
import os
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from bot.live_canary import validate_live_canary_config


def valid_canary_config() -> dict:
    return {
        "trading_enabled": False,
        "trading": {
            "mode": "live",
            "enabled": False,
            "trading_enabled": False,
            "single_trade_mode": True,
            "live_reconciliation": {"block_on_degraded": True},
            "live_identity": {"exchange": "kalshi", "environment": "prod"},
        },
        "max_tradable_balance_usd": 25.0,
        "max_position_size_usd": 2.0,
        "risk": {
            "daily_loss_limit_pct": 0.05,
            "max_drawdown_pct": 0.10,
            "max_open_positions": 3,
        },
    }


class LiveCanaryPreflightTests(unittest.TestCase):
    def test_unsafe_zero_caps_fail_closed(self):
        config = valid_canary_config()
        config["max_tradable_balance_usd"] = 0.0
        config["max_position_size_usd"] = 0.0

        report = validate_live_canary_config(config)

        self.assertFalse(report["ready"])
        self.assertIn("max_tradable_balance_cap", self._failed_check_names(report))
        self.assertIn("max_position_size_usd", self._failed_check_names(report))

    def test_percent_mistake_fails(self):
        config = valid_canary_config()
        config["risk"]["daily_loss_limit_pct"] = 35

        report = validate_live_canary_config(config)

        self.assertFalse(report["ready"])
        self.assertIn("daily_loss_limit_pct", self._failed_check_names(report))
        self.assertTrue(any("whole-percent" in issue for issue in report["issues"]))

    def test_valid_unarmed_config_passes_readiness_with_trading_disabled(self):
        config = valid_canary_config()

        report = validate_live_canary_config(config)

        self.assertTrue(report["ready"])
        self.assertEqual(report["status"], "ready")
        self.assertFalse(config["trading"]["enabled"])
        self.assertFalse(config["trading_enabled"])

    def test_cli_preflight_does_not_instantiate_or_connect_exchange(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "live-canary.yaml"
            config_path.write_text(
                textwrap.dedent(
                    """
                    trading:
                      mode: live
                      enabled: false
                      trading_enabled: false
                      single_trade_mode: true
                      live_reconciliation:
                        block_on_degraded: true
                      live_identity:
                        exchange: kalshi
                        environment: prod
                    trading_enabled: false
                    max_tradable_balance_usd: 25.0
                    max_position_size_usd: 2.0
                    risk:
                      daily_loss_limit_pct: 0.05
                      max_drawdown_pct: 0.1
                      max_open_positions: 3
                    """
                ).strip()
            )

            real_import = __import__

            def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
                if name == "bot.runner" or name.startswith("bot.exchanges"):
                    raise AssertionError(f"canary-preflight must not import {name}")
                return real_import(name, globals, locals, fromlist, level)

            import dotenv

            stdout = io.StringIO()
            argv = ["main.py", "canary-preflight", "--config", str(config_path)]
            original_main = sys.modules.pop("main", None)
            hostile_env = {
                "TRADING_ENABLED": "true",
                "DAILY_LOSS_LIMIT_PCT": "35",
                "OPENROUTER_API_KEY": "must-not-be-read",
            }
            try:
                with patch.object(sys, "argv", argv), patch.dict(os.environ, hostile_env, clear=True), patch(
                    "builtins.__import__", side_effect=guarded_import
                ), patch.object(dotenv, "load_dotenv", side_effect=AssertionError("canary-preflight must not load .env")), redirect_stdout(stdout):
                    main = importlib.import_module("main")
                    main.main()
            finally:
                sys.modules.pop("main", None)
                if original_main is not None:
                    sys.modules["main"] = original_main

        self.assertIn("Live canary preflight: READY", stdout.getvalue())

    @staticmethod
    def _failed_check_names(report: dict) -> set[str]:
        return {check["name"] for check in report["checks"] if not check["ok"]}


if __name__ == "__main__":
    unittest.main()
