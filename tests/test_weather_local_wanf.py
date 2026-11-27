"""Atlanta News First (WANF) Gray Fusion excerpt from a public capture.

The fixture keeps the page's original wx-current-conditions-v3 entry values:
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

SOURCE_ID = 'src_local_wanf_weather'
# Captured 2026-11-27 16:08 UTC (11:08 EST) from https://www.atlantanewsfirst.com/weather/.
CAPTURE_SHA256 = 'fe509b8d454b9bafee17a4cd2467c15d50158412c7cd6f65524fafdd70e617fa'
CAPTURE_BYTES = 187185
CACHE = (
    '{"wx-current-conditions-v3":{"{}":{"lastModified":1795795592262,"data":{"imperial":{"location":{"a'
    'dminDistrictCode":"GA","city":"Atlanta","ianaTimeZone":"America/New_York","latitude":33.751,"longi'
    'tude":-84.39},"dailyForecast":[{"daypart":[{"dayOrNight":"D","daypartName":"Today","iconCode":28,"'
    'narrative":"Cloudy early with partial sunshine expected late. High 50F. Winds W at 10 to 20 mph.",'
    '"precipChance":0,"temperature":50,"uvDescription":"Low","uvIndex":2,"windDirectionCardinal":"W","w'
    'indSpeed":17,"index":0,"relativeHumidity":69,"wxPhraseLong":"Mostly Cloudy"},{"dayOrNight":"N","da'
    'ypartName":"Tonight","iconCode":29,"narrative":"A few clouds. Low 36F. Winds W at 5 to 10 mph.","p'
    'recipChance":0,"temperature":36,"uvDescription":"Low","uvIndex":0,"windDirectionCardinal":"W","win'
    'dSpeed":8,"index":1,"relativeHumidity":75,"wxPhraseLong":"Partly Cloudy"}],"moonPhase":"Waning Gib'
    'bous","moonriseTimeLocal":"2026-11-27T20:48:08-0500","moonriseTimeUtc":1795830488,"moonsetTimeLoca'
    'l":"2026-11-27T10:43:24-0500","moonsetTimeUtc":1795794204,"sunriseTimeLocal":"2026-11-27T07:20:34-'
    '0500","sunriseTimeUtc":1795782034,"sunsetTimeLocal":"2026-11-27T17:29:18-0500","sunsetTimeUtc":179'
    '5818558,"dayOfWeek":"Friday","iconCode":28,"index":0,"precipChance":0,"relativeHumidity":69,"tempe'
    'ratureMax":50,"temperatureMin":36,"validTimeLocal":"2026-11-27T07:00:00-0500","windDirectionCardin'
    'al":"W","windSpeed":17,"wxPhraseLong":"Mostly Cloudy"},{"daypart":[{"dayOrNight":"D","daypartName"'
    ':"Tomorrow","iconCode":34,"narrative":"Mostly sunny skies. High around 61F. Winds W at 5 to 10 mph'
    '.","precipChance":0,"temperature":61,"uvDescription":"Moderate","uvIndex":3,"windDirectionCardinal'
    '":"W","windSpeed":8,"index":2,"relativeHumidity":55,"wxPhraseLong":"Mostly Sunny"},{"dayOrNight":"'
    'N","daypartName":"Tomorrow night","iconCode":33,"narrative":"Mostly clear skies. Low 41F. Winds SS'
    'W at 5 to 10 mph.","precipChance":0,"temperature":41,"uvDescription":"Low","uvIndex":0,"windDirect'
    'ionCardinal":"SSW","windSpeed":6,"index":3,"relativeHumidity":74,"wxPhraseLong":"Mostly Clear"}],"'
    'moonPhase":"Waning Gibbous","moonriseTimeLocal":"2026-11-28T22:01:26-0500","moonriseTimeUtc":17959'
    '21286,"moonsetTimeLocal":"2026-11-28T11:29:41-0500","moonsetTimeUtc":1795883381,"sunriseTimeLocal"'
    ':"2026-11-28T07:21:27-0500","sunriseTimeUtc":1795868487,"sunsetTimeLocal":"2026-11-28T17:29:05-050'
    '0","sunsetTimeUtc":1795904945,"dayOfWeek":"Saturday","iconCode":34,"index":1,"precipChance":0,"rel'
    'ativeHumidity":55,"temperatureMax":61,"temperatureMin":41,"validTimeLocal":"2026-11-28T07:00:00-05'
    '00","windDirectionCardinal":"W","windSpeed":8,"wxPhraseLong":"Mostly Sunny"},{"daypart":[{"dayOrNi'
    'ght":"D","daypartName":"Sunday","iconCode":12,"narrative":"First Alert Weather Day: Increasing clo'
    'uds through the day with rain likely by late afternoon. High 65F. Winds S at 10 to 15 mph. Chance '
    'of rain 70%. Rainfall around a quarter of an inch.","precipChance":70,"temperature":65,"uvDescript'
    'ion":"Low","uvIndex":2,"windDirectionCardinal":"S","windSpeed":12,"index":4,"relativeHumidity":74,'
    '"wxPhraseLong":"Rain"},{"dayOrNight":"N","daypartName":"Sunday night","iconCode":12,"narrative":"F'
    'irst Alert Weather Day: Evening rain likely. Chance of rain 70%. Rainfall near a quarter of an inc'
    'h.","precipChance":70,"temperature":44,"uvDescription":"Low","uvIndex":0,"windDirectionCardinal":"'
    'W","windSpeed":12,"index":5,"relativeHumidity":82,"wxPhraseLong":"Rain"}],"moonPhase":"Waning Gibb'
    'ous","moonriseTimeLocal":"2026-11-29T23:11:19-0500","moonriseTimeUtc":1796011879,"moonsetTimeLocal'
    '":"2026-11-29T12:08:22-0500","moonsetTimeUtc":1795972102,"sunriseTimeLocal":"2026-11-29T07:22:20-0'
    '500","sunriseTimeUtc":1795954940,"sunsetTimeLocal":"2026-11-29T17:28:54-0500","sunsetTimeUtc":1795'
    '991334,"dayOfWeek":"Sunday","iconCode":12,"index":2,"precipChance":70,"relativeHumidity":74,"tempe'
    'ratureMax":65,"temperatureMin":44,"validTimeLocal":"2026-11-29T07:00:00-0500","windDirectionCardin'
    'al":"S","windSpeed":12,"wxPhraseLong":"Rain"}]}}}}}'
)
PAGE = ('<script id="fusion-metadata" type="application/javascript">'
        'Fusion.contentCache=' + CACHE + ';</script>')


class WanfAtlantaGrayTests(unittest.TestCase):
    def parse(self, target, kind, **overrides):
        options = {'city': 'Atlanta', 'target_date': target, 'measurement_kind': kind,
                   'timezone_name': 'America/New_York', **overrides}
        return parse_gray_daily(PAGE, **options)

    def test_captured_high_uses_the_dated_daily_row(self):
        for target, expected in (('2026-11-27', 50), ('2026-11-28', 61), ('2026-11-29', 65)):
            with self.subTest(target=target):
                parsed = self.parse(target, 'high')
                self.assertEqual(parsed['high'], expected)
                self.assertIsNone(parsed['low'])
                self.assertEqual(parsed['period_start'], target + 'T07:00:00-0500')
                self.assertEqual(parsed['forecast_type'], 'local_daytime_high_predictor')
                self.assertEqual(parsed['cache_updated_at_ms'], 1795795592262)
                self.assertEqual(parsed['location']['city'], 'Atlanta')
                self.assertEqual(parsed['location']['adminDistrictCode'], 'GA')
                self.assertEqual(parsed['location']['ianaTimeZone'], 'America/New_York')

    def test_captured_low_is_the_prior_night_minimum(self):
        for target, expected, source_day in (('2026-11-28', 36, '2026-11-27'),
                                             ('2026-11-29', 41, '2026-11-28'),
                                             ('2026-11-30', 44, '2026-11-29')):
            with self.subTest(target=target):
                parsed = self.parse(target, 'low')
                self.assertEqual(parsed['low'], expected)
                self.assertIsNone(parsed['high'])
                self.assertEqual(parsed['period_start'], source_day + 'T07:00:00-0500')
                self.assertEqual(parsed['forecast_type'], 'local_prior_night_low_predictor')

    def test_refuses_other_city_zone_and_dates_outside_the_capture(self):
        for target, kind, overrides, reason in (
                ('2026-11-28', 'high', {'city': 'Marietta'}, 'source_forecast_city_mismatch'),
                ('2026-11-28', 'high', {'timezone_name': 'America/Chicago'}, 'source_forecast_timezone_mismatch'),
                ('2026-11-30', 'high', {}, 'target_date_not_in_source_forecast'),
                ('2026-11-27', 'low', {}, 'target_date_missing_low_forecast_period')):
            with self.subTest(reason=reason), self.assertRaisesRegex(LocalForecastUnavailable, reason):
                self.parse(target, kind, **overrides)
        with self.assertRaisesRegex(LocalForecastUnavailable, 'source_specific_forecast_schema_missing'):
            parse_gray_daily(PAGE.replace('Fusion.contentCache=', 'Fusion.other='), city='Atlanta',
                             target_date='2026-11-28', measurement_kind='high', timezone_name='America/New_York')

    def test_registry_dispatches_wanf_through_the_gray_adapter(self):
        registry = WeatherRegistry.from_file()
        source = next(s for s in registry.collection_sources('Atlanta') if s['source_id'] == SOURCE_ID)
        self.assertEqual(source['city_id'], 'atlanta_ga')
        self.assertEqual(source['timezone'], 'America/New_York')
        self.assertEqual(source['collection_adapter'], 'gray_fusion_daily_v1')
        self.assertIsNone(source.get('collection_blocker'))
        self.assertEqual((source['status'], source['trust_score'], source['verified']), ('watch_only', 0, False))
        with tempfile.TemporaryDirectory() as root, httpx.Client(
                transport=httpx.MockTransport(lambda request: httpx.Response(200, text=PAGE))) as client:
            store = WeatherCaptureStore(storage_root=root)
            feed = weather_pro.LocalForecastFeed(client, capture_store=store)
            for kind, expected in (('high', 61), ('low', 36)):
                with self.subTest(kind=kind):
                    snapshot = feed.get_forecast(source, target_date='2026-11-28', measurement_kind=kind)
                    self.assertTrue(snapshot.scoreable_forecast, snapshot.availability_reason)
                    self.assertEqual(snapshot.high_temp_f if kind == 'high' else snapshot.low_temp_f, expected)
                    self.assertEqual(snapshot.source_details['forecast_query_url'],
                                     'https://www.atlantanewsfirst.com/weather/')
                    self.assertEqual(snapshot.source_details['source_clock_eligibility'], 'consistent_with_capture')
                    self.assertEqual(store.read_verified(snapshot.source_details['capture']).decode('utf-8'), PAGE)


if __name__ == '__main__':
    unittest.main()
