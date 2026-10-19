"""Inert FOX Television Stations Nuxt daily forecasts.

``parse_fox_daily`` has the same seam as ``parse_gray_daily``: pass the captured
HTML, city, ISO target date, measurement kind and IANA timezone. Register it as
``fox_nuxt_daily_v1`` in LocalForecastFeed; no secondary HTTP request is needed.
The site's weeklyData contains actual offset-qualified day/night dates, unlike
its current-condition temperature or the human-readable month/day headings.
Only literal values and function-argument scalar references are read. Neither
the Nuxt function body nor any other page JavaScript is executed.
"""
from datetime import date, datetime, timedelta
import json
import math
import re
from zoneinfo import ZoneInfo

from bot.feeds.weather_local import LocalForecastUnavailable, _Scripts


class _FoxPage(_Scripts):
    def __init__(self):
        super().__init__()
        self.divs = []
        self.cities = []

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        if tag == 'div':
            is_city = 'city' in dict(attrs).get('class', '').split()
            self.divs.append(is_city)
            if is_city:
                self.cities.append('')

    def handle_data(self, data):
        super().handle_data(data)
        if any(self.divs):
            self.cities[-1] += data

    def handle_endtag(self, tag):
        super().handle_endtag(tag)
        if tag == 'div' and self.divs:
            self.divs.pop()


def _balanced(text, start):
    """Return one balanced literal/argument group without evaluating it."""
    stack = []
    quote = None
    escaped = False
    pairs = {')': '(', ']': '[', '}': '{'}
    for pos in range(start, len(text)):
        char = text[pos]
        if quote:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == quote:
                quote = None
        elif char in ('"', "'"):
            quote = char
        elif char in '([{':
            stack.append(char)
        elif char in ')]}':
            if not stack or stack.pop() != pairs[char]:
                break
            if not stack:
                return text[start:pos + 1]
    raise LocalForecastUnavailable('source_specific_forecast_schema_invalid')


def _arguments(text):
    """Split call arguments, preserving nested non-weather objects as text."""
    values = []
    start = pos = 0
    while pos < len(text):
        char = text[pos]
        if char in '([{':
            pos += len(_balanced(text, pos))
        elif char == '"':
            _, length = json.JSONDecoder().raw_decode(text[pos:])
            pos += length
        elif char == ',':
            values.append(text[start:pos].strip())
            start = pos + 1
            pos += 1
        else:
            pos += 1
    values.append(text[start:].strip())
    return values


_IDENTIFIER = re.compile(r'[$A-Za-z_][$A-Za-z_0-9]*')
_NUMBER = re.compile(r'-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?')


class _LiteralReader:
    """Read only weather literal arrays/objects and scalar argument references."""
    def __init__(self, text, bindings):
        self.text, self.bindings, self.pos = text, bindings, 0

    def _space(self):
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1

    def _expect(self, char):
        self._space()
        if self.text[self.pos:self.pos + 1] != char:
            raise ValueError('unexpected weather literal token')
        self.pos += 1

    def read(self):
        self._space()
        char = self.text[self.pos:self.pos + 1]
        if char == '[':
            self.pos += 1
            items = []
            self._space()
            while self.text[self.pos:self.pos + 1] != ']':
                items.append(self.read())
                self._space()
                if self.text[self.pos:self.pos + 1] != ',':
                    break
                self.pos += 1
            self._expect(']')
            return items
        if char == '{':
            self.pos += 1
            obj = {}
            self._space()
            while self.text[self.pos:self.pos + 1] != '}':
                key = _IDENTIFIER.match(self.text, self.pos)
                if key is None:
                    raise ValueError('nonliteral weather property')
                self.pos = key.end()
                self._expect(':')
                obj[key[0]] = self.read()
                self._space()
                if self.text[self.pos:self.pos + 1] != ',':
                    break
                self.pos += 1
                self._space()
            self._expect('}')
            return obj
        if char == '"':
            value, length = json.JSONDecoder().raw_decode(self.text[self.pos:])
            self.pos += length
            return value
        match = _NUMBER.match(self.text, self.pos)
        if match:
            self.pos = match.end()
            return json.loads(match[0])
        match = _IDENTIFIER.match(self.text, self.pos)
        if match:
            self.pos = match.end()
            token = match[0]
            value = json.loads(self.bindings.get(token, token))
            if isinstance(value, (dict, list)):
                raise ValueError('weather reference is not a scalar literal')
            return value
        raise ValueError('nonliteral weather value')


def _weekly_data(script):
    prefix = 'window.__NUXT__=(function('
    if not script.startswith(prefix):
        raise ValueError('unsupported Nuxt envelope')
    params_end = script.index(')', len(prefix))
    params = script[len(prefix):params_end].split(',')
    call = script.rfind('}(')
    if call < 0:
        raise ValueError('missing Nuxt arguments')
    args = _arguments(_balanced(script, call + 1)[1:-1])
    if len(params) != len(args):
        raise ValueError('Nuxt argument count mismatch')
    start = script.index('weeklyData:') + len('weeklyData:')
    literal = _balanced(script, start)
    reader = _LiteralReader(literal, dict(zip(params, args)))
    rows = reader.read()
    if not isinstance(rows, list):
        raise ValueError('weekly forecast must be an array')
    return rows


def parse_fox_daily(page: str, *, city: str, target_date: str,
                    measurement_kind: str | None = None,
                    timezone_name: str | None = None) -> dict:
    """Return a daytime high or prior-night low, not a full-day actual.

    ``source_updated_at`` remains unknown: article publication timestamps and
    the current-observation clock are not forecast issue times. The caller
    retains its HTTP capture/hash/fetch clock and uses capture-time provenance.
    A night beginning on date D predicts the morning of D+1. Its end is the
    captured following day start, or unknown when that day is not in the page.
    """
    try:
        target = date.fromisoformat(target_date)
        parser = _FoxPage()
        parser.feed(page)
        labels = [' '.join(label.split()) for label in parser.cities]
        source_label = next((label for label in labels
                             if label.split(',')[0].casefold() == city.casefold()), None)
        if source_label is None:
            raise LocalForecastUnavailable('source_forecast_city_mismatch')
        script = next((text.strip() for _, text in parser.scripts if 'window.__NUXT__=' in text), '')
        if not script or 'weeklyData:' not in script:
            raise LocalForecastUnavailable('source_specific_forecast_schema_missing')
        rows = _weekly_data(script)
        is_low = measurement_kind == 'low'
        key = 'night' if is_low else 'day'
        source_date = target - timedelta(days=1) if is_low else target
        row = next((row for row in rows if row.get(key, {}).get('date', '')[:10] == source_date.isoformat()), None)
        if row is None:
            raise LocalForecastUnavailable('target_date_missing_low_forecast_period' if is_low
                                           else 'target_date_not_in_source_forecast')
        period = row[key]
        if is_low:
            end = next((other['day']['date'] for other in rows
                        if other.get('day', {}).get('date', '')[:10] == target.isoformat()), None)
        else:
            end = row.get('night', {}).get('date')
        start = datetime.fromisoformat(period['date'])
        if start.utcoffset() is None:
            raise LocalForecastUnavailable('source_forecast_period_timezone_ambiguous')
        if timezone_name is None or start.utcoffset() != start.astimezone(ZoneInfo(timezone_name)).utcoffset():
            raise LocalForecastUnavailable('source_forecast_timezone_mismatch')
        value = period['temp']
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise LocalForecastUnavailable('source_forecast_temperature_invalid')
        return {'high': None if is_low else value, 'low': value if is_low else None,
                'period_start': period['date'], 'period_end': end,
                'forecast_type': 'local_prior_night_low_predictor' if is_low else 'local_daytime_high_predictor',
                'raw_forecast': row, 'selected_period': key,
                'location': {'city': source_label.split(',')[0], 'displayName': source_label,
                             'ianaTimeZone': timezone_name},
                'cache_updated_at_ms': None,
                'source_updated_at': None,
                'payload_locator': f'script:window.__NUXT__;component:fts-forecast-block;'
                                   f'apiDefaultData.weeklyData[{rows.index(row)}].{key}'}
    except (KeyError, TypeError, ValueError, StopIteration) as exc:
        if isinstance(exc, LocalForecastUnavailable):
            raise
        raise LocalForecastUnavailable('source_specific_forecast_schema_invalid') from exc
