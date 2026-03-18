"""
Live data feeds — market-specific real-time data for edge detection.

Each feed provides current data that the strategy can use to make
informed predictions rather than relying on generic price/news signals.

Available feeds:
- WeatherFeed: NWS API forecasts for temperature markets
- CryptoFeed: CoinGecko prices for crypto range/Above-below markets
- ForexFeed: Exchange rate data for currency markets
- NewsSearchFeed: Web search for breaking news (faster than RSS)
"""

import logging
import httpx
from typing import Optional
from datetime import datetime, timezone
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class WeatherForecast:
    city: str
    high_temp_f: float
    low_temp_f: float
    source: str
    forecast_hours_ahead: float
    confidence: float  # 0-1


@dataclass
class CryptoPrice:
    symbol: str
    price_usd: float
    change_24h_pct: float
    volume_24h: float
    timestamp: datetime


@dataclass
class ForexRate:
    pair: str
    rate: float
    change_24h_pct: float
    timestamp: datetime


class WeatherFeed:
    """
    Fetches weather forecasts from NWS API (free, no key needed).
    Used for temperature prediction markets.
    """

    # City → NWS grid coordinates
    CITY_GRIDS = {
        "austin": ("EWX", 152, 91),
        "new york": ("OKX", 34, 37),
        "chicago": ("LOT", 76, 73),
        "los angeles": ("LOX", 154, 44),
        "miami": ("MFL", 110, 50),
        "denver": ("BOU", 62, 60),
        "seattle": ("SEW", 124, 67),
        "philadelphia": ("PHI", 49, 75),
        "san francisco": ("MTR", 85, 105),
        "houston": ("HGX", 65, 97),
        "boston": ("BOX", 71, 65),
        "new orleans": ("LIX", 51, 69),
        "phoenix": ("PSR", 159, 57),
        "dallas": ("FWD", 156, 45),
        "minneapolis": ("MPX", 108, 48),
        "atlanta": ("FFC", 57, 87),
        "san antonio": ("EWX", 158, 97),
        "las vegas": "VEF",  # Will fix below
    }

    def __init__(self):
        self.http = httpx.Client(timeout=10)
        self._cache = {}
        self._cache_ttl = 1800  # 30 min (NWS updates every hour)

    def get_forecast(self, city: str) -> Optional[WeatherForecast]:
        """Get today's high/low temperature forecast for a city."""
        city_lower = city.lower().strip()

        if city_lower not in self.CITY_GRIDS:
            # Try partial match
            for key in self.CITY_GRIDS:
                if key in city_lower or city_lower in key:
                    city_lower = key
                    break
            else:
                return None

        grid_info = self.CITY_GRIDS[city_lower]
        if isinstance(grid_info, tuple):
            office, grid_x, grid_y = grid_info
        else:
            return None

        # Check cache
        cache_key = f"{office}_{grid_x}_{grid_y}"
        if cache_key in self._cache:
            cached, ts = self._cache[cache_key]
            if (datetime.now(timezone.utc) - ts).total_seconds() < self._cache_ttl:
                return cached

        try:
            url = f"https://api.weather.gov/gridpoints/{office}/{grid_x},{grid_y}/forecast"
            resp = self.http.get(url, headers={"User-Agent": "PredictionBot/1.0"})
            resp.raise_for_status()
            data = resp.json()

            periods = data.get("properties", {}).get("periods", [])
            if not periods:
                return None

            # Today's forecast (first daytime period)
            today = periods[0]
            temp = today.get("temperature", 0)
            is_daytime = today.get("isDaytime", True)

            if is_daytime:
                high = temp
                # Find next nighttime for low
                low = periods[1].get("temperature", temp) if len(periods) > 1 else temp
            else:
                low = temp
                high = periods[1].get("temperature", temp) if len(periods) > 1 else temp

            forecast = WeatherForecast(
                city=city_lower,
                high_temp_f=float(high),
                low_temp_f=float(low),
                source="NWS",
                forecast_hours_ahead=6,  # NWS updates frequently
                confidence=0.85,
            )

            self._cache[cache_key] = (forecast, datetime.now(timezone.utc))
            return forecast

        except Exception as e:
            logger.debug(f"Weather fetch error for {city}: {e}")
            return None

    def score_temperature_market(self, question: str, yes_price: float) -> Optional[dict]:
        """
        Score a temperature prediction market.

        Example questions:
        - "Will the high temp in Austin be >71°?"
        - "Will the minimum temperature be <32° in Chicago?"
        - "Maximum temperature 59-60° in Denver?"

        Returns: {predicted_prob, confidence, data} or None
        """
        import re

        # Extract city
        city = None
        for c in self.CITY_GRIDS:
            if c in question.lower():
                city = c
                break
        if not city:
            return None

        forecast = self.get_forecast(city)
        if not forecast:
            return None

        # Parse what the market is asking
        q = question.lower()

        # Extract temperature threshold
        temp_match = re.search(r'(\d+)°', question)
        if not temp_match:
            return None
        threshold = float(temp_match.group(1))

        # Determine if it's asking about high or low
        is_high = "high" in q or "maximum" in q or "max" in q
        actual_temp = forecast.high_temp_f if is_high else forecast.low_temp_f

        # Determine direction
        is_above = ">" in q or "above" in q or "over" in q or "more than" in q
        is_below = "<" in q or "below" in q or "under" in q or "less than" in q
        is_range = re.search(r'(\d+)-(\d+)', q)

        predicted_prob = yes_price  # Default: no opinion

        if is_range:
            range_match = re.search(r'(\d+)-(\d+)', q)
            low_range = float(range_match.group(1))
            high_range = float(range_match.group(2))
            if low_range <= actual_temp <= high_range:
                predicted_prob = 0.95  # Forecast says temp will be in range
            elif abs(actual_temp - (low_range + high_range) / 2) > 10:
                predicted_prob = 0.02  # Way off from range
            else:
                # Close to range — partial probability
                distance = min(abs(actual_temp - low_range), abs(actual_temp - high_range))
                predicted_prob = max(0.05, 0.8 - distance * 0.15)

        elif is_above:
            if actual_temp > threshold + 5:
                predicted_prob = 0.98  # Well above
            elif actual_temp > threshold:
                predicted_prob = 0.85  # Slightly above
            elif actual_temp > threshold - 5:
                predicted_prob = 0.25  # Close but below
            else:
                predicted_prob = 0.05  # Well below

        elif is_below:
            if actual_temp < threshold - 5:
                predicted_prob = 0.98  # Well below
            elif actual_temp < threshold:
                predicted_prob = 0.85  # Slightly below
            elif actual_temp < threshold + 5:
                predicted_prob = 0.25  # Close but above
            else:
                predicted_prob = 0.05  # Well above

        confidence = forecast.confidence

        return {
            "predicted_prob": max(0.01, min(0.99, predicted_prob)),
            "confidence": confidence,
            "data": {
                "forecast_high": forecast.high_temp_f,
                "forecast_low": forecast.low_temp_f,
                "actual_temp_used": actual_temp,
                "threshold": threshold,
                "city": city,
            }
        }

    def close(self):
        self.http.close()


class CryptoFeed:
    """
    Fetches crypto prices from CoinGecko (free, no key needed).
    Used for crypto range/Above-below prediction markets.
    """

    COIN_IDS = {
        "bitcoin": "bitcoin",
        "btc": "bitcoin",
        "ethereum": "ethereum",
        "eth": "ethereum",
        "solana": "solana",
        "sol": "solana",
        "shiba": "shiba-inu",
        "shib": "shiba-inu",
        "litecoin": "litecoin",
        "ltc": "litecoin",
        "chainlink": "chainlink",
        "link": "chainlink",
        "avalanche": "avalanche-2",
        "avax": "avalanche-2",
        "polkadot": "polkadot",
        "dot": "polkadot",
        "ripple": "ripple",
        "xrp": "ripple",
        "bitcoin cash": "bitcoin-cash",
        "bch": "bitcoin-cash",
    }

    def __init__(self):
        self.http = httpx.Client(timeout=10)
        self._cache = {}
        self._cache_ttl = 60  # 1 min

    def get_price(self, symbol: str) -> Optional[CryptoPrice]:
        """Get current crypto price."""
        symbol_lower = symbol.lower().strip()
        coin_id = self.COIN_IDS.get(symbol_lower)
        if not coin_id:
            return None

        cache_key = coin_id
        if cache_key in self._cache:
            cached, ts = self._cache[cache_key]
            if (datetime.now(timezone.utc) - ts).total_seconds() < self._cache_ttl:
                return cached

        try:
            url = f"https://api.coingecko.com/api/v3/simple/price"
            params = {
                "ids": coin_id,
                "vs_currencies": "usd",
                "include_24hr_change": "true",
                "include_24hr_vol": "true",
            }
            resp = self.http.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

            if coin_id not in data:
                return None

            coin_data = data[coin_id]
            price = CryptoPrice(
                symbol=symbol_lower,
                price_usd=coin_data.get("usd", 0),
                change_24h_pct=coin_data.get("usd_24h_change", 0) or 0,
                volume_24h=coin_data.get("usd_24h_vol", 0) or 0,
                timestamp=datetime.now(timezone.utc),
            )

            self._cache[cache_key] = (price, datetime.now(timezone.utc))
            return price

        except Exception as e:
            logger.debug(f"Crypto fetch error for {symbol}: {e}")
            return None

    def score_range_market(self, question: str, yes_price: float) -> Optional[dict]:
        """
        Score a crypto price range/above-below market.
        Works with or without explicit range in the question text.
        """
        import re

        # Extract crypto name
        crypto = None
        for name in self.COIN_IDS:
            if name in question.lower():
                crypto = name
                break
        if not crypto:
            return None

        price_data = self.get_price(crypto)
        if not price_data:
            return None

        current_price = price_data.price_usd
        change_pct = price_data.change_24h_pct

        # Estimate daily volatility from 24h change
        daily_vol_pct = max(abs(change_pct) * 0.8, 2.0)  # At least 2%
        expected_daily_range = current_price * (daily_vol_pct / 100)

        # Try to parse range from question
        range_match = re.search(r'(\d[\d,.]*)\s*-\s*(\d[\d,.]*)', question)
        is_above = "above" in question.lower() or "over" in question
        is_below = "below" in question.lower() or "under" in question

        if range_match:
            # Explicit range market
            low_range = float(range_match.group(1).replace(',', ''))
            high_range = float(range_match.group(2).replace(',', ''))
            range_width = high_range - low_range

            if low_range <= current_price <= high_range:
                if range_width > expected_daily_range * 2:
                    predicted_prob = 0.95
                elif range_width > expected_daily_range:
                    predicted_prob = 0.80
                else:
                    predicted_prob = 0.60
            else:
                distance = min(abs(current_price - low_range), abs(current_price - high_range))
                if distance > expected_daily_range * 3:
                    predicted_prob = 0.05
                elif distance > expected_daily_range:
                    predicted_prob = 0.15
                else:
                    predicted_prob = 0.35
        elif is_above:
            threshold_match = re.search(r'above\s*\$?(\d[\d,.]*)', question.lower())
            if threshold_match:
                threshold = float(threshold_match.group(1).replace(',', ''))
                if current_price > threshold * 1.05:
                    predicted_prob = 0.90
                elif current_price > threshold:
                    predicted_prob = 0.75
                else:
                    predicted_prob = 0.30
            else:
                predicted_prob = yes_price  # Can't parse threshold
        elif is_below:
            threshold_match = re.search(r'below\s*\$?(\d[\d,.]*)', question.lower())
            if threshold_match:
                threshold = float(threshold_match.group(1).replace(',', ''))
                if current_price < threshold * 0.95:
                    predicted_prob = 0.90
                elif current_price < threshold:
                    predicted_prob = 0.75
                else:
                    predicted_prob = 0.30
            else:
                predicted_prob = yes_price
        else:
            # Generic "price range" market without specific range — use volatility
            # If price is stable, likely to stay in range
            # If volatile, more likely to break out
            if abs(change_pct) < 2:
                predicted_prob = 0.85  # Stable = likely in range
            elif abs(change_pct) < 5:
                predicted_prob = 0.65
            else:
                predicted_prob = 0.40  # Very volatile = might break range

        # Adjust confidence by volume (higher volume = more data)
        confidence = 0.70
        if price_data.volume_24h > 1e9:
            confidence = 0.80
        elif price_data.volume_24h < 1e6:
            confidence = 0.55

        return {
            "predicted_prob": max(0.01, min(0.99, predicted_prob)),
            "confidence": confidence,
            "data": {
                "current_price": current_price,
                "change_24h": change_pct,
                "daily_volatility": daily_vol_pct,
            }
        }

    def close(self):
        self.http.close()


class ForexFeed:
    """
    Fetches forex rates from exchangerate.host (free, no key needed).
    Used for EUR/USD, USD/JPY prediction markets.
    """

    def __init__(self):
        self.http = httpx.Client(timeout=10)
        self._cache = {}
        self._cache_ttl = 300  # 5 min

    def get_rate(self, pair: str) -> Optional[ForexRate]:
        """Get current forex rate (e.g., 'EUR/USD')."""
        parts = pair.upper().replace('/', '').replace('-', '')
        if len(parts) != 6:
            return None

        base = parts[:3]
        quote = parts[3:]

        cache_key = pair
        if cache_key in self._cache:
            cached, ts = self._cache[cache_key]
            if (datetime.now(timezone.utc) - ts).total_seconds() < self._cache_ttl:
                return cached

        try:
            url = f"https://api.exchangerate.host/latest?base={base}&symbols={quote}"
            resp = self.http.get(url)
            resp.raise_for_status()
            data = resp.json()

            rates = data.get("rates", {})
            if quote not in rates:
                return None

            rate = ForexRate(
                pair=pair.upper(),
                rate=rates[quote],
                change_24h_pct=0,  # Free API doesn't include this
                timestamp=datetime.now(timezone.utc),
            )

            self._cache[cache_key] = (rate, datetime.now(timezone.utc))
            return rate

        except Exception as e:
            logger.debug(f"Forex fetch error for {pair}: {e}")
            return None

    def score_forex_market(self, question: str, yes_price: float) -> Optional[dict]:
        """Score a forex prediction market."""
        import re

        # Detect pair
        pair = None
        if "eur" in question.lower() and "usd" in question.lower():
            pair = "EUR/USD"
        elif "usd" in question.lower() and "jpy" in question.lower():
            pair = "USD/JPY"
        elif "gbp" in question.lower() and "usd" in question.lower():
            pair = "GBP/USD"

        if not pair:
            return None

        rate_data = self.get_rate(pair)
        if not rate_data:
            return None

        current_rate = rate_data.rate

        # Parse threshold from question
        threshold_match = re.search(r'(\d+\.\d+)', question)
        if not threshold_match:
            return None

        threshold = float(threshold_match.group(1))
        is_above = "above" in question.lower() or ">" in question

        # Forex moves ~0.5-1% daily typically
        daily_volatility_pct = 0.5
        daily_range = current_rate * (daily_volatility_pct / 100)

        if is_above:
            distance = threshold - current_rate
        else:
            distance = current_rate - threshold

        # Convert distance to probability
        if distance > daily_range * 3:
            predicted_prob = 0.95 if not is_above else 0.05
        elif distance > daily_range:
            predicted_prob = 0.80 if not is_above else 0.20
        elif distance > 0:
            predicted_prob = 0.65 if not is_above else 0.35
        elif distance > -daily_range:
            predicted_prob = 0.35 if not is_above else 0.65
        elif distance > -daily_range * 3:
            predicted_prob = 0.20 if not is_above else 0.80
        else:
            predicted_prob = 0.05 if not is_above else 0.95

        return {
            "predicted_prob": max(0.01, min(0.99, predicted_prob)),
            "confidence": 0.70,
            "data": {
                "current_rate": current_rate,
                "threshold": threshold,
                "daily_range": daily_range,
            }
        }

    def close(self):
        self.http.close()


class LiveFeedAggregator:
    """
    Routes market questions to the appropriate live data feed.
    Single entry point for the strategy engine.
    """

    def __init__(self):
        self.weather = WeatherFeed()
        self.crypto = CryptoFeed()
        self.forex = ForexFeed()

    def get_signal(self, question: str, yes_price: float,
                   category: str = "") -> Optional[dict]:
        """
        Get market-specific live data signal.

        Routes to the right feed based on market type:
        - Temperature → WeatherFeed
        - Crypto/Coin → CryptoFeed
        - EUR/USD, USD/JPY → ForexFeed
        """
        q = question.lower()

        # Temperature markets
        if any(w in q for w in ["temperature", "temp", "°", "degrees", "high", "low"]):
            return self.weather.score_temperature_market(question, yes_price)

        # Crypto markets
        if any(w in q for w in ["bitcoin", "btc", "ethereum", "eth", "shiba",
                                 "litecoin", "solana", "chainlink", "avalanche",
                                 "polkadot", "ripple", "crypto"]):
            return self.crypto.score_range_market(question, yes_price)

        # Forex markets
        if any(w in q for w in ["eur/usd", "usd/jpy", "gbp/usd", "exchange rate",
                                 "dollar", "yen", "euro"]):
            return self.forex.score_forex_market(question, yes_price)

        return None

    def close(self):
        self.weather.close()
        self.crypto.close()
        self.forex.close()
