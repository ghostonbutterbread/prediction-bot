import unittest
from unittest.mock import patch

from bot.strategy_policy import normalize_strategy_policy
from scripts import analyze as paper_analyze


class AnalyzeStrategyPolicyStatusTests(unittest.TestCase):
    def test_analyze_result_includes_strategy_policy_status(self):
        policy = normalize_strategy_policy(
            {
                "version": "beta",
                "beta": {
                    "mode": "shadow",
                    "features": {"bucket_distribution_scoring": True},
                },
            }
        )
        with patch.object(paper_analyze, "load_sessions", return_value=[]), patch.object(
            paper_analyze, "load_config", return_value={"strategy_policy_normalized": policy}
        ), patch.object(paper_analyze, "summarize_log_storage", return_value=None):
            result = paper_analyze.analyze(prune_logs=False)

        status = result["strategy_policy_status"]
        self.assertEqual(status["version"], "beta")
        self.assertEqual(status["mode"], "shadow")
        self.assertTrue(status["active"])
        self.assertTrue(status["shadow"])
        self.assertFalse(status["enforce"])
        self.assertTrue(status["enabled_features"]["bucket_distribution_scoring"])

    def test_analyze_prefers_latest_session_policy_status(self):
        latest_status = {
            "version": "beta",
            "mode": "enforce",
            "active": True,
            "shadow": False,
            "enforce": True,
            "enabled_features": {
                "weather_hidden_gem_evidence_card": False,
                "bucket_distribution_scoring": False,
                "hidden_gem_lane_gates": True,
            },
        }
        session = {
            "session_id": "s1",
            "trades": [],
            "summary": {"strategy_policy_status": latest_status},
        }
        fallback = normalize_strategy_policy({"version": "stable"})
        with patch.object(paper_analyze, "load_sessions", return_value=[session]), patch.object(
            paper_analyze, "load_config", return_value={"strategy_policy_normalized": fallback}
        ), patch.object(paper_analyze, "summarize_log_storage", return_value=None):
            result = paper_analyze.analyze(prune_logs=False)

        self.assertEqual(result["strategy_policy_status"]["version"], "beta")
        self.assertEqual(result["strategy_policy_status"]["mode"], "enforce")
        self.assertTrue(result["strategy_policy_status"]["enabled_features"]["hidden_gem_lane_gates"])

    def test_analyze_checks_older_trade_artifacts_for_policy_status(self):
        latest_status = {
            "version": "beta",
            "mode": "shadow",
            "active": True,
            "shadow": True,
            "enforce": False,
            "enabled_features": {
                "weather_hidden_gem_evidence_card": False,
                "bucket_distribution_scoring": False,
                "hidden_gem_lane_gates": True,
            },
        }
        session = {
            "session_id": "s1",
            "summary": {},
            "trades": [
                {
                    "decision_artifact": {
                        "shared_core_decision": {
                            "reasoning": {"strategy_policy_status": latest_status}
                        }
                    }
                },
                {"market_id": "newer-legacy-trade-without-artifact"},
            ],
        }
        fallback = normalize_strategy_policy({"version": "stable"})
        with patch.object(paper_analyze, "load_sessions", return_value=[session]), patch.object(
            paper_analyze, "load_config", return_value={"strategy_policy_normalized": fallback}
        ), patch.object(paper_analyze, "summarize_log_storage", return_value=None):
            result = paper_analyze.analyze(prune_logs=False)

        self.assertEqual(result["strategy_policy_status"]["version"], "beta")
        self.assertEqual(result["strategy_policy_status"]["mode"], "shadow")
        self.assertTrue(result["strategy_policy_status"]["enabled_features"]["hidden_gem_lane_gates"])

    def test_report_includes_concise_strategy_policy_line(self):
        report = paper_analyze.format_report(
            {
                "timestamp": "2026-05-06T08:00:00-07:00",
                "summary": {
                    "current_session": "s1",
                    "scans": 0,
                    "current_trades": 0,
                    "resolved": 0,
                    "trusted_resolved_positions": 0,
                    "resolved_events": 0,
                    "current_session_file": "data/paper/sim.json",
                },
                "strategy_policy_status": {
                    "version": "stable",
                    "mode": "off",
                    "active": False,
                    "shadow": False,
                    "enforce": False,
                    "enabled_features": {
                        "weather_hidden_gem_evidence_card": False,
                        "bucket_distribution_scoring": False,
                        "hidden_gem_lane_gates": False,
                    },
                },
                "performance": {},
                "event_performance": {},
                "signal_quality": {},
                "issues": [],
                "actions": [],
            }
        )

        self.assertIn("Strategy policy: stable/off", report)
        self.assertIn("features=none", report)


if __name__ == "__main__":
    unittest.main()
