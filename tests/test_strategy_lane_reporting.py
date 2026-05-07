import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.strategy_lane_reporting import format_strategy_lane_summary, summarize_strategy_lanes
from scripts import analyze as paper_analyze


def _lane_row(*, action, reason_code, approved, lane_id, would_lane_id=None, differs=False):
    lane = {
        "lane_id": lane_id,
        "allowed": True,
        "reason_code": f"{lane_id}_selected",
        "evidence": {
            "beta_lane_gate": {
                "lane_id": would_lane_id or lane_id,
                "allowed": True,
                "reason_code": f"{would_lane_id or lane_id}_selected",
                "differs_from_final": differs,
            }
        },
    }
    return {
        "market_id": "KXHIGHNY-260506-T71",
        "direction": action,
        "decision_artifact": {
            "final_action": action,
            "final_reason_code": reason_code,
            "shared_core_decision": {
                "approved": approved,
                "reason_code": reason_code,
                "reasoning": {"strategy_lane": lane},
            },
        },
    }


class StrategyLaneReportingTests(unittest.TestCase):
    def test_summarizes_selected_would_select_and_slow_profit_deltas(self):
        summary = summarize_strategy_lanes(
            [
                _lane_row(
                    action="SKIP",
                    reason_code="edge_below_threshold",
                    approved=False,
                    lane_id="edge",
                    would_lane_id="confidence_slow_profit",
                    differs=True,
                ),
                _lane_row(
                    action="BUY_YES",
                    reason_code="approved",
                    approved=True,
                    lane_id="hidden_gem",
                    would_lane_id="hidden_gem",
                ),
                {"market_id": "legacy-no-lane"},
            ]
        )

        self.assertEqual(summary["rows_scanned"], 3)
        self.assertEqual(summary["lane_rows"], 2)
        self.assertEqual(summary["no_lane_rows"], 1)
        self.assertEqual(summary["selected_lane_counts"], {"edge": 1, "hidden_gem": 1})
        self.assertEqual(
            summary["would_select_lane_counts"],
            {"confidence_slow_profit": 1, "hidden_gem": 1},
        )
        self.assertEqual(summary["approved_lane_rows"], 1)
        self.assertEqual(summary["rejected_lane_rows"], 1)
        self.assertEqual(summary["selected_slow_profit_rows"], 0)
        self.assertEqual(summary["would_select_slow_profit_rows"], 1)
        self.assertEqual(summary["slow_profit_differs_from_final_rows"], 1)
        self.assertEqual(summary["lane_selection_delta_rows"], 1)
        self.assertIn("slow-profit selected 0 would 1 diff 1", format_strategy_lane_summary(summary))

    def test_analyze_summarizes_raw_rejected_lane_rows_filtered_from_accounting(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paper_dir = Path(tmpdir) / "paper"
            paper_dir.mkdir()
            session_path = paper_dir / "sim_test.json"
            session_path.write_text(
                json.dumps(
                    {
                        "session_id": "s1",
                        "trades": [
                            _lane_row(
                                action="SKIP",
                                reason_code="edge_below_threshold",
                                approved=False,
                                lane_id="edge",
                                would_lane_id="confidence_slow_profit",
                                differs=True,
                            )
                            | {
                                "position_size": 0.0,
                                "status": "rejected",
                                "decision_reason_code": "edge_below_threshold",
                            }
                        ],
                    }
                )
            )

            with patch.dict(os.environ, {"ANALYZE_DATA_DIR": tmpdir}, clear=False), patch.object(
                paper_analyze,
                "summarize_log_storage",
                return_value=None,
            ):
                sessions = paper_analyze.load_sessions()
                result = paper_analyze.analyze(prune_logs=False)

        self.assertEqual(len(sessions[-1]["trades"]), 0)
        self.assertEqual(sessions[-1]["summary"]["ignored_invalid_trades"], 1)
        summary = result["strategy_lanes"]
        self.assertEqual(summary["rows_scanned"], 1)
        self.assertEqual(summary["lane_rows"], 1)
        self.assertEqual(summary["would_select_slow_profit_rows"], 1)
        self.assertEqual(summary["slow_profit_differs_from_final_rows"], 1)

    def test_format_report_includes_strategy_lane_line(self):
        strategy_lane_summary = summarize_strategy_lanes(
            [
                _lane_row(
                    action="SKIP",
                    reason_code="edge_below_threshold",
                    approved=False,
                    lane_id="edge",
                    would_lane_id="confidence_slow_profit",
                    differs=True,
                )
            ]
        )

        report = paper_analyze.format_report(
            {
                "timestamp": "2026-05-07T08:00:00-07:00",
                "summary": {
                    "current_session": "s1",
                    "scans": 0,
                    "current_trades": 1,
                    "resolved": 0,
                    "trusted_resolved_positions": 0,
                    "resolved_events": 0,
                },
                "performance": {},
                "event_performance": {},
                "signal_quality": {},
                "strategy_lanes": strategy_lane_summary,
                "issues": [],
                "actions": [],
            }
        )

        self.assertIn("Strategy lanes: rows 1/1", report)
        self.assertIn("would-select confidence_slow_profit 1", report)


if __name__ == "__main__":
    unittest.main()
