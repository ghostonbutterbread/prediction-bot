import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot.config import load_config
from bot.file_ops import append_jsonl
from bot.paper_report import build_stable_beta_paper_report
from bot.paper_wallet_runner import run_shared_candidate_paper_evaluation
from bot.paper_wallets import resolve_paper_wallet_contract
from bot.prediction_lab import PredictionLab
from bot.shared_market_feed import SCHEMA_NAME


class PaperReportTests(unittest.TestCase):
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

    def _shared_candidate_row(self, candidate_id: str, *, market_id: str | None = None) -> dict:
        market_id = market_id or f"KXTEST-{candidate_id}"
        return {
            "schema_name": SCHEMA_NAME,
            "candidate_id": candidate_id,
            "market_id": market_id,
            "observed_at": "2026-05-13T12:00:00+00:00",
            "source_runtime": "prediction_lab",
            "provenance": "fixture",
            "market": {
                "id": market_id,
                "exchange": "kalshi",
                "question": f"Fixture {candidate_id}?",
                "category": "weather",
                "group": "weather",
                "route": {"group": "weather", "family": "daily_temperature", "allowed": True},
            },
            "prices": {"yes_price": 0.41, "no_price": 0.59},
            "decision": {"direction": "BUY_YES", "model_probability": 0.67, "edge": 0.26, "confidence": 0.9},
            "evidence": {"station_id": "KNYC", "source_mode": "fixture"},
        }

    def _decision_row(self, candidate_id: str, wallet_id: str, action: str, size: float, reason: str) -> dict:
        return {
            "shared_candidate_id": candidate_id,
            "wallet_id": wallet_id,
            "policy_id": "beta" if wallet_id == "beta_paper" else "stable",
            "policy": "beta_enforce" if wallet_id == "beta_paper" else "normal",
            "run_id": "fixture-run",
            "action": action,
            "side": "YES" if "YES" in action else None,
            "requested_position_size_usd": size,
            "approved_position_size_usd": size if action != "SKIP" else None,
            "reason_code": reason,
            "entry_price": 0.41,
            "observed_at": "2026-05-13T12:00:00+00:00",
            "decided_at": "2026-05-13T12:00:01+00:00",
        }

    def _write_run_and_session(self, root: Path, wallet_id: str, dataset_path: Path, trades: list[dict] | None = None) -> None:
        root.mkdir(parents=True, exist_ok=True)
        session_path = root / "sim_fixture-run.json"
        session_path.write_text(
            json.dumps(
                {
                    "session_id": "fixture-run",
                    "starting_balance": 100.0,
                    "balance": 100.0,
                    "available_cash": 100.0,
                    "reserved_capital": 0.0,
                    "scan_count": 0,
                    "trades": trades or [],
                }
            )
        )
        append_jsonl(
            root / "agent_runs.jsonl",
            {
                "wallet_id": wallet_id,
                "run_id": "fixture-run",
                "status": "finished",
                "candidate_dataset_path": str(dataset_path),
                "session_path": str(session_path),
                "risk_state_path": str(root / "risk_state.json"),
                "accounting_root": str(root),
                "decision_ledger_path": str(root / "agent_decisions.jsonl"),
            },
        )

    def _trade(self, candidate_id: str, pnl: float, *, trade_id: str | None = None) -> dict:
        return {
            "id": trade_id or f"trade-{candidate_id}",
            "direction": "BUY_YES",
            "position_size": 10.0,
            "reserved_capital": 10.0,
            "entry_price": 0.41,
            "resolved": True,
            "net_pnl": pnl,
            "decision_artifact": {"strategy_signal": {"shared_candidate_id": candidate_id}},
        }

    def test_report_compares_runner_outputs_without_mutating_wallet_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            row = self._snapshot_row()
            append_jsonl(dataset_path, row)

            with patch("bot.simulator.KellySizer.calculate", return_value=10.0):
                evaluation = run_shared_candidate_paper_evaluation(dataset_path, config=config)

            before = {
                wallet_id: Path(wallet.session_path).read_text()
                for wallet_id, wallet in evaluation.wallet_runs.items()
            }
            report = build_stable_beta_paper_report(dataset_path, config=config, evaluation_result=evaluation)
            after = {
                wallet_id: Path(wallet.session_path).read_text()
                for wallet_id, wallet in evaluation.wallet_runs.items()
            }

        candidate_id = row["shared_candidate_id"]
        self.assertEqual(before, after)
        self.assertTrue(report["ready"])
        self.assertEqual(report["shared_candidate_ids"], [candidate_id])
        self.assertEqual(report["summary"]["comparison_rows"], 1)
        self.assertEqual(report["summary"]["candidate_rows_with_both_decisions"], 1)
        self.assertEqual(report["summary"]["candidate_rows_with_any_accounting_effect"], 1)
        comparison = report["comparisons"][0]
        self.assertEqual(comparison["shared_candidate_id"], candidate_id)
        self.assertIn("same_action", comparison["delta_categories"])
        self.assertEqual(comparison["candidate_evidence"]["candidate_dataset_path"], str(dataset_path))
        self.assertEqual(comparison["stable_paper"]["decision"]["wallet_id"], "stable_paper")
        self.assertEqual(comparison["beta_paper"]["decision"]["wallet_id"], "beta_paper")
        self.assertEqual(report["wallets"]["stable_paper"]["policy"], "normal")
        self.assertEqual(report["wallets"]["beta_paper"]["policy"], "beta_enforce")
        self.assertNotEqual(report["wallets"]["stable_paper"]["root_dir"], report["wallets"]["beta_paper"]["root_dir"])

    def test_report_labels_delta_categories_from_wallet_decision_ledgers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            for candidate_id in ("same", "stable-only", "beta-only", "size-reason"):
                append_jsonl(dataset_path, self._shared_candidate_row(candidate_id))

            stable = resolve_paper_wallet_contract(config, wallet_id="stable_paper")
            beta = resolve_paper_wallet_contract(config, wallet_id="beta_paper")
            self._write_run_and_session(
                stable.root_dir,
                "stable_paper",
                dataset_path,
                trades=[
                    self._trade("stable-only", -4.0),
                    self._trade("size-reason", 3.0),
                ],
            )
            self._write_run_and_session(
                beta.root_dir,
                "beta_paper",
                dataset_path,
                trades=[
                    self._trade("beta-only", 6.0),
                    self._trade("size-reason", 5.0),
                ],
            )
            for row in (
                self._decision_row("same", "stable_paper", "BUY_YES", 10.0, "approved"),
                self._decision_row("stable-only", "stable_paper", "BUY_YES", 10.0, "approved"),
                self._decision_row("beta-only", "stable_paper", "SKIP", 0.0, "stable_reject"),
                self._decision_row("size-reason", "stable_paper", "BUY_YES", 5.0, "stable_reason"),
            ):
                append_jsonl(stable.root_dir / "agent_decisions.jsonl", row)
            for row in (
                self._decision_row("same", "beta_paper", "BUY_YES", 10.0, "approved"),
                self._decision_row("stable-only", "beta_paper", "SKIP", 0.0, "beta_reject"),
                self._decision_row("beta-only", "beta_paper", "BUY_YES", 10.0, "approved"),
                self._decision_row("size-reason", "beta_paper", "BUY_YES", 8.0, "beta_reason"),
            ):
                append_jsonl(beta.root_dir / "agent_decisions.jsonl", row)

            report = build_stable_beta_paper_report(dataset_path, config=config)

        by_id = {row["shared_candidate_id"]: row["delta_categories"] for row in report["comparisons"]}
        self.assertIn("same_action", by_id["same"])
        self.assertIn("stable_only", by_id["stable-only"])
        self.assertIn("beta_only", by_id["beta-only"])
        self.assertIn("same_action", by_id["size-reason"])
        self.assertIn("size_delta", by_id["size-reason"])
        self.assertIn("reason_delta", by_id["size-reason"])
        self.assertEqual(report["summary"]["delta_category_counts"]["same_action"], 2)
        self.assertEqual(report["summary"]["delta_category_counts"]["stable_only"], 1)
        self.assertEqual(report["summary"]["delta_category_counts"]["beta_only"], 1)
        self.assertEqual(report["summary"]["delta_category_counts"]["size_delta"], 1)
        self.assertEqual(report["summary"]["delta_category_counts"]["reason_delta"], 3)
        self.assertEqual(report["summary"]["outcome_category_counts"]["beta_avoided_stable_loss"], 1)
        self.assertEqual(report["summary"]["outcome_category_counts"]["beta_only_winner"], 1)
        self.assertEqual(report["summary"]["outcome_category_counts"]["beta_outperformed"], 1)
        self.assertEqual(report["summary"]["stable_resolved_pnl"], -1.0)
        self.assertEqual(report["summary"]["beta_resolved_pnl"], 11.0)
        self.assertEqual(report["summary"]["beta_minus_stable_resolved_pnl"], 12.0)

    def test_report_marks_malformed_required_decision_ledger_not_ready(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            append_jsonl(dataset_path, self._shared_candidate_row("same"))
            stable = resolve_paper_wallet_contract(config, wallet_id="stable_paper")
            beta = resolve_paper_wallet_contract(config, wallet_id="beta_paper")
            self._write_run_and_session(stable.root_dir, "stable_paper", dataset_path)
            self._write_run_and_session(beta.root_dir, "beta_paper", dataset_path)
            append_jsonl(stable.root_dir / "agent_decisions.jsonl", self._decision_row("same", "stable_paper", "BUY_YES", 10.0, "approved"))
            (beta.root_dir / "agent_decisions.jsonl").write_text('{"shared_candidate_id": "same"\n')

            report = build_stable_beta_paper_report(dataset_path, config=config)

        self.assertFalse(report["ready"])
        self.assertTrue(any("invalid JSON" in issue for issue in report["issues"]))


if __name__ == "__main__":
    unittest.main()
