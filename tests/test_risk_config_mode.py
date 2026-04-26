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

    def test_event_retrade_settings_load_from_nested_risk_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            risk = RiskManager(
                {
                    "data_dir": tmpdir,
                    "starting_balance": 100.0,
                    "risk": {
                        "max_event_exposure_pct": 0.2,
                        "max_event_positions": 5,
                        "retrade_edge_premium": 0.03,
                        "strict_event_overlap": False,
                    },
                }
            )
            self.assertEqual(risk.max_event_exposure_pct, 0.2)
            self.assertEqual(risk.max_event_positions, 5)
            self.assertEqual(risk.retrade_edge_premium, 0.03)
            self.assertFalse(risk.strict_event_overlap)


if __name__ == "__main__":
    unittest.main()
