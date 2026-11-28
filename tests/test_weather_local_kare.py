"""KARE 11 Minneapolis-St. Paul TEGNA field projection from a public capture, not executed JS.

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

SOURCE_ID = 'src_local_kare_weather'
# Captured 2026-11-28 16:03:38 UTC (Sat 10:03 CST) from https://www.kare11.com/weather.
# The morning page's 10-day rows run Sat Nov 28 - Mon Dec 07; hourly rows start at 10 AM Sat.
CAPTURE_SHA256 = '83dc57adfa2a847a3e60780ba9c3c108c8670d4b7e4d40ea55245e383e35b1fb'
CAPTURE_BYTES = 258927
FETCHED_AT = datetime.fromisoformat('2026-11-28T16:03:38+00:00')
FIXTURE_SHA256 = '6cad3cd1b7f86e8ed175726221c1bd30ca0bf90dabdc9b84a6b9484b0f8a26b3'
PAGE = (
    '<script id="initial_tracking_script" data-tag-manager="gtm" data-call-letters="KARE" data-canonical-url="https://www.kare11.com/weather" data-market="Minneapolis, MN" data-page-type="subindex" data-product="desktop" data-platform="desktop"></script>\n'
    '<div data-module="weather-10-day"><div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Sat</span><span class="weather-10-day__date weather-10-day__date_visible_true">Nov 28</span><div class="weather-10-day__temperature-high">47</div><div class="weather-10-day__temperature-low">24</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Sun</span><span class="weather-10-day__date weather-10-day__date_visible_true">Nov 29</span><div class="weather-10-day__temperature-high">36</div><div class="weather-10-day__temperature-low">31</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Mon</span><span class="weather-10-day__date weather-10-day__date_visible_true">Nov 30</span><div class="weather-10-day__temperature-high">38</div><div class="weather-10-day__temperature-low">31</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Tue</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 01</span><div class="weather-10-day__temperature-high">42</div><div class="weather-10-day__temperature-low">32</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Wed</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 02</span><div class="weather-10-day__temperature-high">46</div><div class="weather-10-day__temperature-low">31</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Thu</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 03</span><div class="weather-10-day__temperature-high">44</div><div class="weather-10-day__temperature-low">35</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Fri</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 04</span><div class="weather-10-day__temperature-high">45</div><div class="weather-10-day__temperature-low">30</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Sat</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 05</span><div class="weather-10-day__temperature-high">38</div><div class="weather-10-day__temperature-low">30</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Sun</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 06</span><div class="weather-10-day__temperature-high">41</div><div class="weather-10-day__temperature-low">31</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Mon</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 07</span><div class="weather-10-day__temperature-high">38</div><div class="weather-10-day__temperature-low">27</div></div></div>\n'
    '<div data-module="weather-hourly"><div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">10 AM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">39&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">11 AM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">42&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">12 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">45&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">1 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">46&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">2 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">47&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">3 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">47&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">4 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">46&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">5 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">44&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">6 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">43&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">7 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">42&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">8 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">41&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">9 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">41&deg;</div></div></div>'
)


class KareMinneapolisTegnaTests(unittest.TestCase):
    def parse(self, target, kind, **overrides):
        options = {'city': 'Minneapolis', 'target_date': target, 'measurement_kind': kind,
                   'timezone_name': 'America/Chicago', 'fetched_at': FETCHED_AT, **overrides}
        return parse_tegna_daily(PAGE, **options)

    def test_fixture_is_the_pinned_projection(self):
        self.assertEqual(hashlib.sha256(PAGE.encode('utf-8')).hexdigest(), FIXTURE_SHA256)

    def test_captured_high_uses_the_month_day_and_weekday_row(self):
        for target, expected, label in (('2026-11-28', 47, 'Nov 28'), ('2026-11-29', 36, 'Nov 29'),
                                        ('2026-12-01', 42, 'Dec 01'), ('2026-12-07', 38, 'Dec 07')):
            with self.subTest(target=target):
                parsed = self.parse(target, 'high')
                self.assertEqual(parsed['high'], expected)
                self.assertIsNone(parsed['low'])
                self.assertEqual(parsed['raw_forecast']['date'], label)
                self.assertEqual(parsed['period_start'], target + 'T00:00:00-06:00')
                self.assertEqual(parsed['forecast_type'], 'local_date_labelled_high_predictor')
                self.assertEqual(parsed['location']['market'], 'Minneapolis, MN')
                self.assertEqual(parsed['location']['ianaTimeZone'], 'America/Chicago')
                self.assertFalse(parsed['full_day_measurement_equivalent'])

    def test_captured_low_is_the_same_day_hourly_point_minimum(self):
        # Saturday's 10 AM - 9 PM CST points; the displayed daily low (24) is not used.
        parsed = self.parse('2026-11-28', 'low')
        self.assertEqual(parsed['low'], 39)
        self.assertIsNone(parsed['high'])
        self.assertEqual(parsed['period_start'], '2026-11-28T10:00:00-06:00')
        self.assertEqual(parsed['period_end'], '2026-11-28T21:00:00-06:00')
        self.assertEqual(parsed['forecast_type'], 'local_partial_day_hourly_minimum_predictor')
        self.assertEqual(parsed['raw_forecast']['daily_low_period_status'], 'unverified_not_used')
        self.assertEqual(len(parsed['raw_forecast']['hourly_points']), 12)

    def test_refuses_other_city_zone_stale_clock_and_dates_outside_the_capture(self):
        for target, kind, overrides, reason in (
                ('2026-11-28', 'high', {'city': 'St. Paul'}, 'source_forecast_city_mismatch'),
                ('2026-11-28', 'high', {'timezone_name': 'America/Denver'}, 'source_forecast_timezone_mismatch'),
                ('2026-11-27', 'high', {}, 'target_date_not_in_source_forecast'),
                ('2026-12-08', 'high', {}, 'target_date_not_in_source_forecast'),
                ('2026-11-29', 'low', {}, 'target_date_missing_low_forecast_period'),
                ('2026-11-28', 'low', {'fetched_at': datetime.fromisoformat('2026-11-28T20:03:38+00:00')},
                 'source_hourly_forecast_clock_ambiguous')):
            with self.subTest(reason=reason, target=target), self.assertRaisesRegex(LocalForecastUnavailable, reason):
                self.parse(target, kind, **overrides)

    def test_registry_dispatches_kare_through_the_tegna_adapter(self):
        registry = WeatherRegistry.from_file()
        source = next(s for s in registry.collection_sources('Minneapolis') if s['source_id'] == SOURCE_ID)
        self.assertEqual(source['city_id'], 'minneapolis_mn')
        self.assertEqual(source['timezone'], 'America/Chicago')
        self.assertEqual(source['collection_adapter'], 'tegna_public_html_v1')
        self.assertIsNone(source.get('collection_blocker'))
        self.assertEqual((source['status'], source['trust_score'], source['verified']), ('watch_only', 0, False))
        with tempfile.TemporaryDirectory() as root, httpx.Client(
                transport=httpx.MockTransport(lambda request: httpx.Response(200, text=PAGE))) as client:
            store = WeatherCaptureStore(storage_root=root)
            feed = weather_pro.LocalForecastFeed(client, capture_store=store)
            for kind, expected in (('high', 47), ('low', 39)):
                with self.subTest(kind=kind):
                    with patch.object(weather_pro, 'datetime', wraps=datetime) as clock:
                        clock.now.return_value = FETCHED_AT
                        snapshot = feed.get_forecast(source, target_date='2026-11-28', measurement_kind=kind)
                    self.assertTrue(snapshot.scoreable_forecast, snapshot.availability_reason)
                    self.assertEqual(snapshot.high_temp_f if kind == 'high' else snapshot.low_temp_f, expected)
                    self.assertEqual(snapshot.source_details['forecast_query_url'], 'https://www.kare11.com/weather')
                    self.assertIs(snapshot.source_details['full_day_measurement_equivalent'], False)
                    self.assertEqual(store.read_verified(snapshot.source_details['capture']).decode('utf-8'), PAGE)


if __name__ == '__main__':
    unittest.main()
