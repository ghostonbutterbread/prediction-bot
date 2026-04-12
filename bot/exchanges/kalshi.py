"""Kalshi exchange adapter."""

import os
import logging
from typing import Optional
from datetime import datetime, timezone, timedelta

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
                    except Exception as e:
                        logger.debug(f"SDK event market fetch failed for {event_ticker}: {e}")
                        raw_markets = self._fetch_event_markets_raw(event_ticker, limit=20)
                        if not raw_markets:
                            continue

                    for m in raw_markets:
                        if isinstance(m, dict):
                            market = _market_from_raw(
                                m,
                                category=getattr(event, 'category', 'other'),
                                metadata={
                                    "event_ticker": event_ticker,
                                    "status": m.get('status', ''),
                                    "source": "events_raw",
                                },
                            )
                            yes_price = market.yes_price if market else 0.0
                            no_price = market.no_price if market else 0.0
                        else:
                            yes_price = _dollars(m, 'yes_ask_dollars')
                            no_price = _dollars(m, 'no_ask_dollars')
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

                        if yes_price <= 0 or yes_price >= 1:
                            continue

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

            # === Market freshness filter — reject already-closed markets ===
            # This prevents ancient/historical markets (e.g. 2017 Shiba Inu) from
            # flooding the scanner and filling position slots.
            # Rejects: closed markets (closes_at in past) AND markets with no close time
            # (missing close time is a red flag for stale/historical markets).
            import re
            before = len(deduped)
            now_ts = datetime.now(timezone.utc)
            two_days_ago = now_ts - timedelta(days=2)
            fresh = []
            for m in deduped:
                # Reject if closes_at is None (malformed/historical market)
                if m.closes_at is None:
                    logger.debug(f"Filtered market with no close time: {m.id}")
                    continue
                # Reject if already closed
                if m.closes_at <= now_ts:
                    logger.debug(f"Filtered closed market: {m.id} (closed {m.closes_at})")
                    continue
                # Reject if ticker date is more than 2 days old (e.g. MAR2217 = March 22, 2017)
                # Ticker pattern: KXSOMETHING-YYMMDD-...  or KXSOMETHING-YYMMDD
                ticker_match = re.search(r'-(\d{6})-', m.id)
                if ticker_match:
                    try:
                        yymmdd = ticker_match.group(1)
                        yy, mm, dd = int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6])
                        # 26 = 2026, 17 = 2017 (Kalshi uses 2-digit year)
                        market_year = 2000 + yy if yy >= 90 else 2000 + yy
                        market_date = datetime(market_year, mm, dd, tzinfo=timezone.utc)
                        if market_date < two_days_ago:
                            logger.debug(f"Filtered stale ticker: {m.id} (ticker date {market_date.date()})")
                            continue
                    except (ValueError, OverflowError):
                        pass  # Can't parse date — let it through
                fresh.append(m)
            deduped = fresh
            if before != len(deduped):
                logger.info(f"Filtered {before - len(deduped)} stale/closed markets (already resolved)")

            logger.info(f"Fetched {len(deduped)} unique Kalshi markets (sorted by close time)")
            return deduped[:limit]

        except Exception as e:
            logger.error(f"Error fetching markets: {e}")
            return []

    def get_market(self, market_id: str) -> Optional[Market]:
        try:
            resp = self.client.get_market(ticker=market_id)
            m = getattr(resp, 'market', None)
            if m:
                return Market(
                    id=getattr(m, 'ticker', '') or market_id,
                    exchange="kalshi",
                    question=getattr(m, 'title', '') or getattr(m, 'subtitle', '') or market_id,
                    yes_price=_dollars(m, 'yes_ask_dollars'),
                    no_price=_dollars(m, 'no_ask_dollars'),
                    volume=_fp(m, 'volume_fp'),
                    liquidity=_dollars(m, 'liquidity_dollars'),
                    closes_at=_parse_dt(getattr(m, 'close_time', None)),
                    category=getattr(m, 'series_ticker', None) or getattr(m, 'market_type', 'binary'),
                    metadata={
                        "status": getattr(m, 'status', ''),
                        "result": getattr(m, 'result', None),
                    },
                    close_price=(_dollars(m, 'close_price_dollars') if getattr(m, 'close_price_dollars', None) is not None else None),
                    yes_bid=_dollars(m, 'yes_bid_dollars'),
                    no_bid=_dollars(m, 'no_bid_dollars'),
                )
        except Exception as e:
            # The SDK's pydantic model may throw ValidationError if the API returns
            # null for required string fields (e.g. subtitle=null on stale/historical markets).
            # Catch it here so the resolver doesn't crash on old positions.
            logger.debug(f"get_market {market_id} failed (SDK error): {e}")
        raw_market = self._fetch_market_raw(market_id)
        if not raw_market:
            return None
        return _market_from_raw(raw_market, market_id=market_id)


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

    def _fetch_market_raw(self, market_id: str) -> Optional[dict]:
        try:
            auth_headers = self.client.kalshi_auth.create_auth_headers(
                'GET', f'/trade-api/v2/markets/{market_id}'
            )
            url = f"{self.host}/markets/{market_id}"
            resp = _http_get_with_retry(url, auth_headers, timeout=8)
            if not resp or resp.status_code != 200:
                return None
            data = resp.json()
            market = data.get("market") if isinstance(data, dict) and isinstance(data.get("market"), dict) else data
            return market if isinstance(market, dict) else None
        except Exception as e:
            logger.debug(f"Raw market fetch failed for {market_id}: {e}")
            return None

    def _fetch_event_markets_raw(self, event_ticker: str, limit: int = 20) -> list[dict]:
        try:
            auth_headers = self.client.kalshi_auth.create_auth_headers(
                'GET', '/trade-api/v2/markets'
            )
            url = f"{self.host}/markets?event_ticker={event_ticker}&limit={limit}"
            resp = _http_get_with_retry(url, auth_headers, timeout=8)
            if not resp or resp.status_code != 200:
                return []
            data = resp.json()
            markets = data.get("markets", [])
            return markets if isinstance(markets, list) else []
        except Exception as e:
            logger.debug(f"Raw event market fetch failed for {event_ticker}: {e}")
            return []

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


def _market_from_raw(
    data: dict,
    *,
    market_id: str = "",
    category: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> Optional[Market]:
    if not isinstance(data, dict):
        return None

    ticker = data.get("ticker") or market_id
    if not ticker:
        return None

    question = data.get("title") or data.get("subtitle") or ticker
    market_metadata = {
        "status": data.get("status", ""),
        "result": data.get("result"),
        "outcome": data.get("outcome"),
        "subtitle": data.get("subtitle"),
    }
    if metadata:
        market_metadata.update(metadata)

    close_price = data.get("close_price_dollars")
    if close_price is None:
        close_price = data.get("close_price")

    return Market(
        id=ticker,
        exchange="kalshi",
        question=question,
        yes_price=_dollars_from_raw(data, 'yes_ask'),
        no_price=_dollars_from_raw(data, 'no_ask'),
        volume=float(data.get('volume_fp', 0) or 0),
        liquidity=_dollars_from_raw(data, 'liquidity'),
        closes_at=_parse_dt_raw(data.get('close_time')),
        category=category or data.get('series_ticker') or data.get('market_type') or 'binary',
        metadata=market_metadata,
        close_price=_dollars_from_raw({"close_price": close_price}, 'close_price') if close_price is not None else None,
        yes_bid=_dollars_from_raw(data, 'yes_bid'),
        no_bid=_dollars_from_raw(data, 'no_bid'),
    )
