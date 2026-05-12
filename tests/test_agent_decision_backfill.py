import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from bot.agent_decision_backfill import backfill_legacy_agent_decisions
from bot.file_ops import load_jsonl

ROOT = Path(__file__).resolve().parent.parent


class AgentDecisionBackfillTests(unittest.TestCase):
    def _write_jsonl(self, path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def test_legacy_row_without_shared_candidate_writes_legacy_identity_decision(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "market_snapshots.jsonl"
            output_dir = Path(tmpdir) / "sidecars"
            row = {
                "timestamp": "2026-05-12T10:00:00+00:00",
                "observed_at": "2026-05-12T10:00:00+00:00",
                "run_id": "plab-run-1",
                "market_id": "KXHIGHNY-260512-T71",
                "yes_price": 0.42,
                "confidence": 0.72,
                "edge": 0.18,
                "model_probability": 0.6,
                "normal_decision": {
                    "action": "BUY_YES",
                    "policy": "normal",
                    "reason_code": "approved",
                    "reason": "legacy approval",
                    "size": 10.0,
                },
            }
            self._write_jsonl(input_path, [row])

            first = backfill_legacy_agent_decisions([input_path], output_dir=output_dir)
            second = backfill_legacy_agent_decisions([input_path], output_dir=output_dir)
            decisions = load_jsonl(output_dir / "agent_decisions.jsonl")
            runs = load_jsonl(output_dir / "agent_runs.jsonl")

        self.assertEqual(len(decisions), 1)
        decision = decisions[0]
        self.assertNotIn("shared_candidate_id", decision)
        self.assertEqual(decision["legacy_candidate_identity"]["identity_type"], "legacy_prediction_lab_market_snapshot")
        self.assertEqual(decision["legacy_candidate_identity"]["source_path"], str(input_path))
        self.assertEqual(decision["legacy_candidate_identity"]["line_number"], 1)
        self.assertEqual(decision["legacy_candidate_identity"]["run_id"], "plab-run-1")
        self.assertEqual(decision["legacy_candidate_identity"]["market_id"], "KXHIGHNY-260512-T71")
        self.assertEqual(decision["legacy_candidate_identity"]["timestamp"], "2026-05-12T10:00:00+00:00")
        self.assertEqual(decision["legacy_candidate_identity"]["decision_role"], "normal")
        self.assertEqual(decision["legacy_candidate_identity"]["policy"], "normal")
        self.assertIn("row_fingerprint_sha256", decision["legacy_candidate_identity"])
        self.assertEqual(decision["candidate_dataset_path"], str(input_path))
        self.assertEqual(decision["mutation_contract"]["mutates_shared_candidate"], False)
        self.assertEqual(decision["mutation_contract"]["mutates_accounting"], False)
        self.assertEqual(decision["mutation_contract"]["places_orders"], False)
        self.assertEqual(first.decision_rows[0]["decision_id"], second.decision_rows[0]["decision_id"])
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["agent_id"], "prediction_lab_legacy_backfill")
        self.assertEqual(runs[0]["runtime"], "backfill")
        self.assertFalse(runs[0]["mutates_accounting"])
        self.assertEqual(runs[0]["decision_ledger_path"], str(output_dir / "agent_decisions.jsonl"))

    def test_shared_candidate_row_uses_normal_shared_candidate_decisions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "market_snapshots.jsonl"
            output_dir = Path(tmpdir) / "sidecars"
            self._write_jsonl(
                input_path,
                [
                    {
                        "timestamp": "2026-05-12T10:00:00+00:00",
                        "observed_at": "2026-05-12T10:00:00+00:00",
                        "run_id": "plab-run-2",
                        "market_id": "KXHIGHNY-260512-T72",
                        "shared_candidate_id": "candidate-123",
                        "yes_price": 0.44,
                        "confidence": 0.7,
                        "edge": 0.16,
                        "decision_artifact": {
                            "strategy_signal": {"model_probability": 0.6},
                        },
                        "main_decision": {
                            "action": "BUY_YES",
                            "policy": "stable",
                            "reason_code": "approved",
                        },
                        "normal_decision": {
                            "action": "BUY_YES",
                            "policy": "stable",
                            "reason": "normal approved",
                            "size": 5.0,
                        },
                    }
                ],
            )

            backfill_legacy_agent_decisions([input_path], output_dir=output_dir)
            decisions = load_jsonl(output_dir / "agent_decisions.jsonl")

        self.assertEqual([row["decision_role"] for row in decisions], ["main", "normal"])
        self.assertEqual({row["shared_candidate_id"] for row in decisions}, {"candidate-123"})
        self.assertTrue(all("legacy_candidate_identity" not in row for row in decisions))
        self.assertTrue(all(row["candidate_dataset_path"] == str(input_path) for row in decisions))
        self.assertTrue(all(row["mutation_contract"] == {"mutates_shared_candidate": False, "mutates_accounting": False, "places_orders": False} for row in decisions))
        self.assertTrue(all(row["model_probability"] == 0.6 for row in decisions))

    def test_shared_candidate_artifact_only_row_preserves_historical_action(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "market_snapshots.jsonl"
            output_dir = Path(tmpdir) / "sidecars"
            self._write_jsonl(
                input_path,
                [
                    {
                        "timestamp": "2026-05-12T10:00:00+00:00",
                        "observed_at": "2026-05-12T10:00:00+00:00",
                        "run_id": "plab-run-artifact-only",
                        "market_id": "KXHIGHNY-260512-T76",
                        "shared_candidate_id": "candidate-artifact-only",
                        "yes_price": 0.41,
                        "decision_artifact": {
                            "final_action": "BUY_YES",
                            "final_reason_code": "approved",
                            "final_reason": "legacy artifact approval",
                        },
                    }
                ],
            )

            backfill_legacy_agent_decisions([input_path], output_dir=output_dir)
            decisions = load_jsonl(output_dir / "agent_decisions.jsonl")

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["shared_candidate_id"], "candidate-artifact-only")
        self.assertEqual(decisions[0]["action"], "BUY_YES")
        self.assertEqual(decisions[0]["side"], "YES")
        self.assertEqual(decisions[0]["reason_code"], "approved")
        self.assertEqual(decisions[0]["reason"], "legacy artifact approval")
        self.assertEqual(decisions[0]["mutation_contract"], {"mutates_shared_candidate": False, "mutates_accounting": False, "places_orders": False})

    def test_backfill_does_not_modify_input_file_bytes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "market_snapshots.jsonl"
            output_dir = Path(tmpdir) / "sidecars"
            self._write_jsonl(
                input_path,
                [
                    {
                        "timestamp": "2026-05-12T10:00:00+00:00",
                        "run_id": "plab-run-3",
                        "market_id": "KXHIGHNY-260512-T73",
                        "direction": "BUY_NO",
                        "skip_reason_code": "manual_fixture",
                    }
                ],
            )
            before = input_path.read_bytes()

            backfill_legacy_agent_decisions([input_path], output_dir=output_dir)
            after = input_path.read_bytes()

        self.assertEqual(after, before)

    def test_partial_unusable_row_is_skipped_and_counted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "market_snapshots.jsonl"
            output_dir = Path(tmpdir) / "sidecars"
            self._write_jsonl(
                input_path,
                [
                    {
                        "timestamp": "2026-05-12T10:00:00+00:00",
                        "run_id": "plab-run-4",
                        "market_id": "KXHIGHNY-260512-T74",
                        "confidence": 0.51,
                    }
                ],
            )

            result = backfill_legacy_agent_decisions([input_path], output_dir=output_dir)
            decisions = load_jsonl(output_dir / "agent_decisions.jsonl")

        self.assertEqual(decisions, [])
        self.assertEqual(result.report["rows_read"], 1)
        self.assertEqual(result.report["skipped_unusable_rows"], 1)
        self.assertEqual(result.report["decision_rows_written"], 0)

    def test_cli_smoke_writes_sidecars_and_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "market_snapshots.jsonl"
            output_dir = Path(tmpdir) / "sidecars"
            self._write_jsonl(
                input_path,
                [
                    {
                        "timestamp": "2026-05-12T10:00:00+00:00",
                        "run_id": "plab-run-5",
                        "market_id": "KXHIGHNY-260512-T75",
                        "decision_artifact": {
                            "final_action": "SKIP",
                            "final_reason_code": "edge_below_threshold",
                        },
                    }
                ],
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "agent_decision_backfill.py"),
                    str(input_path),
                    "--output-dir",
                    str(output_dir),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            report = json.loads(completed.stdout)
            decisions = load_jsonl(output_dir / "agent_decisions.jsonl")
            runs_exists = (output_dir / "agent_runs.jsonl").exists()
            report_exists = (output_dir / "agent_decision_backfill_report.json").exists()

        self.assertEqual(report["decision_rows_written"], 1)
        self.assertEqual(len(decisions), 1)
        self.assertTrue(runs_exists)
        self.assertTrue(report_exists)


if __name__ == "__main__":
    unittest.main()
