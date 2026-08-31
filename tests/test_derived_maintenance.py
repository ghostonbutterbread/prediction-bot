import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot.derived_maintenance import run_derived_maintenance_once
from bot.resolution_feed import ResolutionFeedResult


class DerivedMaintenanceTests(unittest.TestCase):
    def test_due_promotion_runs_after_successful_resolution_and_records_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state_path = root / "derived-maintenance-state.json"
            config = {
                "derived_maintenance": {
                    "enabled": True,
                    "source_router_promotion": {
                        "enabled": True,
                        "interval_seconds": 7200,
                        "collector_snapshots_path": str(root / "snapshots.jsonl"),
                        "strict_resolutions_path": str(root / "resolutions.jsonl"),
                        "state_path": str(state_path),
                    },
                }
            }
            resolution_calls = []
            promotion_calls = []

            def resolve(received_config, *, force):
                resolution_calls.append((received_config, force))
                return ResolutionFeedResult(
                    status="refreshed",
                    refreshed=True,
                    reason=None,
                    output_path=root / "resolutions.jsonl",
                    report_path=None,
                    state_path=root / "resolution-state.json",
                )

            def promote(**kwargs):
                promotion_calls.append(kwargs)
                return {"status": "history_ready", "generation_dir": str(root / "generation")}

            now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
            result = run_derived_maintenance_once(config, now=now, resolve=resolve, promote=promote)
            state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(result.resolution_status, "refreshed")
        self.assertEqual(result.promotion_status, "history_ready")
        self.assertTrue(result.promotion_ran)
        self.assertEqual(resolution_calls, [(config, True)])
        self.assertEqual(
            promotion_calls,
            [{
                "collector_snapshots_path": str(root / "snapshots.jsonl"),
                "strict_resolutions_path": root / "resolutions.jsonl",
                "output_root": None,
            }],
        )
        self.assertEqual(state["last_successful_promotion_at"], "2026-08-31T12:00:00+00:00")

    def test_promotion_is_skipped_until_its_configured_interval_is_due(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state_path = root / "derived-maintenance-state.json"
            state_path.write_text(json.dumps({"last_successful_promotion_at": "2026-08-31T12:00:00+00:00"}), encoding="utf-8")
            config = {
                "derived_maintenance": {
                    "enabled": True,
                    "source_router_promotion": {
                        "enabled": True,
                        "interval_seconds": 7200,
                        "collector_snapshots_path": str(root / "snapshots.jsonl"),
                        "strict_resolutions_path": str(root / "resolutions.jsonl"),
                        "state_path": str(state_path),
                    },
                }
            }

            result = run_derived_maintenance_once(
                config,
                now=datetime(2026, 8, 31, 12, 30, tzinfo=timezone.utc),
                resolve=lambda received_config, *, force: ResolutionFeedResult(
                    status="refreshed", refreshed=True, reason=None, output_path=None, report_path=None, state_path=root / "resolution-state.json"
                ),
                promote=lambda **kwargs: (_ for _ in ()).throw(AssertionError("promotion must not run before its interval")),
            )

        self.assertEqual(result.resolution_status, "refreshed")
        self.assertEqual(result.promotion_status, "skipped_not_due")
        self.assertFalse(result.promotion_ran)

    def test_promotion_error_preserves_its_due_state_without_rerunning_resolver_early(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state_path = root / "state.json"
            config = {
                "derived_maintenance": {
                    "enabled": True,
                    "state_path": str(state_path),
                    "resolution_interval_seconds": 1800,
                    "source_router_promotion": {
                        "enabled": True,
                        "interval_seconds": 7200,
                        "collector_snapshots_path": str(root / "snapshots.jsonl"),
                        "strict_resolutions_path": str(root / "resolutions.jsonl"),
                    },
                }
            }
            resolution_calls = []

            def resolve(received_config, *, force):
                resolution_calls.append((received_config, force))
                return ResolutionFeedResult(
                    status="refreshed", refreshed=True, reason=None, output_path=root / "resolutions.jsonl", report_path=None, state_path=root / "resolution-state.json"
                )

            with self.assertRaisesRegex(RuntimeError, "promotion failed"):
                run_derived_maintenance_once(
                    config,
                    now=datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
                    resolve=resolve,
                    promote=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("promotion failed")),
                )
            second = run_derived_maintenance_once(
                config,
                now=datetime(2026, 8, 31, 12, 5, tzinfo=timezone.utc),
                resolve=resolve,
                promote=lambda **kwargs: (_ for _ in ()).throw(AssertionError("promotion must wait for resolver cadence")),
            )

        self.assertEqual(resolution_calls, [(config, True)])
        self.assertEqual(second.resolution_status, "skipped_not_due")
        self.assertEqual(second.promotion_status, "skipped_resolution_not_refreshed")

    def test_promotion_requires_the_configured_resolution_path_to_match_the_refresh(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = {
                "derived_maintenance": {
                    "enabled": True,
                    "state_path": str(root / "state.json"),
                    "source_router_promotion": {
                        "enabled": True,
                        "interval_seconds": 7200,
                        "collector_snapshots_path": str(root / "snapshots.jsonl"),
                        "strict_resolutions_path": str(root / "stale-resolutions.jsonl"),
                    },
                }
            }
            result = run_derived_maintenance_once(
                config,
                now=datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
                resolve=lambda received_config, *, force: ResolutionFeedResult(
                    status="refreshed", refreshed=True, reason=None, output_path=root / "fresh-resolutions.jsonl", report_path=None, state_path=root / "resolution-state.json"
                ),
                promote=lambda **kwargs: (_ for _ in ()).throw(AssertionError("promotion must reject a mismatched resolution artifact")),
            )

        self.assertEqual(result.promotion_status, "skipped_resolution_output_mismatch")
        self.assertFalse(result.promotion_ran)

    def test_promotion_does_not_run_when_resolution_did_not_refresh(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = {
                "derived_maintenance": {
                    "enabled": True,
                    "source_router_promotion": {
                        "enabled": True,
                        "interval_seconds": 7200,
                        "collector_snapshots_path": str(root / "snapshots.jsonl"),
                        "strict_resolutions_path": str(root / "resolutions.jsonl"),
                        "state_path": str(root / "state.json"),
                    },
                }
            }

            result = run_derived_maintenance_once(
                config,
                now=datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
                resolve=lambda received_config, *, force: ResolutionFeedResult(
                    status="missing_input", refreshed=False, reason="missing_decision_ledger", output_path=None, report_path=None, state_path=root / "resolution-state.json"
                ),
                promote=lambda **kwargs: (_ for _ in ()).throw(AssertionError("promotion must wait for a refreshed resolver pass")),
            )

        self.assertEqual(result.promotion_status, "skipped_resolution_not_refreshed")
        self.assertFalse(result.promotion_ran)
