"""KING 5 Seattle TEGNA field projection from a public capture, not executed JS.

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

SOURCE_ID = 'src_local_king_weather'
# Captured 2026-11-28 16:03:40 UTC (Sat 08:03 PST) from https://www.king5.com/weather.
# The morning page's 10-day rows run Sat Nov 28 - Mon Dec 07; hourly rows start at 8 AM Sat.
CAPTURE_SHA256 = '44eaac9144f0ba62ad0a4232e72298a97cdc69a12abbf3ca721918a5ecaa5c0c'
CAPTURE_BYTES = 239277
FETCHED_AT = datetime.fromisoformat('2026-11-28T16:03:40+00:00')
FIXTURE_SHA256 = 'c8b06333b2ca297f0ba2c68e1f74ee29fb5b51a470603ca4938c60a239500df8'
PAGE = (
    '<script id="initial_tracking_script" data-tag-manager="gtm" data-call-letters="KING" data-canonical-url="https://www.king5.com/weather" data-market="Seattle, WA" data-page-type="subindex" data-product="desktop" data-platform="desktop"></script>\n'
    '<div data-module="weather-10-day"><div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Sat</span><span class="weather-10-day__date weather-10-day__date_visible_true">Nov 28</span><div class="weather-10-day__temperature-high">50</div><div class="weather-10-day__temperature-low">47</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Sun</span><span class="weather-10-day__date weather-10-day__date_visible_true">Nov 29</span><div class="weather-10-day__temperature-high">49</div><div class="weather-10-day__temperature-low">43</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Mon</span><span class="weather-10-day__date weather-10-day__date_visible_true">Nov 30</span><div class="weather-10-day__temperature-high">52</div><div class="weather-10-day__temperature-low">44</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Tue</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 01</span><div class="weather-10-day__temperature-high">49</div><div class="weather-10-day__temperature-low">43</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Wed</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 02</span><div class="weather-10-day__temperature-high">49</div><div class="weather-10-day__temperature-low">44</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Thu</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 03</span><div class="weather-10-day__temperature-high">49</div><div class="weather-10-day__temperature-low">42</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Fri</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 04</span><div class="weather-10-day__temperature-high">49</div><div class="weather-10-day__temperature-low">42</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Sat</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 05</span><div class="weather-10-day__temperature-high">48</div><div class="weather-10-day__temperature-low">40</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Sun</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 06</span><div class="weather-10-day__temperature-high">47</div><div class="weather-10-day__temperature-low">40</div></div>\n'
    '<div class="weather-10-day__row"><span class="weather-10-day__day weather-10-day__day_visible_true">Mon</span><span class="weather-10-day__date weather-10-day__date_visible_true">Dec 07</span><div class="weather-10-day__temperature-high">46</div><div class="weather-10-day__temperature-low">41</div></div></div>\n'
    '<div data-module="weather-hourly"><div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">8 AM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">44&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">9 AM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">45&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">10 AM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">46&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">11 AM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">47&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">12 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">49&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">1 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">50&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">2 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">50&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">3 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">49&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">4 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">48&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">5 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">47&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">6 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">46&deg;</div></div>\n'
    '<div class="weather-hourly__row"><span class="weather-hourly__hour weather-hourly__hour_visible_true">7 PM</span><span class="weather-hourly__day weather-hourly__day_visible_true">Sat</span><div class="weather-hourly__temperature-high">45&deg;</div></div></div>'
)


class KingSeattleTegnaTests(unittest.TestCase):
    def parse(self, target, kind, **overrides):
        options = {'city': 'Seattle', 'target_date': target, 'measurement_kind': kind,
                   'timezone_name': 'America/Los_Angeles', 'fetched_at': FETCHED_AT, **overrides}
        return parse_tegna_daily(PAGE, **options)

    def test_fixture_is_the_pinned_projection(self):
        self.assertEqual(hashlib.sha256(PAGE.encode('utf-8')).hexdigest(), FIXTURE_SHA256)

    def test_captured_high_uses_the_month_day_and_weekday_row(self):
        for target, expected, label in (('2026-11-28', 50, 'Nov 28'), ('2026-11-30', 52, 'Nov 30'),
                                        ('2026-12-01', 49, 'Dec 01'), ('2026-12-07', 46, 'Dec 07')):
            with self.subTest(target=target):
                parsed = self.parse(target, 'high')
                self.assertEqual(parsed['high'], expected)
                self.assertIsNone(parsed['low'])
                self.assertEqual(parsed['raw_forecast']['date'], label)
                self.assertEqual(parsed['period_start'], target + 'T00:00:00-08:00')
                self.assertEqual(parsed['forecast_type'], 'local_date_labelled_high_predictor')
                self.assertEqual(parsed['location']['market'], 'Seattle, WA')
                self.assertEqual(parsed['location']['ianaTimeZone'], 'America/Los_Angeles')
                self.assertFalse(parsed['full_day_measurement_equivalent'])

    def test_captured_low_is_the_same_day_hourly_point_minimum(self):
        # Saturday's 8 AM - 7 PM PST points; the displayed daily low is not used.
        parsed = self.parse('2026-11-28', 'low')
        self.assertEqual(parsed['low'], 44)
        self.assertIsNone(parsed['high'])
        self.assertEqual(parsed['period_start'], '2026-11-28T08:00:00-08:00')
        self.assertEqual(parsed['period_end'], '2026-11-28T19:00:00-08:00')
        self.assertEqual(parsed['forecast_type'], 'local_partial_day_hourly_minimum_predictor')
        self.assertEqual(parsed['raw_forecast']['daily_low_period_status'], 'unverified_not_used')
        self.assertEqual(len(parsed['raw_forecast']['hourly_points']), 12)

    def test_refuses_other_city_zone_stale_clock_and_dates_outside_the_capture(self):
        for target, kind, overrides, reason in (
                ('2026-11-28', 'high', {'city': 'Tacoma'}, 'source_forecast_city_mismatch'),
                ('2026-11-28', 'high', {'timezone_name': 'America/Denver'}, 'source_forecast_timezone_mismatch'),
                ('2026-11-27', 'high', {}, 'target_date_not_in_source_forecast'),
                ('2026-12-08', 'high', {}, 'target_date_not_in_source_forecast'),
                ('2026-11-29', 'low', {}, 'target_date_missing_low_forecast_period'),
                ('2026-11-28', 'low', {'fetched_at': datetime.fromisoformat('2026-11-28T20:03:40+00:00')},
                 'source_hourly_forecast_clock_ambiguous')):
            with self.subTest(reason=reason, target=target), self.assertRaisesRegex(LocalForecastUnavailable, reason):
                self.parse(target, kind, **overrides)

    def test_registry_dispatches_king_through_the_tegna_adapter(self):
        registry = WeatherRegistry.from_file()
        source = next(s for s in registry.collection_sources('Seattle') if s['source_id'] == SOURCE_ID)
        self.assertEqual(source['city_id'], 'seattle_wa')
        self.assertEqual(source['timezone'], 'America/Los_Angeles')
        self.assertEqual(source['collection_adapter'], 'tegna_public_html_v1')
        self.assertIsNone(source.get('collection_blocker'))
        self.assertEqual((source['status'], source['trust_score'], source['verified']), ('watch_only', 0, False))
        with tempfile.TemporaryDirectory() as root, httpx.Client(
                transport=httpx.MockTransport(lambda request: httpx.Response(200, text=PAGE))) as client:
            store = WeatherCaptureStore(storage_root=root)
            feed = weather_pro.LocalForecastFeed(client, capture_store=store)
            for kind, expected in (('high', 50), ('low', 44)):
                with self.subTest(kind=kind):
                    with patch.object(weather_pro, 'datetime', wraps=datetime) as clock:
                        clock.now.return_value = FETCHED_AT
                        snapshot = feed.get_forecast(source, target_date='2026-11-28', measurement_kind=kind)
                    self.assertTrue(snapshot.scoreable_forecast, snapshot.availability_reason)
                    self.assertEqual(snapshot.high_temp_f if kind == 'high' else snapshot.low_temp_f, expected)
                    self.assertEqual(snapshot.source_details['forecast_query_url'], 'https://www.king5.com/weather')
                    self.assertIs(snapshot.source_details['full_day_measurement_equivalent'], False)
                    self.assertEqual(store.read_verified(snapshot.source_details['capture']).decode('utf-8'), PAGE)


if __name__ == '__main__':
    unittest.main()
