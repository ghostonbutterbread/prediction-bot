import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot.agent_decision_ledger import summarize_agent_decision_coverage, summarize_agent_decision_reporting
from bot.config import load_config
from bot.file_ops import append_jsonl, load_jsonl
from bot.paper_shadow_lanes import PAPER_LANE_DECISION_ROLE, paper_shadow_lanes_enabled, write_paper_shadow_lane_decisions
from bot.paper_wallet_runner import run_shared_candidate_paper_evaluation
from bot.prediction_lab import PredictionLab


class PaperShadowLaneTests(unittest.TestCase):
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
    features: {{}}
strategy:
  enable_news: false
  enable_social: false
  enable_ai: false
paper_shadow_lanes:
  enabled: true
  confidence_floor: 0.58
"""
        )
        return load_config(config_path)

    def _market(self, market_id: str):
        return SimpleNamespace(
            id=market_id,
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
                "event_ticker": market_id,
                "market_route": {"group": "weather", "family": "daily_temperature", "allowed": True},
            },
        )

    def _signal(self, *, confidence: float):
        return {
            "direction": "BUY_YES",
            "model_probability": 0.67,
            "market_price": 0.41,
            "yes_market_price": 0.41,
            "no_market_price": 0.59,
            "edge": 0.26,
            "confidence": confidence,
            "station_id": "KNYC",
            "source_as_of": "2026-05-13T12:00:00+00:00",
            "signals": {"unit": 0.67},
        }

    def _snapshot_row(self, *, market_id: str, confidence: float, observed_at: str):
        lab = PredictionLab(
            {
                "data_dir": "/tmp/prediction-lab-fixture",
                "prediction_lab": {"enabled": True, "mode": "collector", "groups": ["weather"]},
                "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
            }
        )
        signal = self._signal(confidence=confidence)
        return lab._build_market_snapshot_row(
            f"run-{market_id}",
            self._market(market_id),
            signal,
            decision_type="buy_yes",
            prediction_recorded=True,
            decision_artifact={
                "final_action": "BUY_YES",
                "final_reason_code": "approved",
                "strategy_signal": signal,
                "shared_core_decision": {
                    "requested_position_size": 10.0,
                    "reason_code": "approved",
                    "confidence": confidence,
                },
            },
            observed_at=observed_at,
        )

    def test_shadow_lanes_write_multiple_non_mutating_decisions_per_shared_candidate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            low_row = self._snapshot_row(
                market_id="KXHIGHNY-260513-T71",
                confidence=0.55,
                observed_at="2026-05-13T12:00:01+00:00",
            )
            high_row = self._snapshot_row(
                market_id="KXHIGHNY-260514-T71",
                confidence=0.91,
                observed_at="2026-05-14T12:00:01+00:00",
            )
            append_jsonl(dataset_path, low_row)
            append_jsonl(dataset_path, high_row)

            with patch("bot.simulator.KellySizer.calculate", return_value=10.0):
                result = run_shared_candidate_paper_evaluation(dataset_path, config=config)

            lane_rows = load_jsonl(Path(result.paper_lane_decision_path))

        self.assertEqual(result.paper_lane_ids, ("control_stable", "shadow_current_beta", "shadow_confidence_floor"))
        self.assertEqual(result.paper_lane_decision_count, 6)
        self.assertEqual(len(lane_rows), 6)
        self.assertEqual({row["decision_role"] for row in lane_rows}, {PAPER_LANE_DECISION_ROLE})
        self.assertEqual({row["runtime"] for row in lane_rows}, {"paper"})
        self.assertEqual({row["agent_id"] for row in lane_rows}, {"paper"})
        self.assertEqual({row["shared_candidate_id"] for row in lane_rows}, set(result.shared_candidate_ids))
        self.assertTrue(all(row["mutation_contract"]["mutates_accounting"] is False for row in lane_rows))
        self.assertTrue(all(row["accounting_ref"]["mutates_accounting"] is False for row in lane_rows))

        by_candidate = {}
        for row in lane_rows:
            by_candidate.setdefault(row["shared_candidate_id"], []).append(row)
        self.assertEqual({candidate_id: len(rows) for candidate_id, rows in by_candidate.items()}, {low_row["shared_candidate_id"]: 3, high_row["shared_candidate_id"]: 3})

        confidence_floor_rows = {
            row["shared_candidate_id"]: row
            for row in lane_rows
            if row["policy"] == "shadow_confidence_floor"
        }
        self.assertEqual(confidence_floor_rows[low_row["shared_candidate_id"]]["action"], "SKIP")
        self.assertEqual(confidence_floor_rows[low_row["shared_candidate_id"]]["reason_code"], "confidence_below_floor")
        self.assertEqual(confidence_floor_rows[high_row["shared_candidate_id"]]["action"], "BUY_YES")
        self.assertEqual(confidence_floor_rows[high_row["shared_candidate_id"]]["reason_code"], "approved_confidence_floor")
        self.assertEqual(confidence_floor_rows[high_row["shared_candidate_id"]]["side"], "YES")

    def test_shadow_lane_rows_are_accepted_by_agent_decision_reporting(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            row = self._snapshot_row(
                market_id="KXHIGHNY-260513-T71",
                confidence=0.55,
                observed_at="2026-05-13T12:00:01+00:00",
            )
            append_jsonl(dataset_path, row)

            with patch("bot.simulator.KellySizer.calculate", return_value=10.0):
                result = run_shared_candidate_paper_evaluation(dataset_path, config=config)

            lane_rows = load_jsonl(Path(result.paper_lane_decision_path))
            coverage = summarize_agent_decision_coverage(lane_rows, shared_candidate_ids=result.shared_candidate_ids)
            report = summarize_agent_decision_reporting(lane_rows, shared_candidate_ids=result.shared_candidate_ids)

        self.assertEqual(coverage["total_rows"], 3)
        self.assertEqual(coverage["matched_shared_candidate_ids"], 1)
        self.assertEqual(coverage["by_decision_role"], {"paper_lane": 3})
        self.assertEqual(
            coverage["by_policy"],
            {
                "control_stable": 1,
                "shadow_confidence_floor": 1,
                "shadow_current_beta": 1,
            },
        )
        self.assertEqual(report["coverage"]["total_rows"], 3)
        self.assertEqual(report["overlap"]["candidate_count_with_multiple_decisions"], 1)
        self.assertEqual(report["policy_drift"]["candidate_count_with_action_drift"], 1)

    def test_string_false_does_not_enable_shadow_lane_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            config["paper_shadow_lanes"]["enabled"] = "false"
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            row = self._snapshot_row(
                market_id="KXHIGHNY-260513-T71",
                confidence=0.91,
                observed_at="2026-05-13T12:00:01+00:00",
            )
            append_jsonl(dataset_path, row)

            with patch("bot.simulator.KellySizer.calculate", return_value=10.0):
                result = run_shared_candidate_paper_evaluation(dataset_path, config=config)

        self.assertFalse(paper_shadow_lanes_enabled(config))
        self.assertIsNone(result.paper_lane_decision_path)
        self.assertEqual(result.paper_lane_decision_count, 0)
        self.assertEqual(result.paper_lane_ids, ())

    def test_shadow_lanes_ignore_stale_wallet_decisions_for_same_candidate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            decision_path = Path(tmpdir) / "lanes.jsonl"
            candidate_id = "candidate-1"
            current_stable = {
                "shared_candidate_id": candidate_id,
                "wallet_id": "stable_paper",
                "run_id": "stable-run",
                "candidate_dataset_path": str(dataset_path),
                "decision_role": "paper_shadow",
                "decision_id": "current-stable",
                "policy": "normal",
                "market_id": "KXHIGHNY-260513-T71",
                "observed_at": "2026-05-13T12:00:01+00:00",
                "action": "BUY_YES",
                "reason_code": "approved",
                "confidence": 0.91,
                "approved_position_size_usd": 10.0,
            }
            stale_stable = dict(current_stable)
            stale_stable.update(
                {
                    "run_id": "old-run",
                    "decision_id": "stale-stable",
                    "action": "SKIP",
                    "reason_code": "stale_skip",
                    "confidence": 0.10,
                    "approved_position_size_usd": 0.0,
                }
            )
            current_beta = dict(current_stable)
            current_beta.update({"wallet_id": "beta_paper", "run_id": "beta-run", "decision_id": "current-beta", "policy": "beta_enforce"})

            result = write_paper_shadow_lane_decisions(
                config={"paper_shadow_lanes": {"enabled": True, "decision_ledger_path": str(decision_path)}},
                candidate_dataset_path=dataset_path,
                inputs_by_shared_candidate_id={
                    candidate_id: {
                        "stable": SimpleNamespace(signal={"market_id": "KXHIGHNY-260513-T71", "confidence": 0.91}),
                    }
                },
                wallet_decision_rows={"stable_paper": [current_stable, stale_stable], "beta_paper": [current_beta]},
                wallet_runs={"stable_paper": SimpleNamespace(session_id="stable-run"), "beta_paper": SimpleNamespace(session_id="beta-run")},
                ledger_root=tmpdir,
            )
            rows = load_jsonl(Path(result.decision_path))

        control_row = next(row for row in rows if row["policy"] == "control_stable")
        confidence_row = next(row for row in rows if row["policy"] == "shadow_confidence_floor")
        self.assertEqual(control_row["action"], "BUY_YES")
        self.assertEqual(control_row["provenance"]["source_decision_id"], "current-stable")
        self.assertEqual(confidence_row["action"], "BUY_YES")
        self.assertEqual(confidence_row["reason_code"], "approved_confidence_floor")


if __name__ == "__main__":
    unittest.main()
