"""Contradictory retained geography must never be repaired into usable history."""
from __future__ import annotations

import unittest

from bot.feeds.weather_pro import CITY_COORDS
from bot.weather.identity_reconciliation import reconcile_weather_identity


class WeatherIdentityGeographyGuardTests(unittest.TestCase):
    def reconcile(self, metadata: dict, *, label: str = 'San Diego'):
        return reconcile_weather_identity(
            market_id='KXHIGHTSAN-26OCT22-T75',
            question='Will the maximum temperature be above 75 degrees on October 22, 2026?',
            market_metadata={},
            source_record={'source_id': 'open_meteo', 'source_location_city': label,
                           'source_metadata': metadata},
        )

    def test_retained_forecast_location_coordinates_contradict_correct_city_label(self):
        latitude, longitude = CITY_COORDS['san antonio']
        result = self.reconcile({'forecast_location': {
            'city': 'San Diego', 'basis': 'city_centroid',
            'coordinate_source': 'configured_city_coordinates',
            'latitude': latitude, 'longitude': longitude,
        }})
        self.assertTrue(result.reasons, 'known wrong-query coordinates must not pass because city strings agree')
        self.assertNotEqual(result.status, 'repaired_source_location_label')

    def test_provider_city_cannot_repair_label_over_contradictory_provider_coordinates(self):
        latitude, longitude = CITY_COORDS['san antonio']
        result = self.reconcile({'source_reported_location': {
            'city': 'San Diego', 'latitude': latitude, 'longitude': longitude,
        }}, label='San Antonio')
        self.assertTrue(result.reasons, 'provider city text must not outrank its known contradictory coordinates')
        self.assertNotEqual(result.status, 'repaired_source_location_label')
        self.assertEqual(result.source_record['source_location_city'], 'San Antonio')

    def test_agreeing_retained_provider_geography_allows_proven_label_repair(self):
        latitude, longitude = CITY_COORDS['san diego']
        result = self.reconcile({'source_reported_location': {
            'city': 'San Diego', 'latitude': latitude, 'longitude': longitude,
        }}, label='San Antonio')
        self.assertFalse(result.reasons)
        self.assertEqual(result.source_record['source_location_city'], 'San Diego')

    def test_unknown_station_coordinate_is_not_invented_as_another_city(self):
        result = self.reconcile({'forecast_location': {
            'city': 'San Diego', 'basis': 'station',
            'latitude': 32.7338, 'longitude': -117.1933,
        }})
        self.assertFalse(result.reasons)
        self.assertEqual(result.source_record['source_location_city'], 'San Diego')


if __name__ == '__main__':
    unittest.main()
