"""CBS 8 San Diego (KFMB) TEGNA field projection from a public capture, not executed JS.

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

SOURCE_ID = 'src_local_kfmb_weather'
# Captured 2026-11-28 04:12:50 UTC (Fri 2026-11-27 20:12 PST) from https://www.cbs8.com/weather.
# The evening page has already dropped Friday's 10-day row; its hourly rows start at 8 PM Fri.
CAPTURE_SHA256 = 'e4df362c30537a9d075954b3be47704dc3a7c2ef2ba0b6544daabe3fa44b52b2'
CAPTURE_BYTES = 234158
FETCHED_AT = datetime.fromisoformat('2026-11-28T04:12:50+00:00')
FIXTURE_SHA256 = '8617640989c325850beffc4119f1864c213fa03690927683e200c7cf0689131f'
PAGE = (
    '<script id="initial_tracking_script" data-tag-manager="gtm" data-call-letters="KFMB" data-canonical-url="https://www.cbs8.com/weather" data-market="San Diego, CA" data-page-type="subindex" data-product="desktop" data-platform="desktop"></script>\n'
    '<div data-module="weather-10-day"><div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Sat</span><span class="weather-10-day__date weather-10-day__date_visible_true">Nov 28</span><div class="weather-10-day__temperature-high">75</div><div class="weather-10-day__temperature-low">54</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Sun</span><span class="weather-10-day__date weather-10-day__date_visible_true">Nov 29</span><div class="weather-10-day__temperature-high">72</div><div class="weather-10-day__temperature-low">58</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Mon</span><span class="weather-10-day__date weather-10-day__date_visible_true">Nov 30</span><div class="weather-10-day__temperature-high">69</div><div class="weather-10-day__temperature-low">60</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Tue</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 01</span><div class="weather-10-day__temperature-high">65</div><div class="weather-10-day__temperature-low">54</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Wed</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 02</span><div class="weather-10-day__temperature-high">67</div><div class="weather-10-day__temperature-low">53</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Thu</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 03</span><div class="weather-10-day__temperature-high">69</div><div class="weather-10-day__temperature-low">54</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Fri</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 04</span><div class="weather-10-day__temperature-high">67</div><div class="weather-10-day__temperature-low">52</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Sat</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 05</span><div class="weather-10-day__temperature-high">67</div><div class="weather-10-day__temperature-low">51</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Sun</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 06</span><div class="weather-10-day__temperature-high">66</div><div class="weather-10-day__temperature-low">49</div></div></div>\n'
    '<div data-module="weather-hourly"><div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">8 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Fri</span><div class="weather-hourly__temperature-high">62&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">9 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Fri</span><div class="weather-hourly__temperature-high">60&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">10 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Fri</span><div class="weather-hourly__temperature-high">60&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">11 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Fri</span><div class="weather-hourly__temperature-high">59&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">12 AM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">59&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">1 AM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">58&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">2 AM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">58&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">3 AM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">57&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">4 AM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">57&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">5 AM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">56&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">6 AM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">56&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">7 AM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">57&deg;</div></div></div>'
)


class KfmbSanDiegoTegnaTests(unittest.TestCase):
    def parse(self, target, kind, **overrides):
        options = {'city': 'San Diego', 'target_date': target, 'measurement_kind': kind,
                   'timezone_name': 'America/Los_Angeles', 'fetched_at': FETCHED_AT, **overrides}
        return parse_tegna_daily(PAGE, **options)

    def test_fixture_is_the_pinned_projection(self):
        self.assertEqual(hashlib.sha256(PAGE.encode('utf-8')).hexdigest(), FIXTURE_SHA256)

    def test_captured_high_uses_the_month_day_and_weekday_row(self):
        for target, expected, label in (('2026-11-28', 75, 'Nov 28'), ('2026-11-29', 72, 'Nov 29'),
                                        ('2026-12-01', 65, 'Dec 01'), ('2026-12-06', 66, 'Dec 06')):
            with self.subTest(target=target):
                parsed = self.parse(target, 'high')
                self.assertEqual(parsed['high'], expected)
                self.assertIsNone(parsed['low'])
                self.assertEqual(parsed['raw_forecast']['date'], label)
                self.assertEqual(parsed['period_start'], target + 'T00:00:00-08:00')
                self.assertEqual(parsed['forecast_type'], 'local_date_labelled_high_predictor')
                self.assertEqual(parsed['location']['market'], 'San Diego, CA')
                self.assertEqual(parsed['location']['ianaTimeZone'], 'America/Los_Angeles')
                self.assertFalse(parsed['full_day_measurement_equivalent'])

    def test_captured_lows_are_target_date_hourly_point_minimums(self):
        # Friday evening points (8-11 PM PST) and Saturday's 12-7 AM points.
        for target, expected, start, end, count in (
                ('2026-11-27', 59, '2026-11-27T20:00:00-08:00', '2026-11-27T23:00:00-08:00', 4),
                ('2026-11-28', 56, '2026-11-28T00:00:00-08:00', '2026-11-28T07:00:00-08:00', 8)):
            with self.subTest(target=target):
                parsed = self.parse(target, 'low')
                self.assertEqual(parsed['low'], expected)
                self.assertIsNone(parsed['high'])
                self.assertEqual((parsed['period_start'], parsed['period_end']), (start, end))
                self.assertEqual(parsed['forecast_type'], 'local_partial_day_hourly_minimum_predictor')
                self.assertEqual(parsed['raw_forecast']['daily_low_period_status'], 'unverified_not_used')
                self.assertEqual(len(parsed['raw_forecast']['hourly_points']), count)

    def test_refuses_other_city_zone_stale_clock_and_dates_outside_the_capture(self):
        for target, kind, overrides, reason in (
                ('2026-11-28', 'high', {'city': 'Los Angeles'}, 'source_forecast_city_mismatch'),
                ('2026-11-28', 'high', {'timezone_name': 'America/Chicago'}, 'source_forecast_timezone_mismatch'),
                # The capture-local date is Friday, but Friday's 10-day row is gone: no substitute.
                ('2026-11-27', 'high', {}, 'target_date_not_in_source_forecast'),
                ('2026-12-07', 'high', {}, 'target_date_not_in_source_forecast'),
                ('2026-11-29', 'low', {}, 'target_date_missing_low_forecast_period'),
                ('2026-11-28', 'low', {'fetched_at': datetime.fromisoformat('2026-11-28T08:12:50+00:00')},
                 'source_hourly_forecast_clock_ambiguous')):
            with self.subTest(reason=reason, target=target), self.assertRaisesRegex(LocalForecastUnavailable, reason):
                self.parse(target, kind, **overrides)

    def test_registry_dispatches_kfmb_through_the_tegna_adapter(self):
        registry = WeatherRegistry.from_file()
        source = next(s for s in registry.collection_sources('San Diego') if s['source_id'] == SOURCE_ID)
        self.assertEqual(source['city_id'], 'san_diego_ca')
        self.assertEqual(source['timezone'], 'America/Los_Angeles')
        self.assertEqual(source['collection_adapter'], 'tegna_public_html_v1')
        self.assertIsNone(source.get('collection_blocker'))
        self.assertEqual((source['status'], source['trust_score'], source['verified']), ('watch_only', 0, False))
        with tempfile.TemporaryDirectory() as root, httpx.Client(
                transport=httpx.MockTransport(lambda request: httpx.Response(200, text=PAGE))) as client:
            store = WeatherCaptureStore(storage_root=root)
            feed = weather_pro.LocalForecastFeed(client, capture_store=store)
            for kind, expected in (('high', 75), ('low', 56)):
                with self.subTest(kind=kind):
                    with patch.object(weather_pro, 'datetime', wraps=datetime) as clock:
                        clock.now.return_value = FETCHED_AT
                        snapshot = feed.get_forecast(source, target_date='2026-11-28', measurement_kind=kind)
                    self.assertTrue(snapshot.scoreable_forecast, snapshot.availability_reason)
                    self.assertEqual(snapshot.high_temp_f if kind == 'high' else snapshot.low_temp_f, expected)
                    self.assertEqual(snapshot.source_details['forecast_query_url'], 'https://www.cbs8.com/weather')
                    self.assertIs(snapshot.source_details['full_day_measurement_equivalent'], False)
                    self.assertEqual(store.read_verified(snapshot.source_details['capture']).decode('utf-8'), PAGE)


if __name__ == '__main__':
    unittest.main()
