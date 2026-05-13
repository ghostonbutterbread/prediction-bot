import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from bot.config import load_config
from bot.file_ops import append_jsonl
from bot.paper_migration_canary import build_paper_migration_canary_plan
from scripts.paper_migration_canary import main as canary_main


class PaperMigrationCanaryTests(unittest.TestCase):
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

    def test_canary_plan_preserves_default_stable_and_beta_wallet_mapping(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            stable_root = Path(tmpdir) / "wallet_data" / "paper"
            beta_root = Path(tmpdir) / "wallet_data" / "beta_shadow" / "paper"
            stable_root.mkdir(parents=True, exist_ok=True)
            beta_root.mkdir(parents=True, exist_ok=True)
            (stable_root / "risk_state.json").write_text('{"available_cash": 100.0}')
            (beta_root / "risk_state.json").write_text('{"available_cash": 95.0}')
            (stable_root / "sim_20260513_010203.json").write_text('{"session_id":"20260513_010203"}')
            (beta_root / "sim_20260513_020304.json").write_text('{"session_id":"20260513_020304"}')

            plan = build_paper_migration_canary_plan(config)

        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["compatibility_mapping"]["stable_paper_root"], str(stable_root))
        self.assertEqual(plan["compatibility_mapping"]["beta_paper_root"], str(beta_root))
        self.assertTrue(plan["wallet_isolation"]["ok"])
        self.assertEqual(plan["wallet_state"]["stable_paper"]["session_file_count"], 1)
        self.assertEqual(plan["wallet_state"]["beta_paper"]["session_file_count"], 1)
        self.assertEqual(plan["candidate_datasets_under_wallet_roots"], [])
        self.assertEqual(plan["copy_plan"], [])
        self.assertEqual(plan["backfill_plan"], [])

    def test_canary_plan_detects_prediction_lab_datasets_under_wallet_roots_and_previews_copy_and_backfill(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            stable_market_snapshots = Path(tmpdir) / "wallet_data" / "paper" / "prediction_lab" / "market_snapshots.jsonl"
            beta_market_snapshots = Path(tmpdir) / "wallet_data" / "beta_shadow" / "paper" / "prediction_lab" / "market_snapshots.jsonl"
            beta_predictions = Path(tmpdir) / "wallet_data" / "beta_shadow" / "paper" / "prediction_lab" / "predictions.jsonl"
            append_jsonl(stable_market_snapshots, {"shared_candidate_id": "stable-1", "market_id": "S-1"})
            append_jsonl(beta_market_snapshots, {"shared_candidate_id": "beta-1", "market_id": "B-1"})
            append_jsonl(beta_predictions, {"shared_candidate_id": "beta-1", "decision": "BUY_YES"})

            plan = build_paper_migration_canary_plan(config)
            detected = plan["candidate_datasets_under_wallet_roots"]
            self.assertEqual(len(detected), 3)
            recommended_paths = {item["recommended_shared_path"] for item in detected}
            shared_root = Path(plan["shared_candidates_root"])
            self.assertIn(
                str(shared_root / "prediction_lab" / "stable_paper" / "market_snapshots.jsonl"),
                recommended_paths,
            )
            self.assertIn(
                str(shared_root / "prediction_lab" / "beta_paper" / "market_snapshots.jsonl"),
                recommended_paths,
            )
            self.assertIn(
                str(shared_root / "prediction_lab" / "beta_paper" / "predictions.jsonl"),
                recommended_paths,
            )
            self.assertTrue(all(item["mode"] == "copy_only_never_move" for item in plan["copy_plan"]))
            self.assertEqual(len(plan["backfill_plan"]), 2)
            beta_backfill = next(item for item in plan["backfill_plan"] if item["wallet_id"] == "beta_paper")
            self.assertIn("--include-predictions", beta_backfill["command"])
            self.assertFalse((shared_root / "prediction_lab" / "stable_paper" / "market_snapshots.jsonl").exists())
            self.assertTrue(stable_market_snapshots.exists())
            self.assertTrue(beta_market_snapshots.exists())
            self.assertTrue(beta_predictions.exists())

    def test_canary_plan_flags_non_isolated_wallet_roots_without_mutating_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            shared_root = Path(tmpdir) / "wallets" / "shared"
            shared_root.mkdir(parents=True, exist_ok=True)
            (shared_root / "risk_state.json").write_text('{"available_cash": 100.0}')
            plan = build_paper_migration_canary_plan(
                {
                    "runtime": {"base_dir": str(Path(tmpdir) / "wallets")},
                    "trading": {"mode": "paper"},
                    "paper_wallets": {
                        "stable_paper": {"root_dir": str(shared_root)},
                        "beta_paper": {"root_dir": str(shared_root)},
                    },
                }
            )
            self.assertTrue((shared_root / "risk_state.json").exists())

        self.assertEqual(plan["status"], "blocked")
        self.assertFalse(plan["wallet_isolation"]["ok"])
        self.assertGreaterEqual(len(plan["blockers"]), 1)

    def test_cli_json_mode_emits_read_only_plan(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                f"""
runtime:
  base_dir: {Path(tmpdir) / "wallet_data"}
trading:
  mode: paper
"""
            )
            append_jsonl(
                Path(tmpdir) / "wallet_data" / "beta_shadow" / "paper" / "prediction_lab" / "market_snapshots.jsonl",
                {"shared_candidate_id": "beta-1", "market_id": "B-1"},
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = canary_main(["--config", str(config_path), "--json"])

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["mode"], "read_only_migration_canary")
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(len(payload["copy_plan"]), 1)
        self.assertEqual(payload["copy_plan"][0]["wallet_id"], "beta_paper")


if __name__ == "__main__":
    unittest.main()
