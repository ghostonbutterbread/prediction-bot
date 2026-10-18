"""Station-location provenance across real producer, collector and replay boundaries."""
import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from bot.collector_paths import COLLECTOR_ROOT_ENV
from bot.collector_replay_inputs import export_collector_replay_inputs
from bot.feeds.weather_pro import CITY_COORDS, ProWeatherEngine
from bot.prediction_lab import PredictionLab
from bot.replay_decision_input import (
    build_replay_decision_input_v1,
    verify_replay_decision_input_record_v1,
    verify_replay_decision_input_v1,
)
from test_replay_decision_input import _snapshot_row


TARGET = "2026-10-19"
QUESTION = "Will Los Angeles high temperature be above 80° on Oct 19, 2026?"
MARKET_ID = "KXHIGHLAX-26OCT19-T80"
# Public NOAA station geometry used by the location regression, in lat/lon order.
LAX_LAT_LON = (33.93806, -118.38889)


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 10, 18, 19, tzinfo=timezone.utc)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


class WeatherLocationReplayTests(unittest.TestCase):
    def producer_row(self):
        requests = []

        def handler(request):
            requests.append(request)
            path = request.url.path
            if path == "/stations/KLAX":
                payload = {
                    "geometry": {"type": "Point", "coordinates": list(LAX_LAT_LON[::-1])},
                    "properties": {"stationIdentifier": "KLAX"},
                }
            elif path == "/points/33.93806,-118.38889":
                # Grid identity is synthetic; coordinates and station identity are not.
                payload = {"properties": {"gridId": "LOX", "gridX": 1, "gridY": 1}}
            elif path == "/gridpoints/LOX/1,1/forecast":
                payload = {"properties": {"periods": [
                    {"name": "Monday", "temperature": 72, "isDaytime": True,
                     "startTime": TARGET + "T06:00:00-07:00", "endTime": TARGET + "T18:00:00-07:00"},
                    {"name": "Monday Night", "temperature": 60, "isDaytime": False,
                     "startTime": TARGET + "T18:00:00-07:00", "endTime": "2026-10-20T06:00:00-07:00"},
                ]}}
            elif request.url.host == "api.open-meteo.com":
                self.assertEqual(
                    (float(request.url.params["latitude"]), float(request.url.params["longitude"])),
                    LAX_LAT_LON,
                )
                payload = {
                    "latitude": 33.95, "longitude": -118.4, "timezone": "America/Los_Angeles",
                    "utc_offset_seconds": -25200, "current": {"temperature_2m": 69},
                    "hourly": {"time": [TARGET + "T12:00"], "temperature_2m": [72]},
                }
            elif path == "/stations/KLAX/observations/latest":
                payload = {"properties": {}}
            else:
                self.fail(f"Unexpected producer request: {request.url}")
            return httpx.Response(200, json=payload)

        engine = ProWeatherEngine()
        self.addCleanup(engine.close)
        for feed in (engine.nws, engine.open_meteo, engine.owm):
            feed.http.close()
            feed.http = httpx.Client(transport=httpx.MockTransport(handler))
        metadata = {"rules_primary": "Maximum temperature recorded at CLILAX according to NWS."}
        with patch("bot.feeds.weather_pro.datetime", _Clock):
            signal = engine._score_temperature_market(
                QUESTION, .5, market_date=TARGET, market_metadata=metadata,
            )
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal["data"]["predicted_temp"], 72)
        artifact = {"strategy_signal": {"signal_details": {"live": signal}}}
        market = SimpleNamespace(id=MARKET_ID, question=QUESTION, metadata=metadata)
        lab = PredictionLab.__new__(PredictionLab)
        snapshot = lab._build_weather_source_snapshot(artifact, market)
        self.assertIsNotNone(snapshot)
        row = _snapshot_row(patch={
            "collector_artifact_schema_version": 2,
            "market_id": MARKET_ID, "question": QUESTION,
            "observed_at": "2026-10-18T19:00:01Z",
        })
        row["decision_artifact"]["source_context"]["as_of"] = "2026-10-18T19:00:00Z"
        row["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"] = snapshot
        return row, signal, requests

    @staticmethod
    def replay_snapshot(record):
        return record["source_inputs"]["source_context"]["data"]["weather_source_snapshot"]

    def test_producer_station_provenance_survives_collector_and_sealed_json_roundtrip(self):
        row, signal, requests = self.producer_row()
        raw_before = copy.deepcopy(row)
        result = build_replay_decision_input_v1(row)
        self.assertTrue(result.ok, result.errors)
        self.assertTrue(verify_replay_decision_input_v1(result.record, result.canonical_input_json))
        sealed = json.loads(json.dumps(result.record))
        self.assertTrue(verify_replay_decision_input_record_v1(sealed))
        # Exercise the real file exporter too, using only this synthetic row.
        # No historical archive is read or modified.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "collector.jsonl"
            raw_bytes = (json.dumps(row) + "\n").encode()
            archive.write_bytes(raw_bytes)
            with patch.dict(os.environ, {COLLECTOR_ROOT_ENV: str(root)}):
                exported = export_collector_replay_inputs(
                    source_archive=archive,
                    output_dir=root / "data" / "derived_reports" / "location-replay",
                    max_rows=1,
                )
            exported_record = json.loads(exported.records_path.read_text())
            self.assertEqual(exported_record, sealed)
            self.assertTrue(verify_replay_decision_input_record_v1(exported_record))
            self.assertEqual(exported.metadata["accepted_row_count"], 1)
            self.assertEqual(exported.metadata["rejected_row_count"], 0)
            self.assertEqual(archive.read_bytes(), raw_bytes)
        self.assertEqual(row, raw_before)
        produced = {s["source_name"]: s for s in signal["data"]["source_details"] if s.get("scoreable_forecast")}
        sources = {s["source_name"]: s for s in self.replay_snapshot(sealed)["sources"] if s.get("scoreable_forecast")}
        self.assertEqual(set(sources), {"nws", "open-meteo"})
        for name, source in sources.items():
            with self.subTest(source=name):
                location = source["source_metadata"].get("forecast_location")
                self.assertIsNotNone(location, "sealed replay silently lost station-location provenance")
                self.assertEqual(location, produced[name]["source_metadata"]["forecast_location"])
                self.assertEqual((location["latitude"], location["longitude"]), LAX_LAT_LON)
                self.assertNotEqual(LAX_LAT_LON, CITY_COORDS["los angeles"])
                self.assertEqual(location["station_id"], "KLAX")
                self.assertEqual(location["station_cli"], "LAX")
                self.assertEqual(location["station_origin"], "market_explicit")
                self.assertEqual(location["station_mapping"], "exact")
                self.assertEqual(location["station_evidence_field"], "rules_primary")
                self.assertEqual(location["coordinate_source"], "nws_station_metadata")
                self.assertEqual(location["coordinate_url"], "https://api.weather.gov/stations/KLAX")
                self.assertEqual(location["coordinates_fetched_at"], "2026-10-18T19:00:00+00:00")
                self.assertEqual(source["target_mapping"]["source_target_date"], TARGET)
                self.assertEqual(source["forecast_high"], 72)
                self.assertEqual(source["source_metadata"]["forecast_query_url"],
                                 produced[name]["source_metadata"]["forecast_query_url"])
        self.assertEqual(sources["open-meteo"]["source_metadata"].get("response_latitude"), 33.95)
        self.assertEqual(sources["open-meteo"]["source_metadata"].get("response_longitude"), -118.4)
        self.assertIn("/points/33.93806,-118.38889", [r.url.path for r in requests])
        # The source_signal copy is independently allowlisted at the same boundary.
        signal_sources = self.replay_snapshot(sealed)["source_signal"]["data"]["source_details"]
        for source in signal_sources:
            if source.get("scoreable_forecast"):
                self.assertEqual(source["source_metadata"]["forecast_location"],
                                 produced[source["source_name"]]["source_metadata"]["forecast_location"])

    @staticmethod
    def location_row():
        row = _snapshot_row()
        snapshot = row["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]
        source = snapshot["sources"][0]
        # These coordinates are synthetic: this fixture tests the schema, while
        # producer_row above checks the real KLAX identity and corrected geometry.
        source["source_metadata"].update({
            "forecast_location": {
                "station_id": "KNYC", "station_cli": "NYC", "station_origin": "market_explicit",
                "station_mapping": "exact", "station_evidence_field": "rules_primary",
                "city": "nyc", "basis": "station", "latitude": 40.8, "longitude": -73.9,
                "coordinate_source": "nws_station_metadata",
                "coordinate_url": "https://api.weather.gov/stations/KNYC",
                "coordinates_fetched_at": "2026-08-12T14:59:00+00:00",
            },
            "forecast_query_url": "https://api.weather.gov/gridpoints/OKX/33,37/forecast",
            "response_latitude": 40.75, "response_longitude": -73.95,
        })
        snapshot["source_signal"] = {"signal_type": "weather", "data": {"source_details": [copy.deepcopy(source)]}}
        return row

    def test_strict_projection_accepts_only_declared_location_fields(self):
        row = self.location_row()
        snapshot = row["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]
        result = build_replay_decision_input_v1(row, strict=True)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(self.replay_snapshot(result.record)["sources"], snapshot["sources"])
        self.assertEqual(self.replay_snapshot(result.record)["source_signal"], snapshot["source_signal"])
        self.assertTrue(verify_replay_decision_input_record_v1(result.record))

    def test_nested_forbidden_location_fields_are_rejected_or_omitted_and_audited(self):
        root = "decision_artifact.source_context.data.weather_source_snapshot"
        cases = (
            ("finalOutcome", "outcome_or_future", "forbidden_outcome_or_future_field"),
            ("settlementTimestamp", "outcome_or_future", "forbidden_outcome_or_future_field"),
            ("modelProbability", "recorded_decision", "forbidden_recorded_decision_field"),
            ("finalAction", "recorded_decision", "forbidden_recorded_decision_field"),
            ("provider_debug", "unallowlisted", "unknown_unallowlisted_input_field"),
        )
        for container in ("sources", "source_signal"):
            for field, category, code in cases:
                with self.subTest(container=container, field=field):
                    row = self.location_row()
                    snapshot = row["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]
                    if container == "sources":
                        source = snapshot["sources"][0]
                        source_path = f"{root}.sources[0]"
                    else:
                        source = snapshot["source_signal"]["data"]["source_details"][0]
                        source_path = f"{root}.source_signal.data.source_details[0]"
                    location = source["source_metadata"]["forecast_location"]
                    clean_location = copy.deepcopy(location)
                    location[field] = "DO_NOT_COPY_LOCATION_PAYLOAD"
                    path = f"{source_path}.source_metadata.forecast_location.{field}"
                    raw_before = copy.deepcopy(row)
                    strict = build_replay_decision_input_v1(row, strict=True)
                    self.assertFalse(strict.ok)
                    self.assertIsNone(strict.record)
                    self.assertIn((code, path), {(e.code, e.path) for e in strict.errors})
                    result = build_replay_decision_input_v1(row)
                    self.assertTrue(result.ok, result.errors)
                    sealed_snapshot = self.replay_snapshot(result.record)
                    sealed_source = (sealed_snapshot["sources"][0] if container == "sources"
                                     else sealed_snapshot["source_signal"]["data"]["source_details"][0])
                    self.assertEqual(sealed_source["source_metadata"]["forecast_location"], clean_location)
                    self.assertIn({"path": path, "category": category}, result.record["sanitization"]["omitted_fields"])
                    self.assertNotIn("DO_NOT_COPY_LOCATION_PAYLOAD", json.dumps(result.record))
                    self.assertEqual(row, raw_before)
                    self.assertTrue(verify_replay_decision_input_record_v1(result.record))

    def test_nested_location_containers_cannot_smuggle_outcomes_or_arbitrary_dicts(self):
        prefix = "decision_artifact.source_context.data.weather_source_snapshot.sources[0].source_metadata.forecast_location"
        for field in ("latitude", "coordinate_url", "provider_debug"):
            with self.subTest(field=field):
                row = self.location_row()
                location = row["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]["sources"][0]["source_metadata"]["forecast_location"]
                location[field] = {"nested": [{"finalOutcome": "DO_NOT_COPY_NESTED_OUTCOME"}]}
                strict = build_replay_decision_input_v1(row, strict=True)
                self.assertFalse(strict.ok)
                self.assertIn(
                    ("forbidden_outcome_or_future_field", f"{prefix}.{field}.nested[0].finalOutcome"),
                    {(e.code, e.path) for e in strict.errors},
                )
                result = build_replay_decision_input_v1(row)
                self.assertTrue(result.ok, result.errors)
                self.assertNotIn(field, self.replay_snapshot(result.record)["sources"][0]["source_metadata"]["forecast_location"])
                self.assertNotIn("DO_NOT_COPY_NESTED_OUTCOME", json.dumps(result.record))
                omitted = result.record["sanitization"]["omitted_fields"]
                expected = ({"path": f"{prefix}.{field}.nested[0].finalOutcome", "category": "outcome_or_future"}
                            if field == "provider_debug" else {"path": f"{prefix}.{field}", "category": "unallowlisted"})
                self.assertIn(expected, omitted)

    def test_station_location_and_query_changes_invalidate_the_sealed_hash(self):
        result = build_replay_decision_input_v1(self.location_row())
        self.assertTrue(result.ok, result.errors)
        for field, value in (("latitude", 34.2), ("station_id", "KBUR"), ("forecast_query_url", "wrong-query")):
            with self.subTest(field=field):
                record = json.loads(json.dumps(result.record))
                metadata = self.replay_snapshot(record)["sources"][0]["source_metadata"]
                (metadata if field == "forecast_query_url" else metadata["forecast_location"])[field] = value
                self.assertFalse(verify_replay_decision_input_record_v1(record))
                self.assertFalse(verify_replay_decision_input_v1(record, result.canonical_input_json))


if __name__ == "__main__":
    unittest.main()
