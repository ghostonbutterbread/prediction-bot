"""Keep previously supported provider locations without inventing source trust."""
from __future__ import annotations
import unittest
from zoneinfo import ZoneInfo

from bot.feeds.weather_pro import CITY_COORDS
from bot.weather.market_mapping import WeatherMarketCityMapper
from bot.weather.registry import WeatherRegistry


class WeatherLegacyCityIdentityTests(unittest.TestCase):
    def test_previously_configured_provider_locations_resolve_without_question_rewriting(self):
        mapper = WeatherMarketCityMapper()
        for name in ('portland', 'nashville', 'detroit', 'tampa', 'death valley'):
            question = f'Will the maximum temperature in {name} exceed 75 degrees on October 22, 2026?'
            with self.subTest(city=name):
                self.assertIn(name, CITY_COORDS)
                identity = mapper.resolve(question)
                self.assertIsNotNone(identity)
                assert identity is not None
                self.assertEqual(identity.city.lower(), name)
                self.assertIsNone(identity.primary_source_id)
                city = mapper.registry.get_city(identity.city_id)
                self.assertEqual(city['status'], 'watch')
                self.assertFalse(city['trusted_primary'])
                self.assertFalse(city['trusted_secondary'])
                self.assertFalse(mapper.registry.get_sources(identity.city_id))
                self.assertTrue(ZoneInfo(city['timezone']))

    def test_previous_exact_collector_aliases_preserve_supported_city(self):
        mapper = WeatherMarketCityMapper()
        for alias, name in (('PDX', 'Portland'), ('NSH', 'Nashville'), ('DET', 'Detroit'), ('TPA', 'Tampa')):
            for family in ('KXHIGH', 'KXLOW'):
                for suffix in ('', '-26OCT22-T75'):
                    with self.subTest(alias=alias, family=family, suffix=suffix):
                        identity = mapper.resolve('', market_id=f'{family}{alias}{suffix}')
                        self.assertIsNotNone(identity)
                        assert identity is not None
                        self.assertEqual(identity.city, name)

    def test_no_unverified_death_valley_ticker_is_invented(self):
        registry = WeatherRegistry.from_file()
        city = next((c for c in registry.as_dict()['cities'] if c['city'].lower() == 'death valley'), None)
        self.assertIsNotNone(city)
        assert city is not None
        self.assertEqual(city['ticker_aliases'], [])


if __name__ == '__main__':
    unittest.main()
