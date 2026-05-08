import unittest

from bot.prediction_lab_shadow_delta import build_shadow_delta, summarize_shadow_delta_rows


def _beta_shadow_policy(*features: str) -> dict:
    return {
        "version": "beta",
        "mode": "shadow",
        "shadow": True,
        "enabled_features": {name: True for name in features},
    }


def _artifact(*, action: str = "BUY_YES", reason_code: str = "approved", size=10.0, reasoning: dict | None = None) -> dict:
    return {
        "final_action": action,
        "final_reason_code": reason_code,
        "shared_core_decision": {
            "approved": action in {"BUY_YES", "BUY_NO"},
            "reason_code": reason_code,
            "requested_position_size": size,
            "reasoning": dict(reasoning or {}),
        },
    }


def _summary_delta(
    *,
    dedupe_key: str | None = "m1|r1|beta-shadow",
    status: str = "complete",
    changed: bool | None = True,
    action_changed: bool | None = True,
    lane_changed: bool | None = False,
    evidence_sources: list[str] | None = None,
) -> dict:
    return {
        key: value
        for key, value in {
            "schema_version": 1,
            "mode": "beta_shadow_delta",
            "status": status,
            "comparison_complete": status == "complete",
            "action_comparison_available": action_changed is not None,
            "stable": {"action": "SKIP", "selected_lane": "edge"},
            "shadow": {"action": "BUY_YES" if action_changed is not None else None, "selected_lane": "hidden_gem" if lane_changed else "edge"},
            "changed": changed,
            "action_changed": action_changed,
            "side_changed": action_changed,
            "buy_decision_changed": action_changed,
            "reason_changed": action_changed,
            "size_changed": False,
            "lane_changed": lane_changed,
            "dedupe_key": dedupe_key,
            "evidence_sources": evidence_sources or ["beta_lane_gate"],
        }.items()
        if value is not None or key not in {"dedupe_key"}
    }


class PredictionLabShadowDeltaTests(unittest.TestCase):
    def test_omits_without_beta_shadow_policy_or_shadow_evidence(self):
        reasoning = {
            "strategy_policy_status": {"version": "stable", "mode": "off", "shadow": False},
            "lane_sizing": {
                "active": True,
                "shadow": True,
                "enforced": False,
                "beta_adjusted_size": 4.0,
            },
        }
        self.assertIsNone(build_shadow_delta(_artifact(reasoning=reasoning), "KXTEST", "run-1"))
        self.assertIsNone(
            build_shadow_delta(
                _artifact(reasoning={}),
                "KXTEST",
                "run-1",
                fallback_strategy_policy={
                    "version": "beta",
                    "beta": {"mode": "shadow", "features": {"lane_sizing_caps": True}},
                },
            )
        )

    def test_beta_lane_gate_allowed_false_changes_action_to_skip(self):
        reasoning = {
            "strategy_policy_status": _beta_shadow_policy("hidden_gem_lane_gates"),
            "strategy_lane": {
                "lane_id": "hidden_gem",
                "evidence": {
                    "beta_lane_gate": {
                        "active": True,
                        "shadow": True,
                        "enforced": False,
                        "lane_id": "confidence_slow_profit",
                        "allowed": False,
                        "reason_code": "strategy_lane_disabled",
                    }
                },
            },
        }

        delta = build_shadow_delta(_artifact(reasoning=reasoning), "KXTEST", "run-1")

        self.assertIsNotNone(delta)
        assert delta is not None
        self.assertEqual(delta["status"], "complete")
        self.assertTrue(delta["comparison_complete"])
        self.assertTrue(delta["action_comparison_available"])
        self.assertEqual(delta["stable"]["action"], "BUY_YES")
        self.assertEqual(delta["shadow"]["action"], "SKIP")
        self.assertEqual(delta["shadow"]["reason_code"], "strategy_lane_disabled")
        self.assertTrue(delta["action_changed"])
        self.assertTrue(delta["buy_decision_changed"])
        self.assertEqual(delta["evidence_sources"], ["beta_lane_gate"])

    def test_beta_allowed_lane_from_stable_skip_is_partial_not_unchanged(self):
        reasoning = {
            "strategy_policy_status": _beta_shadow_policy("hidden_gem_lane_gates"),
            "strategy_lane": {
                "lane_id": "edge",
                "allowed": True,
                "reason_code": "edge_lane_selected",
                "evidence": {
                    "beta_lane_gate": {
                        "beta_behavior_enabled": True,
                        "beta_behavior_enforced": False,
                        "lane_id": "confidence_slow_profit",
                        "allowed": True,
                        "reason_code": "confidence_slow_profit_lane_selected",
                        "differs_from_final": True,
                    }
                },
            },
        }

        delta = build_shadow_delta(
            _artifact(action="SKIP", reason_code="edge_below_threshold", size=None, reasoning=reasoning),
            "KXTEST",
            "run-1",
        )

        self.assertIsNotNone(delta)
        assert delta is not None
        self.assertEqual(delta["status"], "partial_beta_evidence")
        self.assertFalse(delta["comparison_complete"])
        self.assertFalse(delta["action_comparison_available"])
        self.assertEqual(delta["stable"]["action"], "SKIP")
        self.assertIsNone(delta["shadow"]["action"])
        self.assertEqual(delta["shadow"]["decision_type"], "unknown")
        self.assertEqual(delta["shadow"]["selected_lane"], "confidence_slow_profit")
        self.assertTrue(delta["changed"])
        self.assertTrue(delta["lane_changed"])
        self.assertIsNone(delta["action_changed"])
        self.assertIsNone(delta["buy_decision_changed"])
        self.assertEqual(delta["evidence_sources"], ["beta_lane_gate"])

    def test_weather_beta_rejection_changes_shadow_to_skip(self):
        reasoning = {
            "weather_risk": {
                "beta_gate": {
                    "policy": _beta_shadow_policy("weather_hidden_gem_evidence_card"),
                    "active": True,
                    "shadow": True,
                    "enforced": False,
                    "would_reject": True,
                    "reason_code": "weather_tail_hidden_gem_distribution_probability_below_threshold",
                }
            }
        }

        delta = build_shadow_delta(_artifact(reasoning=reasoning), "KXWEATHER", "run-2")

        self.assertIsNotNone(delta)
        assert delta is not None
        self.assertEqual(delta["shadow"]["action"], "SKIP")
        self.assertEqual(
            delta["shadow"]["reason_code"],
            "weather_tail_hidden_gem_distribution_probability_below_threshold",
        )
        self.assertTrue(delta["action_changed"])
        self.assertTrue(delta["buy_decision_changed"])
        self.assertEqual(delta["evidence_sources"], ["weather_risk.beta_gate"])

    def test_weather_beta_sizing_changes_shadow_size_only(self):
        reasoning = {
            "weather_risk": {
                "beta_sizing_gate": {
                    "policy": _beta_shadow_policy("bucket_distribution_scoring"),
                    "active": True,
                    "shadow": True,
                    "enforced": False,
                    "would_adjust_size": True,
                    "requested_size": 10.0,
                    "beta_adjusted_size": 2.5,
                }
            }
        }

        delta = build_shadow_delta(_artifact(reasoning=reasoning), "KXWEATHER", "run-3")

        self.assertIsNotNone(delta)
        assert delta is not None
        self.assertEqual(delta["stable"]["requested_position_size"], 10.0)
        self.assertEqual(delta["shadow"]["requested_position_size"], 2.5)
        self.assertFalse(delta["action_changed"])
        self.assertTrue(delta["size_changed"])
        self.assertEqual(delta["evidence_sources"], ["weather_risk.beta_sizing_gate"])

    def test_dedupe_key_uses_market_run_and_sources_describe_shadow_evidence(self):
        reasoning = {
            "lane_sizing": {
                "policy": _beta_shadow_policy("lane_sizing_caps"),
                "active": True,
                "shadow": True,
                "enforced": False,
                "beta_adjusted_size": 4.0,
            },
            "weather_risk": {
                "beta_sizing_gate": {
                    "active": True,
                    "shadow": True,
                    "enforced": False,
                    "beta_adjusted_size": 3.0,
                }
            },
        }

        delta = build_shadow_delta(_artifact(reasoning=reasoning), "KXTEST", "run-4")

        self.assertIsNotNone(delta)
        assert delta is not None
        self.assertEqual(delta["dedupe_key"], "KXTEST|run-4|beta-shadow")
        self.assertEqual(delta["evidence_sources"], ["lane_sizing", "weather_risk.beta_sizing_gate"])
        self.assertEqual(delta["shadow"]["requested_position_size"], 3.0)

    def test_summary_dedupes_by_key_and_prefers_recorded_prediction(self):
        summary = summarize_shadow_delta_rows(
            [
                {
                    "run_id": "r1",
                    "market_id": "m1",
                    "recorded_prediction": False,
                    "shadow_delta": _summary_delta(changed=False, action_changed=False),
                },
                {
                    "run_id": "r1",
                    "market_id": "m1",
                    "recorded_prediction": True,
                    "shadow_delta": _summary_delta(changed=True, action_changed=True),
                },
            ],
            prediction_lab_rows=True,
        )

        self.assertEqual(summary["total_shadow_delta_rows"], 2)
        self.assertEqual(summary["total_shadow_delta_opportunities"], 1)
        self.assertEqual(summary["deduped_duplicate_rows"], 1)
        self.assertEqual(summary["changed_rows"], 1)
        self.assertEqual(summary["action_changed"], 1)
        self.assertEqual(summary["action_unchanged"], 0)

    def test_summary_fallback_key_and_decision_artifact_preference_do_not_overcount(self):
        summary = summarize_shadow_delta_rows(
            [
                {
                    "run_id": "r2",
                    "market_id": "m2",
                    "shadow_delta": _summary_delta(dedupe_key=None, changed=False, action_changed=False),
                },
                {
                    "run_id": "r2",
                    "market_id": "m2",
                    "decision_artifact": {"final_action": "BUY_YES"},
                    "shadow_delta": _summary_delta(dedupe_key=None, changed=True, action_changed=True),
                },
            ],
            prediction_lab_rows=True,
        )

        self.assertEqual(summary["total_shadow_delta_rows"], 2)
        self.assertEqual(summary["total_shadow_delta_opportunities"], 1)
        self.assertEqual(summary["changed_rows"], 1)
        self.assertEqual(summary["action_changed"], 1)

    def test_summary_partial_beta_evidence_counts_lane_but_not_unchanged_action(self):
        summary = summarize_shadow_delta_rows(
            [
                {
                    "run_id": "r3",
                    "market_id": "m3",
                    "shadow_delta": _summary_delta(
                        dedupe_key=None,
                        status="partial_beta_evidence",
                        changed=True,
                        action_changed=None,
                        lane_changed=True,
                    ),
                }
            ],
            prediction_lab_rows=True,
        )

        self.assertEqual(summary["total_shadow_delta_opportunities"], 1)
        self.assertEqual(summary["status_counts"], {"partial_beta_evidence": 1})
        self.assertEqual(summary["unavailable_action_comparisons"], 1)
        self.assertEqual(summary["lane_changed"], 1)
        self.assertEqual(summary["action_changed"], 0)
        self.assertEqual(summary["action_unchanged"], 0)


if __name__ == "__main__":
    unittest.main()
