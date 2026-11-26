"""FOX 5 Atlanta (WAGA) Nuxt weeklyData excerpt from a public capture, not executed JS.

The fixture keeps the page's city label, its original weeklyData literal and the
scalar function arguments that literal references, verbatim. Unreferenced
arguments and other page content were removed; the capture SHA-256 identifies
the complete raw page. No live network or machine-local fixture path is needed.
"""
import tempfile
import unittest

import httpx

from bot.feeds import weather_pro
from bot.feeds.weather_capture_store import WeatherCaptureStore
from bot.feeds.weather_local import LocalForecastUnavailable
from bot.feeds.weather_local_fox import parse_fox_daily
from bot.weather.registry import WeatherRegistry

SOURCE_ID = 'src_local_waga_weather'
# Captured 2026-11-26 16:10 UTC (11:10 EST) from https://www.fox5atlanta.com/weather.
CAPTURE_SHA256 = '3158fe3040d2369c1b14db3c24c37171959f87fd995ca0831226ac92afd60aba'
CAPTURE_BYTES = 194004
PAGE = (
    '<div class="city">Atlanta, GA</div><script>window.__NUXT__=(function(h,l,E,F,G,H,ag,ah,ai,aj,bM,'
    'bN,bO,bP,bQ,bR,bS,bT,bU,bV,bW,bX){return {weeklyData:[{hi:ag,lo:ah,day:{day_part:"Today",temp:ag'
    ',icon_code:l,date:"2026-11-26T07:00:00-0500"},night:{day_part:"Tonight",temp:ah,icon_code:h,date'
    ':bM}},{hi:bN,lo:bO,day:{day_part:"Tomorrow",temp:bN,icon_code:h,date:bP},night:{day_part:"Tomorr'
    'ow night",temp:bO,icon_code:31,date:"2026-11-27T19:00:00-0500"}},{hi:G,lo:bQ,day:{day_part:"Satu'
    'rday",temp:G,icon_code:32,date:"2026-11-28T07:00:00-0500"},night:{day_part:"Saturday night",temp'
    ':bQ,icon_code:33,date:"2026-11-28T19:00:00-0500"}},{hi:bR,lo:E,day:{day_part:"Sunday",temp:bR,ic'
    'on_code:bS,date:"2026-11-29T07:00:00-0500"},night:{day_part:"Sunday night",temp:E,icon_code:bS,d'
    'ate:"2026-11-29T19:00:00-0500"}},{hi:bT,lo:bU,day:{day_part:"Monday",temp:bT,icon_code:ai,date:"'
    '2026-11-30T07:00:00-0500"},night:{day_part:"Monday night",temp:bU,icon_code:aj,date:"2026-11-30T'
    '19:00:00-0500"}},{hi:bV,lo:F,day:{day_part:"Tuesday",temp:bV,icon_code:ai,date:"2026-12-01T07:00'
    ':00-0500"},night:{day_part:"Tuesday night",temp:F,icon_code:aj,date:"2026-12-01T19:00:00-0500"}}'
    ',{hi:G,lo:H,day:{day_part:"Wednesday",temp:G,icon_code:ai,date:"2026-12-02T07:00:00-0500"},night'
    ':{day_part:"Wednesday night",temp:H,icon_code:aj,date:"2026-12-02T19:00:00-0500"}},{hi:bW,lo:bX,'
    'day:{day_part:"Thursday",temp:bW,icon_code:28,date:"2026-12-03T07:00:00-0500"},night:{day_part:"'
    'Thursday night",temp:bX,icon_code:F,date:"2026-12-03T19:00:00-0500"}}]}}(26,39,47,45,62,48,50,38'
    ',30,29,"2026-11-26T19:00:00-0500",51,34,"2026-11-27T07:00:00-0500",37,63,12,58,44,60,64,54));</s'
    'cript>'
)


class Waga5AtlantaFoxTests(unittest.TestCase):
    def parse(self, target, kind, **overrides):
        options = {'city': 'Atlanta', 'target_date': target, 'measurement_kind': kind,
                   'timezone_name': 'America/New_York', **overrides}
        return parse_fox_daily(PAGE, **options)

    def test_captured_high_uses_the_dated_day_period(self):
        for target, expected, index in (('2026-11-26', 50, 0), ('2026-11-27', 51, 1), ('2026-12-03', 64, 7)):
            with self.subTest(target=target):
                parsed = self.parse(target, 'high')
                self.assertEqual(parsed['high'], expected)
                self.assertIsNone(parsed['low'])
                self.assertEqual(parsed['period_start'], target + 'T07:00:00-0500')
                self.assertEqual(parsed['period_end'], target + 'T19:00:00-0500')
                self.assertEqual(parsed['forecast_type'], 'local_daytime_high_predictor')
                self.assertEqual(parsed['location']['city'], 'Atlanta')
                self.assertEqual(parsed['location']['state'], 'GA')
                self.assertTrue(parsed['payload_locator'].endswith(f'weeklyData[{index}].day'))

    def test_captured_low_is_the_prior_night_period(self):
        for target, expected, night_start in (('2026-11-27', 38, '2026-11-26T19:00:00-0500'),
                                              ('2026-11-28', 34, '2026-11-27T19:00:00-0500'),
                                              ('2026-12-03', 48, '2026-12-02T19:00:00-0500')):
            with self.subTest(target=target):
                parsed = self.parse(target, 'low')
                self.assertEqual(parsed['low'], expected)
                self.assertIsNone(parsed['high'])
                self.assertEqual(parsed['period_start'], night_start)
                self.assertEqual(parsed['period_end'], target + 'T07:00:00-0500')
                self.assertEqual(parsed['forecast_type'], 'local_prior_night_low_predictor')

    def test_refuses_other_city_zone_and_dates_outside_the_capture(self):
        for target, kind, overrides, reason in (
                ('2026-11-27', 'high', {'city': 'Macon'}, 'source_forecast_city_mismatch'),
                ('2026-11-27', 'high', {'timezone_name': 'America/Chicago'}, 'source_forecast_timezone_mismatch'),
                ('2026-12-04', 'high', {}, 'target_date_not_in_source_forecast'),
                ('2026-11-26', 'low', {}, 'target_date_missing_low_forecast_period')):
            with self.subTest(reason=reason), self.assertRaisesRegex(LocalForecastUnavailable, reason):
                self.parse(target, kind, **overrides)
        with self.assertRaisesRegex(LocalForecastUnavailable, 'source_specific_forecast_schema_missing'):
            parse_fox_daily(PAGE.replace('weeklyData:', 'otherData:'), city='Atlanta',
                            target_date='2026-11-27', measurement_kind='high', timezone_name='America/New_York')

    def test_registry_dispatches_waga_through_the_fox_adapter(self):
        registry = WeatherRegistry.from_file()
        source = next(s for s in registry.collection_sources('Atlanta') if s['source_id'] == SOURCE_ID)
        self.assertEqual(source['city_id'], 'atlanta_ga')
        self.assertEqual(source['timezone'], 'America/New_York')
        self.assertEqual(source['collection_adapter'], 'fox_nuxt_daily_v1')
        self.assertIsNone(source.get('collection_blocker'))
        self.assertEqual((source['status'], source['trust_score'], source['verified']), ('watch_only', 0, False))
        with tempfile.TemporaryDirectory() as root, httpx.Client(
                transport=httpx.MockTransport(lambda request: httpx.Response(200, text=PAGE))) as client:
            store = WeatherCaptureStore(storage_root=root)
            feed = weather_pro.LocalForecastFeed(client, capture_store=store)
            for kind, expected in (('high', 51), ('low', 38)):
                with self.subTest(kind=kind):
                    snapshot = feed.get_forecast(source, target_date='2026-11-27', measurement_kind=kind)
                    self.assertTrue(snapshot.scoreable_forecast, snapshot.availability_reason)
                    self.assertEqual(snapshot.high_temp_f if kind == 'high' else snapshot.low_temp_f, expected)
                    self.assertEqual(snapshot.source_details['forecast_query_url'],
                                     'https://www.fox5atlanta.com/weather')
                    self.assertEqual(store.read_verified(snapshot.source_details['capture']).decode('utf-8'), PAGE)


if __name__ == '__main__':
    unittest.main()
