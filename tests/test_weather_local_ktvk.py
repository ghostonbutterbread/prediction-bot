"""Arizona's Family (KTVK/KPHO) Phoenix Gray Fusion excerpt from a public capture.

The fixture keeps the page's original wx-current-conditions-v3 entry verbatim:
lastModified, the imperial location and the first three dailyForecast rows.
Other page content was removed; the capture SHA-256 identifies the complete raw
page. No JavaScript is executed and no live network or machine-local fixture
path is required.
"""
import tempfile
import unittest

import httpx

from bot.feeds import weather_pro
from bot.feeds.weather_capture_store import WeatherCaptureStore
from bot.feeds.weather_local import LocalForecastUnavailable, parse_gray_daily
from bot.weather.registry import WeatherRegistry

SOURCE_ID = 'src_local_ktvk_weather'
# Captured 2026-11-27 04:11 UTC (2026-11-26 21:11 MST) from https://www.azfamily.com/weather/.
CAPTURE_SHA256 = 'e58ec341e7d5dea5118c1db80b16f6372ca5fa5d16a2940248519a45d5cfdbab'
CAPTURE_BYTES = 200849
CACHE = (
    '{"wx-current-conditions-v3":{"{}":{"lastModified":1795752579024,"data":{"imperial":{"location":{'
    '"adminDistrictCode":"AZ","city":"Phoenix","ianaTimeZone":"America/Phoenix","latitude":33.448,"lo'
    'ngitude":-112.075},"dailyForecast":[{"daypart":[{"dayOrNight":null,"daypartName":null,"iconCode"'
    ':null,"narrative":null,"precipChance":null,"temperature":null,"uvDescription":null,"uvIndex":nul'
    'l,"windDirectionCardinal":null,"windSpeed":null,"index":0,"relativeHumidity":null,"wxPhraseLong"'
    ':null},{"dayOrNight":"N","daypartName":"Tonight","iconCode":31,"narrative":"Clear skies. Low 53F'
    '. Winds light and variable.","precipChance":0,"temperature":53,"uvDescription":"Low","uvIndex":0'
    ',"windDirectionCardinal":"E","windSpeed":5,"index":1,"relativeHumidity":40,"wxPhraseLong":"Clear'
    '"}],"moonPhase":"Waning Gibbous","moonriseTimeLocal":"2026-11-26T19:30:59-0700","moonriseTimeUtc'
    '":1795746659,"moonsetTimeLocal":"2026-11-26T09:40:35-0700","moonsetTimeUtc":1795711235,"sunriseT'
    'imeLocal":"2026-11-26T07:09:54-0700","sunriseTimeUtc":1795702194,"sunsetTimeLocal":"2026-11-26T1'
    '7:20:57-0700","sunsetTimeUtc":1795738857,"dayOfWeek":"Thursday","iconCode":31,"index":0,"precipC'
    'hance":0,"relativeHumidity":40,"temperatureMax":79,"temperatureMin":53,"validTimeLocal":"2026-11'
    '-26T07:00:00-0700","windDirectionCardinal":"E","windSpeed":5,"wxPhraseLong":"Clear"},{"daypart":'
    '[{"dayOrNight":"D","daypartName":"Tomorrow","iconCode":30,"narrative":"Sunshine and clouds mixed'
    '. High 79F. Winds light and variable.","precipChance":0,"temperature":79,"uvDescription":"Modera'
    'te","uvIndex":3,"windDirectionCardinal":"SSE","windSpeed":6,"index":2,"relativeHumidity":27,"wxP'
    'hraseLong":"Partly Cloudy"},{"dayOrNight":"N","daypartName":"Tomorrow night","iconCode":33,"narr'
    'ative":"Partly cloudy. Low 51F. Winds light and variable.","precipChance":0,"temperature":51,"uv'
    'Description":"Low","uvIndex":0,"windDirectionCardinal":"SE","windSpeed":5,"index":3,"relativeHum'
    'idity":46,"wxPhraseLong":"Mostly Clear"}],"moonPhase":"Waning Gibbous","moonriseTimeLocal":"2026'
    '-11-27T20:45:28-0700","moonriseTimeUtc":1795837528,"moonsetTimeLocal":"2026-11-27T10:37:15-0700"'
    ',"moonsetTimeUtc":1795801035,"sunriseTimeLocal":"2026-11-27T07:10:47-0700","sunriseTimeUtc":1795'
    '788647,"sunsetTimeLocal":"2026-11-27T17:20:43-0700","sunsetTimeUtc":1795825243,"dayOfWeek":"Frid'
    'ay","iconCode":30,"index":1,"precipChance":0,"relativeHumidity":27,"temperatureMax":79,"temperat'
    'ureMin":51,"validTimeLocal":"2026-11-27T07:00:00-0700","windDirectionCardinal":"SSE","windSpeed"'
    ':6,"wxPhraseLong":"Partly Cloudy"},{"daypart":[{"dayOrNight":"D","daypartName":"Saturday","iconC'
    'ode":30,"narrative":"Partly cloudy skies. High 77F. Winds light and variable.","precipChance":0,'
    '"temperature":77,"uvDescription":"Moderate","uvIndex":3,"windDirectionCardinal":"SSE","windSpeed'
    '":5,"index":4,"relativeHumidity":32,"wxPhraseLong":"Partly Cloudy"},{"dayOrNight":"N","daypartNa'
    'me":"Saturday night","iconCode":29,"narrative":"A few clouds. Low 53F. Winds light and variable.'
    '","precipChance":0,"temperature":53,"uvDescription":"Low","uvIndex":0,"windDirectionCardinal":"S'
    'E","windSpeed":5,"index":5,"relativeHumidity":46,"wxPhraseLong":"Partly Cloudy"}],"moonPhase":"W'
    'aning Gibbous","moonriseTimeLocal":"2026-11-28T21:58:19-0700","moonriseTimeUtc":1795928299,"moon'
    'setTimeLocal":"2026-11-28T11:22:57-0700","moonsetTimeUtc":1795890177,"sunriseTimeLocal":"2026-11'
    '-28T07:11:40-0700","sunriseTimeUtc":1795875100,"sunsetTimeLocal":"2026-11-28T17:20:31-0700","sun'
    'setTimeUtc":1795911631,"dayOfWeek":"Saturday","iconCode":30,"index":2,"precipChance":0,"relative'
    'Humidity":32,"temperatureMax":77,"temperatureMin":53,"validTimeLocal":"2026-11-28T07:00:00-0700"'
    ',"windDirectionCardinal":"SSE","windSpeed":5,"wxPhraseLong":"Partly Cloudy"}]}}}}}'
)
PAGE = ('<script id="fusion-metadata" type="application/javascript">'
        'Fusion.contentCache=' + CACHE + ';</script>')


class KtvkPhoenixGrayTests(unittest.TestCase):
    def parse(self, target, kind, **overrides):
        options = {'city': 'Phoenix', 'target_date': target, 'measurement_kind': kind,
                   'timezone_name': 'America/Phoenix', **overrides}
        return parse_gray_daily(PAGE, **options)

    def test_captured_high_uses_the_dated_daily_row(self):
        for target, expected in (('2026-11-26', 79), ('2026-11-27', 79), ('2026-11-28', 77)):
            with self.subTest(target=target):
                parsed = self.parse(target, 'high')
                self.assertEqual(parsed['high'], expected)
                self.assertIsNone(parsed['low'])
                self.assertEqual(parsed['period_start'], target + 'T07:00:00-0700')
                self.assertEqual(parsed['forecast_type'], 'local_daytime_high_predictor')
                self.assertEqual(parsed['cache_updated_at_ms'], 1795752579024)
                self.assertEqual(parsed['location']['city'], 'Phoenix')
                self.assertEqual(parsed['location']['adminDistrictCode'], 'AZ')
                self.assertEqual(parsed['location']['ianaTimeZone'], 'America/Phoenix')

    def test_captured_low_is_the_prior_night_minimum(self):
        for target, expected, source_day in (('2026-11-27', 53, '2026-11-26'),
                                             ('2026-11-28', 51, '2026-11-27'),
                                             ('2026-11-29', 53, '2026-11-28')):
            with self.subTest(target=target):
                parsed = self.parse(target, 'low')
                self.assertEqual(parsed['low'], expected)
                self.assertIsNone(parsed['high'])
                self.assertEqual(parsed['period_start'], source_day + 'T07:00:00-0700')
                self.assertEqual(parsed['forecast_type'], 'local_prior_night_low_predictor')

    def test_refuses_other_city_zone_and_dates_outside_the_capture(self):
        for target, kind, overrides, reason in (
                ('2026-11-27', 'high', {'city': 'Tucson'}, 'source_forecast_city_mismatch'),
                ('2026-11-27', 'high', {'timezone_name': 'America/Denver'}, 'source_forecast_timezone_mismatch'),
                ('2026-11-29', 'high', {}, 'target_date_not_in_source_forecast'),
                ('2026-11-26', 'low', {}, 'target_date_missing_low_forecast_period')):
            with self.subTest(reason=reason), self.assertRaisesRegex(LocalForecastUnavailable, reason):
                self.parse(target, kind, **overrides)
        with self.assertRaisesRegex(LocalForecastUnavailable, 'source_specific_forecast_schema_missing'):
            parse_gray_daily(PAGE.replace('Fusion.contentCache=', 'Fusion.other='), city='Phoenix',
                             target_date='2026-11-27', measurement_kind='high', timezone_name='America/Phoenix')

    def test_registry_dispatches_ktvk_through_the_gray_adapter(self):
        registry = WeatherRegistry.from_file()
        source = next(s for s in registry.collection_sources('Phoenix') if s['source_id'] == SOURCE_ID)
        self.assertEqual(source['city_id'], 'phoenix_az')
        self.assertEqual(source['timezone'], 'America/Phoenix')
        self.assertEqual(source['collection_adapter'], 'gray_fusion_daily_v1')
        self.assertIsNone(source.get('collection_blocker'))
        self.assertEqual((source['status'], source['trust_score'], source['verified']), ('watch_only', 0, False))
        with tempfile.TemporaryDirectory() as root, httpx.Client(
                transport=httpx.MockTransport(lambda request: httpx.Response(200, text=PAGE))) as client:
            store = WeatherCaptureStore(storage_root=root)
            feed = weather_pro.LocalForecastFeed(client, capture_store=store)
            for kind, expected in (('high', 79), ('low', 53)):
                with self.subTest(kind=kind):
                    snapshot = feed.get_forecast(source, target_date='2026-11-27', measurement_kind=kind)
                    self.assertTrue(snapshot.scoreable_forecast, snapshot.availability_reason)
                    self.assertEqual(snapshot.high_temp_f if kind == 'high' else snapshot.low_temp_f, expected)
                    self.assertEqual(snapshot.source_details['forecast_query_url'], 'https://www.azfamily.com/weather/')
                    self.assertEqual(snapshot.source_details['source_clock_eligibility'], 'consistent_with_capture')
                    self.assertEqual(store.read_verified(snapshot.source_details['capture']).decode('utf-8'), PAGE)


if __name__ == '__main__':
    unittest.main()
