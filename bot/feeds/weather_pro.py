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
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

TEMP_MIN_F = -60
TEMP_MAX_F = 130


@dataclass
class WeatherSnapshot:
    """Unified weather data from any source."""
    city: str
    high_temp_f: float
    low_temp_f: float
    current_temp_f: float
    source: str
    fetched_at: datetime
    forecast_hours_ahead: float
    confidence: float
    # Extra data
    humidity: float = 0
    wind_mph: float = 0
    conditions: str = ""


@dataclass 
class MultiSourceForecast:
    """Cross-validated forecast from multiple sources."""
    city: str
    high_temp_f: float
    low_temp_f: float
    current_temp_f: float
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


class OpenMeteoFeed:
    """
    Open-Meteo: free, no API key, hourly updates.
    Uses ECMWF, GFS, DWD models with 1km resolution.
    """
    BASE = "https://api.open-meteo.com/v1/forecast"

    def __init__(self):
        self.http = httpx.Client(timeout=10)

    def get_forecast(self, city: str) -> Optional[WeatherSnapshot]:
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

            if not temps:
                return None

            # Find today's high/low from next 24 hours
            now = datetime.now()
            today_temps = []
            for i, t in enumerate(times):
                try:
                    dt = datetime.fromisoformat(t)
                    if 0 <= (dt - now).total_seconds() / 3600 <= 24:
                        today_temps.append(temps[i])
                except:
                    continue

            if not today_temps:
                today_temps = temps[:24]

            return WeatherSnapshot(
                city=city.lower(),
                high_temp_f=max(today_temps) if today_temps else 0,
                low_temp_f=min(today_temps) if today_temps else 0,
                current_temp_f=current.get("temperature_2m", 0),
                source="open-meteo",
                fetched_at=datetime.now(timezone.utc),
                forecast_hours_ahead=1,
                confidence=0.85,
                humidity=current.get("relative_humidity_2m", 0),
                wind_mph=current.get("wind_speed_10m", 0) * 0.621371,  # km/h to mph
            )
        except Exception as e:
            logger.debug(f"Open-Meteo error for {city}: {e}")
            return None

    def close(self):
        self.http.close()


class NWSFeed:
    """NWS API: free, no key, US only, 1-2 hour updates. Also provides real-time station observations."""

    def __init__(self):
        self.http = httpx.Client(timeout=10)

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

    def get_forecast(self, city: str) -> Optional[WeatherSnapshot]:
        grid = CITY_NWS.get(city.lower())
        if not grid:
            return None

        office, grid_x, grid_y, station = grid
        try:
            url = f"https://api.weather.gov/gridpoints/{office}/{grid_x},{grid_y}/forecast"
            resp = self.http.get(url, headers={"User-Agent": "PredictionBot/1.0"})
            resp.raise_for_status()
            data = resp.json()

            periods = data.get("properties", {}).get("periods", [])
            if not periods:
                return None

            today = periods[0]
            temp = today.get("temperature", 0)
            is_daytime = today.get("isDaytime", True)

            if is_daytime and len(periods) > 1:
                high = temp
                low = periods[1].get("temperature", temp)
            elif len(periods) > 1:
                low = temp
                high = periods[1].get("temperature", temp)
            else:
                high = temp
                low = temp

            return WeatherSnapshot(
                city=city.lower(),
                high_temp_f=float(high),
                low_temp_f=float(low),
                current_temp_f=float(temp),
                source="nws",
                fetched_at=datetime.now(timezone.utc),
                forecast_hours_ahead=2,
                confidence=0.85,
                conditions=today.get("shortForecast", ""),
            )
        except Exception as e:
            logger.debug(f"NWS error for {city}: {e}")
            return None

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
            temps = [item["main"]["temp"] for item in list_items[:8]]
            current_temp = list_items[0]["main"]["temp"]

            return WeatherSnapshot(
                city=city.lower(),
                high_temp_f=max(temps),
                low_temp_f=min(temps),
                current_temp_f=current_temp,
                source="openweathermap",
                fetched_at=datetime.now(timezone.utc),
                forecast_hours_ahead=0.5,  # Updated every 30 min
                confidence=0.90,
                humidity=list_items[0]["main"].get("humidity", 0),
                wind_mph=list_items[0]["wind"].get("speed", 0),
                conditions=list_items[0]["weather"][0]["description"] if list_items[0].get("weather") else "",
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
        temps = [snapshot.high_temp_f, snapshot.low_temp_f, snapshot.current_temp_f]
        return all(TEMP_MIN_F <= temp <= TEMP_MAX_F for temp in temps)

    def get_forecast(self, city: str) -> Optional[MultiSourceForecast]:
        """Get cross-validated forecast from all sources."""
        city_lower = city.lower().strip()

        # Check cache
        if city_lower in self._cache:
            cached, ts = self._cache[city_lower]
            if (datetime.now(timezone.utc) - ts).total_seconds() < self._cache_ttl:
                return cached

        snapshots = []

        # Fetch from all sources
        om = self.open_meteo.get_forecast(city_lower)
        if om:
            if self._snapshot_is_plausible(om):
                snapshots.append(om)
            else:
                logger.warning(f"Discarding implausible Open-Meteo forecast for {city_lower}")

        nws = self.nws.get_forecast(city_lower)
        if nws:
            if self._snapshot_is_plausible(nws):
                snapshots.append(nws)
            else:
                logger.warning(f"Discarding implausible NWS forecast for {city_lower}")

        owm = self.owm.get_forecast(city_lower)
        if owm:
            if self._snapshot_is_plausible(owm):
                snapshots.append(owm)
            else:
                logger.warning(f"Discarding implausible OpenWeatherMap forecast for {city_lower}")

        if not snapshots:
            return None

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
                "settlement_source": "nws",  # Kalshi uses NWS to settle
                "nws_high": nws_snapshot.high_temp_f if nws_snapshot else None,
                "nws_low": nws_snapshot.low_temp_f if nws_snapshot else None,
                "nws_open_meteo_gap": nws_open_meteo_gap,
            }
        )

        self._cache[city_lower] = (result, datetime.now(timezone.utc))
        return result

    def score_temperature_market(self, question: str, yes_price: float) -> Optional[dict]:
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

        forecast = self.get_forecast(city)
        if not forecast:
            return None

        q = question.lower()

        # Extract threshold
        temp_match = re.search(r'(\d+)°', question)
        if not temp_match:
            return None
        threshold = float(temp_match.group(1))

        is_high = "high" in q or "maximum" in q or "max" in q
        actual_temp = forecast.high_temp_f if is_high else forecast.low_temp_f

        if not (TEMP_MIN_F <= actual_temp <= TEMP_MAX_F):
            logger.warning(f"Rejecting implausible temperature forecast for {city}: {actual_temp:.1f}F")
            return None

        is_above = ">" in q or "above" in q or "over" in q
        is_below = "<" in q or "below" in q or "under" in q
        is_range = re.search(r'(\d+)-(\d+)', q)

        if is_range:
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
                "sources": forecast.sources_used,
                "agreement": forecast.source_agreement,
                "nws_open_meteo_gap": forecast.details.get("nws_open_meteo_gap"),
            }
        }

    def close(self):
        self.open_meteo.close()
        self.nws.close()
        self.owm.close()
