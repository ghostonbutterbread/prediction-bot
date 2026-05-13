import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.strategy_policy import normalize_strategy_policy
from scripts import analyze as paper_analyze


class AnalyzeStrategyPolicyStatusTests(unittest.TestCase):
    def test_load_sessions_can_isolate_env_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            default_root = root / "normal"
            shadow_root = root / "shadow"
            (default_root / "paper").mkdir(parents=True)
            (shadow_root / "paper").mkdir(parents=True)
            (default_root / "paper" / "sim_normal.json").write_text(json.dumps({"session_id": "normal", "trades": []}))
            (shadow_root / "paper" / "sim_shadow.json").write_text(json.dumps({"session_id": "shadow", "trades": []}))

            with patch.object(paper_analyze, "DATA_DIR", default_root), patch.dict(
                "os.environ",
                {"ANALYZE_DATA_DIR": str(shadow_root), "ANALYZE_DATA_DIR_ONLY": "true"},
                clear=False,
            ):
                sessions = paper_analyze.load_sessions()

        self.assertEqual([session["session_id"] for session in sessions], ["shadow"])

    def test_load_sessions_keeps_legacy_env_plus_default_behavior(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            default_root = root / "normal"
            shadow_root = root / "shadow"
            (default_root / "paper").mkdir(parents=True)
            (shadow_root / "paper").mkdir(parents=True)
            (default_root / "paper" / "sim_normal.json").write_text(json.dumps({"session_id": "normal", "trades": []}))
            (shadow_root / "paper" / "sim_shadow.json").write_text(json.dumps({"session_id": "shadow", "trades": []}))

            with patch.object(paper_analyze, "DATA_DIR", default_root), patch.dict(
                "os.environ",
                {"ANALYZE_DATA_DIR": str(shadow_root)},
                clear=False,
            ):
                sessions = paper_analyze.load_sessions()

        self.assertEqual([session["session_id"] for session in sessions], ["normal", "shadow"])

    def test_analyze_uses_analyze_config_env_path(self):
        policy = normalize_strategy_policy({"version": "stable"})
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "shadow.yaml"
            with patch.object(paper_analyze, "load_sessions", return_value=[]), patch.object(
                paper_analyze, "load_config", return_value={"strategy_policy_normalized": policy}
            ) as load_config, patch.object(paper_analyze, "summarize_log_storage", return_value=None), patch.dict(
                "os.environ", {"ANALYZE_CONFIG": str(config_path)}, clear=False
            ):
                paper_analyze.analyze(prune_logs=False)

        load_config.assert_called_once_with(config_path)

    def test_analyze_data_dir_only_without_analyze_config_disables_storage_audit(self):
        policy = normalize_strategy_policy({"version": "stable"})
        with patch.object(paper_analyze, "load_sessions", return_value=[]), patch.object(
            paper_analyze, "load_config", return_value={"strategy_policy_normalized": policy, "storage": {"logs": {"enabled": True}}}
        ), patch.object(paper_analyze, "summarize_log_storage", return_value=None) as summarize_storage, patch.object(
            paper_analyze, "prune_log_storage", return_value=None
        ) as prune_storage, patch.dict(
            "os.environ", {"ANALYZE_DATA_DIR": "data/beta_shadow", "ANALYZE_DATA_DIR_ONLY": "true"}, clear=False
        ):
            paper_analyze.analyze(prune_logs=True)

        summarize_cfg = summarize_storage.call_args.args[0]
        prune_cfg = prune_storage.call_args.args[0]
        self.assertFalse(summarize_cfg["storage"]["logs"]["enabled"])
        self.assertFalse(prune_cfg["storage"]["logs"]["enabled"])

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

    def test_report_formats_malformed_strategy_policy_flags_fail_closed(self):
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
                    "version": "beta",
                    "mode": "shadow",
                    "active": "false",
                    "shadow": "true",
                    "enforce": "",
                    "enabled_features": {
                        "weather_hidden_gem_evidence_card": "true",
                        "hidden_gem_lane_gates": True,
                    },
                },
                "performance": {},
                "event_performance": {},
                "signal_quality": {},
                "issues": [],
                "actions": [],
            }
        )

        self.assertIn("Strategy policy: beta/shadow", report)
        self.assertIn("active=False shadow=False enforce=False", report)
        self.assertIn("features=hidden_gem_lane_gates", report)
        self.assertNotIn("weather_hidden_gem_evidence_card", report)

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

    def test_report_uses_telegram_readable_sections_instead_of_pipe_rows(self):
        report = paper_analyze.format_report(
            {
                "timestamp": "2026-05-09T08:00:00-07:00",
                "summary": {
                    "current_session": "s1",
                    "scans": 4,
                    "current_trades": 2,
                    "resolved": 1,
                    "trusted_resolved_positions": 1,
                    "resolved_events": 1,
                    "current_session_file": "data/paper/sim.json",
                    "ignored_invalid_trades": 1,
                    "invalid_resolved_positions": 1,
                    "total_equity": 1005.0,
                    "available_cash": 990.0,
                    "reserved_capital": 15.0,
                    "open_event_count": 1,
                },
                "strategy_policy_status": {
                    "version": "beta",
                    "mode": "shadow",
                    "active": True,
                    "shadow": True,
                    "enforce": False,
                    "enabled_features": {"hidden_gem_lane_gates": True},
                },
                "performance": {"win_rate": 100.0, "total_pnl": 5.0, "profit_factor": 0},
                "event_performance": {
                    "win_rate": 100.0,
                    "resolved_events": 1,
                    "avg_pnl_per_event": 5.0,
                    "avg_positions_per_resolved_event": 1,
                    "retrade_count": 0,
                },
                "signal_quality": {"avg_edge": 2.5, "max_edge": 3.0, "avg_confidence": 70.0},
                "shadow_delta": {
                    "total_shadow_delta_opportunities": 1,
                    "total_shadow_delta_rows": 1,
                    "action_changed": 1,
                },
                "issues": [
                    {
                        "severity": "error",
                        "code": "UNTRUSTED_RESOLVED_ROWS",
                        "message": "1 resolved rows failed accounting integrity checks",
                    }
                ],
                "actions": [
                    {
                        "priority": 1,
                        "action": "Re-run resolution or clean malformed resolved rows before using paper P&L",
                        "file": "bot/resolver.py",
                    }
                ],
            }
        )

        self.assertIn("📋 **Paper / Shadow**", report)
        self.assertIn("📌 **Snapshot**", report)
        self.assertIn("Strategy policy: beta/shadow", report)
        self.assertIn("🔎 **Detail**", report)
        self.assertIn("`Metric        Paper                 Shadow`", report)
        self.assertIn("`Trades           ▶ 2", report)
        self.assertIn("Shadow delta: 1 opportunities", report)
        self.assertIn("• Resolved: 1 raw / 1 trusted / 1 events", report)
        self.assertIn("• 🟠 [UNTRUSTED_RESOLVED_ROWS]", report)
        self.assertNotIn(" | ", report)

    def test_report_omits_shadow_column_when_shadow_is_not_running(self):
        report = paper_analyze.format_report(
            {
                "timestamp": "2026-05-09T08:00:00-07:00",
                "summary": {
                    "current_session": "s1",
                    "scans": 2,
                    "current_trades": 3,
                    "resolved": 1,
                    "trusted_resolved_positions": 1,
                    "resolved_events": 1,
                },
                "strategy_policy_status": {
                    "version": "stable",
                    "mode": "off",
                    "active": False,
                    "shadow": False,
                    "enforce": False,
                    "enabled_features": {},
                },
                "performance": {"win_rate": 100.0, "total_pnl": 4.0, "profit_factor": 0},
                "event_performance": {},
                "signal_quality": {},
                "issues": [],
                "actions": [],
            }
        )

        self.assertIn("• Trades: 3", report)
        self.assertIn("• Open positions: 2", report)
        self.assertIn("• Closed positions: 1 trusted / 1 raw", report)
        self.assertIn("• PnL: $+4.00", report)
        self.assertNotIn("Paper                    Shadow", report)
        self.assertNotIn("Marker: ▶", report)

    def test_report_marks_shadow_when_shadow_only_evaluation_is_logged(self):
        report = paper_analyze.format_report(
            {
                "timestamp": "2026-05-09T08:00:00-07:00",
                "summary": {
                    "current_session": "shadow-only",
                    "scans": 1,
                    "current_trades": 0,
                    "resolved": 0,
                    "trusted_resolved_positions": 0,
                    "resolved_events": 0,
                },
                "strategy_policy_status": {
                    "version": "beta",
                    "mode": "shadow",
                    "active": True,
                    "shadow": True,
                    "enforce": False,
                    "enabled_features": {"hidden_gem_lane_gates": True},
                },
                "performance": {},
                "event_performance": {},
                "signal_quality": {},
                "shadow_delta": {
                    "total_shadow_delta_opportunities": 3,
                    "total_shadow_delta_rows": 4,
                    "changed_rows": 2,
                    "action_changed": 1,
                    "size_changed": 1,
                    "lane_changed": 2,
                },
                "issues": [],
                "actions": [],
            }
        )

        self.assertIn("`Trades           0                        ▶ 3 eval`", report)
        self.assertIn("`Trading          paper sim                ▶ beta/shadow`", report)
        self.assertIn("• ▶ = actual execution/logging path", report)

    def test_analyze_reports_shadow_delta_without_increasing_trade_counts(self):
        shadow_delta = {
            "schema_version": 1,
            "mode": "beta_shadow_delta",
            "status": "complete",
            "comparison_complete": True,
            "action_comparison_available": True,
            "stable": {"action": "SKIP", "selected_lane": "edge"},
            "shadow": {"action": "BUY_YES", "selected_lane": "hidden_gem"},
            "changed": True,
            "action_changed": True,
            "side_changed": True,
            "buy_decision_changed": True,
            "reason_changed": True,
            "size_changed": False,
            "lane_changed": True,
            "dedupe_key": "m1|run-1|beta-shadow",
            "evidence_sources": ["beta_lane_gate"],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "paper").mkdir(parents=True)
            (root / "paper" / "sim_shadow.json").write_text(
                json.dumps({"session_id": "shadow", "scan_count": 1, "trades": []})
            )
            lab_dir = root / "paper" / "prediction_lab"
            lab_dir.mkdir(parents=True)
            (lab_dir / "predictions.jsonl").write_text(
                json.dumps(
                    {
                        "run_id": "run-1",
                        "market_id": "m1",
                        "recorded_prediction": True,
                        "shadow_delta": shadow_delta,
                    }
                )
                + "\n"
            )
            (lab_dir / "market_snapshots.jsonl").write_text(
                json.dumps(
                    {
                        "run_id": "run-1",
                        "market_id": "m1",
                        "recorded_prediction": False,
                        "shadow_delta": {**shadow_delta, "changed": False, "action_changed": False},
                    }
                )
                + "\n"
            )

            with patch.dict(
                "os.environ",
                {"ANALYZE_DATA_DIR": str(root), "ANALYZE_DATA_DIR_ONLY": "true"},
                clear=False,
            ), patch.object(
                paper_analyze,
                "load_config",
                return_value={
                    "strategy_policy_normalized": normalize_strategy_policy(
                        {"version": "beta", "beta": {"mode": "shadow"}}
                    )
                },
            ), patch.object(
                paper_analyze,
                "summarize_log_storage",
                return_value=None,
            ):
                result = paper_analyze.analyze(prune_logs=False)

        self.assertEqual(result["summary"]["current_trades"], 0)
        self.assertEqual(result["summary"]["total_trades_ever"], 0)
        summary = result["shadow_delta"]
        self.assertEqual(summary["total_shadow_delta_rows"], 2)
        self.assertEqual(summary["total_shadow_delta_opportunities"], 1)
        self.assertEqual(summary["action_changed"], 1)
        self.assertIn("Shadow delta: 1 opportunities", paper_analyze.format_report(result))


if __name__ == "__main__":
    unittest.main()
