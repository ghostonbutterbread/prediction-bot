"""Recorded query geography must not be overwritten by static contract/ticker labels."""
import copy
from functools import partial
import unittest
from unittest.mock import patch

from bot.feeds.weather_capture_store import WeatherCaptureStore
from bot.feeds.weather_pro import ProWeatherEngine

import test_probability_evidence_projection as fixtures
from bot.replay_decision_input import build_replay_decision_input_v1, verify_replay_decision_input_record_v1


class WeatherLabLocationIdentityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProbabilityEvidenceProjectionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        # The shared fixture clears the environment; inject its own scratch store
        # at its weather-engine construction seam, not a global collector root.
        store = WeatherCaptureStore(storage_root=self.fixture.root)
        factory = patch.object(fixtures, "ProWeatherEngine",
                               partial(ProWeatherEngine, capture_store=store))
        factory.start()
        self.addCleanup(factory.stop)

    def sealed(self, **kwargs):
        row, weather, _ = self.fixture.producer(**kwargs)
        before = copy.deepcopy(row)
        result = build_replay_decision_input_v1(row)
        self.assertTrue(result.ok, result.errors)
        self.assertTrue(verify_replay_decision_input_record_v1(result.record))
        self.assertEqual(row, before)
        sealed = result.record["source_inputs"]["source_context"]["data"]["weather_source_snapshot"]
        return sealed, weather

    def test_configured_query_station_remains_inferred_at_lab_and_sealed_root(self):
        sealed, weather = self.sealed(metadata={})
        location = weather["data"]["source_details"][0]["source_metadata"]["forecast_location"]
        self.assertEqual(location["station_origin"], "configured_city_station")
        self.assertEqual(location["station_mapping"], "inferred")
        self.assertEqual(sealed["station_id"], "KLAX")
        self.assertEqual(sealed["station_mapping"], "inferred")
        self.assertEqual(sealed["station_resolution"]["mapping"], "inferred")
        self.assertEqual(sealed["station_resolution"]["source"], "configured_city_station")
        nws = next(s for s in sealed["sources"] if s["source_name"] == "nws")
        self.assertEqual(nws["source_location_basis"], "station")
        self.assertEqual(nws["station_mapping"], "inferred")
        self.assertEqual(nws["source_metadata"]["forecast_location"], location)

    def test_rule_selected_burbank_query_keeps_burbank_root_and_cli_not_ticker_lax(self):
        sealed, weather = self.sealed(metadata={"rules_primary": "Maximum temperature recorded at KBUR according to NWS."})
        location = weather["data"]["source_details"][0]["source_metadata"]["forecast_location"]
        self.assertEqual((location["station_id"], location["station_cli"]), ("KBUR", "BUR"))
        self.assertEqual((location["latitude"], location["longitude"]), (34.2, -118.3))
        self.assertEqual((sealed["station_id"], sealed["station_cli"]), ("KBUR", "BUR"))
        self.assertEqual(sealed["station_mapping"], "exact")
        self.assertEqual(sealed["station_resolution"]["source"], "market_explicit")
        nws = next(s for s in sealed["sources"] if s["source_name"] == "nws")
        self.assertEqual((nws["station_id"], nws["station_cli"]), ("KBUR", "BUR"))
        self.assertEqual(nws["source_location_basis"], "station")
        self.assertEqual(nws["source_metadata"]["forecast_location"], location)

    def test_city_centroid_query_cannot_acquire_ticker_station_or_cli(self):
        sealed, weather = self.sealed(metadata={}, city_only=True)
        self.assertIsNone(sealed.get("station_id"))
        self.assertIsNone(sealed.get("station_cli"))
        self.assertEqual(sealed["station_mapping"], "unknown")
        self.assertEqual(sealed["station_resolution"]["source"], "city_only")
        for source in sealed["sources"] + sealed["source_signal"]["data"]["source_details"]:
            location = source.get("source_metadata", {}).get("forecast_location")
            if location is None:
                continue
            with self.subTest(source=source["source_name"]):
                if source["source_name"].startswith("src_local_"):
                    # This fixture has only global-source mock responses. A failed
                    # city-page attempt retains its own city identity, not the
                    # global query's centroid or the contract's ticker station.
                    self.assertEqual(location["basis"], "city")
                    self.assertFalse(source["scoreable_forecast"])
                    self.assertEqual(source["availability_reason"], "source_forecast_unexpected_error")
                    metadata = source["source_metadata"]
                    self.assertEqual(metadata["adapter_error_type"], "AssertionError")
                    self.assertEqual(len(metadata["collection_attempts"]), 1)
                    self.assertEqual(metadata["collection_attempts"][0]["url"], metadata["forecast_query_url"])
                else:
                    self.assertEqual(location["basis"], "city_centroid")
                self.assertIsNone(location.get("station_id"))
                self.assertIsNone(source.get("station_id"))
                self.assertIsNone(source.get("station_cli"))
                if source.get("scoreable_forecast"):
                    self.assertEqual(source["source_location_basis"], "city_centroid")

    def test_legacy_absent_query_location_keeps_existing_contract_fallback(self):
        from test_replay_decision_input import _snapshot_row
        row, weather, _ = self.fixture.producer()
        for detail in weather["data"]["source_details"]:
            detail.get("source_metadata", {}).pop("forecast_location", None)
            detail.pop("source_location_basis", None)
        weather.pop("probability_evidence", None)
        artifact = {"strategy_signal": {"signal_details": {"live": weather}}}
        market = fixtures.SimpleNamespace(id=fixtures.MARKET_ID, question=fixtures.QUESTION, metadata={})
        snapshot = self.fixture.lab._build_weather_source_snapshot(artifact, market)
        legacy = _snapshot_row()
        legacy["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"] = snapshot
        result = build_replay_decision_input_v1(legacy)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(snapshot["station_id"], "KLAX")
        self.assertEqual(snapshot["station_cli"], "LAX")
        self.assertEqual(snapshot["station_mapping"], "exact")


if __name__ == "__main__":
    unittest.main()
