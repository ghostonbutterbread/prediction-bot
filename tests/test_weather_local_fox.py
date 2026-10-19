"""FOX Nuxt weather literal excerpts from public captures, not executed JS.

Fixtures retain original weeklyData rows and referenced scalar bindings. Other
page content was removed; capture SHA-256 identifies the complete raw evidence.
No live network or machine-local fixture path is required by this test module.
"""
import importlib.util
import unittest

# Captured 2026-10-19 02:14 UTC. Fields: ID, SHA256, city, timezone, high,
 # prior-night low for Oct 19, Oct 19 evening low (Oct 20 predictor), HTML.
CAPTURED = [
    ('src_local_ktbc_weather', 'c92c81e3ea60f67ef75a54bbba929353e72b7abb918e38a86725b41f7e5c6ea0',
     'Austin', 'America/Chicago', 79, 52, 55,
     '<div class="city">Austin, TX</div><script>window.__NUXT__=(function($,P,Q,ao,br,bs,bt,c,dE,dH,dI,dJ){return {weeklyData:[{hi:c,lo:bs,day:{},night:{day_part:"Tonight",temp:bs,icon_code:Q,date:"2026-10-18T19:00:00-0500"}},{hi:dI,lo:dJ,day:{day_part:"Tomorrow",temp:dI,icon_code:P,date:dE},night:{day_part:"Tomorrow night",temp:dJ,icon_code:ao,date:dH}},{hi:bt,lo:$,day:{day_part:"Tuesday",temp:bt,icon_code:br,date:"2026-10-20T07:00:00-0500"},night:{day_part:"Tuesday night",temp:$,icon_code:ao,date:"2026-10-20T19:00:00-0500"}}]}}(54,30,29,31,32,52,83,null,"2026-10-19T07:00:00-0500","2026-10-19T19:00:00-0500",79,55));</script>'),
    ('src_local_kdfw_weather', '0c10b8b99bba280238c33db95d39acd13f9d651aced803156af425cad78a3f0f',
     'Dallas', 'America/Chicago', 78, 51, 55,
     '<div class="city">Dallas, TX</div><script>window.__NUXT__=(function($,E,aF,ae,ap,bM,c,er,eu,ev,ew,ex){return {weeklyData:[{hi:c,lo:bM,day:{},night:{day_part:"Tonight",temp:bM,icon_code:E,date:"2026-10-18T19:00:00-0500"}},{hi:ev,lo:ew,day:{day_part:"Tomorrow",temp:ev,icon_code:$,date:er},night:{day_part:"Tomorrow night",temp:ew,icon_code:ae,date:eu}},{hi:ex,lo:ap,day:{day_part:"Tuesday",temp:ex,icon_code:aF,date:"2026-10-20T07:00:00-0500"},night:{day_part:"Tuesday night",temp:ap,icon_code:ae,date:"2026-10-20T19:00:00-0500"}}]}}(34,29,32,31,56,51,null,"2026-10-19T07:00:00-0500","2026-10-19T19:00:00-0500",78,55,80));</script>'),
    ('src_local_kriv_weather', 'c54d3f9090620ed664ba1f17694d59e0f69bb59e7a72ccf221c70c6926045001',
     'Houston', 'America/Chicago', 80, 60, 58,
     '<div class="city">Houston, TX</div><script>window.__NUXT__=(function(J,M,aD,an,ao,bs,c,ep,es,et,eu,ev,hi){return {weeklyData:[{hi:c,lo:ao,day:{},night:{day_part:"Tonight",temp:ao,icon_code:J,date:"2026-10-18T19:00:00-0500"}},{hi:et,lo:eu,day:{day_part:"Tomorrow",temp:et,icon_code:M,date:ep},night:{day_part:"Tomorrow night",temp:eu,icon_code:J,date:es}},{hi:ev,lo:an,day:{day_part:"Tuesday",temp:ev,icon_code:bs,date:"2026-10-20T07:00:00-0500"},night:{day_part:"Tuesday night",temp:an,icon_code:aD,date:"2026-10-20T19:00:00-0500"}}]}}(29,30,33,62,60,32,null,"2026-10-19T07:00:00-0500","2026-10-19T19:00:00-0500",80,58,82,"2022-12-15T16:52:20-06:00"));</script>'),
    ('src_local_kttv_weather', '8d23db4b26581d7a769f8d3d4d8a234d16e8395105d6a30a305263a599189589',
     'Los Angeles', 'America/Los_Angeles', 85, 63, 63,
     '<div class="city">Los Angeles, CA</div><script>window.__NUXT__=(function(N,bh,bi,c,cM,cN,cO,cP,o,s){return {weeklyData:[{hi:c,lo:N,day:{},night:{day_part:"Tonight",temp:N,icon_code:o,date:"2026-10-18T19:00:00-0700"}},{hi:cM,lo:N,day:{day_part:"Tomorrow",temp:cM,icon_code:s,date:cN},night:{day_part:"Tomorrow night",temp:N,icon_code:bh,date:cO}},{hi:bi,lo:N,day:{day_part:"Tuesday",temp:bi,icon_code:cP,date:"2026-10-20T07:00:00-0700"},night:{day_part:"Tuesday night",temp:N,icon_code:bh,date:"2026-10-20T19:00:00-0700"}}]}}(63,33,82,null,85,"2026-10-19T07:00:00-0700","2026-10-19T19:00:00-0700",30,31,32));</script>'),
    ('src_local_kmsp_tv_weather', '3998c829f2bd7cf0994dd08225c0d65b07b8ac774c86486b3ea927b847c5f3e4',
     'Minneapolis', 'America/Chicago', 67, 42, 43,
     '<div class="city">Minneapolis, MN</div><script>window.__NUXT__=(function(B,C,S,aC,aF,ck,cm,cn,co,d,t,x){return {weeklyData:[{hi:d,lo:aF,day:{},night:{day_part:"Tonight",temp:aF,icon_code:t,date:"2026-10-18T19:00:00-0500"}},{hi:cn,lo:S,day:{day_part:"Tomorrow",temp:cn,icon_code:aC,date:ck},night:{day_part:"Tomorrow night",temp:S,icon_code:t,date:cm}},{hi:co,lo:B,day:{day_part:"Tuesday",temp:co,icon_code:C,date:"2026-10-20T07:00:00-0500"},night:{day_part:"Tuesday night",temp:B,icon_code:x,date:"2026-10-20T19:00:00-0500"}}]}}(48,32,43,34,42,"2026-10-19T07:00:00-0500","2026-10-19T19:00:00-0500",67,70,null,33,31));</script>'),
    ('src_local_wnyw_weather', '4a2419fa566f14afaf744a26994b76e24b51a81503bacf27364e01b1a9201a35',
     'New York', 'America/New_York', 63, 45, 50,
     '<div class="city">New York, NY</div><script>window.__NUXT__=(function(P,ax,bP,bT,bV,bW,bX,bY,c,l,o){return {weeklyData:[{hi:c,lo:ax,day:{},night:{day_part:"Tonight",temp:ax,icon_code:l,date:"2026-10-18T19:00:00-0400"}},{hi:bV,lo:bW,day:{day_part:"Tomorrow",temp:bV,icon_code:o,date:bP},night:{day_part:"Tomorrow night",temp:bW,icon_code:P,date:bT}},{hi:bX,lo:bY,day:{day_part:"Tuesday",temp:bX,icon_code:30,date:"2026-10-20T07:00:00-0400"},night:{day_part:"Tuesday night",temp:bY,icon_code:l,date:"2026-10-20T19:00:00-0400"}}]}}(33,45,"2026-10-19T07:00:00-0400","2026-10-19T19:00:00-0400",63,50,66,47,null,31,32));</script>'),
    ('src_local_wtxf_weather', '4bc940827a99cb7f49c721f44e60854f9f472e956e70bdb64a9655b5151a9eba',
     'Philadelphia', 'America/New_York', 66, 42, 49,
     '<div class="city">Philadelphia, PA</div><script>window.__NUXT__=(function(E,ak,bT,bX,bY,bZ,c,i,k,u){return {weeklyData:[{hi:c,lo:E,day:{},night:{day_part:"Tonight",temp:E,icon_code:i,date:"2026-10-18T19:00:00-0400"}},{hi:bY,lo:ak,day:{day_part:"Tomorrow",temp:bY,icon_code:k,date:bT},night:{day_part:"Tomorrow night",temp:ak,icon_code:i,date:bX}},{hi:bZ,lo:u,day:{day_part:"Tuesday",temp:bZ,icon_code:30,date:"2026-10-20T07:00:00-0400"},night:{day_part:"Tuesday night",temp:u,icon_code:i,date:"2026-10-20T19:00:00-0400"}}]}}(42,49,"2026-10-19T07:00:00-0400","2026-10-19T19:00:00-0400",66,71,null,31,32,48));</script>'),
    ('src_local_ksaz_weather', 'bd75f008bde6b848abd29f40992a6ca32f4c9d53c82aab228de7852e8427574b',
     'Phoenix', 'America/Phoenix', 89, 68, 71,
     '<div class="city">Phoenix, AZ</div><script>window.__NUXT__=(function(L,ai,aj,bd,bh,bi,c,dp,dt,du,dv,y){return {weeklyData:[{hi:c,lo:bh,day:{},night:{day_part:"Tonight",temp:bh,icon_code:L,date:"2026-10-18T19:00:00-0700"}},{hi:du,lo:ai,day:{day_part:"Tomorrow",temp:du,icon_code:y,date:dp},night:{day_part:"Tomorrow night",temp:ai,icon_code:aj,date:dt}},{hi:dv,lo:bd,day:{day_part:"Tuesday",temp:dv,icon_code:y,date:"2026-10-20T07:00:00-0700"},night:{day_part:"Tuesday night",temp:bd,icon_code:bi,date:"2026-10-20T19:00:00-0700"}}]}}(33,71,29,73,68,4,null,"2026-10-19T07:00:00-0700","2026-10-19T19:00:00-0700",89,91,30));</script>'),
    ('src_local_ktvu_weather', '79a24a72ec4f8e135ef0e774a173d6b7cc01219e1224ebbdc023768acd7b73b7',
     'San Francisco', 'America/Los_Angeles', 64, 54, 53,
     '<div class="city">San Francisco, CA</div><script>window.__NUXT__=(function(A,am,bI,c,ed,ei,en,eo,ep,eq){return {weeklyData:[{hi:c,lo:bI,day:{},night:{day_part:"Tonight",temp:bI,icon_code:A,date:ed}},{hi:en,lo:eo,day:{day_part:"Tomorrow",temp:en,icon_code:A,date:ei},night:{day_part:"Tomorrow night",temp:eo,icon_code:A,date:"2026-10-19T19:00:00-0700"}},{hi:ep,lo:am,day:{day_part:"Tuesday",temp:ep,icon_code:A,date:"2026-10-20T07:00:00-0700"},night:{day_part:"Tuesday night",temp:am,icon_code:eq,date:"2026-10-20T19:00:00-0700"}}]}}(26,56,54,null,"2026-10-18T19:00:00-0700","2026-10-19T07:00:00-0700",64,53,69,45));</script>'),
    ('src_local_kcpq_weather', 'd5580249a42b42c6ace9d9c6d79c53161b4f9b466a7d29e2dc5eacd23c7f8e99',
     'Seattle', 'America/Los_Angeles', 61, 46, 47,
     '<div class="city">Seattle, WA</div><script>window.__NUXT__=(function($,aI,ar,bk,c,dG,dH,dI,dw,dy,s,y){return {weeklyData:[{hi:c,lo:ar,day:{},night:{day_part:"Tonight",temp:ar,icon_code:s,date:dw}},{hi:dG,lo:aI,day:{day_part:"Tomorrow",temp:dG,icon_code:y,date:dy},night:{day_part:"Tomorrow night",temp:aI,icon_code:$,date:"2026-10-19T19:00:00-0700"}},{hi:dH,lo:dI,day:{day_part:"Tuesday",temp:dH,icon_code:32,date:"2026-10-20T07:00:00-0700"},night:{day_part:"Tuesday night",temp:dI,icon_code:bk,date:"2026-10-20T19:00:00-0700"}}]}}(29,47,46,33,null,61,60,44,"2026-10-18T19:00:00-0700","2026-10-19T07:00:00-0700",26,30));</script>'),
]
AUSTIN_PAGE = CAPTURED[0][-1]

class FoxForecastTests(unittest.TestCase):
    def parse(self, page=AUSTIN_PAGE, **overrides):
        self.assertIsNotNone(importlib.util.find_spec('bot.feeds.weather_local_fox'),
                             'the FOX dated predictor adapter is not implemented')
        from bot.feeds.weather_local_fox import parse_fox_daily
        kwargs = dict(city='Austin', target_date='2026-10-19',
                      measurement_kind='high', timezone_name='America/Chicago')
        kwargs.update(overrides)
        return parse_fox_daily(page, **kwargs)

    def test_captured_austin_dated_high_not_current_observation(self):
        parsed = self.parse()
        self.assertEqual(parsed['high'], 79)
        self.assertIsNone(parsed['low'])
        self.assertEqual(parsed['period_start'], '2026-10-19T07:00:00-0500')
        self.assertEqual(parsed['period_end'], '2026-10-19T19:00:00-0500')
        self.assertEqual(parsed['forecast_type'], 'local_daytime_high_predictor')
        self.assertEqual(parsed['location']['city'], 'Austin')
        self.assertEqual(parsed['raw_forecast']['day']['temp'], 79)
        self.assertIsNone(parsed['cache_updated_at_ms'])

    def test_captured_low_uses_prior_night_not_target_evening(self):
        low = self.parse(measurement_kind='low')
        self.assertEqual(low['low'], 52)
        self.assertIsNone(low['high'])
        self.assertEqual(low['period_start'], '2026-10-18T19:00:00-0500')
        self.assertEqual(low['period_end'], '2026-10-19T07:00:00-0500')
        self.assertEqual(low['raw_forecast']['night']['temp'], 52)
        self.assertEqual(low['selected_period'], 'night')
        self.assertEqual(low['forecast_type'], 'local_prior_night_low_predictor')
        following = self.parse(measurement_kind='low', target_date='2026-10-20')
        self.assertEqual(following['low'], 55)
        self.assertEqual(following['period_start'], '2026-10-19T19:00:00-0500')

    def test_invalid_numeric_period_values_are_explicitly_unavailable(self):
        from bot.feeds.weather_local import LocalForecastUnavailable
        for value in ('null', 'true', '"79"', '1e400'):
            with self.subTest(value=value):
                page = AUSTIN_PAGE.replace(',79,55)', ',' + value + ',55)')
                with self.assertRaisesRegex(LocalForecastUnavailable, '^source_forecast_temperature_invalid$'):
                    self.parse(page)

    def test_missing_target_period_is_not_an_undated_temperature_fallback(self):
        from bot.feeds.weather_local import LocalForecastUnavailable
        for kind, reason in [('high', 'target_date_not_in_source_forecast'),
                             ('low', 'target_date_missing_low_forecast_period')]:
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(LocalForecastUnavailable, '^' + reason + '$'):
                    self.parse(target_date='2026-10-18', measurement_kind=kind)

    def test_all_ten_captured_city_highs_and_prior_night_lows(self):
        self.assertEqual(len(CAPTURED), 10)
        self.assertEqual(len({row[0] for row in CAPTURED}), 10)
        for sid, sha, city, zone, high, low, next_low, page in CAPTURED:
            with self.subTest(source_id=sid, capture_sha256=sha):
                parsed = self.parse(page, city=city, timezone_name=zone)
                self.assertEqual(parsed['high'], high)
                self.assertEqual(parsed['raw_forecast']['hi'], high)
                parsed = self.parse(page, city=city, timezone_name=zone, measurement_kind='low')
                self.assertEqual(parsed['low'], low)
                self.assertTrue(parsed['period_start'].startswith('2026-10-18T19:'))
                self.assertTrue(parsed['period_end'].startswith('2026-10-19T07:'))
                later = self.parse(page, city=city, timezone_name=zone,
                                   measurement_kind='low', target_date='2026-10-20')
                self.assertEqual(later['low'], next_low)

    def test_city_and_timezone_proofs_cannot_be_relabelled(self):
        from bot.feeds.weather_local import LocalForecastUnavailable
        with self.assertRaisesRegex(LocalForecastUnavailable, '^source_forecast_city_mismatch$'):
            self.parse(city='Houston')
        with self.assertRaisesRegex(LocalForecastUnavailable, '^source_forecast_timezone_mismatch$'):
            self.parse(timezone_name='America/New_York')
        with self.assertRaisesRegex(LocalForecastUnavailable, '^source_forecast_period_timezone_ambiguous$'):
            self.parse(AUSTIN_PAGE.replace('T07:00:00-0500', 'T07:00:00'))

    def test_page_javascript_and_nonliteral_argument_values_are_never_executed(self):
        from bot.feeds.weather_local import LocalForecastUnavailable
        # A body call is ignored; the source literal alone supplies the forecast.
        inert = AUSTIN_PAGE.replace('{return {weeklyData:',
                                   '{throw new Error("must not run");return {weeklyData:')
        self.assertEqual(self.parse(inert)['high'], 79)
        for expr in ('globalThis.steal()', '({value:79})', '79+1'):
            page = AUSTIN_PAGE.replace(',79,55)', ',' + expr + ',55)')
            with self.subTest(expr=expr):
                with self.assertRaisesRegex(LocalForecastUnavailable, '^source_specific_forecast_schema_invalid$'):
                    self.parse(page)

    def test_schema_absence_is_a_data_failure_not_an_unfinished_parser(self):
        from bot.feeds.weather_local import LocalForecastUnavailable
        with self.assertRaisesRegex(LocalForecastUnavailable, '^source_specific_forecast_schema_missing$'):
            self.parse('<div class="city">Austin, TX</div><div>64 degrees right now</div>')

    def test_original_source_city_is_preserved_after_case_insensitive_lookup(self):
        parsed = self.parse(city='austin')
        self.assertEqual(parsed['location']['city'], 'Austin')
        self.assertEqual(parsed['location']['displayName'], 'Austin, TX')
        self.assertIsNone(parsed['source_updated_at'])

    def test_payload_locator_names_the_selected_original_period(self):
        high = self.parse()
        low = self.parse(measurement_kind='low')
        self.assertTrue(high['payload_locator'].endswith('weeklyData[1].day'))
        self.assertTrue(low['payload_locator'].endswith('weeklyData[0].night'))

    def test_nonarray_weekly_payload_is_an_explicit_schema_failure(self):
        from bot.feeds.weather_local import LocalForecastUnavailable
        literal = AUSTIN_PAGE.split('weeklyData:', 1)[1].split('}}(', 1)[0]
        page = AUSTIN_PAGE.replace(literal, '{broken:' + literal + '}')
        with self.assertRaisesRegex(LocalForecastUnavailable, '^source_specific_forecast_schema_invalid$'):
            self.parse(page)

    def test_last_captured_night_remains_usable_without_inventing_an_end(self):
        parsed = self.parse(measurement_kind='low', target_date='2026-10-21')
        self.assertEqual(parsed['low'], 54)
        self.assertEqual(parsed['period_start'], '2026-10-20T19:00:00-0500')
        self.assertIsNone(parsed['period_end'])

if __name__ == '__main__':
    unittest.main()
