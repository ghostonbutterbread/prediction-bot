"""KHOU 11 Houston TEGNA field projection from a public capture, not executed JS.

The fixture keeps the page's initial_tracking_script identity tag and, verbatim,
every weather-10-day and weather-hourly row's day/date/hour/temperature field
elements in source order. Icons, templates, scripts and other page content were
removed; the capture SHA-256 identifies the complete raw page. No live network
or machine-local fixture path is required.
"""
from datetime import datetime
import hashlib
import tempfile
import unittest
from unittest.mock import patch

import httpx

from bot.feeds import weather_pro
from bot.feeds.weather_capture_store import WeatherCaptureStore
from bot.feeds.weather_local import LocalForecastUnavailable
from bot.feeds.weather_local_tegna import parse_tegna_daily
from bot.weather.registry import WeatherRegistry

SOURCE_ID = 'src_local_khou_weather'
# Captured 2026-11-27 16:05:43 UTC (10:05 CST) from https://www.khou.com/weather.
CAPTURE_SHA256 = '8b9694a9959e097b1a02ebee7933a3896aa6a52bda90597dbaa511fe31cb05f1'
CAPTURE_BYTES = 241675
FETCHED_AT = datetime.fromisoformat('2026-11-27T16:05:43+00:00')
FIXTURE_SHA256 = 'bf4b7be5bc12125d7d21a838d25b18a0a390ef663b1bf76bd9b552d1b8435079'
PAGE = (
    '<script id="initial_tracking_script" data-tag-manager="gtm" data-call-letters="KHOU" data-canonical-url="https://www.khou.com/weather" data-market="Houston, TX" data-page-type="subindex" data-product="desktop" data-platform="desktop"></script>\n'
    '<div data-module="weather-10-day"><div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Fri</span><span class="weather-10-day__date weather-10-day__date_visible_true">Nov 27</span><div class="weather-10-day__temperature-high">66</div><div class="weather-10-day__temperature-low">46</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Sat</span><span class="weather-10-day__date weather-10-day__date_visible_true">Nov 28</span><div class="weather-10-day__temperature-high">75</div><div class="weather-10-day__temperature-low">64</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Sun</span><span class="weather-10-day__date weather-10-day__date_visible_true">Nov 29</span><div class="weather-10-day__temperature-high">72</div><div class="weather-10-day__temperature-low">46</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Mon</span><span class="weather-10-day__date weather-10-day__date_visible_true">Nov 30</span><div class="weather-10-day__temperature-high">68</div><div class="weather-10-day__temperature-low">51</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Tue</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 01</span><div class="weather-10-day__temperature-high">76</div><div class="weather-10-day__temperature-low">59</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Wed</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 02</span><div class="weather-10-day__temperature-high">77</div><div class="weather-10-day__temperature-low">64</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Thu</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 03</span><div class="weather-10-day__temperature-high">77</div><div class="weather-10-day__temperature-low">61</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Fri</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 04</span><div class="weather-10-day__temperature-high">72</div><div class="weather-10-day__temperature-low">54</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Sat</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 05</span><div class="weather-10-day__temperature-high">68</div><div class="weather-10-day__temperature-low">55</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Sun</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 06</span><div class="weather-10-day__temperature-high">70</div><div class="weather-10-day__temperature-low">56</div></div></div>\n'
    '<div data-module="weather-hourly"><div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">10 AM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Fri</span><div class="weather-hourly__temperature-high">53&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">11 AM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Fri</span><div class="weather-hourly__temperature-high">58&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">12 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Fri</span><div class="weather-hourly__temperature-high">62&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">1 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Fri</span><div class="weather-hourly__temperature-high">63&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">2 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Fri</span><div class="weather-hourly__temperature-high">64&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">3 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Fri</span><div class="weather-hourly__temperature-high">65&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">4 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Fri</span><div class="weather-hourly__temperature-high">64&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">5 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Fri</span><div class="weather-hourly__temperature-high">61&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">6 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Fri</span><div class="weather-hourly__temperature-high">56&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">7 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Fri</span><div class="weather-hourly__temperature-high">55&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">8 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Fri</span><div class="weather-hourly__temperature-high">53&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">9 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Fri</span><div class="weather-hourly__temperature-high">51&deg;</div></div></div>'
)


class KhouHoustonTegnaTests(unittest.TestCase):
    def parse(self, target, kind, **overrides):
        options = {'city': 'Houston', 'target_date': target, 'measurement_kind': kind,
                   'timezone_name': 'America/Chicago', 'fetched_at': FETCHED_AT, **overrides}
        return parse_tegna_daily(PAGE, **options)

    def test_fixture_is_the_pinned_projection(self):
        self.assertEqual(hashlib.sha256(PAGE.encode('utf-8')).hexdigest(), FIXTURE_SHA256)

    def test_captured_high_uses_the_month_day_and_weekday_row(self):
        for target, expected, label in (('2026-11-27', 66, 'Nov 27'), ('2026-11-28', 75, 'Nov 28'),
                                        ('2026-12-01', 76, 'Dec 01'), ('2026-12-05', 68, 'Dec 05')):
            with self.subTest(target=target):
                parsed = self.parse(target, 'high')
                self.assertEqual(parsed['high'], expected)
                self.assertIsNone(parsed['low'])
                self.assertEqual(parsed['raw_forecast']['date'], label)
                self.assertEqual(parsed['period_start'], target + 'T00:00:00-06:00')
                self.assertEqual(parsed['forecast_type'], 'local_date_labelled_high_predictor')
                self.assertEqual(parsed['location']['market'], 'Houston, TX')
                self.assertEqual(parsed['location']['ianaTimeZone'], 'America/Chicago')
                self.assertFalse(parsed['full_day_measurement_equivalent'])

    def test_zero_padded_early_month_label_matches_once_and_duplicates_fail_closed(self):
        # The capture labels Dec 1-5 as "Dec 01".."Dec 05" (KUSA's page did too).
        parsed = self.parse('2026-12-01', 'high')
        self.assertEqual((parsed['high'], parsed['raw_forecast']['date'], parsed['raw_forecast']['day']),
                         (76, 'Dec 01', 'Tue'))
        # Mutation: the unpadded spelling of the same label still matches.
        unpadded = PAGE.replace('>Dec 01<', '>Dec 1<', 1)
        self.assertEqual(parse_tegna_daily(unpadded, city='Houston', target_date='2026-12-01',
                                           measurement_kind='high', timezone_name='America/Chicago',
                                           fetched_at=FETCHED_AT)['high'], 76)
        # Mutation: both spellings present for one date is ambiguous, not a pick.
        extra = ('<div class="weather-10-day__row"><span class="weather-10-day__day">Tue</span>'
                 '<span class="weather-10-day__date">Dec 1</span>'
                 '<div class="weather-10-day__temperature-high">70</div>'
                 '<div class="weather-10-day__temperature-low">50</div></div>')
        both = PAGE.replace('<div data-module="weather-10-day">', '<div data-module="weather-10-day">' + extra, 1)
        with self.assertRaisesRegex(LocalForecastUnavailable, 'source_forecast_target_date_ambiguous'):
            parse_tegna_daily(both, city='Houston', target_date='2026-12-01', measurement_kind='high',
                              timezone_name='America/Chicago', fetched_at=FETCHED_AT)

    def test_captured_low_is_the_same_day_hourly_point_minimum(self):
        parsed = self.parse('2026-11-27', 'low')
        self.assertEqual(parsed['low'], 51)
        self.assertIsNone(parsed['high'])
        self.assertEqual(parsed['period_start'], '2026-11-27T10:00:00-06:00')
        self.assertEqual(parsed['period_end'], '2026-11-27T21:00:00-06:00')
        self.assertEqual(parsed['forecast_type'], 'local_partial_day_hourly_minimum_predictor')
        self.assertEqual(parsed['raw_forecast']['daily_low_period_status'], 'unverified_not_used')
        self.assertEqual(len(parsed['raw_forecast']['hourly_points']), 12)

    def test_refuses_other_city_zone_stale_clock_and_dates_outside_the_capture(self):
        for target, kind, overrides, reason in (
                ('2026-11-27', 'high', {'city': 'Galveston'}, 'source_forecast_city_mismatch'),
                ('2026-11-27', 'high', {'timezone_name': 'America/New_York'}, 'source_forecast_timezone_mismatch'),
                ('2026-12-07', 'high', {}, 'target_date_not_in_source_forecast'),
                ('2026-11-26', 'high', {}, 'target_date_not_in_source_forecast'),
                ('2026-11-28', 'low', {}, 'target_date_missing_low_forecast_period'),
                ('2026-11-27', 'low', {'fetched_at': datetime.fromisoformat('2026-11-27T20:05:00+00:00')},
                 'source_hourly_forecast_clock_ambiguous')):
            with self.subTest(reason=reason, target=target), self.assertRaisesRegex(LocalForecastUnavailable, reason):
                self.parse(target, kind, **overrides)

    def test_registry_dispatches_khou_through_the_tegna_adapter(self):
        registry = WeatherRegistry.from_file()
        source = next(s for s in registry.collection_sources('Houston') if s['source_id'] == SOURCE_ID)
        self.assertEqual(source['city_id'], 'houston_tx')
        self.assertEqual(source['timezone'], 'America/Chicago')
        self.assertEqual(source['collection_adapter'], 'tegna_public_html_v1')
        self.assertIsNone(source.get('collection_blocker'))
        self.assertEqual((source['status'], source['trust_score'], source['verified']), ('watch_only', 0, False))
        with tempfile.TemporaryDirectory() as root, httpx.Client(
                transport=httpx.MockTransport(lambda request: httpx.Response(200, text=PAGE))) as client:
            store = WeatherCaptureStore(storage_root=root)
            feed = weather_pro.LocalForecastFeed(client, capture_store=store)
            for kind, expected in (('high', 66), ('low', 51)):
                with self.subTest(kind=kind):
                    with patch.object(weather_pro, 'datetime', wraps=datetime) as clock:
                        clock.now.return_value = FETCHED_AT
                        snapshot = feed.get_forecast(source, target_date='2026-11-27', measurement_kind=kind)
                    self.assertTrue(snapshot.scoreable_forecast, snapshot.availability_reason)
                    self.assertEqual(snapshot.high_temp_f if kind == 'high' else snapshot.low_temp_f, expected)
                    self.assertEqual(snapshot.source_details['forecast_query_url'], 'https://www.khou.com/weather')
                    self.assertIs(snapshot.source_details['full_day_measurement_equivalent'], False)
                    self.assertEqual(store.read_verified(snapshot.source_details['capture']).decode('utf-8'), PAGE)


if __name__ == '__main__':
    unittest.main()
