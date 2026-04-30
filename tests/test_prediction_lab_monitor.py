import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import prediction_lab_monitor as monitor


class PredictionLabMonitorTests(unittest.TestCase):
    def _write_config(self, root: Path, *, interval: int = 900, observer: bool = True):
        config_path = root / "config.yaml"
        config_path.write_text(
            "\n".join(
                [
                    f"data_dir: {root / 'data'}",
                    "prediction_lab:",
                    "  enabled: true",
                    "  mode: collector",
                    f"  observer_mode: {'true' if observer else 'false'}",
                    "  max_markets_per_run: 1000",
                    "  continue_collecting: true",
                    f"  collector_interval_seconds: {interval}",
                    "  collection_storage_cap_gb: 25",
                    "  groups: [weather]",
                    "trading:",
                    "  enabled: false",
                    "strategy:",
                    "  enable_news: false",
                    "  enable_social: false",
                    "  enable_ai: false",
                ]
            )
        )
        return config_path

    def _write_state(self, lab_dir: Path, *, now: datetime):
        lab_dir.mkdir(parents=True, exist_ok=True)
        (lab_dir / "state.json").write_text(
            json.dumps(
                {
                    "mode": "collector",
                    "run_state": "idle_watch",
                    "paused": False,
                    "pause_reason": "none",
                    "last_collect_at": (now - timedelta(minutes=5)).isoformat(),
                    "last_error": None,
                    "storage_usage_gb": 0.1,
                    "observer_mode": True,
                    "trading_enabled": False,
                    "order_execution_enabled": False,
                }
            )
        )
        log_dir = lab_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log = log_dir / "collector_test.log"
        log.write_text("ok\n")
        ts = now.timestamp()
        os.utime(log, (ts, ts))

    def test_collector_process_match_is_exact_and_avoids_shell_false_positive(self):
        self.assertTrue(
            monitor.collector_matches(
                [
                    "python3",
                    "scripts/prediction_lab_collect.py",
                    "--config",
                    "config.prediction_lab_weather_overnight.yaml",
                    "--observer",
                ]
            )
        )
        self.assertTrue(
            monitor.collector_matches(
                [
                    "/usr/bin/python3",
                    "/home/ryushe/projects/prediction-bot/scripts/prediction_lab_collect.py",
                    "--observer",
                    "--config",
                    "/home/ryushe/projects/prediction-bot/config.prediction_lab_weather_overnight.yaml",
                ]
            )
        )
        self.assertFalse(
            monitor.collector_matches(
                [
                    "/bin/bash",
                    "-c",
                    "pgrep -af 'python3 scripts/prediction_lab_collect.py --config config.prediction_lab_weather_overnight.yaml'",
                ]
            )
        )
        self.assertTrue(
            monitor.collector_matches(
                [
                    "python3",
                    "scripts/prediction_lab_collect.py",
                    "--observer",
                ],
                expected=["python3", "scripts/prediction_lab_collect.py", "--config", "config.yaml"],
            )
        )
        self.assertFalse(
            monitor.collector_matches(
                [
                    "python3",
                    "scripts/prediction_lab_collect.py",
                    "--observer",
                ],
                expected=[
                    "python3",
                    "scripts/prediction_lab_collect.py",
                    "--config",
                    "config.prediction_lab_weather_overnight.yaml",
                ],
            )
        )

    def test_evaluate_health_healthy_when_collector_recent_and_safe(self):
        now = datetime(2026, 4, 28, 18, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._write_config(root)
            lab_dir = root / "data" / "paper" / "prediction_lab"
            self._write_state(lab_dir, now=now)

            result = monitor.evaluate_health(
                config_path,
                now=now,
                cmdlines=[
                    (
                        123,
                        [
                            "python3",
                            "scripts/prediction_lab_collect.py",
                            "--config",
                            str(config_path),
                            "--observer",
                        ],
                    )
                ],
            )

            self.assertTrue(result.healthy, result.summary())
            self.assertEqual(result.details["collector_processes"][0]["pid"], 123)

    def test_evaluate_health_fails_when_process_missing(self):
        now = datetime(2026, 4, 28, 18, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._write_config(root)
            lab_dir = root / "data" / "paper" / "prediction_lab"
            self._write_state(lab_dir, now=now)

            result = monitor.evaluate_health(config_path, now=now, cmdlines=[])

            self.assertFalse(result.healthy)
            self.assertIn("collector_not_running", [issue.code for issue in result.issues])

    def test_evaluate_health_uses_nested_prediction_lab_config(self):
        now = datetime(2026, 4, 28, 18, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._write_config(root, observer=False)
            lab_dir = root / "data" / "paper" / "prediction_lab"
            self._write_state(lab_dir, now=now)

            result = monitor.evaluate_health(
                config_path,
                now=now,
                cmdlines=[
                    (
                        123,
                        [
                            "python3",
                            "scripts/prediction_lab_collect.py",
                            "--config",
                            str(config_path),
                        ],
                    )
                ],
            )

            self.assertTrue(any(issue.code == "config_drift" and "prediction_lab.observer_mode" in issue.message for issue in result.issues))

    def test_notify_only_on_state_change(self):
        healthy = monitor.MonitorResult(healthy=True)
        broken = monitor.MonitorResult(healthy=False, issues=[monitor.MonitorIssue("collector_not_running", "missing")])

        self.assertTrue(monitor.should_notify(broken, {}))
        self.assertFalse(monitor.should_notify(broken, {"last_state_key": monitor.state_key(broken)}))
        self.assertTrue(monitor.should_notify(healthy, {"last_state_key": monitor.state_key(broken)}))

    def test_main_can_trigger_repair_job_on_unhealthy_state_change(self):
        now = datetime(2026, 4, 28, 18, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._write_config(root)
            lab_dir = root / "data" / "paper" / "prediction_lab"
            self._write_state(lab_dir, now=now)
            monitor_state = root / "monitor_state.json"
            calls = []

            with patch.object(monitor, "utc_now", return_value=now):
                with patch.object(monitor, "read_proc_cmdlines", return_value=[]):
                    with patch.object(monitor, "trigger_repair_cron", side_effect=lambda job_id: calls.append(job_id) or True):
                        code = monitor.main(
                            [
                                "--config",
                                str(config_path),
                                "--state-file",
                                str(monitor_state),
                                "--repair-cron-job-id",
                                "repair-job",
                            ]
                        )

            self.assertEqual(code, 2)
            self.assertEqual(calls, ["repair-job"])


if __name__ == "__main__":
    unittest.main()
