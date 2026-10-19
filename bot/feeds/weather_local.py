"""Inert parsing of observed local forecast payloads; never execute page scripts."""
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
import json
import math
from zoneinfo import ZoneInfo


class LocalForecastUnavailable(ValueError):
    """A page was captured, but cannot support a dated/local predictor."""


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.current = [dict(attrs), '']

    def handle_data(self, data):
        if self.current is not None:
            self.current[1] += data

    def handle_endtag(self, tag):
        if tag == 'script' and self.current is not None:
            self.scripts.append(self.current)
            self.current = None


def local_geography_reason(location: dict, *, city: str, city_id: str) -> str | None:
    """Check explicit contradictions against existing registry/query identities.

    This is not a geocoder or a city-boundary proof. Noncanonical coordinates
    remain source-reported metadata, never verified station/query geography.
    No centroid equality or invented distance/radius is required of a source.
    """
    from bot.feeds.weather_pro import CITY_COORDS
    from bot.weather.registry import WeatherRegistry

    registered = WeatherRegistry.from_file().get_city(city_id)
    # These transports declare the same state-code meaning under different
    # names. Check every declaration, including the selected Scripps day row;
    # choosing a first nonempty alias would conceal explicit contradictions.
    declared_locations = [location]
    if isinstance(location.get('selected_period_location'), dict):
        declared_locations.append(location['selected_period_location'])
    for declared in declared_locations:
        for key in ('adminDistrictCode', 'stateAbbreviation', 'state'):
            state = declared.get(key)
            if state is None:
                continue
            state_code = state.strip().upper() if isinstance(state, str) else None
            if key == 'state' and state_code == 'OKLAHOMA':
                # Observed Sinclair declares the full name beside its code.
                # Interpret that known spelling without rewriting either value.
                state_code = 'OK'
            if state_code != registered['state']:
                return 'source_forecast_state_mismatch'
    lat, lon = location.get('latitude'), location.get('longitude')
    if lat is None and lon is None:
        return None
    if isinstance(lat, bool) or isinstance(lon, bool):
        return 'source_forecast_coordinates_invalid'
    try:
        lat, lon = float(lat), float(lon)
    except (ValueError, TypeError):
        return 'source_forecast_coordinates_invalid'
    if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        return 'source_forecast_coordinates_invalid'
    cities = {name for name, coords in CITY_COORDS.items() if coords == (lat, lon)}
    if cities and city.lower().strip() not in cities:
        return 'source_forecast_coordinate_city_mismatch'
    return None


def parse_gray_daily(page: str, *, city: str, target_date: str,
                     measurement_kind: str | None = None, timezone_name: str | None = None) -> dict:
    """Gray Fusion's imperial dailyForecast is a labelled predictor, not a CLI actual.

    The original minimum belongs to the night after validTimeLocal. It must not
    be relabelled as the same calendar date's morning minimum.
    """
    parser = _Scripts()
    parser.feed(page)
    script = next((text for attrs, text in parser.scripts if attrs.get('id') == 'fusion-metadata'), '')
    marker = 'Fusion.contentCache='
    if marker not in script:
        raise LocalForecastUnavailable('source_specific_forecast_schema_missing')
    try:
        cache, _ = json.JSONDecoder().raw_decode(script.split(marker, 1)[1].lstrip())
        entry = cache['wx-current-conditions-v3']['{}']
        data = entry['data']['imperial']
        location = data['location']
        source_city = location['city']
        if not isinstance(source_city, str):
            raise LocalForecastUnavailable('source_specific_forecast_schema_invalid')
        if source_city.lower() != city.lower():
            raise LocalForecastUnavailable('source_forecast_city_mismatch')
        rows = data['dailyForecast']
        source_date = (date.fromisoformat(target_date) - timedelta(days=1)).isoformat() if measurement_kind == 'low' else target_date
        row = next((row for row in rows if datetime.fromisoformat(row['validTimeLocal']).date().isoformat() == source_date), None)
        if row is None:
            raise LocalForecastUnavailable('target_date_missing_low_forecast_period' if measurement_kind == 'low'
                                           else 'target_date_not_in_source_forecast')
        start = datetime.fromisoformat(row['validTimeLocal'])
        if start.utcoffset() is None:
            raise LocalForecastUnavailable('source_forecast_period_timezone_ambiguous')
        zone = location.get('ianaTimeZone')
        if (timezone_name is not None and zone != timezone_name) or start.utcoffset() != start.astimezone(ZoneInfo(zone)).utcoffset():
            raise LocalForecastUnavailable('source_forecast_timezone_mismatch')
        value = row.get('temperatureMin' if measurement_kind == 'low' else 'temperatureMax')
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or not -60 <= value <= 130:
            raise LocalForecastUnavailable('source_forecast_temperature_invalid')
        return {'high': value if measurement_kind != 'low' else None,
                'low': value if measurement_kind == 'low' else None,
                'period_start': row['validTimeLocal'], 'period_end': None,
                'forecast_type': 'local_prior_night_low_predictor' if measurement_kind == 'low' else 'local_daytime_high_predictor',
                'raw_forecast': row,
                'location': location, 'cache_updated_at_ms': entry.get('lastModified'),
                'payload_locator': 'Fusion.contentCache.wx-current-conditions-v3.{}.data.imperial.dailyForecast'}
    except (KeyError, TypeError, ValueError, StopIteration) as exc:
        if isinstance(exc, LocalForecastUnavailable):
            raise
        raise LocalForecastUnavailable('source_specific_forecast_schema_invalid') from exc
