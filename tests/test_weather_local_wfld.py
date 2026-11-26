"""FOX 32 Chicago (WFLD) Nuxt weeklyData excerpt from a public capture, not executed JS.

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

SOURCE_ID = 'src_local_wfld_weather'
# Captured 2026-11-26 16:06 UTC (10:06 CST) from https://www.fox32chicago.com/weather.
CAPTURE_SHA256 = '94a3c89864aaa0070f119fb320633ecc8fc10e4bd1ab411c0445669fa9bf9977'
CAPTURE_BYTES = 198118
PAGE = (
    '<div class="city">Chicago, IL</div><script>window.__NUXT__=(function(i,s,x,L,M,N,T,ac,aB,aC,aD,a'
    'E,cq,ct,cu,cv,cw,cx,cy,cz){return {weeklyData:[{hi:x,lo:aD,day:{day_part:"Today",temp:x,icon_cod'
    'e:i,date:"2026-11-26T07:00:00-0600"},night:{day_part:"Tonight",temp:aD,icon_code:i,date:cq}},{hi'
    ':cu,lo:cv,day:{day_part:"Tomorrow",temp:cu,icon_code:L,date:ct},night:{day_part:"Tomorrow night"'
    ',temp:cv,icon_code:27,date:"2026-11-27T19:00:00-0600"}},{hi:aC,lo:cw,day:{day_part:"Saturday",te'
    'mp:aC,icon_code:T,date:"2026-11-28T07:00:00-0600"},night:{day_part:"Saturday night",temp:cw,icon'
    '_code:M,date:"2026-11-28T19:00:00-0600"}},{hi:x,lo:T,day:{day_part:"Sunday",temp:x,icon_code:i,d'
    'ate:"2026-11-29T07:00:00-0600"},night:{day_part:"Sunday night",temp:T,icon_code:aE,date:"2026-11'
    '-29T19:00:00-0600"}},{hi:N,lo:cx,day:{day_part:"Monday",temp:N,icon_code:T,date:"2026-11-30T07:0'
    '0:00-0600"},night:{day_part:"Monday night",temp:cx,icon_code:aE,date:"2026-11-30T19:00:00-0600"}'
    '},{hi:x,lo:aB,day:{day_part:"Tuesday",temp:x,icon_code:L,date:"2026-12-01T07:00:00-0600"},night:'
    '{day_part:"Tuesday night",temp:aB,icon_code:aE,date:"2026-12-01T19:00:00-0600"}},{hi:cy,lo:s,day'
    ':{day_part:"Wednesday",temp:cy,icon_code:T,date:"2026-12-02T07:00:00-0600"},night:{day_part:"Wed'
    'nesday night",temp:s,icon_code:L,date:"2026-12-02T19:00:00-0600"}},{hi:cz,lo:N,day:{day_part:"Th'
    'ursday",temp:cz,icon_code:28,date:"2026-12-03T07:00:00-0600"},night:{day_part:"Thursday night",t'
    'emp:N,icon_code:ac,date:"2026-12-03T19:00:00-0600"}}]}}(12,44,46,26,11,42,30,45,35,53,41,29,"202'
    '6-11-26T19:00:00-0600","2026-11-27T07:00:00-0600",47,34,40,32,51,54));</script>'
)


class Wfld32ChicagoFoxTests(unittest.TestCase):
    def parse(self, target, kind, **overrides):
        options = {'city': 'Chicago', 'target_date': target, 'measurement_kind': kind,
                   'timezone_name': 'America/Chicago', **overrides}
        return parse_fox_daily(PAGE, **options)

    def test_captured_high_uses_the_dated_day_period(self):
        for target, expected, index in (('2026-11-26', 46, 0), ('2026-11-27', 47, 1), ('2026-12-03', 54, 7)):
            with self.subTest(target=target):
                parsed = self.parse(target, 'high')
                self.assertEqual(parsed['high'], expected)
                self.assertIsNone(parsed['low'])
                self.assertEqual(parsed['period_start'], target + 'T07:00:00-0600')
                self.assertEqual(parsed['period_end'], target + 'T19:00:00-0600')
                self.assertEqual(parsed['forecast_type'], 'local_daytime_high_predictor')
                self.assertEqual(parsed['location']['city'], 'Chicago')
                self.assertEqual(parsed['location']['state'], 'IL')
                self.assertTrue(parsed['payload_locator'].endswith(f'weeklyData[{index}].day'))

    def test_captured_low_is_the_prior_night_period(self):
        for target, expected, night_start in (('2026-11-27', 41, '2026-11-26T19:00:00-0600'),
                                              ('2026-11-28', 34, '2026-11-27T19:00:00-0600'),
                                              ('2026-12-03', 44, '2026-12-02T19:00:00-0600')):
            with self.subTest(target=target):
                parsed = self.parse(target, 'low')
                self.assertEqual(parsed['low'], expected)
                self.assertIsNone(parsed['high'])
                self.assertEqual(parsed['period_start'], night_start)
                self.assertEqual(parsed['period_end'], target + 'T07:00:00-0600')
                self.assertEqual(parsed['forecast_type'], 'local_prior_night_low_predictor')

    def test_refuses_other_city_zone_and_dates_outside_the_capture(self):
        for target, kind, overrides, reason in (
                ('2026-11-27', 'high', {'city': 'Milwaukee'}, 'source_forecast_city_mismatch'),
                ('2026-11-27', 'high', {'timezone_name': 'America/New_York'}, 'source_forecast_timezone_mismatch'),
                ('2026-12-04', 'high', {}, 'target_date_not_in_source_forecast'),
                ('2026-11-26', 'low', {}, 'target_date_missing_low_forecast_period')):
            with self.subTest(reason=reason), self.assertRaisesRegex(LocalForecastUnavailable, reason):
                self.parse(target, kind, **overrides)
        with self.assertRaisesRegex(LocalForecastUnavailable, 'source_specific_forecast_schema_missing'):
            parse_fox_daily(PAGE.replace('weeklyData:', 'otherData:'), city='Chicago',
                            target_date='2026-11-27', measurement_kind='high', timezone_name='America/Chicago')

    def test_registry_dispatches_wfld_through_the_fox_adapter(self):
        registry = WeatherRegistry.from_file()
        source = next(s for s in registry.collection_sources('Chicago') if s['source_id'] == SOURCE_ID)
        self.assertEqual(source['city_id'], 'chicago_il')
        self.assertEqual(source['timezone'], 'America/Chicago')
        self.assertEqual(source['collection_adapter'], 'fox_nuxt_daily_v1')
        self.assertIsNone(source.get('collection_blocker'))
        self.assertEqual((source['status'], source['trust_score'], source['verified']), ('watch_only', 0, False))
        with tempfile.TemporaryDirectory() as root, httpx.Client(
                transport=httpx.MockTransport(lambda request: httpx.Response(200, text=PAGE))) as client:
            store = WeatherCaptureStore(storage_root=root)
            feed = weather_pro.LocalForecastFeed(client, capture_store=store)
            for kind, expected in (('high', 47), ('low', 41)):
                with self.subTest(kind=kind):
                    snapshot = feed.get_forecast(source, target_date='2026-11-27', measurement_kind=kind)
                    self.assertTrue(snapshot.scoreable_forecast, snapshot.availability_reason)
                    self.assertEqual(snapshot.high_temp_f if kind == 'high' else snapshot.low_temp_f, expected)
                    self.assertEqual(snapshot.source_details['forecast_query_url'],
                                     'https://www.fox32chicago.com/weather')
                    self.assertEqual(store.read_verified(snapshot.source_details['capture']).decode('utf-8'), PAGE)


if __name__ == '__main__':
    unittest.main()
