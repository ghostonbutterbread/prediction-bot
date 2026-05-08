import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.file_ops import load_jsonl
from bot.shared_core.shadow_intents import (
    SHADOW_INTENT_SCHEMA_NAME,
    append_hypothetical_shadow_intent_row,
    build_hypothetical_shadow_intent_row,
    build_hypothetical_shadow_intent_rows,
    is_hypothetical_shadow_intent_row,
)
from bot.trade_audit import EXECUTION_AUDIT_SCHEMA_NAME, is_trade_effective_row


def _shadow_delta(*, policy: dict | None = None, shadow_action: str | None = "BUY_YES") -> dict:
    return {
        "schema_version": 1,
        "mode": "beta_shadow_delta",
        "status": "complete" if shadow_action is not None else "partial_beta_evidence",
        "comparison_complete": shadow_action is not None,
        "action_comparison_available": shadow_action is not None,
        "policy": policy
        or {
            "version": "beta",
            "mode": "shadow",
            "enabled_features": ["hidden_gem_lane_gates"],
        },
        "stable": {
            "action": "SKIP",
            "reason_code": "edge_below_threshold",
            "direction": "SKIP",
            "decision_type": "skip",
            "requested_position_size": None,
            "selected_lane": "edge",
        },
        "shadow": {
            "action": shadow_action,
            "reason_code": "approved" if shadow_action else "confidence_slow_profit_lane_selected",
            "direction": shadow_action,
            "decision_type": "buy_yes" if shadow_action == "BUY_YES" else "unknown",
            "requested_position_size": 3.25 if shadow_action in {"BUY_YES", "BUY_NO"} else None,
            "selected_lane": "hidden_gem",
        },
        "changed": True,
        "action_changed": True if shadow_action else None,
        "side_changed": True if shadow_action else None,
        "buy_decision_changed": True if shadow_action else None,
        "reason_changed": True,
        "size_changed": True,
        "lane_changed": True,
        "dedupe_key": "KXTEST|run-1|beta-shadow",
        "evidence_sources": ["beta_lane_gate"],
    }


class ShadowIntentTests(unittest.TestCase):
    def test_builds_separate_counterfactual_row_for_paper_or_live_without_execution_fields(self):
        row = build_hypothetical_shadow_intent_row(
            {
                "run_id": "run-1",
                "market_id": "KXTEST",
                "timestamp": "2026-05-08T00:00:00+00:00",
                "shadow_delta": _shadow_delta(),
            },
            runtime_mode="live",
        )

        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["schema_name"], SHADOW_INTENT_SCHEMA_NAME)
        self.assertNotEqual(row["schema_name"], EXECUTION_AUDIT_SCHEMA_NAME)
        self.assertTrue(is_hypothetical_shadow_intent_row(row))
        self.assertEqual(row["runtime_mode"], "live")
        self.assertEqual(row["shadow_intent"]["intent_kind"], "trade")
        self.assertEqual(row["shadow_intent"]["action"], "BUY_YES")
        self.assertEqual(row["shadow_intent"]["hypothetical_requested_position_size"], 3.25)
        self.assertFalse(row["execution_allowed"])
        self.assertFalse(row["final_action_mutated"])
        self.assertEqual(row["final_action_effect"], "none")
        self.assertEqual(row["execution"]["status"], "not_executed")
        self.assertEqual(row["execution"]["placed_size"], 0.0)
        self.assertEqual(row["execution"]["filled_size"], 0.0)
        self.assertEqual(row["execution"]["reserved_capital_delta"], 0.0)

    def test_hypothetical_shadow_intent_does_not_count_as_real_trade_or_mutation(self):
        row = build_hypothetical_shadow_intent_row(
            {"run_id": "run-1", "market_id": "KXTEST", "shadow_delta": _shadow_delta()},
            runtime_mode="paper",
            recorded_at="2026-05-08T00:00:00+00:00",
        )

        self.assertIsNotNone(row)
        assert row is not None
        self.assertFalse(row["real_trade"])
        self.assertFalse(row["counts_as_trade"])
        self.assertFalse(row["counts_as_exposure"])
        self.assertFalse(row["counts_as_pnl"])
        self.assertFalse(row["mutates_balances"])
        self.assertFalse(row["mutates_exposure"])
        self.assertFalse(row["mutates_risk_state"])
        self.assertFalse(row["mutates_pnl"])
        self.assertFalse(row["mutates_trade_history"])
        self.assertFalse(row["mutates_open_orders"])
        self.assertFalse(row["mutates_open_positions"])
        self.assertFalse(is_trade_effective_row(row))

    def test_stable_off_and_beta_enforce_do_not_emit_shadow_intents(self):
        stable = {"version": "stable", "mode": "off"}
        enforce = {"version": "beta", "mode": "enforce"}

        self.assertIsNone(
            build_hypothetical_shadow_intent_row(
                {"market_id": "KXTEST", "shadow_delta": _shadow_delta(policy=stable)},
                runtime_mode="paper",
            )
        )
        self.assertIsNone(
            build_hypothetical_shadow_intent_row(
                {"market_id": "KXTEST", "shadow_delta": _shadow_delta(policy=enforce)},
                runtime_mode="live",
            )
        )
        self.assertEqual(
            build_hypothetical_shadow_intent_rows(
                [
                    {"market_id": "KXTEST", "shadow_delta": _shadow_delta(policy=stable)},
                    {"market_id": "KXTEST", "shadow_delta": _shadow_delta(policy=enforce)},
                ],
                runtime_mode="paper",
            ),
            [],
        )

    def test_append_helper_writes_only_separate_shadow_intents_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / "paper" / "shadow_intents.jsonl"
            trade_path = Path(tmpdir) / "paper" / "trades.jsonl"

            row = append_hypothetical_shadow_intent_row(
                ledger_path,
                {
                    "run_id": "run-1",
                    "market_id": "KXTEST",
                    "timestamp": "2026-05-08T00:00:00+00:00",
                    "shadow_delta": _shadow_delta(),
                },
                runtime_mode="paper",
            )

            self.assertIsNotNone(row)
            self.assertTrue(ledger_path.exists())
            self.assertFalse(trade_path.exists())
            rows = load_jsonl(ledger_path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["schema_name"], SHADOW_INTENT_SCHEMA_NAME)
            self.assertFalse(rows[0]["counts_as_trade"])

    def test_append_helper_does_not_create_file_for_stable_off_or_beta_enforce(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / "live" / "shadow_intents.jsonl"
            stable = {"version": "stable", "mode": "off"}
            enforce = {"version": "beta", "mode": "enforce"}

            self.assertIsNone(
                append_hypothetical_shadow_intent_row(
                    ledger_path,
                    {"market_id": "KXTEST", "shadow_delta": _shadow_delta(policy=stable)},
                    runtime_mode="live",
                )
            )
            self.assertIsNone(
                append_hypothetical_shadow_intent_row(
                    ledger_path,
                    {"market_id": "KXTEST", "shadow_delta": _shadow_delta(policy=enforce)},
                    runtime_mode="live",
                )
            )
            self.assertFalse(ledger_path.exists())

    def test_append_helper_is_best_effort_when_ledger_write_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / "paper" / "shadow_intents.jsonl"

            with patch("bot.shared_core.shadow_intents.append_jsonl", side_effect=OSError("disk full")):
                row = append_hypothetical_shadow_intent_row(
                    ledger_path,
                    {
                        "run_id": "run-1",
                        "market_id": "KXTEST",
                        "timestamp": "2026-05-08T00:00:00+00:00",
                        "shadow_delta": _shadow_delta(),
                    },
                    runtime_mode="paper",
                )

            self.assertIsNone(row)
            self.assertFalse(ledger_path.exists())

    def test_partial_shadow_delta_logs_unknown_intent_without_execution(self):
        row = build_hypothetical_shadow_intent_row(
            {"run_id": "run-1", "market_id": "KXTEST", "shadow_delta": _shadow_delta(shadow_action=None)},
            runtime_mode="prediction_lab",
            recorded_at="2026-05-08T00:00:00+00:00",
        )

        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["runtime_mode"], "prediction_lab")
        self.assertEqual(row["shadow_intent"]["intent_kind"], "unknown")
        self.assertEqual(row["shadow_intent"]["action"], "UNKNOWN")
        self.assertIsNone(row["shadow_intent"]["hypothetical_requested_position_size"])
        self.assertFalse(row["shadow_intent"]["action_comparison_available"])
        self.assertFalse(row["execution_allowed"])


if __name__ == "__main__":
    unittest.main()
