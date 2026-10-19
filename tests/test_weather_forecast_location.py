"""Location requests are exercised through real feeds; all HTTP is mocked."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import httpx

from unittest.mock import patch

from bot.feeds.weather_capture_store import WeatherCaptureStore
from bot.feeds.weather_pro import CITY_COORDS, NWSFeed, OpenMeteoFeed, OpenWeatherMapFeed, ProWeatherEngine
from bot.weather.registry import WeatherRegistry


TARGET = "2026-10-19"
# Public NOAA /stations/KLAX geometry checked for this repair; not model output.
LAX_COORDS = (-118.38889, 33.93806)  # GeoJSON longitude, latitude
# Synthetic dated city predictor, deliberately not station-associated evidence.
CITY_FORECAST_PAGE = (
    '<div class="city">Los Angeles, CA</div><script>'
    'window.__NUXT__=(function(t){return {weeklyData:[{hi:t,lo:63,'
    'day:{temp:t,date:"2026-10-19T07:00:00-0700"},'
    'night:{temp:63,date:"2026-10-19T19:00:00-0700"}}]}}(85));</script>'
)


def station_payload(station="KLAX", coords=LAX_COORDS):
    return {"geometry": {"type": "Point", "coordinates": list(coords)},
            "properties": {"stationIdentifier": station}}


def forecast_payload(high=72):
    return {"properties": {"periods": [
        {"name": "Monday", "temperature": high, "isDaytime": True,
         "startTime": TARGET + "T06:00:00-07:00", "endTime": TARGET + "T18:00:00-07:00"},
        {"name": "Monday Night", "temperature": 60, "isDaytime": False,
         "startTime": TARGET + "T18:00:00-07:00", "endTime": "2026-10-20T06:00:00-07:00"},
    ]}}


class ForecastLocationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(temporary.cleanup)
        self.capture_store = WeatherCaptureStore(storage_root=temporary.name)

    def client(self, handler):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return client

    def test_nws_queries_station_geometry_not_los_angeles_centroid(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.path == "/stations/KLAX":
                return httpx.Response(200, json=station_payload())
            if request.url.path == "/points/33.93806,-118.38889":
                return httpx.Response(200, json={"properties": {"gridId": "LOX", "gridX": 147, "gridY": 40}})
            if request.url.path == "/gridpoints/LOX/147,40/forecast":
                return httpx.Response(200, json=forecast_payload())
            return httpx.Response(404)

        feed = NWSFeed(capture_store=self.capture_store)
        feed.http.close()
        feed.http = self.client(handler)
        snapshot = feed.get_forecast("los angeles", target_date=TARGET)

        assert snapshot is not None
        self.assertTrue(snapshot.scoreable_forecast, snapshot.availability_reason)
        self.assertEqual(snapshot.high_temp_f, 72)
        self.assertEqual([r.url.path for r in requests], [
            "/stations/KLAX", "/points/33.93806,-118.38889", "/gridpoints/LOX/147,40/forecast",
        ])
        location = snapshot.source_details["forecast_location"]
        self.assertEqual((location["latitude"], location["longitude"]), LAX_COORDS[::-1])
        self.assertNotEqual((location["latitude"], location["longitude"]), CITY_COORDS["los angeles"])
        self.assertEqual(location["station_id"], "KLAX")
        self.assertEqual(location["station_origin"], "configured_city_station")
        self.assertEqual(location["station_mapping"], "inferred")
        self.assertEqual(location["coordinate_source"], "nws_station_metadata")
        self.assertEqual(location["coordinate_url"], "https://api.weather.gov/stations/KLAX")
        self.assertTrue(location["coordinates_fetched_at"])
        self.assertEqual(snapshot.source_details["forecast_query_url"],
                         "https://api.weather.gov/gridpoints/LOX/147,40/forecast")
    def test_open_meteo_requests_station_coordinates_and_records_model_grid_separately(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.host == "api.weather.gov":
                return httpx.Response(200, json=station_payload())
            return httpx.Response(200, json={
                "latitude": 33.95, "longitude": -118.4, "timezone": "America/Los_Angeles",
                "current": {"temperature_2m": 69},
                "hourly": {"time": [TARGET + "T12:00"], "temperature_2m": [72]},
            })

        feed = OpenMeteoFeed(capture_store=self.capture_store)
        feed.http.close()
        feed.http = self.client(handler)
        snapshot = feed.get_forecast("los angeles", target_date=TARGET)

        assert snapshot is not None
        self.assertTrue(snapshot.scoreable_forecast)
        query = requests[-1].url.params
        self.assertEqual((float(query["latitude"]), float(query["longitude"])), LAX_COORDS[::-1])
        location = snapshot.source_details["forecast_location"]
        self.assertEqual(location["station_id"], "KLAX")
        self.assertEqual(location["station_origin"], "configured_city_station")
        self.assertEqual(snapshot.source_details["response_latitude"], 33.95)
        self.assertEqual(snapshot.source_details["response_longitude"], -118.4)
    def engine(self, handler, *, city_forecast=False):
        engine = ProWeatherEngine(capture_store=self.capture_store)
        self.addCleanup(engine.close)
        if city_forecast:
            engine.registry = WeatherRegistry.from_file()
            [source] = engine.registry.collection_sources("los angeles")
            # Explicit adapter fixture; do not depend on concurrent registry edits.
            source["collection_adapter"] = "fox_nuxt_daily_v1"
            registry = patch.object(engine.registry, "collection_sources", return_value=[source])
            registry.start()
            self.addCleanup(registry.stop)
            original = handler
            def city_handler(request):
                if str(request.url) == source["url"]:
                    original(request)  # Preserve the test's complete request log.
                    return httpx.Response(200, text=CITY_FORECAST_PAGE)
                return original(request)
            handler = city_handler
        for feed in (engine.nws, engine.open_meteo, engine.owm):
            feed.http.close()
            feed.http = self.client(handler)
        return engine

    def assert_city_collection_only(self, signal, requests, reason, *, collected_requests=None):
        self.assertIsInstance(signal, dict)
        self.assertTrue(signal["collection_only"])
        self.assertIs(signal["prediction_available"], False)
        self.assertEqual(signal["confidence"], 0)
        self.assertNotIn("predicted_prob", signal)
        self.assertNotIn("direction", signal)
        self.assertEqual(signal["availability_reason"], "no_eligible_forecast_sources")
        self.assertEqual(signal["data"]["sources"], [])
        sources = signal["data"]["source_details"]
        self.assertTrue(sources)
        self.assertTrue(all(not row["scoreable_forecast"] for row in sources))
        local = next(row for row in sources if row["source_id"] == "src_local_kttv_weather")
        self.assertEqual(local["availability_reason"], reason)
        self.assertEqual(local["forecast_high"], 85)
        metadata = local["source_metadata"]
        association = metadata["market_station_association"]
        self.assertEqual(association["station_origin"], "market_explicit")
        self.assertIsNone(association.get("station_id"))
        if reason == "conflicting_explicit_stations":
            self.assertEqual(association["availability_reason"], reason)
        self.assertEqual(metadata["forecast_location"]["basis"], "city")
        self.assertNotIn("station_id", metadata["forecast_location"])
        self.assertEqual(metadata["collection_attempts"][0]["http_status"], 200)
        self.assertEqual(self.capture_store.read_verified(metadata["capture"]), CITY_FORECAST_PAGE.encode())
        receipt = json.loads(self.capture_store.read_receipt_verified(signal["data"]["collection_receipt"]))
        retained = next(row for row in receipt["sources"] if row["source_id"] == local["source_id"])
        self.assertEqual(retained["source_metadata"]["capture"], metadata["capture"])
        self.assertEqual(retained["source_metadata"]["collection_attempts"], metadata["collection_attempts"])
        self.assertEqual(retained["availability_reason"], reason)
        # Equivalent unresolved identities may legitimately reuse a retained
        # collection; a warm *configured* forecast still must not score them.
        collected = requests if collected_requests is None else collected_requests
        self.assertTrue(any(str(r.url) == metadata["forecast_query_url"] for r in collected))
        # Current station observations may still be collected, never forecasts
        # or station-geometry queries for an unresolved/conflicting association.
        self.assertTrue(all(r.url.path.endswith("/observations/latest") for r in requests
                            if r.url.host == "api.weather.gov"))
        self.assertFalse(any(r.url.host == "api.open-meteo.com" for r in requests))
        for source in sources:
            if source["source_id"] in {"nws", "open_meteo"}:
                self.assertEqual(source["availability_reason"], reason)
            if source.get("evidence_type") == "observation":
                self.assertFalse(source["scoreable_forecast"])

    def test_conflicting_explicit_metadata_aliases_collect_city_without_scoring(self):
        for metadata in (
            {"station_id": "KBUR", "settlement_station": "KLAX"},
            {"station_id": "KBUR", "station_cli": "LAX"},
            {"station_id": "KBUR", "metadata": {"station_id": "KLAX"}},
            {"weather_context": {"station_id": "KBUR"}, "metadata": {"station_cli": "LAX"}},
        ):
            with self.subTest(metadata=metadata):
                requests = []
                def handler(request):
                    requests.append(request)
                    if request.url.path.startswith("/stations/"):
                        payload = station_payload(request.url.path.rsplit("/", 1)[-1])
                    elif request.url.path.startswith("/points/"):
                        payload = {"properties": {"gridId": "LOX", "gridX": 1, "gridY": 1}}
                    elif request.url.path.startswith("/gridpoints/"):
                        payload = forecast_payload()
                    else:
                        payload = {"hourly": {"time": [TARGET + "T12:00"], "temperature_2m": [72]},
                                   "current": {"temperature_2m": 69}}
                    return httpx.Response(200, json=payload)
                engine = self.engine(handler, city_forecast=True)
                signal = engine._score_temperature_market(
                    "Will Los Angeles high temperature be above 80° on Oct 19, 2026?", .5,
                    market_date=TARGET, market_metadata=metadata,
                )
                self.assert_city_collection_only(signal, requests, "conflicting_explicit_stations")

    def test_unparseable_explicit_metadata_cannot_use_configured_forecast_or_warm_cache(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.path == "/stations/KLAX":
                payload = station_payload()
            elif request.url.path.startswith("/points/"):
                payload = {"properties": {"gridId": "LOX", "gridX": 1, "gridY": 1}}
            elif request.url.path.startswith("/gridpoints/"):
                payload = forecast_payload()
            elif request.url.host == "api.open-meteo.com":
                payload = {"hourly": {"time": [TARGET + "T12:00"], "temperature_2m": [72]},
                           "current": {"temperature_2m": 69}}
            else:
                payload = {"properties": {}}
            return httpx.Response(200, json=payload)

        engine = self.engine(handler, city_forecast=True)
        question = "Will Los Angeles high temperature be above 80° on Oct 19, 2026?"
        # Absent station metadata remains a usable, inferred configured forecast.
        self.assertIsNotNone(engine._score_temperature_market(question, .5, market_date=TARGET))
        cases = (
            ({"station_id": "not-a-station"}, "explicit_station_unresolved"),
            ({"station_cli": "not-a-station"}, "explicit_station_unresolved"),
            ({"weather_context": {"station_id": "not-a-station"}}, "explicit_station_unresolved"),
            ({"station_id": "KBUR", "metadata": {"station_id": "not-a-station"}}, "conflicting_explicit_stations"),
            ({"station_id": "not-a-station", "rules_primary": "Recorded at CLILAX"}, "conflicting_explicit_stations"),
        )
        for metadata, reason in cases:
            with self.subTest(metadata=metadata):
                before = len(requests)
                signal = engine._score_temperature_market(
                    question, .5, market_date=TARGET, market_metadata=metadata,
                )
                self.assert_city_collection_only(signal, requests[before:], reason, collected_requests=requests)
                result = list(engine._cache.values())[-1][0]
                for snapshot in result.details["source_snapshots"][:2]:
                    self.assertFalse(snapshot.scoreable_forecast)
                    self.assertEqual(snapshot.availability_reason, reason)
                    location = snapshot.source_details["forecast_location"]
                    self.assertEqual(location["station_origin"], "market_explicit")
                    self.assertIsNone(location.get("station_id"))
                    self.assertNotIn("latitude", location)

    def test_market_explicit_station_changes_real_queries_and_engine_cache_identity(self):
        requests = []
        # KBUR point and grids are synthetic distinct-location test values.
        def handler(request):
            requests.append(request)
            path = request.url.path
            if path == "/stations/KLAX":
                payload = station_payload()
            elif path == "/stations/KBUR":
                payload = station_payload("KBUR", (-118.3, 34.2))
            elif path.startswith("/points/"):
                payload = {"properties": {"gridId": "LOX", "gridX": 1 if "33.93806" in path else 2, "gridY": 1}}
            elif path.startswith("/gridpoints/"):
                payload = forecast_payload(72 if "/1,1/" in path else 88)
            elif request.url.host == "api.open-meteo.com":
                temp = 72 if float(request.url.params["latitude"]) == LAX_COORDS[1] else 88
                payload = {"hourly": {"time": [TARGET + "T12:00"], "temperature_2m": [temp]},
                           "current": {"temperature_2m": 69}}
            elif path.endswith("/observations/latest"):
                payload = {"properties": {}}
            else:
                return httpx.Response(404)
            return httpx.Response(200, json=payload)

        engine = self.engine(handler)
        question = "Will Los Angeles high temperature be above 80° on Oct 19, 2026?"
        configured = engine._score_temperature_market(question, .5, market_date=TARGET)
        explicit = engine._score_temperature_market(
            question, .5, market_date=TARGET, market_metadata={"station_id": "KBUR"},
        )
        assert configured is not None and explicit is not None
        self.assertEqual(configured["data"]["predicted_temp"], 72)
        self.assertEqual(explicit["data"]["predicted_temp"], 88)
        self.assertEqual(explicit["data"]["settlement_source"], "unknown")
        for source in explicit["data"]["source_details"]:
            if source.get("scoreable_forecast"):
                self.assertEqual(source["source_location_basis"], "station")
                location = source["source_metadata"]["forecast_location"]
                self.assertEqual(location["station_id"], "KBUR")
                self.assertEqual(location["station_origin"], "market_explicit")
        self.assertIn("/points/34.2,-118.3", [r.url.path for r in requests])
        before = len(requests)
        again = engine._score_temperature_market(
            question, .5, market_date=TARGET, market_metadata={"station_id": "KBUR"},
        )
        assert again is not None
        self.assertEqual(again["data"]["predicted_temp"], 88)
        self.assertEqual(len(requests), before)
    def test_contract_cli_rules_are_market_explicit_not_configured_station_proof(self):
        def handler(request):
            if request.url.path == "/stations/KLAX":
                payload = station_payload()
            elif request.url.path.startswith("/points/"):
                payload = {"properties": {"gridId": "LOX", "gridX": 1, "gridY": 1}}
            elif request.url.path.startswith("/gridpoints/"):
                payload = forecast_payload()
            elif request.url.host == "api.open-meteo.com":
                payload = {"hourly": {"time": [TARGET + "T12:00"], "temperature_2m": [72]},
                           "current": {"temperature_2m": 69}}
            else:
                payload = {"properties": {}}
            return httpx.Response(200, json=payload)

        engine = self.engine(handler)
        question = "Will Los Angeles high temperature be above 80° on Oct 19, 2026?"
        for provider, expected in (("The Weather Company", "the_weather_company"), ("NWS", "nws")):
            signal = engine.score_temperature_market_with_context(
                question, .5, category="KXHIGHLAX-26OCT19-T80",
                market_metadata={"rules_primary": f"The maximum recorded at CLILAX is above 80°F according to {provider}."},
            )
            assert signal is not None
            self.assertEqual(signal["data"]["settlement_source"], expected)
            self.assertEqual(signal["data"]["forecast_driver"], "nws")
            for source in signal["data"]["source_details"]:
                if source.get("scoreable_forecast"):
                    location = source["source_metadata"]["forecast_location"]
                    self.assertEqual(location["station_id"], "KLAX")
                    self.assertEqual(location["station_origin"], "market_explicit")
                    self.assertEqual(location["station_mapping"], "exact")
                    self.assertEqual(location["station_evidence_field"], "rules_primary")
    def test_unresolved_explicit_cli_cannot_fall_back_to_city_forecast(self):
        requests = []
        def handler(request):
            requests.append(request)
            if request.url.path.startswith("/points/"):
                payload = {"properties": {"gridId": "LOX", "gridX": 1, "gridY": 1}}
            elif request.url.path.startswith("/gridpoints/"):
                payload = forecast_payload()
            elif request.url.host == "api.open-meteo.com":
                payload = {"hourly": {"time": [TARGET + "T12:00"], "temperature_2m": [72]},
                           "current": {"temperature_2m": 69}}
            else:
                payload = {"properties": {}}
            return httpx.Response(200, json=payload)
        engine = self.engine(handler, city_forecast=True)
        signal = engine._score_temperature_market(
            "Will Los Angeles high temperature be above 80°?", .5, market_date=TARGET,
            market_metadata={"rules_primary": "The maximum recorded at CLIZZZ is above 80°F."},
        )
        self.assert_city_collection_only(signal, requests, "explicit_station_unresolved")
        result = next(iter(engine._cache.values()))[0]
        for snapshot in result.details["source_snapshots"][:2]:
            self.assertEqual(snapshot.availability_reason, "explicit_station_unresolved")
            self.assertEqual(snapshot.source_details["forecast_location"]["station_cli"], "ZZZ")
            self.assertEqual(snapshot.source_details["forecast_location"]["station_mapping"], "unknown")
            self.assertNotIn("latitude", snapshot.source_details["forecast_location"])
    def test_conflicting_explicit_stations_do_not_choose_first_or_reuse_city_cache(self):
        requests = []
        def handler(request):
            requests.append(request)
            if request.url.path.startswith("/stations/") and not request.url.path.endswith("/latest"):
                return httpx.Response(200, json=station_payload())
            if request.url.path.startswith("/points/"):
                return httpx.Response(200, json={"properties": {"gridId": "LOX", "gridX": 1, "gridY": 1}})
            if request.url.path.startswith("/gridpoints/"):
                return httpx.Response(200, json=forecast_payload())
            return httpx.Response(200, json={"properties": {}, "current": {"temperature_2m": 69},
                                           "hourly": {"time": [TARGET + "T12:00"], "temperature_2m": [72]}})
        engine = self.engine(handler, city_forecast=True)
        question = "Will Los Angeles high temperature be above 80°?"
        self.assertIsNotNone(engine._score_temperature_market(question, .5, market_date=TARGET))
        for metadata in (
            {"rules_primary": "Recorded at CLILAX", "rules_secondary": "Recorded at CLIPHX"},
            {"station_id": "KLAX", "rules_primary": "Recorded at CLIPHX"},
            {"rules_primary": "Recorded at CLILAX or KPHX"},
        ):
            with self.subTest(metadata=metadata):
                before = len(requests)
                signal = engine._score_temperature_market(question, .5, market_date=TARGET, market_metadata=metadata)
                self.assert_city_collection_only(signal, requests[before:], "conflicting_explicit_stations",
                                                 collected_requests=requests)
        result = list(engine._cache.values())[-1][0]
        self.assertEqual(result.details["source_snapshots"][0].availability_reason, "conflicting_explicit_stations")
    def test_optional_openweathermap_does_not_reintroduce_city_centroid_into_station_blend(self):
        requests = []
        def handler(request):
            requests.append(request)
            if request.url.host == "api.weather.gov":
                return httpx.Response(200, json=station_payload())
            return httpx.Response(200, json={"list": [
                {"main": {"temp": 72}, "wind": {"speed": 2}, "dt_txt": TARGET + " 12:00:00"},
            ]})
        feed = OpenWeatherMapFeed(api_key="test-only-placeholder")
        feed.http.close()
        feed.http = self.client(handler)
        snapshot = feed.get_forecast("los angeles")
        assert snapshot is not None
        query = requests[-1].url.params
        self.assertEqual((float(query["lat"]), float(query["lon"])), LAX_COORDS[::-1])
        self.assertEqual(snapshot.source_details["forecast_location"]["station_id"], "KLAX")
    def test_forecast_request_failure_retains_station_query_provenance(self):
        def handler(request):
            if request.url.path == "/stations/KLAX":
                return httpx.Response(200, json=station_payload())
            if request.url.path.startswith("/points/"):
                return httpx.Response(200, json={"properties": {"gridId": "LOX", "gridX": 1, "gridY": 1}})
            return httpx.Response(503)
        for cls in (NWSFeed, OpenMeteoFeed, OpenWeatherMapFeed):
            with self.subTest(feed=cls.__name__):
                feed = cls(api_key="test-only-placeholder") if cls is OpenWeatherMapFeed else cls(capture_store=self.capture_store)
                feed.http.close()
                feed.http = self.client(handler)
                snapshot = feed.get_forecast("los angeles")
                self.assertIsNotNone(snapshot)
                assert snapshot is not None
                self.assertFalse(snapshot.scoreable_forecast)
                self.assertEqual(snapshot.availability_reason, "source_forecast_request_failed")
                self.assertIn("forecast_location", snapshot.source_details)
                self.assertEqual(snapshot.source_details["forecast_location"]["station_id"], "KLAX")
                self.assertIn("forecast_query_url", snapshot.source_details)
    def test_unavailable_target_still_records_the_location_actually_queried(self):
        def handler(request):
            if request.url.path == "/stations/KLAX":
                payload = station_payload()
            elif request.url.path.startswith("/points/"):
                payload = {"properties": {"gridId": "LOX", "gridX": 1, "gridY": 1}}
            elif request.url.path.startswith("/gridpoints/"):
                payload = forecast_payload()
            else:
                payload = {"hourly": {"time": [TARGET + "T12:00"], "temperature_2m": [72]}}
            return httpx.Response(200, json=payload)
        for cls in (NWSFeed, OpenMeteoFeed):
            with self.subTest(feed=cls.__name__):
                feed = cls(capture_store=self.capture_store)
                feed.http.close()
                feed.http = self.client(handler)
                snapshot = feed.get_forecast("los angeles", target_date="2026-10-25")
                assert snapshot is not None
                self.assertFalse(snapshot.scoreable_forecast)
                self.assertEqual(snapshot.availability_reason, "target_date_not_in_source_forecast")
                self.assertIn("forecast_location", snapshot.source_details)
                self.assertEqual(snapshot.source_details["forecast_location"]["station_id"], "KLAX")
    def test_bad_station_metadata_is_unavailable_without_forecast_or_centroid_requests(self):
        invalid_payloads = (
            [], {"geometry": []}, {"geometry": {"type": "Point"}, "properties": []},
            station_payload("KPHX"), station_payload(coords=(float("nan"), 33.9)),
            station_payload(coords=(True, 33.9)), station_payload(coords=(-118.3, 100)),
            station_payload(coords=("-118.3", "33.9")), {"properties": {"stationIdentifier": "KLAX"}},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                requests = []
                def handler(request):
                    requests.append(request)
                    # json= rejects NaN, so return encoded JSON for that case.
                    import json
                    return httpx.Response(200, text=json.dumps(payload))
                feed = NWSFeed(capture_store=self.capture_store)
                feed.http.close()
                feed.http = self.client(handler)
                try:
                    snapshot = feed.get_forecast("los angeles", target_date=TARGET)
                except Exception as exc:
                    self.fail(f"Station metadata must fail unavailable, not raise {type(exc).__name__}")
                assert snapshot is not None
                self.assertFalse(snapshot.scoreable_forecast)
                self.assertEqual(snapshot.availability_reason, "station_coordinates_unavailable")
                self.assertEqual([r.url.path for r in requests], ["/stations/KLAX"])
                self.assertEqual(feed._station_location_cache, {})
                self.assertEqual(feed._points_cache, {})


    def test_no_station_city_only_forecasts_remain_available_and_truthfully_labeled(self):
        requests = []
        def handler(request):
            requests.append(request)
            if request.url.path.startswith("/points/"):
                payload = {"properties": {"gridId": "LOX", "gridX": 1, "gridY": 1}}
            elif request.url.path.startswith("/gridpoints/"):
                payload = forecast_payload()
            else:
                payload = {"hourly": {"time": [TARGET + "T12:00"], "temperature_2m": [72]},
                           "current": {"temperature_2m": 69}}
            return httpx.Response(200, json=payload)
        engine = self.engine(handler)
        # A configured city coordinate without a configured station is a
        # supported compatibility case, not an exact station forecast.
        with patch.dict("bot.feeds.weather_pro.CITY_NWS", {}, clear=True):
            forecast = engine.get_forecast("los angeles", target_date=TARGET)
        assert forecast is not None
        self.assertEqual(set(forecast.sources_used), {"nws", "open-meteo"})
        self.assertFalse(any("/stations/" in r.url.path for r in requests))
        for source in engine._source_contribution_details(forecast):
            if source.get("scoreable_forecast"):
                self.assertEqual(source["source_location_basis"], "city_centroid")
                self.assertNotIn("station_id", source)
                location = source["source_metadata"]["forecast_location"]
                self.assertEqual((location["latitude"], location["longitude"]), CITY_COORDS["los angeles"])
                self.assertEqual(location["station_origin"], "city_only")
                self.assertEqual(location["station_mapping"], "unknown")

    def test_station_metadata_or_points_failure_never_uses_old_city_grid_cache(self):
        for fail_station in (True, False):
            with self.subTest(fail_station=fail_station):
                requests = []
                def handler(request):
                    requests.append(request)
                    if request.url.path == "/stations/KLAX" and not fail_station:
                        return httpx.Response(200, json=station_payload())
                    return httpx.Response(503)
                feed = NWSFeed(capture_store=self.capture_store)
                feed.http.close()
                feed.http = self.client(handler)
                feed._points_cache["los angeles"] = (datetime.now(timezone.utc), ("LOX", 154, 44))
                snapshot = feed.get_forecast("los angeles", target_date=TARGET)
                assert snapshot is not None
                self.assertFalse(snapshot.scoreable_forecast)
                expected = "station_coordinates_unavailable" if fail_station else "source_forecast_grid_unavailable"
                self.assertEqual(snapshot.availability_reason, expected)
                self.assertFalse(any("/gridpoints/" in r.url.path for r in requests))
                self.assertEqual(snapshot.source_details["forecast_location"]["station_id"], "KLAX")

    def test_coordinate_cache_expiry_resolves_new_location_without_reusing_prior_grid(self):
        requests = []
        coordinates = LAX_COORDS
        def handler(request):
            requests.append(request)
            if request.url.path == "/stations/KLAX":
                payload = station_payload(coords=coordinates)
            elif request.url.path.startswith("/points/"):
                payload = {"properties": {"gridId": "LOX", "gridX": 1 if "33.93806" in request.url.path else 2, "gridY": 1}}
            else:
                payload = forecast_payload(72 if "/1,1/" in request.url.path else 88)
            return httpx.Response(200, json=payload)
        feed = NWSFeed(capture_store=self.capture_store)
        feed.http.close()
        feed.http = self.client(handler)
        first = feed.get_forecast("los angeles", target_date=TARGET)
        again = feed.get_forecast("los angeles", target_date=TARGET)
        assert first is not None and again is not None
        self.assertEqual(first.high_temp_f, 72)
        self.assertEqual(again.high_temp_f, 72)
        self.assertEqual(sum(r.url.path.startswith("/stations/") for r in requests), 1)
        self.assertEqual(sum(r.url.path.startswith("/points/") for r in requests), 1)
        _, cached = feed._station_location_cache["KLAX"]
        feed._station_location_cache["KLAX"] = (datetime.now(timezone.utc) - timedelta(hours=7), cached)
        coordinates = (-118.3, 34.2)  # Synthetic moved metadata, not an actual station move.
        changed = feed.get_forecast("los angeles", target_date=TARGET)
        assert changed is not None
        self.assertEqual(changed.high_temp_f, 88)
        self.assertIn("/points/34.2,-118.3", [r.url.path for r in requests])
    def test_nws_follows_official_point_precision_redirect_for_station_geometry(self):
        requests = []
        def handler(request):
            requests.append(request)
            path = request.url.path
            if path == "/stations/KLAX":
                return httpx.Response(200, json=station_payload())
            if path == "/points/33.93806,-118.38889":
                return httpx.Response(301, headers={"Location": "/points/33.9381,-118.3889"})
            if path == "/points/33.9381,-118.3889":
                return httpx.Response(200, json={"properties": {"gridId": "LOX", "gridX": 1, "gridY": 1}})
            if path.startswith("/gridpoints/"):
                return httpx.Response(200, json=forecast_payload())
            return httpx.Response(404)
        feed = NWSFeed(capture_store=self.capture_store)
        feed.http.close()
        feed.http = self.client(handler)
        forecast = feed.get_forecast("los angeles", target_date=TARGET)
        assert forecast is not None
        self.assertTrue(forecast.scoreable_forecast, forecast.availability_reason)
        self.assertIn("/points/33.9381,-118.3889", [r.url.path for r in requests])
        location = forecast.source_details["forecast_location"]
        self.assertEqual((location["latitude"], location["longitude"]), LAX_COORDS[::-1])
    def test_malformed_points_metadata_is_unavailable_instead_of_raising_or_using_city_grid(self):
        for payload in ([], {"properties": []}, {"properties": None}):
            with self.subTest(payload=payload):
                def handler(request):
                    return httpx.Response(200, json=station_payload() if request.url.path.startswith("/stations/") else payload)
                feed = NWSFeed(capture_store=self.capture_store)
                feed.http.close()
                feed.http = self.client(handler)
                try:
                    forecast = feed.get_forecast("los angeles", target_date=TARGET)
                except Exception as exc:
                    self.fail(f"Points failure must be unavailable, not raise {type(exc).__name__}")
                assert forecast is not None
                self.assertFalse(forecast.scoreable_forecast)
                self.assertEqual(forecast.availability_reason, "source_forecast_grid_unavailable")
                self.assertEqual(feed._points_cache, {})


if __name__ == "__main__":
    unittest.main()
