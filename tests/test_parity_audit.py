import tempfile
import unittest
from pathlib import Path

from bot.parity_audit import build_parity_view, normalize_parity_trade_row, write_parity_comparison_artifact
from bot.trade_audit import apply_execution_audit_contract, enrich_trade_audit_fields, validate_execution_audit_row


class ParityAuditTests(unittest.TestCase):
    def test_resolved_enrichment_prefers_entry_price_over_market_snapshot_price(self):
        row = {
            "trade_id": "resolved-price-basis",
            "timestamp": "2026-04-23T00:00:00+00:00",
            "market_id": "m-entry",
            "question": "Did we use the actual fill price?",
            "exchange": "kalshi",
            "direction": "BUY_YES",
            "status": "resolved",
            "resolved": True,
            "resolved_at": "2026-04-24T00:00:00+00:00",
            "market_price": 0.5,
            "entry_price": 0.6,
            "fill_price": 0.6,
            "position_size": 6.0,
            "outcome": "YES",
            "pnl": 3.72,
            "settlement_value": 9.72,
            "resolution_type": "settled",
            "decision_reason_code": "approved",
        }

        enriched = enrich_trade_audit_fields(row, fee_rate=0.07)

        self.assertEqual(enriched["entry_price"], 0.6)
        self.assertEqual(enriched["market_price"], 0.5)
        self.assertEqual(enriched["contracts"], 10.0)
        self.assertEqual(enriched["gross_pnl"], 4.0)
        self.assertEqual(enriched["fee_paid"], 0.28)
        self.assertEqual(enriched["expected_pnl"], 3.72)
        self.assertEqual(enriched["outcome"], "YES")
        self.assertEqual(enriched["resolution_outcome"], "YES")
        self.assertEqual(enriched["resolution_result"], "won")
        self.assertEqual(enriched["exit_price"], 1.0)
        self.assertEqual(enriched["integrity_status"], "ok")
        self.assertEqual(enriched["integrity_errors"], [])

    def test_resolved_enrichment_canonicalizes_legacy_trade_result_outcome(self):
        row = {
            "trade_id": "resolved-legacy-result",
            "timestamp": "2026-04-23T00:00:00+00:00",
            "market_id": "m-legacy-result",
            "question": "Did legacy outcome mean the trade result?",
            "exchange": "kalshi",
            "direction": "BUY_NO",
            "status": "resolved",
            "resolved": True,
            "resolved_at": "2026-04-24T00:00:00+00:00",
            "market_price": 0.6,
            "entry_price": 0.6,
            "position_size": 6.0,
            "outcome": "won",
            "pnl": 3.72,
            "settlement_value": 9.72,
            "resolution_type": "settled",
            "decision_reason_code": "approved",
        }

        enriched = enrich_trade_audit_fields(row, fee_rate=0.07)

        self.assertEqual(enriched["outcome"], "NO")
        self.assertEqual(enriched["resolution_outcome"], "NO")
        self.assertEqual(enriched["resolution_result"], "won")
        self.assertEqual(enriched["exit_price"], 0.0)
        self.assertEqual(enriched["settlement_value"], 9.72)
        self.assertEqual(enriched["integrity_status"], "ok")
        self.assertEqual(enriched["integrity_errors"], [])

    def test_resolved_enrichment_forces_canonical_resolved_lifecycle(self):
        row = {
            "trade_id": "resolved-prior-open-state",
            "timestamp": "2026-04-23T00:00:00+00:00",
            "market_id": "m-prior-open",
            "question": "Did this settle after being open?",
            "exchange": "kalshi",
            "direction": "BUY_YES",
            "status": "filled",
            "lifecycle_state": "filled_open",
            "resolved": True,
            "resolved_at": "2026-04-24T00:00:00+00:00",
            "market_price": 0.6,
            "entry_price": 0.6,
            "position_size": 6.0,
            "outcome": "YES",
            "pnl": 3.72,
            "settlement_value": 9.72,
            "resolution_type": "settled",
            "decision_reason_code": "approved",
        }

        enriched = enrich_trade_audit_fields(row, fee_rate=0.07)

        self.assertEqual(enriched["status"], "resolved")
        self.assertEqual(enriched["lifecycle_state"], "resolved_position")
        self.assertEqual(enriched["outcome"], "YES")
        self.assertEqual(enriched["resolution_result"], "won")
        self.assertEqual(enriched["integrity_status"], "ok")
        self.assertEqual(enriched["integrity_errors"], [])

    def test_manual_mark_close_does_not_infer_market_resolution_from_trade_result(self):
        row = {
            "trade_id": "manual-close-result",
            "timestamp": "2026-04-23T00:00:00+00:00",
            "market_id": "m-manual-close",
            "question": "Was this manually closed?",
            "exchange": "kalshi",
            "direction": "BUY_NO",
            "status": "resolved",
            "resolved": True,
            "resolved_at": "2026-04-24T00:00:00+00:00",
            "market_price": 0.6,
            "entry_price": 0.6,
            "position_size": 6.0,
            "outcome": "won",
            "pnl": 1.25,
            "settlement_value": 7.25,
            "resolution_type": "manual_mark_close",
            "decision_reason_code": "approved",
        }

        enriched = enrich_trade_audit_fields(row, fee_rate=0.07)

        self.assertEqual(enriched["outcome"], "won")
        self.assertEqual(enriched["resolution_result"], "won")
        self.assertNotIn("resolution_outcome", enriched)
        self.assertNotIn("exit_price", enriched)
        self.assertEqual(enriched["settlement_value"], 7.25)
        self.assertEqual(enriched["integrity_status"], "ok")
        self.assertEqual(enriched["integrity_errors"], [])

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

    def test_normalize_parity_trade_row_surfaces_schema_gaps_lifecycle_and_price_deltas(self):
        row = {
            "trade_id": "gap-1",
            "timestamp": "2026-04-23T00:00:00+00:00",
            "market_id": "m-gap",
            "direction": "BUY_YES",
            "status": "placed",
            "lifecycle_state": "placed_open",
            "requested_size": 1.0,
            "approved_size": 1.0,
            "placed_size": 1.0,
            "filled_size": 1.0,
            "remaining_size": 0.0,
            "market_price": 0.4,
            "entry_price": 0.4,
            "decision_reason_code": "approved",
            "execution_snapshot_source": "book",
            "original_signal_snapshot": {"market_price": 0.40},
            "execution_snapshot": {"market_price": 0.46},
            "original_decision_reason_code": "approved",
            "execution_decision_reason_code": "price_above_threshold",
        }

        normalized = normalize_parity_trade_row(row, source="live")

        self.assertIn("missing_schema_name", normalized["schema_gaps"])
        self.assertIn("missing_event_key", normalized["schema_gaps"])
        self.assertIn("missing_exchange", normalized["schema_gaps"])
        self.assertEqual(normalized["status"], "filled")
        self.assertIn("filled_lifecycle_mismatch", normalized["lifecycle_contradictions"])
        self.assertTrue(normalized["decision_reason_delta"])
        self.assertTrue(normalized["execution_price_delta"])
        self.assertEqual(normalized["original_market_price"], 0.40)
        self.assertEqual(normalized["execution_market_price"], 0.46)
        self.assertAlmostEqual(normalized["execution_market_price_delta"], 0.06)

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
        self.assertIn("resolved_without_outcome", issues)
        self.assertIn("resolved_without_pnl", issues)
        self.assertIn("resolved_without_settlement_value", issues)

    def test_execution_contract_canonicalizes_resolved_flag_status(self):
        row = apply_execution_audit_contract(
            {
                "trade_id": "resolved-flag-mismatch",
                "timestamp": "2026-04-23T00:00:00+00:00",
                "market_id": "m6",
                "direction": "BUY_YES",
                "status": "filled",
                "resolved": True,
                "resolved_at": "2026-04-24T00:00:00+00:00",
                "outcome": "YES",
                "pnl": 1.0,
                "settlement_value": 2.0,
                "requested_size": 1.0,
                "approved_size": 1.0,
                "placed_size": 1.0,
                "filled_size": 1.0,
                "remaining_size": 0.0,
                "market_price": 0.5,
                "decision_reason_code": "approved",
            }
        )

        issues = validate_execution_audit_row(row)
        self.assertEqual(row["status"], "resolved")
        self.assertEqual(row["lifecycle_state"], "resolved_position")
        self.assertNotIn("resolved_flag_status_mismatch", issues)

    def test_execution_contract_preserves_legacy_string_false_resolved_flag(self):
        row = apply_execution_audit_contract(
            {
                "trade_id": "legacy-string-false-resolved",
                "timestamp": "2026-04-23T00:00:00+00:00",
                "market_id": "m-string-false",
                "direction": "BUY_YES",
                "status": "filled",
                "resolved": "False",
                "requested_size": 1.0,
                "approved_size": 1.0,
                "placed_size": 1.0,
                "filled_size": 1.0,
                "remaining_size": 0.0,
                "market_price": 0.5,
                "decision_reason_code": "approved",
            }
        )

        issues = validate_execution_audit_row(row)
        self.assertEqual(row["status"], "filled")
        self.assertEqual(row["lifecycle_state"], "filled_open")
        self.assertFalse(row["resolved"])
        self.assertNotIn("resolved_flag_status_mismatch", issues)

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

            view = build_parity_view(
                data_dir,
                config={"trading": {"mode": "live"}, "parity_mode": {"enabled": True, "comparison_mode": "identical_risk"}},
            )

            self.assertEqual(view["paper_summary"]["total_rows"], 1)
            self.assertEqual(view["comparison_context"]["paper_mode_label"], "parity paper")
            self.assertEqual(view["comparison_context"]["live_mode_label"], "identical-risk comparison")
            self.assertTrue(view["comparison_context"]["apples_to_apples"])
            self.assertEqual(view["live_summary"]["total_rows"], 2)
            self.assertEqual(view["comparison"]["paper_rows"], 1)
            self.assertEqual(view["comparison"]["live_rows"], 2)
            self.assertEqual(view["comparison_artifact_path"], str(data_dir / "parity_comparison.json"))
            self.assertEqual(view["paper_rows"][0]["decision_reason_code"], "approved")
            self.assertEqual(view["live_rows"][0]["trade_id"], "live-1")
            self.assertEqual(view["live_rows"][0]["status"], "placed")
            self.assertEqual(view["live_rows"][1]["status"], "rejected")
            self.assertFalse(view["live_rows"][1]["contract_issues"])

    def test_build_parity_view_labels_enabled_false_identical_risk_live_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            (data_dir / "paper").mkdir(parents=True)
            (data_dir / "live").mkdir(parents=True)

            view = build_parity_view(
                data_dir,
                config={"trading": {"mode": "live"}, "parity_mode": {"enabled": False, "comparison_mode": "identical_risk"}},
            )
            context = view["comparison_context"]

            self.assertFalse(context["parity_mode_enabled"])
            self.assertEqual(context["parity_comparison_mode"], "identical_risk")
            self.assertEqual(context["live_mode_label"], "identical-risk comparison")
            self.assertEqual(context["live_risk_preset_mode"], "paper")
            self.assertTrue(context["apples_to_apples"])
            self.assertFalse(context["differences_expected"])

    def test_build_parity_view_keeps_enabled_false_production_live_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            (data_dir / "paper").mkdir(parents=True)
            (data_dir / "live").mkdir(parents=True)

            view = build_parity_view(
                data_dir,
                config={"trading": {"mode": "live"}, "parity_mode": {"enabled": False, "comparison_mode": "production"}},
            )
            context = view["comparison_context"]

            self.assertFalse(context["parity_mode_enabled"])
            self.assertEqual(context["parity_comparison_mode"], "production")
            self.assertEqual(context["live_mode_label"], "live")
            self.assertEqual(context["live_risk_preset_mode"], "live")
            self.assertFalse(context["apples_to_apples"])
            self.assertTrue(context["differences_expected"])

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

    def test_build_parity_view_compares_equivalent_paper_live_rows_and_exports_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            (data_dir / "paper").mkdir(parents=True)
            (data_dir / "live").mkdir(parents=True)
            (data_dir / "paper" / "sim_20260423_000000.json").write_text(
                '{"trades":[{"schema_name":"execution_audit_row","schema_version":1,"id":"paper-1","trade_id":"paper-1","timestamp":"2026-04-23T00:00:00+00:00","market_id":"m1","event_key":"event-1","question":"Q","exchange":"kalshi","direction":"BUY_YES","status":"filled","lifecycle_state":"filled_open","requested_size":1.0,"approved_size":1.0,"placed_size":1.0,"filled_size":1.0,"remaining_size":0.0,"reserved_capital":1.0,"market_price":0.4,"entry_price":0.4,"decision_reason_code":"approved","parity_mode_enabled":true,"execution_revalidated":true,"execution_revalidation_outcome":"approved","execution_snapshot_source":"book","original_decision_reason_code":"approved","execution_decision_reason_code":"approved","original_signal_snapshot":{"market_price":0.4},"execution_snapshot":{"market_price":0.4}}]}'
            )
            (data_dir / "live" / "trades.jsonl").write_text(
                '{"schema_name":"execution_audit_row","schema_version":1,"trade_id":"live-1","timestamp":"2026-04-23T00:00:01+00:00","market_id":"m1","event_key":"event-1","question":"Q","exchange":"kalshi","direction":"BUY_YES","status":"rejected","lifecycle_state":"revalidation_rejected","failure_stage":"revalidation","requested_size":1.0,"approved_size":1.0,"placed_size":0.0,"filled_size":0.0,"remaining_size":0.0,"reserved_capital":0.0,"market_price":0.4,"entry_price":0.4,"decision_reason_code":"price_above_threshold","parity_mode_enabled":true,"execution_revalidated":true,"execution_revalidation_outcome":"rejected","execution_snapshot_source":"book","original_decision_reason_code":"approved","execution_decision_reason_code":"price_above_threshold","original_signal_snapshot":{"market_price":0.4},"execution_snapshot":{"market_price":0.47}}\n'
            )

            view = build_parity_view(data_dir)
            comparison = view["comparison"]

            self.assertEqual(comparison["matched_keys"], 1)
            self.assertEqual(comparison["matched_pairs"], 1)
            self.assertEqual(comparison["mismatched_pair_count"], 1)
            self.assertIn(("logic_drift", 1), comparison["drift_category_counts"])
            self.assertIn(("lifecycle_drift", 1), comparison["drift_category_counts"])
            self.assertIn(("execution_drift", 1), comparison["drift_category_counts"])
            self.assertIn(("status", 1), comparison["mismatch_field_counts"])
            self.assertEqual(comparison["mismatch_examples"][0]["drift_categories"], ["lifecycle_drift", "execution_drift", "logic_drift"])
            self.assertEqual(view["live_summary"]["decision_delta_rows"], 1)
            self.assertEqual(view["live_summary"]["execution_price_delta_rows"], 1)
            self.assertEqual(view["live_summary"]["top_decision_delta_pairs"][0][0], "approved -> price_above_threshold")

            artifact = write_parity_comparison_artifact(
                data_dir,
                config={"trading": {"mode": "live"}, "parity_mode": {"enabled": True, "comparison_mode": "identical_risk"}},
            )
            self.assertTrue(artifact.exists())
            artifact_text = artifact.read_text()
            self.assertIn('"comparison"', artifact_text)
            self.assertIn('"comparison_context"', artifact_text)
            self.assertIn('"drift_category_counts"', artifact_text)
            self.assertIn('"live_mode_label": "identical-risk comparison"', artifact_text)

    def test_build_parity_view_classifies_logic_drift_without_risk_or_lifecycle_drift(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            (data_dir / "paper").mkdir(parents=True)
            (data_dir / "live").mkdir(parents=True)
            (data_dir / "paper" / "sim_20260423_000000.json").write_text(
                '{"trades":[{"schema_name":"execution_audit_row","schema_version":1,"trade_id":"paper-logic-1","timestamp":"2026-04-23T00:00:00+00:00","market_id":"m-logic","event_key":"event-logic","question":"Q","exchange":"kalshi","direction":"BUY_YES","status":"filled","lifecycle_state":"filled_open","requested_size":1.0,"approved_size":1.0,"placed_size":1.0,"filled_size":1.0,"remaining_size":0.0,"reserved_capital":1.0,"market_price":0.4,"entry_price":0.4,"decision_reason_code":"edge_above_threshold","parity_mode_enabled":true,"execution_revalidated":true,"execution_revalidation_outcome":"approved","execution_snapshot_source":"book","original_decision_reason_code":"edge_above_threshold","execution_decision_reason_code":"edge_above_threshold","original_signal_snapshot":{"market_price":0.4},"execution_snapshot":{"market_price":0.4}}]}'
            )
            (data_dir / "live" / "trades.jsonl").write_text(
                '{"schema_name":"execution_audit_row","schema_version":1,"trade_id":"live-logic-1","timestamp":"2026-04-23T00:00:01+00:00","market_id":"m-logic","event_key":"event-logic","question":"Q","exchange":"kalshi","direction":"BUY_YES","status":"filled","lifecycle_state":"filled_open","requested_size":1.0,"approved_size":1.0,"placed_size":1.0,"filled_size":1.0,"remaining_size":0.0,"reserved_capital":1.0,"market_price":0.4,"entry_price":0.4,"decision_reason_code":"momentum_tiebreaker","parity_mode_enabled":true,"execution_revalidated":true,"execution_revalidation_outcome":"approved","execution_snapshot_source":"book","original_decision_reason_code":"momentum_tiebreaker","execution_decision_reason_code":"momentum_tiebreaker","original_signal_snapshot":{"market_price":0.4},"execution_snapshot":{"market_price":0.4}}\n'
            )

            view = build_parity_view(data_dir)
            comparison = view["comparison"]

            self.assertEqual(comparison["matched_pairs"], 1)
            self.assertEqual(comparison["drift_category_counts"], [("logic_drift", 1)])
            self.assertEqual(comparison["mismatch_examples"][0]["drift_categories"], ["logic_drift"])
            self.assertIn(("decision_reason_code", 1), comparison["mismatch_field_counts"])

    def test_build_parity_view_keeps_risk_drift_for_risk_stage_rejections(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            (data_dir / "paper").mkdir(parents=True)
            (data_dir / "live").mkdir(parents=True)
            (data_dir / "paper" / "sim_20260423_000000.json").write_text(
                '{"trades":[{"schema_name":"execution_audit_row","schema_version":1,"trade_id":"paper-risk-1","timestamp":"2026-04-23T00:00:00+00:00","market_id":"m-risk","event_key":"event-risk","question":"Q","exchange":"kalshi","direction":"BUY_YES","status":"approved","lifecycle_state":"approved","requested_size":1.0,"approved_size":1.0,"placed_size":0.0,"filled_size":0.0,"remaining_size":0.0,"reserved_capital":0.0,"market_price":0.4,"entry_price":0.4,"decision_reason_code":"approved","execution_snapshot_source":"book"}]}'
            )
            (data_dir / "live" / "trades.jsonl").write_text(
                '{"schema_name":"execution_audit_row","schema_version":1,"trade_id":"live-risk-1","timestamp":"2026-04-23T00:00:01+00:00","market_id":"m-risk","event_key":"event-risk","question":"Q","exchange":"kalshi","direction":"BUY_YES","status":"rejected","lifecycle_state":"risk_check_rejected","failure_stage":"risk_block","requested_size":1.0,"approved_size":0.0,"placed_size":0.0,"filled_size":0.0,"remaining_size":0.0,"reserved_capital":0.0,"market_price":0.4,"entry_price":0.4,"decision_reason_code":"kelly_zero_size","execution_snapshot_source":"book"}\n'
            )

            comparison = build_parity_view(data_dir)["comparison"]

            self.assertIn(("risk_drift", 1), comparison["drift_category_counts"])
            self.assertEqual(comparison["mismatch_examples"][0]["drift_categories"], ["lifecycle_drift", "risk_drift"])

    def test_build_parity_view_does_not_map_generic_contract_invalidity_to_lifecycle_drift(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            (data_dir / "paper").mkdir(parents=True)
            (data_dir / "live").mkdir(parents=True)
            (data_dir / "paper" / "sim_20260423_000000.json").write_text(
                '{"trades":[{"schema_name":"execution_audit_row","schema_version":1,"trade_id":"paper-contract-1","timestamp":"2026-04-23T00:00:00+00:00","market_id":"m-contract","event_key":"event-contract","question":"Q","exchange":"kalshi","direction":"BUY_YES","status":"filled","lifecycle_state":"filled_open","requested_size":1.0,"approved_size":1.0,"placed_size":1.0,"filled_size":1.0,"remaining_size":0.0,"reserved_capital":1.0,"market_price":0.4,"entry_price":0.4,"decision_reason_code":"approved","parity_mode_enabled":true,"execution_revalidated":true,"execution_revalidation_outcome":"approved","execution_snapshot_source":"book","original_signal_snapshot":{"market_price":0.4},"execution_snapshot":{"market_price":0.4}}]}'
            )
            (data_dir / "live" / "trades.jsonl").write_text(
                '{"schema_name":"execution_audit_row","schema_version":1,"trade_id":"live-contract-1","timestamp":"2026-04-23T00:00:01+00:00","market_id":"m-contract","event_key":"event-contract","question":"Q","exchange":"kalshi","direction":"BUY_YES","status":"filled","lifecycle_state":"filled_open","requested_size":1.0,"approved_size":1.0,"placed_size":1.0,"filled_size":1.0,"remaining_size":0.0,"reserved_capital":1.0,"market_price":0.4,"entry_price":0.4,"decision_reason_code":"approved","parity_mode_enabled":true,"execution_revalidated":true,"execution_revalidation_outcome":"approved","execution_snapshot_source":"book","original_signal_snapshot":{"market_price":0.4}}\n'
            )

            comparison = build_parity_view(data_dir)["comparison"]

            self.assertIn(("contract_valid", 1), comparison["mismatch_field_counts"])
            self.assertEqual(comparison["drift_category_counts"], [("logic_drift", 1)])
            self.assertEqual(comparison["mismatch_examples"][0]["drift_categories"], ["logic_drift"])


if __name__ == "__main__":
    unittest.main()
