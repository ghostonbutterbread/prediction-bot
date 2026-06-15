import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot.file_ops import append_jsonl, load_jsonl
from bot.shared_market_runtime import SharedMarketRuntimeManager, build_shared_market_snapshot_metadata
from bot.simulator import Simulator


class FakeExchange:
    name = "kalshi"

    def __init__(self, markets=None):
        self.calls = 0
        self.markets = list(markets or [])
        self.last_limit = None

    def get_markets(self, limit=100):
        self.calls += 1
        self.last_limit = limit
        return list(self.markets)


def fake_market(market_id="KXHIGHNY-26APR29-T80"):
    return SimpleNamespace(
        id=market_id,
        question="Will NYC high temperature be above 80 degrees?",
        yes_price=0.4,
        no_price=0.6,
        closes_at=None,
        metadata={"series_ticker": "KXHIGHNY", "event_ticker": "KXHIGHNY-26APR29"},
    )


def simulator_config(
    tmpdir,
    runtime_root,
    *,
    enabled=True,
    instance_id="paper-test",
):
    return {
        "data_dir": tmpdir,
        "enable_social": False,
        "paper": {
            "shared_market_runtime_enabled": enabled,
            "shared_market_runtime_instance_id": instance_id,
            "shared_market_max_snapshot_age_seconds": 300,
            "shared_market_desired_interval_seconds": 60,
        },
        "shared_market": {
            "enabled": True,
            "runtime_root": str(runtime_root),
            "snapshot_ttl_seconds": 300,
            "default_interval_seconds": 60,
            "min_interval_seconds": 1,
            "publisher_lease_timeout_seconds": 300,
            "consumer_timeout_seconds": 300,
        },
        "strategy": {
            "enable_news": False,
            "enable_social": False,
            "enable_ai": False,
        },
    }


class SimulatorSharedMarketRuntimeTests(unittest.TestCase):
    def test_default_off_keeps_legacy_direct_fetch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / "shared-runtime"
            sim = Simulator(simulator_config(tmpdir, runtime_root, enabled=False))
            exchange = FakeExchange([fake_market()])

            with patch.object(sim.strategy, "analyze_market", return_value=None):
                result = sim.scan(exchange)

            self.assertEqual(exchange.calls, 1)
            self.assertEqual(result["markets"], 1)
            self.assertNotIn("shared_market", result)
            self.assertFalse((runtime_root / "runtime_state.json").exists())

    def test_shared_enabled_paper_publishes_when_no_owner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / "shared-runtime"
            sim = Simulator(simulator_config(tmpdir, runtime_root, enabled=True, instance_id="paper-publisher"))
            exchange = FakeExchange([fake_market()])

            with patch.object(sim.strategy, "analyze_market", return_value=None):
                result = sim.scan(exchange)

            latest = json.loads((runtime_root / "latest_snapshot.json").read_text(encoding="utf-8"))
            state = json.loads((runtime_root / "runtime_state.json").read_text(encoding="utf-8"))
            self.assertEqual(exchange.calls, 1)
            self.assertEqual(latest["publisher_runtime"], "paper")
            self.assertEqual(latest["publisher_instance_id"], "paper-publisher")
            self.assertEqual(latest["candidate_count"], 1)
            self.assertEqual(latest["market_count"], 1)
            self.assertEqual(latest["source_exchange"], "kalshi")
            self.assertEqual(state["latest_snapshot"], latest)
            self.assertIsNone(state["publisher"])
            self.assertEqual(state["consumers"], {})
            self.assertEqual(result["shared_market"]["source"], "direct_publisher")
            self.assertEqual(result["shared_market"]["snapshot_id"], latest["snapshot_id"])

    def test_shared_enabled_paper_skips_direct_fetch_for_fresh_collector_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / "shared-runtime"
            now = datetime.now(timezone.utc)
            manager = SharedMarketRuntimeManager(
                runtime_root=runtime_root,
                config=simulator_config(tmpdir, runtime_root),
            )
            manager.attach(
                runtime_kind="collector",
                instance_id="collector-owner",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=60,
                now=now,
            )
            manager.record_snapshot_metadata(
                build_shared_market_snapshot_metadata(
                    snapshot_id="collector-snapshot",
                    observed_at=now,
                    published_at=now,
                    publisher_runtime="collector",
                    publisher_instance_id="collector-owner",
                    candidate_count=3,
                    market_count=3,
                    ttl_seconds=300,
                    source_exchange="kalshi",
                ),
                now=now,
            )
            sim = Simulator(simulator_config(tmpdir, runtime_root, enabled=True, instance_id="paper-consumer"))
            exchange = FakeExchange([fake_market()])

            result = sim.scan(exchange)

            state = json.loads((runtime_root / "runtime_state.json").read_text(encoding="utf-8"))
            self.assertEqual(exchange.calls, 0)
            self.assertEqual(result["markets"], 3)
            self.assertEqual(result["trades"], 0)
            self.assertEqual(result["shared_market"]["source"], "shared")
            self.assertEqual(result["shared_market"]["snapshot_id"], "collector-snapshot")
            self.assertEqual(result["shared_market"]["publisher_runtime"], "collector")
            self.assertEqual(state["publisher"]["runtime_kind"], "collector")
            self.assertIn("collector:collector-owner", state["consumers"])
            self.assertNotIn("paper:paper-consumer", state["consumers"])

    def test_shared_skip_path_emits_shadow_lanes_from_collector_market_snapshots(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / "shared-runtime"
            decision_path = Path(tmpdir) / "paper_shadow_lane_decisions.jsonl"
            scoreboard_path = Path(tmpdir) / "source_scoreboard_by_slice.jsonl"
            now = datetime.now(timezone.utc)
            manager = SharedMarketRuntimeManager(
                runtime_root=runtime_root,
                config=simulator_config(tmpdir, runtime_root),
            )
            manager.attach(
                runtime_kind="collector",
                instance_id="collector-owner",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=60,
                now=now,
            )
            manager.record_snapshot_metadata(
                build_shared_market_snapshot_metadata(
                    snapshot_id="collector-snapshot",
                    observed_at=now,
                    published_at=now,
                    publisher_runtime="collector",
                    publisher_instance_id="collector-owner",
                    candidate_count=1,
                    market_count=1,
                    ttl_seconds=300,
                    source_exchange="kalshi",
                ),
                now=now,
            )
            config = simulator_config(tmpdir, runtime_root, enabled=True, instance_id="paper-consumer")
            config["paper_shadow_lanes"] = {
                "enabled": True,
                "enabled_lanes": ["shadow_source_scoreboard"],
                "source_scoreboard_path": str(scoreboard_path),
                "decision_ledger_path": str(decision_path),
            }
            append_jsonl(
                scoreboard_path,
                {
                    "source_id": "nws",
                    "source_name": "nws",
                    "city_id": "nyc_ny",
                    "market_kind": "high",
                    "contract_shape": "tail",
                    "sample_count": 20,
                    "threshold_direction_accuracy": 0.8,
                },
            )
            append_jsonl(
                Path(tmpdir) / "paper" / "prediction_lab" / "market_snapshots.jsonl",
                {
                    "timestamp": now.isoformat(),
                    "observed_at": now.isoformat(),
                    "run_id": "collector-snapshot",
                    "market_id": "KXHIGHNY-26APR29-T80",
                    "question": "Will NYC high temperature be above 80 degrees?",
                    "series": "KXHIGHNY",
                    "yes_price": 0.4,
                    "no_price": 0.6,
                    "confidence": 0.7,
                    "edge": 0.2,
                    "direction": "BUY_YES",
                    "normal_decision": {
                        "action": "BUY_YES",
                        "size": 5.0,
                        "reason_code": "collector_stable_buy",
                    },
                    "shared_candidate": {
                        "schema_name": "shared_market_candidate",
                        "schema_version": 1,
                        "candidate_id": "prediction_lab:KXHIGHNY-26APR29-T80:test",
                        "run_id": "collector-snapshot",
                        "market_id": "KXHIGHNY-26APR29-T80",
                        "observed_at": now.isoformat(),
                        "snapshot_as_of": now.isoformat(),
                        "source_runtime": "prediction_lab",
                        "provenance": "live_known_at_time",
                        "market": {
                            "id": "KXHIGHNY-26APR29-T80",
                            "exchange": "kalshi",
                            "question": "Will NYC high temperature be above 80 degrees?",
                            "category": "weather",
                            "series": "KXHIGHNY",
                        },
                        "prices": {
                            "yes_price": 0.4,
                            "no_price": 0.6,
                            "yes_market_price": 0.4,
                            "no_market_price": 0.6,
                            "best_yes_ask": 0.4,
                            "best_no_ask": 0.6,
                        },
                        "decision": {
                            "direction": "BUY_YES",
                            "confidence": 0.7,
                            "edge": 0.2,
                            "model_probability": 0.6,
                        },
                        "normal_decision": {
                            "action": "BUY_YES",
                            "size": 5.0,
                            "reason_code": "collector_stable_buy",
                        },
                    },
                    "shared_candidate_id": "prediction_lab:KXHIGHNY-26APR29-T80:test",
                },
            )
            sim = Simulator(config)
            exchange = FakeExchange([fake_market()])

            result = sim.scan(exchange)
            second_result = sim.scan(exchange)

            lane_rows = load_jsonl(decision_path)

            self.assertEqual(exchange.calls, 0)
            self.assertEqual(result["markets"], 1)
            self.assertEqual(result["shadow_lane_emission"]["status"], "emitted")
            self.assertEqual(result["shadow_lane_emission"]["accepted_candidate_count"], 1)
            self.assertEqual(second_result["shadow_lane_emission"]["reason_code"], "snapshot_already_emitted")
            self.assertEqual(len(lane_rows), 1)
            self.assertEqual(lane_rows[0]["policy"], "shadow_source_scoreboard")
            self.assertEqual(lane_rows[0]["action"], "BUY_YES")
            self.assertEqual(lane_rows[0]["approved_position_size_usd"], 5.0)
            self.assertIn("source_scoreboard_inputs", lane_rows[0]["candidate_dataset_path"])

    def test_stale_shared_snapshot_falls_back_to_direct_without_republishing_when_not_owner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / "shared-runtime"
            now = datetime.now(timezone.utc)
            manager = SharedMarketRuntimeManager(
                runtime_root=runtime_root,
                config=simulator_config(tmpdir, runtime_root),
            )
            manager.attach(
                runtime_kind="collector",
                instance_id="collector-owner",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=60,
                now=now,
            )
            manager.record_snapshot_metadata(
                build_shared_market_snapshot_metadata(
                    snapshot_id="stale-collector-snapshot",
                    observed_at=now - timedelta(seconds=600),
                    published_at=now - timedelta(seconds=600),
                    publisher_runtime="collector",
                    publisher_instance_id="collector-owner",
                    candidate_count=5,
                    market_count=5,
                    ttl_seconds=1,
                    source_exchange="kalshi",
                ),
                now=now,
            )
            sim = Simulator(simulator_config(tmpdir, runtime_root, enabled=True, instance_id="paper-fallback"))
            exchange = FakeExchange([fake_market("KXHIGHNY-26APR29-T81")])

            with patch.object(sim.strategy, "analyze_market", return_value=None):
                result = sim.scan(exchange)

            latest = json.loads((runtime_root / "latest_snapshot.json").read_text(encoding="utf-8"))
            self.assertEqual(exchange.calls, 1)
            self.assertEqual(result["markets"], 1)
            self.assertEqual(result["shared_market"]["source"], "direct_bypass")
            self.assertEqual(result["shared_market"]["snapshot_id"], "stale-collector-snapshot")
            self.assertEqual(latest["snapshot_id"], "stale-collector-snapshot")
            self.assertEqual(latest["publisher_runtime"], "collector")

    def test_missing_shared_snapshot_falls_back_to_direct_when_other_publisher_owns_feed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / "shared-runtime"
            now = datetime.now(timezone.utc)
            manager = SharedMarketRuntimeManager(
                runtime_root=runtime_root,
                config=simulator_config(tmpdir, runtime_root),
            )
            manager.attach(
                runtime_kind="collector",
                instance_id="collector-owner",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=60,
                now=now,
            )
            sim = Simulator(simulator_config(tmpdir, runtime_root, enabled=True, instance_id="paper-fallback"))
            exchange = FakeExchange([fake_market("KXHIGHNY-26APR29-T82")])

            with patch.object(sim.strategy, "analyze_market", return_value=None):
                result = sim.scan(exchange)

            self.assertEqual(exchange.calls, 1)
            self.assertEqual(result["markets"], 1)
            self.assertEqual(result["shared_market"]["source"], "direct_bypass")
            self.assertIsNone(result["shared_market"]["snapshot_id"])
            self.assertFalse((runtime_root / "latest_snapshot.json").exists())

    def test_mismatched_shared_snapshot_falls_back_to_direct_without_republishing_when_not_owner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / "shared-runtime"
            now = datetime.now(timezone.utc)
            manager = SharedMarketRuntimeManager(
                runtime_root=runtime_root,
                config=simulator_config(tmpdir, runtime_root),
            )
            manager.attach(
                runtime_kind="collector",
                instance_id="collector-owner",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=60,
                now=now,
            )
            manager.record_snapshot_metadata(
                build_shared_market_snapshot_metadata(
                    snapshot_id="collector-snapshot",
                    observed_at=now,
                    published_at=now,
                    publisher_runtime="collector",
                    publisher_instance_id="collector-owner",
                    candidate_count=5,
                    market_count=5,
                    ttl_seconds=300,
                    source_exchange="kalshi",
                ),
                now=now,
            )
            mismatched_snapshot = build_shared_market_snapshot_metadata(
                snapshot_id="old-paper-snapshot",
                observed_at=now,
                published_at=now,
                publisher_runtime="paper",
                publisher_instance_id="old-paper-owner",
                candidate_count=5,
                market_count=5,
                ttl_seconds=300,
                source_exchange="kalshi",
            )
            state_path = runtime_root / "runtime_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["latest_snapshot"] = mismatched_snapshot
            state_path.write_text(json.dumps(state), encoding="utf-8")
            (runtime_root / "latest_snapshot.json").write_text(json.dumps(mismatched_snapshot), encoding="utf-8")
            sim = Simulator(simulator_config(tmpdir, runtime_root, enabled=True, instance_id="paper-fallback"))
            exchange = FakeExchange([fake_market("KXHIGHNY-26APR29-T83")])

            with patch.object(sim.strategy, "analyze_market", return_value=None):
                result = sim.scan(exchange)

            latest = json.loads((runtime_root / "latest_snapshot.json").read_text(encoding="utf-8"))
            self.assertEqual(exchange.calls, 1)
            self.assertEqual(result["markets"], 1)
            self.assertEqual(result["shared_market"]["source"], "direct_bypass")
            self.assertEqual(result["shared_market"]["snapshot_id"], "old-paper-snapshot")
            self.assertEqual(latest["snapshot_id"], "old-paper-snapshot")
            self.assertEqual(latest["publisher_runtime"], "paper")
            self.assertEqual(latest["publisher_instance_id"], "old-paper-owner")

    def test_public_publisher_snapshot_due_helper_is_not_reintroduced(self):
        source = Path("bot/shared_market_runtime.py").read_text(encoding="utf-8")

        self.assertNotIn("def publisher_snapshot_due(", source)
        self.assertIn("def publisher_snapshot_due_for(", source)


if __name__ == "__main__":
    unittest.main()
