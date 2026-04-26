import tempfile
import unittest
from pathlib import Path

from bot.parity_audit import build_parity_view, normalize_parity_trade_row
from bot.trade_audit import apply_execution_audit_contract, validate_execution_audit_row


class ParityAuditTests(unittest.TestCase):
    def test_normalize_parity_trade_row_preserves_core_fields(self):
        row = {
            "id": "paper-1",
            "timestamp": "2026-04-23T00:00:00+00:00",
            "market_id": "m1",
            "question": "Will it rain?",
            "direction": "BUY_YES",
            "status": "filled",
            "position_size": 2.5,
            "market_price": 0.4,
            "decision_reason_code": "approved",
            "reserved_capital": 2.5,
            "decision_trace": {
                "parity_mode": {
                    "enabled": True,
                    "execution_revalidated": True,
                    "execution_revalidation_outcome": "approved",
                    "execution_snapshot_source": "book",
                    "execution_snapshot": {"market_price": 0.41},
                }
            },
        }

        normalized = normalize_parity_trade_row(row, source="paper")

        self.assertEqual(normalized["source"], "paper")
        self.assertEqual(normalized["schema_name"], "execution_audit_row")
        self.assertEqual(normalized["schema_version"], 1)
        self.assertEqual(normalized["trade_id"], "paper-1")
        self.assertEqual(normalized["requested_size"], 2.5)
        self.assertEqual(normalized["approved_size"], 2.5)
        self.assertEqual(normalized["placed_size"], 2.5)
        self.assertEqual(normalized["filled_size"], 2.5)
        self.assertEqual(normalized["lifecycle_state"], "filled_open")
        self.assertTrue(normalized["parity_mode_enabled"])
        self.assertEqual(normalized["contract_issues"], [])

    def test_summarize_normalized_rows_surfaces_parity_visibility_fields(self):
        rows = [
            {
                "status": "filled",
                "lifecycle_state": "filled_open",
                "decision_reason_code": "approved",
                "original_decision_reason_code": "approved",
                "execution_decision_reason_code": "approved",
                "execution_snapshot_source": "book",
                "parity_mode_enabled": True,
                "execution_revalidated": True,
                "execution_revalidation_outcome": "approved",
                "original_signal_snapshot": {"market_price": 0.40},
                "execution_snapshot": {"market_price": 0.40},
                "is_parity_candidate": True,
            },
            {
                "status": "resolved",
                "lifecycle_state": "resolved_position",
                "outcome": "NO",
                "decision_reason_code": "approved",
                "original_decision_reason_code": "approved",
                "execution_decision_reason_code": "price_above_threshold",
                "execution_snapshot_source": "fallback",
                "parity_mode_enabled": True,
                "execution_revalidated": True,
                "execution_revalidation_outcome": "rejected",
                "original_signal_snapshot": {"market_price": 0.40},
                "execution_snapshot": {"market_price": 0.47},
                "is_parity_candidate": True,
            },
        ]

        from bot.parity_audit import summarize_normalized_rows

        summary = summarize_normalized_rows(rows)

        self.assertEqual(summary["total_rows"], 2)
        self.assertEqual(summary["parity_candidates"], 2)
        self.assertEqual(summary["parity_enabled_rows"], 2)
        self.assertEqual(summary["execution_revalidated_rows"], 2)
        self.assertEqual(summary["execution_rejected_rows"], 1)
        self.assertEqual(summary["fallback_rows"], 1)
        self.assertEqual(summary["invalid_contract_rows"], 0)
        self.assertEqual(summary["snapshot_source_counts"]["book"], 1)
        self.assertEqual(summary["snapshot_source_counts"]["fallback"], 1)
        self.assertEqual(summary["execution_revalidation_outcome_counts"]["approved"], 1)
        self.assertEqual(summary["execution_revalidation_outcome_counts"]["rejected"], 1)
        self.assertEqual(summary["resolved_outcome_counts"]["NO"], 1)
        self.assertEqual(summary["decision_delta_rows"], 1)
        self.assertEqual(summary["execution_price_delta_rows"], 1)
        self.assertEqual(summary["top_execution_reason_codes"][0][0], "approved")

    def test_normalize_parity_trade_row_surfaces_contract_issues(self):
        row = {
            "trade_id": "bad-1",
            "timestamp": "2026-04-23T00:00:00+00:00",
            "market_id": "m1",
            "direction": "BUY_YES",
            "status": "filled",
            "requested_size": 1.0,
            "approved_size": 1.0,
            "placed_size": 1.0,
            "filled_size": 1.0,
            "remaining_size": 1.0,
            "market_price": 0.4,
            "decision_reason_code": "approved",
            "execution_snapshot_source": "book",
        }

        normalized = normalize_parity_trade_row(row, source="paper")

        self.assertIn("filled_with_remaining", normalized["contract_issues"])
        self.assertEqual(normalized["contract_issue_count"], 1)
        self.assertFalse(normalized["contract_valid"])

    def test_execution_contract_flags_resolved_rows_missing_resolution_fields(self):
        row = apply_execution_audit_contract(
            {
                "trade_id": "resolved-bad-1",
                "timestamp": "2026-04-23T00:00:00+00:00",
                "market_id": "m5",
                "direction": "BUY_YES",
                "status": "resolved",
                "requested_size": 2.0,
                "approved_size": 2.0,
                "placed_size": 2.0,
                "filled_size": 2.0,
                "remaining_size": 0.0,
                "market_price": 0.4,
                "decision_reason_code": "approved",
                "resolved": False,
            }
        )

        issues = validate_execution_audit_row(row)
        self.assertIn("resolved_without_timestamp", issues)

    def test_execution_contract_distinguishes_canceled_partial_and_stale(self):
        canceled = apply_execution_audit_contract(
            {
                "trade_id": "cancel-1",
                "timestamp": "2026-04-23T00:00:00+00:00",
                "market_id": "m3",
                "direction": "BUY_YES",
                "status": "cancelled",
                "requested_size": 4.0,
                "approved_size": 4.0,
                "placed_size": 4.0,
                "filled_size": 1.5,
                "remaining_size": 0.0,
                "market_price": 0.4,
                "decision_reason_code": "reconciled_resting_order",
            }
        )
        stale = apply_execution_audit_contract(
            {
                "trade_id": "stale-1",
                "timestamp": "2026-04-23T00:00:00+00:00",
                "market_id": "m4",
                "direction": "BUY_NO",
                "status": "expired",
                "requested_size": 2.0,
                "approved_size": 2.0,
                "placed_size": 2.0,
                "filled_size": 0.0,
                "remaining_size": 0.0,
                "market_price": 0.6,
                "decision_reason_code": "reconciled_resting_order",
            }
        )

        self.assertEqual(canceled["status"], "canceled")
        self.assertEqual(canceled["lifecycle_state"], "canceled_partial")
        self.assertEqual(validate_execution_audit_row(canceled), [])
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["lifecycle_state"], "stale_open_order")
        self.assertEqual(validate_execution_audit_row(stale), [])

    def test_build_parity_view_reads_paper_and_live_sources(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            (data_dir / "paper").mkdir(parents=True)
            (data_dir / "live").mkdir(parents=True)
            (data_dir / "paper" / "sim_20260423_000000.json").write_text(
                '{"trades":[{"id":"paper-1","timestamp":"2026-04-23T00:00:00+00:00","market_id":"m1","direction":"BUY_YES","position_size":2.0,"market_price":0.4,"decision_reason_code":"approved","decision_trace":{}}]}'
            )
            (data_dir / "live" / "trades.jsonl").write_text(
                '{"trade_id":"live-1","timestamp":"2026-04-23T00:05:00+00:00","market_id":"m-live","direction":"BUY_YES","status":"placed","requested_size":1.0,"approved_size":1.0,"placed_size":1.0,"filled_size":0.0,"remaining_size":1.0,"market_price":0.4,"decision_reason_code":"approved"}\n'
            )
            (data_dir / "live" / "risk_blocks.jsonl").write_text(
                '{"timestamp":"2026-04-23T00:10:00+00:00","market_id":"m2","question":"Q","exchange":"kalshi","direction":"BUY_YES","blocked_reason":"kelly_zero_size","decision_reason":"Rejected","decision_reason_code":"kelly_zero_size"}\n'
            )

            view = build_parity_view(data_dir)

            self.assertEqual(view["paper_summary"]["total_rows"], 1)
            self.assertEqual(view["live_summary"]["total_rows"], 2)
            self.assertEqual(view["paper_rows"][0]["decision_reason_code"], "approved")
            self.assertEqual(view["live_rows"][0]["trade_id"], "live-1")
            self.assertEqual(view["live_rows"][0]["status"], "placed")
            self.assertEqual(view["live_rows"][1]["status"], "rejected")
            self.assertFalse(view["live_rows"][1]["contract_issues"])

    def test_build_parity_view_reads_canonical_live_risk_block_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            (data_dir / "live").mkdir(parents=True)
            (data_dir / "live" / "risk_blocks.jsonl").write_text(
                '{"schema_name":"execution_audit_row","schema_version":1,"timestamp":"2026-04-23T00:10:00+00:00","trade_id":"risk-block:m2:blocker:2026-04-23T00:10:00+00:00","market_id":"m2","question":"Q","exchange":"kalshi","direction":"BUY_YES","status":"rejected","lifecycle_state":"risk_check_rejected","failure_stage":"risk_block","decision_reason":"Rejected","decision_reason_code":"kelly_zero_size","blocked_reason":"kelly_zero_size","requested_size":0.0,"approved_size":0.0,"placed_size":0.0,"filled_size":0.0,"remaining_size":0.0,"market_price":0.4,"entry_price":0.4,"execution_snapshot_source":"fallback"}\n'
            )

            view = build_parity_view(data_dir)

            self.assertEqual(view["live_summary"]["total_rows"], 1)
            self.assertEqual(view["live_rows"][0]["trade_id"], "risk-block:m2:blocker:2026-04-23T00:10:00+00:00")
            self.assertEqual(view["live_rows"][0]["status"], "rejected")
            self.assertFalse(view["live_rows"][0]["contract_issues"])

    def test_build_parity_view_reads_legacy_root_layout_for_backward_compat(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            data_dir.mkdir(parents=True)
            (data_dir / "sim_20260423_000000.json").write_text(
                '{"trades":[{"id":"paper-legacy","timestamp":"2026-04-23T00:00:00+00:00","market_id":"m1","direction":"BUY_YES","position_size":2.0,"market_price":0.4,"decision_reason_code":"approved","decision_trace":{}}]}'
            )
            (data_dir / "trades.jsonl").write_text(
                '{"trade_id":"live-legacy","timestamp":"2026-04-23T00:05:00+00:00","market_id":"m-live","direction":"BUY_YES","status":"placed","requested_size":1.0,"approved_size":1.0,"placed_size":1.0,"filled_size":0.0,"remaining_size":1.0,"market_price":0.4,"decision_reason_code":"approved"}\n'
            )

            view = build_parity_view(data_dir)

            self.assertEqual(view["paper_summary"]["total_rows"], 1)
            self.assertEqual(view["live_summary"]["total_rows"], 1)
            self.assertEqual(view["paper_rows"][0]["trade_id"], "paper-legacy")
            self.assertEqual(view["live_rows"][0]["trade_id"], "live-legacy")


if __name__ == "__main__":
    unittest.main()
