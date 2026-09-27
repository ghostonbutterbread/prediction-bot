import unittest
from datetime import datetime, timezone

from bot.feeds.weather_pro import NWSFeed, OpenMeteoFeed, ProWeatherEngine, WeatherSnapshot
from bot.prediction_lab import PredictionLab
from bot.weather.source_scoreboard import extract_source_forecast_observations


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Http:
    def __init__(self, payload):
        self.payload = payload

    def get(self, *args, **kwargs):
        return _Response(self.payload)

    def close(self):
        return None


class ForecastTargetingTests(unittest.TestCase):
    def test_open_meteo_selects_exact_next_day_source_local_forecast(self):
        feed = OpenMeteoFeed()
        feed.http = _Http({
            "timezone": "America/Chicago",
            "utc_offset_seconds": -18000,
            "current": {"temperature_2m": 70},
            "hourly": {
                "time": ["2026-08-01T22:00", "2026-08-02T12:00", "2026-08-02T15:00"],
                "temperature_2m": [71, 74, 82],
            },
        })

        snapshot = feed.get_forecast("austin", target_date="2026-08-02")

        self.assertTrue(snapshot.scoreable_forecast)
        self.assertEqual(snapshot.forecast_date, "2026-08-02")
        self.assertEqual(snapshot.high_temp_f, 82)
        self.assertEqual(snapshot.source_details["target_mapping"]["market_target_date"], "2026-08-02")
        self.assertEqual(snapshot.source_details["target_mapping"]["source_target_date"], "2026-08-02")
        self.assertEqual(snapshot.source_details["source_as_of"], snapshot.fetched_at.isoformat())

    def test_current_day_open_meteo_window_is_unavailable_for_next_day_market(self):
        feed = OpenMeteoFeed()
        feed.http = _Http({
            "timezone": "America/Chicago",
            "current": {"temperature_2m": 70},
            "hourly": {"time": ["2026-08-01T12:00"], "temperature_2m": [80]},
        })

        snapshot = feed.get_forecast("austin", target_date="2026-08-02")

        self.assertFalse(snapshot.scoreable_forecast)
        self.assertEqual(snapshot.availability_reason, "target_date_not_in_source_forecast")
        self.assertIsNone(snapshot.high_temp_f)

    def test_scoring_declines_when_no_exact_target_forecast_is_available(self):
        engine = ProWeatherEngine()
        try:
            engine.get_forecast = lambda *_args, **_kwargs: type("Forecast", (), {
                "sources_used": [], "high_temp_f": None, "low_temp_f": None,
            })()
            result = engine._score_temperature_market(
                "Will Austin high temperature be above 80° on Aug 2, 2026?",
                0.55,
                market_date="2026-08-02",
            )
        finally:
            engine.close()

        self.assertIsNone(result)

    def test_nws_records_offset_aware_source_timezone_for_exact_target(self):
        feed = NWSFeed()
        feed.http = _Http({
            "properties": {"periods": [
                {"number": 1, "name": "Tomorrow", "temperature": 84, "isDaytime": True,
                 "startTime": "2026-08-02T06:00:00-05:00", "endTime": "2026-08-02T18:00:00-05:00"},
                {"number": 2, "name": "Tomorrow Night", "temperature": 69, "isDaytime": False,
                 "startTime": "2026-08-02T18:00:00-05:00", "endTime": "2026-08-03T06:00:00-05:00"},
            ]},
        })
        feed._points_cache["austin"] = (datetime.now(timezone.utc), ("EWX", 152, 91))

        snapshot = feed.get_forecast("austin", target_date="2026-08-02")

        self.assertTrue(snapshot.scoreable_forecast)
        self.assertEqual(snapshot.source_details["target_mapping"]["source_timezone"], "UTC-05:00")

    def test_nws_low_uses_night_ending_on_target_morning(self):
        feed = NWSFeed()
        feed.http = _Http({"properties": {"periods": [
            {"number": 1, "name": "Saturday Night", "temperature": 58, "isDaytime": False,
             "startTime": "2026-09-26T18:00:00-07:00", "endTime": "2026-09-27T06:00:00-07:00"},
            {"number": 2, "name": "Sunday", "temperature": 84, "isDaytime": True,
             "startTime": "2026-09-27T06:00:00-07:00", "endTime": "2026-09-27T18:00:00-07:00"},
            {"number": 3, "name": "Sunday Night", "temperature": 68, "isDaytime": False,
             "startTime": "2026-09-27T18:00:00-07:00", "endTime": "2026-09-28T06:00:00-07:00"},
        ]}})
        feed._points_cache["austin"] = (datetime.now(timezone.utc), ("EWX", 152, 91))

        low = feed.get_forecast("austin", target_date="2026-09-27", measurement_kind="low")
        self.assertTrue(low.scoreable_forecast)
        self.assertEqual(low.low_temp_f, 58)
        self.assertEqual(low.source_details["low_period"]["name"], "Saturday Night")
        self.assertEqual(low.source_details["target_mapping"]["source_period_start"], "2026-09-26T18:00:00-07:00")
        self.assertEqual(low.source_details["target_mapping"]["source_period_end"], "2026-09-27T06:00:00-07:00")

    def test_nws_low_missing_prior_night_does_not_borrow_next_morning(self):
        feed = NWSFeed()
        feed.http = _Http({"properties": {"periods": [
            {"number": 1, "name": "Sunday", "temperature": 84, "isDaytime": True,
             "startTime": "2026-09-27T06:00:00-07:00", "endTime": "2026-09-27T18:00:00-07:00"},
            {"number": 2, "name": "Sunday Night", "temperature": 68, "isDaytime": False,
             "startTime": "2026-09-27T18:00:00-07:00", "endTime": "2026-09-28T06:00:00-07:00"},
        ]}})
        feed._points_cache["austin"] = (datetime.now(timezone.utc), ("EWX", 152, 91))

        low = feed.get_forecast("austin", target_date="2026-09-27", measurement_kind="low")
        self.assertFalse(low.scoreable_forecast)
        self.assertEqual(low.availability_reason, "target_date_missing_low_forecast_period")
        high = feed.get_forecast("austin", target_date="2026-09-27", measurement_kind="high")
        self.assertTrue(high.scoreable_forecast)
        self.assertEqual(high.high_temp_f, 84)

    def test_nws_requested_kind_survives_unrelated_missing_temperature(self):
        feed = NWSFeed()
        feed._points_cache["austin"] = (datetime.now(timezone.utc), ("EWX", 152, 91))
        feed.http = _Http({"properties": {"periods": [
            {"name": "Sunday", "temperature": 84, "isDaytime": True,
             "startTime": "2026-09-27T06:00:00-07:00", "endTime": "2026-09-27T18:00:00-07:00"},
            {"name": "Sunday Night", "temperature": None, "isDaytime": False,
             "startTime": "2026-09-27T18:00:00-07:00", "endTime": "2026-09-28T06:00:00-07:00"},
        ]}})
        high = feed.get_forecast("austin", target_date="2026-09-27", measurement_kind="high")
        self.assertTrue(high.scoreable_forecast)
        self.assertEqual(high.high_temp_f, 84)
        self.assertIsNone(high.low_temp_f)

        feed.http = _Http({"properties": {"periods": [
            {"name": "Saturday Night", "temperature": 58, "isDaytime": False,
             "startTime": "2026-09-26T18:00:00-07:00", "endTime": "2026-09-27T06:00:00-07:00"},
            {"name": "Sunday", "temperature": None, "isDaytime": True,
             "startTime": "2026-09-27T06:00:00-07:00", "endTime": "2026-09-27T18:00:00-07:00"},
        ]}})
        low = feed.get_forecast("austin", target_date="2026-09-27", measurement_kind="low")
        self.assertTrue(low.scoreable_forecast)
        self.assertEqual(low.low_temp_f, 58)
        self.assertIsNone(low.high_temp_f)

    def test_scored_low_uses_morning_period_and_retains_source_period_proof(self):
        engine = ProWeatherEngine()
        try:
            setattr(engine.nws, "http", _Http({"properties": {"periods": [
                {"name": "Saturday Night", "temperature": 58, "isDaytime": False,
                 "startTime": "2026-09-26T18:00:00-07:00", "endTime": "2026-09-27T06:00:00-07:00"},
                {"name": "Sunday", "temperature": None, "isDaytime": True,
                 "startTime": "2026-09-27T06:00:00-07:00", "endTime": "2026-09-27T18:00:00-07:00"},
                {"name": "Sunday Night", "temperature": 68, "isDaytime": False,
                 "startTime": "2026-09-27T18:00:00-07:00", "endTime": "2026-09-28T06:00:00-07:00"},
            ]}}))
            engine.nws._points_cache["austin"] = (datetime.now(timezone.utc), ("EWX", 152, 91))
            setattr(engine.nws, "get_station_observation", lambda city: None)
            engine.open_meteo.get_forecast = lambda *_args, **_kwargs: None
            signal = engine._score_temperature_market(
                "Will Austin low temperature be below 70° on Sep 27, 2026?",
                0.5, market_date="2026-09-27",
            )
            assert signal is not None
            self.assertEqual(signal["data"]["predicted_temp"], 58)
            [source] = [s for s in signal["data"]["source_details"] if s.get("source_id") == "nws"]
            self.assertEqual(source["forecast_measurement_kind"], "low")
            self.assertEqual(source["target_mapping"]["source_period_end"], "2026-09-27T06:00:00-07:00")
            self.assertEqual(source["target_mapping"]["mapping"], "source_local_nws_night_low_proxy")
        finally:
            engine.close()

    def test_high_scoring_survives_missing_unrelated_night_temperature(self):
        engine = ProWeatherEngine()
        try:
            setattr(engine.nws, "http", _Http({"properties": {"periods": [
                {"name": "Sunday", "temperature": 84, "isDaytime": True,
                 "startTime": "2026-09-27T06:00:00-07:00", "endTime": "2026-09-27T18:00:00-07:00"},
                {"name": "Sunday Night", "temperature": None, "isDaytime": False,
                 "startTime": "2026-09-27T18:00:00-07:00", "endTime": "2026-09-28T06:00:00-07:00"},
            ]}}))
            engine.nws._points_cache["austin"] = (datetime.now(timezone.utc), ("EWX", 152, 91))
            setattr(engine.nws, "get_station_observation", lambda city: None)
            engine.open_meteo.get_forecast = lambda *_args, **_kwargs: None
            signal = engine._score_temperature_market(
                "Will Austin high temperature be above 80° on Sep 27, 2026?",
                0.5, market_date="2026-09-27",
            )
            assert signal is not None
            self.assertEqual(signal["data"]["predicted_temp"], 84)
        finally:
            engine.close()

    def test_requested_kind_confidence_ignores_other_temperature_kind(self):
        engine = ProWeatherEngine()
        try:
            now = datetime.now(timezone.utc)
            def snapshot(source, high, low):
                return WeatherSnapshot(
                    city="austin", source=source, high_temp_f=high, low_temp_f=low,
                    current_temp_f=75, fetched_at=now, forecast_hours_ahead=2,
                    confidence=0.85,
                )
            engine.nws.get_forecast = lambda *_args, **_kwargs: snapshot("nws", 84, 58)
            engine.nws.get_station_observation = lambda *_args: None
            for kind, comparison in (
                ("high", ((84, 58), (84, 70))),
                ("low", ((84, 58), (104, 58))),
            ):
                results = []
                for high, low in comparison:
                    engine._cache.clear()
                    engine.open_meteo.get_forecast = lambda *_args, high=high, low=low, **_kwargs: snapshot("open-meteo", high, low)
                    results.append(engine.get_forecast("austin", target_date="2026-09-27", require_target=True, measurement_kind=kind))
                self.assertEqual(results[0].confidence, results[1].confidence, kind)
                self.assertEqual(results[0].source_agreement, results[1].source_agreement, kind)
                self.assertEqual(results[0].details["nws_open_meteo_gap"], results[1].details["nws_open_meteo_gap"], kind)
            engine._cache.clear()
            engine.open_meteo.get_forecast = lambda *_args, **_kwargs: snapshot("open-meteo", 94, 58)
            disagreed = engine.get_forecast("austin", target_date="2026-09-27", require_target=True, measurement_kind="high")
            self.assertEqual(disagreed.confidence, 0.43)  # Existing single-metric spread scale.
            self.assertEqual(disagreed.details["nws_open_meteo_gap"], 10)
        finally:
            engine.close()

    def test_nws_fails_closed_when_target_period_timezone_is_ambiguous(self):
        feed = NWSFeed()
        feed.http = _Http({
            "properties": {
                "periods": [{
                    "number": 1, "name": "Tomorrow", "temperature": 84,
                    "isDaytime": True, "startTime": "2026-08-02T06:00:00", "endTime": "2026-08-02T18:00:00",
                }],
            },
        })
        feed._points_cache["austin"] = (datetime.now(timezone.utc), ("EWX", 152, 91))

        snapshot = feed.get_forecast("austin", target_date="2026-08-02")

        self.assertFalse(snapshot.scoreable_forecast)
        self.assertEqual(snapshot.availability_reason, "source_forecast_period_timezone_ambiguous")

    def test_local_station_current_observation_is_typed_as_observation(self):
        engine = ProWeatherEngine()
        try:
            details = engine._source_contribution_details(type("Forecast", (), {
                "sources_used": [],
                "details": {
                    "local_station_observation": {
                        "station": "KAUS", "city": "austin", "current_temp_f": 81.0,
                        "observation_time": "2026-08-01T17:00:00Z", "source": "nws_observation",
                    },
                },
            })())
        finally:
            engine.close()

        [station] = details
        self.assertEqual(station["evidence_type"], "observation")
        self.assertFalse(station["scoreable_forecast"])
        self.assertEqual(station["forecast_target"], "current_observation")

    def test_collector_normalization_preserves_exact_target_and_source_provenance(self):
        source = {
            "source_name": "nws", "forecast_high": 84.0, "as_of": "2026-08-01T12:00:00Z",
            "source_id": "nws", "source_location_city": "Austin", "forecast_measurement_kind": "high",
            "contract_shape": "tail", "question_side": "above",
            "source_evidence_version": 1, "evidence_type": "forecast", "forecast_availability": "available",
            "scoreable_forecast": True, "market_target_date": "2026-08-02", "source_target_date": "2026-08-02",
            "target_mapping": {"market_target_date": "2026-08-02", "source_target_date": "2026-08-02", "mapping": "exact_source_local_nws_period"},
        }
        [emitted] = PredictionLab._weather_snapshot_sources(
            {"source_details": [source]}, settlement_source="nws", market_date="2026-08-02",
            as_of="2026-08-01T12:01:00Z", station_resolution={}, source_agreement=None,
        )

        self.assertEqual(emitted["market_target_date"], "2026-08-02")
        self.assertEqual(emitted["source_target_date"], "2026-08-02")
        self.assertEqual(emitted["source_as_of"], "2026-08-01T12:00:00Z")
        self.assertEqual(emitted["target_mapping"]["mapping"], "exact_source_local_nws_period")
        self.assertEqual(emitted["source_id"], "nws")
        self.assertEqual(emitted["source_location_city"], "Austin")
        self.assertEqual(emitted["forecast_measurement_kind"], "high")
        self.assertEqual(emitted["contract_shape"], "tail")
        self.assertEqual(emitted["question_side"], "above")

    def test_sanitized_observations_score_only_exact_forecasts(self):
        row = {
            "market_id": "KXHIGHTAUS-26AUG02-T80",
            "question": "Will Austin high temperature be above 80° on Aug 2, 2026?",
            "weather_source_snapshot": {"market_date": "2026-08-02", "sources": [
                {"source_id": "good", "source_name": "Good", "forecast_high": 82.0,
                 "source_evidence_version": 1, "evidence_type": "forecast", "scoreable_forecast": True,
                 "target_mapping": {"market_target_date": "2026-08-02", "source_target_date": "2026-08-02"}},
                {"source_id": "current", "source_name": "Current", "current_forecast": 81.0,
                 "source_evidence_version": 1, "evidence_type": "observation", "scoreable_forecast": False,
                 "forecast_target": "current_observation"},
                {"source_id": "ambiguous", "source_name": "Ambiguous", "forecast_high": 81.0,
                 "source_evidence_version": 1, "evidence_type": "forecast_unavailable", "scoreable_forecast": False,
                 "availability_reason": "source_forecast_period_timezone_ambiguous"},
            ]},
        }

        observations = {item.source_id: item for item in extract_source_forecast_observations(row)}

        self.assertEqual(observations["good"].forecast_temp_f, 82.0)
        self.assertIsNone(observations["current"].forecast_temp_f)
        self.assertIn("source_evidence_not_forecast", observations["current"].missing_reasons)
        self.assertIsNone(observations["ambiguous"].forecast_temp_f)
        self.assertIn("source_forecast_unavailable:source_forecast_period_timezone_ambiguous", observations["ambiguous"].missing_reasons)
