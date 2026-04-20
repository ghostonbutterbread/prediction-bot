import tempfile
import unittest

from bot.risk import RiskManager


class RiskConfigModeTests(unittest.TestCase):
    def test_trading_mode_live_overrides_paper_mode_env_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            risk = RiskManager(
                {
                    "data_dir": tmpdir,
                    "starting_balance": 100.0,
                    "trading": {"mode": "live", "trading_enabled": True},
                }
            )
            self.assertTrue(risk.is_live)

    def test_trading_nested_flag_is_source_of_truth(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            risk = RiskManager(
                {
                    "data_dir": tmpdir,
                    "starting_balance": 100.0,
                    "trading_enabled": True,
                    "trading": {"mode": "paper", "trading_enabled": False},
                }
            )
            self.assertFalse(risk.trading_enabled)
            self.assertFalse(risk.state.trading_enabled)


if __name__ == "__main__":
    unittest.main()
