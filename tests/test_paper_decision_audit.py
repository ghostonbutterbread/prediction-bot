import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.file_ops import load_jsonl
from bot.simulator import Simulator


class PaperDecisionAuditTests(unittest.TestCase):
    def _simulator(self, tmpdir: str) -> Simulator:
        return Simulator(
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

    def _signal(self, **overrides):
        signal = {
            "market_id": "KXHIGHNY-260506-T71",
            "question": "Will the high temperature in New York exceed 71 degrees?",
            "exchange": "kalshi",
            "direction": "BUY_YES",
            "model_probability": 0.7,
            "market_price": 0.4,
            "yes_market_price": 0.4,
            "no_market_price": 0.6,
            "edge": 0.3,
            "confidence": 0.9,
            "category": "KXHIGHNY",
            "market_family": "daily_temperature",
            "signals": {},
        }
        signal.update(overrides)
        return signal

    def test_paper_decision_audit_rows_link_shared_candidate_and_accounting_ref(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sim = self._simulator(tmpdir)
            dataset_path = Path(tmpdir) / "prediction_lab" / "market_snapshots.jsonl"
            signal = self._signal(
                shared_candidate_id="candidate-1",
                candidate_dataset_path=str(dataset_path),
            )

            with patch.object(sim.kelly, "calculate", return_value=10.0):
                trade = sim._create_trade(signal)

            self.assertIsNotNone(trade)
            run_rows = load_jsonl(sim.data_dir / "agent_runs.jsonl")
            decision_rows = load_jsonl(sim.data_dir / "agent_decisions.jsonl")

        self.assertEqual(len(run_rows), 1)
        self.assertEqual(run_rows[0]["agent_id"], "paper")
        self.assertEqual(run_rows[0]["runtime"], "paper")
        self.assertEqual(run_rows[0]["decision_ledger_path"], str(Path(tmpdir) / "paper" / "agent_decisions.jsonl"))
        self.assertEqual(run_rows[0]["wallet_id"], "stable_paper")
        self.assertEqual(run_rows[0]["policy_id"], "stable")
        self.assertEqual(run_rows[0]["wallet_namespace"], "paper_stable")
        self.assertEqual(run_rows[0]["accounting_root"], str(Path(tmpdir) / "paper"))
        self.assertEqual(run_rows[0]["risk_state_path"], str(Path(tmpdir) / "paper" / "risk_state.json"))
        self.assertEqual(run_rows[0]["session_path"], str(Path(tmpdir) / "paper" / f"sim_{sim.session_id}.json"))
        self.assertFalse(run_rows[0]["places_live_orders"])
        self.assertTrue(run_rows[0]["mutates_accounting"])
        self.assertEqual(len(decision_rows), 1)
        row = decision_rows[0]
        self.assertEqual(row["shared_candidate_id"], "candidate-1")
        self.assertNotIn("legacy_candidate_identity", row)
        self.assertEqual(row["candidate_dataset_path"], str(dataset_path))
        self.assertEqual(row["decision_role"], "paper")
        self.assertEqual(row["wallet_id"], "stable_paper")
        self.assertEqual(row["policy_id"], "stable")
        self.assertEqual(row["accounting_ref"]["wallet_id"], "stable_paper")
        self.assertEqual(row["accounting_ref"]["policy_id"], "stable")
        self.assertEqual(row["accounting_ref"]["trade_id"], trade.id)
        self.assertEqual(row["accounting_ref"]["namespace"], str(Path(tmpdir) / "paper"))
        self.assertEqual(row["accounting_ref"]["wallet_namespace"], "paper_stable")
        self.assertEqual(row["accounting_ref"]["namespace"], run_rows[0]["accounting_namespace"])
        self.assertEqual(row["accounting_ref"]["root_path"], str(Path(tmpdir) / "paper"))
        self.assertEqual(row["accounting_ref"]["risk_state_path"], str(Path(tmpdir) / "paper" / "risk_state.json"))
        self.assertEqual(row["accounting_ref"]["session_path"], str(Path(tmpdir) / "paper" / f"sim_{sim.session_id}.json"))
        self.assertTrue(row["accounting_ref"]["mutates_balance"])
        self.assertTrue(row["accounting_ref"]["mutates_accounting"])
        self.assertFalse(row["accounting_ref"]["places_live_orders"])
        self.assertFalse(row["mutation_contract"]["mutates_shared_candidate"])
        self.assertTrue(row["mutation_contract"]["mutates_accounting"])
        self.assertEqual(row["mutation_contract"]["accounting_mutation_scope"], "paper_only")
        self.assertFalse(row["mutation_contract"]["places_orders"])

    def test_paper_decision_audit_rows_use_explicit_legacy_identity_without_shared_candidate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sim = self._simulator(tmpdir)
            signal = self._signal()

            with self.assertLogs("bot.paper_decision_audit", level="WARNING") as logs:
                with patch.object(sim.kelly, "calculate", return_value=10.0):
                    trade = sim._create_trade(signal)

            decision_rows = load_jsonl(sim.data_dir / "agent_decisions.jsonl")

        self.assertIsNotNone(trade)
        self.assertTrue(any("legacy candidate identity" in message for message in logs.output))
        self.assertEqual(len(decision_rows), 1)
        row = decision_rows[0]
        self.assertNotIn("shared_candidate_id", row)
        self.assertEqual(row["candidate_dataset_identity"], f"legacy:paper_session:{sim.session_id}")
        self.assertEqual(row["legacy_candidate_identity"]["identity_type"], "legacy_paper_signal")
        self.assertEqual(row["legacy_candidate_identity"]["session_id"], sim.session_id)
        self.assertEqual(row["legacy_candidate_identity"]["market_id"], "KXHIGHNY-260506-T71")

    def test_paper_decision_audit_failure_does_not_block_paper_trade_or_accounting(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sim = self._simulator(tmpdir)
            signal = self._signal(shared_candidate_id="candidate-1")

            with patch("bot.paper_decision_audit.append_jsonl", side_effect=OSError("audit disk failed")):
                with self.assertLogs("bot.simulator", level="WARNING") as logs:
                    with patch.object(sim.kelly, "calculate", return_value=10.0):
                        trade = sim._create_trade(signal)

            risk_state = json.loads(Path(sim.risk.data_path).read_text())

        self.assertIsNotNone(trade)
        self.assertTrue(any("failed to append paper agent decision audit row" in message for message in logs.output))
        self.assertEqual(trade.position_size, 10.0)
        self.assertEqual(sim.available_cash, 90.0)
        self.assertEqual(sim.reserved_capital, 10.0)
        self.assertEqual(risk_state["available_cash"], 90.0)
        self.assertEqual(risk_state["reserved_capital"], 10.0)
        self.assertEqual(risk_state["open_positions"], 1)
        self.assertFalse((Path(tmpdir) / "paper" / "agent_decisions.jsonl").exists())
        self.assertFalse((Path(tmpdir) / "paper" / "reconciliation.jsonl").exists())
        self.assertFalse((Path(tmpdir) / "paper" / "lifecycle.jsonl").exists())


    def test_paper_agent_run_failure_does_not_block_simulator_construction(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("bot.paper_decision_audit.append_jsonl", side_effect=OSError("run audit failed")):
                with self.assertLogs("bot.simulator", level="WARNING") as logs:
                    sim = self._simulator(tmpdir)

            self.assertEqual(sim.runtime_mode, "paper")
            self.assertTrue(any("failed to append paper agent run audit row" in message for message in logs.output))
            self.assertFalse((Path(tmpdir) / "paper" / "agent_runs.jsonl").exists())

    def test_live_mode_simulator_does_not_create_paper_sidecars(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sim = Simulator(
                {
                    "data_dir": tmpdir,
                    "trading": {"mode": "live"},
                    "enable_social": False,
                    "strategy": {
                        "enable_news": False,
                        "enable_social": False,
                        "enable_ai": False,
                    },
                }
            )

            self.assertEqual(sim.runtime_mode, "live")
            self.assertTrue(str(sim.data_dir).endswith("/live"))
            self.assertFalse((Path(tmpdir) / "live" / "agent_runs.jsonl").exists())
            self.assertFalse((Path(tmpdir) / "paper" / "agent_runs.jsonl").exists())

    def test_rejected_paper_decision_audit_row_does_not_mutate_accounting(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sim = self._simulator(tmpdir)
            signal = self._signal(shared_candidate_id="candidate-rejected", edge=0.0)

            trade = sim._create_trade(signal)
            risk_state_path = Path(sim.risk.data_path)
            decision_rows = load_jsonl(sim.data_dir / "agent_decisions.jsonl")

        self.assertIsNone(trade)
        self.assertEqual(sim.available_cash, 100.0)
        self.assertEqual(sim.reserved_capital, 0.0)
        self.assertFalse(risk_state_path.exists())
        self.assertEqual(len(decision_rows), 1)
        row = decision_rows[0]
        self.assertEqual(row["shared_candidate_id"], "candidate-rejected")
        self.assertEqual(row["action"], "SKIP")
        self.assertEqual(row["wallet_id"], "stable_paper")
        self.assertEqual(row["policy_id"], "stable")
        self.assertFalse(row["accounting_ref"]["mutates_balance"])
        self.assertFalse(row["accounting_ref"]["mutates_accounting"])
        self.assertFalse(row["accounting_ref"]["places_live_orders"])
        self.assertFalse(row["mutation_contract"]["mutates_accounting"])
        self.assertIsNone(row["accounting_ref"].get("trade_id"))

    def test_beta_shadow_runtime_uses_beta_wallet_contract_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sim = Simulator(
                {
                    "data_dir": str(Path(tmpdir) / "data" / "beta_shadow"),
                    "strategy_policy": {"version": "beta", "beta": {"mode": "shadow"}},
                    "enable_social": False,
                    "strategy": {
                        "enable_news": False,
                        "enable_social": False,
                        "enable_ai": False,
                    },
                }
            )
            signal = self._signal(shared_candidate_id="candidate-beta")

            with patch.object(sim.kelly, "calculate", return_value=10.0):
                trade = sim._create_trade(signal)

            run_rows = load_jsonl(sim.data_dir / "agent_runs.jsonl")
            decision_rows = load_jsonl(sim.data_dir / "agent_decisions.jsonl")

        self.assertIsNotNone(trade)
        self.assertEqual(str(sim.data_dir), str(Path(tmpdir) / "data" / "beta_shadow" / "paper"))
        self.assertEqual(run_rows[0]["wallet_id"], "beta_paper")
        self.assertEqual(run_rows[0]["policy_id"], "beta")
        self.assertEqual(run_rows[0]["wallet_namespace"], "paper_beta")
        self.assertEqual(decision_rows[0]["wallet_id"], "beta_paper")
        self.assertEqual(decision_rows[0]["policy_id"], "beta")
        self.assertEqual(decision_rows[0]["accounting_ref"]["wallet_id"], "beta_paper")
        self.assertEqual(decision_rows[0]["accounting_ref"]["policy_id"], "beta")
        self.assertEqual(decision_rows[0]["accounting_ref"]["namespace"], str(Path(tmpdir) / "data" / "beta_shadow" / "paper"))
        self.assertEqual(decision_rows[0]["accounting_ref"]["wallet_namespace"], "paper_beta")
        self.assertEqual(decision_rows[0]["accounting_ref"]["namespace"], run_rows[0]["accounting_namespace"])
        self.assertEqual(decision_rows[0]["accounting_ref"]["root_path"], str(Path(tmpdir) / "data" / "beta_shadow" / "paper"))
    def test_beta_mode_off_runtime_stays_on_stable_wallet_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sim = Simulator(
                {
                    "data_dir": str(Path(tmpdir) / "data"),
                    "strategy_policy": {"version": "beta", "beta": {"mode": "off"}},
                    "enable_social": False,
                    "strategy": {
                        "enable_news": False,
                        "enable_social": False,
                        "enable_ai": False,
                    },
                }
            )
            signal = self._signal(shared_candidate_id="candidate-beta-off")

            with patch.object(sim.kelly, "calculate", return_value=10.0):
                trade = sim._create_trade(signal)

            run_rows = load_jsonl(sim.data_dir / "agent_runs.jsonl")
            decision_rows = load_jsonl(sim.data_dir / "agent_decisions.jsonl")

        self.assertIsNotNone(trade)
        self.assertEqual(str(sim.data_dir), str(Path(tmpdir) / "data" / "paper"))
        self.assertEqual(run_rows[0]["wallet_id"], "stable_paper")
        self.assertEqual(run_rows[0]["policy_id"], "stable")
        self.assertEqual(decision_rows[0]["wallet_id"], "stable_paper")
        self.assertEqual(decision_rows[0]["policy_id"], "stable")
        self.assertEqual(decision_rows[0]["accounting_ref"]["namespace"], str(Path(tmpdir) / "data" / "paper"))
        self.assertEqual(decision_rows[0]["accounting_ref"]["wallet_namespace"], "paper_stable")



if __name__ == "__main__":
    unittest.main()
