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

    def test_parity_paper_mode_gets_explicit_label(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            risk = RiskManager(
                {
                    "data_dir": tmpdir,
                    "starting_balance": 100.0,
                    "trading": {"mode": "paper", "trading_enabled": True},
                    "parity_mode": {"enabled": True},
                }
            )
            status = risk.get_status()
            self.assertEqual(status["mode"], "🟠 PARITY PAPER")
            self.assertEqual(status["mode_label"], "parity paper")
            self.assertEqual(status["risk_preset_mode"], "paper")
            self.assertEqual(status["parity_comparison_mode"], "production")

    def test_identical_risk_live_mode_uses_paper_preset_and_label(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            risk = RiskManager(
                {
                    "data_dir": tmpdir,
                    "starting_balance": 100.0,
                    "trading": {"mode": "live", "trading_enabled": True},
                    "parity_mode": {"enabled": True, "comparison_mode": "identical_risk"},
                }
            )
            status = risk.get_status()
            self.assertTrue(risk.is_live)
            self.assertEqual(status["mode"], "🟣 IDENTICAL-RISK COMPARISON")
            self.assertEqual(status["mode_label"], "identical-risk comparison")
            self.assertEqual(status["risk_preset_mode"], "paper")
            self.assertEqual(risk.kelly_fraction, 0.50)
            self.assertEqual(risk.max_bet_pct, 0.10)

    def test_identical_risk_live_mode_uses_paper_preset_and_label_when_parity_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            risk = RiskManager(
                {
                    "data_dir": tmpdir,
                    "starting_balance": 100.0,
                    "trading": {"mode": "live", "trading_enabled": True},
                    "parity_mode": {"enabled": False, "comparison_mode": "identical_risk"},
                }
            )
            status = risk.get_status()
            self.assertTrue(risk.is_live)
            self.assertEqual(status["mode"], "🟣 IDENTICAL-RISK COMPARISON")
            self.assertEqual(status["mode_label"], "identical-risk comparison")
            self.assertEqual(status["risk_preset_mode"], "paper")
            self.assertEqual(status["parity_comparison_mode"], "identical_risk")
            self.assertEqual(risk.kelly_fraction, 0.50)
            self.assertEqual(risk.max_bet_pct, 0.10)

    def test_production_live_mode_uses_live_preset_and_label_when_parity_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            risk = RiskManager(
                {
                    "data_dir": tmpdir,
                    "starting_balance": 100.0,
                    "trading": {"mode": "live", "trading_enabled": True},
                    "parity_mode": {"enabled": False, "comparison_mode": "production"},
                }
            )
            status = risk.get_status()
            self.assertTrue(risk.is_live)
            self.assertEqual(status["mode"], "🔴 LIVE")
            self.assertEqual(status["mode_label"], "live")
            self.assertEqual(status["risk_preset_mode"], "live")
            self.assertEqual(status["parity_comparison_mode"], "production")
            self.assertEqual(risk.kelly_fraction, 0.25)
            self.assertEqual(risk.max_bet_pct, 0.05)

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
