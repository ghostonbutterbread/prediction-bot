import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot.config import load_config
from bot.decision_pipeline import DecisionPipelineEvaluator
from bot.file_ops import append_jsonl, load_jsonl
from bot.paper_wallet_runner import (
    build_paper_wallet_runner_config,
    run_shared_candidate_paper_evaluation,
)
from bot.prediction_lab import PredictionLab
from bot.prediction_lab_replay import replay_from_paths
from bot.risk import RiskDecision
from bot.simulator import Simulator
from bot.strategies.enhanced import StrategyTrace


class FixedReplayStrategy:
    def __init__(self, signal: dict | None):
        self.signal = signal

    def analyze_market_with_trace(self, market, order_book=None):
        trace = StrategyTrace(
            raw_signals={"unit": {"provided": self.signal is not None}},
            accepted_signals={"unit": dict(self.signal)} if self.signal else {},
            rejected_signals={},
            ensemble_signal=dict(self.signal) if self.signal else None,
        )
        return (dict(self.signal) if self.signal else None), trace


class AllowReplayRisk:
    def check_trade(self, signal: dict, position_size: float, *, available_cash: float | None = None):
        return RiskDecision(
            approved=True,
            reason="Approved",
            adjusted_size=position_size,
            original_size=position_size,
        )


class PaperWalletRunnerTests(unittest.TestCase):
    def _config(self, tmpdir: str) -> dict:
        config_path = Path(tmpdir) / "config.yaml"
        config_path.write_text(
            f"""
runtime:
  base_dir: {Path(tmpdir) / "wallet_data"}
trading:
  mode: paper
strategy_policy:
  version: beta
  beta:
    mode: shadow
    features:
      weather_hidden_gem_evidence_card: true
      bucket_distribution_scoring: true
      hidden_gem_lane_gates: true
      lane_sizing_caps: true
strategy:
  enable_news: false
  enable_social: false
  enable_ai: false
"""
        )
        return load_config(config_path)

    def _market(self):
        return SimpleNamespace(
            id="KXHIGHNY-260513-T71",
            exchange="kalshi",
            question="Will the high temperature in New York exceed 71 degrees?",
            category="weather",
            yes_price=0.41,
            no_price=0.59,
            volume=1200,
            metadata={
                "market_group": "weather",
                "market_family": "daily_temperature",
                "series": "daily_temperature",
                "series_ticker": "KXHIGHNY",
                "event_ticker": "EVT-1",
                "market_route": {"group": "weather", "family": "daily_temperature", "allowed": True},
            },
        )

    def _signal(self):
        return {
            "direction": "BUY_YES",
            "model_probability": 0.67,
            "market_price": 0.41,
            "yes_market_price": 0.41,
            "no_market_price": 0.59,
            "edge": 0.26,
            "confidence": 0.91,
            "station_id": "KNYC",
            "source_as_of": "2026-05-13T12:00:00+00:00",
            "signals": {"unit": 0.67},
        }

    def _snapshot_row(self):
        lab = PredictionLab(
            {
                "data_dir": "/tmp/prediction-lab-fixture",
                "prediction_lab": {"enabled": True, "mode": "collector", "groups": ["weather"]},
                "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
            }
        )
        return lab._build_market_snapshot_row(
            "run-1",
            self._market(),
            self._signal(),
            decision_type="buy_yes",
            prediction_recorded=True,
            decision_artifact={
                "final_action": "BUY_YES",
                "final_reason_code": "approved",
                "strategy_signal": self._signal(),
                "shared_core_decision": {
                    "requested_position_size": 10.0,
                    "reason_code": "approved",
                },
            },
            observed_at="2026-05-13T12:00:01+00:00",
        )

    def test_build_paper_wallet_runner_config_materializes_stable_and_beta_roots_and_policies(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)

            stable = build_paper_wallet_runner_config(config, wallet_id="stable_paper")
            beta = build_paper_wallet_runner_config(config, wallet_id="beta_paper")

        self.assertEqual(stable["paper_wallets"]["active_wallet_id"], "stable_paper")
        self.assertEqual(beta["paper_wallets"]["active_wallet_id"], "beta_paper")
        self.assertEqual(stable["strategy_policy_normalized"]["version"], "stable")
        self.assertEqual(stable["strategy_policy_normalized"]["beta_mode"], "off")
        self.assertFalse(stable["strategy_policy_normalized"].is_active)
        self.assertEqual(beta["strategy_policy_normalized"]["version"], "beta")
        self.assertEqual(beta["strategy_policy_normalized"]["beta_mode"], "enforce")
        self.assertTrue(beta["strategy_policy_normalized"].is_enforce)
        self.assertEqual(Path(stable["data_dir"]), Path(tmpdir) / "wallet_data" / "paper")
        self.assertEqual(Path(beta["data_dir"]), Path(tmpdir) / "wallet_data" / "beta_shadow" / "paper")
        self.assertEqual(Path(stable["runtime"]["base_dir"]), Path(tmpdir) / "wallet_data")
        self.assertEqual(Path(beta["runtime"]["base_dir"]), Path(tmpdir) / "wallet_data" / "beta_shadow")
        self.assertEqual(Path(stable["paper_wallets"]["stable_paper"]["root_dir"]), Path(tmpdir) / "wallet_data" / "paper")
        self.assertEqual(Path(beta["paper_wallets"]["beta_paper"]["root_dir"]), Path(tmpdir) / "wallet_data" / "beta_shadow" / "paper")

    def test_wallet_paper_root_does_not_load_parent_legacy_session_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir) / "wallet_data"
            parent.mkdir(parents=True)
            (parent / "sim_legacy.json").write_text(
                json.dumps(
                    {
                        "session_id": "legacy",
                        "starting_balance": 100.0,
                        "balance": 100.0,
                        "available_cash": 90.0,
                        "reserved_capital": 10.0,
                        "scan_count": 7,
                        "trades": [],
                    }
                )
            )

            sim = Simulator(
                {
                    "data_dir": str(parent / "paper"),
                    "paper_wallets": {"active_wallet_id": "stable_paper"},
                    "enable_social": False,
                    "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
                }
            )

        self.assertNotEqual(sim.session_id, "legacy")
        self.assertEqual(sim.scan_count, 0)
        self.assertEqual(sim.available_cash, 100.0)
        self.assertEqual(sim.reserved_capital, 0.0)

    def test_run_shared_candidate_paper_evaluation_uses_isolated_wallet_roots_for_same_candidate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            row = self._snapshot_row()
            append_jsonl(dataset_path, row)

            with patch("bot.simulator.KellySizer.calculate", return_value=10.0):
                result = run_shared_candidate_paper_evaluation(dataset_path, config=config)

            candidate_id = row["shared_candidate_id"]
            stable = result.wallet_runs["stable_paper"]
            beta = result.wallet_runs["beta_paper"]
            stable_decisions = load_jsonl(Path(stable.agent_decision_path))
            beta_decisions = load_jsonl(Path(beta.agent_decision_path))
            stable_runs = load_jsonl(Path(stable.agent_run_path))
            beta_runs = load_jsonl(Path(beta.agent_run_path))
            stable_risk = json.loads(Path(stable.risk_state_path).read_text())
            beta_risk = json.loads(Path(beta.risk_state_path).read_text())

        self.assertEqual(result.loaded_row_count, 1)
        self.assertEqual(result.accepted_candidate_count, 1)
        self.assertEqual(result.shared_candidate_ids, (candidate_id,))
        self.assertEqual(stable.shared_candidate_ids, (candidate_id,))
        self.assertEqual(beta.shared_candidate_ids, (candidate_id,))
        self.assertEqual(len(stable.accepted_trade_ids), 1)
        self.assertEqual(len(beta.accepted_trade_ids), 1)
        self.assertEqual(stable.policy, "normal")
        self.assertEqual(beta.policy, "beta_enforce")
        self.assertEqual(Path(stable.data_dir), Path(tmpdir) / "wallet_data" / "paper")
        self.assertEqual(Path(beta.data_dir), Path(tmpdir) / "wallet_data" / "beta_shadow" / "paper")
        self.assertNotEqual(stable.data_dir, beta.data_dir)
        self.assertNotEqual(stable.risk_state_path, beta.risk_state_path)
        self.assertNotEqual(stable.session_path, beta.session_path)
        self.assertNotEqual(stable.agent_decision_path, beta.agent_decision_path)
        self.assertEqual(stable_decisions[0]["shared_candidate_id"], candidate_id)
        self.assertEqual(beta_decisions[0]["shared_candidate_id"], candidate_id)
        self.assertEqual(stable_decisions[0]["candidate_dataset_path"], str(dataset_path))
        self.assertEqual(beta_decisions[0]["candidate_dataset_path"], str(dataset_path))
        self.assertEqual(stable_decisions[0]["wallet_id"], "stable_paper")
        self.assertEqual(beta_decisions[0]["wallet_id"], "beta_paper")
        self.assertEqual(stable_decisions[0]["policy"], "normal")
        self.assertEqual(beta_decisions[0]["policy"], "beta_enforce")
        self.assertEqual(stable_runs[0]["accounting_root"], stable.data_dir)
        self.assertEqual(beta_runs[0]["accounting_root"], beta.data_dir)
        self.assertEqual(stable_risk["available_cash"], 90.0)
        self.assertEqual(beta_risk["available_cash"], 90.0)
        self.assertEqual(stable_risk["reserved_capital"], 10.0)
        self.assertEqual(beta_risk["reserved_capital"], 10.0)

    def test_paper_evaluation_decision_rows_replay_by_shared_candidate_id_with_paper_shadow_role(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            row = self._snapshot_row()
            append_jsonl(dataset_path, row)

            with patch("bot.simulator.KellySizer.calculate", return_value=10.0):
                result = run_shared_candidate_paper_evaluation(dataset_path, config=config)

            shared_candidate_id = row["shared_candidate_id"]
            stable = result.wallet_runs["stable_paper"]
            beta = result.wallet_runs["beta_paper"]
            stable_decisions = load_jsonl(Path(stable.agent_decision_path))
            beta_decisions = load_jsonl(Path(beta.agent_decision_path))
            resolution_path = Path(tmpdir) / "shared" / "prediction_lab" / "resolutions.jsonl"
            append_jsonl(
                resolution_path,
                {
                    "shared_candidate_id": shared_candidate_id,
                    "market_id": row["market_id"],
                    "run_id": row["run_id"],
                    "outcome": "YES",
                },
            )

            replay = replay_from_paths(
                [dataset_path],
                config=config,
                evaluator=DecisionPipelineEvaluator(
                    strategy=FixedReplayStrategy(self._signal()),
                    risk_policy=AllowReplayRisk(),
                ),
                decision_paths=[stable.agent_decision_path, beta.agent_decision_path],
                resolution_paths=[resolution_path],
            )

        self.assertEqual(stable_decisions[0]["decision_role"], "paper_shadow")
        self.assertEqual(beta_decisions[0]["decision_role"], "paper_shadow")
        self.assertEqual(stable_decisions[0]["shared_candidate_id"], shared_candidate_id)
        self.assertEqual(beta_decisions[0]["shared_candidate_id"], shared_candidate_id)
        self.assertEqual(stable_decisions[0]["candidate_dataset_path"], str(dataset_path))
        self.assertEqual(beta_decisions[0]["candidate_dataset_path"], str(dataset_path))
        coverage = replay.summary["agent_decision_coverage"]
        self.assertEqual(coverage["requested_shared_candidate_ids"], 1)
        self.assertEqual(coverage["matched_shared_candidate_ids"], 1)
        self.assertEqual(coverage["unmatched_input_rows"], 0)
        self.assertEqual(coverage["by_agent_id"], {"paper": 2})
        self.assertEqual(coverage["by_runtime"], {"paper": 2})
        self.assertEqual(coverage["by_decision_role"], {"paper_shadow": 2})
        outcomes = replay.summary["agent_decision_report"]["outcomes"]
        self.assertEqual(outcomes["joined_rows"], 2)
        self.assertEqual(outcomes["unresolved_rows"], 0)


if __name__ == "__main__":
    unittest.main()
