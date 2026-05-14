import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot.file_ops import append_jsonl, load_jsonl
from bot.paper_wallet_runner import run_shared_candidate_paper_evaluation
from bot.prediction_lab import PredictionLab
from bot.runner import PredictionBot
from bot.shared_market_feed import normalize_shared_candidate_input
from bot.shared_market_runtime import (
    SharedMarketRuntimeManager,
    build_shared_market_snapshot_metadata,
)
from bot.simulator import Simulator


class FakeExchange:
    name = "kalshi"

    def __init__(self, markets=None):
        self.markets = list(markets or [])
        self.market_calls = 0
        self.order_book_calls = 0
        self.closed = False

    def get_markets(self, limit=50, category=None):
        self.market_calls += 1
        return list(self.markets[:limit])

    def get_order_book(self, market_id):
        self.order_book_calls += 1
        return {
            "best_yes_bid": 0.39,
            "best_yes_ask": 0.41,
            "best_no_bid": 0.57,
            "best_no_ask": 0.59,
        }

    def close(self):
        self.closed = True


def fake_market(
    market_id="KXHIGHNY-260514-T71",
    *,
    question="Will the high temperature in New York exceed 71 degrees on May 14?",
    yes_price=0.41,
    no_price=0.59,
):
    return SimpleNamespace(
        id=market_id,
        exchange="kalshi",
        question=question,
        category="weather",
        yes_price=yes_price,
        no_price=no_price,
        volume=1200,
        liquidity=1200,
        closes_at=None,
        metadata={
            "market_group": "weather",
            "market_family": "daily_temperature",
            "series": "daily_temperature",
            "series_ticker": "KXHIGHNY",
            "event_ticker": f"{market_id}-EVENT",
            "market_route": {
                "allowed": True,
                "group": "weather",
                "family": "daily_temperature",
            },
        },
    )


def fake_signal(
    market_id="KXHIGHNY-260514-T71",
    *,
    direction="BUY_YES",
    model_probability=0.68,
    market_price=0.41,
    yes_price=0.41,
    no_price=0.59,
):
    return {
        "market_id": market_id,
        "direction": direction,
        "model_probability": model_probability,
        "market_price": market_price,
        "yes_market_price": yes_price,
        "no_market_price": no_price,
        "edge": 0.24,
        "confidence": 0.92,
        "station_id": "KNYC",
        "source_as_of": "2026-05-14T12:00:00+00:00",
        "signals": {"unit": 0.68},
        "category": "weather",
    }


def shared_runtime_config(tmpdir, runtime_root, *, paper_enabled=True, live_enabled=True):
    return {
        "runtime": {"base_dir": str(Path(tmpdir) / "wallet_data"), "mode": "paper"},
        "data_dir": str(Path(tmpdir) / "wallet_data" / "paper"),
        "log_dir": str(Path(tmpdir) / "wallet_data" / "paper"),
        "trading_enabled": False,
        "trading": {
            "mode": "live",
            "enabled": False,
            "shared_market_runtime_enabled": live_enabled,
            "shared_market_runtime_instance_id": "live-e2e",
        },
        "paper": {
            "shared_market_runtime_enabled": paper_enabled,
            "shared_market_runtime_instance_id": "paper-e2e",
            "shared_market_max_snapshot_age_seconds": 60,
            "shared_market_desired_interval_seconds": 30,
        },
        "scan": {
            "markets_per_exchange": 5,
            "allowed_market_routes": ["weather.daily_temperature"],
        },
        "prediction_lab": {
            "enabled": True,
            "mode": "collector",
            "groups": ["weather"],
            "continue_collecting": True,
            "collector_interval_seconds": 30,
            "collector_record_market_snapshots": True,
            "collector_record_predictions": False,
            "score_only": True,
            "shared_market_runtime_enabled": True,
            "shared_market_runtime_instance_id": "collector-e2e",
        },
        "strategy": {
            "min_edge": 0.01,
            "min_confidence": 0.01,
            "enable_news": False,
            "enable_social": False,
            "enable_ai": False,
        },
        "shared_market": {
            "enabled": True,
            "runtime_root": str(runtime_root),
            "snapshot_ttl_seconds": 300,
            "default_interval_seconds": 30,
            "min_interval_seconds": 1,
            "publisher_lease_timeout_seconds": 300,
            "consumer_timeout_seconds": 300,
            "live": {
                "enabled": live_enabled,
                "allow_direct_bypass": True,
                "max_snapshot_age_seconds": 30,
                "desired_interval_seconds": 15,
            },
        },
    }


def paper_runtime_config(config):
    materialized = copy.deepcopy(config)
    trading = dict(materialized.get("trading", {}) or {})
    trading["mode"] = "paper"
    materialized["trading"] = trading
    runtime = dict(materialized.get("runtime", {}) or {})
    runtime["mode"] = "paper"
    materialized["runtime"] = runtime
    return materialized


def wallet_config(tmpdir):
    return {
        "runtime": {"base_dir": str(Path(tmpdir) / "wallet_data"), "mode": "paper"},
        "data_dir": str(Path(tmpdir) / "wallet_data" / "paper"),
        "log_dir": str(Path(tmpdir) / "wallet_data" / "paper"),
        "trading": {"mode": "paper"},
        "strategy_policy": {
            "version": "beta",
            "beta": {
                "mode": "shadow",
                "features": {
                    "weather_hidden_gem_evidence_card": True,
                    "bucket_distribution_scoring": True,
                    "hidden_gem_lane_gates": True,
                    "lane_sizing_caps": True,
                },
            },
        },
        "strategy": {
            "min_edge": 0.01,
            "min_confidence": 0.01,
            "enable_news": False,
            "enable_social": False,
            "enable_ai": False,
        },
    }


def collector_snapshot_row(tmpdir, market, signal, *, run_id, observed_at):
    lab = PredictionLab(
        {
            "data_dir": str(Path(tmpdir) / "collector_data"),
            "prediction_lab": {
                "enabled": True,
                "mode": "collector",
                "groups": ["weather"],
                "collector_interval_seconds": 30,
            },
            "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
            "strategy": {
                "enable_news": False,
                "enable_social": False,
                "enable_ai": False,
            },
        }
    )
    return lab._build_market_snapshot_row(
        run_id,
        market,
        signal,
        decision_type="buy_yes" if signal["direction"] == "BUY_YES" else "buy_no",
        prediction_recorded=False,
        decision_artifact={
            "final_action": signal["direction"],
            "final_reason_code": "approved",
            "strategy_signal": dict(signal),
            "shared_core_decision": {
                "requested_position_size": 10.0,
                "position_size": 10.0,
                "reason_code": "approved",
                "confidence": signal["confidence"],
                "edge": signal["edge"],
                "win_probability": signal["model_probability"],
                "entry_price": signal["market_price"],
                "reasoning": {"strategy_lane": {"lane_id": "edge"}},
            },
        },
        observed_at=observed_at,
    )


def attach_collector_owner(manager, *, now, snapshot=None):
    manager.attach(
        runtime_kind="collector",
        instance_id="collector-e2e",
        can_publish=True,
        can_consume=True,
        desired_interval_seconds=30,
        now=now,
    )
    state = manager.acquire_publisher_lease(
        runtime_kind="collector",
        instance_id="collector-e2e",
        now=now,
    )
    if snapshot is not None:
        state = manager.record_snapshot_metadata(snapshot, now=now)
    return state


def run_live_scan(config, market):
    bot = PredictionBot(config)
    bot.exchanges["kalshi"] = FakeExchange([market])
    captured = []

    def capture(signal):
        captured.append(copy.deepcopy(signal))
        return {}

    with patch.object(bot.strategy, "analyze_market", return_value=fake_signal(market.id)), patch.object(
        bot,
        "_process_signal",
        side_effect=capture,
    ):
        result = bot.scan_once()
    return result, captured, bot.exchanges["kalshi"]


class SharedMarketEndToEndTests(unittest.TestCase):
    def test_runtime_lifecycle_and_fresh_snapshot_provenance_across_modules(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / "shared-runtime"
            config = shared_runtime_config(tmpdir, runtime_root)
            manager = SharedMarketRuntimeManager(runtime_root=runtime_root, config=config)
            now = datetime.now(timezone.utc)
            market = fake_market()
            snapshot = build_shared_market_snapshot_metadata(
                snapshot_id="collector-run-1",
                observed_at=now,
                published_at=now,
                publisher_runtime="collector",
                publisher_instance_id="collector-e2e",
                candidate_count=1,
                market_count=1,
                ttl_seconds=300,
                source_exchange="kalshi",
            )

            self.assertFalse((runtime_root / "runtime_state.json").exists())
            self.assertFalse((runtime_root / "latest_snapshot.json").exists())

            state = attach_collector_owner(manager, now=now, snapshot=snapshot)
            collector_row = collector_snapshot_row(
                tmpdir,
                market,
                fake_signal(market.id),
                run_id="collector-run-1",
                observed_at=snapshot["observed_at"],
            )
            collector_row["shared_market"] = {
                "source": "shared_reference",
                "shared_snapshot_id": snapshot["snapshot_id"],
                "shared_snapshot_observed_at": snapshot["observed_at"],
                "publisher_runtime": snapshot["publisher_runtime"],
                "publisher_instance_id": snapshot["publisher_instance_id"],
                "freshness_status": "fresh",
            }
            normalized = normalize_shared_candidate_input(collector_row)

            self.assertEqual(state["publisher"]["runtime_kind"], "collector")
            self.assertEqual(state["latest_snapshot"]["snapshot_id"], "collector-run-1")
            self.assertTrue(normalized.ok)
            self.assertTrue(normalized.reconstructable)
            self.assertEqual(normalized.shared_candidate_id, collector_row["shared_candidate_id"])
            self.assertEqual(normalized.provenance["shared_snapshot_id"], "collector-run-1")
            self.assertEqual(normalized.provenance["publisher_runtime"], "collector")
            self.assertTrue(normalized.provenance["read_only"])

            with self.assertRaisesRegex(ValueError, "does not match active publisher lease"):
                manager.record_snapshot_metadata(
                    build_shared_market_snapshot_metadata(
                        snapshot_id="paper-should-not-publish",
                        observed_at=now,
                        publisher_runtime="paper",
                        publisher_instance_id="paper-e2e",
                        candidate_count=1,
                        ttl_seconds=300,
                    ),
                    now=now,
                )

            paper = Simulator(paper_runtime_config(config))
            paper_exchange = FakeExchange([market])
            paper_result = paper.scan(paper_exchange)

            self.assertEqual(paper_exchange.market_calls, 0)
            self.assertEqual(paper_result["markets"], 1)
            self.assertEqual(paper_result["shared_market"]["source"], "shared")
            self.assertEqual(paper_result["shared_market"]["snapshot_id"], "collector-run-1")
            self.assertEqual(paper_result["shared_market"]["publisher_runtime"], "collector")

            live_result, live_signals, live_exchange = run_live_scan(config, market)

            self.assertEqual(live_exchange.market_calls, 1)
            self.assertEqual(live_exchange.order_book_calls, 1)
            self.assertEqual(live_result["shared_market"]["source"], "shared_reference")
            self.assertTrue(live_result["shared_market"]["direct_fetch_required"])
            self.assertEqual(live_result["shared_market"]["shared_snapshot_id"], "collector-run-1")
            self.assertEqual(live_signals[0]["shared_market"]["publisher_runtime"], "collector")
            self.assertEqual(live_signals[0]["shared_market"]["shared_snapshot_id"], "collector-run-1")

            after_consumers = manager.read_state(now=now + timedelta(seconds=1))
            self.assertEqual(after_consumers["publisher"]["runtime_kind"], "collector")
            self.assertNotIn("paper:paper-e2e", after_consumers["consumers"])
            self.assertNotIn("live:live-e2e", after_consumers["consumers"])
            self.assertEqual(after_consumers["latest_snapshot"]["snapshot_id"], "collector-run-1")

            idle = manager.detach(
                runtime_kind="collector",
                instance_id="collector-e2e",
                now=now + timedelta(seconds=2),
            )
            self.assertTrue(idle["idle"])
            self.assertEqual(idle["consumers"], {})
            self.assertIsNone(idle["publisher"])
            self.assertEqual(idle["latest_snapshot"]["snapshot_id"], "collector-run-1")

    def test_stale_missing_and_mismatched_snapshots_bypass_without_non_owner_writes(self):
        scenarios = ("missing", "stale", "mismatched")
        for scenario in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmpdir:
                runtime_root = Path(tmpdir) / "shared-runtime"
                config = shared_runtime_config(tmpdir, runtime_root)
                manager = SharedMarketRuntimeManager(runtime_root=runtime_root, config=config)
                now = datetime.now(timezone.utc)
                market = fake_market(f"KXHIGHNY-260514-T7{len(scenario)}")
                initial_snapshot = None
                if scenario in {"stale", "mismatched"}:
                    observed_at = now - timedelta(seconds=120) if scenario == "stale" else now
                    initial_snapshot = build_shared_market_snapshot_metadata(
                        snapshot_id=f"collector-{scenario}",
                        observed_at=observed_at,
                        published_at=observed_at,
                        publisher_runtime="collector",
                        publisher_instance_id="collector-e2e",
                        candidate_count=2,
                        market_count=2,
                        ttl_seconds=300,
                        source_exchange="kalshi",
                    )
                attach_collector_owner(manager, now=now, snapshot=initial_snapshot)
                if scenario == "mismatched":
                    mismatched = build_shared_market_snapshot_metadata(
                        snapshot_id="paper-stale-owner",
                        observed_at=now,
                        published_at=now,
                        publisher_runtime="paper",
                        publisher_instance_id="old-paper",
                        candidate_count=2,
                        market_count=2,
                        ttl_seconds=300,
                        source_exchange="kalshi",
                    )
                    state_path = runtime_root / "runtime_state.json"
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    state["latest_snapshot"] = mismatched
                    state_path.write_text(json.dumps(state), encoding="utf-8")
                    (runtime_root / "latest_snapshot.json").write_text(json.dumps(mismatched), encoding="utf-8")

                paper = Simulator(paper_runtime_config(config))
                paper_exchange = FakeExchange([market])
                with patch.object(paper.strategy, "analyze_market", return_value=None):
                    paper_result = paper.scan(paper_exchange)
                live_result, live_signals, live_exchange = run_live_scan(config, market)

                latest = (
                    json.loads((runtime_root / "latest_snapshot.json").read_text(encoding="utf-8"))
                    if (runtime_root / "latest_snapshot.json").exists()
                    else None
                )
                self.assertEqual(paper_exchange.market_calls, 1)
                self.assertEqual(live_exchange.market_calls, 1)
                self.assertEqual(paper_result["shared_market"]["source"], "direct_bypass")
                self.assertFalse(paper_result["shared_market"]["owns_publisher"])
                self.assertTrue(live_result["shared_market"]["direct_fetch_required"])
                self.assertEqual(live_signals[0]["shared_market"]["source"], live_result["shared_market"]["source"])
                if scenario == "missing":
                    self.assertIsNone(latest)
                    self.assertIsNone(paper_result["shared_market"]["snapshot_id"])
                    self.assertEqual(live_result["shared_market"]["source"], "direct_bypass_missing")
                elif scenario == "stale":
                    self.assertEqual(latest["snapshot_id"], "collector-stale")
                    self.assertEqual(latest["publisher_runtime"], "collector")
                    self.assertEqual(paper_result["shared_market"]["snapshot_id"], "collector-stale")
                    self.assertEqual(live_result["shared_market"]["source"], "direct_bypass_stale")
                else:
                    self.assertEqual(latest["snapshot_id"], "paper-stale-owner")
                    self.assertEqual(latest["publisher_runtime"], "paper")
                    self.assertEqual(paper_result["shared_market"]["snapshot_id"], "paper-stale-owner")
                    self.assertEqual(live_result["shared_market"]["source"], "direct_bypass_mismatched")

    def test_shared_candidate_dataset_normalization_and_dual_wallet_evaluation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = wallet_config(tmpdir)
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            yes_market = fake_market("KXHIGHNY-260514-T71", yes_price=0.41, no_price=0.59)
            no_market = fake_market(
                "KXHIGHNY-260514-T75",
                question="Will the high temperature in New York exceed 75 degrees on May 14?",
                yes_price=0.58,
                no_price=0.42,
            )
            yes_row = collector_snapshot_row(
                tmpdir,
                yes_market,
                fake_signal(yes_market.id, direction="BUY_YES", market_price=0.41, yes_price=0.41, no_price=0.59),
                run_id="collector-run-yes",
                observed_at="2026-05-14T12:00:01+00:00",
            )
            no_row = collector_snapshot_row(
                tmpdir,
                no_market,
                fake_signal(
                    no_market.id,
                    direction="BUY_NO",
                    model_probability=0.32,
                    market_price=0.42,
                    yes_price=0.58,
                    no_price=0.42,
                ),
                run_id="collector-run-no",
                observed_at="2026-05-14T12:00:02+00:00",
            )
            for row in (yes_row, no_row):
                normalized = normalize_shared_candidate_input(row)
                self.assertTrue(normalized.ok)
                self.assertTrue(normalized.reconstructable)
                self.assertEqual(normalized.shared_candidate_id, row["shared_candidate_id"])
                self.assertEqual(normalized.signal["direction"], row["direction"])
                append_jsonl(dataset_path, row)

            unsafe_row = copy.deepcopy(yes_row["shared_candidate"])
            unsafe_row["prices"].pop("yes_price", None)
            unsafe_row["prices"].pop("yes_market_price", None)
            unsafe = normalize_shared_candidate_input(unsafe_row)
            self.assertFalse(unsafe.ok)
            self.assertFalse(unsafe.reconstructable)
            self.assertEqual(unsafe.reason_code, "invalid_price_fields")

            with patch("bot.simulator.KellySizer.calculate", return_value=10.0):
                result = run_shared_candidate_paper_evaluation(dataset_path, config=config)

            stable = result.wallet_runs["stable_paper"]
            beta = result.wallet_runs["beta_paper"]
            stable_decisions = load_jsonl(Path(stable.agent_decision_path))
            beta_decisions = load_jsonl(Path(beta.agent_decision_path))
            stable_risk = json.loads(Path(stable.risk_state_path).read_text(encoding="utf-8"))
            beta_risk = json.loads(Path(beta.risk_state_path).read_text(encoding="utf-8"))
            expected_ids = (yes_row["shared_candidate_id"], no_row["shared_candidate_id"])

        self.assertEqual(result.loaded_row_count, 2)
        self.assertEqual(result.accepted_candidate_count, 2)
        self.assertEqual(result.shared_candidate_ids, expected_ids)
        self.assertEqual(stable.shared_candidate_ids, expected_ids)
        self.assertEqual(beta.shared_candidate_ids, expected_ids)
        self.assertEqual(len(stable.accepted_trade_ids), 2)
        self.assertEqual(len(beta.accepted_trade_ids), 2)
        self.assertNotEqual(stable.data_dir, beta.data_dir)
        self.assertNotEqual(stable.risk_state_path, beta.risk_state_path)
        self.assertEqual({row["shared_candidate_id"] for row in stable_decisions}, set(expected_ids))
        self.assertEqual({row["shared_candidate_id"] for row in beta_decisions}, set(expected_ids))
        self.assertEqual({row["decision_role"] for row in stable_decisions}, {"paper_shadow"})
        self.assertEqual({row["decision_role"] for row in beta_decisions}, {"paper_shadow"})
        self.assertEqual({row["runtime"] for row in stable_decisions + beta_decisions}, {"paper"})
        self.assertEqual({row["wallet_id"] for row in stable_decisions}, {"stable_paper"})
        self.assertEqual({row["wallet_id"] for row in beta_decisions}, {"beta_paper"})
        self.assertEqual({row["candidate_dataset_path"] for row in stable_decisions}, {str(dataset_path)})
        self.assertEqual({row["candidate_dataset_path"] for row in beta_decisions}, {str(dataset_path)})
        self.assertEqual(stable_risk["available_cash"], 80.0)
        self.assertEqual(beta_risk["available_cash"], 80.0)
        self.assertEqual(stable_risk["reserved_capital"], 20.0)
        self.assertEqual(beta_risk["reserved_capital"], 20.0)


if __name__ == "__main__":
    unittest.main()
