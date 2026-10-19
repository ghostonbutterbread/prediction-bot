"""Inert adapters for observed public local-weather transports.

These produce labelled predictors, never settlement observations. No JavaScript,
OCR, API credentials, or live I/O is executed here. See OTHER_LOCAL_URLS for
public pages whose dated payload differs from the registry's landing page.
"""
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
import json
import math
from zoneinfo import ZoneInfo

from bot.feeds.weather_local import LocalForecastUnavailable, _Scripts, parse_gray_daily


def _temperature(value):
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) or not -60 <= value <= 130):
        raise LocalForecastUnavailable('source_forecast_temperature_invalid')
    return value


def parse_sinclair_daily(page: str, *, city: str, target_date: str,
                         measurement_kind: str | None = None,
                         timezone_name: str | None = None) -> dict:
    """Read Sinclair's Next Flight JSON string, not rendered weekday labels."""
    scripts = _Scripts()
    scripts.feed(page)
    fragments = []
    for _, text in scripts.scripts:
        prefix = 'self.__next_f.push('
        if text.startswith(prefix):
            try:
                chunk, _ = json.JSONDecoder().raw_decode(text[len(prefix):])
                if chunk[0] == 1 and isinstance(chunk[1], str):
                    fragments.append(chunk[1])
            except (ValueError, TypeError, IndexError):
                raise LocalForecastUnavailable('source_specific_forecast_schema_invalid')
    flight = ''.join(fragments)
    marker = '"forecast":'
    position = flight.find(marker)
    if position < 0:
        raise LocalForecastUnavailable('source_specific_forecast_schema_missing')
    try:
        forecast, _ = json.JSONDecoder().raw_decode(flight[position + len(marker):].lstrip())
        if forecast['city'].casefold() != city.casefold():
            raise LocalForecastUnavailable('source_forecast_city_mismatch')
        if timezone_name is not None and timezone_name != 'America/Chicago':
            raise LocalForecastUnavailable('source_forecast_timezone_mismatch')
        zone = ZoneInfo('America/Chicago')
        source_date = (date.fromisoformat(target_date) - timedelta(days=1)).isoformat() if measurement_kind == 'low' else target_date
        for row in forecast['weatherForecasts']:
            start = datetime.fromtimestamp(row['dateTime'] / 1000, timezone.utc).astimezone(zone)
            if start.date().isoformat() == source_date:
                suffix = 'LoTempF' if measurement_kind == 'low' else 'HiTempF'
                value = _temperature(row.get('modified' + suffix, row.get('orig' + suffix)))
                return {'high': value if measurement_kind != 'low' else None,
                        'low': value if measurement_kind == 'low' else None,
                        'period_start': start.isoformat(), 'period_end': None,
                        'forecast_type': 'local_prior_night_low_predictor' if measurement_kind == 'low' else 'local_daytime_high_predictor',
                        'raw_forecast': row,
                        'location': {**{key: forecast.get(key) for key in
                                        ('city', 'stateAbbreviation', 'latitude', 'longitude')},
                                     'ianaTimeZone': 'America/Chicago'},
                        'cache_updated_at_ms': None,
                        'payload_locator': 'self.__next_f[1].forecast.weatherForecasts'}
        raise LocalForecastUnavailable('target_date_missing_low_forecast_period' if measurement_kind == 'low'
                                       else 'target_date_not_in_source_forecast')
    except (ValueError, TypeError, KeyError, OverflowError, OSError) as exc:
        if isinstance(exc, LocalForecastUnavailable):
            raise
        raise LocalForecastUnavailable('source_specific_forecast_schema_invalid') from exc


class _ScrippsProps(HTMLParser):
    def __init__(self):
        super().__init__()
        self.weather = None

    def handle_starttag(self, tag, attrs):
        value = dict(attrs).get('data-props')
        if tag == 'div' and value and '"days"' in value:
            self.weather = json.loads(value)


def parse_scripps_daily(page: str, *, city: str, target_date: str,
                        measurement_kind: str | None = None,
                        timezone_name: str | None = None) -> dict:
    """Decode KGTV's date-bearing data-props, including the prior-night low.

    Dates are source row labels, not evidence of exact day/night start times.
    Midnight is the local date-label boundary; period_end remains unknown.
    """
    try:
        parser = _ScrippsProps()
        parser.feed(page)
        data = parser.weather
        if data is None:
            raise LocalForecastUnavailable('source_specific_forecast_schema_missing')
        location = data['currentConditions'][0] if 'currentConditions' in data else data['days'][0]
        if location['city'].casefold() != city.casefold():
            raise LocalForecastUnavailable('source_forecast_city_mismatch')
        # The full page proves tempUnits=f; its daily-only sibling carries the
        # same imperial days schema and values, but omits currentConditions.
        if 'currentConditions' in data and location['tempUnits'] != 'f':
            raise LocalForecastUnavailable('source_forecast_units_not_fahrenheit')
        if timezone_name is not None and timezone_name != 'America/Los_Angeles':
            raise LocalForecastUnavailable('source_forecast_timezone_mismatch')
        wanted = date.fromisoformat(target_date)
        if measurement_kind == 'low':
            wanted -= timedelta(days=1)
        row = next((row for row in data['days']
                    if datetime.strptime(row['date'], '%m/%d/%Y').date() == wanted), None)
        if row is None:
            raise LocalForecastUnavailable('target_date_missing_low_forecast_period' if measurement_kind == 'low'
                                           else 'target_date_not_in_source_forecast')
        if row['city'].casefold() != city.casefold():
            raise LocalForecastUnavailable('source_forecast_city_mismatch')
        try:
            raw_value = row['low' if measurement_kind == 'low' else 'high']
            if isinstance(raw_value, bool):
                raise LocalForecastUnavailable('source_forecast_temperature_invalid')
            value = _temperature(float(raw_value))
        except (TypeError, ValueError) as exc:
            raise LocalForecastUnavailable('source_forecast_temperature_invalid') from exc
        start = datetime.combine(wanted, datetime.min.time(), ZoneInfo('America/Los_Angeles'))
        return {'high': value if measurement_kind != 'low' else None,
                'low': value if measurement_kind == 'low' else None,
                'period_start': start.isoformat(), 'period_end': None,
                'period_basis': 'source_calendar_date_label',
                'forecast_type': 'local_prior_night_low_predictor' if measurement_kind == 'low' else 'local_daytime_high_predictor',
                'raw_forecast': row,
                'location': {**{key: location.get(key) for key in ('city', 'state')},
                             'ianaTimeZone': 'America/Los_Angeles'},
                'cache_updated_at_ms': None,
                'payload_locator': 'div[data-props].days'}
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        if isinstance(exc, LocalForecastUnavailable):
            raise
        raise LocalForecastUnavailable('source_specific_forecast_schema_invalid') from exc


def _node_text(node):
    """Read only article content nodes; never unrelated cards/ads/attributes."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return ''.join(_node_text(child) for child in node)
    if isinstance(node, dict):
        return _node_text(node.get('content', []))
    return ''


def parse_abc_chicago_daily(page: str, *, city: str, target_date: str,
                            measurement_kind: str | None = None,
                            timezone_name: str | None = None) -> dict:
    """Read WLS's seven-day article table anchored to its own modified clock.

    The observed table does not prove which side of midnight its lows cover.
    Preserve daytime highs without silently assigning those lows to a date.
    """
    import re

    try:
        scripts = _Scripts()
        scripts.feed(page)
        marker = "window['__abcotv__']="
        script = next((text for _, text in scripts.scripts if marker in text), None)
        if script is None:
            raise LocalForecastUnavailable('source_specific_forecast_schema_missing')
        data, _ = json.JSONDecoder().raw_decode(script.split(marker, 1)[1].lstrip())
        content = data['page']['content']
        story = content['storyData']
        if (city.casefold() != 'chicago' or story['origin']['source'] != 'WLS'
                or not story['dateline'].startswith('CHICAGO (WLS)')):
            raise LocalForecastUnavailable('source_forecast_city_mismatch')
        if timezone_name is not None and timezone_name != 'America/Chicago':
            raise LocalForecastUnavailable('source_forecast_timezone_mismatch')
        updated = datetime.fromisoformat(story['modified'].replace('Z', '+00:00'))
        if updated.utcoffset() is None:
            raise LocalForecastUnavailable('source_forecast_period_timezone_ambiguous')
        if measurement_kind == 'low':
            raise LocalForecastUnavailable('source_low_period_semantics_unproven')
        zone = ZoneInfo('America/Chicago')
        anchor = updated.astimezone(zone).date()
        weekdays = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
        rows = []
        in_table = False
        for component in content['articleData']['mainComponents']:
            if component.get('name') != 'Body':
                continue
            for batch in component['props']['body']:
                for node in batch:
                    text = _node_text(node).strip()
                    if text == "Here's your 7-day forecast:":
                        in_table = True
                        continue
                    if not in_table or node.get('type') != 'p':
                        continue
                    matched = re.fullmatch(r'(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday):[^\n]*High: (-?\d+), Low: (-?\d+)', text)
                    if matched:
                        weekday, high, low = matched.groups()
                        rows.append((weekday, _temperature(int(high)), low, node))
        if not rows:
            raise LocalForecastUnavailable('source_specific_forecast_schema_missing')
        offset = (weekdays.index(rows[0][0]) - anchor.weekday()) % 7
        if offset > 1:
            raise LocalForecastUnavailable('source_forecast_weekday_date_ambiguous')
        start_date = anchor + timedelta(days=offset)
        for index, (weekday, high, low, node) in enumerate(rows):
            day = start_date + timedelta(days=index)
            if index >= 7 or weekdays[day.weekday()] != weekday:
                raise LocalForecastUnavailable('source_forecast_weekday_date_ambiguous')
        for index, (weekday, high, low, node) in enumerate(rows):
            day = start_date + timedelta(days=index)
            if day.isoformat() != target_date:
                continue
            return {'high': high, 'low': None,
                    'period_start': datetime.combine(day, datetime.min.time(), zone).isoformat(),
                    'period_end': None, 'period_basis': 'source_modified_clock_and_weekday_label',
                    'forecast_type': 'local_daytime_high_predictor',
                    'raw_forecast': {'weekday': weekday, 'paragraph': node, 'source_modified_at': story['modified']},
                    'location': {'city': 'Chicago', 'ianaTimeZone': 'America/Chicago'},
                    'source_updated_at': updated.isoformat(),
                    'cache_updated_at_ms': int(updated.timestamp() * 1000),
                    'payload_locator': "window.__abcotv__.page.content.articleData.mainComponents.Body"}
        raise LocalForecastUnavailable('target_date_not_in_source_forecast')
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        if isinstance(exc, LocalForecastUnavailable):
            raise
        raise LocalForecastUnavailable('source_specific_forecast_schema_invalid') from exc


# Collection owns I/O and raw retention; these seams only select verified formats.
OTHER_LOCAL_ADAPTERS = {
    'src_local_wls_weather': parse_abc_chicago_daily,
    'src_local_kokh_weather': parse_sinclair_daily,
    'src_local_kgtv_weather': parse_scripps_daily,
    'src_local_wdrb_weather': parse_gray_daily,
}
OTHER_LOCAL_URLS = {
    'src_local_wls_weather': 'https://abc7chicago.com/chicago-weather-forecast-7-day-temperatures/110063/',
    'src_local_kgtv_weather': 'https://www.10news.com/weather/daily-forecast',
    # Registry trailing slash is a 404; slashless redirects to the homepage.
    # Homepage canonical host plus its /weather link identifies this new page.
    'src_local_wdrb_weather': 'https://www.wdrbwave.com/weather/',
}
OTHER_LOCAL_UNAVAILABLE_REASONS = {
    # Inspected Boston graphic: numeric values and weekdays, no date/year/issue clock.
    'src_local_wfxt_weather': 'source_forecast_graphic_has_no_calendar_date_or_issue_clock',
    # WSVN's daily widget defaults to Fort Lauderdale, not Miami, and the JS
    # transport calls a licensed provider with an API key. No key was used.
    'src_local_wsvn_weather': 'source_miami_dated_numeric_missing_widget_requires_provider_api_key',
    # Both travel-planner city queries return radar/talent only, no forecast.
    # Their dated articles describe NYC/Philadelphia, not Newark/Trenton.
    'src_local_abc7ny_weather': 'source_newark_numeric_missing_only_new_york_city_article',
    'src_local_6abc_weather': 'source_trenton_numeric_missing_only_philadelphia_article',
}
