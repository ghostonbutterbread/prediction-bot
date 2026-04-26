import json
import tempfile
import unittest
from pathlib import Path

from bot.parity_audit import build_parity_view
from bot.simulator import Simulator


class PaperSessionBackwardCompatTests(unittest.TestCase):
    def test_simulator_loads_legacy_session_without_parity_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            session_file = data_dir / "sim_20260423_000000.json"
            session_file.write_text(
                json.dumps(
                    {
                        "session_id": "20260423_000000",
                        "starting_balance": 100.0,
                        "balance": 103.0,
                        "available_cash": 98.0,
                        "reserved_capital": 5.0,
                        "scan_count": 9,
                        "trades": [
                            {
                                "id": "legacy-1",
                                "timestamp": "2026-04-23T00:00:00+00:00",
                                "exchange": "kalshi",
                                "market_id": "legacy-market",
                                "question": "Legacy market",
                                "direction": "BUY_YES",
                                "model_probability": 0.62,
                                "market_price": 0.41,
                                "edge": 0.21,
                                "confidence": 0.77,
                                "position_size": 5.0,
                                "signals": {},
                                "resolved": False,
                                "pnl": None,
                            }
                        ],
                    }
                )
            )

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
            trade = sim.trades[0]
            self.assertEqual(trade.id, "legacy-1")
            self.assertFalse(getattr(trade, "parity_mode_enabled", False))
            self.assertFalse(getattr(trade, "execution_revalidated", False))
            self.assertEqual(sim.available_cash, 98.0)
            self.assertEqual(sim.reserved_capital, 5.0)

    def test_parity_view_normalizes_legacy_paper_session_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            (data_dir / "paper").mkdir(parents=True)
            (data_dir / "live").mkdir(parents=True)
            (data_dir / "paper" / "sim_20260423_000000.json").write_text(
                json.dumps(
                    {
                        "session_id": "20260423_000000",
                        "trades": [
                            {
                                "id": "legacy-2",
                                "timestamp": "2026-04-23T01:00:00+00:00",
                                "exchange": "kalshi",
                                "market_id": "legacy-market-2",
                                "question": "Legacy market 2",
                                "direction": "BUY_NO",
                                "market_price": 0.37,
                                "position_size": 3.0,
                                "decision_reason_code": "approved",
                                "resolved": False,
                            }
                        ],
                    }
                )
            )

            view = build_parity_view(data_dir)

            self.assertEqual(view["paper_summary"]["total_rows"], 1)
            row = view["paper_rows"][0]
            self.assertEqual(row["trade_id"], "legacy-2")
            self.assertFalse(row["parity_mode_enabled"])
            self.assertFalse(row["execution_revalidated"])
            self.assertEqual(row["entry_price"], 0.37)
            self.assertEqual(row["requested_size"], 3.0)


if __name__ == "__main__":
    unittest.main()
