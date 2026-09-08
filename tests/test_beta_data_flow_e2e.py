"""Synthetic, no-network producer-to-paper Source Router regression.

No raw collector rows, replay records, resolutions or scorecards are fabricated
at an internal handoff. The resolver reads the collector daemon's compact index;
promotion reads that same index's immutable raw archive and the resolver output.
External boundaries: exchange market/book reads, the weather-feed adapter and
wall/elapsed clocks (including resolver rate-limit sleeps). All strategy,
collector, index, binding, source-proof,
collapse, publication, verification and paper-router logic remains real.

This is schema/chronology evidence, not historical P&L, weather skill, executable
paper-wallet parity, scheduler activation or a live-trading test. The synthetic
100-event cohort deliberately meets the production reliability minimum without
changing its thresholds; its long forecast horizon is not a realism claim.
"""

import hashlib
import json
import os
import sys
import tempfile
import unittest
from collections import Counter
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot.auto_source_router_promotion import auto_populate_source_router_history
from bot.collector_paths import COLLECTOR_ROOT_ENV, auto_source_router_history_root
from bot.collector_replay_index import load_indexed_collector_rows
from bot.file_ops import load_jsonl
from bot.paper_evaluator_input import load_shared_candidate_paper_inputs
from bot.paper_shadow_lanes import _LaneDefinition, _source_router_decision, write_paper_shadow_lane_decisions
from bot.prediction_lab import PredictionLab
from bot.prediction_lab_collect import PredictionLabCollectorDaemon
from bot.resolution_feed import run_resolution_feed_once
from bot.replay_decision_input import verify_replay_decision_input_record_v1


UTC = timezone.utc
COLLECTED_AT = datetime(2026, 6, 1, 12, tzinfo=UTC)
SETTLEMENT_AT = datetime(2026, 9, 15, 12, tzinfo=UTC)
RETRIEVED_AT = SETTLEMENT_AT + timedelta(days=2)


class _Clock(datetime):
    current = COLLECTED_AT

    @classmethod
    def now(cls, tz=None):
        value = cls.current
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


class _ExchangeBoundary:
    """Read-only external exchange fixture; no wallet/order methods exist."""

    def __init__(self, markets):
        self.markets = markets
        self.market_calls = 0
        self.book_calls = []

    def get_markets_direct(self, **kwargs):
        self.market_calls += 1
        return deepcopy(self.markets)

    def get_order_book(self, market_id):
        self.book_calls.append(market_id)
        return {
            "best_yes_ask": 0.42, "best_yes_bid": 0.40,
            "best_no_ask": 0.60, "best_no_bid": 0.58,
        }


class _WeatherBoundary:
    """Fake external weather adapter, NOT a fake collector/strategy artifact."""

    def __init__(self, markets, *, future_source_ids=()):
        self.markets = {market.id: market for market in markets}
        self.future_source_ids = set(future_source_ids)
        self.calls = []

    def score_temperature_market_with_context(self, question, yes_price, *, category):
        market = self.markets[category]
        self.calls.append(category)
        target = market.metadata["market_date"]
        threshold = market.metadata["threshold"]
        fetched = (_Clock.current - timedelta(minutes=1)).isoformat()
        source_as_of = (
            _Clock.current + timedelta(minutes=1)
            if category in self.future_source_ids
            else _Clock.current - timedelta(minutes=1)
        ).isoformat()
        source = {
            "source_id": "nws", "source_name": "nws",
            "source_location_city": "Seattle", "forecast_measurement_kind": "high",
            "contract_shape": "tail", "question_side": "above",
            "forecast_high": 75.0, "as_of": source_as_of, "fetched_at": fetched,
            "source_evidence_version": 1, "evidence_type": "forecast",
            "scoreable_forecast": True, "weather_date": target,
            "target_mapping": {
                "market_target_date": target, "source_target_date": target,
                "source_timezone": "America/Los_Angeles",
            },
        }
        probability = 0.90 if threshold == 70 else 0.10
        return {
            "signal_type": "weather", "predicted_prob": probability,
            "confidence": 0.95, "source_timestamp": fetched, "ttl_seconds": 600,
            "question_side": "above", "edge": abs(probability - yes_price),
            "data": {
                "forecast_high": 75.0, "actual_temp_used": 75.0,
                "predicted_temp": 75.0, "threshold": threshold, "city": "seattle",
                "sources": ["nws"], "source_details": [source],
                "agreement": 1.0, "settlement_source": "nws",
                "weather_date": target, "fetched_at": fetched, "as_of": fetched,
                "station_id": "KSEA", "station_cli": "SEA",
                "date_validation": {
                    "ok": True, "reason": "dates_match", "market_date": target,
                    "weather_date": target, "source": "synthetic_external_weather",
                },
            },
        }


def _market(target, threshold=70, *, iso_date=True):
    ticker = f"KXHIGHSEA-{target.strftime('%y%b%d').upper()}-T{threshold}"
    # An ISO target date is not a temperature range; retain the explicit tail
    # contract through the real consumer instead of enriching its signal here.
    displayed_date = target.isoformat() if iso_date else target.strftime("%B %d, %Y")
    return SimpleNamespace(
        id=ticker, exchange="kalshi", category="KXHIGHSEA",
        question=f"Will Seattle high temperature be above {threshold}° on {displayed_date}?",
        yes_price=0.42, no_price=0.58, volume=4500,
        closes_at=datetime.combine(target, datetime.min.time(), tzinfo=UTC) + timedelta(days=1),
        metadata={
            "event_ticker": ticker.rsplit("-", 1)[0], "market_date": target.isoformat(),
            "city": "Seattle", "city_id": "seattle_wa", "market_kind": "high",
            "contract_shape": "tail", "threshold": threshold, "question_side": "above",
            "market_group": "weather", "market_family": "daily_temperature",
            "series_ticker": "KXHIGHSEA", "series": "daily_temperature",
        },
    )


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class BetaDataFlowEndToEndTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="beta-data-flow-e2e-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.original_cwd = Path.cwd()
        self.addCleanup(os.chdir, self.original_cwd)
        self.cwd_a, self.cwd_b = self.root / "start-cwd", self.root / "restart-cwd"
        self.cwd_a.mkdir()
        self.cwd_b.mkdir()
        os.chdir(self.cwd_a)
        self.collector_root = self.root / "collector-volume"
        self.enterContext(patch.dict(os.environ, {
            COLLECTOR_ROOT_ENV: str(self.collector_root),
            "MODE": "paper", "SIMULATION": "true",
        }))
        _Clock.current = COLLECTED_AT
        # Freeze only external clocks, not internal producers or adapters.
        for name, module in list(sys.modules.items()):
            if name.startswith("bot.") and getattr(module, "datetime", None) is datetime:
                self.enterContext(patch.object(module, "datetime", _Clock))
        self.enterContext(patch("time.perf_counter", return_value=0.0))
        self.enterContext(patch("time.sleep", return_value=None))
        self.network_guards = [
            self.enterContext(patch(target, side_effect=AssertionError("E2E forbids network")))
            for target in (
                "requests.sessions.Session.request", "socket.create_connection",
                "socket.getaddrinfo", "socket.socket.connect", "socket.socket.connect_ex",
            )
        ]
        self.config = {
            "mode": "paper", "simulation": True, "trading_enabled": False,
            "data_dir": str(self.collector_root / "data" / "paper"),
            "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
            "prediction_lab": {
                "enabled": True, "mode": "collector", "observer_mode": True,
                "groups": ["weather"], "score_only": True,
                "record_all_scored": True, "collector_record_predictions": False,
                "collector_record_market_snapshots": True, "use_shared_pipeline": True,
                "collector_fetch_mode": "direct_markets", "send_telegram_updates": False,
                "replay_index": {"enabled": True},
            },
            "strategy": {
                "enable_news": False, "enable_social": False, "enable_ai": False,
                "enable_weather_observation_log": False,
                "min_edge": 0.01, "min_confidence": 0.5,
            },
            "max_entry_price": 0.7,
        }

    def tearDown(self):
        # A swallowed transport exception is still a test failure.
        for guard in self.network_guards:
            guard.assert_not_called()

    def _lab(self, config, weather):
        lab = PredictionLab(config)
        # Close unused HTTP clients before replacing only the weather adapter.
        lab.strategy.live_feeds.weather.close()
        lab.strategy.live_feeds.weather = weather
        self.addCleanup(lab.strategy.live_feeds.crypto.close)
        self.addCleanup(lab.strategy.live_feeds.forex.close)
        return lab

    def _assert_outcome_free(self, value):
        forbidden = {
            "official_outcome", "kalshi_result", "settlement_ts", "direction_correct",
            "resolution_id", "resolution", "resolved_at", "resolution_retrieved_at",
            "outcome", "known_after",
        }
        if isinstance(value, dict):
            self.assertFalse(forbidden.intersection(value), f"outcome leak: {forbidden.intersection(value)}")
            for child in value.values():
                self._assert_outcome_free(child)
        elif isinstance(value, list):
            for child in value:
                self._assert_outcome_free(child)

    def test_iso_calendar_date_is_not_a_temperature_range(self):
        from bot.weather.source_confidence import _threshold_range_from_question
        from bot.weather.source_reliability import build_reliability_candidate_row

        for text, side, shape, bounds in (
            ("above 70°", "above", "tail", (None, None)),
            ("below 80°", "below", "tail", (None, None)),
            ("70°", "binary_bucket", "bucket", (None, None)),
            ("between 70-75°", "range", "range", (70.0, 75.0)),
            ("between -10 to -5°", "range", "range", (-10.0, -5.0)),
        ):
            with self.subTest(text=text):
                question = f"On 2026-09-16, will Seattle high temperature be {text}?"
                row = build_reliability_candidate_row({"question": question})
                self.assertEqual(row["question_side"], side)
                self.assertEqual(row["contract_shape"], shape)
                self.assertEqual(_threshold_range_from_question(question), bounds)

    def test_actual_collector_index_resolver_promotion_and_paper_router(self):
        # 100 independent binary events, one VOID and one future-dated source.
        markets = [
            _market(date(2026, 6, 2) + timedelta(days=i), 70 if i % 2 == 0 else 80)
            for i in range(102)
        ]
        binary_ids = {market.id for market in markets[:100]}
        void_id, future_id = markets[100].id, markets[101].id
        outcomes = {market.id: "yes" if market.metadata["threshold"] == 70 else "no" for market in markets}
        outcomes[void_id] = "void"
        exchange = _ExchangeBoundary(markets)
        weather = _WeatherBoundary(markets, future_source_ids=[future_id])
        lab = self._lab(self.config, weather)
        first_run = lab.run(exchange)
        self.assertEqual(first_run.scanned_markets, 102)
        self.assertEqual(first_run.recorded_predictions, 0)
        archive = lab.market_snapshots_path
        self.assertTrue(archive.is_absolute())
        first_bytes = archive.read_bytes()
        first_index = PredictionLabCollectorDaemon._update_replay_index(self.config, lab)
        assert first_index is not None, "collector daemon did not publish its enabled replay index"
        self.assertEqual(first_index["indexed_rows"], 102)

        # A fresh collector object starts from the same durable root in a new CWD.
        os.chdir(self.cwd_b)
        _Clock.current = COLLECTED_AT + timedelta(minutes=5)
        restarted = self._lab(deepcopy(self.config), weather)
        self.assertEqual(restarted.market_snapshots_path, archive)
        self.assertEqual(restarted.state["last_collect_at"], COLLECTED_AT.isoformat())
        restarted.run(exchange)
        index = PredictionLabCollectorDaemon._update_replay_index(self.config, restarted)
        assert index is not None, "restarted collector did not update its enabled replay index"
        self.assertEqual(index["new_indexed_rows"], 102)
        self.assertEqual(index["indexed_rows"], 204)
        self.assertTrue(archive.read_bytes().startswith(first_bytes))
        archive_bytes = archive.read_bytes()
        raw_rows = load_jsonl(archive)
        index_path, index_manifest = Path(index["index_path"]), Path(index["manifest_path"])
        self.assertEqual(index["source_path"], str(archive))
        self.assertEqual(index["invalid_rows"], 0)
        self.assertEqual(index["indexed_source_bytes"], len(archive_bytes))
        hydrated = list(load_indexed_collector_rows(index_path, index_manifest, max_rows=204))
        self.assertEqual(hydrated, raw_rows, "compact index must hydrate the actual collector bytes")
        self.assertEqual(Counter(row["market_id"] for row in hydrated), Counter({market.id: 2 for market in markets}))
        self.assertEqual(len({row["shared_candidate_id"] for row in raw_rows}), 204)
        self.assertEqual(len(weather.calls), 204)
        self.assertEqual(exchange.market_calls, 2)
        for row in raw_rows:
            source = row["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]["sources"][0]
            self.assertEqual(source["source_location_city"], "Seattle")
            self.assertEqual(source["target_mapping"]["source_target_date"], source["market_date"])
        for row in load_jsonl(index_path):
            self.assertNotIn("decision_artifact", row)
            self.assertNotIn("sources", row)
        unchanged_index = index_path.read_bytes()
        index_retry = PredictionLabCollectorDaemon._update_replay_index(self.config, restarted)
        assert index_retry is not None
        self.assertEqual(index_retry["new_indexed_rows"], 0)
        self.assertEqual(index_path.read_bytes(), unchanged_index)

        # The REAL independent resolver consumes compact index market references.
        fetched = []

        def fetch_market(market_id):
            fetched.append(market_id)
            return {
                "ticker": market_id, "status": "finalized", "result": outcomes[market_id],
                "settlement_ts": SETTLEMENT_AT.isoformat(),
            }

        feed_config = {
            "resolution_feed": {
                "enabled": True, "mode": "incremental_unresolved",
                "market_ref_paths": [str(index_path)],
                "output_dir": str(self.collector_root / "data" / "resolution-feed"),
                "central_output_dir": str(self.collector_root / "data" / "canonical-resolutions"),
                "interval_seconds": 1, "max_fetch_attempts": 1, "retry_delay_seconds": 0,
            },
        }
        _Clock.current = RETRIEVED_AT
        feed = run_resolution_feed_once(feed_config, now=RETRIEVED_AT, fetch_market=fetch_market)
        self.assertEqual(feed.status, "refreshed")
        assert feed.output_path is not None, "resolver failed to publish its independent resolution ledger"
        self.assertEqual((feed.resolved_market_count, feed.fetch_error_count), (102, 0))
        self.assertEqual(Counter(fetched), Counter({market.id: 1 for market in markets}))
        resolved = load_jsonl(feed.output_path)
        self.assertEqual({row["resolution"]["outcome"] for row in resolved}, {"YES", "NO", "VOID"})
        for row in resolved:
            self.assertEqual(row["requested_market_id"], row["market_id"])
            self.assertEqual(row["returned_market_id"], row["market_id"])
            self.assertEqual(row["settlement_ts"], SETTLEMENT_AT.isoformat())
            self.assertEqual(datetime.fromisoformat(row["resolved_at"]), RETRIEVED_AT)
        resolution_bytes = feed.output_path.read_bytes()

        promotion = auto_populate_source_router_history(
            collector_snapshots_path=archive, strict_resolutions_path=feed.output_path,
        )
        manifest = json.loads(promotion.manifest_path.read_text())
        artifacts = manifest["artifacts"]
        self.assertEqual(promotion.status, "history_ready", manifest)
        self.assertEqual(promotion.counts["eligible"], 100, promotion.counts)
        self.assertEqual(promotion.counts["collapsed"], 100, promotion.counts)
        self.assertEqual(promotion.counts["invalid"], 0, promotion.counts)
        self.assertTrue(promotion.generation_dir.is_relative_to(auto_source_router_history_root()))
        self.assertEqual(manifest["input_sha256"], {"collector_snapshots": _sha(archive), "strict_resolutions": _sha(feed.output_path)})
        for name, path in artifacts.items():
            self.assertEqual(_sha(path), manifest["artifact_sha256"][name], name)
        replay_inputs = load_jsonl(Path(artifacts["replay_inputs"]))
        self.assertEqual(len(replay_inputs), 204)
        self.assertTrue(all(verify_replay_decision_input_record_v1(row) for row in replay_inputs))
        raw_by_candidate = {row["shared_candidate_id"]: row for row in raw_rows}
        for row in replay_inputs:
            original = raw_by_candidate[row["shared_candidate_id"]]
            self.assertEqual(row["decision_key"]["shared_snapshot_id"], original["shared_snapshot_id"])
            self.assertEqual(row["decision_key"]["shared_candidate_id"], original["shared_candidate_id"])
            self.assertEqual(row["decision_key"]["market_id"], original["market_id"])
        self._assert_outcome_free(replay_inputs)
        self._assert_outcome_free(load_jsonl(promotion.generation_dir / "source_observations" / "pending_source_observations.jsonl"))
        finalized = load_jsonl(Path(artifacts["finalized_outcomes"]))
        self.assertEqual(len(finalized), 204, "resolver -> exact outcome binder lost collector decisions")
        identity = lambda row: (row["canonical_input_sha256"], json.dumps(row["decision_key"], sort_keys=True))
        self.assertEqual(Counter(map(identity, finalized)), Counter(map(identity, replay_inputs)))
        for row in finalized:
            self.assertEqual(row["official_outcome"], outcomes[row["market_id"]].upper())
            self.assertEqual(row["provenance"]["strict_resolution_source_sha256"], _sha(feed.output_path))
        history = load_jsonl(promotion.history_path)
        independent = load_jsonl(Path(artifacts["strict_independent_history"]))
        # The collector retains normalized sources AND raw source-signal copies;
        # the materializer may preserve non-strict diagnostic rows for the latter.
        # Only independently proved NWS forecasts may contribute to history.
        strict_history = [row for row in history if row.get("eligible_for_source_history")]
        self.assertEqual(len(strict_history), 200)
        self.assertEqual(len(independent), 100)
        self.assertEqual({row["market_id"] for row in independent}, binary_ids)
        self.assertEqual(Counter(row["official_outcome"] for row in strict_history), {"YES": 100, "NO": 100})
        for row in independent:
            self.assertTrue(row["eligible_for_source_history"])
            self.assertTrue(row["direction_correct"])
            self.assertEqual(row["source_id"], "nws")
            self.assertEqual(datetime.fromisoformat(row["source_as_of"]), COLLECTED_AT - timedelta(minutes=1))
            self.assertEqual(datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00")), COLLECTED_AT)
            self.assertEqual(row["known_after"], row["settlement_ts"])
            self.assertEqual(datetime.fromisoformat(row["settlement_ts"].replace("Z", "+00:00")), SETTLEMENT_AT)
            self.assertIn("source_record_sha256", row["source_provenance"])
            self.assertIn("strict_resolution_source_sha256", row["resolution_provenance"])
        void_rows = load_jsonl(promotion.generation_dir / "source_observations" / "void_source_observations.jsonl")
        self.assertEqual(len([row for row in void_rows if row["source_correctness_eligibility"] == "eligible_strict_source_proof"]), 2,
                         "VOID must survive the actual independent resolver -> binder seam")
        for row in void_rows:
            self.assertEqual(row["market_id"], void_id)
            self.assertEqual(row["official_outcome"], "VOID")
            self.assertFalse(row["eligible_for_source_history"])
            self.assertNotIn("direction_correct", row)
        unusable = load_jsonl(Path(artifacts["unusable_observations"]))
        future_sources = [row for row in history + unusable if row["market_id"] == future_id and row["source_id"] == "nws"]
        self.assertEqual(sum("source_as_of_after_immutable_observed_at" in row["strict_source_proof"]["reasons"] for row in future_sources), 2)
        for row in future_sources:
            self.assertFalse(row.get("eligible_for_source_history"))
            self.assertNotEqual(row["strict_source_proof"]["status"], "eligible")
        scorecards = load_jsonl(promotion.scoreboard_path)
        self.assertEqual(len(scorecards), 1)
        self.assertEqual(scorecards[0]["sample_count"], 100)
        self.assertEqual(scorecards[0]["threshold_direction_accuracy"], 1.0)
        self.assertEqual(len(scorecards[0]["provenance"]["settled_observations"]), 100)

        # Reuse both durable consumers; neither polling nor restart adds samples.
        reused = auto_populate_source_router_history(collector_snapshots_path=archive, strict_resolutions_path=feed.output_path)
        self.assertTrue(reused.reused)
        self.assertEqual(reused.generation_dir, promotion.generation_dir)
        self.assertEqual(reused.counts, promotion.counts)
        run_resolution_feed_once(feed_config, now=RETRIEVED_AT + timedelta(seconds=2), fetch_market=fetch_market)
        self.assertEqual(len(fetched), 102, "resolved markets must not be refetched after restart")
        self.assertEqual(feed.output_path.read_bytes(), resolution_bytes)

        # Fresh actual collector candidates feed the actual paper router through
        # the published current symlink; do not hand-author consumer source data.
        current_scorecard = auto_source_router_history_root() / "current" / "source_router_scoreboard" / "strict_finalized_source_scoreboard.jsonl"
        self.assertEqual(current_scorecard.resolve(), promotion.scoreboard_path)
        lane = _LaneDefinition("shadow_source_router", lane_type="source_router", parameters={"scoreboard_path": str(current_scorecard)})
        later_markets = [_market(date(2026, 9, 16), threshold) for threshold in (70, 80)]
        candidate_config = {**deepcopy(self.config), "data_dir": str(self.root / "candidate-collector")}
        candidate_lab = self._lab(candidate_config, _WeatherBoundary(later_markets))
        candidate_exchange = _ExchangeBoundary(later_markets)
        for delta, iso_date, expected in (
            (-1, False, ("SKIP", "SKIP")), (0, False, ("SKIP", "SKIP")),
            (1, True, ("BUY_YES", "BUY_NO")), (2, False, ("BUY_YES", "BUY_NO")),
        ):
            # Paired external-question control consumes the SAME history. Keep
            # the ISO-date regression failing if prose is the only working form.
            candidate_exchange.markets = [_market(date(2026, 9, 16), threshold, iso_date=iso_date) for threshold in (70, 80)]
            _Clock.current = SETTLEMENT_AT + timedelta(seconds=delta)
            candidate_lab.run(candidate_exchange)
            candidates = load_jsonl(candidate_lab.market_snapshots_path)[-2:]
            loaded = load_shared_candidate_paper_inputs(
                candidate_lab.market_snapshots_path,
                config=candidate_config, data_dir=self.root / "synthetic-paper-contracts",
            )
            self.assertFalse(loaded.skipped_rows)
            selected_inputs = {
                candidate["shared_candidate_id"]: loaded.inputs_by_shared_candidate_id[candidate["shared_candidate_id"]]
                for candidate in candidates
            }
            written = write_paper_shadow_lane_decisions(
                config={"paper_shadow_lanes": {
                    "enabled": True, "enabled_lanes": ["shadow_source_router"],
                    "shadow_source_router": {"parameters": {"scoreboard_path": str(current_scorecard)}},
                }},
                candidate_dataset_path=candidate_lab.market_snapshots_path,
                inputs_by_shared_candidate_id=selected_inputs,
                wallet_decision_rows={}, wallet_runs={}, ledger_root=self.root / "paper-lane-receipts",
            )
            self.assertEqual(written.rows_written, 2)
            receipts = {row["shared_candidate_id"]: row for row in load_jsonl(Path(written.decision_path))[-2:]}
            decisions = []
            for candidate in candidates:
                candidate_input = next(iter(selected_inputs[candidate["shared_candidate_id"]].values()))
                shared = candidate_input.shared_candidate
                self.assertEqual(datetime.fromisoformat(shared["observed_at"]), _Clock.current)
                before = deepcopy(candidate)
                decision = _source_router_decision(lane, candidate_input.signal, None, shared)
                self.assertEqual(candidate, before, "paper consumer mutated collector evidence")
                decisions.append(decision)
                receipt = receipts[candidate["shared_candidate_id"]]
                with self.subTest(seam="collector -> paper receipt identity", cutoff_delta=delta, market_id=candidate["market_id"]):
                    self.assertEqual(receipt.get("shared_snapshot_id"), candidate["shared_snapshot_id"])
                    self.assertEqual(receipt["shared_candidate"].get("shared_snapshot_id"), candidate["shared_snapshot_id"])
                    self.assertEqual(receipt["shared_candidate_id"], candidate["shared_candidate_id"])
                    self.assertEqual(datetime.fromisoformat(receipt["observed_at"]), _Clock.current)
                self.assertEqual(receipt["action"], decision["action"])
                with self.subTest(cutoff_delta=delta, market_id=candidate["market_id"]):
                    self.assertEqual(decision["source_router"]["scoreboard_path"], str(current_scorecard))
                    if delta <= 0:
                        self.assertEqual(decision["reason_code"], "no_usable_reliability_after_backoff")
                        self.assertEqual(decision["approved_position_size_usd"], 0.0)
                        self.assertEqual(decision["source_router"]["sources_used"], [])
                    else:
                        self.assertEqual([source["sample_count"] for source in decision["source_router"]["sources_used"]], [100])
            with self.subTest(seam="canonical paper adapter -> router decision", cutoff_delta=delta, iso_date=iso_date):
                self.assertEqual(tuple(decision["action"] for decision in decisions), expected,
                                 [(decision["action"], decision["reason_code"]) for decision in decisions])
        self.assertEqual(archive.read_bytes(), archive_bytes)
        self.assertEqual(feed.output_path.read_bytes(), resolution_bytes)
        self.assertEqual(index_path.read_bytes(), unchanged_index)
        self.assertFalse(lab.predictions_path.exists(), "observer-only fixture must not write paper positions")
        self.assertFalse((self.cwd_b / "data" / "prediction_lab").exists(), "restart forked durable collector state by CWD")

    def test_collector_captures_at_or_after_settlement_never_become_strict_history(self):
        """Do not relabel fresh post-facto captures as historical forecasts."""
        markets = [_market(date(2026, 6, 2)), _market(date(2026, 6, 3), 80)]
        lab = self._lab(self.config, _WeatherBoundary(markets))
        lab.run(_ExchangeBoundary(markets))
        archive_bytes = lab.market_snapshots_path.read_bytes()
        index = PredictionLabCollectorDaemon._update_replay_index(self.config, lab)
        assert index is not None
        self.assertEqual(index["indexed_rows"], 2)
        os.chdir(self.cwd_b)
        _Clock.current = RETRIEVED_AT
        settlement_times = {
            markets[0].id: COLLECTED_AT - timedelta(seconds=1),
            markets[1].id: COLLECTED_AT,
        }
        fetched = []

        def fetch_market(market_id):
            fetched.append(market_id)
            return {
                "ticker": market_id, "status": "finalized",
                "result": "yes" if market_id == markets[0].id else "no",
                "settlement_ts": settlement_times[market_id].isoformat(),
            }

        feed = run_resolution_feed_once({"resolution_feed": {
            "enabled": True, "mode": "incremental_unresolved",
            "market_ref_paths": [index["index_path"]],
            "output_dir": str(self.collector_root / "data" / "late-capture-resolution-feed"),
            "max_fetch_attempts": 1, "retry_delay_seconds": 0,
        }}, now=RETRIEVED_AT, fetch_market=fetch_market)
        self.assertEqual(Counter(fetched), Counter({market.id: 1 for market in markets}))
        self.assertEqual(feed.resolved_market_count, 2)
        assert feed.output_path is not None
        promotion = auto_populate_source_router_history(
            collector_snapshots_path=lab.market_snapshots_path,
            strict_resolutions_path=feed.output_path,
        )
        self.assertEqual(lab.market_snapshots_path.read_bytes(), archive_bytes)
        manifest = json.loads(promotion.manifest_path.read_text())
        pending = load_jsonl(promotion.generation_dir / "source_observations" / "pending_source_observations.jsonl")
        self._assert_outcome_free(pending)
        for market in markets:
            with self.subTest(seam="collector capture -> independently known settlement", market_id=market.id):
                self.assertFalse([
                    row["source_observation_id"] for row in load_jsonl(promotion.history_path)
                    if row["market_id"] == market.id and row.get("eligible_for_source_history")
                ], "source_as_of before capture is insufficient: immutable capture must precede settlement")
        with self.subTest(seam="post-facto capture -> strict history publication"):
            self.assertEqual(promotion.counts["eligible"], 0, promotion.counts)
            self.assertEqual(promotion.status, "no_router_history")
            self.assertEqual(load_jsonl(Path(manifest["artifacts"]["strict_independent_history"])), [])


if __name__ == "__main__":
    unittest.main()
