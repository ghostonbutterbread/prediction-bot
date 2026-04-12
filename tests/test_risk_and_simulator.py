import json
import tempfile
import unittest
from pathlib import Path

from bot.risk import RiskManager
from bot.simulator import Simulator


class RiskManagerTests(unittest.TestCase):
    def test_max_exposure_caps_size_instead_of_full_reject(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            risk = RiskManager(
                {
                    "data_dir": tmpdir,
                    "starting_balance": 100.0,
                    "max_exposure_pct": 0.40,
                }
            )
            risk.state.current_balance = 100.0
            risk.state.total_exposure = 39.0

            decision = risk.check_trade({"question": "Will BTC rise?"}, 5.0)

            self.assertTrue(decision.approved)
            self.assertEqual(decision.adjusted_size, 1.0)
            self.assertTrue(any("Exposure headroom capped size" in warning for warning in decision.warnings))


class SimulatorSessionTests(unittest.TestCase):
    def test_load_session_discards_zero_sized_trade_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            session_file = data_dir / "sim_20260321_193703.json"
            session = {
                "session_id": "20260321_193703",
                "starting_balance": 100.0,
                "balance": 105.0,
                "scan_count": 12,
                "trades": [
                    {
                        "id": "bad-1",
                        "timestamp": "2026-03-21T00:00:00+00:00",
                        "exchange": "kalshi",
                        "market_id": "bad-market",
                        "question": "Bad market",
                        "direction": "BUY_YES",
                        "model_probability": 0.6,
                        "market_price": 0.2,
                        "edge": 0.4,
                        "confidence": 0.8,
                        "position_size": 0.0,
                        "signals": {},
                        "resolved": True,
                        "pnl": 0.0,
                    },
                    {
                        "id": "good-1",
                        "timestamp": "2026-03-21T01:00:00+00:00",
                        "exchange": "kalshi",
                        "market_id": "good-market",
                        "question": "Good market",
                        "direction": "BUY_YES",
                        "model_probability": 0.6,
                        "market_price": 0.2,
                        "edge": 0.4,
                        "confidence": 0.8,
                        "position_size": 5.0,
                        "signals": {},
                        "resolved": False,
                        "pnl": None,
                    },
                ],
            }
            session_file.write_text(json.dumps(session))

            sim = Simulator(
                {
                    "data_dir": tmpdir,
                    "enable_social": False,
                    "strategy": {
                        "enable_news": False,
                        "enable_social": False,
                        "enable_ai": False,
                    },
                }
            )

            self.assertEqual(len(sim.trades), 1)
            self.assertEqual(sim.trades[0].id, "good-1")
            self.assertEqual(sim.risk.state.open_positions, 1)
            self.assertEqual(sim.risk.state.total_exposure, 5.0)


if __name__ == "__main__":
    unittest.main()
