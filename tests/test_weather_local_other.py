"""Inert, offline regressions for observed non-FOX/non-TEGNA formats."""
import importlib
import importlib.util
import json
import unittest
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo


def sinclair_page(city: object = "Oklahoma City", rows=None, **location):
    if rows is None:
        rows = []
        for day, high, low in [(18, 65, 40), (19, 69, 47), (20, 73, 50)]:
            start = datetime(2026, 10, day, 7, tzinfo=ZoneInfo("America/Chicago"))
            rows.append({"dateTime": int(start.timestamp() * 1000),
                         "origHiTempF": high + 3, "modifiedHiTempF": high, "origLoTempF": low + 9,
                         "modifiedLoTempF": low, "dayOfWeek": start.strftime("%A"),
                         "phraseNight": "Clear overnight"})
    data = {"forecast": {"city": city, "stateAbbreviation": "OK",
                          "latitude": "35.508", "longitude": "-97.478",
                          "weatherForecasts": rows}}
    data['forecast'].update(location)
    # Real Next Flight transport: a JSON string containing a Flight record.
    flight = "1:" + json.dumps(data) + "\n"
    return "<script>self.__next_f.push(" + json.dumps([1, flight]) + ")</script>"


def scripps_page(city: object = "San Diego", units="f", high: object = "81", daily_only=False):
    data = {"currentConditions": [{"city": city, "state": "CA", "tempUnits": units}],
            "days": [{"city": city, "state": "CA", "date": "10/18/2026", "day": "Sunday",
                      "high": "-", "low": "64", "icon": "https://weathercenter.ewscloud.com/static/weathericons/nighttime/clear.png"},
                     {"city": city, "state": "CA", "date": "10/19/2026", "day": "Monday",
                      "high": high, "low": "65", "icon": "https://weathercenter.ewscloud.com/static/weathericons/daytime/partlycloudy.png"}]}
    if daily_only:
        data.pop("currentConditions")
    return '<div data-props="' + escape(json.dumps(data), quote=True) + '"></div>'


def abc_chicago_page(modified="2026-10-19T02:48:33Z", dateline="CHICAGO (WLS) --"):
    body = [[{"type": "h3", "content": ["Here's your 7-day forecast:"]}]]
    for day, high in [("Monday", 66), ("Tuesday", 67), ("Wednesday", 71),
                      ("Thursday", 72), ("Friday", 69), ("Saturday", 68), ("Sunday", 72)]:
        body[0].append({"type": "p", "content": [{"type": "strong", "content": [day]},
                                                f": Sunny. High: {high}, Low: 48"]})
    content = {"storyData": {"modified": modified, "dateline": dateline,
                              "origin": {"source": "WLS"}},
               "articleData": {"mainComponents": [{"name": "Body", "props": {"body": body}}]}}
    return "<script>window['__abcotv__']=" + json.dumps({"page": {"content": content}}) + ";</script>"


class OtherLocalForecastTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec("bot.feeds.weather_local_other"),
                             "other-local dated forecast adapters are missing")
        return importlib.import_module("bot.feeds.weather_local_other")

    def test_sinclair_high_uses_dated_rsc_not_current_conditions(self):
        parsed = self.module().parse_sinclair_daily(
            sinclair_page(), city="oklahoma city", target_date="2026-10-19",
            measurement_kind="high", timezone_name="America/Chicago")
        self.assertEqual(parsed["high"], 69)
        self.assertIsNone(parsed["low"])
        self.assertEqual(parsed["period_start"], "2026-10-19T07:00:00-05:00")
        self.assertEqual(parsed["location"]["city"], "Oklahoma City")
        self.assertEqual(parsed["location"]["ianaTimeZone"], "America/Chicago")
        self.assertEqual(parsed["raw_forecast"]["origHiTempF"], 72)

    def test_sinclair_low_uses_previous_night_and_editor_override(self):
        parsed = self.module().parse_sinclair_daily(
            sinclair_page(), city="oklahoma city", target_date="2026-10-19",
            measurement_kind="low", timezone_name="America/Chicago")
        self.assertEqual(parsed["low"], 40)
        self.assertIsNone(parsed["high"])
        self.assertEqual(parsed["raw_forecast"]["origLoTempF"], 49)
        self.assertTrue(parsed["period_start"].startswith("2026-10-18"))
        self.assertEqual(parsed["forecast_type"], "local_prior_night_low_predictor")

    def test_sinclair_rejects_wrong_city_zone_missing_date_and_bad_temperature(self):
        module = self.module()
        for options, reason in [
            ({"city": "newark"}, "source_forecast_city_mismatch"),
            ({"timezone_name": "America/New_York"}, "source_forecast_timezone_mismatch"),
            ({"target_date": "2026-12-01"}, "target_date_not_in_source_forecast"),
        ]:
            with self.subTest(reason=reason), self.assertRaisesRegex(module.LocalForecastUnavailable, reason):
                module.parse_sinclair_daily(sinclair_page(), **{
                    "city": "oklahoma city", "target_date": "2026-10-19",
                    "timezone_name": "America/Chicago", **options})
        for bad in [True, float("nan"), 150, "69"]:
            row = {"dateTime": 1792411200000, "origHiTempF": bad}
            with self.subTest(bad=bad), self.assertRaisesRegex(
                    module.LocalForecastUnavailable, "source_forecast_temperature_invalid"):
                module.parse_sinclair_daily(sinclair_page(rows=[row]), city="oklahoma city",
                                            target_date="2026-10-19", timezone_name="America/Chicago")

    def test_scripps_decodes_data_props_explicit_date_and_fahrenheit(self):
        module = self.module()
        self.assertTrue(hasattr(module, "parse_scripps_daily"), "Scripps parser missing")
        high = module.parse_scripps_daily(scripps_page(), city="san diego", target_date="2026-10-19",
                                          measurement_kind="high", timezone_name="America/Los_Angeles")
        self.assertEqual(high["high"], 81)
        self.assertIsNone(high["low"])
        self.assertEqual(high["period_start"], "2026-10-19T00:00:00-07:00")
        self.assertEqual(high["raw_forecast"]["date"], "10/19/2026")
        low = module.parse_scripps_daily(scripps_page(), city="san diego", target_date="2026-10-19",
                                         measurement_kind="low", timezone_name="America/Los_Angeles")
        self.assertEqual(low["low"], 64)
        self.assertIsNone(low["high"])
        self.assertEqual(low["raw_forecast"]["date"], "10/18/2026")
        self.assertEqual(low["period_basis"], "source_calendar_date_label")

    def test_chicago_article_anchors_weekdays_to_source_clock_not_fetch_clock(self):
        module = self.module()
        self.assertTrue(hasattr(module, "parse_abc_chicago_daily"), "WLS dated article parser missing")
        result = module.parse_abc_chicago_daily(abc_chicago_page(), city="chicago",
                                                target_date="2026-10-19", measurement_kind="high",
                                                timezone_name="America/Chicago")
        self.assertEqual(result["high"], 66)
        self.assertIsNone(result["low"])
        self.assertEqual(result["source_updated_at"], "2026-10-19T02:48:33+00:00")
        self.assertEqual(result["period_start"], "2026-10-19T00:00:00-05:00")
        self.assertEqual(result["raw_forecast"]["weekday"], "Monday")
        sunday = module.parse_abc_chicago_daily(abc_chicago_page(), city="chicago",
                                                target_date="2026-10-25", measurement_kind="high")
        self.assertEqual(sunday["high"], 72)
        for options, reason in [
            ({"measurement_kind": "low"}, "source_low_period_semantics_unproven"),
            ({"city": "newark"}, "source_forecast_city_mismatch"),
            ({"target_date": "2026-10-26"}, "target_date_not_in_source_forecast"),
        ]:
            with self.subTest(reason=reason), self.assertRaisesRegex(module.LocalForecastUnavailable, reason):
                module.parse_abc_chicago_daily(abc_chicago_page(), **{
                    "city": "chicago", "target_date": "2026-10-19", **options})

    def test_eight_sources_have_explicit_adapters_or_observed_data_blockers(self):
        module = self.module()
        self.assertTrue(hasattr(module, "OTHER_LOCAL_ADAPTERS"), "parent wiring seam missing")
        expected = {"src_local_" + name + "_weather" for name in
                    ("wfxt", "wls", "wsvn", "kokh", "abc7ny", "kgtv", "wdrb", "6abc")}
        self.assertEqual(set(module.OTHER_LOCAL_ADAPTERS) | set(module.OTHER_LOCAL_UNAVAILABLE_REASONS), expected)
        self.assertFalse(set(module.OTHER_LOCAL_ADAPTERS) & set(module.OTHER_LOCAL_UNAVAILABLE_REASONS))
        self.assertEqual(module.OTHER_LOCAL_URLS["src_local_wdrb_weather"], "https://www.wdrbwave.com/weather/")
        self.assertEqual(module.OTHER_LOCAL_URLS["src_local_kgtv_weather"], "https://www.10news.com/weather/daily-forecast")
        cache = {"wx-current-conditions-v3": {"{}": {"lastModified": 1792378476324,
                 "data": {"imperial": {"location": {"city": "Louisville", "ianaTimeZone": "America/New_York"},
                                      "dailyForecast": [
                                          {"validTimeLocal": "2026-10-18T07:00:00-0400", "temperatureMax": 69, "temperatureMin": 42},
                                          {"validTimeLocal": "2026-10-19T07:00:00-0400", "temperatureMax": 70, "temperatureMin": 44}]}}}}}
        page = '<script id="fusion-metadata">Fusion.contentCache=' + json.dumps(cache) + ';</script>'
        adapter = module.OTHER_LOCAL_ADAPTERS["src_local_wdrb_weather"]
        for kind, wanted in [("high", 70), ("low", 42)]:
            with self.subTest(kind=kind):
                value = adapter(page, city="louisville", target_date="2026-10-19", measurement_kind=kind,
                                timezone_name="America/New_York")
                self.assertEqual(value[kind], wanted)
                self.assertEqual(value["cache_updated_at_ms"], 1792378476324)
        for source_id, reason in module.OTHER_LOCAL_UNAVAILABLE_REASONS.items():
            self.assertNotIn("needs_parser", reason)
            self.assertNotIn("adapter_not_implemented", reason)

    def test_scripps_daily_page_has_days_without_current_conditions(self):
        result = self.module().parse_scripps_daily(scripps_page(daily_only=True), city="san diego",
                                                   target_date="2026-10-19", measurement_kind="high")
        self.assertEqual(result["high"], 81)
        self.assertEqual(result["location"]["city"], "San Diego")
        self.assertEqual(result["location"]["ianaTimeZone"], "America/Los_Angeles")

    def test_scripps_rejects_placeholder_units_wrong_city_and_night_date_gap(self):
        module = self.module()
        for page, options, reason in [
            (scripps_page(high="-"), {}, "source_forecast_temperature_invalid"),
            (scripps_page(high=True), {}, "source_forecast_temperature_invalid"),
            (scripps_page(units="c"), {}, "source_forecast_units_not_fahrenheit"),
            (scripps_page(city="Los Angeles"), {}, "source_forecast_city_mismatch"),
            (scripps_page(), {"timezone_name": "America/New_York"}, "source_forecast_timezone_mismatch"),
            (scripps_page(), {"target_date": "2026-10-18", "measurement_kind": "low"}, "target_date_missing_low_forecast_period"),
        ]:
            with self.subTest(reason=reason), self.assertRaisesRegex(module.LocalForecastUnavailable, reason):
                module.parse_scripps_daily(page, **{"city": "san diego", "target_date": "2026-10-19", **options})

    def test_malformed_source_payloads_return_explicit_unavailable(self):
        module = self.module()
        huge_clock = sinclair_page(rows=[{"dateTime": 10**30, "origHiTempF": 69}])
        for adapter, page, city in [
            (module.parse_sinclair_daily, huge_clock, "oklahoma city"),
            (module.parse_sinclair_daily, '<script>self.__next_f.push([1])</script>', "oklahoma city"),
            (module.parse_scripps_daily, '<div data-props="{&quot;days&quot;:broken}"></div>', "san diego"),
            (module.parse_abc_chicago_daily, abc_chicago_page(modified="not-a-date"), "chicago"),
            (module.parse_abc_chicago_daily, abc_chicago_page(modified="2026-10-19T00:00:00"), "chicago"),
            (module.parse_abc_chicago_daily, abc_chicago_page(modified="2026-10-16T10:00:00Z"), "chicago"),
        ]:
            with self.subTest(adapter=adapter.__name__, page=page[:30]), self.assertRaises(module.LocalForecastUnavailable):
                adapter(page, city=city, target_date="2026-10-19")

    def test_split_rsc_is_inert_and_missing_low_does_not_suppress_high(self):
        module = self.module()
        page = sinclair_page()
        encoded = page.removeprefix('<script>self.__next_f.push(').removesuffix(')</script>')
        flight = json.loads(encoded)[1]
        middle = len(flight) // 2
        split = ''.join('<script>self.__next_f.push(' + json.dumps([1, part]) + ');throw new Error("never execute");</script>'
                        for part in [flight[:middle], flight[middle:]])
        result = module.parse_sinclair_daily(split, city="oklahoma city", target_date="2026-10-18")
        self.assertEqual(result["high"], 65)
        with self.assertRaisesRegex(module.LocalForecastUnavailable, "target_date_missing_low_forecast_period"):
            module.parse_sinclair_daily(split, city="oklahoma city", target_date="2026-10-18", measurement_kind="low")


    def test_null_and_type_invalid_provider_city_are_schema_errors(self):
        module = self.module()
        for invalid in (None, 42, [], {}):
            for adapter, page, city in (
                (module.parse_sinclair_daily, sinclair_page(city=invalid), 'oklahoma city'),
                (module.parse_scripps_daily, scripps_page(city=invalid), 'san diego'),
            ):
                with self.subTest(adapter=adapter.__name__, invalid=invalid):
                    with self.assertRaisesRegex(module.LocalForecastUnavailable,
                                                'source_specific_forecast_schema_invalid'):
                        adapter(page, city=city, target_date='2026-10-19')


if __name__ == "__main__":
    unittest.main()
