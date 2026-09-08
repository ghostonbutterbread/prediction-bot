import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from bot.config import load_config
from bot.derived_maintenance import run_derived_maintenance_once
from bot.resolution_feed import ResolutionFeedResult


class DerivedMaintenanceTests(unittest.TestCase):
    def test_disabled_configuration_is_a_noop_without_a_state_path(self):
        result = run_derived_maintenance_once(
            {"derived_maintenance": {"enabled": False}},
            resolve=lambda received_config, *, force: (_ for _ in ()).throw(AssertionError("disabled maintenance must not resolve")),
            promote=lambda **kwargs: (_ for _ in ()).throw(AssertionError("disabled maintenance must not promote")),
        )

        self.assertEqual(result.resolution_status, "disabled")
        self.assertEqual(result.promotion_status, "disabled")
        self.assertFalse(result.resolution_ran)
        self.assertFalse(result.promotion_ran)

    def test_loaded_storage_root_reaches_real_promotion(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {}, clear=True):
            root = Path(tmpdir)
            storage = root / "collector"
            inputs = storage / "inputs"
            inputs.mkdir(parents=True)
            snapshots = inputs / "snapshots.jsonl"
            resolutions = inputs / "resolutions.jsonl"
            snapshots.write_text(json.dumps({
                "market_id": "KXHIGHSEA-26AUG03-T70",
                "observed_at": "2026-08-01T12:00:00+00:00",
                "question": "Will Seattle high temperature be above 70°?",
                "yes_price": 0.42,
                "no_price": 0.58,
            }) + "\n", encoding="utf-8")
            resolutions.write_text("", encoding="utf-8")
            original_inputs = (snapshots.read_bytes(), resolutions.read_bytes())
            config_path = root / "config.yaml"
            config_path.write_text(json.dumps({
                "runtime": {"storage_root": str(storage)},
                "derived_maintenance": {
                    "enabled": True,
                    "state_path": "data/derived_reports/maintenance/state.json",
                    "source_router_promotion": {
                        "enabled": True,
                        "collector_snapshots_path": "inputs/snapshots.jsonl",
                        "strict_resolutions_path": "inputs/resolutions.jsonl",
                    },
                },
            }), encoding="utf-8")
            config = load_config(config_path)
            calls = []

            def resolve(received_config, *, force):
                calls.append((received_config, force))
                return ResolutionFeedResult(
                    status="refreshed", refreshed=True, reason=None,
                    output_path=resolutions, report_path=None,
                    state_path=storage / "data/derived_reports/resolver/state.json",
                )

            result = run_derived_maintenance_once(
                config, now=datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
                resolve=resolve,
            )

            self.assertEqual(calls, [(config, True)])
            self.assertTrue(result.promotion_ran)
            self.assertEqual(result.promotion_status, "no_router_history")
            derived = storage / "data/derived_reports"
            output = derived / "auto_source_router_history"
            current = output / "current"
            self.assertTrue(current.is_symlink())
            self.assertTrue(current.resolve().is_relative_to(output / "generations"))
            manifest = json.loads((current / "source_router_history_promotion.manifest.json").read_text())
            self.assertEqual(manifest["status"], "no_router_history")
            self.assertTrue(manifest["artifacts"])
            for artifact in manifest["artifacts"].values():
                self.assertTrue(Path(artifact).is_file())
                self.assertTrue(Path(artifact).resolve().is_relative_to(derived))
            state = json.loads(result.state_path.read_text())
            self.assertTrue(result.state_path.is_relative_to(derived))
            self.assertEqual(state["last_successful_promotion_at"], "2026-08-31T12:00:00+00:00")
            self.assertEqual((snapshots.read_bytes(), resolutions.read_bytes()), original_inputs)

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
                "storage_root": None,
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
