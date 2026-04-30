from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
import csv
import io
import httpx

from bot.feeds.weather_pro import CITY_COORDS, CITY_NWS, TEMP_MAX_F, TEMP_MIN_F
from bot.weather.date_matcher import validate_weather_date_match
from bot.weather.registry import WeatherRegistry
from bot.weather.station_mapping import WeatherStationResolution, resolve_weather_station

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HistoricalWeatherObservation:
    city: str
    weather_date: str
    high_temp_f: float
    low_temp_f: float
    source: str = "open_meteo_archive"
    station_id: str | None = None
    station_cli: str | None = None
    source_quality: str = "grid_fallback"

    def as_dict(self) -> dict[str, Any]:
        return {
            "city": self.city,
            "weather_date": self.weather_date,
            "high_temp_f": self.high_temp_f,
            "low_temp_f": self.low_temp_f,
            "source": self.source,
            "station_id": self.station_id,
            "station_cli": self.station_cli,
            "source_quality": self.source_quality,
        }


class HistoricalOpenMeteoWeatherEngine:
    """Historical observed-weather scorer for archive replay.

    This intentionally mirrors the `score_temperature_market` shape from
    ProWeatherEngine, but uses Open-Meteo's archive endpoint and validates that
    the returned observation date matches the market target date.
    """

    BASE_URL = "https://archive-api.open-meteo.com/v1/archive"
    IEM_ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
    NOAA_DAILY_URL = "https://www.ncei.noaa.gov/access/services/data/v1"
    NOAA_DAILY_STATIONS = {
        "KATL": "USW00013874",
        "KAUS": "USW00013958",
        "KBOS": "USW00014739",
        "KMDW": "USW00014819",
        "KDFW": "USW00003927",
        "KDEN": "USW00003017",
        "KHOU": "USW00012918",
        "KLAS": "USW00023169",
        "KLAX": "USW00023174",
        "KMIA": "USW00012839",
        "KMSP": "USW00014922",
        "KMSY": "USW00012916",
        "KNYC": "USW00094728",
        "KOKC": "USW00013967",
        "KPDX": "USW00024229",
        "KPHL": "USW00013739",
        "KPHX": "USW00023183",
        "KSAN": "USW00023188",
        "KSAT": "USW00012921",
        "KSEA": "USW00024233",
        "KSFO": "USW00023234",
        "KTPA": "USW00012842",
    }

    def __init__(self, *, cache_path: str | Path | None = None, timeout: float = 20.0):
        self.http = httpx.Client(timeout=timeout)
        self.cache_path = Path(cache_path) if cache_path else None
        self._cache: dict[str, dict[str, Any]] = {}
        self._last_iem_request_at = 0.0
        self._iem_min_interval_seconds = 1.25
        if self.cache_path and self.cache_path.exists():
            try:
                self._cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except Exception:
                logger.warning("Could not load historical weather cache %s", self.cache_path)
                self._cache = {}

    def close(self) -> None:
        if self.cache_path:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self._cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.http.close()

    def get_observation(
        self,
        city: str,
        market_date: date,
        *,
        station_resolution: WeatherStationResolution | None = None,
    ) -> Optional[HistoricalWeatherObservation]:
        city_key = city.lower().strip()
        station_obs = self._get_official_daily_station_observation(
            city_key,
            market_date,
            station_resolution=station_resolution,
        )
        if station_obs is not None:
            return station_obs
        station_obs = self._get_station_observation(city_key, market_date, station_resolution=station_resolution)
        if station_obs is not None:
            return station_obs
        return self._get_open_meteo_observation(city_key, market_date)

    def _get_official_daily_station_observation(
        self,
        city_key: str,
        market_date: date,
        *,
        station_resolution: WeatherStationResolution | None = None,
    ) -> Optional[HistoricalWeatherObservation]:
        station_id, station_cli = self._station_for_city(city_key, station_resolution=station_resolution)
        if not station_id:
            return None
        noaa_station = self.NOAA_DAILY_STATIONS.get(station_id)
        if not noaa_station:
            return None
        date_iso = market_date.isoformat()
        cache_key = f"noaa_daily:{station_id}:{date_iso}"
        cached = self._cache.get(cache_key)
        if cached:
            return self._observation_from_cache(cached)

        params = {
            "dataset": "daily-summaries",
            "stations": noaa_station,
            "startDate": date_iso,
            "endDate": date_iso,
            "dataTypes": "TMAX,TMIN",
            "units": "standard",
            "format": "json",
        }
        try:
            resp = self.http.get(self.NOAA_DAILY_URL, params=params)
            resp.raise_for_status()
            payload = resp.json()
            if not payload:
                return None
            row = payload[0]
            high = float(row.get("TMAX"))
            low = float(row.get("TMIN"))
            if not (TEMP_MIN_F <= high <= TEMP_MAX_F and TEMP_MIN_F <= low <= TEMP_MAX_F):
                return None
            obs = HistoricalWeatherObservation(
                city=city_key,
                weather_date=str(row.get("DATE") or date_iso),
                high_temp_f=high,
                low_temp_f=low,
                source="noaa_daily_summaries_station",
                station_id=station_id,
                station_cli=station_cli,
                source_quality="settlement_station_official_daily",
            )
            self._cache[cache_key] = obs.as_dict()
            return obs
        except Exception as exc:
            logger.debug("NOAA daily archive error for %s %s %s: %s", station_id, city_key, date_iso, exc)
            return None

    def _get_station_observation(
        self,
        city_key: str,
        market_date: date,
        *,
        station_resolution: WeatherStationResolution | None = None,
    ) -> Optional[HistoricalWeatherObservation]:
        station_id, station_cli = self._station_for_city(city_key, station_resolution=station_resolution)
        if not station_id:
            return None
        date_iso = market_date.isoformat()
        cache_key = f"station:{station_id}:{date_iso}"
        cached = self._cache.get(cache_key)
        if cached:
            return self._observation_from_cache(cached)

        timezone_name = _timezone_for_city(city_key)
        next_day = market_date + timedelta(days=1)
        params = {
            "station": station_id,
            "data": "tmpf",
            "year1": market_date.year,
            "month1": market_date.month,
            "day1": market_date.day,
            "year2": next_day.year,
            "month2": next_day.month,
            "day2": next_day.day,
            "tz": timezone_name,
            "format": "onlycomma",
            "latlon": "no",
            "elev": "no",
            "missing": "M",
            "trace": "T",
            "direct": "no",
            "report_type": ["1", "2"],
        }
        try:
            resp = self._get_iem_asos(params)
            resp.raise_for_status()
            temps = self._parse_iem_temperature_csv(resp.text, market_date.isoformat())
            if not temps:
                return None
            obs = HistoricalWeatherObservation(
                city=city_key,
                weather_date=date_iso,
                high_temp_f=max(temps),
                low_temp_f=min(temps),
                source="iem_asos_station_hourly",
                station_id=station_id,
                station_cli=station_cli,
                source_quality="settlement_station_hourly",
            )
            self._cache[cache_key] = obs.as_dict()
            return obs
        except Exception as exc:
            logger.debug("IEM ASOS archive error for %s %s %s: %s", station_id, city_key, date_iso, exc)
            return None

    def _get_iem_asos(self, params: dict[str, Any]):
        elapsed = time.monotonic() - self._last_iem_request_at
        if elapsed < self._iem_min_interval_seconds:
            time.sleep(self._iem_min_interval_seconds - elapsed)
        response = self.http.get(self.IEM_ASOS_URL, params=params)
        self._last_iem_request_at = time.monotonic()
        if getattr(response, "status_code", None) == 429:
            retry_after = getattr(response, "headers", {}).get("Retry-After") if hasattr(response, "headers") else None
            try:
                delay = min(10.0, max(1.0, float(retry_after))) if retry_after else 3.0
            except (TypeError, ValueError):
                delay = 3.0
            time.sleep(delay)
            response = self.http.get(self.IEM_ASOS_URL, params=params)
            self._last_iem_request_at = time.monotonic()
        return response

    def _get_open_meteo_observation(self, city_key: str, market_date: date) -> Optional[HistoricalWeatherObservation]:
        coords = CITY_COORDS.get(city_key)
        if not coords:
            return None
        date_iso = market_date.isoformat()
        cache_key = f"open_meteo:{city_key}:{date_iso}"
        # Backward-compatible read for cache entries created before provider namespacing.
        cached = self._cache.get(cache_key) or self._cache.get(f"{city_key}:{date_iso}")
        if cached:
            return self._observation_from_cache(cached)

        lat, lon = coords
        params = {
            "latitude": lat,
            "longitude": lon,
            "start_date": date_iso,
            "end_date": date_iso,
            "daily": "temperature_2m_max,temperature_2m_min",
            "temperature_unit": "fahrenheit",
            "timezone": "auto",
        }
        try:
            resp = self.http.get(self.BASE_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
            daily = data.get("daily") or {}
            dates = daily.get("time") or []
            highs = daily.get("temperature_2m_max") or []
            lows = daily.get("temperature_2m_min") or []
            if not dates or not highs or not lows:
                return None
            high = float(highs[0])
            low = float(lows[0])
            if not (TEMP_MIN_F <= high <= TEMP_MAX_F and TEMP_MIN_F <= low <= TEMP_MAX_F):
                return None
            obs = HistoricalWeatherObservation(
                city=city_key,
                weather_date=str(dates[0]),
                high_temp_f=high,
                low_temp_f=low,
                source="open_meteo_archive",
                source_quality="grid_fallback",
            )
            self._cache[cache_key] = obs.as_dict()
            return obs
        except Exception as exc:
            logger.debug("Open-Meteo archive error for %s %s: %s", city_key, date_iso, exc)
            return None

    @staticmethod
    def _parse_iem_temperature_csv(payload: str, expected_date: str) -> list[float]:
        temps: list[float] = []
        reader = csv.DictReader(io.StringIO(payload))
        for row in reader:
            valid = str(row.get("valid") or "")
            if expected_date and not valid.startswith(expected_date):
                continue
            raw_temp = str(row.get("tmpf") or "").strip()
            if not raw_temp or raw_temp.upper() in {"M", "NA", "NAN"}:
                continue
            try:
                temp = float(raw_temp)
            except ValueError:
                continue
            if TEMP_MIN_F <= temp <= TEMP_MAX_F:
                temps.append(temp)
        return temps

    @staticmethod
    def _observation_from_cache(cached: dict[str, Any]) -> HistoricalWeatherObservation:
        return HistoricalWeatherObservation(
            city=str(cached["city"]),
            weather_date=str(cached["weather_date"]),
            high_temp_f=float(cached["high_temp_f"]),
            low_temp_f=float(cached["low_temp_f"]),
            source=str(cached.get("source") or "open_meteo_archive"),
            station_id=str(cached.get("station_id") or "") or None,
            station_cli=str(cached.get("station_cli") or "") or None,
            source_quality=str(cached.get("source_quality") or "grid_fallback"),
        )

    @staticmethod
    def _station_for_city(
        city_key: str,
        *,
        station_resolution: WeatherStationResolution | None = None,
    ) -> tuple[str | None, str | None]:
        if station_resolution and station_resolution.station_id:
            station_id = station_resolution.station_id.upper()
            return station_id, station_resolution.station_cli or station_id[1:] if station_id.startswith("K") else station_id
        city_station = CITY_NWS.get(city_key)
        if city_station:
            station_id = str(city_station[3]).upper()
            return station_id, station_id[1:] if station_id.startswith("K") else station_id
        return None, None

    def score_temperature_market(self, question: str, yes_price: float) -> Optional[dict[str, Any]]:
        return self.score_temperature_market_with_context(question, yes_price)

    def score_temperature_market_with_context(
        self,
        question: str,
        yes_price: float,
        *,
        category: str = "",
    ) -> Optional[dict[str, Any]]:
        city = self._extract_city(question) or self._city_from_ticker(category)
        if not city:
            return None
        market_context = {"question": question, "market_ticker": category, "category": category, "market_id": category, "ticker": category}
        station_resolution = resolve_weather_station(market_context)
        market_date_derivation = validate_weather_date_match(market_context, weather_date=None)
        # The validator above intentionally fails because no weather date exists yet;
        # use the derived market date from its result, then validate after fetch.
        market_date_text = market_date_derivation.market_date
        if not market_date_text:
            from bot.weather.date_matcher import derive_market_date

            derived = derive_market_date(market_context)
            market_date_text = derived.isoformat
        if not market_date_text:
            return {
                "signal_type": "weather",
                "predicted_prob": yes_price,
                "confidence": 0.0,
                "source_timestamp": datetime.now(timezone.utc).isoformat(),
                "ttl_seconds": 0,
                "question_side": None,
                "edge": 0.0,
                "data": {"city": city, "date_validation": {"ok": False, "reason": "missing_market_date"}},
                "warnings": ["missing_market_date"],
            }
        try:
            market_date = date.fromisoformat(market_date_text)
        except ValueError:
            return None

        obs = self.get_observation(city, market_date, station_resolution=station_resolution)
        if not obs:
            return None
        validation = validate_weather_date_match(market_context, obs.as_dict())
        if not validation.ok:
            return {
                "signal_type": "weather",
                "predicted_prob": yes_price,
                "confidence": 0.0,
                "source_timestamp": datetime.now(timezone.utc).isoformat(),
                "ttl_seconds": 0,
                "question_side": None,
                "edge": 0.0,
                "data": {"city": city, "date_validation": validation.as_dict(), **obs.as_dict()},
                "warnings": [validation.reason],
            }

        q = question.lower()
        threshold = self._extract_threshold(question)
        if threshold is None:
            return None
        is_high = "high" in q or "maximum" in q or "max" in q
        actual_temp = obs.high_temp_f if is_high else obs.low_temp_f
        is_above = ">" in q or "above" in q or "over" in q or "more than" in q
        is_below = "<" in q or "below" in q or "under" in q or "less than" in q
        range_match = re.search(r"(\d+)\s*-\s*(\d+)", q)

        if range_match:
            low_r = float(range_match.group(1))
            high_r = float(range_match.group(2))
            # In historical replay we are using observed settlement-station data,
            # not a forecast distribution. Range contracts should therefore be
            # scored almost deterministically from the observed daily value.
            predicted_prob = 0.99 if low_r <= actual_temp <= high_r else 0.02
            question_side = "range"
        elif is_above:
            # Kalshi threshold tickers are effectively inclusive at the printed integer
            # boundary in historical settlements (for example, T90 resolves YES when
            # the station high is exactly 90F).
            diff = actual_temp - threshold
            predicted_prob = 0.99 if diff > 10 else 0.95 if diff > 5 else 0.85 if diff >= 0 else 0.35 if diff > -3 else 0.10 if diff > -8 else 0.02
            question_side = "above"
        elif is_below:
            diff = threshold - actual_temp
            predicted_prob = 0.99 if diff > 10 else 0.95 if diff > 5 else 0.85 if diff >= 0 else 0.35 if diff > -3 else 0.10 if diff > -8 else 0.02
            question_side = "below"
        else:
            predicted_prob = yes_price
            question_side = None

        predicted_prob = round(max(0.01, min(0.99, predicted_prob)), 4)
        return {
            "signal_type": "weather",
            "predicted_prob": predicted_prob,
            "confidence": 0.995 if obs.source_quality == "settlement_station_official_daily" else 0.98 if obs.source_quality.startswith("settlement_station") else 0.85,
            "source_timestamp": f"{obs.weather_date}T23:59:59Z",
            "ttl_seconds": 0,
            "question_side": question_side,
            "edge": round(abs(predicted_prob - yes_price), 4),
            "data": {
                "forecast_high": obs.high_temp_f,
                "forecast_low": obs.low_temp_f,
                "historical_high": obs.high_temp_f,
                "historical_low": obs.low_temp_f,
                "actual_temp_used": actual_temp,
                "predicted_temp": actual_temp,
                "threshold": threshold,
                "city": city,
                "sources": [obs.source],
                "agreement": 1.0 if obs.source_quality.startswith("settlement_station") else 0.75,
                "source_quality": obs.source_quality,
                "station_id": obs.station_id,
                "station_cli": obs.station_cli,
                "station_resolution": station_resolution.to_dict(),
                "weather_date": obs.weather_date,
                "date_validation": validation.as_dict(),
                "historical_replay": True,
            },
        }

    @staticmethod
    def _extract_city(question: str) -> str | None:
        q = question.lower()
        for city in sorted(CITY_COORDS, key=len, reverse=True):
            if city in q:
                return city
        return None

    @staticmethod
    def _city_from_ticker(ticker: str) -> str | None:
        if not ticker:
            return None
        ticker_upper = ticker.upper()
        ticker_cities = {
            "AUS": "austin",
            "PHIL": "philadelphia",
            "CHI": "chicago",
            "LA": "los angeles",
            "NYC": "new york",
            "NY": "new york",
            "MIA": "miami",
            "DEN": "denver",
            "SEA": "seattle",
            "BOS": "boston",
            "HOU": "houston",
            "DAL": "dallas",
            "PHX": "phoenix",
            "ATL": "atlanta",
            "MIN": "minneapolis",
            "NOLA": "new orleans",
            "SA": "san antonio",
            "LV": "las vegas",
            "OKC": "oklahoma city",
            "PDX": "portland",
            "NSH": "nashville",
            "DET": "detroit",
            "SD": "san diego",
            "TPA": "tampa",
            "SF": "san francisco",
        }
        for suffix, city in sorted(ticker_cities.items(), key=lambda item: -len(item[0])):
            if suffix in ticker_upper:
                return city
        return None

    @staticmethod
    def _extract_threshold(question: str) -> float | None:
        match = re.search(r"(\d+)\s*°", question)
        if not match:
            match = re.search(r"(?:>|above|over|below|under|less than|more than)\s*\$?(\d+)", question, re.IGNORECASE)
        if not match:
            match = re.search(r"-T(\d+)", question, re.IGNORECASE)
        if not match:
            return None
        try:
            return float(match.group(1))
        except ValueError:
            return None


_CITY_TIMEZONE_CACHE: dict[str, str] | None = None


def _timezone_for_city(city_key: str) -> str:
    global _CITY_TIMEZONE_CACHE
    if _CITY_TIMEZONE_CACHE is None:
        lookup: dict[str, str] = {}
        try:
            for city in WeatherRegistry.from_file().as_dict().get("cities", []):
                if not isinstance(city, dict):
                    continue
                name = str(city.get("city") or "").strip().lower()
                timezone_name = str(city.get("timezone") or "").strip()
                if name and timezone_name:
                    lookup[name] = timezone_name
                    for alias in city.get("aliases", []) or []:
                        alias_name = str(alias or "").strip().lower()
                        if alias_name:
                            lookup[alias_name] = timezone_name
        except Exception:
            lookup = {}
        _CITY_TIMEZONE_CACHE = lookup
    return _CITY_TIMEZONE_CACHE.get(city_key, "UTC")
