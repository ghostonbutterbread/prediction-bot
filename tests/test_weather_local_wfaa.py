"""WFAA 8 Dallas-Fort Worth TEGNA field projection from a public capture, not executed JS.

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

SOURCE_ID = 'src_local_wfaa_weather'
# Captured 2026-11-28 04:20:05 UTC (Fri 2026-11-27 22:20 CST) from https://www.wfaa.com/weather.
# The late-evening page has dropped Friday's 10-day row; its hourly rows start at 11 PM Fri.
CAPTURE_SHA256 = 'ea7f71c384d2ac228043bf504cd1427af72387665f07ccdb712212756cd01b64'
CAPTURE_BYTES = 229080
FETCHED_AT = datetime.fromisoformat('2026-11-28T04:20:05+00:00')
FIXTURE_SHA256 = '9ca9b86f32920917e9dca2411875efcaa2368a9fa5ef6ad9a2366fdfb7dbefa9'
PAGE = (
    '<script id="initial_tracking_script" data-tag-manager="gtm" data-call-letters="WFAA" data-canonical-url="https://www.wfaa.com/weather" data-market="Dallas, TX" data-page-type="subindex" data-product="desktop" data-platform="desktop"></script>\n'
    '<div data-module="weather-10-day"><div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Sat</span><span class="weather-10-day__date weather-10-day__date_visible_true">Nov 28</span><div class="weather-10-day__temperature-high">72</div><div class="weather-10-day__temperature-low">49</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Sun</span><span class="weather-10-day__date weather-10-day__date_visible_true">Nov 29</span><div class="weather-10-day__temperature-high">60</div><div class="weather-10-day__temperature-low">38</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Mon</span><span class="weather-10-day__date weather-10-day__date_visible_true">Nov 30</span><div class="weather-10-day__temperature-high">64</div><div class="weather-10-day__temperature-low">48</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Tue</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 01</span><div class="weather-10-day__temperature-high">72</div><div class="weather-10-day__temperature-low">56</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Wed</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 02</span><div class="weather-10-day__temperature-high">74</div><div class="weather-10-day__temperature-low">62</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Thu</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 03</span><div class="weather-10-day__temperature-high">72</div><div class="weather-10-day__temperature-low">55</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Fri</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 04</span><div class="weather-10-day__temperature-high">65</div><div class="weather-10-day__temperature-low">44</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Sat</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 05</span><div class="weather-10-day__temperature-high">63</div><div class="weather-10-day__temperature-low">49</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Sun</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 06</span><div class="weather-10-day__temperature-high">61</div><div class="weather-10-day__temperature-low">52</div></div></div>\n'
    '<div data-module="weather-hourly"><div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">11 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Fri</span><div class="weather-hourly__temperature-high">53&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">12 AM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">52&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">1 AM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">51&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">2 AM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">50&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">3 AM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">49&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">4 AM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">48&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">5 AM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">47&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">6 AM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">46&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">7 AM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">47&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">8 AM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">46&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">9 AM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">51&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">10 AM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">58&deg;</div></div></div>'
)


class WfaaDallasTegnaTests(unittest.TestCase):
    def parse(self, target, kind, **overrides):
        options = {'city': 'Dallas', 'target_date': target, 'measurement_kind': kind,
                   'timezone_name': 'America/Chicago', 'fetched_at': FETCHED_AT, **overrides}
        return parse_tegna_daily(PAGE, **options)

    def test_fixture_is_the_pinned_projection(self):
        self.assertEqual(hashlib.sha256(PAGE.encode('utf-8')).hexdigest(), FIXTURE_SHA256)

    def test_captured_high_uses_the_month_day_and_weekday_row(self):
        for target, expected, label in (('2026-11-28', 72, 'Nov 28'), ('2026-11-29', 60, 'Nov 29'),
                                        ('2026-12-01', 72, 'Dec 01'), ('2026-12-06', 61, 'Dec 06')):
            with self.subTest(target=target):
                parsed = self.parse(target, 'high')
                self.assertEqual(parsed['high'], expected)
                self.assertIsNone(parsed['low'])
                self.assertEqual(parsed['raw_forecast']['date'], label)
                self.assertEqual(parsed['period_start'], target + 'T00:00:00-06:00')
                self.assertEqual(parsed['forecast_type'], 'local_date_labelled_high_predictor')
                self.assertEqual(parsed['location']['market'], 'Dallas, TX')
                self.assertEqual(parsed['location']['ianaTimeZone'], 'America/Chicago')
                self.assertFalse(parsed['full_day_measurement_equivalent'])

    def test_captured_lows_are_target_date_hourly_point_minimums(self):
        # One Friday point (11 PM CST) and Saturday's 12-10 AM points.
        for target, expected, start, end, count in (
                ('2026-11-27', 53, '2026-11-27T23:00:00-06:00', '2026-11-27T23:00:00-06:00', 1),
                ('2026-11-28', 46, '2026-11-28T00:00:00-06:00', '2026-11-28T10:00:00-06:00', 11)):
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
                ('2026-11-28', 'high', {'city': 'Fort Worth'}, 'source_forecast_city_mismatch'),
                ('2026-11-28', 'high', {'timezone_name': 'America/Denver'}, 'source_forecast_timezone_mismatch'),
                # The capture-local date is Friday, but Friday's 10-day row is gone: no substitute.
                ('2026-11-27', 'high', {}, 'target_date_not_in_source_forecast'),
                ('2026-12-07', 'high', {}, 'target_date_not_in_source_forecast'),
                ('2026-11-29', 'low', {}, 'target_date_missing_low_forecast_period'),
                ('2026-11-28', 'low', {'fetched_at': datetime.fromisoformat('2026-11-28T08:20:05+00:00')},
                 'source_hourly_forecast_clock_ambiguous')):
            with self.subTest(reason=reason, target=target), self.assertRaisesRegex(LocalForecastUnavailable, reason):
                self.parse(target, kind, **overrides)

    def test_registry_dispatches_wfaa_through_the_tegna_adapter(self):
        registry = WeatherRegistry.from_file()
        source = next(s for s in registry.collection_sources('Dallas') if s['source_id'] == SOURCE_ID)
        self.assertEqual(source['city_id'], 'dallas_tx')
        self.assertEqual(source['timezone'], 'America/Chicago')
        self.assertEqual(source['collection_adapter'], 'tegna_public_html_v1')
        self.assertIsNone(source.get('collection_blocker'))
        self.assertEqual((source['status'], source['trust_score'], source['verified']), ('watch_only', 0, False))
        with tempfile.TemporaryDirectory() as root, httpx.Client(
                transport=httpx.MockTransport(lambda request: httpx.Response(200, text=PAGE))) as client:
            store = WeatherCaptureStore(storage_root=root)
            feed = weather_pro.LocalForecastFeed(client, capture_store=store)
            for kind, expected in (('high', 72), ('low', 46)):
                with self.subTest(kind=kind):
                    with patch.object(weather_pro, 'datetime', wraps=datetime) as clock:
                        clock.now.return_value = FETCHED_AT
                        snapshot = feed.get_forecast(source, target_date='2026-11-28', measurement_kind=kind)
                    self.assertTrue(snapshot.scoreable_forecast, snapshot.availability_reason)
                    self.assertEqual(snapshot.high_temp_f if kind == 'high' else snapshot.low_temp_f, expected)
                    self.assertEqual(snapshot.source_details['forecast_query_url'], 'https://www.wfaa.com/weather')
                    self.assertIs(snapshot.source_details['full_day_measurement_equivalent'], False)
                    self.assertEqual(store.read_verified(snapshot.source_details['capture']).decode('utf-8'), PAGE)


if __name__ == '__main__':
    unittest.main()
