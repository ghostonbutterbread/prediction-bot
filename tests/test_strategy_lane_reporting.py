import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.hidden_gem_evidence import summarize_hidden_gem_evidence_cards
from bot.strategy_lane_reporting import (
    build_strategy_lane_rollout_readiness,
    format_strategy_lane_rollout_readiness,
    format_strategy_lane_summary,
    summarize_strategy_lanes,
)
from scripts import analyze as paper_analyze


def _lane_row(*, action, reason_code, approved, lane_id, would_lane_id=None, differs=False, lane_sizing=None):
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
    reasoning = {"strategy_lane": lane}
    if isinstance(lane_sizing, dict):
        reasoning["lane_sizing"] = dict(lane_sizing)
    return {
        "market_id": "KXHIGHNY-260506-T71",
        "direction": action,
        "decision_artifact": {
            "final_action": action,
            "final_reason_code": reason_code,
            "shared_core_decision": {
                "approved": approved,
                "reason_code": reason_code,
                "reasoning": reasoning,
            },
        },
    }


def _hidden_gem_card():
    return {
        "artifact_version": 1,
        "lane": "hidden_gem",
        "market_id": "KXHIGHNY-260506-T71",
        "weather_shape": "bucket",
        "hidden_gem_tier": "normal",
        "reason_codes": {"weather_reject": None, "beta_reject": None, "resize": None},
    }


def _shadow_policy_status(**features):
    enabled = {
        "weather_hidden_gem_evidence_card": False,
        "bucket_distribution_scoring": False,
        "hidden_gem_lane_gates": False,
        "lane_sizing_caps": False,
    }
    enabled.update(features)
    return {
        "version": "beta",
        "mode": "shadow",
        "active": True,
        "shadow": True,
        "enforce": False,
        "enabled_features": enabled,
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
        self.assertEqual(summary["beta_lane_gate_rows"], 2)
        self.assertEqual(summary["beta_lane_gate_missing_rows"], 0)
        self.assertEqual(summary["lane_selection_delta_rows"], 1)
        self.assertIn("slow-profit selected 0 would 1 diff 1", format_strategy_lane_summary(summary))

    def test_summarizes_lane_sizing_from_direct_and_decision_trace_rows(self):
        summary = summarize_strategy_lanes(
            [
                {
                    "strategy_lane": {"lane_id": "edge", "evidence": {}},
                    "lane_sizing": {
                        "lane_id": "edge",
                        "configured": True,
                        "requested_size": 10.0,
                        "beta_adjusted_size": 4.0,
                        "would_adjust_size": True,
                        "applied": False,
                        "preserved_stable_size": True,
                        "shadow": True,
                    },
                },
                {
                    "direction": "BUY_YES",
                    "decision_trace": {
                        "strategy_lane": {"lane_id": "hidden_gem", "evidence": {}},
                        "lane_sizing": {
                            "lane_id": "hidden_gem",
                            "configured": True,
                            "final_requested_size_before_lane_caps": 8.0,
                            "beta_adjusted_size": 2.0,
                            "applied": True,
                            "applied_size": 2.0,
                        },
                    },
                },
            ]
        )

        self.assertEqual(summary["lane_sizing_rows"], 2)
        self.assertEqual(summary["lane_sizing_configured_rows"], 2)
        self.assertEqual(summary["lane_sizing_would_adjust_rows"], 2)
        self.assertEqual(summary["lane_sizing_applied_rows"], 1)
        self.assertEqual(summary["lane_sizing_preserved_rows"], 1)
        self.assertEqual(summary["lane_sizing_shadow_rows"], 1)
        self.assertEqual(summary["lane_sizing_selected_lane_counts"], {"edge": 1, "hidden_gem": 1})
        self.assertEqual(summary["lane_sizing_size_counts"], {"applied": 1, "beta_adjusted": 2, "requested": 2})
        self.assertEqual(summary["lane_sizing_size_totals"], {"applied": 2.0, "beta_adjusted": 6.0, "requested": 18.0})
        self.assertEqual(summary["lane_sizing_size_avgs"], {"applied": 2.0, "beta_adjusted": 3.0, "requested": 9.0})
        line = format_strategy_lane_summary(summary)
        self.assertIn("sizing configured 2/2 would-adjust 2 applied 1 preserved 1 shadow 1", line)
        self.assertIn("sizes req 18.00 avg 9.00 beta 6.00 avg 3.00 applied 2.00 avg 2.00", line)

    def test_ignores_non_finite_lane_sizing_values_without_crashing(self):
        summary = summarize_strategy_lanes(
            [
                {
                    "strategy_lane": {"lane_id": "edge", "evidence": {}},
                    "lane_sizing": {
                        "lane_id": "edge",
                        "configured": True,
                        "requested_size": "nan",
                        "beta_adjusted_size": "inf",
                        "applied_size": "not-a-number",
                    },
                },
                {
                    "decision_artifact": {
                        "shared_core_decision": {
                            "reasoning": {
                                "lane_sizing": {
                                    "configured": False,
                                    "requested_size": "",
                                    "metadata_adjusted_size": None,
                                }
                            }
                        }
                    }
                },
            ]
        )

        self.assertEqual(summary["lane_sizing_rows"], 2)
        self.assertEqual(summary["lane_sizing_configured_rows"], 1)
        self.assertEqual(summary["lane_sizing_size_counts"], {})
        self.assertEqual(summary["lane_sizing_size_totals"], {})
        self.assertEqual(summary["lane_sizing_size_avgs"], {})
        self.assertIn("sizing configured 1/2", format_strategy_lane_summary(summary))

    def test_builds_ready_shadow_rollout_checklist_from_clean_evidence(self):
        rows = [
            _lane_row(
                action="SKIP",
                reason_code="edge_below_threshold",
                approved=False,
                lane_id="edge",
                would_lane_id="confidence_slow_profit",
                differs=True,
                lane_sizing={
                    "lane_id": "edge",
                    "configured": True,
                    "requested_size": 10.0,
                    "beta_adjusted_size": 3.0,
                    "would_adjust_size": True,
                    "differs_from_final": True,
                    "shadow": True,
                },
            )
            | {"hidden_gem_evidence_card": _hidden_gem_card()},
            _lane_row(
                action="BUY_YES",
                reason_code="approved",
                approved=True,
                lane_id="hidden_gem",
                would_lane_id="hidden_gem",
                lane_sizing={
                    "lane_id": "hidden_gem",
                    "configured": True,
                    "requested_size": 2.0,
                    "beta_adjusted_size": 2.0,
                    "shadow": True,
                },
            )
            | {"hidden_gem_evidence_card": _hidden_gem_card()},
        ]

        readiness = build_strategy_lane_rollout_readiness(
            policy_status=_shadow_policy_status(
                weather_hidden_gem_evidence_card=True,
                hidden_gem_lane_gates=True,
                lane_sizing_caps=True,
            ),
            strategy_lane_summary=summarize_strategy_lanes(rows),
            hidden_gem_evidence_summary=summarize_hidden_gem_evidence_cards(rows),
        )

        self.assertEqual(readiness["status"], "ready")
        self.assertTrue(readiness["ready_for_enforce"])
        self.assertEqual(readiness["blockers"], [])
        self.assertEqual(readiness["warnings"], [])
        self.assertEqual(readiness["coverage"]["hidden_gem_evidence_cards"]["coverage_pct"], 100.0)
        self.assertEqual(readiness["coverage"]["lane_delta"]["coverage_pct"], 100.0)
        self.assertEqual(readiness["coverage"]["lane_sizing_delta"]["coverage_pct"], 100.0)
        line = format_strategy_lane_rollout_readiness(readiness)
        self.assertIn("Strategy lane readiness: ready", line)
        self.assertIn("cards 2/2 clean 2", line)
        self.assertIn("lane-delta 2/2 diff 1", line)
        self.assertIn("sizing-delta 2/2 diff 1", line)

    def test_rollout_checklist_blocks_legacy_shadow_policy_without_normalized_flags(self):
        rows = [
            _lane_row(
                action="SKIP",
                reason_code="edge_below_threshold",
                approved=False,
                lane_id="edge",
                would_lane_id="confidence_slow_profit",
                differs=True,
                lane_sizing={
                    "lane_id": "edge",
                    "configured": True,
                    "requested_size": 10.0,
                    "beta_adjusted_size": 3.0,
                    "would_adjust_size": True,
                    "differs_from_final": True,
                    "shadow": True,
                },
            )
            | {"hidden_gem_evidence_card": _hidden_gem_card()}
        ]

        readiness = build_strategy_lane_rollout_readiness(
            policy_status={
                "version": "beta",
                "mode": "shadow",
                "enabled_features": {
                    "weather_hidden_gem_evidence_card": True,
                    "hidden_gem_lane_gates": True,
                    "lane_sizing_caps": True,
                },
            },
            strategy_lane_summary=summarize_strategy_lanes(rows),
            hidden_gem_evidence_summary=summarize_hidden_gem_evidence_cards(rows),
        )

        self.assertEqual(readiness["status"], "blocked")
        self.assertFalse(readiness["ready_for_enforce"])
        self.assertIn("pre_enforce_shadow_policy", self._failed_check_names(readiness))

    def test_rollout_checklist_blocks_malformed_shadow_policy_flags(self):
        rows = [
            _lane_row(
                action="SKIP",
                reason_code="edge_below_threshold",
                approved=False,
                lane_id="edge",
                would_lane_id="confidence_slow_profit",
                differs=True,
                lane_sizing={
                    "lane_id": "edge",
                    "configured": True,
                    "requested_size": 10.0,
                    "beta_adjusted_size": 3.0,
                    "would_adjust_size": True,
                    "differs_from_final": True,
                    "shadow": True,
                },
            )
            | {"hidden_gem_evidence_card": _hidden_gem_card()}
        ]

        readiness = build_strategy_lane_rollout_readiness(
            policy_status={
                "version": "beta",
                "mode": "shadow",
                "active": "false",
                "shadow": "true",
                "enforce": "",
                "enabled_features": {
                    "weather_hidden_gem_evidence_card": "true",
                    "hidden_gem_lane_gates": True,
                    "lane_sizing_caps": True,
                },
            },
            strategy_lane_summary=summarize_strategy_lanes(rows),
            hidden_gem_evidence_summary=summarize_hidden_gem_evidence_cards(rows),
        )

        self.assertEqual(readiness["status"], "blocked")
        self.assertFalse(readiness["ready_for_enforce"])
        self.assertIn("pre_enforce_shadow_policy", self._failed_check_names(readiness))
        self.assertIn("weather_hidden_gem_evidence_card_feature", self._failed_check_names(readiness))

    def test_rollout_checklist_handles_non_finite_counts_without_crashing(self):
        readiness = build_strategy_lane_rollout_readiness(
            policy_status=_shadow_policy_status(
                weather_hidden_gem_evidence_card=True,
                hidden_gem_lane_gates=True,
                lane_sizing_caps=True,
            ),
            strategy_lane_summary={
                "rows_scanned": float("inf"),
                "lane_rows": float("nan"),
                "beta_lane_gate_rows": "bad",
                "lane_selection_delta_rows": None,
                "lane_sizing_rows": float("inf"),
                "lane_sizing_differs_from_final_rows": "",
                "lane_sizing_would_adjust_rows": object(),
            },
            hidden_gem_evidence_summary={
                "rows_scanned": float("inf"),
                "card_rows": float("nan"),
                "insufficient_data_rows": object(),
            },
        )
        line = format_strategy_lane_rollout_readiness(
            {
                "status": "blocked",
                "policy": readiness["policy"],
                "coverage": {
                    "hidden_gem_evidence_cards": {"card_rows": float("inf"), "rows_scanned": float("nan")},
                    "lane_delta": {"beta_lane_gate_rows": object(), "lane_rows": float("inf")},
                    "lane_sizing_delta": {"lane_sizing_rows": float("nan"), "lane_rows": "bad"},
                },
                "blockers": readiness["blockers"],
                "warnings": readiness["warnings"],
            }
        )

        self.assertEqual(readiness["status"], "blocked")
        self.assertIn("cards 0/0", line)
        self.assertIn("lane-delta 0/0", line)
        self.assertIn("sizing-delta 0/0", line)

    def test_rollout_checklist_blocks_when_shadow_evidence_is_missing(self):
        readiness = build_strategy_lane_rollout_readiness(
            policy_status={
                "version": "stable",
                "mode": "off",
                "active": False,
                "shadow": False,
                "enforce": False,
                "enabled_features": {},
            },
            strategy_lane_summary=summarize_strategy_lanes([{"market_id": "legacy"}]),
            hidden_gem_evidence_summary=summarize_hidden_gem_evidence_cards([{"market_id": "legacy"}]),
        )

        self.assertEqual(readiness["status"], "blocked")
        self.assertFalse(readiness["ready_for_enforce"])
        self.assertIn("pre_enforce_shadow_policy", self._failed_check_names(readiness))
        self.assertIn("hidden_gem_evidence_cards_present", self._failed_check_names(readiness))
        self.assertIn("lane_delta_coverage_present", self._failed_check_names(readiness))
        self.assertIn("lane_sizing_delta_coverage_present", self._failed_check_names(readiness))
        self.assertGreater(len(readiness["blockers"]), 0)

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
                                lane_sizing={
                                    "lane_id": "edge",
                                    "configured": True,
                                    "requested_size": 10.0,
                                    "beta_adjusted_size": 3.0,
                                    "would_adjust_size": True,
                                    "preserved_stable_size": True,
                                },
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
        self.assertEqual(summary["lane_sizing_rows"], 1)
        self.assertEqual(summary["lane_sizing_configured_rows"], 1)
        self.assertEqual(summary["lane_sizing_would_adjust_rows"], 1)
        self.assertEqual(summary["lane_sizing_preserved_rows"], 1)
        self.assertEqual(result["strategy_lane_rollout_readiness"]["status"], "blocked")

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
                "strategy_lane_rollout_readiness": build_strategy_lane_rollout_readiness(
                    policy_status=_shadow_policy_status(
                        weather_hidden_gem_evidence_card=True,
                        hidden_gem_lane_gates=True,
                        lane_sizing_caps=True,
                    ),
                    strategy_lane_summary=strategy_lane_summary,
                    hidden_gem_evidence_summary=summarize_hidden_gem_evidence_cards(
                        [
                            _lane_row(
                                action="SKIP",
                                reason_code="edge_below_threshold",
                                approved=False,
                                lane_id="edge",
                                would_lane_id="confidence_slow_profit",
                                differs=True,
                            )
                            | {"hidden_gem_evidence_card": _hidden_gem_card()}
                        ]
                    ),
                ),
                "issues": [],
                "actions": [],
            }
        )

        self.assertIn("Strategy lanes: rows 1/1", report)
        self.assertIn("would-select confidence_slow_profit 1", report)
        self.assertIn("Strategy lane readiness:", report)

    @staticmethod
    def _failed_check_names(readiness: dict) -> set[str]:
        return {check["name"] for check in readiness["checks"] if not check["ok"]}


if __name__ == "__main__":
    unittest.main()
