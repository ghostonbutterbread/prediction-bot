import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.file_ops import append_jsonl
from bot.hidden_gem_evidence import (
    format_hidden_gem_evidence_summary,
    summarize_hidden_gem_evidence_cards,
)
from bot.prediction_lab import PredictionLab
from scripts import analyze as paper_analyze


def _card(*, shape="bucket", tier="current", weather_reject=None, beta_reject=None):
    return {
        "artifact_version": 1,
        "lane": "hidden_gem",
        "market_id": "KXHIGHNY-260506-T71",
        "weather_shape": shape,
        "hidden_gem_tier": tier,
        "reason_codes": {
            "weather_reject": weather_reject,
            "beta_reject": beta_reject,
            "resize": None,
        },
    }


def _artifact_row(*, action, reason_code, approved, card):
    return {
        "market_id": "KXHIGHNY-260506-T71",
        "direction": action,
        "decision_artifact": {
            "final_action": action,
            "final_reason_code": reason_code,
            "shared_core_decision": {
                "approved": approved,
                "reason_code": reason_code,
                "reasoning": {"hidden_gem_evidence_card": card},
            },
        },
    }


class HiddenGemEvidenceReportingTests(unittest.TestCase):
    def test_aggregates_approved_rejected_beta_and_missing_cards(self):
        rows = [
            _artifact_row(
                action="BUY_YES",
                reason_code="approved",
                approved=True,
                card=_card(shape="bucket", tier="current"),
            ),
            _artifact_row(
                action="SKIP",
                reason_code="weather_bucket_hidden_gem_missing_distribution_probability",
                approved=False,
                card=_card(
                    shape="bucket",
                    tier="exceptional",
                    beta_reject="weather_bucket_hidden_gem_missing_distribution_probability",
                ),
            ),
            {
                "market_id": "legacy-no-card",
                "direction": "BUY_YES",
                "decision_reason_code": "approved",
            },
        ]

        summary = summarize_hidden_gem_evidence_cards(rows)

        self.assertEqual(summary["rows_scanned"], 3)
        self.assertEqual(summary["card_rows"], 2)
        self.assertEqual(summary["approved_cards"], 1)
        self.assertEqual(summary["rejected_cards"], 1)
        self.assertEqual(summary["beta_rejected_cards"], 1)
        self.assertEqual(summary["no_card_rows"], 1)
        self.assertEqual(summary["insufficient_data_rows"], 0)
        self.assertEqual(
            {
                (row["weather_shape"], row["hidden_gem_tier"], row["reason_code"]): row["count"]
                for row in summary["by_shape_tier_reason"]
            },
            {
                ("bucket", "current", "approved"): 1,
                ("bucket", "exceptional", "weather_bucket_hidden_gem_missing_distribution_probability"): 1,
            },
        )

    def test_rejected_buy_direction_card_is_not_counted_as_approved(self):
        summary = summarize_hidden_gem_evidence_cards(
            [
                _artifact_row(
                    action="BUY_YES",
                    reason_code="weather_bucket_hidden_gem_missing_distribution_probability",
                    approved=False,
                    card=_card(
                        shape="bucket",
                        tier="exceptional",
                        beta_reject="weather_bucket_hidden_gem_missing_distribution_probability",
                    ),
                )
                | {"status": "rejected"}
            ]
        )

        self.assertEqual(summary["approved_cards"], 0)
        self.assertEqual(summary["rejected_cards"], 1)
        self.assertEqual(summary["by_shape_tier_reason"][0]["approved"], 0)
        self.assertEqual(summary["by_shape_tier_reason"][0]["rejected"], 1)

    def test_bucket_source_station_quality_beta_reason_is_reported(self):
        reason = "weather_bucket_hidden_gem_source_station_quality_below_minimum"
        summary = summarize_hidden_gem_evidence_cards(
            [
                _artifact_row(
                    action="BUY_YES",
                    reason_code="approved",
                    approved=True,
                    card=_card(shape="bucket", tier="normal", beta_reject=reason),
                )
            ]
        )

        self.assertEqual(summary["beta_rejected_cards"], 1)
        self.assertEqual(summary["reason_code_counts"], {reason: 1})
        self.assertEqual(summary["by_shape_tier_reason"][0]["reason_code"], reason)

    def test_incomplete_card_counts_as_insufficient_data_without_crashing(self):
        summary = summarize_hidden_gem_evidence_cards(
            [{"market_id": "incomplete", "hidden_gem_evidence_card": {"reason_codes": {}}}]
        )

        self.assertEqual(summary["card_rows"], 1)
        self.assertEqual(summary["insufficient_data_rows"], 1)
        self.assertEqual(summary["by_shape_tier_reason"][0]["weather_shape"], "unknown")
        self.assertEqual(summary["by_shape_tier_reason"][0]["hidden_gem_tier"], "unknown")

    def test_format_report_includes_concise_hidden_gem_line(self):
        hidden_summary = summarize_hidden_gem_evidence_cards(
            [
                _artifact_row(
                    action="BUY_YES",
                    reason_code="approved",
                    approved=True,
                    card=_card(shape="bucket", tier="current"),
                ),
                {"market_id": "legacy-no-card", "direction": "BUY_YES"},
            ]
        )

        report = paper_analyze.format_report(
            {
                "timestamp": "2026-05-06T08:00:00-07:00",
                "summary": {
                    "current_session": "s1",
                    "scans": 0,
                    "current_trades": 2,
                    "resolved": 0,
                    "trusted_resolved_positions": 0,
                    "resolved_events": 0,
                },
                "performance": {},
                "event_performance": {},
                "signal_quality": {},
                "hidden_gem_evidence_cards": hidden_summary,
                "issues": [],
                "actions": [],
            }
        )

        self.assertIn("Hidden-gem evidence: cards 1/2", report)
        self.assertIn("final approved 1 rejected 0", report)
        self.assertIn("no-card 1 insufficient 0", report)
        self.assertIn("bucket/current/approved=1", report)
        self.assertIn("Hidden-gem evidence: cards 1/2", format_hidden_gem_evidence_summary(hidden_summary))

    def test_analyze_summarizes_raw_rejected_card_rows_filtered_from_accounting(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paper_dir = Path(tmpdir) / "paper"
            paper_dir.mkdir()
            session_path = paper_dir / "sim_test.json"
            session_path.write_text(
                json.dumps(
                    {
                        "session_id": "s1",
                        "trades": [
                            _artifact_row(
                                action="SKIP",
                                reason_code="weather_bucket_hidden_gem_missing_distribution_probability",
                                approved=False,
                                card=_card(
                                    shape="bucket",
                                    tier="exceptional",
                                    beta_reject="weather_bucket_hidden_gem_missing_distribution_probability",
                                ),
                            )
                            | {
                                "position_size": 0.0,
                                "status": "rejected",
                                "decision_reason_code": "weather_bucket_hidden_gem_missing_distribution_probability",
                            }
                        ],
                    }
                )
            )

            with patch.dict(os.environ, {"ANALYZE_DATA_DIR": tmpdir}, clear=False):
                sessions = paper_analyze.load_sessions()
                result = paper_analyze.analyze(prune_logs=False)

        self.assertEqual(len(sessions[-1]["trades"]), 0)
        self.assertEqual(sessions[-1]["summary"]["ignored_invalid_trades"], 1)
        summary = result["hidden_gem_evidence_cards"]
        self.assertEqual(summary["rows_scanned"], 1)
        self.assertEqual(summary["card_rows"], 1)
        self.assertEqual(summary["rejected_cards"], 1)
        self.assertEqual(summary["beta_rejected_cards"], 1)

    def test_prediction_lab_summary_aggregates_deduped_prediction_and_snapshot_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lab = PredictionLab(
                {
                    "data_dir": tmpdir,
                    "prediction_lab": {"mode": "collector", "groups": ["weather"]},
                    "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
                }
            )
            append_jsonl(
                lab.predictions_path,
                {
                    "prediction_id": "p1",
                    "run_id": "r1",
                    "market_id": "m1",
                    "group": "weather",
                    "status": "open",
                    "confidence": 0.8,
                    "direction": "BUY_YES",
                },
            )
            append_jsonl(
                lab.market_snapshots_path,
                _artifact_row(
                    action="BUY_YES",
                    reason_code="approved",
                    approved=True,
                    card=_card(shape="bucket", tier="current"),
                )
                | {"run_id": "r1", "market_id": "m1"},
            )
            append_jsonl(
                lab.market_snapshots_path,
                _artifact_row(
                    action="SKIP",
                    reason_code="weather_tail_hidden_gem_live_probability_mismatch",
                    approved=False,
                    card=_card(
                        shape="tail_high",
                        tier="suspicious",
                        beta_reject="weather_tail_hidden_gem_live_probability_mismatch",
                    ),
                )
                | {"run_id": "r1", "market_id": "m2"},
            )

            summary = lab.summarize()["hidden_gem_evidence_cards"]

        self.assertEqual(summary["rows_scanned"], 2)
        self.assertEqual(summary["card_rows"], 2)
        self.assertEqual(summary["approved_cards"], 1)
        self.assertEqual(summary["rejected_cards"], 1)
        self.assertEqual(summary["beta_rejected_cards"], 1)


if __name__ == "__main__":
    unittest.main()
