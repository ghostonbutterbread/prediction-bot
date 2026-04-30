import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot.live_execution import RunnerLiveExecutionAdapter
from bot.runner import PredictionBot
from bot.simulator import Simulator


class ArtifactExchange:
    def __init__(self, *, environment="demo", api_key_id="test-key", private_key_path="/tmp/test-key.pem"):
        self.orders = []
        self.environment = environment
        self.api_key_id = api_key_id
        self.private_key_path = private_key_path

    def describe_runtime_identity(self):
        return {
            "exchange": "kalshi",
            "environment": self.environment,
            "api_key_id": self.api_key_id,
            "private_key_path": self.private_key_path,
        }

    def get_balance(self):
        return 25.0

    def get_market_bid_ask(self, market_id):
        return {
            "best_yes_ask": 0.40,
            "best_no_ask": 0.60,
            "best_yes_bid": 0.39,
            "best_no_bid": 0.59,
        }

    def place_order(self, market_id, side, price, size):
        self.orders.append({"market_id": market_id, "side": side, "price": price, "size": size})
        return SimpleNamespace(id=f"ord-{len(self.orders)}", status="open")


class Phase5DecisionArtifactTests(unittest.TestCase):
    def _signal(self):
        return {
            "market_id": "phase5-market",
            "question": "Will Phase 5 stay aligned?",
            "exchange": "kalshi",
            "direction": "BUY_YES",
            "model_probability": 0.70,
            "market_price": 0.40,
            "yes_price": 0.40,
            "no_price": 0.60,
            "edge": 0.30,
            "confidence": 0.90,
            "signals": {},
        }

    def _make_live_bot(self, tmpdir):
        bot = PredictionBot(
            {
                "log_dir": tmpdir,
                "data_dir": tmpdir,
                "trading": {"mode": "live", "enabled": True},
                "strategy": {
                    "min_edge": 0.05,
                    "min_confidence": 0.5,
                    "enable_news": False,
                    "enable_social": False,
                    "enable_ai": False,
                },
                "max_tradable_balance_usd": 10.0,
                "max_position_size_usd": 4.0,
            }
        )
        bot.risk.state.current_balance = 25.0
        bot.risk.state.available_cash = 25.0
        bot.risk.state.peak_balance = 25.0
        bot.risk.state.session_starting_balance = 25.0
        bot.risk.state.session_peak_balance = 25.0
        bot.risk.state.max_drawdown_halt = False
        return bot

    def test_paper_and_live_audit_rows_share_pre_execution_artifact_shape(self):
        signal = self._signal()
        with tempfile.TemporaryDirectory() as tmpdir:
            sim = Simulator(
                {
                    "data_dir": tmpdir,
                    "enable_social": False,
                    "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
                }
            )
            sim.available_cash = 25.0
            sim.risk.state.available_cash = 25.0
            with patch.object(sim.kelly, "calculate", return_value=2.0):
                paper_trade = sim._create_trade(dict(signal))

        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_live_bot(tmpdir)
            adapter = RunnerLiveExecutionAdapter(bot)
            exchange = ArtifactExchange()
            initial_decision = SimpleNamespace(
                action="BUY_YES",
                approved=True,
                position_size=2.0,
                entry_price=0.40,
                reason="approved",
                reason_code="approved",
                requested_position_size=2.0,
                win_probability=0.70,
                reasoning={},
            )
            result = adapter.execute(dict(signal), initial_decision, exchange)

        self.assertIsNotNone(paper_trade)
        self.assertIsNotNone(result)
        paper_artifact = paper_trade.decision_artifact
        live_artifact = bot.trade_history[0]["decision_artifact"]

        self.assertEqual(set(paper_artifact.keys()), set(live_artifact.keys()))
        self.assertEqual(set(paper_artifact["shared_core_decision"].keys()), set(live_artifact["shared_core_decision"].keys()))
        self.assertEqual(set(paper_artifact["account_state_snapshot"].keys()), set(live_artifact["account_state_snapshot"].keys()))
        self.assertEqual(set(paper_artifact["source_context"].keys()), set(live_artifact["source_context"].keys()))
        self.assertEqual(paper_artifact["artifact_kind"], "pre_execution_decision")
        self.assertEqual(live_artifact["artifact_kind"], "pre_execution_decision")
        self.assertEqual(paper_artifact["mode"], "paper_portfolio")
        self.assertEqual(live_artifact["mode"], "live")
        self.assertEqual(paper_artifact["market_id"], live_artifact["market_id"])

    def test_live_identity_gate_remains_adapter_owned_and_records_artifact_without_placement(self):
        signal = self._signal()
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_live_bot(tmpdir)
            bot.config["trading"]["live_identity"] = {
                "exchange": "kalshi",
                "environment": "prod",
                "api_key_id": "expected-key",
                "private_key_path": "/keys/prod.pem",
            }
            adapter = RunnerLiveExecutionAdapter(bot)
            exchange = ArtifactExchange(environment="demo", api_key_id="wrong-key", private_key_path="/keys/demo.pem")
            decision = SimpleNamespace(
                action="BUY_YES",
                approved=True,
                position_size=2.0,
                entry_price=0.40,
                reason="approved",
                reason_code="approved",
                requested_position_size=2.0,
                win_probability=0.70,
                reasoning={},
            )

            result = adapter.execute(signal, decision, exchange)

        self.assertEqual(result["blocked_reason"], "live_identity_mismatch")
        self.assertEqual(exchange.orders, [])
        artifact = bot.trade_history[0]["decision_artifact"]
        self.assertEqual(artifact["mode"], "live")
        self.assertEqual(artifact["final_action"], "SKIP")
        self.assertEqual(artifact["final_reason_code"], "live_identity_mismatch")
        self.assertIsNone(artifact["trade_context"])
        self.assertIn("live_identity", artifact["shared_core_decision"]["reasoning"])
        runtime_identity = artifact["shared_core_decision"]["reasoning"]["live_identity"]["runtime"]
        self.assertEqual(runtime_identity["api_key_id"], "<redacted>")
        self.assertEqual(runtime_identity["private_key_path"], "<redacted>")

    def test_runner_live_identity_gate_threads_artifact_to_risk_block_row(self):
        signal = self._signal()
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_live_bot(tmpdir)
            bot.config["trading"]["live_identity"] = {
                "exchange": "kalshi",
                "environment": "prod",
                "api_key_id": "expected-key",
                "private_key_path": "/keys/prod.pem",
            }
            exchange = ArtifactExchange(environment="demo", api_key_id="wrong-key", private_key_path="/keys/demo.pem")
            bot.exchanges["kalshi"] = exchange
            bot.startup_reconciliation_status["kalshi"] = {
                "completed": True,
                "status": "safe",
                "runtime_state": "safe",
                "reconciliation_verdict": "safe",
                "reconciliation_issues": [],
            }

            with patch.object(bot.kelly, "calculate", return_value=2.0):
                result = bot._process_signal(signal)
                bot._log_risk_block_event(signal, result)

            row = json.loads((Path(tmpdir) / "live" / "risk_blocks.jsonl").read_text().strip())

        self.assertEqual(result["blocked_reason"], "live_identity_mismatch")
        self.assertEqual(exchange.orders, [])
        self.assertIn("decision_artifact", result)
        self.assertIn("decision_artifact", row)
        self.assertEqual(row["decision_artifact"]["mode"], "live")
        self.assertEqual(row["decision_artifact"]["artifact_kind"], "pre_execution_decision")
        self.assertEqual(row["decision_artifact"]["final_action"], "SKIP")
        self.assertEqual(row["decision_artifact"]["final_reason_code"], "live_identity_mismatch")
        self.assertEqual(row["decision_artifact"]["final_reason_code"], row["decision_reason_code"])

    def test_runner_live_revalidation_skip_threads_artifact_to_risk_block_row(self):
        class HighAskExchange(ArtifactExchange):
            def get_market_bid_ask(self, market_id):
                return {
                    "best_yes_ask": 0.80,
                    "best_no_ask": 0.20,
                    "best_yes_bid": 0.79,
                    "best_no_bid": 0.19,
                }

        signal = self._signal()
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_live_bot(tmpdir)
            exchange = HighAskExchange()
            bot.exchanges["kalshi"] = exchange
            bot.startup_reconciliation_status["kalshi"] = {
                "completed": True,
                "status": "safe",
                "runtime_state": "safe",
                "reconciliation_verdict": "safe",
                "reconciliation_issues": [],
            }

            with patch.object(bot.kelly, "calculate", return_value=2.0):
                result = bot._process_signal(signal)
                bot._log_risk_block_event(signal, result)
            row = json.loads((Path(tmpdir) / "live" / "risk_blocks.jsonl").read_text().strip())

        self.assertEqual(exchange.orders, [])
        self.assertEqual(result["blocked_reason"], "entry_price_above_cap")
        self.assertIn("decision_artifact", result)
        self.assertIn("decision_artifact", row)
        self.assertEqual(row["decision_artifact"]["final_reason_code"], "entry_price_above_cap")
        self.assertEqual(row["decision_artifact"]["final_reason_code"], row["decision_reason_code"])

    def test_runner_live_shared_decision_skip_threads_artifact_to_risk_block_row(self):
        signal = self._signal()
        signal["market_price"] = 0.80
        signal["yes_price"] = 0.80
        signal["no_price"] = 0.20
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_live_bot(tmpdir)
            exchange = ArtifactExchange()
            bot.exchanges["kalshi"] = exchange
            bot.startup_reconciliation_status["kalshi"] = {
                "completed": True,
                "status": "safe",
                "runtime_state": "safe",
                "reconciliation_verdict": "safe",
                "reconciliation_issues": [],
            }

            result = bot._process_signal(signal)
            bot._log_risk_block_event(signal, result)
            row = json.loads((Path(tmpdir) / "live" / "risk_blocks.jsonl").read_text().strip())

        self.assertEqual(exchange.orders, [])
        self.assertEqual(row["status"], "rejected")
        self.assertIn("decision_artifact", row)
        self.assertEqual(row["decision_artifact"]["mode"], "live")
        self.assertEqual(row["decision_artifact"]["artifact_kind"], "pre_execution_decision")
        self.assertEqual(row["decision_artifact"]["final_action"], "SKIP")
        self.assertEqual(row["decision_artifact"]["final_reason_code"], row["decision_reason_code"])


if __name__ == "__main__":
    unittest.main()
