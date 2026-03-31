"""Kalshi exchange adapter."""

import os
import logging
from typing import Optional
from datetime import datetime, timezone

from kalshi_python_sync import Configuration, KalshiClient
from kalshi_python_sync.auth import KalshiAuth

from .base import BaseExchange, Market, Order, Position

logger = logging.getLogger(__name__)

KALSHI_DEMO = "https://demo-api.kalshi.co/trade-api/v2"
KALSHI_PROD = "https://api.elections.kalshi.com/trade-api/v2"


def _http_get_with_retry(url: str, headers: dict, timeout: int = 10, max_retries: int = 3) -> Optional[httpx.Response]:
    """Fetch a URL with exponential backoff on rate limiting."""
    import time
    import httpx
    for attempt in range(max_retries):
        try:
            resp = httpx.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 429:
                wait = 2 ** attempt
                logger.warning(f"Rate limited (429). Waiting {wait}s before retry {attempt+1}/{max_retries}")
                time.sleep(wait)
                continue
            return resp
        except httpx.RequestError as e:
            if attempt == max_retries - 1:
                logger.error(f"HTTP request failed after {max_retries} attempts: {e}")
                return None
            time.sleep(2 ** attempt)
    return None


class KalshiExchange(BaseExchange):
    name = "kalshi"

    def __init__(self, api_key_id: str, private_key_path: str, demo: bool = False):
        self.api_key_id = api_key_id
        self.private_key_path = private_key_path
        self.host = KALSHI_DEMO if demo else KALSHI_PROD
        self.client = None
        self._daily_series_tickers: list[str] = []

    def connect(self) -> bool:
        try:
            with open(self.private_key_path, "r") as f:
                private_key_pem = f.read()

            config = Configuration(host=self.host)
            self.client = KalshiClient(config)
            self.client.kalshi_auth = KalshiAuth(self.api_key_id, private_key_pem)

            # Test connection
            balance = self.client.get_balance()
            bal = (balance.balance or 0) / 100
            logger.info(f"Kalshi connected! Balance: ${bal:.2f}")

            # Discover daily series for quick-resolution markets
            self._discover_daily_series()

            return True

        except Exception as e:
            logger.error(f"Kalshi connection failed: {e}")
            return False

    def get_markets(self, limit: int = 50, category: str = None) -> list[Market]:
        try:
            markets = []
            now = datetime.now(timezone.utc)

            # === Pass 0: Daily series markets (BTC, ETH, S&P 500, etc.) ===
            # These resolve daily — exactly what we need for quick paper trading cycles
            if self._daily_series_tickers:
                try:
                    import httpx
                    auth_headers = self.client.kalshi_auth.create_auth_headers(
                        'GET', '/trade-api/v2/markets'
                    )
                    for series_ticker in self._daily_series_tickers:
                        if len(markets) >= limit:
                            break
                        try:
                            url = f'{self.host}/markets?status=open&limit=5&series_ticker={series_ticker}'
                            resp = httpx.get(url, headers=auth_headers, timeout=5)
                            if resp.status_code != 200:
                                continue
                            data = resp.json()
                            for m in data.get('markets', []):
                                if len(markets) >= limit:
                                    break
                                yes_price = _dollars_from_raw(m, 'yes_ask')
                                no_price = _dollars_from_raw(m, 'no_ask')
                                if yes_price <= 0 or yes_price >= 1:
                                    continue
                                close_time = _parse_dt_raw(m.get('close_time'))
                                market = Market(
                                    id=m.get('ticker', ''),
                                    exchange="kalshi",
                                    question=m.get('title', ''),
                                    yes_price=yes_price,
                                    no_price=no_price,
                                    volume=float(m.get('volume_fp', 0) or 0),
                                    liquidity=_dollars_from_raw(m, 'liquidity'),
                                    closes_at=close_time,
                                    category=series_ticker,
                                    metadata={
                                        "status": m.get('status', ''),
                                        "source": "daily_series",
                                        "series": series_ticker,
                                    },
                                    yes_bid=_dollars_from_raw(m, 'yes_bid'),
                                    no_bid=_dollars_from_raw(m, 'no_bid'),
                                )
                                markets.append(market)
                        except Exception:
                            continue
                    logger.info(f"Daily series pass: {len(markets)} markets from {len(self._daily_series_tickers)} series")
                except Exception as e:
                    logger.debug(f"Daily series fetch failed: {e}")

            # === Pass 1: Direct /markets endpoint (catches daily markets like BTC price) ===
            try:
                import httpx
                auth_headers = self.client.kalshi_auth.create_auth_headers(
                    'GET', '/trade-api/v2/markets'
                )
                cursor = None
                direct_count = 0
                while len(markets) < limit and direct_count < 200:
                    params = f'?status=open&limit=100'
                    if cursor:
                        params += f'&cursor={cursor}'
                    url = f'{self.host}/markets{params}'
                    resp = _http_get_with_retry(url, auth_headers, timeout=10)
                    if not resp or resp.status_code != 200:
                        break
                    data = resp.json()
                    raw = data.get('markets', [])
                    if not raw:
                        break

                    for m in raw:
                        yes_price = _dollars_from_raw(m, 'yes_ask')
                        no_price = _dollars_from_raw(m, 'no_ask')

                        if yes_price <= 0 or yes_price >= 1:
                            continue

                        close_time = _parse_dt_raw(m.get('close_time'))

                        market = Market(
                            id=m.get('ticker', ''),
                            exchange="kalshi",
                            question=m.get('title', ''),
                            yes_price=yes_price,
                            no_price=no_price,
                            volume=float(m.get('volume_fp', 0) or 0),
                            liquidity=_dollars_from_raw(m, 'liquidity'),
                            closes_at=close_time,
                            category=m.get('series_ticker', 'other'),
                            metadata={
                                "status": m.get('status', ''),
                                "source": "direct",
                            },
                            yes_bid=_dollars_from_raw(m, 'yes_bid'),
                            no_bid=_dollars_from_raw(m, 'no_bid'),
                        )
                        markets.append(market)
                        direct_count += 1

                    cursor = data.get('cursor')
                    if not cursor:
                        break

                logger.info(f"Direct markets pass: {direct_count} markets")
            except Exception as e:
                logger.debug(f"Direct markets fetch failed: {e}")

            # === Pass 2: Events → markets (catches everything else) ===
            if len(markets) < limit:
                all_events = []
                cursor = None
                while len(all_events) < 200:
                    kwargs = {"limit": 50, "status": "open"}
                    if cursor:
                        kwargs["cursor"] = cursor
                    events_resp = self.client.get_events(**kwargs)
                    events = getattr(events_resp, 'events', []) or []
                    if not events:
                        break
                    all_events.extend(events)
                    cursor = getattr(events_resp, 'cursor', None)
                    if not cursor:
                        break

                logger.info(f"Fetched {len(all_events)} events from Kalshi")

                for event in all_events:
                    if len(markets) >= limit:
                        break

                    event_ticker = getattr(event, 'event_ticker', '')
                    if not event_ticker:
                        continue

                    try:
                        mresp = self.client.get_markets(event_ticker=event_ticker, limit=20)
                        raw_markets = getattr(mresp, 'markets', []) or []
                    except Exception:
                        continue

                    for m in raw_markets:
                        yes_price = _dollars(m, 'yes_ask_dollars')
                        no_price = _dollars(m, 'no_ask_dollars')

                        if yes_price <= 0 or yes_price >= 1:
                            continue

                        market = Market(
                            id=getattr(m, 'ticker', ''),
                            exchange="kalshi",
                            question=getattr(m, 'title', ''),
                            yes_price=yes_price,
                            no_price=no_price,
                            volume=_fp(m, 'volume_fp'),
                            liquidity=_dollars(m, 'liquidity_dollars'),
                            closes_at=_parse_dt(getattr(m, 'close_time', None)),
                            category=getattr(event, 'category', 'other'),
                            metadata={
                                "event_ticker": event_ticker,
                                "status": getattr(m, 'status', ''),
                                "source": "events",
                            }
                        )
                        markets.append(market)

                        if len(markets) >= limit:
                            break

            # Dedup by market ID
            seen = set()
            deduped = []
            for m in markets:
                if m.id not in seen:
                    seen.add(m.id)
                    deduped.append(m)

            # Sort: prioritize markets closing sooner
            deduped.sort(key=lambda m: (
                (m.closes_at - now).total_seconds() if isinstance(m.closes_at, datetime) else float('inf')
            ))

            logger.info(f"Fetched {len(deduped)} unique Kalshi markets (sorted by close time)")
            return deduped[:limit]

        except Exception as e:
            logger.error(f"Error fetching markets: {e}")
            return []

    def get_market(self, market_id: str) -> Optional[Market]:
        try:
            resp = self.client.get_market(ticker=market_id)
            m = getattr(resp, 'market', None)
            if not m:
                return None

            return Market(
                id=getattr(m, 'ticker', ''),
                exchange="kalshi",
                question=getattr(m, 'title', ''),
                yes_price=_dollars(m, 'yes_ask_dollars'),
                no_price=_dollars(m, 'no_ask_dollars'),
                volume=_fp(m, 'volume_fp'),
                liquidity=_dollars(m, 'liquidity_dollars'),
                closes_at=_parse_dt(getattr(m, 'close_time', None)),
                category=getattr(m, 'market_type', 'binary'),
                metadata={"status": getattr(m, 'status', '')},
                close_price=(_dollars(m, 'close_price_dollars') if getattr(m, 'close_price_dollars', None) is not None else None),
            )
        except Exception as e:
            logger.error(f"Error fetching market {market_id}: {e}")
            return None

    def _discover_daily_series(self):
        """Find all daily-frequency series tickers on Kalshi."""
        try:
            import httpx
            auth_headers = self.client.kalshi_auth.create_auth_headers(
                'GET', '/trade-api/v2/series'
            )
            cursor = None
            self._daily_series_tickers = []
            while True:
                params = '?limit=200'
                if cursor:
                    params += f'&cursor={cursor}'
                url = f'{self.host}/series{params}'
                resp = _http_get_with_retry(url, auth_headers, timeout=15)
                if not resp or resp.status_code != 200:
                    break
                data = resp.json()
                series_list = data.get('series', [])
                if not series_list:
                    break
                for s in series_list:
                    if s.get('frequency') == 'daily':
                        # Daily series use 'ticker', not 'series_ticker'
                        ticker = s.get('ticker', '')
                        if ticker:
                            self._daily_series_tickers.append(ticker)
                cursor = data.get('cursor')
                if not cursor:
                    break
            logger.info(f"Discovered {len(self._daily_series_tickers)} daily series")
        except Exception as e:
            logger.warning(f"Could not discover daily series: {e}")
            self._daily_series_tickers = []

    def get_order_book(self, market_id: str) -> Optional[dict]:
        """Get order book — uses market-level bid/ask from cached data."""
        # The order book is already embedded in market data (yes_bid_dollars, etc.)
        # This method returns None to signal "use market-level data in the signal engine"
        return None

    def get_market_bid_ask(self, market_id: str) -> Optional[dict]:
        """Get bid/ask for a specific market by fetching it directly."""
        try:
            import httpx
            auth_headers = self.client.kalshi_auth.create_auth_headers(
                'GET', f'/trade-api/v2/markets/{market_id}'
            )
            url = f"{self.host}/markets/{market_id}"
            resp = httpx.get(url, headers=auth_headers, timeout=5)
            resp.raise_for_status()
            data = resp.json()

            yes_bid = float(data.get("yes_bid", 0)) / 100 if data.get("yes_bid") else 0
            yes_ask = float(data.get("yes_ask", 0)) / 100 if data.get("yes_ask") else 0
            no_bid = float(data.get("no_bid", 0)) / 100 if data.get("no_bid") else 0
            no_ask = float(data.get("no_ask", 0)) / 100 if data.get("no_ask") else 0

            mid_yes = (yes_bid + yes_ask) / 2 if yes_ask > 0 else 0
            spread = yes_ask - yes_bid if yes_ask > 0 and yes_bid > 0 else 0

            return {
                "best_yes_ask": yes_ask,
                "best_yes_bid": yes_bid,
                "best_no_ask": no_ask,
                "best_no_bid": no_bid,
                "mid_yes": mid_yes,
                "spread": spread,
                "spread_pct": (spread / mid_yes * 100) if mid_yes > 0 else 0,
            }
        except Exception as e:
            logger.debug(f"Error getting bid/ask for {market_id}: {e}")
            return None

    def place_order(self, market_id: str, side: str, price: float,
                    size: float) -> Optional[Order]:
        try:
            price_cents = int(price * 100)
            action = "buy"
            # size is in dollars; each contract costs `price` dollars → convert to contract count
            count = max(1, int(size / price)) if price > 0 else 1

            kwargs = {
                "ticker": market_id,
                "client_order_id": f"bot_{datetime.now().timestamp()}",
                "action": action,
                "count": count,
                "type": "limit",
            }

            if side == "YES":
                kwargs["side"] = "yes"
                kwargs["yes_price"] = price_cents
            else:
                kwargs["side"] = "no"
                kwargs["no_price"] = price_cents

            resp = self.client.create_order(**kwargs)
            order_data = getattr(resp, 'order', None)
            order_id = getattr(order_data, 'order_id', '') if order_data else ''

            order = Order(
                id=order_id,
                exchange="kalshi",
                market_id=market_id,
                side=side,
                price=price,
                size=count,
                status="submitted",
                created_at=datetime.now(timezone.utc),
            )
            logger.info(f"Kalshi order: {side} {count} @ ${price:.2f} on {market_id}")
            return order

        except Exception as e:
            # Log the actual error response for debugging
            err_detail = str(e)
            if hasattr(e, 'response') and e.response is not None:
                try:
                    err_body = e.response.json()
                    err_detail = f"HTTP {e.response.status_code} - {err_body}"
                except Exception:
                    err_detail = f"HTTP {e.response.status_code} - {e.response.text[:200]}"

            # 409 Conflict means an order already exists on this market — treat as success
            if hasattr(e, 'response') and e.response is not None and e.response.status_code == 409:
                logger.warning(f"Order already exists on {market_id} — skipping duplicate")
                return Order(
                    id="existing",
                    exchange="kalshi",
                    market_id=market_id,
                    side=side,
                    price=price,
                    size=count,
                    status="existing",
                    created_at=datetime.now(timezone.utc),
                )

            logger.error(f"Order failed on {market_id}: {err_detail}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        try:
            self.client.cancel_order(order_id=order_id)
            return True
        except Exception as e:
            logger.error(f"Cancel failed: {e}")
            return False

    def get_positions(self) -> list[Position]:
        try:
            resp = self.client.get_positions()
            positions = getattr(resp, 'positions', []) or []
            result = []
            for p in positions:
                pos = getattr(p, 'position', 0) or 0
                result.append(Position(
                    market_id=getattr(p, 'ticker', ''),
                    exchange="kalshi",
                    question=getattr(p, 'title', ''),
                    side="YES" if pos > 0 else "NO",
                    entry_price=0,
                    size=abs(pos),
                    current_price=0,
                    pnl=(getattr(p, 'realized_pnl', 0) or 0) / 100,
                    opened_at=datetime.now(timezone.utc),
                ))
            return result
        except Exception as e:
            logger.error(f"Error getting positions: {e}")
            return []

    def get_balance(self) -> float:
        try:
            resp = self.client.get_balance()
            return (getattr(resp, 'balance', 0) or 0) / 100
        except Exception as e:
            logger.error(f"Error getting balance: {e}")
            return 0

    def close(self):
        pass


def _dollars(obj, attr: str) -> float:
    """Extract dollar value from SDK object."""
    val = getattr(obj, attr, None)
    return round(float(val), 4) if val is not None else 0.0


def _fp(obj, attr: str) -> float:
    """Extract fixed-point value from SDK object."""
    val = getattr(obj, attr, None)
    return round(float(val), 2) if val is not None else 0.0


def _parse_dt(dt) -> Optional[datetime]:
    """Parse datetime from SDK."""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt
    try:
        return datetime.fromtimestamp(int(dt), tz=timezone.utc)
    except (ValueError, TypeError):
        return None


def _dollars_from_raw(data: dict, key: str) -> float:
    """Extract dollar value from raw API JSON (already in dollars)."""
    # Try with _dollars suffix first (raw API format), then without
    val = data.get(f'{key}_dollars') or data.get(key)
    if val is None:
        return 0.0
    try:
        return round(float(val), 4)
    except (ValueError, TypeError):
        return 0.0


def _parse_dt_raw(dt_str) -> Optional[datetime]:
    """Parse datetime from raw API JSON string."""
    if dt_str is None:
        return None
    if isinstance(dt_str, datetime):
        return dt_str
    try:
        return datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None
