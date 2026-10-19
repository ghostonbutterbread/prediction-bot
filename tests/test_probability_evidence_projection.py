"""Exercise real probability producers and serializers, never repair dropped receipts in tests."""
import copy
import json
import os
from contextlib import nullcontext
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from bot.feeds.weather_pro import ProWeatherEngine
from bot.prediction_lab import PredictionLab
from test_weather_location_replay import _Clock, LAX_LAT_LON, MARKET_ID, QUESTION, TARGET


ROOT = Path(__file__).resolve().parents[1]
STAMP = "2026-10-18T19:00:01+00:00"


class ProbabilityEvidenceProjectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {"TMPDIR": os.environ["TMPDIR"]}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        for name in ("socket.socket.connect", "socket.create_connection"):
            guard = patch(name, side_effect=AssertionError("network forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        self.lab = PredictionLab({"data_dir": str(self.root / "collector"),
            "runtime": {"storage_root": str(self.root / "collector")},
            "prediction_lab": {"mode": "collector"}, "strategy": {
                "enable_news": False, "enable_social": False, "enable_ai": False,
                "min_edge": 0.01, "signal_audit_path": str(self.root / "audit.jsonl")}})
        self.addCleanup(self.lab.strategy.live_feeds.close)
        # Forecast and candidate clocks are fixed; validate at that candidate,
        # not the later wall clock used to run this historical fixture.
        self.lab.strategy.validator.as_of = _Clock.fromisoformat(STAMP)

    def weather(self, high, *, metadata, city_only=False):
        def handler(request):
            path = request.url.path
            if path in ("/stations/KLAX", "/stations/KBUR"):
                station = path.rsplit("/", 1)[-1]
                coordinates = [-118.3, 34.2] if station == "KBUR" else list(LAX_LAT_LON[::-1])
                payload = {"geometry": {"type": "Point", "coordinates": coordinates},
                           "properties": {"stationIdentifier": station}}
            elif path.startswith("/points/"):
                payload = {"properties": {"gridId": "LOX", "gridX": 1, "gridY": 1}}
            elif path == "/gridpoints/LOX/1,1/forecast":
                payload = {"properties": {"periods": [
                    {"name": "Monday", "temperature": high, "isDaytime": True,
                     "startTime": TARGET + "T06:00:00-07:00", "endTime": TARGET + "T18:00:00-07:00"},
                    {"name": "Monday Night", "temperature": 60, "isDaytime": False,
                     "startTime": TARGET + "T18:00:00-07:00", "endTime": "2026-10-20T06:00:00-07:00"}]}}
            elif path.endswith("/observations/latest"):
                payload = {"properties": {}}
            elif request.url.host == "api.open-meteo.com":
                return httpx.Response(503)
            else:
                self.fail(f"unexpected weather request: {request.url}")
            return httpx.Response(200, json=payload)
        engine = ProWeatherEngine(capture_store=self.lab.capture_store)
        self.addCleanup(engine.close)
        for feed in (engine.nws, engine.open_meteo, engine.owm):
            feed.http.close()
            feed.http = httpx.Client(transport=httpx.MockTransport(handler))
        with patch("bot.feeds.weather_pro.datetime", _Clock), \
             (patch.dict("bot.feeds.weather_pro.CITY_NWS", {}, clear=True) if city_only else nullcontext()):
            weather = engine._score_temperature_market(QUESTION, 0.4, market_date=TARGET,
                market_metadata=metadata)
        self.assertIsNotNone(weather)
        assert weather is not None
        return weather

    def producer(self, high=88, *, change_weather=None, metadata=None, city_only=False):
        metadata = {"rules_primary": "Maximum temperature recorded at CLILAX according to NWS."} if metadata is None else metadata
        weather = self.weather(high, metadata=metadata, city_only=city_only)
        if change_weather:
            change_weather(weather)
        market = SimpleNamespace(id=MARKET_ID, question=QUESTION, exchange="kalshi",
            yes_price=0.4, no_price=0.4, volume=10000, liquidity=10000, category="Weather",
            metadata={**metadata,
                "market_group": "weather", "series": "daily_temperature",
                "event_ticker": "KXHIGHLAX-26OCT19", "market_route": {
                    "allowed": True, "handler_id": "weather.daily_temperature.v1"}})
        with patch.object(self.lab.strategy, "_price_signal", return_value={
                "signal_type": "price", "predicted_prob": 0.65, "confidence": 0.9}), \
             patch.object(self.lab.strategy, "_live_data_signal", return_value=copy.deepcopy(weather)), \
             patch.object(self.lab.strategy, "_volume_signal", return_value=None), \
             patch.object(self.lab.strategy, "_time_signal", return_value=None):
            ensemble = self.lab.strategy.analyze_market(market)
        self.assertIsNotNone(ensemble)
        assert ensemble is not None
        artifact = {"strategy_signal": ensemble, "observed_at": STAMP,
            "source_context": {"source": "provided", "as_of": STAMP,
                               "data": {"market_metadata": market.metadata}},
            "execution_snapshot": {"source": "book", "best_yes_ask": 0.4,
                                   "best_no_ask": 0.4, "estimated_fill_price": 0.45},
            "pre_logic_order_book_snapshot": {"source": "book", "market_id": MARKET_ID, "observed_at": STAMP,
                           "data": {"best_yes_ask": 0.4, "best_no_ask": 0.4}},
            "order_book": {"source": "book", "market_id": MARKET_ID, "observed_at": STAMP,
                           "data": {"best_yes_ask": 0.4, "best_no_ask": 0.4}}}
        self.lab._attach_weather_source_snapshot(artifact, market)
        row = self.lab._build_market_snapshot_row("snapshot", market, ensemble,
            decision_type=ensemble["direction"].lower(), prediction_recorded=True,
            decision_artifact=artifact, observed_at=STAMP)
        return row, weather, ensemble

    @staticmethod
    def snapshot(row):
        return row["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]

    def test_producer_fixture_uses_recorded_decision_time_when_wall_clock_advances(self):
        class LaterWallClock(_Clock):
            @classmethod
            def now(cls, tz=None):
                return super().now(tz).replace(day=20)

        with patch("bot.strategies.signal_validator.datetime", LaterWallClock):
            row, weather, ensemble = self.producer(72)
        self.assertEqual(ensemble["direction"], "BUY_NO")
        self.assertEqual(self.snapshot(row)["source_signal"]["probability_evidence"], weather["probability_evidence"])

    def test_collector_rebuild_preserves_valid_weather_yes_receipt_not_ensemble_odds(self):
        for high, action in ((88, "BUY_YES"), (72, "BUY_NO")):
            with self.subTest(action=action):
                row, weather, ensemble = self.producer(high)
                self.assertEqual(ensemble["direction"], action)
                signal = self.snapshot(row)["source_signal"]
                self.assertEqual(signal.get("probability_evidence"), weather["probability_evidence"])
                self.assertEqual(signal["predicted_prob"], weather["predicted_prob"])
                self.assertNotEqual(weather["predicted_prob"], ensemble["model_probability"])
                self.assertEqual(signal["probability_evidence"]["calibration_status"], "not_established")
                self.assertNotIn("model_probability", signal)

    def test_collector_weather_receipt_survives_sealed_replay_and_real_export(self):
        from bot.collector_paths import COLLECTOR_ROOT_ENV
        from bot.collector_replay_inputs import export_collector_replay_inputs
        from bot.replay_decision_input import build_replay_decision_input_v1, verify_replay_decision_input_record_v1
        for high in (88, 72):
            with self.subTest(high=high):
                row, weather, ensemble = self.producer(high)
                before = copy.deepcopy(row)
                result = build_replay_decision_input_v1(row)
                self.assertTrue(result.ok, result.errors)
                sealed = json.loads(json.dumps(result.record))
                source = sealed["source_inputs"]["source_context"]["data"]["weather_source_snapshot"]["source_signal"]
                self.assertEqual(source.get("probability_evidence"), weather["probability_evidence"])
                self.assertNotIn("model_probability", source)
                self.assertNotEqual(source["probability_evidence"], ensemble["probability_evidence"])
                self.assertTrue(verify_replay_decision_input_record_v1(sealed))
                source["probability_evidence"]["location_provenance"]["station_id"] = "KBUR"
                self.assertFalse(verify_replay_decision_input_record_v1(sealed))
                archive = self.root / f"collector-{high}.jsonl"
                raw_bytes = (json.dumps(row) + "\n").encode()
                archive.write_bytes(raw_bytes)
                with patch.dict(os.environ, {COLLECTOR_ROOT_ENV: str(self.root)}):
                    exported = export_collector_replay_inputs(source_archive=archive,
                        output_dir=self.root / "data" / "derived_reports" / str(high), max_rows=1)
                self.assertEqual(json.loads(exported.records_path.read_text()), result.record)
                self.assertEqual(exported.metadata["accepted_row_count"], 1)
                self.assertEqual(exported.metadata["rejected_row_count"], 0)
                self.assertEqual(archive.read_bytes(), raw_bytes)
                self.assertEqual(row, before)

    def strict_envelope(self, row):
        from bot.replay_decision_input import build_replay_decision_input_v1
        from test_replay_decision_input import _snapshot_row
        result = build_replay_decision_input_v1(row)
        self.assertTrue(result.ok, result.errors)
        strict = _snapshot_row(patch={"market_id": MARKET_ID, "question": QUESTION, "observed_at": STAMP})
        # Only supply the strict fixture's required metadata around the actual
        # collector->sanitizer output; never manufacture a receipt it dropped.
        strict["decision_artifact"]["source_context"] = copy.deepcopy(result.record["source_inputs"]["source_context"])
        return strict

    def test_replay_rejects_inconsistent_or_ensemble_source_receipts_and_keeps_legacy(self):
        from bot.replay_decision_input import build_replay_decision_input_v1
        row, weather, ensemble = self.producer()
        strict = self.strict_envelope(row)
        expected = weather["probability_evidence"]
        self.assertEqual(self.snapshot(strict)["source_signal"].get("probability_evidence"), expected)
        valid = build_replay_decision_input_v1(strict, strict=True)
        self.assertTrue(valid.ok, valid.errors)
        cases = [("yes_probability", x) for x in (None, True, "0.95", -0.1, 1.1, 0.2)]
        cases += [("schema_version", True), ("schema_version", 2), ("basis", []),
                  ("basis", "heuristic_weighted_ensemble_v1"), ("basis", "router_rank_score"),
                  ("calibration_status", "calibrated"), ("fallback_reason", None)]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                malformed = copy.deepcopy(strict)
                signal = self.snapshot(malformed)["source_signal"]
                signal["probability_evidence"][field] = value
                rejected = build_replay_decision_input_v1(malformed, strict=True)
                self.assertFalse(rejected.ok)
                cleaned = build_replay_decision_input_v1(malformed)
                self.assertTrue(cleaned.ok, cleaned.errors)
                projected = cleaned.record["source_inputs"]["source_context"]["data"]["weather_source_snapshot"]["source_signal"]
                self.assertNotIn("probability_evidence", projected)
                self.assertEqual(projected["predicted_prob"], weather["predicted_prob"])
                self.assertTrue(any(x["path"].endswith("source_signal.probability_evidence")
                                    for x in cleaned.record["sanitization"]["omitted_fields"]))
        # Even numerically matching final-ensemble odds are not blind weather inputs.
        signal = self.snapshot(strict)["source_signal"]
        signal["probability_evidence"] = ensemble["probability_evidence"]
        signal["predicted_prob"] = ensemble["model_probability"]
        self.assertFalse(build_replay_decision_input_v1(strict, strict=True).ok)
        signal.pop("probability_evidence")
        legacy = build_replay_decision_input_v1(strict, strict=True)
        self.assertTrue(legacy.ok, legacy.errors)

    def lane_rows(self, row):
        from bot.file_ops import load_jsonl
        from bot.paper_evaluator_input import load_shared_candidate_paper_inputs
        from bot.paper_shadow_lanes import write_paper_shadow_lane_decisions
        dataset = self.root / "candidates.jsonl"
        dataset.write_text(json.dumps(row) + "\n")
        loaded = load_shared_candidate_paper_inputs(dataset, config={}, data_dir=self.root / "wallets",
                                                  wallet_ids=["stable_paper"])
        self.assertEqual(loaded.accepted_candidate_count, 1)
        stable = {"shared_candidate_id": row["shared_candidate_id"], "wallet_id": "stable_paper",
            "run_id": "run", "candidate_dataset_path": str(dataset), "decision_role": "paper_shadow",
            "decision_id": "stable", "market_id": MARKET_ID, "observed_at": STAMP,
            "action": row["direction"], "entry_price": 0.4, "reason_code": "approved"}
        config = {"paper_shadow_lanes": {"enabled": True, "definitions_dir": str(ROOT / "lanes"),
            "enabled_lanes": ["control_stable", "shadow_pre_risk_picker", "shadow_source_router",
                              "shadow_pre_risk_fee_aware_loss_guard"],
            "shadow_source_router": {"parameters": {"collector_replay_index_path": "fixture-index",
                "collector_replay_manifest_path": "fixture-manifest", "strict_resolutions_path": "fixture-resolutions"}}}}
        direct = SimpleNamespace(scorecard_rows=[], bucket_coverage={}, counts={},
                                 index_manifest_sha256="index", resolution_sha256="resolution", archive_receipts=[])
        side = row["direction"][4:]
        # History availability and selection are external fixtures; all lane
        # serialization, composition and Kelly/Simulator consumption are real.
        with patch("bot.weather.source_router_direct_history.load_direct_strict_source_history", return_value=direct), \
             patch("bot.weather.source_confidence.build_source_confidence_row", return_value={
                 "source_direction": side, "source_grade": "STRONG_" + side}):
            written = write_paper_shadow_lane_decisions(config=config, candidate_dataset_path=dataset,
                inputs_by_shared_candidate_id=loaded.inputs_by_shared_candidate_id,
                wallet_decision_rows={"stable_paper": [stable]},
                wallet_runs={"stable_paper": SimpleNamespace(session_id="run")}, ledger_root=self.root / "lanes")
        rows = load_jsonl(Path(written.decision_path))
        # Repeated helper calls use a fresh ledger, not appended duplicate rows.
        Path(written.decision_path).unlink()
        self.assertEqual(len(rows), 4)
        return rows

    def test_actual_lane_serializer_to_composer_to_persistent_kelly_preserves_ensemble_yes_receipt(self):
        from bot.forward_composed_paper import compose_forward_rows
        from bot.persistent_composed_paper import consume_persistent_composed_paper
        from bot.strategies.enhanced import KellySizer
        from datetime import datetime
        for high, action in ((88, "BUY_YES"), (72, "BUY_NO")):
            with self.subTest(action=action):
                raw, weather, ensemble = self.producer(high)
                rows = self.lane_rows(raw)
                evidence = ensemble["probability_evidence"]
                for policy in ("control_stable", "shadow_pre_risk_picker"):
                    lane = next(r for r in rows if r["policy"] == policy)
                    self.assertEqual(lane["action"], action)
                    self.assertEqual(lane["model_probability"], ensemble["model_probability"])
                    self.assertEqual(lane.get("probability_evidence"), evidence)
                _, intents = compose_forward_rows(rows)
                intent = next(i for i in intents if i["lane_id"] == "forward_composed_paper")
                self.assertEqual(intent["source_context"].get("probability_evidence"), evidence)
                self.assertNotEqual(evidence, weather["probability_evidence"])
                account = self.root / action / "paper"
                params = dict(saved_config_path=ROOT / "lane_compositions" / "pre_risk_router_guard_kelly.yaml",
                    lane_rows=rows, cohort_id="probability-evidence",
                    publisher_state={"publisher": {"runtime_kind": "collector", "instance_id": "owner"},
                        "latest_snapshot": {"snapshot_id": "snapshot", "publisher_runtime": "collector",
                            "publisher_instance_id": "owner", "published_at": STAMP}},
                    paper_root=account,
                    protected_roots={k: self.root / k / "paper" for k in ("stable", "beta", "live")},
                    account_config={"starting_balance": 100.0, "strategy": {
                        "enable_ai": False, "enable_news": False, "enable_social": False},
                        "risk": {"kelly_fraction": 0.5, "max_bet_pct": 0.1}},
                    enabled=True, now=datetime.fromisoformat(STAMP))
                report = consume_persistent_composed_paper(**params)
                self.assertEqual(report["decisions"][0]["status"], "opened", report)
                session_path, = account.glob("sim_*.json")
                session = json.loads(session_path.read_text())
                trade, = session["trades"]
                selected = ensemble["model_probability"] if action == "BUY_YES" else 1 - ensemble["model_probability"]
                self.assertAlmostEqual(trade["model_probability"], selected, places=4)
                self.assertEqual(trade["signals"].get("probability_evidence"), evidence)
                self.assertEqual(trade["signals"]["probability_evidence"]["calibration_status"], "not_established")
                self.assertEqual(trade["position_size"], KellySizer(kelly_fraction=0.5, max_bet_pct=0.1).calculate(selected, 0.45, 100))
                before = session_path.read_bytes()
                self.assertEqual(consume_persistent_composed_paper(**params)["decisions"][0]["status"], "already_processed")
                self.assertEqual(session_path.read_bytes(), before)
                legacy_raw = copy.deepcopy(raw)
                legacy_raw["decision_artifact"]["strategy_signal"].pop("probability_evidence")
                legacy_rows = self.lane_rows(legacy_raw)
                for recorded, legacy in zip(rows, legacy_rows):
                    self.assertEqual({k: v for k, v in recorded.items() if k != "probability_evidence"}, legacy)
                # Transport-only enrichment cannot change an existing no-config
                # journal identity, in either the enriched or legacy direction.
                legacy_params = {**params, "lane_rows": legacy_rows}
                self.assertEqual(consume_persistent_composed_paper(**legacy_params)["decisions"][0]["status"], "already_processed")
                self.assertEqual(session_path.read_bytes(), before)
                legacy_account = self.root / (action + "-legacy") / "paper"
                legacy_report = consume_persistent_composed_paper(**{**legacy_params, "paper_root": legacy_account})
                self.assertEqual(legacy_report["decisions"][0]["status"], "opened")
                legacy_session_path, = legacy_account.glob("sim_*.json")
                legacy_trade, = json.loads(legacy_session_path.read_text())["trades"]
                for key in ("model_probability", "position_size", "entry_price"):
                    self.assertEqual(legacy_trade[key], trade[key])
                self.assertNotIn("probability_evidence", legacy_trade["signals"])

    def test_collector_omits_invalid_or_absent_weather_receipts_without_changing_numbers(self):
        row, weather, _ = self.producer()
        baseline = self.snapshot(row)
        malformed = [("yes_probability", value) for value in (None, True, "0.95", float("nan"), float("inf"), 0.2, -0.1, 1.1)]
        malformed += [("schema_version", True), ("basis", "heuristic_weighted_ensemble_v1"),
                      ("calibration_status", "calibrated"), ("fallback_reason", [])]
        market = SimpleNamespace(id=MARKET_ID, question=QUESTION, metadata={})
        for field, value in malformed + [(None, None)]:
            with self.subTest(field=field, value=value):
                artifact = copy.deepcopy(row["decision_artifact"])
                live = artifact["strategy_signal"]["signal_details"]["live"]
                if field is None:
                    live.pop("probability_evidence")
                else:
                    live["probability_evidence"][field] = value
                snapshot = self.lab._build_weather_source_snapshot(artifact, market)
                self.assertNotIn("probability_evidence", snapshot["source_signal"])
                self.assertEqual(snapshot["predicted_prob"], baseline["predicted_prob"])
                self.assertEqual(snapshot["confidence"], baseline["confidence"])
                self.assertEqual(snapshot["forecast"], baseline["forecast"])

    def test_lane_serializer_omits_malformed_receipts_and_validates_each_actual_yes_probability(self):
        raw, _, ensemble = self.producer()
        baseline = self.lane_rows(raw)
        malformed = [("yes_probability", value) for value in (None, True, "0.8167", float("nan"), float("inf"), 0.2, -0.1, 1.1)]
        malformed += [("schema_version", True), ("basis", {}), ("calibration_status", "calibrated")]
        for field, value in malformed + [(None, None)]:
            with self.subTest(field=field, value=value):
                changed = copy.deepcopy(raw)
                receipt = changed["decision_artifact"]["strategy_signal"]
                if field is None:
                    receipt.pop("probability_evidence")
                else:
                    receipt["probability_evidence"][field] = value
                rows = self.lane_rows(changed)
                for before, after in zip(baseline, rows):
                    self.assertNotIn("probability_evidence", after)
                    self.assertEqual({k: v for k, v in before.items() if k != "probability_evidence"}, after)
        # Root probability and recorded pre-risk probability can diverge after
        # wallet processing: attach only to the row it actually describes.
        raw["model_probability"] = 0.99
        rows = self.lane_rows(raw)
        control = next(r for r in rows if r["policy"] == "control_stable")
        picker = next(r for r in rows if r["policy"] == "shadow_pre_risk_picker")
        self.assertEqual(control["model_probability"], 0.99)
        self.assertNotIn("probability_evidence", control)
        self.assertEqual(picker["probability_evidence"], ensemble["probability_evidence"])

    def test_replay_receipt_nested_allowlist_preserves_guards_and_safe_optional_scalars(self):
        from bot.replay_decision_input import build_replay_decision_input_v1
        raw, weather, _ = self.producer()
        strict = self.strict_envelope(raw)
        path = "decision_artifact.source_context.data.weather_source_snapshot.source_signal.probability_evidence"
        for container in ("receipt", "location"):
            for field, code, category in (("provider_debug", "unknown_unallowlisted_input_field", "unallowlisted"),
                                           ("finalOutcome", "forbidden_outcome_or_future_field", "outcome_or_future"),
                                           ("modelProbability", "forbidden_recorded_decision_field", "recorded_decision")):
                with self.subTest(container=container, field=field):
                    row = copy.deepcopy(strict)
                    receipt = self.snapshot(row)["source_signal"]["probability_evidence"]
                    target = receipt if container == "receipt" else receipt["location_provenance"]
                    target[field] = "NEVER_COPY"
                    field_path = path + (".location_provenance" if container == "location" else "") + "." + field
                    rejected = build_replay_decision_input_v1(row, strict=True)
                    self.assertFalse(rejected.ok)
                    self.assertIn((code, field_path), {(e.code, e.path) for e in rejected.errors})
                    cleaned = build_replay_decision_input_v1(row)
                    self.assertTrue(cleaned.ok, cleaned.errors)
                    signal = cleaned.record["source_inputs"]["source_context"]["data"]["weather_source_snapshot"]["source_signal"]
                    self.assertEqual(signal["probability_evidence"], weather["probability_evidence"])
                    self.assertIn({"path": field_path, "category": category}, cleaned.record["sanitization"]["omitted_fields"])
        row = copy.deepcopy(strict)
        receipt = self.snapshot(row)["source_signal"]["probability_evidence"]
        receipt["source_id"] = {"finalOutcome": "NEVER_COPY"}
        receipt["location_provenance"]["latitude"] = {"settlementTimestamp": "NEVER_COPY"}
        rejected = build_replay_decision_input_v1(row, strict=True)
        self.assertFalse(rejected.ok)
        self.assertTrue(any(e.code == "forbidden_outcome_or_future_field" for e in rejected.errors))
        cleaned = build_replay_decision_input_v1(row)
        self.assertTrue(cleaned.ok, cleaned.errors)
        self.assertNotIn("NEVER_COPY", json.dumps(cleaned.record))
        signal = cleaned.record["source_inputs"]["source_context"]["data"]["weather_source_snapshot"]["source_signal"]
        self.assertNotIn("source_id", signal["probability_evidence"])
        self.assertNotIn("latitude", signal["probability_evidence"]["location_provenance"])


if __name__ == "__main__":
    unittest.main()
