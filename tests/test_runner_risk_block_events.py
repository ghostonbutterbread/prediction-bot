import json
import os
import tempfile
import unittest
from unittest.mock import patch

from bot.runner import PredictionBot


class FakeExchange:
    def get_balance(self):
        return 25.0


class RunnerRiskBlockEventTests(unittest.TestCase):
    def _make_bot(self, tmpdir):
        config = {
            "log_dir": tmpdir,
            "data_dir": tmpdir,
            "trading_enabled": False,
            "trading": {"mode": "live", "trading_enabled": False},
            "strategy": {
                "min_edge": 0.05,
                "min_confidence": 0.5,
                "enable_news": False,
                "enable_social": False,
                "enable_ai": False,
            },
        }
        bot = PredictionBot(config)
        bot.exchanges["kalshi"] = FakeExchange()
        bot.risk.state.current_balance = 25.0
        bot.risk.state.available_cash = 25.0
        bot.risk.state.trading_enabled = False
        return bot

    def test_risk_block_event_logged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            signal = {
                "exchange": "kalshi",
                "market_id": "KXHIGHNY-26APR29-T80",
                "question": "Will NYC high temperature be above 80 degrees?",
                "series_ticker": "KXHIGHNY",
                "event_ticker": "KXHIGHNY-26APR29",
                "direction": "BUY_YES",
                "market_price": 0.40,
                "yes_price": 0.40,
                "no_price": 0.60,
                "model_probability": 0.70,
                "edge": 0.30,
                "confidence": 0.90,
            }

            with patch.object(bot.kelly, "calculate", return_value=5.0):
                result = bot._process_signal(signal)
                bot._log_risk_block_event(signal, result)

            with open(f"{tmpdir}/live/risk_blocks.jsonl") as f:
                entries = [json.loads(line) for line in f if line.strip()]

            self.assertEqual(entries[0]["schema_name"], "execution_audit_row")
            self.assertEqual(entries[0]["status"], "rejected")
            self.assertEqual(entries[0]["lifecycle_state"], "risk_check_rejected")
            self.assertEqual(entries[0]["blocked_reason"], "trading_disabled")
            self.assertEqual(entries[0]["decision_reason_code"], "trading_disabled")
            self.assertTrue(entries[0]["trade_id"].startswith("risk-block:KXHIGHNY-26APR29-T80:trading_disabled:"))

    def test_scan_log_includes_canonical_candidate_summaries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            signals = [
                {
                    "exchange": "kalshi",
                    "market_id": "KXHIGHNY-26APR29-T80",
                    "question": "Will NYC high temperature be above 80 degrees?",
                    "series_ticker": "KXHIGHNY",
                    "event_ticker": "KXHIGHNY-26APR29",
                    "direction": "BUY_YES",
                    "market_price": 0.40,
                    "yes_price": 0.40,
                    "no_price": 0.60,
                    "best_yes_ask": 0.41,
                    "model_probability": 0.70,
                    "edge": 0.30,
                    "confidence": 0.90,
                    "market_group": "weather",
                },
                {
                    "exchange": "kalshi",
                    "market_id": "KXLOWNY-26APR29-T60",
                    "question": "Will NYC low temperature be below 60 degrees?",
                    "series_ticker": "KXLOWNY",
                    "event_ticker": "KXLOWNY-26APR29",
                    "direction": "BUY_NO",
                    "market_price": 0.62,
                    "yes_price": 0.38,
                    "no_price": 0.62,
                    "best_no_ask": 0.63,
                    "model_probability": 0.66,
                    "edge": 0.12,
                    "confidence": 0.77,
                    "market_group": "weather",
                },
            ]

            bot._log_scan(signals, trades=0, blocked_reasons={"risk_trading_paused_by_operator": 2})

            scan_files = [name for name in os.listdir(f"{tmpdir}/live") if name.startswith("scans_")]
            with open(f"{tmpdir}/live/{scan_files[0]}") as f:
                payload = json.loads(f.readline())

            self.assertEqual(payload["signals"], 2)
            self.assertIn("top_signals", payload)
            self.assertIn("candidate_summaries", payload)
            self.assertEqual(len(payload["candidate_summaries"]), 2)
            self.assertEqual(payload["candidate_summaries"][0]["schema_name"], "execution_audit_row")
            self.assertEqual(payload["candidate_summaries"][0]["status"], "candidate")
            self.assertEqual(payload["candidate_summaries"][0]["execution_snapshot_source"], "book")
            self.assertEqual(payload["candidate_summaries"][1]["direction"], "BUY_NO")
            self.assertEqual(payload["candidate_summaries"][1]["execution_snapshot_source"], "book")


if __name__ == "__main__":
    unittest.main()
