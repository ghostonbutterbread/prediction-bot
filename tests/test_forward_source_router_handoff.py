import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from bot.weather.forward_source_router_handoff import (
    ForwardPaperHandoffError,
    build_forward_source_router_handoff,
    decision_idempotency_id,
    execution_intent_id,
    validate_forward_snapshot,
    validate_shared_lane_snapshots,
)


ROOT = Path(__file__).resolve().parents[1]


class ForwardSourceRouterHandoffTests(unittest.TestCase):
    def _manifest(self, **overrides):
        values = {
            "cohort_id": "source-router-forward-20260813",
            "start_at": "2026-08-14T00:00:00Z",
            "shared_snapshot_manifest_id": "collector-manifest-sha256:abc",
            "policy_bundles": {
                "control_stable": {"version": "stable-v4", "sha256": "a" * 64},
                "shadow_source_router_no_price_guard": {"version": "router-v1", "sha256": "b" * 64},
            },
        }
        values.update(overrides)
        return build_forward_source_router_handoff(**values)

    def test_shared_snapshot_cannot_collide_across_lane_decisions_or_wallets(self):
        manifest = self._manifest()
        snapshot = {
            "collector_snapshot_manifest_id": manifest["shared_collector_snapshot_manifest"]["manifest_id"],
            "shared_snapshot_id": "snapshot-1",
            "shared_candidate_id": "candidate-1",
            "market_id": "KXTEST-1",
            "observed_at_utc": "2026-08-14T00:00:01Z",
            "raw_row_sha256": "c" * 64,
            "weather_source_snapshot": {"station_resolution": {"city_id": "seattle_wa"}},
        }

        control = validate_forward_snapshot(manifest, snapshot, lane_id="control_stable")
        candidate = validate_forward_snapshot(manifest, snapshot, lane_id="shadow_source_router_no_price_guard")
        self.assertEqual(control["shared_snapshot_id"], candidate["shared_snapshot_id"])
        self.assertNotEqual(control["decision_namespace"], candidate["decision_namespace"])
        self.assertNotEqual(control["paper_wallet_namespace"], candidate["paper_wallet_namespace"])
        self.assertNotEqual(control["decision_idempotency_id"], candidate["decision_idempotency_id"])

        retry = decision_idempotency_id(
            decision_key=control["decision_key"], environment="paper", lane_id="control_stable",
            policy_bundle_hash="a" * 64, evaluator_version=manifest["evaluator_version"], decision_role="control",
        )
        self.assertEqual(retry, control["decision_idempotency_id"])
        self.assertNotEqual(
            execution_intent_id(control["decision_idempotency_id"], "paper", control["paper_wallet_namespace"]),
            execution_intent_id(control["decision_idempotency_id"], "live", "live/future"),
        )

    def test_rejects_prestart_and_mismatched_collector_snapshot_manifest(self):
        manifest = self._manifest()
        good = {
            "collector_snapshot_manifest_id": "collector-manifest-sha256:abc",
            "shared_snapshot_id": "snapshot-1", "shared_candidate_id": "candidate-1", "market_id": "KXTEST-1",
            "observed_at_utc": "2026-08-14T00:00:00Z",
            "raw_row_sha256": "c" * 64,
        }
        with self.assertRaisesRegex(ForwardPaperHandoffError, "pre-start"):
            validate_forward_snapshot(manifest, {**good, "observed_at_utc": "2026-08-13T23:59:59Z"}, lane_id="control_stable")
        with self.assertRaisesRegex(ForwardPaperHandoffError, "manifest identity"):
            validate_forward_snapshot(manifest, {**good, "collector_snapshot_manifest_id": "other"}, lane_id="control_stable")

    def test_rejects_control_candidate_snapshot_identity_mismatch(self):
        manifest = self._manifest()
        snapshot = {
            "collector_snapshot_manifest_id": "collector-manifest-sha256:abc", "shared_snapshot_id": "snapshot-1",
            "shared_candidate_id": "candidate-1", "market_id": "KXTEST-1", "observed_at_utc": "2026-08-14T00:00:00Z",
            "raw_row_sha256": "c" * 64,
        }
        with self.assertRaisesRegex(ForwardPaperHandoffError, "identities must match"):
            validate_shared_lane_snapshots(manifest, {
                "control_stable": snapshot,
                "shadow_source_router_no_price_guard": {**snapshot, "shared_snapshot_id": "snapshot-2"},
            })

    def test_rejects_single_lane_pair_validation(self):
        manifest = self._manifest()
        snapshot = {
            "collector_snapshot_manifest_id": "collector-manifest-sha256:abc", "shared_snapshot_id": "snapshot-1",
            "shared_candidate_id": "candidate-1", "market_id": "KXTEST-1", "observed_at_utc": "2026-08-14T00:00:00Z",
            "raw_row_sha256": "c" * 64,
        }
        with self.assertRaisesRegex(ForwardPaperHandoffError, "exactly control and candidate"):
            validate_shared_lane_snapshots(manifest, {"control_stable": snapshot})

    def test_is_disabled_and_prohibits_outcome_wallet_and_pnl_router_inputs(self):
        manifest = self._manifest()
        self.assertFalse(manifest["enabled"])
        self.assertTrue(manifest["paper_only"])
        self.assertEqual(manifest["router_input_contract"]["forbidden_categories"], ["resolution", "wallet", "pnl"])
        self.assertEqual(manifest["forward_paper_requirements"]["capacity_status"], "pending_recorded_executable_quotes")

    def test_rejects_outcome_data_but_allows_station_resolution_metadata(self):
        manifest = self._manifest()
        snapshot = {
            "collector_snapshot_manifest_id": "collector-manifest-sha256:abc", "shared_snapshot_id": "snapshot-1",
            "shared_candidate_id": "candidate-1", "market_id": "KXTEST-1", "observed_at_utc": "2026-08-14T00:00:00Z",
            "raw_row_sha256": "c" * 64, "weather_source_snapshot": {"station_resolution": {"city_id": "seattle_wa"}},
        }
        validate_forward_snapshot(manifest, snapshot, lane_id="control_stable")
        with self.assertRaisesRegex(ForwardPaperHandoffError, "prohibits"):
            validate_forward_snapshot(manifest, {**snapshot, "official_outcome": "YES"}, lane_id="control_stable")

    def test_smoke_cli_writes_a_disabled_manifest_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "forward-handoff.json"
            completed = subprocess.run(
                [
                    sys.executable, "scripts/build_forward_source_router_handoff.py", "--cohort-id", "smoke",
                    "--start-at", "2026-08-14T00:00:00Z", "--shared-snapshot-manifest-id", "collector-smoke",
                    "--control-policy-version", "stable-v4", "--control-policy-sha256", "a" * 64,
                    "--router-policy-version", "router-v1", "--router-policy-sha256", "b" * 64,
                    "--output", str(output),
                ], cwd=ROOT, check=True, capture_output=True, text=True,
            )
            self.assertIn("enabled=false", completed.stdout)
            self.assertIn('"enabled": false', output.read_text(encoding="utf-8"))

    def test_smoke_cli_refuses_to_overwrite_an_existing_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "existing.json"
            output.write_text("preserve-me\n", encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable, "scripts/build_forward_source_router_handoff.py", "--cohort-id", "smoke",
                    "--start-at", "2026-08-14T00:00:00Z", "--shared-snapshot-manifest-id", "collector-smoke",
                    "--control-policy-version", "stable-v4", "--control-policy-sha256", "a" * 64,
                    "--router-policy-version", "router-v1", "--router-policy-sha256", "b" * 64,
                    "--output", str(output),
                ], cwd=ROOT, capture_output=True, text=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(output.read_text(encoding="utf-8"), "preserve-me\n")


if __name__ == "__main__":
    unittest.main()
