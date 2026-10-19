"""Inert adapters for the observed WXIA/KUSA/KENS/WUSA public HTML transport.

The site renders numeric forecasts server-side. Its weather-10-day JavaScript
only expands HTML templates; there is no required extra weather API request.
Dates omit the year: a timezone-aware capture clock and the bounded forecast
horizon are required to associate a label, never the requested target's year.
"""
from datetime import date, datetime, time, timedelta, timezone
import hashlib
from html.parser import HTMLParser
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bot.feeds.weather_local import LocalForecastUnavailable


# These are the four source markets' existing city-registry timezones, not
# provider issue-time claims or an allowlist of observed dates/temperatures.
_MARKET_ZONES = {'atlanta': 'America/New_York', 'denver': 'America/Denver',
                 'san antonio': 'America/Chicago', 'washington': 'America/New_York'}
_WEEKDAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
_MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
_VOID = frozenset(('area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'))


class _ForecastHTML(HTMLParser):
    """Read only the observed identity and module-scoped row/field classes."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.markets = []
        self.rows = {'weather-10-day': [], 'weather-hourly': []}
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id') == 'initial_tracking_script':
            self.markets.append(attrs.get('data-market', ''))
        classes = (attrs.get('class') or '').split()
        module = attrs.get('data-module')
        inherited = next((node['module'] for node in reversed(self.stack) if node['module']), None)
        module = module if module in self.rows else inherited
        ignored = tag in ('script', 'style', 'template') or any(node['ignored'] for node in self.stack)
        node = {'tag': tag, 'module': module, 'ignored': ignored, 'row': False, 'field': None}
        if not ignored and module:
            if module + '__row' in classes:
                self.current = {'module': module, 'fields': {}}
                node['row'] = True
            if self.current and self.current['module'] == module:
                for field in ('day', 'date', 'hour', 'temperature-high', 'temperature-low'):
                    if module + '__' + field in classes:
                        node['field'] = field
                        self.current['fields'].setdefault(field, []).append('')
                        break
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self.current:
            for node in reversed(self.stack):
                if node['ignored']:
                    break
                if node['field']:
                    self.current['fields'][node['field']][-1] += data
                    break

    def handle_endtag(self, tag):
        index = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i]['tag'] == tag), None)
        if index is None:
            return
        for node in self.stack[index:]:
            if node['row'] and self.current:
                self.rows[self.current['module']].append(self.current['fields'])
                self.current = None
        del self.stack[index:]


def _field(row, name):
    values = row.get(name, [])
    if len(values) != 1:
        raise LocalForecastUnavailable('source_specific_forecast_schema_invalid')
    return values[0].strip()


def _temperature(text):
    try:
        value = float(text.removesuffix('°').strip())
    except ValueError as exc:
        raise LocalForecastUnavailable('source_forecast_temperature_invalid') from exc
    if not math.isfinite(value) or not -60 <= value <= 130:
        raise LocalForecastUnavailable('source_forecast_temperature_invalid')
    return value


def _hourly_minimum(rows, target, local_capture):
    """Associate forecast point labels with the capture-local hourly sequence.

    Displayed daily lows do not expose their night/date association. Instead,
    retain the minimum of available *target-date hourly forecast points* as a
    partial-day predictor. This is neither an actual nor a full-day forecast low.
    """
    if not rows:
        raise LocalForecastUnavailable('target_date_missing_low_forecast_period')
    first = rows[0]
    first_hour, first_day = _field(first, 'hour'), _field(first, 'day')
    anchor = local_capture.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    # Normal first forecast is the current/next hour; one cache hour either side
    # is tolerated. Never infer a date from the caller's requested target.
    candidates = []
    for offset in range(-1, 3):
        stamp = (anchor + timedelta(hours=offset)).astimezone(local_capture.tzinfo)
        label = f'{stamp.hour % 12 or 12} {"AM" if stamp.hour < 12 else "PM"}'
        if label == first_hour and _WEEKDAYS[stamp.weekday()] == first_day:
            candidates.append(stamp.astimezone(timezone.utc))
    if len(candidates) != 1:
        raise LocalForecastUnavailable('source_hourly_forecast_clock_ambiguous')
    start_utc = candidates[0]
    points = []
    for index, row in enumerate(rows):
        stamp = (start_utc + timedelta(hours=index)).astimezone(local_capture.tzinfo)
        label = f'{stamp.hour % 12 or 12} {"AM" if stamp.hour < 12 else "PM"}'
        if _field(row, 'hour') != label or _field(row, 'day') != _WEEKDAYS[stamp.weekday()]:
            raise LocalForecastUnavailable('source_hourly_forecast_sequence_invalid')
        if stamp.date() == target:
            points.append({**{key: _field(row, key) for key in row},
                           'valid_time': stamp.isoformat(),
                           'temperature_f': _temperature(_field(row, 'temperature-high'))})
    if not points:
        raise LocalForecastUnavailable('target_date_missing_low_forecast_period')
    return (min(point['temperature_f'] for point in points),
            {'hourly_points': points, 'daily_low_period_status': 'unverified_not_used'},
            points[0]['valid_time'], points[-1]['valid_time'])


def parse_tegna_daily(page: str, *, city: str, target_date: str,
                      measurement_kind: str | None = None,
                      timezone_name: str | None = None,
                      fetched_at: datetime | None = None) -> dict:
    """Return a dated forecast predictor in the LocalForecastFeed parser shape.

    A high's period bounds describe the displayed *date label*, not proof of a
    provider full-day measurement interval. Preserve ``period_semantics`` and
    ``full_day_measurement_equivalent`` when archiving the result.
    """
    if measurement_kind not in (None, 'high', 'low'):
        raise LocalForecastUnavailable('source_forecast_measurement_kind_unsupported')
    if not isinstance(fetched_at, datetime) or fetched_at.utcoffset() is None:
        raise LocalForecastUnavailable('source_forecast_capture_clock_missing')
    try:
        target = date.fromisoformat(target_date)
    except (TypeError, ValueError) as exc:
        raise LocalForecastUnavailable('source_forecast_target_date_invalid') from exc
    if not isinstance(timezone_name, str):
        raise LocalForecastUnavailable('source_forecast_timezone_mismatch')
    try:
        zone = ZoneInfo(timezone_name)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise LocalForecastUnavailable('source_forecast_timezone_mismatch') from exc
    parser = _ForecastHTML()
    parser.feed(page)
    parser.close()
    markets = set(parser.markets)
    if len(markets) != 1:
        raise LocalForecastUnavailable('source_forecast_city_missing_or_ambiguous')
    market = markets.pop()
    source_city = market.split(',', 1)[0].strip()
    if source_city.casefold() != city.casefold():
        raise LocalForecastUnavailable('source_forecast_city_mismatch')
    if _MARKET_ZONES.get(source_city.casefold()) != timezone_name:
        raise LocalForecastUnavailable('source_forecast_timezone_mismatch')
    local_capture = fetched_at.astimezone(zone)
    if not local_capture.date() <= target <= local_capture.date() + timedelta(days=10):
        raise LocalForecastUnavailable('target_date_not_in_source_forecast')
    if measurement_kind == 'low':
        value, raw, start_text, end_text = _hourly_minimum(parser.rows['weather-hourly'], target, local_capture)
        forecast_type = 'local_partial_day_hourly_minimum_predictor'
        semantics = 'forecast_point_sample_bounds_inclusive_not_full_day'
        locator = 'weather-hourly.weather-hourly__row'
    else:
        wanted_date = f'{_MONTHS[target.month - 1]} {target.day}'
        rows = [row for row in parser.rows['weather-10-day'] if _field(row, 'date') == wanted_date]
        if not rows:
            raise LocalForecastUnavailable('target_date_not_in_source_forecast')
        if len(rows) != 1:
            raise LocalForecastUnavailable('source_forecast_target_date_ambiguous')
        row = rows[0]
        if _field(row, 'day') != _WEEKDAYS[target.weekday()]:
            raise LocalForecastUnavailable('source_forecast_weekday_mismatch')
        value = _temperature(_field(row, 'temperature-high'))
        raw = {key: _field(row, key) for key in row}
        start_text = datetime.combine(target, time(), zone).isoformat()
        end_text = datetime.combine(target + timedelta(days=1), time(), zone).isoformat()
        forecast_type = 'local_date_labelled_high_predictor'
        semantics = 'calendar_label_bounds_not_provider_measurement_interval'
        locator = 'weather-10-day.weather-10-day__row'
    return {'high': value if measurement_kind != 'low' else None,
            'low': value if measurement_kind == 'low' else None,
            'period_start': start_text, 'period_end': end_text,
            'forecast_type': forecast_type,
            'period_semantics': semantics,
            'full_day_measurement_equivalent': False,
            'raw_forecast': raw,
            'location': {'city': source_city, 'market': market, 'ianaTimeZone': timezone_name,
                         'timezone_basis': 'configured_city_registry'},
            'cache_updated_at_ms': None,
            'source_as_of': fetched_at.isoformat(),
            'source_clock_basis': 'capture_time_provider_issue_time_unavailable',
            'date_mapping_basis': ('displayed_weekday_hour_sequence_anchored_to_capture_local_clock'
                                   if measurement_kind == 'low' else
                                   'displayed_month_day_weekday_within_capture_local_10_day_horizon'),
            'payload_locator': locator,
            'payload_sha256': hashlib.sha256(page.encode('utf-8')).hexdigest()}
