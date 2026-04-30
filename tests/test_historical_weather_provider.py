from datetime import date

from bot.weather.historical_provider import HistoricalOpenMeteoWeatherEngine
from bot.weather.station_mapping import WeatherStationResolution


class FakeResponse:
    def __init__(self, *, text="", payload=None, status_error=None):
        self.text = text
        self._payload = payload or {}
        self._status_error = status_error

    def raise_for_status(self):
        if self._status_error:
            raise self._status_error

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        pass


def test_iem_station_csv_parser_filters_to_expected_local_date():
    payload = "station,valid,tmpf\nNYC,2021-07-16 23:51,99\nNYC,2021-07-17 00:51,82\nNYC,2021-07-17 14:51,90\nNYC,2021-07-17 15:51,M\n"

    temps = HistoricalOpenMeteoWeatherEngine._parse_iem_temperature_csv(payload, "2021-07-17")

    assert temps == [82.0, 90.0]


def test_official_daily_station_observation_is_preferred_first():
    engine = HistoricalOpenMeteoWeatherEngine()
    engine.http = FakeClient([
        FakeResponse(payload=[{"DATE": "2021-07-17", "TMAX": "91", "TMIN": "73"}]),
    ])

    obs = engine.get_observation(
        "new york",
        date(2021, 7, 17),
        station_resolution=WeatherStationResolution(mapping="exact", station_id="KNYC", station_cli="NYC"),
    )

    assert obs is not None
    assert obs.high_temp_f == 91.0
    assert obs.low_temp_f == 73.0
    assert obs.source == "noaa_daily_summaries_station"
    assert obs.source_quality == "settlement_station_official_daily"
    assert obs.station_id == "KNYC"
    assert engine.http.calls[0][1]["stations"] == "USW00094728"


def test_iem_station_observation_is_used_when_noaa_daily_fails():
    engine = HistoricalOpenMeteoWeatherEngine()
    engine.http = FakeClient([
        RuntimeError("noaa unavailable"),
        FakeResponse(text="station,valid,tmpf\nNYC,2021-07-17 00:51,82\nNYC,2021-07-17 14:51,90\n"),
    ])

    obs = engine.get_observation(
        "new york",
        date(2021, 7, 17),
        station_resolution=WeatherStationResolution(mapping="exact", station_id="KNYC", station_cli="NYC"),
    )

    assert obs is not None
    assert obs.high_temp_f == 90.0
    assert obs.low_temp_f == 82.0
    assert obs.source == "iem_asos_station_hourly"
    assert obs.source_quality == "settlement_station_hourly"
    assert obs.station_id == "KNYC"
    assert engine.http.calls[1][1]["station"] == "KNYC"


def test_open_meteo_is_used_when_station_archive_fails():
    engine = HistoricalOpenMeteoWeatherEngine()
    engine.http = FakeClient([
        RuntimeError("noaa unavailable"),
        RuntimeError("station unavailable"),
        FakeResponse(payload={
            "daily": {
                "time": ["2024-10-24"],
                "temperature_2m_max": [82.0],
                "temperature_2m_min": [72.2],
            }
        }),
    ])

    obs = engine.get_observation("miami", date(2024, 10, 24))

    assert obs is not None
    assert obs.high_temp_f == 82.0
    assert obs.low_temp_f == 72.2
    assert obs.source == "open_meteo_archive"
    assert obs.source_quality == "grid_fallback"


def test_scored_signal_contains_date_match_and_station_source_metadata():
    engine = HistoricalOpenMeteoWeatherEngine()
    engine.http = FakeClient([
        FakeResponse(payload=[{"DATE": "2021-07-17", "TMAX": "91", "TMIN": "73"}]),
    ])

    signal = engine.score_temperature_market_with_context(
        "Will the high temperature in New York City be over 90° on Saturday?",
        0.99,
        category="HIGHNY0-21JUL17",
    )

    assert signal is not None
    data = signal["data"]
    assert data["date_validation"]["ok"] is True
    assert data["date_validation"]["market_date"] == "2021-07-17"
    assert data["date_validation"]["weather_date"] == "2021-07-17"
    assert data["source_quality"] == "settlement_station_official_daily"
    assert data["station_id"] == "KNYC"
    assert data["historical_high"] == 91.0
    assert signal["predicted_prob"] == 0.85
