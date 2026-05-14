import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot.shared_market_runtime import (
    SharedMarketRuntimeManager,
    build_shared_market_snapshot_metadata,
    shared_market_runtime_root,
    shared_snapshot_is_fresh,
    should_bypass_shared_snapshot,
    snapshot_age_seconds,
)


class SharedMarketRuntimeManagerTests(unittest.TestCase):
    def _dt(self, text: str) -> datetime:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)

    def _manager(
        self,
        root: str | Path,
        *,
        shared_market_overrides: dict[str, object] | None = None,
    ) -> SharedMarketRuntimeManager:
        shared_market = {
            "default_interval_seconds": 900,
            "min_interval_seconds": 300,
            "publisher_lease_timeout_seconds": 120,
            "consumer_timeout_seconds": 300,
            "stop_when_idle": True,
            "snapshot_ttl_seconds": 1200,
        }
        if shared_market_overrides:
            shared_market.update(shared_market_overrides)
        return SharedMarketRuntimeManager(
            runtime_root=root,
            config={
                "shared_market": shared_market,
            },
        )

    def test_runtime_root_defaults_to_shared_market_directory(self):
        self.assertEqual(shared_market_runtime_root(), Path("data/shared_market_runtime"))
        self.assertEqual(
            shared_market_runtime_root({"runtime": {"base_dir": "/tmp/prediction-runtime"}}),
            Path("/tmp/prediction-runtime/shared_market_runtime"),
        )
        self.assertEqual(
            shared_market_runtime_root({"data_dir": "/tmp/prediction-runtime/paper"}),
            Path("/tmp/prediction-runtime/shared_market_runtime"),
        )

    def test_attach_bootstraps_publisher_and_acquire_renews_same_lease(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager(tmpdir)
            start = self._dt("2026-05-14T12:00:00+00:00")

            state = manager.attach(
                runtime_kind="paper",
                instance_id="paper-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=900,
                now=start,
            )
            self.assertEqual(state["publisher"]["runtime_kind"], "paper")
            self.assertEqual(state["publisher"]["instance_id"], "paper-a")

            renewed = manager.acquire_publisher_lease(
                runtime_kind="paper",
                instance_id="paper-a",
                now=start + timedelta(seconds=10),
            )
            self.assertEqual(renewed["publisher"]["runtime_kind"], "paper")
            self.assertEqual(renewed["publisher"]["instance_id"], "paper-a")
            self.assertEqual(
                renewed["publisher"]["last_heartbeat_at"],
                "2026-05-14T12:00:10+00:00",
            )

    def test_higher_priority_attach_does_not_preempt_healthy_publisher_and_detach_keeps_feed_alive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager(tmpdir)
            start = self._dt("2026-05-14T12:00:00+00:00")

            manager.attach(
                runtime_kind="paper",
                instance_id="paper-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=900,
                now=start,
            )
            attached = manager.attach(
                runtime_kind="collector",
                instance_id="collector-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=600,
                now=start + timedelta(seconds=5),
            )
            self.assertEqual(attached["publisher"]["runtime_kind"], "paper")
            self.assertEqual(attached["publisher"]["instance_id"], "paper-a")

            detached = manager.detach(
                runtime_kind="collector",
                instance_id="collector-a",
                now=start + timedelta(seconds=10),
            )
            self.assertEqual(detached["publisher"]["runtime_kind"], "paper")
            self.assertEqual(detached["publisher"]["instance_id"], "paper-a")
            self.assertEqual(len(detached["consumers"]), 1)

    def test_release_re_elects_by_priority_then_attachment_time_then_instance_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager(tmpdir)
            start = self._dt("2026-05-14T12:00:00+00:00")

            manager.attach(
                runtime_kind="paper",
                instance_id="paper-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=900,
                now=start,
            )
            tie_time = start + timedelta(seconds=5)
            manager.attach(
                runtime_kind="collector",
                instance_id="collector-b",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=600,
                now=tie_time,
            )
            manager.attach(
                runtime_kind="collector",
                instance_id="collector-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=600,
                now=tie_time,
            )

            released = manager.release_publisher_lease(
                runtime_kind="paper",
                instance_id="paper-a",
                now=start + timedelta(seconds=20),
            )
            self.assertEqual(released["publisher"]["runtime_kind"], "collector")
            self.assertEqual(released["publisher"]["instance_id"], "collector-a")

    def test_publisher_heartbeat_and_expiry_trigger_takeover(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager(tmpdir)
            start = self._dt("2026-05-14T12:00:00+00:00")

            manager.attach(
                runtime_kind="paper",
                instance_id="paper-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=900,
                now=start,
            )
            manager.attach(
                runtime_kind="collector",
                instance_id="collector-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=600,
                now=start + timedelta(seconds=1),
            )
            manager.renew_publisher_lease(
                runtime_kind="paper",
                instance_id="paper-a",
                now=start + timedelta(seconds=60),
            )

            still_healthy = manager.cleanup_expired(now=start + timedelta(seconds=180))
            self.assertEqual(still_healthy["publisher"]["runtime_kind"], "paper")

            taken_over = manager.cleanup_expired(now=start + timedelta(seconds=181))
            self.assertEqual(taken_over["publisher"]["runtime_kind"], "collector")
            self.assertEqual(taken_over["publisher"]["instance_id"], "collector-a")

    def test_expired_publisher_is_not_immediately_re_elected_without_reacquire(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager(
                tmpdir,
                shared_market_overrides={
                    "publisher_priority": {
                        "paper": 50,
                        "collector": 30,
                    }
                },
            )
            start = self._dt("2026-05-14T12:00:00+00:00")

            manager.attach(
                runtime_kind="paper",
                instance_id="paper-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=900,
                now=start,
            )
            manager.attach(
                runtime_kind="collector",
                instance_id="collector-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=600,
                now=start + timedelta(seconds=1),
            )
            manager.renew_publisher_lease(
                runtime_kind="paper",
                instance_id="paper-a",
                now=start + timedelta(seconds=60),
            )

            taken_over = manager.cleanup_expired(now=start + timedelta(seconds=181))
            after_heartbeat = manager.heartbeat(
                runtime_kind="paper",
                instance_id="paper-a",
                now=start + timedelta(seconds=182),
            )

            self.assertEqual(taken_over["publisher"]["runtime_kind"], "collector")
            self.assertEqual(taken_over["publisher"]["instance_id"], "collector-a")
            self.assertEqual(
                taken_over["consumers"]["paper:paper-a"]["publisher_reacquire_required_at"],
                "2026-05-14T12:03:01+00:00",
            )
            self.assertEqual(
                after_heartbeat["consumers"]["paper:paper-a"]["publisher_reacquire_required_at"],
                "2026-05-14T12:03:01+00:00",
            )

    def test_re_attaching_stale_former_publisher_does_not_clear_reacquire_guard(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager(
                tmpdir,
                shared_market_overrides={
                    "publisher_priority": {
                        "paper": 50,
                        "collector": 30,
                    }
                },
            )
            start = self._dt("2026-05-14T12:00:00+00:00")

            manager.attach(
                runtime_kind="paper",
                instance_id="paper-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=900,
                now=start,
            )
            manager.attach(
                runtime_kind="collector",
                instance_id="collector-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=600,
                now=start + timedelta(seconds=1),
            )
            manager.renew_publisher_lease(
                runtime_kind="paper",
                instance_id="paper-a",
                now=start + timedelta(seconds=60),
            )

            manager.cleanup_expired(now=start + timedelta(seconds=181))
            reattached = manager.attach(
                runtime_kind="paper",
                instance_id="paper-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=900,
                now=start + timedelta(seconds=182),
            )
            after_collector_detach = manager.detach(
                runtime_kind="collector",
                instance_id="collector-a",
                now=start + timedelta(seconds=183),
            )
            reacquired = manager.acquire_publisher_lease(
                runtime_kind="paper",
                instance_id="paper-a",
                now=start + timedelta(seconds=184),
            )

            self.assertEqual(
                reattached["consumers"]["paper:paper-a"]["publisher_reacquire_required_at"],
                "2026-05-14T12:03:01+00:00",
            )
            self.assertEqual(reattached["publisher"]["runtime_kind"], "collector")
            self.assertEqual(reattached["publisher"]["instance_id"], "collector-a")
            self.assertIsNone(after_collector_detach["publisher"])
            self.assertEqual(
                after_collector_detach["consumers"]["paper:paper-a"]["publisher_reacquire_required_at"],
                "2026-05-14T12:03:01+00:00",
            )
            self.assertEqual(reacquired["publisher"]["runtime_kind"], "paper")
            self.assertEqual(reacquired["publisher"]["instance_id"], "paper-a")
            self.assertNotIn(
                "publisher_reacquire_required_at",
                reacquired["consumers"]["paper:paper-a"],
            )

    def test_detach_and_reattach_do_not_clear_reacquire_guard_for_demoted_publisher(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager(
                tmpdir,
                shared_market_overrides={
                    "publisher_priority": {
                        "paper": 50,
                        "collector": 30,
                    }
                },
            )
            start = self._dt("2026-05-14T12:00:00+00:00")

            manager.attach(
                runtime_kind="paper",
                instance_id="paper-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=900,
                now=start,
            )
            manager.attach(
                runtime_kind="collector",
                instance_id="collector-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=600,
                now=start + timedelta(seconds=1),
            )

            demoted = manager.release_publisher_lease(
                runtime_kind="paper",
                instance_id="paper-a",
                now=start + timedelta(seconds=2),
            )
            detached = manager.detach(
                runtime_kind="paper",
                instance_id="paper-a",
                now=start + timedelta(seconds=3),
            )
            reattached = manager.attach(
                runtime_kind="paper",
                instance_id="paper-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=900,
                now=start + timedelta(seconds=4),
            )
            after_collector_detach = manager.detach(
                runtime_kind="collector",
                instance_id="collector-a",
                now=start + timedelta(seconds=5),
            )
            reacquired = manager.acquire_publisher_lease(
                runtime_kind="paper",
                instance_id="paper-a",
                now=start + timedelta(seconds=6),
            )

            self.assertEqual(demoted["publisher"]["runtime_kind"], "collector")
            self.assertEqual(demoted["publisher"]["instance_id"], "collector-a")
            self.assertEqual(
                demoted["consumers"]["paper:paper-a"]["publisher_reacquire_required_at"],
                "2026-05-14T12:00:02+00:00",
            )
            self.assertEqual(
                detached["publisher_reacquire_guards"]["paper:paper-a"],
                "2026-05-14T12:00:02+00:00",
            )
            self.assertNotIn("paper:paper-a", detached["consumers"])
            self.assertEqual(
                reattached["consumers"]["paper:paper-a"]["publisher_reacquire_required_at"],
                "2026-05-14T12:00:02+00:00",
            )
            self.assertEqual(reattached["publisher"]["runtime_kind"], "collector")
            self.assertEqual(reattached["publisher"]["instance_id"], "collector-a")
            self.assertIsNone(after_collector_detach["publisher"])
            self.assertEqual(
                after_collector_detach["consumers"]["paper:paper-a"]["publisher_reacquire_required_at"],
                "2026-05-14T12:00:02+00:00",
            )
            self.assertEqual(reacquired["publisher"]["runtime_kind"], "paper")
            self.assertEqual(reacquired["publisher"]["instance_id"], "paper-a")
            self.assertNotIn("publisher_reacquire_required_at", reacquired["consumers"]["paper:paper-a"])
            self.assertEqual(reacquired["publisher_reacquire_guards"], {})

    def test_failed_reacquire_while_other_publisher_is_healthy_does_not_clear_guard(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager(
                tmpdir,
                shared_market_overrides={
                    "publisher_priority": {
                        "paper": 50,
                        "collector": 30,
                    }
                },
            )
            start = self._dt("2026-05-14T12:00:00+00:00")

            manager.attach(
                runtime_kind="paper",
                instance_id="paper-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=900,
                now=start,
            )
            manager.attach(
                runtime_kind="collector",
                instance_id="collector-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=600,
                now=start + timedelta(seconds=1),
            )
            demoted = manager.release_publisher_lease(
                runtime_kind="paper",
                instance_id="paper-a",
                now=start + timedelta(seconds=2),
            )
            failed_reacquire = manager.acquire_publisher_lease(
                runtime_kind="paper",
                instance_id="paper-a",
                now=start + timedelta(seconds=3),
            )
            after_collector_detach = manager.detach(
                runtime_kind="collector",
                instance_id="collector-a",
                now=start + timedelta(seconds=4),
            )
            reacquired = manager.acquire_publisher_lease(
                runtime_kind="paper",
                instance_id="paper-a",
                now=start + timedelta(seconds=5),
            )

            self.assertEqual(demoted["publisher"]["runtime_kind"], "collector")
            self.assertEqual(
                failed_reacquire["consumers"]["paper:paper-a"]["publisher_reacquire_required_at"],
                "2026-05-14T12:00:02+00:00",
            )
            self.assertEqual(failed_reacquire["publisher"]["runtime_kind"], "collector")
            self.assertEqual(failed_reacquire["publisher"]["instance_id"], "collector-a")
            self.assertIsNone(after_collector_detach["publisher"])
            self.assertEqual(
                after_collector_detach["consumers"]["paper:paper-a"]["publisher_reacquire_required_at"],
                "2026-05-14T12:00:02+00:00",
            )
            self.assertEqual(reacquired["publisher"]["runtime_kind"], "paper")
            self.assertEqual(reacquired["publisher"]["instance_id"], "paper-a")
            self.assertNotIn("publisher_reacquire_required_at", reacquired["consumers"]["paper:paper-a"])

    def test_effective_interval_uses_min_attached_interval_bounded_by_minimum(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager(tmpdir)
            start = self._dt("2026-05-14T12:00:00+00:00")

            manager.attach(
                runtime_kind="collector",
                instance_id="collector-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=900,
                now=start,
            )
            manager.attach(
                runtime_kind="paper",
                instance_id="paper-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=600,
                now=start + timedelta(seconds=1),
            )
            state = manager.attach(
                runtime_kind="live",
                instance_id="live-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=60,
                latency_sensitive=True,
                now=start + timedelta(seconds=2),
            )

            self.assertEqual(state["effective_interval_seconds"], 300)

    def test_detach_last_consumer_releases_publisher_when_idle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager(tmpdir)
            start = self._dt("2026-05-14T12:00:00+00:00")

            manager.attach(
                runtime_kind="paper",
                instance_id="paper-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=900,
                now=start,
            )
            state = manager.detach(
                runtime_kind="paper",
                instance_id="paper-a",
                now=start + timedelta(seconds=5),
            )

            self.assertTrue(state["idle"])
            self.assertEqual(state["consumers"], {})
            self.assertIsNone(state["publisher"])
            self.assertTrue((Path(tmpdir) / "runtime_state.json").exists())

    def test_record_snapshot_metadata_requires_active_publisher(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager(tmpdir)
            start = self._dt("2026-05-14T12:00:00+00:00")
            snapshot = build_shared_market_snapshot_metadata(
                snapshot_id="snapshot-1",
                observed_at=start,
                publisher_runtime="collector",
                publisher_instance_id="collector-a",
                candidate_count=4,
                ttl_seconds=120,
            )

            with self.assertRaisesRegex(RuntimeError, "active publisher lease"):
                manager.record_snapshot_metadata(snapshot, now=start + timedelta(seconds=1))

            self.assertFalse((Path(tmpdir) / "runtime_state.json").exists())
            self.assertFalse((Path(tmpdir) / "latest_snapshot.json").exists())

    def test_record_snapshot_metadata_rejects_stale_previous_publisher_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager(
                tmpdir,
                shared_market_overrides={
                    "publisher_priority": {
                        "paper": 50,
                        "collector": 30,
                    }
                },
            )
            start = self._dt("2026-05-14T12:00:00+00:00")

            manager.attach(
                runtime_kind="paper",
                instance_id="paper-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=900,
                now=start,
            )
            manager.attach(
                runtime_kind="collector",
                instance_id="collector-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=600,
                now=start + timedelta(seconds=1),
            )
            manager.renew_publisher_lease(
                runtime_kind="paper",
                instance_id="paper-a",
                now=start + timedelta(seconds=60),
            )

            manager.cleanup_expired(now=start + timedelta(seconds=181))
            stale_snapshot = build_shared_market_snapshot_metadata(
                snapshot_id="snapshot-stale",
                observed_at=start + timedelta(seconds=181),
                publisher_runtime="paper",
                publisher_instance_id="paper-a",
                candidate_count=5,
                ttl_seconds=120,
            )

            with self.assertRaisesRegex(ValueError, "does not match active publisher lease"):
                manager.record_snapshot_metadata(stale_snapshot, now=start + timedelta(seconds=181))

            state = manager.read_state(now=start + timedelta(seconds=181))
            self.assertIsNone(state["latest_snapshot"])
            self.assertFalse((Path(tmpdir) / "latest_snapshot.json").exists())

    def test_publisher_snapshot_due_for_new_owner_when_fresh_snapshot_belongs_to_previous_publisher(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager(
                tmpdir,
                shared_market_overrides={
                    "publisher_priority": {
                        "paper": 50,
                        "collector": 30,
                    },
                    "publisher_lease_timeout_seconds": 1200,
                    "consumer_timeout_seconds": 1200,
                },
            )
            start = self._dt("2026-05-14T12:00:00+00:00")

            manager.attach(
                runtime_kind="paper",
                instance_id="paper-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=900,
                now=start,
            )
            manager.attach(
                runtime_kind="collector",
                instance_id="collector-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=600,
                now=start + timedelta(seconds=1),
            )
            manager.record_snapshot_metadata(
                build_shared_market_snapshot_metadata(
                    snapshot_id="snapshot-paper",
                    observed_at=start + timedelta(seconds=2),
                    publisher_runtime="paper",
                    publisher_instance_id="paper-a",
                    candidate_count=5,
                    ttl_seconds=120,
                ),
                now=start + timedelta(seconds=2),
            )
            manager.release_publisher_lease(
                runtime_kind="paper",
                instance_id="paper-a",
                now=start + timedelta(seconds=3),
            )

            self.assertTrue(
                manager.publisher_snapshot_due_for(
                    runtime_kind="collector",
                    instance_id="collector-a",
                    now=start + timedelta(seconds=4),
                )
            )
            self.assertFalse(
                manager.publisher_snapshot_due_for(
                    runtime_kind="paper",
                    instance_id="paper-a",
                    now=start + timedelta(seconds=4),
                )
            )

    def test_restart_restores_reacquire_guard_until_explicit_reacquire(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            shared_market_overrides = {
                "publisher_priority": {
                    "paper": 50,
                    "collector": 30,
                }
            }
            manager = self._manager(tmpdir, shared_market_overrides=shared_market_overrides)
            start = self._dt("2026-05-14T12:00:00+00:00")

            manager.attach(
                runtime_kind="paper",
                instance_id="paper-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=900,
                now=start,
            )
            manager.attach(
                runtime_kind="collector",
                instance_id="collector-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=600,
                now=start + timedelta(seconds=1),
            )
            manager.release_publisher_lease(
                runtime_kind="paper",
                instance_id="paper-a",
                now=start + timedelta(seconds=2),
            )
            detached = manager.detach(
                runtime_kind="paper",
                instance_id="paper-a",
                now=start + timedelta(seconds=3),
            )

            restarted = self._manager(tmpdir, shared_market_overrides=shared_market_overrides)
            reattached = restarted.attach(
                runtime_kind="paper",
                instance_id="paper-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=900,
                now=start + timedelta(seconds=4),
            )
            after_collector_detach = restarted.detach(
                runtime_kind="collector",
                instance_id="collector-a",
                now=start + timedelta(seconds=5),
            )

            restarted_again = self._manager(tmpdir, shared_market_overrides=shared_market_overrides)
            reacquired = restarted_again.acquire_publisher_lease(
                runtime_kind="paper",
                instance_id="paper-a",
                now=start + timedelta(seconds=6),
            )
            persisted = json.loads((Path(tmpdir) / "runtime_state.json").read_text(encoding="utf-8"))

            self.assertEqual(
                detached["publisher_reacquire_guards"]["paper:paper-a"],
                "2026-05-14T12:00:02+00:00",
            )
            self.assertEqual(
                reattached["consumers"]["paper:paper-a"]["publisher_reacquire_required_at"],
                "2026-05-14T12:00:02+00:00",
            )
            self.assertEqual(reattached["publisher"]["runtime_kind"], "collector")
            self.assertEqual(reattached["publisher"]["instance_id"], "collector-a")
            self.assertIsNone(after_collector_detach["publisher"])
            self.assertEqual(reacquired["publisher"]["runtime_kind"], "paper")
            self.assertEqual(reacquired["publisher"]["instance_id"], "paper-a")
            self.assertNotIn("publisher_reacquire_required_at", reacquired["consumers"]["paper:paper-a"])
            self.assertEqual(persisted["publisher_reacquire_guards"], {})

    def test_record_snapshot_metadata_persists_normalized_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager(tmpdir)
            start = self._dt("2026-05-14T12:00:00+00:00")

            manager.attach(
                runtime_kind="collector",
                instance_id="collector-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=900,
                now=start,
            )
            expected = build_shared_market_snapshot_metadata(
                snapshot_id="snapshot-1",
                observed_at=start,
                published_at=start + timedelta(seconds=3),
                publisher_runtime="collector",
                publisher_instance_id="collector-a",
                candidate_count=4,
                market_count=6,
                ttl_seconds=120,
                source_exchange="kalshi",
            )

            state = manager.record_snapshot_metadata(
                {
                    "snapshot_id": "snapshot-1",
                    "observed_at": start,
                    "published_at": start + timedelta(seconds=3),
                    "publisher_runtime": "collector",
                    "publisher_instance_id": "collector-a",
                    "candidate_count": "4",
                    "market_count": "6",
                    "ttl_seconds": "120",
                    "source_exchange": "kalshi",
                },
                now=start + timedelta(seconds=4),
            )
            persisted_state = json.loads((Path(tmpdir) / "runtime_state.json").read_text(encoding="utf-8"))
            persisted_snapshot = json.loads((Path(tmpdir) / "latest_snapshot.json").read_text(encoding="utf-8"))

            self.assertEqual(state["latest_snapshot"], expected)
            self.assertEqual(persisted_state["latest_snapshot"], expected)
            self.assertEqual(persisted_snapshot, expected)

    def test_read_state_cleans_up_and_persists_current_takeover_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager(
                tmpdir,
                shared_market_overrides={
                    "publisher_priority": {
                        "paper": 50,
                        "collector": 30,
                    }
                },
            )
            start = self._dt("2026-05-14T12:00:00+00:00")

            manager.attach(
                runtime_kind="paper",
                instance_id="paper-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=900,
                now=start,
            )
            manager.attach(
                runtime_kind="collector",
                instance_id="collector-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=600,
                now=start + timedelta(seconds=1),
            )
            manager.renew_publisher_lease(
                runtime_kind="paper",
                instance_id="paper-a",
                now=start + timedelta(seconds=60),
            )

            state = manager.read_state(now=start + timedelta(seconds=181))
            persisted = json.loads((Path(tmpdir) / "runtime_state.json").read_text(encoding="utf-8"))

            self.assertEqual(state["publisher"]["runtime_kind"], "collector")
            self.assertEqual(persisted["publisher"]["instance_id"], "collector-a")
            self.assertEqual(
                persisted["consumers"]["paper:paper-a"]["publisher_reacquire_required_at"],
                "2026-05-14T12:03:01+00:00",
            )

    def test_snapshot_helpers_cover_freshness_bypass_and_due_logic(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager(
                tmpdir,
                shared_market_overrides={
                    "publisher_lease_timeout_seconds": 1200,
                    "consumer_timeout_seconds": 1200,
                },
            )
            start = self._dt("2026-05-14T12:00:00+00:00")
            manager.attach(
                runtime_kind="collector",
                instance_id="collector-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=900,
                now=start,
            )
            manager.attach(
                runtime_kind="paper",
                instance_id="paper-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=900,
                now=start + timedelta(seconds=1),
            )

            snapshot = build_shared_market_snapshot_metadata(
                snapshot_id="snapshot-1",
                observed_at=start,
                published_at=start + timedelta(seconds=3),
                publisher_runtime="collector",
                publisher_instance_id="collector-a",
                candidate_count=4,
                ttl_seconds=120,
                source_exchange="kalshi",
            )
            self.assertEqual(snapshot["expires_at"], "2026-05-14T12:02:00+00:00")
            self.assertEqual(snapshot_age_seconds(snapshot, now=start + timedelta(seconds=20)), 20.0)
            self.assertTrue(
                shared_snapshot_is_fresh(
                    snapshot,
                    max_snapshot_age_seconds=30,
                    now=start + timedelta(seconds=20),
                )
            )
            self.assertFalse(
                shared_snapshot_is_fresh(
                    snapshot,
                    max_snapshot_age_seconds=300,
                    now=start + timedelta(seconds=121),
                )
            )
            self.assertTrue(
                shared_snapshot_is_fresh(
                    snapshot,
                    max_snapshot_age_seconds=300,
                    now=start + timedelta(seconds=120),
                )
            )
            self.assertFalse(
                shared_snapshot_is_fresh(
                    snapshot,
                    max_snapshot_age_seconds=300,
                    now=start + timedelta(seconds=120, microseconds=1),
                )
            )
            self.assertTrue(
                should_bypass_shared_snapshot(
                    snapshot,
                    max_snapshot_age_seconds=30,
                    now=start + timedelta(seconds=31),
                )
            )

            interval_snapshot = build_shared_market_snapshot_metadata(
                snapshot_id="snapshot-interval",
                observed_at=start,
                published_at=start + timedelta(seconds=3),
                publisher_runtime="collector",
                publisher_instance_id="collector-a",
                candidate_count=4,
                ttl_seconds=1200,
                source_exchange="kalshi",
            )
            manager.record_snapshot_metadata(interval_snapshot, now=start + timedelta(seconds=4))
            self.assertFalse(
                manager.publisher_snapshot_due_for(
                    runtime_kind="collector",
                    instance_id="collector-a",
                    now=start + timedelta(seconds=902),
                )
            )
            self.assertTrue(
                manager.publisher_snapshot_due_for(
                    runtime_kind="collector",
                    instance_id="collector-a",
                    now=start + timedelta(seconds=903),
                )
            )
            self.assertFalse(
                manager.publisher_snapshot_due_for(
                    runtime_kind="paper",
                    instance_id="paper-a",
                    now=start + timedelta(seconds=903),
                )
            )

    def test_publisher_snapshot_due_for_expired_snapshot_even_before_interval(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = self._manager(
                tmpdir,
                shared_market_overrides={
                    "publisher_lease_timeout_seconds": 1200,
                    "consumer_timeout_seconds": 1200,
                },
            )
            start = self._dt("2026-05-14T12:00:00+00:00")
            manager.attach(
                runtime_kind="collector",
                instance_id="collector-a",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=900,
                now=start,
            )
            snapshot = build_shared_market_snapshot_metadata(
                snapshot_id="snapshot-short-ttl",
                observed_at=start,
                published_at=start + timedelta(seconds=3),
                publisher_runtime="collector",
                publisher_instance_id="collector-a",
                candidate_count=4,
                ttl_seconds=120,
                source_exchange="kalshi",
            )

            manager.record_snapshot_metadata(snapshot, now=start + timedelta(seconds=4))
            self.assertFalse(
                manager.publisher_snapshot_due_for(
                    runtime_kind="collector",
                    instance_id="collector-a",
                    now=start + timedelta(seconds=120),
                )
            )
            self.assertTrue(
                manager.publisher_snapshot_due_for(
                    runtime_kind="collector",
                    instance_id="collector-a",
                    now=start + timedelta(seconds=120, microseconds=1),
                )
            )


if __name__ == "__main__":
    unittest.main()
