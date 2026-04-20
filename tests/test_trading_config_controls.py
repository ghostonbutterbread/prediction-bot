import tempfile
import unittest

from bot.config import _apply_env_overrides
from bot.risk import RiskManager


class TradingConfigControlTests(unittest.TestCase):
    def test_env_overrides_populate_trading_section(self):
        config = {"trading": {"mode": "paper", "enabled": True}}

        import os
        old_mode = os.environ.get("TRADING_MODE")
        old_enabled = os.environ.get("TRADING_ENABLED")
        try:
            os.environ["TRADING_MODE"] = "live"
            os.environ["TRADING_ENABLED"] = "false"
            updated = _apply_env_overrides(config)
        finally:
            if old_mode is None:
                os.environ.pop("TRADING_MODE", None)
            else:
                os.environ["TRADING_MODE"] = old_mode
            if old_enabled is None:
                os.environ.pop("TRADING_ENABLED", None)
            else:
                os.environ["TRADING_ENABLED"] = old_enabled

        self.assertEqual(updated["trading"]["mode"], "live")
        self.assertFalse(updated["trading"]["enabled"])

    def test_risk_manager_prefers_trading_enabled_field(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            risk = RiskManager(
                {
                    "data_dir": tmpdir,
                    "trading_enabled": True,
                    "trading": {"mode": "paper", "enabled": False},
                }
            )
            self.assertFalse(risk.trading_enabled)
            self.assertFalse(risk.state.trading_enabled)


if __name__ == "__main__":
    unittest.main()
