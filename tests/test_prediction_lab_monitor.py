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
                    "  collection_storage_cap_gb: 100",
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

    def _write_state(
        self,
        lab_dir: Path,
        *,
        now: datetime,
        last_collect_delta: timedelta = timedelta(minutes=5),
        last_storage_check_delta: timedelta | None = None,
        write_collector_log: bool = True,
        write_market_snapshots: bool = True,
    ):
        lab_dir.mkdir(parents=True, exist_ok=True)
        state = {
            "mode": "collector",
            "run_state": "idle_watch",
            "paused": False,
            "pause_reason": "none",
            "last_collect_at": (now - last_collect_delta).isoformat(),
            "last_error": None,
            "storage_usage_gb": 0.1,
            "observer_mode": True,
            "trading_enabled": False,
            "order_execution_enabled": False,
        }
        if last_storage_check_delta is not None:
            state["last_storage_check_at"] = (now - last_storage_check_delta).isoformat()
        (lab_dir / "state.json").write_text(
            json.dumps(state)
        )
        ts = now.timestamp()
        if write_collector_log:
            log_dir = lab_dir / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log = log_dir / "collector_test.log"
            log.write_text("ok\n")
            os.utime(log, (ts, ts))
        if write_market_snapshots:
            snapshots = lab_dir / "market_snapshots.jsonl"
            snapshots.write_text(json.dumps({"market_id": "KXTEST", "observed_at": now.isoformat()}) + "\n")
            os.utime(snapshots, (ts, ts))

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


    def test_monitor_cron_beta_shadow_selection_requires_explicit_profile(self):
        script = (Path(__file__).resolve().parents[1] / "scripts" / "prediction_lab_monitor_cron.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn('MONITOR_PROFILE="${PREDICTION_LAB_MONITOR_PROFILE:-stable}"', script)
        self.assertIn('beta_shadow)', script)
        self.assertIn('Beta-shadow monitoring is opt-in', script)
        self.assertNotIn('|| [[ -f "$REPO/data/beta_shadow/paper/prediction_lab/state.json" ]]', script)
        self.assertNotIn('|| [[ -f "$REPO/data/beta_shadow/paper/prediction_lab/collector.pid" ]]', script)

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

    def test_evaluate_health_accepts_recent_state_heartbeat_when_file_log_is_stale(self):
        now = datetime(2026, 4, 28, 18, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._write_config(root)
            lab_dir = root / "data" / "paper" / "prediction_lab"
            self._write_state(lab_dir, now=now)
            state_path = lab_dir / "state.json"
            state = json.loads(state_path.read_text())
            state["last_storage_check_at"] = (now - timedelta(seconds=30)).isoformat()
            state_path.write_text(json.dumps(state))
            old_log_time = (now - timedelta(hours=2)).timestamp()
            for log in (lab_dir / "logs").glob("collector_*.log"):
                os.utime(log, (old_log_time, old_log_time))

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
                stale_log_seconds=1800,
            )

            self.assertTrue(result.healthy, result.summary())
            self.assertNotIn("stale_log", [issue.code for issue in result.issues])

    def test_evaluate_health_accepts_missing_log_when_state_heartbeat_is_fresh(self):
        now = datetime(2026, 4, 28, 18, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._write_config(root)
            lab_dir = root / "data" / "paper" / "prediction_lab"
            self._write_state(lab_dir, now=now)
            for log in (lab_dir / "logs").glob("collector_*.log"):
                log.unlink()
            state_path = lab_dir / "state.json"
            state = json.loads(state_path.read_text())
            state["last_storage_check_at"] = (now - timedelta(seconds=30)).isoformat()
            state_path.write_text(json.dumps(state))

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
                stale_log_seconds=1800,
            )

            self.assertTrue(result.healthy, result.summary())
            self.assertNotIn("missing_log", [issue.code for issue in result.issues])
            self.assertEqual(result.details["latest_log_status"], "missing_but_state_heartbeat_fresh")

    def test_evaluate_health_accepts_fresh_supervisor_log(self):
        now = datetime(2026, 4, 28, 18, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._write_config(root)
            lab_dir = root / "data" / "paper" / "prediction_lab"
            self._write_state(lab_dir, now=now, write_collector_log=False)
            supervisor_log = lab_dir / "collector.supervisor.log"
            supervisor_log.write_text("collector alive\n")
            ts = now.timestamp()
            os.utime(supervisor_log, (ts, ts))

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

            issue_codes = [issue.code for issue in result.issues]
            self.assertTrue(result.healthy, result.summary())
            self.assertEqual(result.details["latest_log"], str(supervisor_log))
            self.assertNotIn("missing_log", issue_codes)
            market_snapshots = result.details["replay_inputs"]["market_snapshots"]
            self.assertEqual(market_snapshots["path"], str(lab_dir / "market_snapshots.jsonl"))
            self.assertTrue(market_snapshots["exists"])
            self.assertGreater(market_snapshots["size_bytes"], 0)
            self.assertEqual(market_snapshots["age_seconds"], 0.0)

    def test_evaluate_health_downgrades_stale_collect_when_liveness_is_fresh(self):
        now = datetime(2026, 4, 28, 18, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = self._write_config(root)
            lab_dir = root / "data" / "paper" / "prediction_lab"
            self._write_state(
                lab_dir,
                now=now,
                last_collect_delta=timedelta(hours=2),
                last_storage_check_delta=timedelta(seconds=30),
                write_collector_log=False,
            )

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
                stale_log_seconds=1800,
            )

            stale_collect = [issue for issue in result.issues if issue.code == "stale_collect"]
            self.assertTrue(result.healthy, result.summary())
            self.assertEqual(len(stale_collect), 1)
            self.assertEqual(stale_collect[0].severity, "warning")
            self.assertTrue(result.details["liveness_fresh"])
            self.assertTrue(result.details["replay_inputs"]["market_snapshots"]["exists"])

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

    def test_openclaw_resolver_uses_env_path_when_cron_path_is_minimal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            openclaw = Path(tmpdir) / "openclaw"
            openclaw.write_text("#!/bin/sh\nexit 0\n")
            openclaw.chmod(0o755)

            with patch.dict(os.environ, {"OPENCLAW_BIN": str(openclaw), "PATH": "/usr/bin"}, clear=False):
                with patch("shutil.which", return_value=None):
                    self.assertEqual(monitor.resolve_openclaw_bin(), str(openclaw))

    def test_alert_and_repair_use_resolved_openclaw_path(self):
        calls = []

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        with patch.object(monitor, "resolve_openclaw_bin", return_value="/opt/openclaw"):
            with patch.object(monitor, "run_command", side_effect=lambda cmd, **kwargs: calls.append(cmd) or Result()):
                self.assertTrue(monitor.send_telegram_alert("msg", target="chat", thread_id="8"))
                self.assertTrue(monitor.trigger_repair_cron("job-1"))

        self.assertEqual(calls[0][0], "/opt/openclaw")
        self.assertEqual(calls[0][1:4], ["message", "send", "--channel"])
        self.assertEqual(calls[1], ["/opt/openclaw", "cron", "run", "job-1"])

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
