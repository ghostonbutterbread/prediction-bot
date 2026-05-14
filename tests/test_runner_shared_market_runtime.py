import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot.runner import PredictionBot
from bot.shared_market_runtime import SharedMarketRuntimeManager, build_shared_market_snapshot_metadata


class FakeExchange:
    name = "kalshi"

    def __init__(self, markets=None):
        self.markets = list(markets or [])
        self.market_calls = 0
        self.order_book_calls = 0
        self.last_limit = None

    def get_markets(self, limit=30):
        self.market_calls += 1
        self.last_limit = limit
        return list(self.markets)

    def get_order_book(self, market_id):
        self.order_book_calls += 1
        return {"best_yes_ask": 0.40, "best_no_ask": 0.60}


def fake_market(market_id="KXHIGHNY-26APR29-T80"):
    return SimpleNamespace(
        id=market_id,
        question="Will NYC high temperature be above 80 degrees?",
        yes_price=0.4,
        no_price=0.6,
        metadata={"market_group": "weather", "market_family": "daily_temperature"},
    )


def fake_signal(market_id="KXHIGHNY-26APR29-T80"):
    return {
        "market_id": market_id,
        "direction": "BUY_YES",
        "market_price": 0.40,
        "model_probability": 0.70,
        "edge": 0.30,
        "confidence": 0.90,
        "category": "KXHIGHNY",
    }


def runner_config(tmpdir, runtime_root, *, enabled=True, instance_id="live-test", max_age=30):
    return {
        "log_dir": tmpdir,
        "data_dir": tmpdir,
        "trading_enabled": False,
        "trading": {
            "mode": "live",
            "enabled": False,
            "shared_market_runtime_enabled": enabled,
            "shared_market_runtime_instance_id": instance_id,
        },
        "scan": {"markets_per_exchange": 5, "allowed_market_routes": ["weather.daily_temperature"]},
        "strategy": {
            "min_edge": 0.05,
            "min_confidence": 0.5,
            "enable_news": False,
            "enable_social": False,
            "enable_ai": False,
        },
        "shared_market": {
            "enabled": True,
            "runtime_root": str(runtime_root),
            "snapshot_ttl_seconds": 300,
            "default_interval_seconds": 60,
            "min_interval_seconds": 1,
            "publisher_lease_timeout_seconds": 300,
            "consumer_timeout_seconds": 300,
            "live": {
                "allow_direct_bypass": True,
                "max_snapshot_age_seconds": max_age,
            },
        },
    }


class RunnerSharedMarketRuntimeTests(unittest.TestCase):
    def _make_bot(self, tmpdir, runtime_root, *, enabled=True, instance_id="live-test", markets=None):
        bot = PredictionBot(runner_config(tmpdir, runtime_root, enabled=enabled, instance_id=instance_id))
        bot.exchanges["kalshi"] = FakeExchange(markets if markets is not None else [fake_market()])
        return bot

    def _scan_with_captured_signals(self, bot):
        captured = []

        def capture(signal):
            captured.append(dict(signal))
            return {}

        with patch.object(bot.strategy, "analyze_market", return_value=fake_signal()), patch.object(
            bot,
            "_process_signal",
            side_effect=capture,
        ):
            result = bot.scan_once()
        return result, captured

    def _attach_collector(self, tmpdir, runtime_root, *, snapshot=None):
        now = datetime.now(timezone.utc)
        manager = SharedMarketRuntimeManager(
            runtime_root=runtime_root,
            config=runner_config(tmpdir, runtime_root),
        )
        manager.attach(
            runtime_kind="collector",
            instance_id="collector-owner",
            can_publish=True,
            can_consume=True,
            desired_interval_seconds=60,
            now=now,
        )
        if snapshot is not None:
            manager.record_snapshot_metadata(snapshot, now=now)
        return manager

    def test_default_off_keeps_legacy_live_direct_scan_without_shared_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / "shared-runtime"
            bot = self._make_bot(tmpdir, runtime_root, enabled=False)

            result, captured = self._scan_with_captured_signals(bot)

            self.assertEqual(bot.exchanges["kalshi"].market_calls, 1)
            self.assertEqual(result["markets_scanned"], 1)
            self.assertNotIn("shared_market", result)
            self.assertNotIn("shared_market", captured[0])
            self.assertFalse((runtime_root / "runtime_state.json").exists())
            self.assertFalse((runtime_root / "latest_snapshot.json").exists())

    def test_live_enabled_without_owner_publishes_as_direct_publisher_and_detaches(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / "shared-runtime"
            bot = self._make_bot(tmpdir, runtime_root, enabled=True, instance_id="live-publisher")

            result, captured = self._scan_with_captured_signals(bot)

            latest = json.loads((runtime_root / "latest_snapshot.json").read_text(encoding="utf-8"))
            state = json.loads((runtime_root / "runtime_state.json").read_text(encoding="utf-8"))
            self.assertEqual(bot.exchanges["kalshi"].market_calls, 1)
            self.assertEqual(latest["publisher_runtime"], "live")
            self.assertEqual(latest["publisher_instance_id"], "live-publisher")
            self.assertEqual(latest["candidate_count"], 1)
            self.assertEqual(state["latest_snapshot"], latest)
            self.assertIsNone(state["publisher"])
            self.assertEqual(state["consumers"], {})
            self.assertEqual(result["shared_market"]["source"], "direct_publisher")
            self.assertEqual(result["shared_market"]["shared_snapshot_id"], latest["snapshot_id"])
            self.assertEqual(captured[0]["shared_market"]["source"], "direct_publisher")
            self.assertEqual(captured[0]["shared_market"]["shared_feed_instance_id"], "live-publisher")

    def test_live_begin_detaches_if_setup_fails_after_attach(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / "shared-runtime"
            bot = self._make_bot(tmpdir, runtime_root, enabled=True, instance_id="live-failing")

            with patch.object(
                SharedMarketRuntimeManager,
                "acquire_publisher_lease",
                side_effect=RuntimeError("lease failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "lease failed"):
                    bot.scan_once()

            state = json.loads((runtime_root / "runtime_state.json").read_text(encoding="utf-8"))
            self.assertIsNone(state["publisher"])
            self.assertNotIn("live:live-failing", state["consumers"])

    def test_live_enabled_with_fresh_collector_snapshot_uses_reference_but_fetches_directly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / "shared-runtime"
            now = datetime.now(timezone.utc)
            snapshot = build_shared_market_snapshot_metadata(
                snapshot_id="collector-fresh",
                observed_at=now,
                published_at=now,
                publisher_runtime="collector",
                publisher_instance_id="collector-owner",
                candidate_count=3,
                market_count=3,
                ttl_seconds=300,
                source_exchange="kalshi",
            )
            self._attach_collector(tmpdir, runtime_root, snapshot=snapshot)
            bot = self._make_bot(tmpdir, runtime_root, enabled=True, instance_id="live-consumer")

            result, captured = self._scan_with_captured_signals(bot)

            latest = json.loads((runtime_root / "latest_snapshot.json").read_text(encoding="utf-8"))
            state = json.loads((runtime_root / "runtime_state.json").read_text(encoding="utf-8"))
            self.assertEqual(bot.exchanges["kalshi"].market_calls, 1)
            self.assertEqual(bot.exchanges["kalshi"].order_book_calls, 1)
            self.assertEqual(latest["snapshot_id"], "collector-fresh")
            self.assertEqual(state["publisher"]["runtime_kind"], "collector")
            self.assertNotIn("live:live-consumer", state["consumers"])
            self.assertEqual(result["shared_market"]["source"], "shared_reference")
            self.assertTrue(result["shared_market"]["fresh_shared_snapshot"])
            self.assertTrue(result["shared_market"]["direct_fetch_required"])
            self.assertEqual(captured[0]["shared_market"]["shared_snapshot_id"], "collector-fresh")

    def test_live_enabled_with_stale_snapshot_under_other_owner_records_stale_bypass(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / "shared-runtime"
            now = datetime.now(timezone.utc)
            snapshot = build_shared_market_snapshot_metadata(
                snapshot_id="collector-stale",
                observed_at=now - timedelta(seconds=120),
                published_at=now - timedelta(seconds=120),
                publisher_runtime="collector",
                publisher_instance_id="collector-owner",
                candidate_count=3,
                market_count=3,
                ttl_seconds=300,
                source_exchange="kalshi",
            )
            self._attach_collector(tmpdir, runtime_root, snapshot=snapshot)
            bot = self._make_bot(tmpdir, runtime_root, enabled=True, instance_id="live-bypass")

            result, captured = self._scan_with_captured_signals(bot)

            latest = json.loads((runtime_root / "latest_snapshot.json").read_text(encoding="utf-8"))
            self.assertEqual(bot.exchanges["kalshi"].market_calls, 1)
            self.assertEqual(latest["snapshot_id"], "collector-stale")
            self.assertEqual(latest["publisher_runtime"], "collector")
            self.assertEqual(result["shared_market"]["source"], "direct_bypass_stale")
            self.assertEqual(result["shared_market"]["freshness_status"], "stale")
            self.assertEqual(captured[0]["shared_market"]["source"], "direct_bypass_stale")

    def test_live_enabled_with_missing_snapshot_under_other_owner_records_missing_bypass(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / "shared-runtime"
            self._attach_collector(tmpdir, runtime_root)
            bot = self._make_bot(tmpdir, runtime_root, enabled=True, instance_id="live-bypass")

            result, captured = self._scan_with_captured_signals(bot)

            state = json.loads((runtime_root / "runtime_state.json").read_text(encoding="utf-8"))
            self.assertEqual(bot.exchanges["kalshi"].market_calls, 1)
            self.assertEqual(state["publisher"]["runtime_kind"], "collector")
            self.assertFalse((runtime_root / "latest_snapshot.json").exists())
            self.assertEqual(result["shared_market"]["source"], "direct_bypass_missing")
            self.assertIsNone(result["shared_market"]["shared_snapshot_id"])
            self.assertEqual(captured[0]["shared_market"]["source"], "direct_bypass_missing")

    def test_live_enabled_with_mismatched_snapshot_under_other_owner_records_mismatch_bypass(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / "shared-runtime"
            now = datetime.now(timezone.utc)
            collector_snapshot = build_shared_market_snapshot_metadata(
                snapshot_id="collector-fresh",
                observed_at=now,
                published_at=now,
                publisher_runtime="collector",
                publisher_instance_id="collector-owner",
                candidate_count=3,
                market_count=3,
                ttl_seconds=300,
                source_exchange="kalshi",
            )
            self._attach_collector(tmpdir, runtime_root, snapshot=collector_snapshot)
            mismatched_snapshot = build_shared_market_snapshot_metadata(
                snapshot_id="paper-mismatched",
                observed_at=now,
                published_at=now,
                publisher_runtime="paper",
                publisher_instance_id="paper-owner",
                candidate_count=3,
                market_count=3,
                ttl_seconds=300,
                source_exchange="kalshi",
            )
            state_path = runtime_root / "runtime_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["latest_snapshot"] = mismatched_snapshot
            state_path.write_text(json.dumps(state), encoding="utf-8")
            (runtime_root / "latest_snapshot.json").write_text(json.dumps(mismatched_snapshot), encoding="utf-8")
            bot = self._make_bot(tmpdir, runtime_root, enabled=True, instance_id="live-bypass")

            result, captured = self._scan_with_captured_signals(bot)

            latest = json.loads((runtime_root / "latest_snapshot.json").read_text(encoding="utf-8"))
            self.assertEqual(bot.exchanges["kalshi"].market_calls, 1)
            self.assertEqual(latest["snapshot_id"], "paper-mismatched")
            self.assertEqual(latest["publisher_runtime"], "paper")
            self.assertEqual(result["shared_market"]["source"], "direct_bypass_mismatched")
            self.assertEqual(result["shared_market"]["shared_feed_instance_id"], "paper-owner")
            self.assertEqual(captured[0]["shared_market"]["source"], "direct_bypass_mismatched")

    def test_public_publisher_snapshot_due_helper_is_not_reintroduced(self):
        source = Path("bot/shared_market_runtime.py").read_text(encoding="utf-8")

        self.assertNotIn("def publisher_snapshot_due(", source)
        self.assertIn("def publisher_snapshot_due_for(", source)


if __name__ == "__main__":
    unittest.main()
