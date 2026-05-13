import tempfile
import unittest
from pathlib import Path

from bot.decision_pipeline import build_pre_execution_decision_artifact
from bot.file_ops import load_jsonl
from bot.live_decision_audit import append_live_readonly_decision_audit
from types import SimpleNamespace


class LiveDecisionAuditTests(unittest.TestCase):
    def _signal(self, **overrides):
        signal = {
            "exchange": "kalshi",
            "market_id": "KXHIGHNY-260506-T71",
            "question": "Will the high temperature in New York exceed 71 degrees?",
            "direction": "BUY_YES",
            "market_price": 0.40,
            "yes_price": 0.40,
            "no_price": 0.60,
            "model_probability": 0.70,
            "edge": 0.30,
            "confidence": 0.90,
            "category": "KXHIGHNY",
            "market_family": "daily_temperature",
        }
        signal.update(overrides)
        return signal

    def _artifact(self, signal):
        decision = SimpleNamespace(
            action=signal["direction"],
            approved=True,
            position_size=8.0,
            requested_position_size=8.0,
            entry_price=signal["market_price"],
            win_probability=signal["model_probability"],
            reason="approved",
            reason_code="approved",
            reasoning={"strategy_lane": {"lane_id": "edge"}},
        )
        return build_pre_execution_decision_artifact(
            mode="live",
            context=None,
            decision=decision,
            signal=signal,
            source_context=signal,
            config_snapshot={"trading": {"mode": "live", "trading_enabled": False}},
        )

    def test_live_readonly_decision_links_shared_candidate_and_never_mutates_accounting(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dataset_path = Path(tmpdir) / "custom_candidates.jsonl"
            signal = self._signal(shared_candidate_id="candidate-live-1", candidate_dataset_path=str(dataset_path))
            artifact = self._artifact(signal)

            row = append_live_readonly_decision_audit(
                data_dir=Path(tmpdir),
                session_id="live-runner",
                scan_count=7,
                signal=signal,
                decision_artifact=artifact,
                config={"trading": {"mode": "live", "trading_enabled": False}},
                audit_stage="initial_decision",
            )

            run_rows = load_jsonl(Path(tmpdir) / "agent_runs.jsonl")
            decision_rows = load_jsonl(Path(tmpdir) / "agent_decisions.jsonl")
            candidate_rows = load_jsonl(Path(tmpdir) / "live_readonly_candidates.jsonl")
            original_candidate_rows = load_jsonl(dataset_path)

        self.assertEqual(len(run_rows), 1)
        self.assertEqual(run_rows[0]["agent_id"], "live")
        self.assertEqual(run_rows[0]["mode"], "live_readonly")
        self.assertFalse(run_rows[0]["mutates_accounting"])
        self.assertEqual(len(decision_rows), 1)
        self.assertEqual(row["shared_candidate_id"], "candidate-live-1")
        self.assertEqual(decision_rows[0]["shared_candidate_id"], "candidate-live-1")
        self.assertEqual(decision_rows[0]["decision_role"], "live_readonly")
        self.assertEqual(decision_rows[0]["candidate_dataset_path"], str(Path(tmpdir) / "live_readonly_candidates.jsonl"))
        self.assertEqual(decision_rows[0]["provenance"]["source_candidate_dataset_path"], str(dataset_path))
        self.assertEqual(decision_rows[0]["approved_position_size_usd"], 8.0)
        self.assertEqual(decision_rows[0]["provenance"]["audit_stage"], "initial_decision")
        self.assertEqual(decision_rows[0]["accounting_ref"]["balance_model"], "live_balance_readonly")
        self.assertFalse(decision_rows[0]["accounting_ref"]["mutates_balance"])
        self.assertFalse(decision_rows[0]["mutation_contract"]["mutates_shared_candidate"])
        self.assertFalse(decision_rows[0]["mutation_contract"]["mutates_accounting"])
        self.assertFalse(decision_rows[0]["mutation_contract"]["places_orders"])
        self.assertEqual(len(candidate_rows), 1)
        self.assertEqual(candidate_rows[0]["shared_candidate_id"], "candidate-live-1")
        self.assertEqual(candidate_rows[0]["audit_stage"], "initial_decision")
        self.assertEqual(original_candidate_rows, [])

    def test_live_readonly_decision_uses_explicit_legacy_identity_without_shared_candidate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            signal = self._signal()
            artifact = self._artifact(signal)

            with self.assertLogs("bot.live_decision_audit", level="WARNING") as logs:
                append_live_readonly_decision_audit(
                    data_dir=Path(tmpdir),
                    session_id="live-runner",
                    scan_count=3,
                    signal=signal,
                    decision_artifact=artifact,
                    config={"trading": {"mode": "live"}},
                    audit_stage="execution_revalidated",
                )

            decision_rows = load_jsonl(Path(tmpdir) / "agent_decisions.jsonl")

        self.assertTrue(any("legacy candidate identity" in message for message in logs.output))
        self.assertEqual(len(decision_rows), 1)
        self.assertNotIn("shared_candidate_id", decision_rows[0])
        self.assertEqual(decision_rows[0]["candidate_dataset_identity"], "legacy:live_readonly:live-runner")
        self.assertEqual(decision_rows[0]["legacy_candidate_identity"]["identity_type"], "legacy_live_readonly_signal")
        self.assertEqual(decision_rows[0]["legacy_candidate_identity"]["audit_stage"], "execution_revalidated")


if __name__ == "__main__":
    unittest.main()
