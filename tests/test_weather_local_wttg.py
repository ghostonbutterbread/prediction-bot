"""FOX 5 DC (WTTG) Nuxt weeklyData excerpt from a public capture, not executed JS.

The fixture keeps the page's city label, its original weeklyData literal and the
scalar function arguments that literal references, verbatim. Unreferenced
arguments and other page content were removed; the capture SHA-256 identifies
the complete raw page. No live network or machine-local fixture path is needed.

The page labels its city "Washington, D.C."; the registry spells the same
district "DC". The geography check must accept that spelling for Washington
only, without rewriting the source-reported label.
"""
import tempfile
import unittest

import httpx

from bot.feeds import weather_pro
from bot.feeds.weather_capture_store import WeatherCaptureStore
from bot.feeds.weather_local import LocalForecastUnavailable, local_geography_reason
from bot.feeds.weather_local_fox import parse_fox_daily
from bot.weather.registry import WeatherRegistry

SOURCE_ID = 'src_local_wttg_weather'
# Captured 2026-11-27 04:05 UTC (2026-11-26 23:05 EST) from https://www.fox5dc.com/weather.
CAPTURE_SHA256 = '1ae0231642429be5cb64f7a8f5ab816921d711d3f3fd84993d421cacedd3160d'
CAPTURE_BYTES = 281813
PAGE = (
    '<div class="city">Washington, D.C.</div><script>window.__NUXT__=(function(c,t,C,H,Y,ae,aq,as,at,'
    'au,av,aw,br,bs,ds,du,dv,dw,dx,dy,dz){return {weeklyData:[{hi:c,lo:as,day:{},night:{day_part:"Ton'
    'ight",temp:as,icon_code:du,date:"2026-11-26T19:00:00-0500"}},{hi:aq,lo:dw,day:{day_part:"Tomorro'
    'w",temp:aq,icon_code:t,date:ds},night:{day_part:"Tomorrow night",temp:dw,icon_code:Y,date:dv}},{'
    'hi:au,lo:av,day:{day_part:"Saturday",temp:au,icon_code:dx,date:"2026-11-28T07:00:00-0500"},night'
    ':{day_part:"Saturday night",temp:av,icon_code:H,date:"2026-11-28T19:00:00-0500"}},{hi:aw,lo:C,da'
    'y:{day_part:"Sunday",temp:aw,icon_code:t,date:"2026-11-29T07:00:00-0500"},night:{day_part:"Sunda'
    'y night",temp:C,icon_code:12,date:"2026-11-29T19:00:00-0500"}},{hi:ae,lo:bs,day:{day_part:"Monda'
    'y",temp:ae,icon_code:bs,date:"2026-11-30T07:00:00-0500"},night:{day_part:"Monday night",temp:bs,'
    'icon_code:33,date:"2026-11-30T19:00:00-0500"}},{hi:dy,lo:dz,day:{day_part:"Tuesday",temp:dy,icon'
    '_code:dx,date:"2026-12-01T07:00:00-0500"},night:{day_part:"Tuesday night",temp:dz,icon_code:H,da'
    'te:"2026-12-01T19:00:00-0500"}},{hi:ae,lo:av,day:{day_part:"Wednesday",temp:ae,icon_code:32,date'
    ':"2026-12-02T07:00:00-0500"},night:{day_part:"Wednesday night",temp:av,icon_code:H,date:"2026-12'
    '-02T19:00:00-0500"}},{hi:aw,lo:at,day:{day_part:"Thursday",temp:aw,icon_code:br,date:"2026-12-03'
    'T07:00:00-0500"},night:{day_part:"Thursday night",temp:at,icon_code:Y,date:"2026-12-03T19:00:00-'
    '0500"}}]}}(null,26,48,29,27,54,49,46,44,57,35,60,28,34,"2026-11-27T07:00:00-0500",45,"2026-11-27'
    'T19:00:00-0500",42,30,51,31));</script>'
)


class Wttg5WashingtonFoxTests(unittest.TestCase):
    def parse(self, target, kind, **overrides):
        options = {'city': 'Washington', 'target_date': target, 'measurement_kind': kind,
                   'timezone_name': 'America/New_York', **overrides}
        return parse_fox_daily(PAGE, **options)

    def test_captured_high_uses_the_dated_day_period(self):
        for target, expected, index in (('2026-11-27', 49, 1), ('2026-11-28', 57, 2), ('2026-12-03', 60, 7)):
            with self.subTest(target=target):
                parsed = self.parse(target, 'high')
                self.assertEqual(parsed['high'], expected)
                self.assertIsNone(parsed['low'])
                self.assertEqual(parsed['period_start'], target + 'T07:00:00-0500')
                self.assertEqual(parsed['period_end'], target + 'T19:00:00-0500')
                self.assertEqual(parsed['forecast_type'], 'local_daytime_high_predictor')
                self.assertEqual(parsed['location']['city'], 'Washington')
                self.assertEqual(parsed['location']['state'], 'D.C.')
                self.assertEqual(parsed['location']['displayName'], 'Washington, D.C.')
                self.assertTrue(parsed['payload_locator'].endswith(f'weeklyData[{index}].day'))

    def test_captured_low_is_the_prior_night_period(self):
        for target, expected, night_start in (('2026-11-27', 46, '2026-11-26T19:00:00-0500'),
                                              ('2026-11-28', 42, '2026-11-27T19:00:00-0500'),
                                              ('2026-12-03', 35, '2026-12-02T19:00:00-0500')):
            with self.subTest(target=target):
                parsed = self.parse(target, 'low')
                self.assertEqual(parsed['low'], expected)
                self.assertIsNone(parsed['high'])
                self.assertEqual(parsed['period_start'], night_start)
                self.assertEqual(parsed['period_end'], target + 'T07:00:00-0500')
                self.assertEqual(parsed['forecast_type'], 'local_prior_night_low_predictor')

    def test_refuses_other_city_zone_and_dates_outside_the_capture(self):
        for target, kind, overrides, reason in (
                ('2026-11-27', 'high', {'city': 'Baltimore'}, 'source_forecast_city_mismatch'),
                ('2026-11-27', 'high', {'timezone_name': 'America/Chicago'}, 'source_forecast_timezone_mismatch'),
                # The capture's first row is tonight only: its day period is empty.
                ('2026-11-26', 'high', {}, 'target_date_not_in_source_forecast'),
                ('2026-12-04', 'high', {}, 'target_date_not_in_source_forecast'),
                ('2026-11-26', 'low', {}, 'target_date_missing_low_forecast_period')):
            with self.subTest(reason=reason, target=target), self.assertRaisesRegex(LocalForecastUnavailable, reason):
                self.parse(target, kind, **overrides)
        with self.assertRaisesRegex(LocalForecastUnavailable, 'source_specific_forecast_schema_missing'):
            parse_fox_daily(PAGE.replace('weeklyData:', 'otherData:'), city='Washington',
                            target_date='2026-11-27', measurement_kind='high', timezone_name='America/New_York')

    def test_district_spelling_is_accepted_only_for_washington(self):
        self.assertIsNone(local_geography_reason({'city': 'Washington', 'state': 'D.C.'},
                                                 city='washington', city_id='washington_dc'))
        self.assertIsNone(local_geography_reason({'city': 'Washington', 'state': 'DC'},
                                                 city='washington', city_id='washington_dc'))
        self.assertEqual(local_geography_reason({'city': 'Washington', 'state': 'MD'},
                                                city='washington', city_id='washington_dc'),
                         'source_forecast_state_mismatch')
        self.assertEqual(local_geography_reason({'city': 'Atlanta', 'state': 'D.C.'},
                                                city='atlanta', city_id='atlanta_ga'),
                         'source_forecast_state_mismatch')

    def test_registry_dispatches_wttg_through_the_fox_adapter(self):
        registry = WeatherRegistry.from_file()
        source = next(s for s in registry.collection_sources('Washington') if s['source_id'] == SOURCE_ID)
        self.assertEqual(source['city_id'], 'washington_dc')
        self.assertEqual(source['timezone'], 'America/New_York')
        self.assertEqual(source['collection_adapter'], 'fox_nuxt_daily_v1')
        self.assertIsNone(source.get('collection_blocker'))
        self.assertEqual((source['status'], source['trust_score'], source['verified']), ('watch_only', 0, False))
        with tempfile.TemporaryDirectory() as root, httpx.Client(
                transport=httpx.MockTransport(lambda request: httpx.Response(200, text=PAGE))) as client:
            store = WeatherCaptureStore(storage_root=root)
            feed = weather_pro.LocalForecastFeed(client, capture_store=store)
            for kind, expected in (('high', 49), ('low', 46)):
                with self.subTest(kind=kind):
                    snapshot = feed.get_forecast(source, target_date='2026-11-27', measurement_kind=kind)
                    self.assertTrue(snapshot.scoreable_forecast, snapshot.availability_reason)
                    self.assertEqual(snapshot.high_temp_f if kind == 'high' else snapshot.low_temp_f, expected)
                    self.assertEqual(snapshot.source_details['forecast_query_url'],
                                     'https://www.fox5dc.com/weather')
                    self.assertEqual(snapshot.source_details['source_reported_location']['state'], 'D.C.')
                    self.assertEqual(store.read_verified(snapshot.source_details['capture']).decode('utf-8'), PAGE)


if __name__ == '__main__':
    unittest.main()
