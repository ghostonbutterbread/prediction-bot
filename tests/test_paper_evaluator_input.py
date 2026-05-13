import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot.config import load_config
from bot.file_ops import append_jsonl, load_jsonl
from bot.paper_evaluator_input import load_shared_candidate_paper_inputs
from bot.prediction_lab import PredictionLab
from bot.simulator import Simulator


class PaperEvaluatorInputTests(unittest.TestCase):
    def _config(self, tmpdir: str) -> dict:
        config_path = Path(tmpdir) / "config.yaml"
        config_path.write_text(
            f"""
runtime:
  base_dir: {Path(tmpdir) / "wallet_data"}
trading:
  mode: paper
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

    def test_load_shared_candidate_inputs_builds_stable_and_beta_wallet_views_from_one_read(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            row = self._snapshot_row()
            append_jsonl(dataset_path, row)

            with patch("bot.paper_evaluator_input.load_jsonl", wraps=load_jsonl) as mocked_load_jsonl:
                result = load_shared_candidate_paper_inputs(dataset_path, config=config)

        self.assertEqual(mocked_load_jsonl.call_count, 1)
        self.assertEqual(result.loaded_row_count, 1)
        self.assertEqual(result.accepted_candidate_count, 1)
        self.assertEqual(result.skipped_rows, ())
        candidate_id = row["shared_candidate_id"]
        wallet_inputs = result.inputs_by_shared_candidate_id[candidate_id]
        self.assertEqual(set(wallet_inputs), {"stable_paper", "beta_paper"})
        stable = wallet_inputs["stable_paper"]
        beta = wallet_inputs["beta_paper"]
        self.assertEqual(stable.shared_candidate_id, candidate_id)
        self.assertEqual(beta.shared_candidate_id, candidate_id)
        self.assertEqual(stable.policy_id, "stable")
        self.assertEqual(beta.policy_id, "beta")
        self.assertTrue(stable.candidate_feed_read_only)
        self.assertTrue(beta.candidate_feed_read_only)
        self.assertEqual(stable.signal, beta.signal)
        self.assertEqual(stable.signal["candidate_dataset_path"], str(dataset_path))
        self.assertEqual(stable.signal["shared_candidate_id"], candidate_id)
        self.assertTrue(stable.wallet_contract["root_dir"].endswith("/wallet_data/paper"))
        self.assertTrue(beta.wallet_contract["root_dir"].endswith("/wallet_data/beta_shadow/paper"))

    def test_load_shared_candidate_inputs_derives_missing_top_level_id_from_embedded_shared_candidate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            row = self._snapshot_row()
            candidate_id = row.pop("shared_candidate_id")
            append_jsonl(dataset_path, row)

            result = load_shared_candidate_paper_inputs(dataset_path, config=config)

        self.assertEqual(result.accepted_candidate_count, 1)
        self.assertEqual(set(result.inputs_by_shared_candidate_id), {candidate_id})

    def test_load_shared_candidate_inputs_skips_row_without_usable_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            append_jsonl(
                dataset_path,
                {
                    "market_id": "KXHIGHNY-260513-T71",
                    "question": "Will the high temperature in New York exceed 71 degrees?",
                    "direction": "BUY_YES",
                },
            )

            result = load_shared_candidate_paper_inputs(dataset_path, config=config)

        self.assertEqual(result.accepted_candidate_count, 0)
        self.assertEqual(len(result.skipped_rows), 1)
        self.assertEqual(result.skipped_rows[0].reason_code, "missing_usable_shared_candidate_id")

    def test_load_shared_candidate_inputs_skips_mismatched_top_level_and_embedded_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            row = self._snapshot_row()
            row["shared_candidate_id"] = "candidate-top-level"
            row["shared_candidate"]["candidate_id"] = "candidate-embedded"
            append_jsonl(dataset_path, row)

            result = load_shared_candidate_paper_inputs(dataset_path, config=config)

        self.assertEqual(result.accepted_candidate_count, 0)
        self.assertEqual(len(result.skipped_rows), 1)
        self.assertEqual(result.skipped_rows[0].reason_code, "shared_candidate_id_mismatch")

    def test_load_shared_candidate_inputs_skips_duplicate_candidate_ids_after_first_row(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            first = self._snapshot_row()
            second = self._snapshot_row()
            second["decision_artifact"]["final_reason_code"] = "approved_duplicate"
            append_jsonl(dataset_path, first)
            append_jsonl(dataset_path, second)

            result = load_shared_candidate_paper_inputs(dataset_path, config=config)

        self.assertEqual(result.accepted_candidate_count, 1)
        self.assertEqual(len(result.skipped_rows), 1)
        self.assertEqual(result.skipped_rows[0].reason_code, "duplicate_shared_candidate_id")

    def test_load_shared_candidate_inputs_rejects_candidate_dataset_inside_wallet_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            dataset_path = Path(tmpdir) / "wallet_data" / "paper" / "prediction_lab" / "market_snapshots.jsonl"
            append_jsonl(dataset_path, self._snapshot_row())

            with self.assertRaises(ValueError):
                load_shared_candidate_paper_inputs(dataset_path, config=config)

    def test_wallet_inputs_do_not_share_nested_candidate_payloads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            row = self._snapshot_row()
            append_jsonl(dataset_path, row)

            result = load_shared_candidate_paper_inputs(dataset_path, config=config)
            candidate_id = row["shared_candidate_id"]
            stable = result.inputs_by_shared_candidate_id[candidate_id]["stable_paper"]
            beta = result.inputs_by_shared_candidate_id[candidate_id]["beta_paper"]

            stable.signal["signals"]["stable_only"] = True
            stable.signal["market_route"]["wallet_annotation"] = "stable"
            stable.shared_candidate["market"]["wallet_annotation"] = "stable"

        self.assertNotIn("stable_only", beta.signal["signals"])
        self.assertNotIn("wallet_annotation", beta.signal["market_route"])
        self.assertNotIn("wallet_annotation", beta.shared_candidate["market"])

    def test_load_shared_candidate_inputs_rejects_dataset_inside_any_canonical_wallet_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            dataset_path = Path(tmpdir) / "wallet_data" / "beta_shadow" / "paper" / "prediction_lab" / "market_snapshots.jsonl"
            append_jsonl(dataset_path, self._snapshot_row())

            with self.assertRaises(ValueError):
                load_shared_candidate_paper_inputs(dataset_path, config=config, wallet_ids=("stable_paper",))

    def test_load_shared_candidate_inputs_skips_top_level_id_that_conflicts_with_canonical_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            row = self._snapshot_row()
            row.pop("shared_candidate", None)
            row["shared_candidate_id"] = "stale-corrupt-id"
            append_jsonl(dataset_path, row)

            result = load_shared_candidate_paper_inputs(dataset_path, config=config)

        self.assertEqual(result.accepted_candidate_count, 0)
        self.assertEqual(len(result.skipped_rows), 1)
        self.assertEqual(result.skipped_rows[0].reason_code, "shared_candidate_id_mismatch")

    def test_generated_signal_is_compatible_with_phase1_paper_wallet_contracts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            row = self._snapshot_row()
            append_jsonl(dataset_path, row)
            result = load_shared_candidate_paper_inputs(dataset_path, config=config)
            candidate_id = row["shared_candidate_id"]
            stable_input = result.inputs_by_shared_candidate_id[candidate_id]["stable_paper"]
            beta_input = result.inputs_by_shared_candidate_id[candidate_id]["beta_paper"]

            stable_sim = Simulator(
                {
                    "data_dir": str(Path(tmpdir) / "wallet_data"),
                    "enable_social": False,
                    "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
                }
            )
            beta_sim = Simulator(
                {
                    "data_dir": str(Path(tmpdir) / "wallet_data" / "beta_shadow"),
                    "strategy_policy": {"version": "beta", "beta": {"mode": "shadow"}},
                    "enable_social": False,
                    "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
                }
            )

            with patch.object(stable_sim.kelly, "calculate", return_value=10.0):
                stable_trade = stable_sim._create_trade(stable_input.signal)
            with patch.object(beta_sim.kelly, "calculate", return_value=10.0):
                beta_trade = beta_sim._create_trade(beta_input.signal)

            stable_rows = load_jsonl(stable_sim.data_dir / "agent_decisions.jsonl")
            beta_rows = load_jsonl(beta_sim.data_dir / "agent_decisions.jsonl")

        self.assertIsNotNone(stable_trade)
        self.assertIsNotNone(beta_trade)
        self.assertEqual(stable_rows[0]["shared_candidate_id"], candidate_id)
        self.assertEqual(beta_rows[0]["shared_candidate_id"], candidate_id)
        self.assertEqual(stable_rows[0]["candidate_dataset_path"], str(dataset_path))
        self.assertEqual(beta_rows[0]["candidate_dataset_path"], str(dataset_path))
        self.assertEqual(stable_rows[0]["wallet_id"], "stable_paper")
        self.assertEqual(beta_rows[0]["wallet_id"], "beta_paper")


if __name__ == "__main__":
    unittest.main()
