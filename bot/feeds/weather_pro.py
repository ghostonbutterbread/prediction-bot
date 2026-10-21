"""
Multi-source weather engine — fastest, most accurate free weather data.

Sources (in priority order):
1. Open-Meteo: free, no key, hourly updates, 1km resolution
2. NWS: free, no key, US only, 1-2 hour updates
3. OpenWeatherMap: free tier, minute-level updates, needs API key (optional)

Cross-validates multiple sources for higher confidence.
"""

import logging
import math
import re
import httpx
from typing import Any, Mapping, Optional
from datetime import date, datetime, timezone, timedelta
from dataclasses import dataclass, field
from bot.collector_paths import collector_root
from bot.feeds.weather_capture_store import WeatherCaptureStore, CaptureRetentionError

logger = logging.getLogger(__name__)

TEMP_MIN_F = -60
TEMP_MAX_F = 130


@dataclass
class WeatherSnapshot:
    """Unified weather data from any source."""
    city: str
    high_temp_f: float | None
    low_temp_f: float | None
    current_temp_f: float | None
    source: str
    fetched_at: datetime
    forecast_hours_ahead: float
    confidence: float
    # Extra data
    humidity: float = 0
    wind_mph: float = 0
    conditions: str = ""
    as_of: datetime | None = None
    weather_date: str | None = None
    forecast_date: str | None = None
    forecast_start: str | None = None
    forecast_end: str | None = None
    forecast_times: list[str] = field(default_factory=list)
    forecast_period_name: str | None = None
    forecast_period_start: str | None = None
    forecast_period_end: str | None = None
    source_details: dict = field(default_factory=dict)
    scoreable_forecast: bool = True
    availability_reason: str | None = None


@dataclass 
class MultiSourceForecast:
    """Cross-validated forecast from multiple sources."""
    city: str
    high_temp_f: float | None
    low_temp_f: float | None
    current_temp_f: float | None
    sources_used: list
    confidence: float
    fetched_at: datetime
    # Agreements between sources
    source_agreement: float = 0  # 0-1, how much sources agree
    details: dict = field(default_factory=dict)


# City → coordinates for APIs that need lat/lon
CITY_COORDS = {
    "austin": (30.2672, -97.7431),
    "new york": (40.7128, -74.0060),
    "chicago": (41.8781, -87.6298),
    "los angeles": (34.0522, -118.2437),
    "miami": (25.7617, -80.1918),
    "denver": (39.7392, -104.9903),
    "seattle": (47.6062, -122.3321),
    "philadelphia": (39.9526, -75.1652),
    "san francisco": (37.7749, -122.4194),
    "houston": (29.7604, -95.3698),
    "boston": (42.3601, -71.0589),
    "new orleans": (29.9511, -90.0715),
    "phoenix": (33.4484, -112.0740),
    "dallas": (32.7767, -96.7970),
    "minneapolis": (44.9778, -93.2650),
    "atlanta": (33.7490, -84.3880),
    "san antonio": (29.4241, -98.4936),
    "las vegas": (36.1699, -115.1398),
    "oklahoma city": (35.4676, -97.5164),
    "portland": (45.5152, -122.6784),
    "nashville": (36.1627, -86.7816),
    "detroit": (42.3314, -83.0458),
    "san diego": (32.7157, -117.1611),
    "tampa": (27.9506, -82.4572),
    "death valley": (36.5054, -117.0794),
    # City-only query defaults: no new settlement-station identity is asserted.
    "washington": (38.9072, -77.0369),
    "newark": (40.7357, -74.1724),
    "louisville": (38.2527, -85.7585),
    "trenton": (40.2171, -74.7429),
}

# NWS grid/station mapping is forecast and observation context, not proof of
# the market-specific settlement authority.
CITY_NWS = {
    "austin": ("EWX", 152, 91, "KAUS"),        # Austin-Bergstrom Airport
    "new york": ("OKX", 34, 37, "KNYC"),        # Central Park
    "chicago": ("LOT", 76, 73, "KMDW"),         # Chicago-Midway
    "los angeles": ("LOX", 154, 44, "KLAX"),    # LAX
    "miami": ("MFL", 110, 50, "KMIA"),          # Miami International
    "denver": ("BOU", 62, 60, "KDEN"),          # Denver International
    "seattle": ("SEW", 124, 67, "KSEA"),        # SeaTac
    "philadelphia": ("PHI", 49, 75, "KPHL"),    # Philadelphia International
    "san francisco": ("MTR", 85, 105, "KSFO"),  # SFO
    "houston": ("HGX", 65, 97, "KHOU"),         # Houston Hobby
    "boston": ("BOX", 71, 65, "KBOS"),          # Logan
    "new orleans": ("LIX", 51, 69, "KMSY"),     # Louis Armstrong
    "phoenix": ("PSR", 159, 57, "KPHX"),        # Sky Harbor
    "dallas": ("FWD", 156, 45, "KDFW"),         # DFW
    "minneapolis": ("MPX", 108, 48, "KMSP"),    # Minneapolis-St Paul
    "atlanta": ("FFC", 57, 87, "KATL"),         # Hartsfield-Jackson
    "san antonio": ("EWX", 158, 97, "KSAT"),    # San Antonio International
    "las vegas": ("VEF", 153, 47, "KLAS"),      # Harry Reid
    "oklahoma city": ("OUN", 47, 38, "KOKC"),   # Will Rogers
    "portland": ("PQR", 113, 68, "KPDX"),       # Portland International
    "nashville": ("OHX", 42, 57, "KBNA"),       # Nashville International
    "detroit": ("DTX", 66, 33, "KDTW"),         # Detroit Metro
    "san diego": ("SGX", 155, 49, "KSAN"),      # San Diego International
    "tampa": ("TBW", 97, 47, "KTPA"),           # Tampa International
    "death valley": ("VEF", 158, 38, "KDWA"),   # Death Valley
}


def _configured_forecast_station(city: str) -> dict:
    """A forecast default is not evidence of the contract's settlement station."""
    configured = CITY_NWS.get(city.lower().strip())
    return {
        "station_id": configured[3] if configured else None,
        "station_origin": "configured_city_station" if configured else "city_only",
        "station_mapping": "inferred" if configured else "unknown",
    }


def _forecast_station_selection(city: str, metadata: dict | None = None) -> dict:
    # Lazy import avoids the historical-provider -> weather_pro import cycle.
    from bot.weather.station_mapping import _iter_signal_values, resolve_weather_station

    candidates = []
    # Use the resolver's exact direct/nested alias set, but resolve every
    # assertion separately: its first-match shortcut cannot prove agreement.
    for value in _iter_signal_values(metadata or {}):
        if value is None or value == "":
            continue
        resolved = resolve_weather_station({"station_id": value})
        # Present-but-unresolved is still an explicit assertion, not permission
        # to use the configured city station or discard it beside a valid alias.
        candidates.append((resolved, "station_metadata"))
    for field_name in ("rules_primary", "rules_secondary"):
        text = (metadata or {}).get(field_name)
        if not isinstance(text, str):
            continue
        for match in re.finditer(r"\b(?:CLI[A-Z0-9]{3,4}|K[A-Z0-9]{3,4})\b", text):
            token = match.group()
            resolved = resolve_weather_station({"station_cli" if token.startswith("CLI") else "station_id": token})
            candidates.append((resolved, field_name))
    if candidates:
        identities = {resolved.station_id or f"CLI{resolved.station_cli}" for resolved, _ in candidates}
        resolved, field_name = candidates[0]
        return {"station_id": resolved.station_id if len(identities) == 1 else None,
                "station_cli": resolved.station_cli if len(identities) == 1 else None,
                "station_origin": "market_explicit",
                "station_mapping": "conflicting" if len(identities) > 1 else "exact" if resolved.station_id else "unknown",
                "station_evidence_field": field_name,
                **({"availability_reason": "conflicting_explicit_stations"} if len(identities) > 1 else {})}
    return _configured_forecast_station(city)


def _resolve_forecast_location(city: str, http: httpx.Client, cache: dict, station: dict) -> dict:
    """Resolve GeoJSON station geometry, never substitute a centroid for a station."""
    location = {**station, "city": city.lower().strip()}
    station_id = station.get("station_id")
    if station.get("availability_reason"):
        return {**location, "basis": "station"}
    if not station_id and station.get("station_origin") == "market_explicit":
        return {**location, "basis": "station", "availability_reason": "explicit_station_unresolved"}
    if not station_id:
        coords = CITY_COORDS.get(location["city"])
        return {**location, "basis": "city_centroid", "coordinate_source": "configured_city_coordinates",
                "latitude": coords[0] if coords else None, "longitude": coords[1] if coords else None}

    url = f"https://api.weather.gov/stations/{station_id}"
    location.update(basis="station", coordinate_source="nws_station_metadata", coordinate_url=url)
    now = datetime.now(timezone.utc)
    cached = cache.get(station_id)
    if cached and now - cached[0] < timedelta(hours=6):
        return {**location, **cached[1]}
    try:
        resp = http.get(url, headers={"User-Agent": "PredictionBot/1.0"})
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError("invalid station metadata")
        geometry = data.get("geometry")
        props = data.get("properties")
        if (not isinstance(geometry, dict) or not isinstance(props, dict)
                or props.get("stationIdentifier") != station_id or geometry.get("type") != "Point"):
            raise ValueError("station metadata identity or geometry mismatch")
        lon, lat = geometry["coordinates"]
        if (isinstance(lat, bool) or isinstance(lon, bool)
                or not isinstance(lat, (int, float)) or not isinstance(lon, (int, float))
                or not math.isfinite(lat) or not math.isfinite(lon)
                or not -90 <= lat <= 90 or not -180 <= lon <= 180):
            raise ValueError("invalid station coordinates")
        coordinates = {"latitude": lat, "longitude": lon, "coordinates_fetched_at": now.isoformat()}
        cache[station_id] = (now, coordinates)
        return {**location, **coordinates}
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        logger.debug("NWS station coordinates unavailable for %s: %s", station_id, exc)
        return {**location, "availability_reason": "station_coordinates_unavailable"}


def _settlement_authority(metadata: dict | None) -> dict:
    """Identify a provider from explicit contract rules, not forecast sources."""
    rules = metadata if isinstance(metadata, dict) else {}
    matches = []
    unresolved_provider = False
    for field_name in ("rules_primary", "rules_secondary"):
        text = rules.get(field_name)
        if not isinstance(text, str) or not text.strip():
            continue
        providers = set()
        for attribution in re.finditer(
            r"\b(?:according to|reported by)\s+(?:the\s+)?"
            r"(?P<provider>Weather Company|NWS|National Weather Service)\b",
            text, re.I,
        ):
            prefix = text[:attribution.start()]
            # Attributing a forecast to a provider does not establish who
            # reports the final measured temperature for settlement.
            if re.search(r"\b(?:forecasts?|predictions?)\s*,?\s*$", prefix, re.I):
                continue
            if re.search(
                r"\b(?:not|never)\s+(?:(?:be|been|being)\s+)?"
                r"(?:(?:determined|resolved|settled|measured)\s+)?$",
                prefix, re.I,
            ):
                continue
            provider = attribution.group("provider").lower()
            providers.add("the_weather_company" if provider == "weather company" else "nws")
        if re.search(r"\baccording to\b", text, re.I) and not re.search(
            r"\baccording to\s+(?:the\s+)?(?:Weather Company|NWS\b|National Weather Service\b)", text, re.I
        ):
            unresolved_provider = True
        matches.append((field_name, providers))
    providers = set().union(*(found for _, found in matches)) if matches else set()
    status = ("conflicting_rules" if len(providers) > 1 or (providers and unresolved_provider)
              else "identified" if providers else "unknown_rules")
    source = next(iter(providers)) if status == "identified" else "unknown"
    return {
        "source": source, "status": status,
        "evidence_field": next((field for field, found in matches if source in found), None),
        "evidence_origin": "kalshi_market_metadata",
    }


def _c_to_f(c: float) -> float:
    return c * 9/5 + 32



def _timezone_offset_label(value: object) -> str | None:
    """Preserve an explicit source-local UTC offset from NWS period evidence."""
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    offset = parsed.utcoffset()
    if offset is None:
        return None
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def _target_date_text(target_date: str | None) -> str | None:
    if not isinstance(target_date, str):
        return None
    try:
        return date.fromisoformat(target_date[:10]).isoformat()
    except ValueError:
        return None


def _market_target_date(question: str, *, category: str = "") -> str | None:
    # Keep this import lazy: bot.weather's package initializer imports the
    # historical provider, which in turn reuses this feed's constants.
    from bot.weather.date_matcher import derive_market_date

    return derive_market_date({"question": question, "market_ticker": category, "market_id": category}).isoformat


def _unavailable_snapshot(
    *, city: str, source: str, target_date: str | None, reason: str, fetched_at: datetime | None = None,
    source_details: dict | None = None,
) -> WeatherSnapshot:
    fetched_at = fetched_at or datetime.now(timezone.utc)
    target = _target_date_text(target_date)
    return WeatherSnapshot(
        city=city.lower(), high_temp_f=None, low_temp_f=None, current_temp_f=None, source=source,
        fetched_at=fetched_at, forecast_hours_ahead=0, confidence=0, as_of=fetched_at,
        source_details={
            **(source_details or {}),
            "source_evidence_version": 1,
            "evidence_type": "forecast_unavailable",
            "forecast_availability": "unavailable",
            "availability_reason": reason,
            "source_as_of": fetched_at.isoformat(),
            "target_mapping": {
                "market_target_date": target,
                "source_target_date": None,
                "mapping": "no_exact_source_local_forecast_period",
            },
        },
        scoreable_forecast=False, availability_reason=reason,
    )


def _response_capture(response, *, capture_store: WeatherCaptureStore, url: str, parameters: dict | None = None) -> dict:
    """Retain exact response bytes once; return only verified compact references.

    Minimal legacy HTTP fixtures expose JSON only; mark their reserialization
    explicitly rather than claiming those are original transport bytes.
    """
    import json

    raw = getattr(response, 'content', None)
    representation = 'http_response_bytes'
    if not isinstance(raw, bytes):
        raw = json.dumps(response.json(), ensure_ascii=False).encode('utf-8')
        representation = 'json_reserialized_no_transport_bytes'
    headers = getattr(response, 'headers', {})
    return capture_store.retain(raw, metadata={
            'retrieval_url': url, 'request_parameters': parameters or {},
            'http_status': getattr(response, 'status_code', None),
            'content_type': headers.get('content-type'), 'representation': representation,
            'fetched_at': datetime.now(timezone.utc).isoformat()})


def _retain_forecast_response(details: dict, response, *, capture_store: WeatherCaptureStore, url: str, parameters: dict | None = None) -> None:
    capture = _response_capture(response, capture_store=capture_store, url=url, parameters=parameters)
    details['capture'] = capture
    details['collection_attempts'][-1].update(http_status=capture['http_status'],
        fetched_at=capture['fetched_at'], attempt_status='response_retained')


def _forecast_attempt(details: dict, url: str, *, parameters: dict | None = None) -> None:
    details.setdefault('collection_attempts', []).append({
        'url': url, 'request_parameters': parameters or {},
        'attempted_at': datetime.now(timezone.utc).isoformat(), 'attempt_status': 'attempted',
    })


def _forecast_failure(details: dict, exc: Exception) -> None:
    attempt = details['collection_attempts'][-1]
    if isinstance(exc, httpx.HTTPStatusError):
        category = 'http_status_failure'
    elif isinstance(exc, httpx.RequestError):
        category = 'transport_failure'
    else:
        category = 'parser_failure' if 'capture' in details else 'adapter_failure'
    attempt.update(attempt_status=category, error_type=type(exc).__name__)
    details['adapter_error_type'] = type(exc).__name__
    details['adapter_error_category'] = category


class OpenMeteoFeed:
    """
    Open-Meteo: free, no API key, hourly updates.
    Uses ECMWF, GFS, DWD models with 1km resolution.
    """
    BASE = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, *, capture_store: WeatherCaptureStore | None = None):
        self.capture_store = capture_store or WeatherCaptureStore(storage_root=collector_root())
        self.http = httpx.Client(timeout=10)
        self._station_location_cache: dict = {}

    def get_forecast(
        self, city: str, *, target_date: str | None = None, station_resolution: dict | None = None,
    ) -> Optional[WeatherSnapshot]:
        location = _resolve_forecast_location(
            city, self.http, self._station_location_cache,
            station_resolution if station_resolution is not None else _configured_forecast_station(city),
        )
        location_details = {"forecast_location": location, "forecast_query_url": self.BASE}
        lat, lon = location.get("latitude"), location.get("longitude")
        if lat is None or lon is None:
            return _unavailable_snapshot(
                city=city, source="open-meteo", target_date=target_date,
                reason=location.get("availability_reason") or "forecast_coordinates_unavailable",
                source_details=location_details,
            )

        params = {
                "latitude": lat,
                "longitude": lon,
                "hourly": "temperature_2m",
                "current": "temperature_2m,relative_humidity_2m,wind_speed_10m",
                "temperature_unit": "fahrenheit",
                "forecast_days": 2,
                "timezone": "auto",
        }
        _forecast_attempt(location_details, self.BASE, parameters=params)
        try:
            resp = self.http.get(self.BASE, params=params)
            _retain_forecast_response(location_details, resp, capture_store=self.capture_store, url=self.BASE, parameters=params)
            resp.raise_for_status()
            data = resp.json()
            location_details['collection_attempts'][-1]['attempt_status'] = 'parsed'

            current = data.get("current", {})
            hourly = data.get("hourly", {})
            temps = hourly.get("temperature_2m", [])
            times = hourly.get("time", [])

            fetched_at = datetime.now(timezone.utc)
            if not temps:
                return _unavailable_snapshot(
                    city=city, source="open-meteo", target_date=target_date,
                    reason="source_forecast_missing_hourly_temperatures", fetched_at=fetched_at,
                    source_details=location_details,
                )

            target = _target_date_text(target_date)
            if target_date is not None and target is None:
                return _unavailable_snapshot(
                    city=city, source="open-meteo", target_date=target_date,
                    reason="invalid_market_target_date", fetched_at=fetched_at,
                    source_details=location_details,
                )
            selected_temps = []
            selected_times = []
            for i, t in enumerate(times):
                try:
                    dt = datetime.fromisoformat(t)
                    if target is None or dt.date().isoformat() == target:
                        selected_temps.append(temps[i])
                        selected_times.append(t)
                except (TypeError, ValueError, IndexError):
                    continue
            if not selected_temps:
                return _unavailable_snapshot(
                    city=city, source="open-meteo", target_date=target,
                    reason="target_date_not_in_source_forecast", fetched_at=fetched_at,
                    source_details={**location_details, "timezone": data.get("timezone"), "utc_offset_seconds": data.get("utc_offset_seconds")},
                )

            forecast_start = selected_times[0]
            forecast_end = selected_times[-1]

            return WeatherSnapshot(
                city=city.lower(),
                high_temp_f=max(selected_temps),
                low_temp_f=min(selected_temps),
                current_temp_f=current.get("temperature_2m", 0),
                source="open-meteo",
                fetched_at=fetched_at,
                forecast_hours_ahead=1,
                confidence=0.85,
                humidity=current.get("relative_humidity_2m", 0),
                wind_mph=current.get("wind_speed_10m", 0) * 0.621371,  # km/h to mph
                as_of=fetched_at,
                weather_date=target,
                forecast_date=target,
                forecast_start=forecast_start,
                forecast_end=forecast_end,
                forecast_times=list(selected_times),
                source_details={
                    "source_evidence_version": 1 if target else None,
                    "evidence_type": "forecast" if target else None,
                    "forecast_availability": "available" if target else None,
                    "source_as_of": fetched_at.isoformat(),
                    "target_mapping": {
                        "market_target_date": target,
                        "source_target_date": target,
                        "mapping": "exact_source_local_hourly_date" if target else None,
                        "source_timezone": data.get("timezone"),
                        "source_period_start": forecast_start,
                        "source_period_end": forecast_end,
                    },
                    **location_details,
                    "forecast_query_url": self.BASE,
                    "response_latitude": data.get("latitude"),
                    "response_longitude": data.get("longitude"),
                    "timezone": data.get("timezone"),
                    "utc_offset_seconds": data.get("utc_offset_seconds"),
                    "current_time": current.get("time"),
                    "forecast_times_used": list(selected_times),
                },
            )
        except CaptureRetentionError:
            raise
        except Exception as e:
            logger.debug(f"Open-Meteo error for {city}: {e}")
            _forecast_failure(location_details, e)
            reason = 'source_forecast_request_failed'
            return _unavailable_snapshot(
                city=city, source="open-meteo", target_date=target_date,
                reason=reason,
                source_details=location_details,
            )

    def close(self):
        self.http.close()


class NWSFeed:
    """NWS API: free, no key, US only, 1-2 hour updates. Also provides real-time station observations."""

    def __init__(self, *, capture_store: WeatherCaptureStore | None = None):
        self.capture_store = capture_store or WeatherCaptureStore(storage_root=collector_root())
        self.http = httpx.Client(timeout=10)
        self._station_location_cache: dict = {}
        self._points_cache: dict[tuple, tuple[datetime, tuple[str, int, int]]] = {}
        self._points_cache_ttl = timedelta(hours=6)

    def _resolve_forecast_grid(self, city: str, *, forecast_location: dict | None = None) -> Optional[tuple[str, int, int, str | None]]:
        location = forecast_location if forecast_location is not None else _resolve_forecast_location(
            city, self.http, self._station_location_cache, _configured_forecast_station(city),
        )
        lat, lon = location.get("latitude"), location.get("longitude")
        if lat is None or lon is None:
            return None
        station = location.get("station_id")
        cache_key = (location["basis"], station, lat, lon)
        cached = self._points_cache.get(cache_key)
        now = datetime.now(timezone.utc)
        if cached and (now - cached[0]) < self._points_cache_ttl:
            office, grid_x, grid_y = cached[1]
            return office, grid_x, grid_y, station

        try:
            resp = self.http.get(
                f"https://api.weather.gov/points/{lat},{lon}",
                headers={"User-Agent": "PredictionBot/1.0"},
                # NWS redirects station geometry to its supported precision.
                follow_redirects=True,
            )
            resp.raise_for_status()
            data = resp.json()
            props = data.get("properties") if isinstance(data, dict) else None
            if not isinstance(props, dict):
                raise ValueError("invalid NWS points metadata")
            office = props.get("gridId")
            grid_x = props.get("gridX")
            grid_y = props.get("gridY")
            if office and grid_x is not None and grid_y is not None:
                resolved = (str(office), int(grid_x), int(grid_y))
                self._points_cache[cache_key] = (now, resolved)
                return resolved[0], resolved[1], resolved[2], station
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            logger.debug("NWS points lookup error for %s: %s", city, exc)
        return None

    def get_station_observation(self, city: str) -> Optional[dict]:
        """
        Get real-time NWS station observation for the mapped city station.
        
        This is a current observation, not the contract's daily report.
        
        Note: Daily high will almost always be higher than any individual
        hourly reading, because the true max occurs between readings.
        """
        grid = CITY_NWS.get(city.lower())
        if not grid:
            return None

        office, grid_x, grid_y, station = grid
        
        try:
            url = f"https://api.weather.gov/stations/{station}/observations/latest"
            resp = self.http.get(url, headers={"User-Agent": "PredictionBot/1.0"})
            resp.raise_for_status()
            data = resp.json()
            
            props = data.get("properties", {})
            temp_c = props.get("temperature", {}).get("value")
            
            if temp_c is None:
                return None
            
            temp_f = temp_c * 9/5 + 32
            
            return {
                "station": station,
                "city": city.lower(),
                "current_temp_f": round(temp_f, 1),
                "observation_time": props.get("timestamp", ""),
                "source": "nws_observation",
            }
            
        except Exception as e:
            logger.debug(f"NWS observation error for {station}: {e}")
            return None

    def get_forecast(
        self, city: str, *, target_date: str | None = None, measurement_kind: str | None = None,
        station_resolution: dict | None = None,
    ) -> Optional[WeatherSnapshot]:
        location = _resolve_forecast_location(
            city, self.http, self._station_location_cache,
            station_resolution if station_resolution is not None else _configured_forecast_station(city),
        )
        location_details: dict = {"forecast_location": location}
        grid = self._resolve_forecast_grid(city, forecast_location=location)
        if not grid:
            return _unavailable_snapshot(
                city=city, source="nws", target_date=target_date,
                reason=location.get("availability_reason") or "source_forecast_grid_unavailable",
                source_details=location_details,
            )

        office, grid_x, grid_y, station = grid
        url = f"https://api.weather.gov/gridpoints/{office}/{grid_x},{grid_y}/forecast"
        location_details["forecast_query_url"] = url
        _forecast_attempt(location_details, url)
        try:
            resp = self.http.get(url, headers={"User-Agent": "PredictionBot/1.0"})
            _retain_forecast_response(location_details, resp, capture_store=self.capture_store, url=url)
            resp.raise_for_status()
            data = resp.json()
            location_details['collection_attempts'][-1]['attempt_status'] = 'parsed'
            location_details['source_reported_updated_at'] = data.get('properties', {}).get('updateTime')

            periods = data.get("properties", {}).get("periods", [])
            fetched_at = datetime.now(timezone.utc)
            target = _target_date_text(target_date)
            if target_date is not None and target is None:
                return _unavailable_snapshot(city=city, source="nws", target_date=target_date, reason="invalid_market_target_date", fetched_at=fetched_at, source_details=location_details)
            if not periods:
                return _unavailable_snapshot(city=city, source="nws", target_date=target, reason="source_forecast_missing_periods", fetched_at=fetched_at, source_details=location_details)
            if target:
                dated_periods: list[dict] = []
                for period in periods:
                    start = period.get("startTime")
                    if not isinstance(start, str) or not start:
                        return _unavailable_snapshot(city=city, source="nws", target_date=target, reason="source_forecast_period_timezone_ambiguous", fetched_at=fetched_at, source_details=location_details)
                    parsed = datetime.fromisoformat(start.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        return _unavailable_snapshot(city=city, source="nws", target_date=target, reason="source_forecast_period_timezone_ambiguous", fetched_at=fetched_at, source_details=location_details)
                    if parsed.date().isoformat() == target:
                        dated_periods.append(period)
                if not dated_periods:
                    return _unavailable_snapshot(city=city, source="nws", target_date=target, reason="target_date_not_in_source_forecast", fetched_at=fetched_at, source_details=location_details)
                high_period = next((p for p in dated_periods if p.get("isDaytime") is True), None)
                if high_period is None:
                    return _unavailable_snapshot(city=city, source="nws", target_date=target, reason="target_date_missing_daytime_forecast_period", fetched_at=fetched_at, source_details=location_details)
                # The night beginning on the target date forecasts the *next*
                # morning. Prefer the night ending on the target morning for a
                # daily low; never use the following morning for a low market.
                morning_low = None
                for period in periods:
                    if period.get("isDaytime") is not False:
                        continue
                    try:
                        end = datetime.fromisoformat(str(period.get("endTime")).replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if end.tzinfo is not None and end.date().isoformat() == target:
                        morning_low = period
                        break
                if measurement_kind == "low" and morning_low is None:
                    return _unavailable_snapshot(city=city, source="nws", target_date=target, reason="target_date_missing_low_forecast_period", fetched_at=fetched_at, source_details=location_details)
                low_period = morning_low or next((p for p in dated_periods if p.get("isDaytime") is False), high_period)
            else:
                high_period = next((p for p in periods if p.get("isDaytime") is True), periods[0])
                low_period = next((p for p in periods if p.get("isDaytime") is False), high_period)
            high = high_period.get("temperature")
            low = low_period.get("temperature")
            if ((measurement_kind == "high" and high is None)
                    or (measurement_kind == "low" and low is None)
                    or (measurement_kind not in {"high", "low"} and (high is None or low is None))):
                return _unavailable_snapshot(city=city, source="nws", target_date=target, reason="source_forecast_missing_temperature", fetched_at=fetched_at, source_details=location_details)
            selected_period = low_period if target and measurement_kind == "low" else high_period
            today = selected_period
            temp = selected_period.get("temperature")
            is_daytime = bool(today.get("isDaytime", True))
            period_name = today.get("name")
            period_start = today.get("startTime")
            period_end = today.get("endTime")
            periods_used = [self._period_ref(period) for period in (high_period, low_period)]

            snapshot = WeatherSnapshot(
                city=city.lower(),
                high_temp_f=float(high) if high is not None else None,
                low_temp_f=float(low) if low is not None else None,
                current_temp_f=float(temp) if temp is not None else None,
                source="nws",
                fetched_at=fetched_at,
                forecast_hours_ahead=2,
                confidence=0.85,
                conditions=today.get("shortForecast", ""),
                as_of=fetched_at,
                weather_date=target,
                forecast_date=target,
                forecast_start=period_start,
                forecast_end=period_end,
                forecast_period_name=period_name,
                forecast_period_start=period_start,
                forecast_period_end=period_end,
                source_details={
                    "source_evidence_version": 1 if target else None,
                    "evidence_type": "forecast" if target else None,
                    "forecast_availability": "available" if target else None,
                    "source_as_of": fetched_at.isoformat(),
                    "target_mapping": {
                        "market_target_date": target,
                        "source_target_date": target,
                        "mapping": (
                            "source_local_nws_night_low_proxy" if target and measurement_kind == "low"
                            else "exact_source_local_nws_period" if target else None
                        ),
                        "source_period_start": period_start,
                        "source_period_end": period_end,
                        "source_timezone": _timezone_offset_label(period_start),
                    },
                    **location_details,
                    "forecast_query_url": url,
                    "office": office,
                    "grid_x": grid_x,
                    "grid_y": grid_y,
                    "station_id": station,
                    "period_name": period_name,
                    "period_start": period_start,
                    "period_end": period_end,
                    "period_number": today.get("number"),
                    "is_daytime": is_daytime,
                    "periods_used": periods_used,
                    "high_period": self._period_ref(high_period),
                    "low_period": self._period_ref(low_period),
                    "source_clock_basis": "capture_time_with_separate_provider_update_time",
                },
            )
            return _apply_source_clock_eligibility(snapshot, location_details.get('source_reported_updated_at'))
        except CaptureRetentionError:
            raise
        except Exception as e:
            logger.debug(f"NWS error for {city}: {e}")
            _forecast_failure(location_details, e)
            reason = 'source_forecast_request_failed'
            return _unavailable_snapshot(city=city, source="nws", target_date=target_date, reason=reason, source_details=location_details)

    @staticmethod
    def _period_ref(period: dict) -> dict:
        return _drop_none(
            {
                "number": period.get("number"),
                "name": period.get("name"),
                "startTime": period.get("startTime"),
                "endTime": period.get("endTime"),
                "isDaytime": period.get("isDaytime"),
                "temperature": period.get("temperature"),
                "temperatureUnit": period.get("temperatureUnit"),
            }
        )

    def close(self):
        self.http.close()


def _apply_source_clock_eligibility(snapshot: WeatherSnapshot, value: object, *, milliseconds: bool = False) -> WeatherSnapshot:
    """A source cache/update clock is evidence, not automatically issuance time."""
    if value is None:
        return snapshot
    try:
        if milliseconds:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError('invalid cache clock')
            clock = datetime.fromtimestamp(value / 1000, timezone.utc)
        else:
            clock = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if clock.utcoffset() is None:
            raise ValueError('timezone ambiguous source clock')
        capture_at = datetime.fromisoformat(snapshot.source_details['capture']['fetched_at'])
        reason = 'source_reported_clock_after_capture' if clock > capture_at else None
    except (TypeError, ValueError, OverflowError, OSError):
        reason = 'source_reported_clock_invalid'
    snapshot.source_details['source_clock_eligibility'] = 'ineligible' if reason else 'consistent_with_capture'
    if reason:
        snapshot.scoreable_forecast = False
        snapshot.availability_reason = reason
        snapshot.source_details.update(forecast_availability='unavailable', availability_reason=reason)
    return snapshot


class LocalForecastFeed:
    """Capture each configured public page; numeric support is schema-specific.

    Raw captures belong to the collector store, not repeated scoring wrappers.
    """
    def __init__(self, http: httpx.Client, *, capture_store: WeatherCaptureStore | None = None):
        self.http = http
        self.capture_store = capture_store or WeatherCaptureStore(storage_root=collector_root())

    def get_forecast(self, source: dict, *, target_date: str | None,
                     measurement_kind: str | None = None) -> WeatherSnapshot:
        from bot.feeds.weather_local import LocalForecastUnavailable, parse_gray_daily, local_geography_reason
        from bot.feeds.weather_local_fox import parse_fox_daily
        from bot.feeds.weather_local_tegna import parse_tegna_daily
        from bot.feeds.weather_local_other import OTHER_LOCAL_ADAPTERS, OTHER_LOCAL_URLS, OTHER_LOCAL_UNAVAILABLE_REASONS

        city, source_id = source['city'].lower(), source['source_id']
        request_url = OTHER_LOCAL_URLS.get(source_id, source['url'])
        details = {'source_id': source_id, 'source_name': source['name'],
                   'source_family': 'local_broadcast', 'collection_adapter': source.get('collection_adapter'),
                   'registry_url': source['url'], 'forecast_query_url': request_url, 'collection_attempts': [],
                   'capability_blocker': source.get('collection_blocker'),
                   'forecast_location': {'city': city, 'basis': 'city', 'city_id': source['city_id']}}
        target = _target_date_text(target_date)
        if target is None:
            return _unavailable_snapshot(city=city, source=source_id, target_date=target_date,
                                         reason='missing_market_target_date', source_details=details)
        attempt = {'url': request_url, 'attempted_at': datetime.now(timezone.utc).isoformat()}
        details['collection_attempts'].append(attempt)
        fetched_at = None
        try:
            response = self.http.get(request_url, follow_redirects=True)
            attempt['http_status'] = response.status_code
            fetched_at = datetime.now(timezone.utc)
            attempt['fetched_at'] = fetched_at.isoformat()
            details['capture'] = _response_capture(response, capture_store=self.capture_store, url=request_url)
            response.raise_for_status()
            if source_id in OTHER_LOCAL_UNAVAILABLE_REASONS:
                raise LocalForecastUnavailable(OTHER_LOCAL_UNAVAILABLE_REASONS[source_id])
            parser = OTHER_LOCAL_ADAPTERS.get(source_id) or {'gray_fusion_daily_v1': parse_gray_daily,
                      'fox_nuxt_daily_v1': parse_fox_daily,
                      'tegna_public_html_v1': parse_tegna_daily}.get(source.get('collection_adapter', ''))
            if parser is None:
                details['prerequisite'] = 'observed_dated_local_forecast_payload_and_parser'
                raise LocalForecastUnavailable('source_specific_numeric_adapter_unavailable')
            parsed = parser(response.text, city=source['city'], target_date=target,
                            measurement_kind=measurement_kind, timezone_name=source['timezone'],
                            **({'fetched_at': fetched_at} if parser is parse_tegna_daily else {}))
            details.update(forecast_type=parsed['forecast_type'], raw_forecast=parsed['raw_forecast'],
                           payload_locator=parsed['payload_locator'],
                           source_cache_updated_at_ms=parsed['cache_updated_at_ms'])
            for key in ('selected_period', 'source_updated_at', 'period_semantics',
                        'full_day_measurement_equivalent', 'date_mapping_basis', 'period_basis'):
                if key in parsed:
                    details[key] = parsed[key]
            if 'payload_sha256' in parsed:
                details['parser_payload_sha256'] = parsed['payload_sha256']
            details['source_reported_location'] = parsed['location']
            # The registry's city/timezone is the predictor binding. Provider
            # coordinates are not an independently verified query or station.
            geography_reason = local_geography_reason(parsed['location'], city=city, city_id=source['city_id'])
            details['forecast_location']['geography_basis'] = 'registry_city_and_source_label_timezone'
            details['forecast_location']['coordinate_basis'] = 'source_reported_unverified'
            details.update(source_evidence_version=1, evidence_type='forecast', forecast_availability='available',
                           source_as_of=parsed.get('source_as_of', fetched_at.isoformat()),
                           source_clock_basis=parsed.get('source_clock_basis', 'capture_time_provider_issue_time_unavailable'),
                           target_mapping={'market_target_date': target, 'source_target_date': target,
                                           'mapping': 'source_local_dated_predictor',
                                           'source_timezone': parsed['location'].get('ianaTimeZone'),
                                           'source_period_start': parsed['period_start'],
                                           'source_period_end': parsed['period_end']})
            snapshot = WeatherSnapshot(city=city, source=source_id, high_temp_f=parsed['high'],
                low_temp_f=parsed['low'], current_temp_f=None, fetched_at=fetched_at,
                as_of=fetched_at, forecast_hours_ahead=0, confidence=0,
                weather_date=target, forecast_date=target, forecast_start=parsed['period_start'],
                forecast_end=parsed['period_end'], source_details=details)
            if geography_reason:
                snapshot.scoreable_forecast = False
                snapshot.availability_reason = geography_reason
                details.update(forecast_availability='unavailable', availability_reason=geography_reason)
            return _apply_source_clock_eligibility(snapshot, parsed['cache_updated_at_ms'], milliseconds=True)
        except CaptureRetentionError:
            # Storage is an operational failure, not a missing forecast. Never
            # let the engine cache a result whose evidence could not be retained.
            raise
        except LocalForecastUnavailable as exc:
            reason = str(exc)
            details['adapter_error_type'] = attempt['error_type'] = type(exc).__name__
        except httpx.HTTPStatusError:
            reason = f"source_http_status_{attempt['http_status']}"
        except httpx.HTTPError as exc:
            reason = 'source_forecast_request_failed'
            attempt['error_type'] = type(exc).__name__
        except UnicodeError as exc:
            reason = 'source_payload_encoding_unsupported'
            details['adapter_error_type'] = attempt['error_type'] = type(exc).__name__
        except Exception as exc:
            # This boundary owns the in-progress capture and attempt. The outer
            # engine cannot reconstruct them if a parser unexpectedly fails.
            reason = 'source_forecast_unexpected_error'
            details['adapter_error_type'] = attempt['error_type'] = type(exc).__name__
            logger.warning('Weather adapter %s failed: %s', source_id, type(exc).__name__)
        return _unavailable_snapshot(city=city, source=source_id, target_date=target,
                                     reason=reason, fetched_at=fetched_at, source_details=details)


class OpenWeatherMapFeed:
    """
    OpenWeatherMap: free tier 1000 calls/day, minute-level updates.
    Needs API key (set OPENWEATHER_API_KEY env var).
    """
    BASE = "https://api.openweathermap.org/data/2.5"

    def __init__(self, api_key: str = None):
        import os
        self.api_key = api_key or os.getenv("OPENWEATHER_API_KEY", "")
        self.http = httpx.Client(timeout=10)
        self.available = bool(self.api_key)
        self._station_location_cache: dict = {}

    def get_forecast(self, city: str, *, station_resolution: dict | None = None) -> Optional[WeatherSnapshot]:
        if not self.available:
            return None

        location = _resolve_forecast_location(
            city, self.http, self._station_location_cache,
            station_resolution if station_resolution is not None else _configured_forecast_station(city),
        )
        lat, lon = location.get("latitude"), location.get("longitude")
        if lat is None or lon is None:
            return _unavailable_snapshot(
                city=city, source="openweathermap", target_date=None,
                reason=location.get("availability_reason") or "forecast_coordinates_unavailable",
                source_details={"forecast_location": location},
            )

        try:
            # Current weather + forecast
            params = {
                "lat": lat,
                "lon": lon,
                "appid": self.api_key,
                "units": "imperial",
            }
            resp = self.http.get(f"{self.BASE}/forecast", params=params)
            resp.raise_for_status()
            data = resp.json()

            list_items = data.get("list", [])
            if not list_items:
                return None

            # Get temps from next 24 hours (3-hour intervals = 8 items)
            window_items = list_items[:8]
            temps = [item["main"]["temp"] for item in window_items]
            current_temp = window_items[0]["main"]["temp"]
            times_used = [item.get("dt_txt") or item.get("dt") for item in window_items if item.get("dt_txt") or item.get("dt")]
            fetched_at = datetime.now(timezone.utc)

            return WeatherSnapshot(
                city=city.lower(),
                high_temp_f=max(temps),
                low_temp_f=min(temps),
                current_temp_f=current_temp,
                source="openweathermap",
                fetched_at=fetched_at,
                forecast_hours_ahead=0.5,  # Updated every 30 min
                confidence=0.90,
                humidity=window_items[0]["main"].get("humidity", 0),
                wind_mph=window_items[0]["wind"].get("speed", 0),
                conditions=window_items[0]["weather"][0]["description"] if window_items[0].get("weather") else "",
                as_of=fetched_at,
                forecast_start=str(times_used[0]) if times_used else None,
                forecast_end=str(times_used[-1]) if times_used else None,
                forecast_times=[str(value) for value in times_used],
                source_details={
                    "forecast_location": location,
                    "forecast_query_url": f"{self.BASE}/forecast",
                    "forecast_times_used": [str(value) for value in times_used],
                },
            )
        except Exception as e:
            logger.debug(f"OpenWeatherMap error for {city}: {e}")
            return _unavailable_snapshot(
                city=city, source="openweathermap", target_date=None, reason="source_forecast_request_failed",
                source_details={"forecast_location": location, "forecast_query_url": f"{self.BASE}/forecast"},
            )

    def close(self):
        self.http.close()


def project_probability_evidence(value: Any, yes_probability: Any) -> dict[str, Any] | None:
    """Copy only a consistent, decision-time heuristic receipt (YES semantics)."""
    if not isinstance(value, Mapping) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        return None
    probability = value.get("yes_probability")
    if (isinstance(probability, bool) or not isinstance(probability, (int, float))
            or not 0 <= probability <= 1 or not math.isfinite(probability)
            or isinstance(yes_probability, bool) or probability != yes_probability
            or value.get("basis") not in ("heuristic_weather_distance_v1", "heuristic_weighted_ensemble_v1")
            or value.get("calibration_status") != "not_established"
            or value.get("fallback_reason") != "empirical_contract_probability_unavailable"):
        return None
    receipt = {key: value[key] for key in (
        "schema_version", "yes_probability", "basis", "calibration_status", "fallback_reason")}
    for key in ("source_id", "forecast_policy_id"):
        if isinstance(value.get(key), str) and value[key]:
            receipt[key] = value[key]
    location = value.get("location_provenance")
    if isinstance(location, Mapping):
        projected = {key: item for key, item in location.items()
            if key in {"basis", "station_id", "station_cli", "station_origin", "station_mapping",
                       "station_evidence_field", "city", "latitude", "longitude", "coordinate_source",
                       "coordinate_url", "coordinates_fetched_at"}
            and isinstance(item, (str, int, float)) and not isinstance(item, bool)
            and (not isinstance(item, float) or math.isfinite(item))}
        if projected:
            receipt["location_provenance"] = projected
    return receipt


class ProWeatherEngine:
    """
    Multi-source weather engine with cross-validation.
    
    Fetches from all available sources, averages them, and
    returns a high-confidence forecast.
    """

    def __init__(self, *, capture_store: WeatherCaptureStore | None = None):
        # Collector owner injects one store, rooted independently of this checkout.
        self.capture_store = capture_store or WeatherCaptureStore(storage_root=collector_root())
        self.open_meteo = OpenMeteoFeed(capture_store=self.capture_store)
        self.nws = NWSFeed(capture_store=self.capture_store)
        self.owm = OpenWeatherMapFeed()
        self._cache = {}
        self._cache_ttl = 600  # 10 min

    def _snapshot_is_plausible(self, snapshot: WeatherSnapshot, measurement_kind: str | None = None) -> bool:
        if not snapshot.scoreable_forecast:
            return False
        temps = (
            [snapshot.high_temp_f] if measurement_kind == 'high'
            else [snapshot.low_temp_f] if measurement_kind == 'low'
            else [snapshot.high_temp_f, snapshot.low_temp_f]
        )
        return all(isinstance(temp, (int, float)) and not isinstance(temp, bool)
                   and math.isfinite(temp) and TEMP_MIN_F <= temp <= TEMP_MAX_F for temp in temps)

    def get_forecast(
        self, city: str, *, target_date: str | None = None, require_target: bool = False,
        measurement_kind: str | None = None, station_resolution: dict | None = None,
    ) -> Optional[MultiSourceForecast]:
        """Get cross-validated forecast from all sources."""
        city_lower = city.lower().strip()
        target = _target_date_text(target_date)
        station = station_resolution if station_resolution is not None else _configured_forecast_station(city_lower)

        # Location identity and origin are part of the forecast promise. Never
        # let a configured-city result satisfy a different explicit station.
        cache_key = (city_lower, target, require_target, measurement_kind,
                     tuple(sorted(station.items())), CITY_COORDS.get(city_lower))
        if cache_key in self._cache:
            cached, ts = self._cache[cache_key]
            if (datetime.now(timezone.utc) - ts).total_seconds() < self._cache_ttl:
                return cached

        from bot.weather.registry import WeatherRegistry
        registry = getattr(self, 'registry', None) or WeatherRegistry.from_file()
        self.registry = registry
        local_candidates = registry.collection_sources(city_lower)
        unavailable_candidates = []
        for candidate in registry.collection_catalog():
            if candidate['source_id'] in {'nws', 'open_meteo', 'openweathermap'}:
                continue
            observation = candidate['category'] == 'nowcast_station_adjacent'
            snapshot = _unavailable_snapshot(city=city_lower, source=candidate['source_id'], target_date=target,
                reason='current_observation_not_future_forecast' if observation else 'source_adapter_unavailable',
                source_details={'collection_attempts': [], 'adapter_status': candidate['adapter_status'],
                    'source_id': candidate['source_id'], 'source_family': candidate['family'],
                    'prerequisite': 'observation_not_forecast' if observation else 'implemented_adapter_and_authorized_provider_access'})
            if observation:
                snapshot.source_details.update(evidence_type='observation', forecast_availability='not_applicable')
            unavailable_candidates.append(snapshot)
        if require_target and target is None:
            source_snapshots = [
                _unavailable_snapshot(city=city_lower, source=source, target_date=target_date, reason="missing_market_target_date")
                for source in ("open-meteo", "nws", "openweathermap", *(s['source_id'] for s in local_candidates))
            ] + unavailable_candidates
            result = MultiSourceForecast(
                city=city_lower, high_temp_f=None, low_temp_f=None, current_temp_f=None,
                sources_used=[], confidence=0, fetched_at=datetime.now(timezone.utc),
                details={"source_snapshots": source_snapshots},
            )
            self._retain_collection_receipt(result, target=target, measurement_kind=measurement_kind)
            self._cache[cache_key] = (result, datetime.now(timezone.utc))
            return result

        source_snapshots = []

        # Every provider owns its own attempt/failure. Trust and selection never
        # gate collection, and a broken adapter must not suppress its neighbours.
        def collect(source, fetch):
            try:
                snapshot = fetch()
            except CaptureRetentionError:
                raise
            except Exception as exc:
                logger.warning('Weather adapter %s failed: %s', source, type(exc).__name__)
                snapshot = _unavailable_snapshot(city=city_lower, source=source, target_date=target,
                    reason='source_forecast_unexpected_error',
                    source_details={'adapter_error_type': type(exc).__name__})
            if snapshot is None:
                snapshot = _unavailable_snapshot(city=city_lower, source=source, target_date=target,
                                                 reason='source_returned_no_forecast')
            snapshot.source_details.setdefault('source_id', source.replace('-', '_').replace(' ', '_'))
            if snapshot.scoreable_forecast and not self._snapshot_is_plausible(snapshot, measurement_kind):
                snapshot.scoreable_forecast = False
                snapshot.availability_reason = 'implausible_or_missing_requested_forecast'
                snapshot.source_details.update(forecast_availability='unavailable',
                                               availability_reason=snapshot.availability_reason)
            source_snapshots.append(snapshot)
            return snapshot

        om = collect('open-meteo', lambda: self.open_meteo.get_forecast(
            city_lower, target_date=target, station_resolution=station))
        nws = collect('nws', lambda: self.nws.get_forecast(
            city_lower, target_date=target, measurement_kind=measurement_kind, station_resolution=station))
        collect('openweathermap', lambda: (
            _unavailable_snapshot(city=city_lower, source='openweathermap', target_date=target,
                                  reason='missing_api_key' if not self.owm.available
                                  else 'source_forecast_period_timezone_ambiguous',
                                  source_details={'collection_attempts': [],
                                      'prerequisite': 'OPENWEATHER_API_KEY' if not self.owm.available
                                      else 'explicit_source_local_date_forecast_adapter'})
            if target or not self.owm.available else self.owm.get_forecast(city_lower, station_resolution=station)))

        local_feed = LocalForecastFeed(self.nws.http, capture_store=self.capture_store)
        for source in local_candidates:
            snapshot = collect(source['source_id'], lambda source=source: local_feed.get_forecast(
                source, target_date=target, measurement_kind=measurement_kind))
            # Capture is city-owned; unresolved contract authority disqualifies
            # use only, never the independent fetch or its original values.
            snapshot.source_details['market_station_association'] = dict(station)
            if station.get('availability_reason') or (station.get('station_origin') == 'market_explicit'
                                                      and not station.get('station_id')):
                snapshot.scoreable_forecast = False
                snapshot.availability_reason = station.get('availability_reason') or 'explicit_station_unresolved'
                snapshot.source_details.update(forecast_availability='unavailable',
                                               availability_reason=snapshot.availability_reason)
        source_snapshots.extend(unavailable_candidates)
        snapshots = [snapshot for snapshot in source_snapshots if self._snapshot_is_plausible(snapshot, measurement_kind)]

        if not snapshots:
            local_station_observation = self.nws.get_station_observation(city_lower) if nws else None
            result = MultiSourceForecast(
                city=city_lower, high_temp_f=None, low_temp_f=None, current_temp_f=None,
                sources_used=[], confidence=0, fetched_at=datetime.now(timezone.utc),
                details={"source_snapshots": source_snapshots, "local_station_observation": local_station_observation},
            )
            self._retain_collection_receipt(result, target=target, measurement_kind=measurement_kind)
            self._cache[cache_key] = (result, datetime.now(timezone.utc))
            return result

        local_station_observation = None
        if nws:
            local_station_observation = self.nws.get_station_observation(city_lower)

        # Cross-validate
        highs = [s.high_temp_f for s in snapshots if s.high_temp_f is not None]
        lows = [s.low_temp_f for s in snapshots if s.low_temp_f is not None]

        # Forecast policy: prefer NWS when available, regardless of the
        # market-specific settlement provider; others validate confidence.
        nws_snapshot = next((s for s in snapshots if s.source == "nws"), None)
        
        if nws_snapshot:
            # Use NWS as primary, others for confidence boost
            avg_high = nws_snapshot.high_temp_f
            avg_low = nws_snapshot.low_temp_f
            avg_current = nws_snapshot.current_temp_f
            
            # Check if other sources agree with NWS
            metric = "low_temp_f" if measurement_kind == "low" else "high_temp_f"
            primary = getattr(nws_snapshot, metric)
            other_values = [getattr(s, metric) for s in snapshots if s.source != "nws" and getattr(s, metric) is not None]
            if other_values and primary is not None:
                # Compare the metric being scored, not an unrelated high.
                nws_agreement = 1 - (abs(primary - sum(other_values)/len(other_values)) / 10)
                nws_agreement = max(0.3, min(1.0, nws_agreement))
            else:
                nws_agreement = 0.85  # NWS alone is still good
        else:
            # No NWS — use average of available sources
            avg_high = sum(highs) / len(highs) if highs else None
            avg_low = sum(lows) / len(lows) if lows else None
            avg_current = sum(currents) / len(currents) if (currents := [s.current_temp_f for s in snapshots if s.current_temp_f is not None]) else None
            nws_agreement = 1.0

        # Source agreement: how much do sources agree?
        if len(snapshots) > 1:
            high_spread = max(highs) - min(highs) if highs else 0.0
            low_spread = max(lows) - min(lows) if lows else 0.0
            spread = (low_spread if measurement_kind == "low" else high_spread
                      if measurement_kind == "high" else high_spread + low_spread)
            agreement = max(0, 1 - spread / 20)
        else:
            agreement = 1.0

        # Confidence: more sources + NWS agreement = higher confidence
        # Existing NWS forecast presence boosts confidence.
        has_nws = nws_snapshot is not None
        base_confidence = {1: 0.70, 2: 0.82, 3: 0.90}.get(len(snapshots), 0.70)
        if has_nws:
            base_confidence += 0.05
        
        confidence = base_confidence * agreement
        om_snapshot = next((s for s in snapshots if s.source == "open-meteo"), None)
        nws_open_meteo_gap = None
        if nws_snapshot and om_snapshot:
            metrics = ("low_temp_f",) if measurement_kind == "low" else (
                ("high_temp_f",) if measurement_kind == "high" else ("high_temp_f", "low_temp_f")
            )
            gaps = [abs(getattr(nws_snapshot, metric) - getattr(om_snapshot, metric))
                    for metric in metrics if getattr(nws_snapshot, metric) is not None
                    and getattr(om_snapshot, metric) is not None]
            nws_open_meteo_gap = max(gaps) if gaps else None
            if nws_open_meteo_gap is not None and nws_open_meteo_gap > 10:
                confidence = max(0.10, confidence - 0.15)

        result = MultiSourceForecast(
            city=city_lower,
            high_temp_f=round(avg_high, 1) if avg_high is not None else None,
            low_temp_f=round(avg_low, 1) if avg_low is not None else None,
            current_temp_f=round(avg_current, 1) if avg_current is not None else None,
            sources_used=[s.source for s in snapshots],
            confidence=round(confidence, 2),
            fetched_at=datetime.now(timezone.utc),
            source_agreement=round(agreement, 2),
            details={
                "individual_highs": {s.source: s.high_temp_f for s in snapshots},
                "individual_lows": {s.source: s.low_temp_f for s in snapshots},
                "individual_currents": {s.source: s.current_temp_f for s in snapshots},
                "source_confidences": {s.source: s.confidence for s in snapshots},
                "source_fetched_at": {s.source: s.fetched_at.isoformat() for s in snapshots},
                "source_as_of": {s.source: s.as_of.isoformat() if isinstance(s.as_of, datetime) else s.as_of for s in snapshots if s.as_of},
                "source_weather_dates": {s.source: s.weather_date for s in snapshots if s.weather_date},
                "source_forecast_dates": {s.source: s.forecast_date for s in snapshots if s.forecast_date},
                "source_forecast_starts": {s.source: s.forecast_start for s in snapshots if s.forecast_start},
                "source_forecast_ends": {s.source: s.forecast_end for s in snapshots if s.forecast_end},
                "source_forecast_times": {s.source: list(s.forecast_times) for s in snapshots if s.forecast_times},
                "source_forecast_period_names": {s.source: s.forecast_period_name for s in snapshots if s.forecast_period_name},
                "source_forecast_period_starts": {s.source: s.forecast_period_start for s in snapshots if s.forecast_period_start},
                "source_forecast_period_ends": {s.source: s.forecast_period_end for s in snapshots if s.forecast_period_end},
                "source_metadata": {s.source: dict(s.source_details) for s in snapshots if s.source_details},
                "local_station_observation": local_station_observation,
                "forecast_driver": "nws" if has_nws else "equal_source_average",
                "nws_high": nws_snapshot.high_temp_f if nws_snapshot else None,
                "nws_low": nws_snapshot.low_temp_f if nws_snapshot else None,
                "nws_open_meteo_gap": nws_open_meteo_gap,
            }
        )

        result.details["source_snapshots"] = source_snapshots
        self._retain_collection_receipt(result, target=target, measurement_kind=measurement_kind)
        self._cache[cache_key] = (result, datetime.now(timezone.utc))
        return result

    def _retain_collection_receipt(self, forecast: MultiSourceForecast, *, target: str | None,
                                   measurement_kind: str | None) -> MultiSourceForecast:
        """Collection evidence survives independently of a scoring decision."""
        forecast.details['collection_receipt'] = self.capture_store.retain_receipt({
            'schema_version': 1, 'kind': 'weather_collection', 'city': forecast.city,
            'market_target_date': target, 'forecast_measurement_kind': measurement_kind,
            'observed_at': forecast.fetched_at.isoformat(),
            'sources': self._source_contribution_details(forecast, city=forecast.city,
                                                        forecast_measurement_kind=measurement_kind)})
        return forecast

    @staticmethod
    def select_source_forecast(forecast: MultiSourceForecast, source_id: str, *,
                               measurement_kind: str | None = None) -> MultiSourceForecast:
        """Pass a contextual selector's chosen stable ID to the numeric path.

        This does not rank sources, fabricate probabilities, mutate the original
        collection, or discard a competing forecast. Missing choices fail closed.
        """
        from dataclasses import replace

        selected = next((snapshot for snapshot in forecast.details.get('source_snapshots', [])
                         if isinstance(snapshot, WeatherSnapshot)
                         and (snapshot.source_details.get('source_id') or snapshot.source.replace('-', '_').replace(' ', '_')) == source_id), None)
        if selected is None or not selected.scoreable_forecast:
            raise ValueError('selected_source_forecast_unavailable')
        values = ([selected.high_temp_f] if measurement_kind == 'high' else [selected.low_temp_f]
                  if measurement_kind == 'low' else [selected.high_temp_f, selected.low_temp_f])
        if any(not isinstance(value, (int, float)) or isinstance(value, bool)
               or not math.isfinite(value) or not TEMP_MIN_F <= value <= TEMP_MAX_F for value in values):
            raise ValueError('selected_source_forecast_unavailable')
        return replace(forecast, high_temp_f=selected.high_temp_f, low_temp_f=selected.low_temp_f,
                       current_temp_f=selected.current_temp_f,
                       details={**forecast.details, 'forecast_driver': selected.source,
                                'selected_source_id': source_id, 'selection_policy': 'contextual_source_choice'})

    def score_temperature_market(self, question: str, yes_price: float) -> Optional[dict]:
        return self.score_temperature_market_with_context(question, yes_price)

    def score_temperature_market_with_context(
        self, question: str, yes_price: float, *, category: str = "",
        market_metadata: dict | None = None, selected_source_id: str | None = None,
        market_id: str = "", city: str | None = None, measurement_kind: str | None = None,
    ) -> Optional[dict]:
        from bot.weather.market_mapping import WeatherMarketCityMapper, resolve_weather_measurement_kind

        if city is None:
            context = WeatherMarketCityMapper().resolve(question, category, market_id=market_id,
                                                         market_metadata=market_metadata)
            if context is None:
                return None
            city = context.city.lower()
        if measurement_kind is None:
            measurement_kind = resolve_weather_measurement_kind(question, category, market_id=market_id,
                                                                  market_metadata=market_metadata)
        if measurement_kind not in ('high', 'low'):
            return None
        market_date = _market_target_date(question, category=market_id or category)
        return self._score_temperature_market(question, yes_price, market_date=market_date,
            market_metadata=market_metadata, selected_source_id=selected_source_id,
            city=city, measurement_kind=measurement_kind)

    def _score_temperature_market(
        self, question: str, yes_price: float, *, market_date: str | None,
        market_metadata: dict | None = None, selected_source_id: str | None = None,
        city: str | None = None, measurement_kind: str | None = None,
    ) -> Optional[dict]:
        """Score a temperature market using multi-source data."""
        import re
        from bot.weather.market_mapping import WeatherMarketCityMapper, resolve_weather_measurement_kind

        # Direct legacy callers share the resolver; contextual callers pass the
        # original resolved identity explicitly, never as appended question text.
        if city is None:
            context = WeatherMarketCityMapper().resolve(question, market_metadata=market_metadata)
            city = context.city.lower() if context else None
        if measurement_kind is None:
            measurement_kind = resolve_weather_measurement_kind(question, market_metadata=market_metadata)
        if city not in CITY_COORDS or measurement_kind not in ('high', 'low'):
            return None

        q = question.lower()
        is_high = measurement_kind == 'high'
        forecast = self.get_forecast(
            city, target_date=market_date, require_target=True,
            measurement_kind="high" if is_high else "low",
            station_resolution=_forecast_station_selection(city, market_metadata),
        )
        if not forecast:
            return None
        if not forecast.sources_used:
            return {
                'signal_type': 'weather', 'collection_only': True, 'prediction_available': False,
                'availability_reason': 'no_eligible_forecast_sources', 'confidence': 0,
                'source_timestamp': forecast.fetched_at.isoformat(), 'ttl_seconds': self._cache_ttl,
                'data': {'city': city, 'market_target_date': market_date, 'sources': [],
                         'forecast_high': None, 'forecast_low': None, 'forecast_driver': None,
                         'collection_receipt': forecast.details.get('collection_receipt'),
                         'source_details': self._source_contribution_details(forecast, city=city,
                             forecast_measurement_kind='high' if is_high else 'low')},
            }
        if selected_source_id is not None:
            forecast = self.select_source_forecast(forecast, selected_source_id,
                                                  measurement_kind='high' if is_high else 'low')

        # Extract threshold
        temp_match = re.search(r'(\d+)°', question)
        if not temp_match:
            return None
        threshold = float(temp_match.group(1))

        actual_temp = forecast.high_temp_f if is_high else forecast.low_temp_f

        if actual_temp is not None and not (TEMP_MIN_F <= actual_temp <= TEMP_MAX_F):
            logger.warning(f"Rejecting implausible temperature forecast for {city}: {actual_temp:.1f}F")
            actual_temp = None

        is_above = ">" in q or "above" in q or "over" in q
        is_below = "<" in q or "below" in q or "under" in q
        is_range = re.search(r'(\d+)-(\d+)', q)

        if actual_temp is None:
            predicted_prob = yes_price
        elif is_range:
            low_r = float(is_range.group(1))
            high_r = float(is_range.group(2))
            mid = (low_r + high_r) / 2
            spread = high_r - low_r

            if low_r <= actual_temp <= high_r:
                predicted_prob = 0.95
            elif abs(actual_temp - mid) > spread + 5:
                predicted_prob = 0.02
            else:
                distance = min(abs(actual_temp - low_r), abs(actual_temp - high_r))
                predicted_prob = max(0.05, 0.85 - distance * 0.1)
        elif is_above:
            diff = actual_temp - threshold
            if diff > 10:
                predicted_prob = 0.99
            elif diff > 5:
                predicted_prob = 0.95
            elif diff > 0:
                predicted_prob = 0.85
            elif diff > -3:
                predicted_prob = 0.35
            elif diff > -8:
                predicted_prob = 0.10
            else:
                predicted_prob = 0.02
        elif is_below:
            diff = threshold - actual_temp
            if diff > 10:
                predicted_prob = 0.99
            elif diff > 5:
                predicted_prob = 0.95
            elif diff > 0:
                predicted_prob = 0.85
            elif diff > -3:
                predicted_prob = 0.35
            elif diff > -8:
                predicted_prob = 0.10
            else:
                predicted_prob = 0.02
        else:
            predicted_prob = yes_price

        edge = abs(predicted_prob - yes_price)

        authority = _settlement_authority(market_metadata)
        # Describe the existing heuristic; this is not a fitted distribution or
        # an empirical per-contract probability. Keep the numeric path intact.
        yes_probability = round(max(0.01, min(0.99, predicted_prob)), 4)
        probability_evidence = {
            "schema_version": 1, "yes_probability": yes_probability,
            "basis": "heuristic_weather_distance_v1",
            "calibration_status": "not_established",
            "fallback_reason": "empirical_contract_probability_unavailable",
        }
        driver = forecast.details.get("forecast_driver")
        if isinstance(driver, str) and driver:
            probability_evidence["forecast_policy_id"] = driver
        if driver in forecast.sources_used:
            probability_evidence["source_id"] = driver
            snapshot = next((s for s in forecast.details.get("source_snapshots", [])
                             if isinstance(s, WeatherSnapshot) and s.source == driver), None)
            location = snapshot.source_details.get("forecast_location") if snapshot else None
            if isinstance(location, dict):
                probability_evidence["location_provenance"] = {
                    key: value for key, value in location.items()
                    if key in {"basis", "station_id", "station_cli", "station_origin", "station_mapping",
                               "station_evidence_field", "city", "latitude", "longitude",
                               "coordinate_source", "coordinate_url", "coordinates_fetched_at"}
                    and isinstance(value, (str, int, float)) and not isinstance(value, bool)
                    and (not isinstance(value, float) or math.isfinite(value))
                }
        return {
            "signal_type": "weather",
            "predicted_prob": yes_probability,
            "probability_evidence": probability_evidence,
            "confidence": forecast.confidence,
            "source_timestamp": forecast.fetched_at.isoformat(),
            "ttl_seconds": self._cache_ttl,
            "question_side": "range" if is_range else "above" if is_above else "below" if is_below else None,
            "edge": round(edge, 4),
            "data": {
                "forecast_high": forecast.high_temp_f,
                "forecast_low": forecast.low_temp_f,
                "current_temp": forecast.current_temp_f,
                "actual_temp_used": actual_temp,
                "predicted_temp": actual_temp,
                "threshold": threshold,
                "city": city,
                "market_target_date": market_date,
                "sources": forecast.sources_used,
                "collection_receipt": forecast.details.get('collection_receipt'),
                "agreement": forecast.source_agreement,
                "source_details": self._source_contribution_details(
                    forecast,
                    city=city,
                    forecast_measurement_kind="high" if is_high else "low",
                    contract_shape="range" if is_range else "tail",
                    question_side="range" if is_range else "above" if is_above else "below" if is_below else None,
                ),
                "settlement_source": authority["source"],
                "settlement_authority": authority,
                "forecast_driver": forecast.details.get("forecast_driver") or (
                    "nws" if "nws" in forecast.sources_used else "equal_source_average"
                ),
                "nws_high": forecast.details.get("nws_high"),
                "nws_low": forecast.details.get("nws_low"),
                "nws_open_meteo_gap": forecast.details.get("nws_open_meteo_gap"),
            }
        }

    @staticmethod
    def _source_contribution_details(
        forecast: MultiSourceForecast,
        *,
        city: str | None = None,
        forecast_measurement_kind: str | None = None,
        contract_shape: str | None = None,
        question_side: str | None = None,
    ) -> list[dict]:
        forecast_driver = forecast.details.get("forecast_driver") or (
            "nws" if "nws" in forecast.sources_used else "equal_source_average"
        )
        sources = list(forecast.sources_used or [])
        has_forecast_driver = forecast_driver in sources
        equal_weight = round(1.0 / len(sources), 6) if sources else None
        details = []
        source_snapshots = forecast.details.get("source_snapshots", [])
        snapshot_by_source = {
            snapshot.source: snapshot for snapshot in source_snapshots if isinstance(snapshot, WeatherSnapshot)
        }
        for source in sources:
            if has_forecast_driver:
                drives_forecast = source == forecast_driver
                weight = 1.0 if drives_forecast else 0.0
                note = "forecast_driver_policy" if drives_forecast else "validator_only_forecast_driver_policy"
            else:
                weight = equal_weight
                note = "equal_weight_average_no_forecast_driver"
            snapshot = snapshot_by_source.get(source)
            target_mapping = snapshot.source_details.get("target_mapping") if snapshot else None
            details.append(_drop_none({
                    "source_id": source.lower().replace("-", "_").replace(" ", "_"),
                    "source_name": source,
                    "source_location_city": city,
                    "source_location_basis": snapshot.source_details.get("forecast_location", {}).get("basis") if snapshot else None,
                    "station_id": snapshot.source_details.get("forecast_location", {}).get("station_id") if snapshot else None,
                    "station_mapping": snapshot.source_details.get("forecast_location", {}).get("station_mapping") if snapshot else None,
                    "forecast_measurement_kind": forecast_measurement_kind,
                    "contract_shape": contract_shape,
                    "question_side": question_side,
                    "source_evidence_version": snapshot.source_details.get("source_evidence_version") if snapshot else None,
                    "evidence_type": snapshot.source_details.get("evidence_type") if snapshot else None,
                    "forecast_availability": snapshot.source_details.get("forecast_availability") if snapshot else None,
                    "scoreable_forecast": snapshot.scoreable_forecast if snapshot else None,
                    "availability_reason": snapshot.availability_reason if snapshot else None,
                    "market_target_date": target_mapping.get("market_target_date") if isinstance(target_mapping, dict) else None,
                    "source_target_date": target_mapping.get("source_target_date") if isinstance(target_mapping, dict) else None,
                    "target_mapping": target_mapping,
                    "role": "forecast_primary" if source == forecast_driver else "forecast_contributor" if not has_forecast_driver else "cross_validation",
                    "weight": weight,
                    "contribution": weight,
                    "weight_note": note,
                    "forecast_high": forecast.details.get("individual_highs", {}).get(source),
                    "forecast_low": forecast.details.get("individual_lows", {}).get(source),
                    "current_forecast": forecast.details.get("individual_currents", {}).get(source),
                    "confidence": forecast.details.get("source_confidences", {}).get(source),
                    "fetched_at": forecast.details.get("source_fetched_at", {}).get(source),
                    "as_of": forecast.details.get("source_as_of", {}).get(source),
                    "weather_date": forecast.details.get("source_weather_dates", {}).get(source),
                    "forecast_date": forecast.details.get("source_forecast_dates", {}).get(source),
                    "forecast_start": forecast.details.get("source_forecast_starts", {}).get(source),
                    "forecast_end": forecast.details.get("source_forecast_ends", {}).get(source),
                    "forecast_times": forecast.details.get("source_forecast_times", {}).get(source),
                    "forecast_period_name": forecast.details.get("source_forecast_period_names", {}).get(source),
                    "forecast_period_start": forecast.details.get("source_forecast_period_starts", {}).get(source),
                    "forecast_period_end": forecast.details.get("source_forecast_period_ends", {}).get(source),
                    "source_metadata": forecast.details.get("source_metadata", {}).get(source),
            }))
        for snapshot in source_snapshots:
            if not isinstance(snapshot, WeatherSnapshot) or snapshot.scoreable_forecast or snapshot.source in sources:
                continue
            target_mapping = snapshot.source_details.get("target_mapping")
            details.append(_drop_none({
                "source_id": snapshot.source_details.get('source_id') or snapshot.source.replace('-', '_').replace(' ', '_'),
                "source_name": snapshot.source,
                "source_location_city": snapshot.city,
                "source_location_basis": snapshot.source_details.get('forecast_location', {}).get('basis'),
                "forecast_measurement_kind": forecast_measurement_kind,
                "contract_shape": contract_shape,
                "question_side": question_side,
                "source_evidence_version": snapshot.source_details.get("source_evidence_version"),
                "evidence_type": snapshot.source_details.get("evidence_type"),
                "forecast_availability": snapshot.source_details.get("forecast_availability"),
                "scoreable_forecast": False,
                "forecast_high": snapshot.high_temp_f,
                "forecast_low": snapshot.low_temp_f,
                "current_forecast": snapshot.current_temp_f,
                "availability_reason": snapshot.availability_reason,
                "market_target_date": target_mapping.get("market_target_date") if isinstance(target_mapping, dict) else None,
                "source_target_date": target_mapping.get("source_target_date") if isinstance(target_mapping, dict) else None,
                "target_mapping": target_mapping,
                "as_of": snapshot.as_of.isoformat() if isinstance(snapshot.as_of, datetime) else snapshot.as_of,
                "fetched_at": snapshot.fetched_at.isoformat(),
                "source_as_of": snapshot.source_details.get("source_as_of"),
                "source_metadata": snapshot.source_details,
            }))
        local_station = forecast.details.get("local_station_observation")
        if isinstance(local_station, dict):
            station_id = str(local_station.get("station") or "").strip().upper()
            station_cli = station_id[1:] if station_id.startswith("K") else station_id
            source_id = f"local_station_{station_cli.lower()}" if station_cli else "local_station_unknown"
            details.append(
                _drop_none(
                    {
                        "source_id": source_id,
                        "source_name": f"Local station {station_cli}" if station_cli else "Local station",
                        "source_family": "local_station",
                        "source_location_basis": "station",
                        "source_evidence_version": 1,
                        "evidence_type": "observation",
                        "forecast_availability": "not_applicable",
                        "scoreable_forecast": False,
                        "availability_reason": "current_observation_not_future_forecast",
                        "role": "local_station_observation",
                        "forecast_target": "current_observation",
                        "current_forecast": local_station.get("current_temp_f"),
                        "current_temp": local_station.get("current_temp_f"),
                        "station_id": station_id or None,
                        "station_cli": station_cli or None,
                        "observed_at": local_station.get("observation_time"),
                        "as_of": local_station.get("observation_time"),
                        "source_metadata": {
                            "source": local_station.get("source"),
                            "city": local_station.get("city"),
                            "observation_time": local_station.get("observation_time"),
                        },
                    }
                )
            )
        return details

    def close(self):
        self.open_meteo.close()
        self.nws.close()
        self.owm.close()


def _drop_none(value: dict) -> dict:
    return {key: item for key, item in value.items() if item is not None}
