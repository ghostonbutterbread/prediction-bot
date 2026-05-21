import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import morning_bot_status_report as morning
from scripts import prediction_lab_monitor as monitor


class MorningBotStatusReportTests(unittest.TestCase):
    def test_script_process_match_avoids_shell_false_positive(self):
        cmdlines = [
            (11, ["python3", "paper_loop.py"]),
            (12, ["/usr/bin/python3", "scripts/prediction_lab_collect.py", "--config", "config.yaml"]),
            (13, ["/bin/bash", "-c", "pgrep -af 'python3 paper_loop.py'"]),
            (14, ["python3", "main.py", "live", "--config", "config.live.yaml"]),
        ]

        paper = morning.find_script_processes("paper_loop.py", cmdlines)
        collector = morning.find_script_processes("prediction_lab_collect.py", cmdlines)
        live = morning.find_main_command_processes("live", cmdlines)

        self.assertEqual([process["pid"] for process in paper], [11])
        self.assertEqual([process["pid"] for process in collector], [12])
        self.assertEqual([process["pid"] for process in live], [14])


    def test_latest_paper_report_uses_read_only_analysis(self):
        analysis = {"summary": {"total_sessions": 1}}
        with patch.object(morning.paper_analyze, "analyze", return_value=analysis) as analyze:
            with patch.object(morning.paper_analyze, "format_report", return_value="report"):
                report = morning.latest_paper_report()

        self.assertEqual(report, "report")
        analyze.assert_called_once_with(prune_logs=False)

    def test_build_report_includes_only_active_paper_section_and_inactive_others(self):
        now = datetime(2026, 4, 30, 15, 0, tzinfo=timezone.utc)
        report = "📊 **Bot Report** — 2026-04-30T08:00\nSession: abc | Scans: 7"

        with patch.object(morning, "latest_paper_report", return_value=report):
            text = morning.build_report(
                cmdlines=[(11, ["python3", "paper_loop.py"])],
                now=now,
            )

        self.assertIn("Active modes: Paper Trading", text)
        self.assertIn("Inactive modes: Prediction Lab Collector, Live Trading", text)
        self.assertIn("**Paper Trading**", text)
        self.assertIn("PID(s): 11", text)
        self.assertIn(report, text)
        self.assertNotIn("**Prediction Lab Collector**", text)
        self.assertNotIn("**Live Trading**", text)


    def test_build_report_includes_active_paper_shadow_lanes(self):
        now = datetime(2026, 4, 30, 15, 0, tzinfo=timezone.utc)
        lane_status = {
            "enabled": True,
            "lane_ids": ["control_stable", "shadow_confidence_floor", "shadow_source_scoreboard"],
            "decision_path": "data/beta_shadow/paper/source_scoreboard/paper_shadow_lane_decisions.jsonl",
            "lane_row_counts": {
                "control_stable": 10,
                "shadow_confidence_floor": 10,
                "shadow_source_scoreboard": 10,
            },
            "source_scoreboard_readiness": {
                "evaluated_rows": 10,
                "independent_label_rows": 2,
                "order_book_quote_rows": 8,
                "execution_snapshot_rows": 3,
                "estimated_fill_price_rows": 3,
            },
        }

        with patch.object(morning, "latest_paper_report", return_value="paper report"):
            with patch.object(morning, "_paper_shadow_lane_status", return_value=lane_status):
                text = morning.build_report(
                    cmdlines=[(11, ["python3", "paper_loop.py", "--config", "config.paper.yaml"])],
                    now=now,
                )

        self.assertIn("Paper shadow lanes: control_stable, shadow_confidence_floor, shadow_source_scoreboard", text)
        self.assertIn("Lane rows: control_stable=10, shadow_confidence_floor=10, shadow_source_scoreboard=10", text)
        self.assertIn("Lane ledger: data/beta_shadow/paper/source_scoreboard/paper_shadow_lane_decisions.jsonl", text)
        self.assertIn("Source-scoreboard readiness: rows=10, independent_labels=2, order_book=8, execution=3, estimated_fill=3", text)

    def test_build_report_includes_paper_shadow_lanes_from_second_paper_config(self):
        now = datetime(2026, 4, 30, 15, 0, tzinfo=timezone.utc)
        lane_status = {
            "enabled": True,
            "lane_ids": ["shadow_limited"],
            "decision_path": "data/beta_shadow/paper/limited/paper_shadow_lane_decisions.jsonl",
            "lane_row_counts": {"shadow_limited": 3},
        }

        def lane_status_for_config(config_path):
            if config_path == Path("data/runtime_configs/paper_limited_shadow_20260516.yaml"):
                return lane_status
            return {"enabled": False, "lane_ids": [], "decision_path": None, "lane_row_counts": {}}

        with patch.object(morning, "latest_paper_report", return_value="paper report"):
            with patch.object(morning, "_paper_shadow_lane_status", side_effect=lane_status_for_config) as shadow_status:
                text = morning.build_report(
                    cmdlines=[
                        (11, ["python3", "paper_loop.py", "--config", "config.paper.yaml"]),
                        (
                            12,
                            [
                                "python3",
                                "paper_loop.py",
                                "--config",
                                "data/runtime_configs/paper_limited_shadow_20260516.yaml",
                            ],
                        ),
                    ],
                    now=now,
                )

        self.assertEqual(
            [call.args[0] for call in shadow_status.call_args_list],
            [Path("config.paper.yaml"), Path("data/runtime_configs/paper_limited_shadow_20260516.yaml")],
        )
        self.assertIn("PID(s): 11, 12", text)
        self.assertIn("Paper shadow lanes: shadow_limited", text)
        self.assertIn("Lane rows: shadow_limited=3", text)

    def test_build_json_includes_paper_shadow_lanes(self):
        lane_status = {"enabled": True, "lane_ids": ["control_stable"], "decision_path": "lanes.jsonl", "lane_row_counts": {"control_stable": 1}}
        with patch.object(morning, "_paper_shadow_lane_status", return_value=lane_status):
            payload = morning.build_json(cmdlines=[(11, ["python3", "paper_loop.py", "--config", "config.paper.yaml"])])

        self.assertEqual(payload["paper_shadow_lanes"], lane_status)

    def test_build_report_includes_active_collector_health_and_inactive_others(self):
        now = datetime(2026, 4, 30, 15, 0, tzinfo=timezone.utc)
        result = monitor.MonitorResult(
            healthy=False,
            issues=[monitor.MonitorIssue("stale_collect", "last collect is stale", severity="critical")],
            details={
                "collector_processes": [{"pid": 22, "cmdline": ["python3", "scripts/prediction_lab_collect.py"]}],
                "last_collect_age_seconds": 1900,
                "latest_log": "data/paper/prediction_lab/logs/collector_test.log",
            },
        )

        with patch.object(morning.lab_monitor, "evaluate_health", return_value=result):
            with patch.object(morning, "latest_paper_report") as paper_report:
                text = morning.build_report(
                    cmdlines=[(22, ["python3", "scripts/prediction_lab_collect.py"])],
                    prediction_lab_config=Path("config.test.yaml"),
                    now=now,
                )

        paper_report.assert_not_called()
        self.assertIn("Active modes: Prediction Lab Collector", text)
        self.assertIn("Inactive modes: Paper Trading, Live Trading", text)
        self.assertIn("**Prediction Lab Collector**", text)
        self.assertIn("Status: unhealthy", text)
        self.assertIn("PID(s): 22", text)
        self.assertIn("Last collect age: 1900s", text)
        self.assertIn("stale_collect: last collect is stale", text)
        self.assertNotIn("**Paper Trading**", text)
        self.assertNotIn("**Live Trading**", text)


    def test_collector_config_mismatch_does_not_overwrite_health_processes(self):
        now = datetime(2026, 4, 30, 15, 0, tzinfo=timezone.utc)
        result = monitor.MonitorResult(
            healthy=False,
            issues=[monitor.MonitorIssue("collector_not_running", "collector process is not running")],
            details={"collector_processes": []},
        )

        with patch.object(morning.lab_monitor, "evaluate_health", return_value=result):
            text = morning.build_report(
                cmdlines=[(22, ["python3", "scripts/prediction_lab_collect.py", "--config", "config.a.yaml"])],
                prediction_lab_config=Path("config.b.yaml"),
                now=now,
            )

        self.assertIn("**Prediction Lab Collector**", text)
        self.assertIn("Process: active", text)
        self.assertNotIn("PID(s): 22", text)
        self.assertIn("collector_not_running: collector process is not running", text)

    def test_build_report_includes_only_active_live_section_and_inactive_others(self):
        now = datetime(2026, 4, 30, 15, 0, tzinfo=timezone.utc)

        with patch.object(morning, "latest_paper_report") as paper_report:
            text = morning.build_report(
                cmdlines=[(33, ["python3", "main.py", "live", "--config", "config.live.yaml"])],
                now=now,
            )

        paper_report.assert_not_called()
        self.assertIn("Active modes: Live Trading", text)
        self.assertIn("Inactive modes: Paper Trading, Prediction Lab Collector", text)
        self.assertIn("**Live Trading**", text)
        self.assertIn("PID(s): 33", text)
        self.assertIn("real-money live runner detected", text)
        self.assertNotIn("**Paper Trading**", text)
        self.assertNotIn("**Prediction Lab Collector**", text)

    def test_build_report_includes_all_three_active_sections(self):
        now = datetime(2026, 4, 30, 15, 0, tzinfo=timezone.utc)
        paper_report = "📊 **Bot Report** — 2026-04-30T08:00\nSession: abc | Scans: 7"
        lab_result = monitor.MonitorResult(
            healthy=True,
            details={
                "collector_processes": [{"pid": 22, "cmdline": ["python3", "scripts/prediction_lab_collect.py"]}],
                "last_collect_age_seconds": 120,
            },
        )

        with patch.object(morning, "latest_paper_report", return_value=paper_report):
            with patch.object(morning.lab_monitor, "evaluate_health", return_value=lab_result):
                text = morning.build_report(
                    cmdlines=[
                        (11, ["python3", "paper_loop.py"]),
                        (22, ["python3", "scripts/prediction_lab_collect.py"]),
                        (33, ["python3", "main.py", "live"]),
                    ],
                    now=now,
                )

        self.assertIn("Active modes: Paper Trading, Prediction Lab Collector, Live Trading", text)
        self.assertIn("Inactive modes: none", text)
        self.assertIn("**Paper Trading**", text)
        self.assertIn("PID(s): 11", text)
        self.assertIn(paper_report, text)
        self.assertIn("**Prediction Lab Collector**", text)
        self.assertIn("PID(s): 22", text)
        self.assertIn("**Live Trading**", text)
        self.assertIn("PID(s): 33", text)

    def test_build_report_when_none_active_includes_latest_paper_analysis(self):
        now = datetime(2026, 4, 30, 15, 0, tzinfo=timezone.utc)
        report = "📊 **Bot Report** — 2026-04-30T08:00\nSession: abc | Scans: 7"

        with patch.object(morning, "latest_paper_report", return_value=report):
            text = morning.build_report(cmdlines=[], now=now)

        self.assertIn("Active modes: none", text)
        self.assertIn("Inactive modes: Paper Trading, Prediction Lab Collector, Live Trading", text)
        self.assertIn("**Latest Paper Analysis**", text)
        self.assertIn(report, text)
        self.assertNotIn("**Paper Trading**\nProcess: active", text)
        self.assertNotIn("**Prediction Lab Collector**", text)
        self.assertNotIn("**Live Trading**", text)


if __name__ == "__main__":
    unittest.main()
