import importlib
import io
import json
import os
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from bot.live_repeatability import build_live_repeatability_report


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def clean_lifecycle() -> list[dict]:
    return [
        {
            "timestamp": "2026-04-20T10:00:00+00:00",
            "event": "startup",
            "details": {"mode": "live", "trading_enabled": False},
        },
        {
            "timestamp": "2026-04-20T10:10:00+00:00",
            "event": "reconciliation_completed",
            "details": {"reconciliation_verdict": "safe", "reconciliation_issues": []},
        },
        {
            "timestamp": "2026-04-20T10:20:00+00:00",
            "event": "shutdown",
            "details": {"mode": "live", "scans": 1, "trades": 0},
        },
        {
            "timestamp": "2026-04-21T10:00:00+00:00",
            "event": "startup",
            "details": {"mode": "live", "trading_enabled": False},
        },
        {
            "timestamp": "2026-04-21T10:10:00+00:00",
            "event": "reconciliation_completed",
            "details": {"reconciliation_verdict": "safe", "reconciliation_issues": []},
        },
        {
            "timestamp": "2026-04-21T10:20:00+00:00",
            "event": "shutdown",
            "details": {"mode": "live", "scans": 1, "trades": 0},
        },
    ]


def clean_reconciliation() -> list[dict]:
    return [
        {
            "timestamp": "2026-04-20T10:05:00+00:00",
            "source": "startup_reconciliation",
            "exchange": "kalshi",
            "verdict": "safe",
            "severity": "none",
            "action": "log_only",
            "issues": [],
            "state_flags": [],
            "balance": 25.0,
            "available_cash": 25.0,
            "reserved_capital": 0.0,
            "filled_exposure": 0.0,
            "pending_exposure": 0.0,
            "open_positions": 0,
            "open_orders": 0,
            "partial_fills": 0,
        },
        {
            "timestamp": "2026-04-21T10:05:00+00:00",
            "source": "startup_reconciliation",
            "exchange": "kalshi",
            "verdict": "safe",
            "severity": "none",
            "action": "log_only",
            "issues": [],
            "state_flags": [],
            "balance": 25.0,
            "available_cash": 25.0,
            "reserved_capital": 0.0,
            "filled_exposure": 0.0,
            "pending_exposure": 0.0,
            "open_positions": 0,
            "open_orders": 0,
            "partial_fills": 0,
        },
    ]


def clean_hourly() -> list[dict]:
    return [
        {
            "timestamp": "2026-04-20T10:15:00+00:00",
            "live_runtime_state": {"state": "safe", "issues": []},
            "safety_pause": {"active": False},
        },
        {
            "timestamp": "2026-04-21T10:15:00+00:00",
            "live_runtime_state": {"state": "safe", "issues": []},
            "safety_pause": {"active": False},
        },
    ]


def clean_trade_row(**overrides) -> dict:
    row = {
        "timestamp": "2026-04-21T10:06:00+00:00",
        "schema_name": "execution_audit_row",
        "schema_version": 1,
        "trade_id": "trade-1",
        "market_id": "MKT-1",
        "direction": "BUY_YES",
        "status": "filled",
        "lifecycle_state": "filled_open",
        "decision_reason_code": "approved",
        "requested_size": 1.0,
        "approved_size": 1.0,
        "placed_size": 1.0,
        "filled_size": 1.0,
        "remaining_size": 0.0,
        "reserved_capital": 1.0,
        "execution_revalidated": True,
        "execution_revalidation_outcome": "approved",
        "execution_snapshot_source": "book",
        "resolved": False,
    }
    row.update(overrides)
    return row


def write_empty_jsonl(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def write_clean_required_artifacts(data_dir: Path) -> None:
    write_jsonl(data_dir / "lifecycle.jsonl", clean_lifecycle())
    write_jsonl(data_dir / "reconciliation.jsonl", clean_reconciliation())
    write_jsonl(data_dir / "hourly_summary.jsonl", clean_hourly())
    write_empty_jsonl(data_dir / "trades.jsonl")
    write_empty_jsonl(data_dir / "risk_blocks.jsonl")


class LiveRepeatabilityReportTests(unittest.TestCase):
    def test_insufficient_sessions_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            write_jsonl(data_dir / "lifecycle.jsonl", clean_lifecycle()[:3])
            write_jsonl(data_dir / "reconciliation.jsonl", clean_reconciliation()[:1])
            write_jsonl(data_dir / "hourly_summary.jsonl", clean_hourly()[:1])
            write_empty_jsonl(data_dir / "trades.jsonl")
            write_empty_jsonl(data_dir / "risk_blocks.jsonl")

            report = build_live_repeatability_report(data_dir)

        self.assertFalse(report["ready"])
        self.assertEqual(report["status"], "blocked")
        self.assertTrue(any("at least 2" in issue for issue in report["issues"]))

    def test_clean_repeated_sessions_pass_readiness(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            write_clean_required_artifacts(data_dir)

            report = build_live_repeatability_report(data_dir, sessions=2)

        self.assertTrue(report["ready"])
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["sessions_reviewed"], 2)
        self.assertTrue(report["summary"]["direct_reconciliation_fields_present"])

    def test_legacy_string_false_resolved_trade_does_not_fail_repeatability(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            write_jsonl(data_dir / "lifecycle.jsonl", clean_lifecycle())
            write_jsonl(data_dir / "reconciliation.jsonl", clean_reconciliation())
            write_jsonl(data_dir / "hourly_summary.jsonl", clean_hourly())
            write_jsonl(
                data_dir / "trades.jsonl",
                [
                    {
                        "timestamp": "2026-04-21T10:06:00+00:00",
                        "schema_name": "execution_audit_row",
                        "schema_version": 1,
                        "trade_id": "legacy-string-false",
                        "market_id": "MKT-1",
                        "direction": "BUY_YES",
                        "status": "filled",
                        "lifecycle_state": "filled_open",
                        "decision_reason_code": "approved",
                        "requested_size": 1.0,
                        "approved_size": 1.0,
                        "placed_size": 1.0,
                        "filled_size": 1.0,
                        "remaining_size": 0.0,
                        "reserved_capital": 1.0,
                        "execution_revalidated": True,
                        "execution_revalidation_outcome": "approved",
                        "execution_snapshot_source": "book",
                        "resolved": "False",
                    }
                ],
            )
            write_empty_jsonl(data_dir / "risk_blocks.jsonl")

            report = build_live_repeatability_report(data_dir, sessions=2)

        self.assertTrue(report["ready"])
        self.assertFalse(any("resolved_flag_status_mismatch" in issue for issue in report["issues"]))

    def test_unparseable_trade_time_anchor_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            write_jsonl(data_dir / "lifecycle.jsonl", clean_lifecycle())
            write_jsonl(data_dir / "reconciliation.jsonl", clean_reconciliation())
            write_jsonl(data_dir / "hourly_summary.jsonl", clean_hourly())
            write_jsonl(data_dir / "trades.jsonl", [clean_trade_row(timestamp="not-a-timestamp")])
            write_empty_jsonl(data_dir / "risk_blocks.jsonl")

            report = build_live_repeatability_report(data_dir, sessions=2)

        self.assertFalse(report["ready"])
        joined = "\n".join(report["issues"])
        self.assertIn("trades.jsonl row 1", joined)
        self.assertIn("no parseable time anchor", joined)

    def test_created_at_anchor_is_accepted_for_trade_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            write_jsonl(data_dir / "lifecycle.jsonl", clean_lifecycle())
            write_jsonl(data_dir / "reconciliation.jsonl", clean_reconciliation())
            write_jsonl(data_dir / "hourly_summary.jsonl", clean_hourly())
            row = clean_trade_row(created_at="2026-04-21T10:06:00+00:00")
            row.pop("timestamp")
            write_jsonl(data_dir / "trades.jsonl", [row])
            write_empty_jsonl(data_dir / "risk_blocks.jsonl")

            report = build_live_repeatability_report(data_dir, sessions=2)

        self.assertTrue(report["ready"])
        self.assertEqual(report["sessions"][-1]["trade_rows"], 1)

    def test_missing_expected_artifact_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            write_jsonl(data_dir / "lifecycle.jsonl", clean_lifecycle())
            write_jsonl(data_dir / "reconciliation.jsonl", clean_reconciliation())
            write_empty_jsonl(data_dir / "trades.jsonl")
            write_empty_jsonl(data_dir / "risk_blocks.jsonl")

            report = build_live_repeatability_report(data_dir, sessions=2)

        self.assertFalse(report["ready"])
        self.assertTrue(any("hourly_summary.jsonl" in issue for issue in report["issues"]))

    def test_lifecycle_degraded_reconciliation_event_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            lifecycle = clean_lifecycle()
            lifecycle[1]["details"] = {
                "reconciliation_verdict": "degraded",
                "runtime_state": "degraded",
                "reconciliation_issues": ["resting_orders_present"],
            }
            write_jsonl(data_dir / "lifecycle.jsonl", lifecycle)
            write_jsonl(data_dir / "reconciliation.jsonl", clean_reconciliation())
            write_jsonl(data_dir / "hourly_summary.jsonl", clean_hourly())
            write_empty_jsonl(data_dir / "trades.jsonl")
            write_empty_jsonl(data_dir / "risk_blocks.jsonl")

            report = build_live_repeatability_report(data_dir, sessions=2)

        self.assertFalse(report["ready"])
        joined = "\n".join(report["issues"])
        self.assertIn("lifecycle reconciliation event: reconciliation verdict is degraded", joined)
        self.assertIn("resting_orders_present", joined)

    def test_degraded_and_contradictory_artifacts_fail(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            reconciliation = clean_reconciliation()
            reconciliation[1]["verdict"] = "degraded"
            reconciliation[1]["issues"] = ["partial_fill_exposure_present"]
            write_jsonl(data_dir / "lifecycle.jsonl", clean_lifecycle())
            write_jsonl(data_dir / "reconciliation.jsonl", reconciliation)
            write_jsonl(data_dir / "hourly_summary.jsonl", clean_hourly())
            write_jsonl(
                data_dir / "trades.jsonl",
                [
                    {
                        "timestamp": "2026-04-21T10:06:00+00:00",
                        "schema_name": "execution_audit_row",
                        "schema_version": 1,
                        "trade_id": "ord-1",
                        "market_id": "MKT-1",
                        "direction": "BUY_YES",
                        "status": "canceled",
                        "lifecycle_state": "canceled_unfilled",
                        "decision_reason_code": "test",
                        "requested_size": 2.0,
                        "approved_size": 2.0,
                        "placed_size": 2.0,
                        "filled_size": 0.0,
                        "remaining_size": 1.0,
                        "reserved_capital": 0.0,
                        "execution_revalidated": False,
                        "execution_revalidation_outcome": None,
                        "execution_snapshot_source": "book",
                        "resolved": False,
                    }
                ],
            )
            write_empty_jsonl(data_dir / "risk_blocks.jsonl")

            report = build_live_repeatability_report(data_dir, sessions=2)

        self.assertFalse(report["ready"])
        joined = "\n".join(report["issues"])
        self.assertIn("reconciliation verdict is degraded", joined)
        self.assertIn("canceled_order_has_remaining_exposure", joined)

    def test_cli_report_does_not_import_runner_exchange_or_read_env(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "data" / "live"
            write_clean_required_artifacts(data_dir)
            config_path = root / "live.yaml"
            config_path.write_text(
                textwrap.dedent(
                    """
                    trading:
                      mode: live
                      enabled: false
                    runtime:
                      base_dir: data
                    """
                ).strip(),
                encoding="utf-8",
            )

            real_import = __import__
            real_getenv = os.getenv

            def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
                if name == "bot.runner" or name == "bot.config" or name.startswith("bot.exchanges"):
                    raise AssertionError(f"live-repeatability-report must not import {name}")
                return real_import(name, globals, locals, fromlist, level)

            def guarded_getenv(name, default=None):
                if name in {
                    "TRADING_ENABLED",
                    "TRADING_MODE",
                    "KALSHI_API_KEY_ID",
                    "KALSHI_PRIVATE_KEY_PATH",
                    "OPENROUTER_API_KEY",
                }:
                    raise AssertionError(f"live-repeatability-report must not read env {name}")
                return real_getenv(name, default)

            import dotenv

            stdout = io.StringIO()
            argv = ["main.py", "live-repeatability-report", "--config", str(config_path)]
            original_main = sys.modules.pop("main", None)
            try:
                with patch.object(sys, "argv", argv), patch.dict(os.environ, {"TRADING_ENABLED": "true"}, clear=True), patch(
                    "builtins.__import__", side_effect=guarded_import
                ), patch.object(os, "getenv", side_effect=guarded_getenv), patch.object(
                    dotenv, "load_dotenv", side_effect=AssertionError("live-repeatability-report must not load .env")
                ), patch.object(sys, "exit", side_effect=AssertionError("clean report should not exit")), redirect_stdout(stdout):
                    with _cwd(root):
                        main = importlib.import_module("main")
                        main.main()
            finally:
                sys.modules.pop("main", None)
                if original_main is not None:
                    sys.modules["main"] = original_main

        self.assertIn("Live repeatability evidence: READY", stdout.getvalue())


class _cwd:
    def __init__(self, path: Path):
        self.path = str(path)
        self.previous = None

    def __enter__(self):
        self.previous = os.getcwd()
        os.chdir(self.path)

    def __exit__(self, exc_type, exc, tb):
        os.chdir(self.previous)


if __name__ == "__main__":
    unittest.main()
