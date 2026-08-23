"""
Multi-source weather engine — fastest, most accurate free weather data.

Sources (in priority order):
1. Open-Meteo: free, no key, hourly updates, 1km resolution
2. NWS: free, no key, US only, 1-2 hour updates
3. OpenWeatherMap: free tier, minute-level updates, needs API key (optional)

Cross-validates multiple sources for higher confidence.
"""

import logging
import httpx
from typing import Optional
from datetime import date, datetime, timezone, timedelta
from dataclasses import dataclass, field

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
}

# NWS grid coordinates + exact station IDs used for Kalshi settlement
# CRITICAL: Kalshi settles using specific NWS stations, not city averages
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


def _c_to_f(c: float) -> float:
    return c * 9/5 + 32


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


class OpenMeteoFeed:
    """
    Open-Meteo: free, no API key, hourly updates.
    Uses ECMWF, GFS, DWD models with 1km resolution.
    """
    BASE = "https://api.open-meteo.com/v1/forecast"

    def __init__(self):
        self.http = httpx.Client(timeout=10)

    def get_forecast(self, city: str, *, target_date: str | None = None) -> Optional[WeatherSnapshot]:
        coords = CITY_COORDS.get(city.lower())
        if not coords:
            return None

        lat, lon = coords
        try:
            params = {
                "latitude": lat,
                "longitude": lon,
                "hourly": "temperature_2m",
                "current": "temperature_2m,relative_humidity_2m,wind_speed_10m",
                "temperature_unit": "fahrenheit",
                "forecast_days": 2,
                "timezone": "auto",
            }
            resp = self.http.get(self.BASE, params=params)
            resp.raise_for_status()
            data = resp.json()

            current = data.get("current", {})
            hourly = data.get("hourly", {})
            temps = hourly.get("temperature_2m", [])
            times = hourly.get("time", [])

            fetched_at = datetime.now(timezone.utc)
            if not temps:
                return _unavailable_snapshot(
                    city=city, source="open-meteo", target_date=target_date,
                    reason="source_forecast_missing_hourly_temperatures", fetched_at=fetched_at,
                )

            target = _target_date_text(target_date)
            if target_date is not None and target is None:
                return _unavailable_snapshot(
                    city=city, source="open-meteo", target_date=target_date,
                    reason="invalid_market_target_date", fetched_at=fetched_at,
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
                    source_details={"timezone": data.get("timezone"), "utc_offset_seconds": data.get("utc_offset_seconds")},
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
                    "timezone": data.get("timezone"),
                    "utc_offset_seconds": data.get("utc_offset_seconds"),
                    "current_time": current.get("time"),
                    "forecast_times_used": list(selected_times),
                },
            )
        except Exception as e:
            logger.debug(f"Open-Meteo error for {city}: {e}")
            return _unavailable_snapshot(
                city=city, source="open-meteo", target_date=target_date,
                reason="source_forecast_request_failed",
            )

    def close(self):
        self.http.close()


class NWSFeed:
    """NWS API: free, no key, US only, 1-2 hour updates. Also provides real-time station observations."""

    def __init__(self):
        self.http = httpx.Client(timeout=10)
        self._points_cache: dict[str, tuple[datetime, tuple[str, int, int]]] = {}
        self._points_cache_ttl = timedelta(hours=6)

    def _resolve_forecast_grid(self, city: str) -> Optional[tuple[str, int, int, str]]:
        city_key = city.lower().strip()
        station_data = CITY_NWS.get(city_key)
        coords = CITY_COORDS.get(city_key)
        if not station_data or not coords:
            return None

        _, _, _, station = station_data
        cached = self._points_cache.get(city_key)
        now = datetime.now(timezone.utc)
        if cached and (now - cached[0]) < self._points_cache_ttl:
            office, grid_x, grid_y = cached[1]
            return office, grid_x, grid_y, station

        lat, lon = coords
        try:
            resp = self.http.get(
                f"https://api.weather.gov/points/{lat},{lon}",
                headers={"User-Agent": "PredictionBot/1.0"},
            )
            resp.raise_for_status()
            props = resp.json().get("properties", {})
            office = props.get("gridId")
            grid_x = props.get("gridX")
            grid_y = props.get("gridY")
            if office and grid_x is not None and grid_y is not None:
                resolved = (str(office), int(grid_x), int(grid_y))
                self._points_cache[city_key] = (now, resolved)
                return resolved[0], resolved[1], resolved[2], station
        except Exception as e:
            logger.debug(f"NWS points lookup error for {city}: {e}")

        office, grid_x, grid_y, station = station_data
        return office, grid_x, grid_y, station

    def get_station_observation(self, city: str) -> Optional[dict]:
        """
        Get real-time NWS station observation for the exact station
        used by Kalshi to settle temperature markets.
        
        This is the REAL edge — you can see current temperature at
        the settlement station throughout the day.
        
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

    def get_forecast(self, city: str, *, target_date: str | None = None) -> Optional[WeatherSnapshot]:
        grid = self._resolve_forecast_grid(city)
        if not grid:
            return None

        office, grid_x, grid_y, station = grid
        try:
            url = f"https://api.weather.gov/gridpoints/{office}/{grid_x},{grid_y}/forecast"
            resp = self.http.get(url, headers={"User-Agent": "PredictionBot/1.0"})
            resp.raise_for_status()
            data = resp.json()

            periods = data.get("properties", {}).get("periods", [])
            fetched_at = datetime.now(timezone.utc)
            target = _target_date_text(target_date)
            if target_date is not None and target is None:
                return _unavailable_snapshot(city=city, source="nws", target_date=target_date, reason="invalid_market_target_date", fetched_at=fetched_at)
            if not periods:
                return _unavailable_snapshot(city=city, source="nws", target_date=target, reason="source_forecast_missing_periods", fetched_at=fetched_at)
            if target:
                dated_periods: list[dict] = []
                for period in periods:
                    start = period.get("startTime")
                    if not isinstance(start, str) or not start:
                        return _unavailable_snapshot(city=city, source="nws", target_date=target, reason="source_forecast_period_timezone_ambiguous", fetched_at=fetched_at)
                    parsed = datetime.fromisoformat(start.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        return _unavailable_snapshot(city=city, source="nws", target_date=target, reason="source_forecast_period_timezone_ambiguous", fetched_at=fetched_at)
                    if parsed.date().isoformat() == target:
                        dated_periods.append(period)
                if not dated_periods:
                    return _unavailable_snapshot(city=city, source="nws", target_date=target, reason="target_date_not_in_source_forecast", fetched_at=fetched_at)
                high_period = next((p for p in dated_periods if p.get("isDaytime") is True), None)
                if high_period is None:
                    return _unavailable_snapshot(city=city, source="nws", target_date=target, reason="target_date_missing_daytime_forecast_period", fetched_at=fetched_at)
                low_period = next((p for p in dated_periods if p.get("isDaytime") is False), high_period)
            else:
                high_period = next((p for p in periods if p.get("isDaytime") is True), periods[0])
                low_period = next((p for p in periods if p.get("isDaytime") is False), high_period)
            high = high_period.get("temperature")
            low = low_period.get("temperature")
            if high is None or low is None:
                return _unavailable_snapshot(city=city, source="nws", target_date=target, reason="source_forecast_missing_temperature", fetched_at=fetched_at)
            today = high_period
            temp = high
            is_daytime = bool(today.get("isDaytime", True))
            period_name = today.get("name")
            period_start = today.get("startTime")
            period_end = today.get("endTime")
            periods_used = [self._period_ref(period) for period in (high_period, low_period)]

            return WeatherSnapshot(
                city=city.lower(),
                high_temp_f=float(high),
                low_temp_f=float(low),
                current_temp_f=float(temp),
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
                        "mapping": "exact_source_local_nws_period" if target else None,
                        "source_period_start": period_start,
                        "source_period_end": period_end,
                    },
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
                },
            )
        except Exception as e:
            logger.debug(f"NWS error for {city}: {e}")
            return _unavailable_snapshot(city=city, source="nws", target_date=target_date, reason="source_forecast_request_failed")

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

    def get_forecast(self, city: str) -> Optional[WeatherSnapshot]:
        if not self.available:
            return None

        coords = CITY_COORDS.get(city.lower())
        if not coords:
            return None

        lat, lon = coords
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
                    "forecast_times_used": [str(value) for value in times_used],
                },
            )
        except Exception as e:
            logger.debug(f"OpenWeatherMap error for {city}: {e}")
            return None

    def close(self):
        self.http.close()


class ProWeatherEngine:
    """
    Multi-source weather engine with cross-validation.
    
    Fetches from all available sources, averages them, and
    returns a high-confidence forecast.
    """

    def __init__(self):
        self.open_meteo = OpenMeteoFeed()
        self.nws = NWSFeed()
        self.owm = OpenWeatherMapFeed()
        self._cache = {}
        self._cache_ttl = 600  # 10 min

    def _snapshot_is_plausible(self, snapshot: WeatherSnapshot) -> bool:
        if not snapshot.scoreable_forecast:
            return False
        temps = [snapshot.high_temp_f, snapshot.low_temp_f, snapshot.current_temp_f]
        return all(isinstance(temp, (int, float)) and TEMP_MIN_F <= temp <= TEMP_MAX_F for temp in temps)

    def get_forecast(
        self, city: str, *, target_date: str | None = None, require_target: bool = False,
    ) -> Optional[MultiSourceForecast]:
        """Get cross-validated forecast from all sources."""
        city_lower = city.lower().strip()
        target = _target_date_text(target_date)

        # Check cache
        cache_key = (city_lower, target, require_target)
        if cache_key in self._cache:
            cached, ts = self._cache[cache_key]
            if (datetime.now(timezone.utc) - ts).total_seconds() < self._cache_ttl:
                return cached

        if require_target and target is None:
            source_snapshots = [
                _unavailable_snapshot(city=city_lower, source=source, target_date=target_date, reason="missing_market_target_date")
                for source in ("open-meteo", "nws", "openweathermap")
            ]
            result = MultiSourceForecast(
                city=city_lower, high_temp_f=None, low_temp_f=None, current_temp_f=None,
                sources_used=[], confidence=0, fetched_at=datetime.now(timezone.utc),
                details={"source_snapshots": source_snapshots, "settlement_source": "nws"},
            )
            self._cache[cache_key] = (result, datetime.now(timezone.utc))
            return result

        snapshots = []
        source_snapshots = []

        # Fetch from all sources
        om = self.open_meteo.get_forecast(city_lower, target_date=target)
        if om:
            source_snapshots.append(om)
            if self._snapshot_is_plausible(om):
                snapshots.append(om)
            else:
                logger.warning(f"Open-Meteo forecast unavailable for {city_lower}: {om.availability_reason or 'implausible_forecast'}")

        nws = self.nws.get_forecast(city_lower, target_date=target)
        if nws:
            source_snapshots.append(nws)
            if self._snapshot_is_plausible(nws):
                snapshots.append(nws)
            else:
                logger.warning(f"NWS forecast unavailable for {city_lower}: {nws.availability_reason or 'implausible_forecast'}")

        owm = (
            _unavailable_snapshot(
                city=city_lower, source="openweathermap", target_date=target,
                reason="source_forecast_period_timezone_ambiguous",
            )
            if target else self.owm.get_forecast(city_lower)
        )
        if owm:
            source_snapshots.append(owm)
            if self._snapshot_is_plausible(owm):
                snapshots.append(owm)
            else:
                logger.warning(f"OpenWeatherMap forecast unavailable for {city_lower}: {owm.availability_reason or 'implausible_forecast'}")

        if not snapshots:
            local_station_observation = self.nws.get_station_observation(city_lower) if nws else None
            result = MultiSourceForecast(
                city=city_lower, high_temp_f=None, low_temp_f=None, current_temp_f=None,
                sources_used=[], confidence=0, fetched_at=datetime.now(timezone.utc),
                details={"source_snapshots": source_snapshots, "local_station_observation": local_station_observation, "settlement_source": "nws"},
            )
            self._cache[cache_key] = (result, datetime.now(timezone.utc))
            return result

        local_station_observation = None
        if nws:
            local_station_observation = self.nws.get_station_observation(city_lower)

        # Cross-validate
        highs = [s.high_temp_f for s in snapshots]
        lows = [s.low_temp_f for s in snapshots]

        # NWS is the settlement source for Kalshi temperature markets.
        # Weight NWS more heavily when available, but use others for validation.
        nws_snapshot = next((s for s in snapshots if s.source == "nws"), None)
        
        if nws_snapshot:
            # Use NWS as primary, others for confidence boost
            avg_high = nws_snapshot.high_temp_f
            avg_low = nws_snapshot.low_temp_f
            avg_current = nws_snapshot.current_temp_f
            
            # Check if other sources agree with NWS
            other_highs = [s.high_temp_f for s in snapshots if s.source != "nws"]
            if other_highs:
                # How close are other sources to NWS?
                nws_agreement = 1 - (abs(avg_high - sum(other_highs)/len(other_highs)) / 10)
                nws_agreement = max(0.3, min(1.0, nws_agreement))
            else:
                nws_agreement = 0.85  # NWS alone is still good
        else:
            # No NWS — use average of available sources
            avg_high = sum(highs) / len(highs)
            avg_low = sum(lows) / len(lows)
            avg_current = sum(s.current_temp_f for s in snapshots) / len(snapshots)
            nws_agreement = 1.0

        # Source agreement: how much do sources agree?
        if len(snapshots) > 1:
            high_spread = max(highs) - min(highs)
            low_spread = max(lows) - min(lows)
            agreement = max(0, 1 - (high_spread + low_spread) / 20)
        else:
            agreement = 1.0

        # Confidence: more sources + NWS agreement = higher confidence
        # NWS is the settlement source, so NWS presence boosts confidence
        has_nws = nws_snapshot is not None
        base_confidence = {1: 0.70, 2: 0.82, 3: 0.90}.get(len(snapshots), 0.70)
        if has_nws:
            base_confidence += 0.05  # NWS is the settlement source
        
        confidence = base_confidence * agreement
        om_snapshot = next((s for s in snapshots if s.source == "open-meteo"), None)
        nws_open_meteo_gap = None
        if nws_snapshot and om_snapshot:
            nws_open_meteo_gap = max(
                abs(nws_snapshot.high_temp_f - om_snapshot.high_temp_f),
                abs(nws_snapshot.low_temp_f - om_snapshot.low_temp_f),
            )
            if nws_open_meteo_gap > 10:
                confidence = max(0.10, confidence - 0.15)

        result = MultiSourceForecast(
            city=city_lower,
            high_temp_f=round(avg_high, 1),
            low_temp_f=round(avg_low, 1),
            current_temp_f=round(avg_current, 1),
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
                "settlement_source": "nws",  # Kalshi uses NWS to settle
                "forecast_driver": "nws" if has_nws else "equal_source_average",
                "nws_high": nws_snapshot.high_temp_f if nws_snapshot else None,
                "nws_low": nws_snapshot.low_temp_f if nws_snapshot else None,
                "nws_open_meteo_gap": nws_open_meteo_gap,
            }
        )

        result.details["source_snapshots"] = source_snapshots
        self._cache[cache_key] = (result, datetime.now(timezone.utc))
        return result

    def score_temperature_market(self, question: str, yes_price: float) -> Optional[dict]:
        return self.score_temperature_market_with_context(question, yes_price)

    def score_temperature_market_with_context(
        self, question: str, yes_price: float, *, category: str = "",
    ) -> Optional[dict]:
        market_date = _market_target_date(question, category=category)
        return self._score_temperature_market(question, yes_price, market_date=market_date)

    def _score_temperature_market(
        self, question: str, yes_price: float, *, market_date: str | None,
    ) -> Optional[dict]:
        """Score a temperature market using multi-source data."""
        import re

        # Find city
        city = None
        for c in CITY_COORDS:
            if c in question.lower():
                city = c
                break
        if not city:
            return None

        forecast = self.get_forecast(city, target_date=market_date, require_target=True)
        if not forecast or not forecast.sources_used:
            return None

        q = question.lower()

        # Extract threshold
        temp_match = re.search(r'(\d+)°', question)
        if not temp_match:
            return None
        threshold = float(temp_match.group(1))

        is_high = "high" in q or "maximum" in q or "max" in q
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

        return {
            "signal_type": "weather",
            "predicted_prob": round(max(0.01, min(0.99, predicted_prob)), 4),
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
                "agreement": forecast.source_agreement,
                "source_details": self._source_contribution_details(
                    forecast,
                    city=city,
                    forecast_measurement_kind="high" if is_high else "low",
                    contract_shape="range" if is_range else "tail",
                    question_side="range" if is_range else "above" if is_above else "below" if is_below else None,
                ),
                "settlement_source": forecast.details.get("settlement_source"),
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
        settlement_source = forecast.details.get("settlement_source")
        sources = list(forecast.sources_used or [])
        has_settlement_source = settlement_source in sources
        equal_weight = round(1.0 / len(sources), 6) if sources else None
        details = []
        source_snapshots = forecast.details.get("source_snapshots", [])
        snapshot_by_source = {
            snapshot.source: snapshot for snapshot in source_snapshots if isinstance(snapshot, WeatherSnapshot)
        }
        for source in sources:
            if has_settlement_source:
                drives_forecast = source == settlement_source
                weight = 1.0 if drives_forecast else 0.0
                note = "settlement_source_drives_forecast" if drives_forecast else "validator_only_settlement_source_drives_forecast"
            else:
                weight = equal_weight
                note = "equal_weight_average_no_settlement_source"
            snapshot = snapshot_by_source.get(source)
            target_mapping = snapshot.source_details.get("target_mapping") if snapshot else None
            details.append(_drop_none({
                    "source_id": source.lower().replace("-", "_").replace(" ", "_"),
                    "source_name": source,
                    "source_location_city": city,
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
                    "role": "settlement_primary" if source == settlement_source else "forecast_contributor" if not has_settlement_source else "cross_validation",
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
                "source_name": snapshot.source,
                "source_evidence_version": snapshot.source_details.get("source_evidence_version"),
                "evidence_type": snapshot.source_details.get("evidence_type"),
                "forecast_availability": snapshot.source_details.get("forecast_availability"),
                "scoreable_forecast": False,
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
