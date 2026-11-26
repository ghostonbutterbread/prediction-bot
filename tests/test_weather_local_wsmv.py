"""WSMV 4 Nashville Gray Fusion excerpt from a public capture, not executed JS.

The fixture keeps the page's original wx-current-conditions-v3 entry verbatim:
lastModified, the imperial location and the first three dailyForecast rows.
Other page content was removed; the capture SHA-256 identifies the complete raw
page. No live network or machine-local fixture path is required.
"""
import tempfile
import unittest

import httpx

from bot.feeds import weather_pro
from bot.feeds.weather_capture_store import WeatherCaptureStore
from bot.feeds.weather_local import LocalForecastUnavailable, parse_gray_daily
from bot.weather.registry import WeatherRegistry

SOURCE_ID = 'src_local_wsmv_weather'
# Captured 2026-11-26 07:07 UTC (01:07 CST) from https://www.wsmv.com/weather/.
CAPTURE_SHA256 = '0e3710c65975fe04538bc7cd490d29f2e6c5d50f7520ea447ed2431bf30d2ff9'
CAPTURE_BYTES = 182117
CACHE = (
    '{"wx-current-conditions-v3":{"{}":{"lastModified":1795676804811,"data":{"imperial":{"location":{'
    '"adminDistrictCode":"TN","city":"Nashville","ianaTimeZone":"America/Chicago","latitude":36.165,"'
    'longitude":-86.781},"dailyForecast":[{"daypart":[{"dayOrNight":null,"daypartName":null,"iconCode'
    '":null,"narrative":null,"precipChance":null,"temperature":null,"uvDescription":null,"uvIndex":nu'
    'll,"windDirectionCardinal":null,"windSpeed":null,"index":0,"relativeHumidity":null,"wxPhraseLong'
    '":null},{"dayOrNight":"N","daypartName":"Tonight","iconCode":12,"narrative":"Cloudy with periods'
    ' of rain. Potential for heavy rainfall. Low 40F. Winds NNW at 5 to 10 mph. Chance of rain 70%. R'
    'ainfall near a half an inch.","precipChance":90,"temperature":40,"uvDescription":"Low","uvIndex"'
    ':0,"windDirectionCardinal":"NNW","windSpeed":7,"index":1,"relativeHumidity":96,"wxPhraseLong":"R'
    'ain"}],"moonPhase":"Waning Gibbous","moonriseTimeLocal":"2026-11-25T17:25:42-0600","moonriseTime'
    'Utc":1795649142,"moonsetTimeLocal":"2026-11-25T07:57:43-0600","moonsetTimeUtc":1795615063,"sunri'
    'seTimeLocal":"2026-11-25T06:33:48-0600","sunriseTimeUtc":1795610028,"sunsetTimeLocal":"2026-11-2'
    '5T16:33:49-0600","sunsetTimeUtc":1795646029,"dayOfWeek":"Wednesday","iconCode":12,"index":0,"pre'
    'cipChance":90,"relativeHumidity":96,"temperatureMax":57,"temperatureMin":40,"validTimeLocal":"20'
    '26-11-25T07:00:00-0600","windDirectionCardinal":"NNW","windSpeed":7,"wxPhraseLong":"Rain"},{"day'
    'part":[{"dayOrNight":"D","daypartName":"Tomorrow","iconCode":5,"narrative":"Cloudy. Periods of l'
    'ight rain early. High 46F. Winds WNW at 10 to 15 mph. Chance of rain 60%.","precipChance":90,"te'
    'mperature":41,"uvDescription":"Low","uvIndex":1,"windDirectionCardinal":"W","windSpeed":14,"inde'
    'x":2,"relativeHumidity":93,"wxPhraseLong":"Rain/Snow"},{"dayOrNight":"N","daypartName":"Tomorrow'
    ' night","iconCode":5,"narrative":"Cloudy early with partial clearing expected late. Low 32F. Win'
    'ds W at 5 to 10 mph.","precipChance":20,"temperature":35,"uvDescription":"Low","uvIndex":0,"wind'
    'DirectionCardinal":"WSW","windSpeed":13,"index":3,"relativeHumidity":93,"wxPhraseLong":"Rain/Sno'
    'w Showers Early"}],"moonPhase":"Waning Gibbous","moonriseTimeLocal":"2026-11-26T18:35:35-0600","'
    'moonriseTimeUtc":1795739735,"moonsetTimeLocal":"2026-11-26T09:04:29-0600","moonsetTimeUtc":17957'
    '05469,"sunriseTimeLocal":"2026-11-26T06:34:46-0600","sunriseTimeUtc":1795696486,"sunsetTimeLocal'
    '":"2026-11-26T16:33:29-0600","sunsetTimeUtc":1795732409,"dayOfWeek":"Thursday","iconCode":5,"ind'
    'ex":1,"precipChance":90,"relativeHumidity":93,"temperatureMax":41,"temperatureMin":35,"validTime'
    'Local":"2026-11-26T07:00:00-0600","windDirectionCardinal":"W","windSpeed":14,"wxPhraseLong":"Rai'
    'n/Snow"},{"daypart":[{"dayOrNight":"D","daypartName":"Friday","iconCode":28,"narrative":"Sunshin'
    'e along with some cloudy intervals. High 47F. Winds W at 10 to 15 mph.","precipChance":0,"temper'
    'ature":47,"uvDescription":"Low","uvIndex":1,"windDirectionCardinal":"W","windSpeed":13,"index":4'
    ',"relativeHumidity":78,"wxPhraseLong":"Mostly Cloudy"},{"dayOrNight":"N","daypartName":"Friday n'
    'ight","iconCode":33,"narrative":"Mostly clear skies. Low 31F. Winds light and variable.","precip'
    'Chance":0,"temperature":31,"uvDescription":"Low","uvIndex":0,"windDirectionCardinal":"SW","windS'
    'peed":6,"index":5,"relativeHumidity":86,"wxPhraseLong":"Mostly Clear"}],"moonPhase":"Waning Gibb'
    'ous","moonriseTimeLocal":"2026-11-27T19:51:10-0600","moonriseTimeUtc":1795830670,"moonsetTimeLoc'
    'al":"2026-11-27T10:00:23-0600","moonsetTimeUtc":1795795223,"sunriseTimeLocal":"2026-11-27T06:35:'
    '44-0600","sunriseTimeUtc":1795782944,"sunsetTimeLocal":"2026-11-27T16:33:11-0600","sunsetTimeUtc'
    '":1795818791,"dayOfWeek":"Friday","iconCode":28,"index":2,"precipChance":0,"relativeHumidity":78'
    ',"temperatureMax":47,"temperatureMin":31,"validTimeLocal":"2026-11-27T07:00:00-0600","windDirect'
    'ionCardinal":"W","windSpeed":13,"wxPhraseLong":"Mostly Cloudy"}]}}}}}'
)
PAGE = ('<script id="fusion-metadata" type="application/javascript">'
        'Fusion.contentCache=' + CACHE + ';</script>')


class WsmvNashvilleGrayTests(unittest.TestCase):
    def parse(self, target, kind, **overrides):
        options = {'city': 'Nashville', 'target_date': target, 'measurement_kind': kind,
                   'timezone_name': 'America/Chicago', **overrides}
        return parse_gray_daily(PAGE, **options)

    def test_captured_high_uses_the_dated_daily_row(self):
        for target, expected in (('2026-11-26', 41), ('2026-11-27', 47)):
            with self.subTest(target=target):
                parsed = self.parse(target, 'high')
                self.assertEqual(parsed['high'], expected)
                self.assertIsNone(parsed['low'])
                self.assertEqual(parsed['period_start'], target + 'T07:00:00-0600')
                self.assertEqual(parsed['forecast_type'], 'local_daytime_high_predictor')
                self.assertEqual(parsed['cache_updated_at_ms'], 1795676804811)
                self.assertEqual(parsed['location']['city'], 'Nashville')
                self.assertEqual(parsed['location']['adminDistrictCode'], 'TN')
                self.assertEqual(parsed['location']['ianaTimeZone'], 'America/Chicago')

    def test_captured_low_is_the_prior_night_minimum(self):
        for target, expected, source_day in (('2026-11-26', 40, '2026-11-25'), ('2026-11-27', 35, '2026-11-26')):
            with self.subTest(target=target):
                parsed = self.parse(target, 'low')
                self.assertEqual(parsed['low'], expected)
                self.assertIsNone(parsed['high'])
                self.assertEqual(parsed['period_start'], source_day + 'T07:00:00-0600')
                self.assertEqual(parsed['forecast_type'], 'local_prior_night_low_predictor')

    def test_refuses_other_city_zone_and_dates_outside_the_capture(self):
        for target, kind, overrides, reason in (
                ('2026-11-26', 'high', {'city': 'Memphis'}, 'source_forecast_city_mismatch'),
                ('2026-11-26', 'high', {'timezone_name': 'America/New_York'}, 'source_forecast_timezone_mismatch'),
                ('2026-11-28', 'high', {}, 'target_date_not_in_source_forecast'),
                ('2026-11-25', 'low', {}, 'target_date_missing_low_forecast_period')):
            with self.subTest(reason=reason), self.assertRaisesRegex(LocalForecastUnavailable, reason):
                self.parse(target, kind, **overrides)
        with self.assertRaisesRegex(LocalForecastUnavailable, 'source_specific_forecast_schema_missing'):
            parse_gray_daily(PAGE.replace('Fusion.contentCache=', 'Fusion.other='), city='Nashville',
                             target_date='2026-11-26', measurement_kind='high', timezone_name='America/Chicago')

    def test_registry_dispatches_wsmv_through_the_gray_adapter(self):
        registry = WeatherRegistry.from_file()
        source = next(s for s in registry.collection_sources('Nashville') if s['source_id'] == SOURCE_ID)
        self.assertEqual(source['city_id'], 'nashville_tn')
        self.assertEqual(source['timezone'], 'America/Chicago')
        self.assertEqual(source['collection_adapter'], 'gray_fusion_daily_v1')
        self.assertIsNone(source.get('collection_blocker'))
        self.assertEqual((source['status'], source['trust_score'], source['verified']), ('watch_only', 0, False))
        with tempfile.TemporaryDirectory() as root, httpx.Client(
                transport=httpx.MockTransport(lambda request: httpx.Response(200, text=PAGE))) as client:
            store = WeatherCaptureStore(storage_root=root)
            feed = weather_pro.LocalForecastFeed(client, capture_store=store)
            for kind, expected in (('high', 41), ('low', 40)):
                with self.subTest(kind=kind):
                    snapshot = feed.get_forecast(source, target_date='2026-11-26', measurement_kind=kind)
                    self.assertTrue(snapshot.scoreable_forecast, snapshot.availability_reason)
                    self.assertEqual(snapshot.high_temp_f if kind == 'high' else snapshot.low_temp_f, expected)
                    self.assertEqual(snapshot.source_details['forecast_query_url'], 'https://www.wsmv.com/weather/')
                    self.assertEqual(snapshot.source_details['source_clock_eligibility'], 'consistent_with_capture')
                    self.assertEqual(store.read_verified(snapshot.source_details['capture']).decode('utf-8'), PAGE)


if __name__ == '__main__':
    unittest.main()
