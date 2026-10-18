"""Truthful heuristic receipts; fixture forecasts are not calibration evidence."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from bot.feeds.weather_pro import ProWeatherEngine, WeatherSnapshot


SCRATCH = os.environ["TMPDIR"]
QUESTION = "Will Los Angeles high temperature be above 80° on Oct 19, 2026?"
LOCATION = {"basis": "station", "station_id": "KLAX", "station_origin": "configured_city_station",
            "station_mapping": "inferred", "latitude": 33.93806, "longitude": -118.38889,
            "coordinate_source": "nws_station_metadata"}


class WeatherProbabilityEvidenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {"TMPDIR": SCRATCH}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        for target in ("socket.socket.connect", "socket.create_connection"):
            guard = patch(target, side_effect=AssertionError("network forbidden"))
            guard.start()
            self.addCleanup(guard.stop)

    def weather(self, high=88):
        engine = ProWeatherEngine()
        self.addCleanup(engine.close)
        now = datetime.now(timezone.utc)
        snapshot = WeatherSnapshot(city="los angeles", high_temp_f=high, low_temp_f=60,
            current_temp_f=70, source="nws", fetched_at=now, forecast_hours_ahead=12,
            confidence=0.8, forecast_date="2026-10-19", weather_date="2026-10-19",
            source_details={"forecast_location": dict(LOCATION)})
        with patch.object(engine.nws, "get_forecast", return_value=snapshot), \
             patch.object(engine.nws, "get_station_observation", return_value=None), \
             patch.object(engine.open_meteo, "get_forecast", return_value=None):
            return engine._score_temperature_market(QUESTION, 0.4, market_date="2026-10-19")

    def test_weather_heuristic_receipt_describes_exact_unchanged_yes_probability(self):
        signal = self.weather()
        self.assertEqual(signal["predicted_prob"], 0.95)
        receipt = signal.get("probability_evidence")
        self.assertIsInstance(receipt, dict)
        self.assertEqual(receipt, {"schema_version": 1, "yes_probability": 0.95,
            "basis": "heuristic_weather_distance_v1", "calibration_status": "not_established",
            "fallback_reason": "empirical_contract_probability_unavailable",
            "source_id": "nws", "forecast_policy_id": "nws",
            "location_provenance": LOCATION})
        self.assertNotIn("distribution_probability", signal["data"])
        self.assertNotIn("calibrated", receipt)
    def ensemble(self, weather, *, trace=False):
        from types import SimpleNamespace
        from bot.strategies.enhanced import EnhancedStrategyEngine
        engine = EnhancedStrategyEngine({"enable_news": False, "enable_social": False, "enable_ai": False,
            "min_edge": 0.01, "signal_audit_path": str(self.root / "audit.jsonl")})
        self.addCleanup(engine.live_feeds.close)
        market = SimpleNamespace(id="KXHIGHLAX-26OCT19-T80", exchange="kalshi", question=QUESTION,
            yes_price=0.4, no_price=0.4, volume=10000, liquidity=10000, category="Weather")
        price = {"signal_type": "price", "predicted_prob": 0.65, "confidence": 0.9}
        with patch.object(engine, "_price_signal", return_value=price), \
             patch.object(engine, "_live_data_signal", return_value=copy.deepcopy(weather)), \
             patch.object(engine, "_volume_signal", return_value=None), \
             patch.object(engine, "_time_signal", return_value=None):
            result = engine.analyze_market_with_trace(market)[0] if trace else engine.analyze_market(market)
        assert result is not None
        return result

    def test_final_ensemble_receipt_labels_actual_weighted_probability_not_raw_weather(self):
        weather = self.weather()
        for trace in (False, True):
            with self.subTest(trace=trace):
                result = self.ensemble(weather, trace=trace)
                expected = round((weather["predicted_prob"] * 0.5 + 0.65 * 0.4) / 0.9, 4)
                self.assertEqual(result["model_probability"], expected)
                self.assertNotEqual(expected, weather["predicted_prob"])
                receipt = result.get("probability_evidence")
                self.assertIsInstance(receipt, dict)
                self.assertEqual(receipt, {"schema_version": 1, "yes_probability": expected,
                    "basis": "heuristic_weighted_ensemble_v1", "calibration_status": "not_established",
                    "fallback_reason": "empirical_contract_probability_unavailable"})
                self.assertEqual(result["signal_details"]["live"]["probability_evidence"], weather["probability_evidence"])

    def test_ensemble_omits_inconsistent_copied_weather_receipt_without_changing_odds(self):
        weather = self.weather()
        original = self.ensemble(weather)
        weather["probability_evidence"]["yes_probability"] = 0.2
        for trace in (False, True):
            with self.subTest(trace=trace):
                result = self.ensemble(weather, trace=trace)
                self.assertEqual(result["model_probability"], original["model_probability"])
                self.assertNotIn("probability_evidence", result["signal_details"]["live"])
                self.assertEqual(result["probability_evidence"], original["probability_evidence"])

    def test_malformed_receipts_are_omitted_at_composer_and_wallet_boundaries(self):
        from bot.forward_composed_paper import compose_forward_rows
        from bot.composed_lane_wallet import build_composed_trade_context
        from bot.persistent_composed_paper import _prepare_signal
        from bot.shared_core import AccountState
        signal = self.ensemble(self.weather())
        malformed = [("yes_probability", value) for value in (None, True, "0.8", float("nan"), float("inf"), -0.1, 1.1, 0.42, 10 ** 1000)]
        malformed += [("schema_version", value) for value in (True, 2, None)]
        malformed += [("basis", value) for value in ([], {}, "router_rank_score")]
        malformed += [("calibration_status", "calibrated")]
        for field, value in malformed:
            with self.subTest(field=field, value=value):
                rows = self.rows(signal)
                rows[1]["probability_evidence"][field] = value
                pairs, intents = compose_forward_rows(rows)
                intent = next(i for i in intents if i["lane_id"] == "forward_composed_paper")
                self.assertNotIn("probability_evidence", intent["source_context"])
                # A persisted legacy intent can bypass the composer: validate it
                # again at each actual wallet consumer, not just at the producer.
                intent["source_context"]["probability_evidence"] = rows[1]["probability_evidence"]
                context, receipt = build_composed_trade_context(intent, AccountState(100, 100, 100, 0, 0, 0))
                self.assertNotIn("probability_evidence", context.source_context)
                self.assertNotIn("probability_evidence", receipt)
                prepared = _prepare_signal(intent, pairs[0], rows)
                self.assertNotIn("probability_evidence", prepared)
                self.assertNotIn("probability_evidence", prepared["signals"])

    def test_no_config_restart_keeps_legacy_fingerprint_even_when_receipt_is_available(self):
        from bot import persistent_composed_paper as consumer
        rows = self.rows(self.ensemble(self.weather()))
        original = consumer._prepare_signal
        def legacy_signal(*args, **kwargs):
            signal = original(*args, **kwargs)
            signal.pop("probability_evidence", None)
            signal["signals"].pop("probability_evidence", None)
            return signal
        with patch.object(consumer, "_prepare_signal", side_effect=legacy_signal):
            root, params, report = self.consume(rows, "legacy-fingerprint")
        self.assertEqual(report["decisions"][0]["status"], "opened")
        before = next(root.glob("sim_*.json")).read_bytes()
        self.assertEqual(consumer.consume_persistent_composed_paper(**params)["decisions"][0]["status"], "already_processed")
        self.assertEqual(next(root.glob("sim_*.json")).read_bytes(), before)

    def test_receipt_projection_drops_unknown_nested_fields_and_absence_stays_usable(self):
        from bot.forward_composed_paper import compose_forward_rows
        from bot.composed_lane_wallet import build_composed_trade_context
        from bot.shared_core import AccountState
        signal = self.ensemble(self.weather())
        rows = self.rows(signal)
        rows[1]["probability_evidence"].update({"unreviewed": {"anything": [1]},
            "source_id": {"not": "a source"}, "forecast_policy_id": ["not a policy"],
            "location_provenance": {"station_id": "KLAX", "latitude": float("nan"),
                "station_origin": {"not": "an origin"}, "unreviewed": {"anything": 1}}})
        _, intents = compose_forward_rows(rows)
        intent = next(i for i in intents if i["lane_id"] == "forward_composed_paper")
        self.assertEqual(intent["source_context"]["probability_evidence"],
            {**signal["probability_evidence"], "location_provenance": {"station_id": "KLAX"}})
        legacy = copy.deepcopy(rows)
        legacy[1].pop("probability_evidence")
        _, legacy_intents = compose_forward_rows(legacy)
        legacy_intent = next(i for i in legacy_intents if i["lane_id"] == "forward_composed_paper")
        context, receipt = build_composed_trade_context(legacy_intent, AccountState(100, 100, 100, 0, 0, 0))
        self.assertEqual(receipt["probability_evidence_status"], "legacy_unknown")
        self.assertNotIn("probability_evidence", context.source_context)
        _, _, with_receipt = self.consume(rows, "with-receipt")
        legacy_root, _, without_receipt = self.consume(legacy, "without-receipt")
        self.assertEqual(with_receipt["decisions"][0]["status"], "opened")
        self.assertEqual(without_receipt["decisions"][0]["status"], "opened")
        trade = json.loads(next(legacy_root.glob("sim_*.json")).read_text())["trades"][0]
        self.assertNotIn("probability_evidence", trade["signals"])
        self.assertEqual(trade["position_size"], 10)
        rows[1]["probability_evidence"]["unreviewed"] = {"nested": {"outcome": "YES"}}
        with self.assertRaisesRegex(ValueError, "outcome-like"):
            compose_forward_rows(rows)

    def rows(self, signal):
        from bot.forward_composed_paper import CONTROL, PICKER, ROUTER, PRE_RISK_GUARD
        identity = {"shared_candidate_id": "candidate", "shared_snapshot_id": "snapshot", "run_id": "run",
            "market_id": signal["market_id"], "observed_at": "2026-10-18T12:00:00+00:00"}
        # This fixture starts at the explicit lane-row serialization contract;
        # the parent owns projection from collector/shared-candidate artifacts.
        picker = {**identity, "policy": PICKER, "decision_id": "picker", "action": signal["direction"],
            "question": QUESTION, "exchange": "kalshi", "event_ticker": "KXHIGHLAX-26OCT19",
            "model_probability": signal["model_probability"], "confidence": signal["confidence"],
            "probability_evidence": copy.deepcopy(signal["probability_evidence"]), "entry_price": 0.4,
            "market_route": {"allowed": True, "handler_id": "weather.daily_temperature.v1"},
            "provenance": {"picker_intent_source": "decision_artifact.strategy_signal"}}
        return [{**identity, "policy": CONTROL, "decision_id": "control", "action": "SKIP"}, picker,
            {**identity, "policy": ROUTER, "decision_id": "router", "action": signal["direction"],
             "provenance": {"source_router": {"source_grade": "STRONG_" + signal["direction"][4:],
                 "rank_score": 0.999, "direct_history": {"mode": "committed_index_direct_read"}}}},
            {**identity, "policy": PRE_RISK_GUARD, "decision_id": "guard", "action": signal["direction"],
             "entry_price": 0.45, "provenance": {"payout_aware": {"selected_side_ask": 0.4,
                 "entry_price": 0.45, "quote_not_fill": True}}}]

    def consume(self, rows, name):
        from bot.persistent_composed_paper import consume_persistent_composed_paper
        from datetime import datetime
        observed = rows[0]["observed_at"]
        root = self.root / name / "paper"
        params = dict(saved_config_path=Path(__file__).resolve().parents[1] / "lane_compositions" /
            "pre_risk_router_guard_kelly.yaml", lane_rows=rows, cohort_id="evidence-test",
            publisher_state={"publisher": {"runtime_kind": "collector", "instance_id": "owner"},
                "latest_snapshot": {"snapshot_id": "snapshot", "publisher_runtime": "collector",
                    "publisher_instance_id": "owner", "published_at": observed}},
            paper_root=root, protected_roots={key: self.root / key / "paper" for key in ("stable", "beta", "live")},
            account_config={"starting_balance": 100.0,
                "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
                "risk": {"kelly_fraction": 0.5, "max_bet_pct": 0.1}},
            enabled=True, now=datetime.fromisoformat(observed))
        report = consume_persistent_composed_paper(**params)
        return root, params, report

    def test_weather_ensemble_receipt_reaches_composed_and_persistent_actual_kelly_unchanged(self):
        from bot.forward_composed_paper import compose_forward_rows, evaluate_forward_composition
        from bot.composed_lane_wallet import build_composed_trade_context
        from bot.shared_core import AccountState
        from bot.risk import RiskManager
        from bot.strategies.enhanced import KellySizer
        from bot.persistent_composed_paper import consume_persistent_composed_paper
        for high, action in ((88, "BUY_YES"), (72, "BUY_NO")):
            with self.subTest(action=action):
                signal = self.ensemble(self.weather(high))
                self.assertEqual(signal["direction"], action)
                rows = self.rows(signal)
                _, intents = compose_forward_rows(rows)
                intent = next(i for i in intents if i["lane_id"] == "forward_composed_paper")
                evidence = signal["probability_evidence"]
                self.assertEqual(intent["source_context"].get("probability_evidence"), evidence)
                context, receipt = build_composed_trade_context(intent, AccountState(100, 100, 100, 0, 0, 0))
                self.assertEqual(context.source_context.get("probability_evidence"), evidence)
                self.assertEqual(receipt.get("probability_evidence"), evidence)
                selected = signal["model_probability"] if action == "BUY_YES" else 1 - signal["model_probability"]
                kelly = KellySizer(kelly_fraction=0.5, max_bet_pct=0.1)
                risk = RiskManager({"starting_balance": 100, "data_dir": str(self.root / (action + "-risk"))})
                report = evaluate_forward_composition(rows, [], starting_balance_usd=100, kelly_sizer=kelly,
                    risk_policy=risk, min_edge=0.01, min_confidence=0.5, max_entry_price=0.99)
                decision = report["combined_wallet"]["decision_rows"][0]
                self.assertEqual(decision["status"], "opened")
                self.assertEqual(decision.get("probability_evidence"), evidence)
                self.assertEqual(report["combined_wallet"]["open_positions"][0].get("probability_evidence"), evidence)
                self.assertAlmostEqual(decision["shared_core_reasoning"]["normalized"]["win_probability"], selected)
                root, params, report = self.consume(rows, action)
                self.assertEqual(report["decisions"][0]["status"], "opened")
                trade = json.loads(next(root.glob("sim_*.json")).read_text())["trades"][0]
                self.assertEqual(trade["signals"].get("probability_evidence"), evidence)
                self.assertAlmostEqual(trade["model_probability"], selected, places=4)
                self.assertEqual(trade["position_size"], kelly.calculate(selected, 0.45, 100))
                self.assertEqual(consume_persistent_composed_paper(**params)["decisions"][0]["status"], "already_processed")


if __name__ == "__main__":
    unittest.main()
