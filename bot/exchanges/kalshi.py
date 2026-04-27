"""Kalshi exchange adapter."""

import os
import logging
import httpx
from typing import Optional
from datetime import datetime, timezone, timedelta

from kalshi_python_sync import Configuration, KalshiClient
from kalshi_python_sync.auth import KalshiAuth

from ..http_rate_limit import RateLimitProfile, RequestThrottle, call_with_retry, http_get_with_retry
from ..market_classification import apply_classification_metadata, classify_market_object
from .base import BaseExchange, Market, Order, Position, RestingOrder

logger = logging.getLogger(__name__)

KALSHI_DEMO = "https://demo-api.kalshi.co/trade-api/v2"
KALSHI_PROD = "https://api.elections.kalshi.com/trade-api/v2"



class KalshiExchange(BaseExchange):
    name = "kalshi"

    def __init__(self, api_key_id: str, private_key_path: str, demo: bool = False):
        self.api_key_id = api_key_id
        self.private_key_path = private_key_path
        self.demo = bool(demo)
        self.host = KALSHI_DEMO if demo else KALSHI_PROD
        self.client = None
        self._daily_series_tickers: list[str] = []
        self._allowed_market_groups: set[str] = {"weather", "sports"}
        self._account_tier = os.getenv("KALSHI_ACCOUNT_TIER", "basic")
        self._throttle = RequestThrottle(RateLimitProfile.from_account_tier(self._account_tier))

    def set_allowed_market_groups(self, groups: list[str] | set[str] | tuple[str, ...] | None):
        normalized = {str(group).strip().lower() for group in (groups or []) if str(group).strip()}
        self._allowed_market_groups = normalized or {"weather", "sports"}

    def _refresh_rate_limit_profile(self) -> None:
        env_reads = os.getenv("KALSHI_READS_PER_SECOND")
        env_writes = os.getenv("KALSHI_WRITES_PER_SECOND")
        if env_reads or env_writes:
            reads = float(env_reads or 20)
            writes = float(env_writes or 10)
            tier = os.getenv("KALSHI_ACCOUNT_TIER", "custom")
            self._account_tier = tier
            self._throttle.update_profile(RateLimitProfile.from_values(reads, writes, tier))
            return

        profile = self._fetch_account_limit_profile()
        if profile is not None:
            self._account_tier = profile.account_tier
            self._throttle.update_profile(profile)
            return

        tier = self._fetch_account_tier() or self._account_tier
        self._account_tier = tier
        self._throttle.update_profile(RateLimitProfile.from_account_tier(tier))

    def _fetch_account_tier(self) -> str | None:
        env_tier = os.getenv("KALSHI_ACCOUNT_TIER")
        if env_tier:
            return str(env_tier).strip().lower()
        return None

    def _fetch_account_limit_profile(self) -> Optional[RateLimitProfile]:
        if not self.client:
            return None
        try:
            auth_headers = self.client.kalshi_auth.create_auth_headers('GET', '/trade-api/v2/account/limits')
            url = f"{self.host}/account/limits"
            resp = http_get_with_retry(url, auth_headers, throttle=self._throttle, timeout=8)
            if not resp or resp.status_code != 200:
                return None
            data = resp.json() if hasattr(resp, 'json') else {}
            usage_tier = str(data.get('usage_tier') or self._account_tier or 'basic').strip().lower()
            read_limit = float(data.get('read_limit') or 0)
            write_limit = float(data.get('write_limit') or 0)
            if read_limit <= 0 or write_limit <= 0:
                return None
            logger.info("Kalshi API limits detected: tier=%s reads/s=%s writes/s=%s", usage_tier, read_limit, write_limit)
            return RateLimitProfile.from_values(read_limit, write_limit, usage_tier)
        except Exception as e:
            logger.debug(f"Could not fetch Kalshi account limits: {e}")
            return None

    def _normalize_market_group(self, market: Market) -> str | None:
        classification = classify_market_object(market)
        return classification.market_group if classification else None

    def _market_allowed(self, market: Market) -> bool:
        classification = apply_classification_metadata(market)
        if classification is None:
            return False
        return classification.market_group in self._allowed_market_groups

    def describe_runtime_identity(self) -> dict[str, object]:
        return {
            "exchange": self.name,
            "environment": "demo" if self.demo else "prod",
            "host": self.host,
            "api_key_id": self.api_key_id,
            "private_key_path": os.path.abspath(self.private_key_path),
            "account_tier": self._account_tier,
        }

    def connect(self) -> bool:
        try:
            with open(self.private_key_path, "r") as f:
                private_key_pem = f.read()

            config = Configuration(host=self.host)
            self.client = KalshiClient(config)
            self.client.kalshi_auth = KalshiAuth(self.api_key_id, private_key_pem)

            self._refresh_rate_limit_profile()

            # Test connection
            balance = call_with_retry(self.client.get_balance, throttle=self._throttle, kind="read")
            bal = (balance.balance or 0) / 100
            logger.info(f"Kalshi connected! Balance: ${bal:.2f}")

            # Discover daily series for quick-resolution markets
            self._discover_daily_series()

            return True

        except Exception as e:
            logger.error(f"Kalshi connection failed: {e}")
            return False

    def get_markets_direct(self, limit: int = 50, page_size: int = 200, max_pages: int = 10) -> list[Market]:
        try:
            markets = []
            cursor = None
            pages = 0
            auth_headers = self.client.kalshi_auth.create_auth_headers('GET', '/trade-api/v2/markets')
            logger.info('Kalshi direct market pull: limit=%s page_size=%s max_pages=%s groups=%s', limit, page_size, max_pages, sorted(self._allowed_market_groups))
            while len(markets) < limit and pages < max_pages:
                pages += 1
                params = f'?status=open&limit={max(1, min(page_size, 1000))}'
                if cursor:
                    params += f'&cursor={cursor}'
                url = f'{self.host}/markets{params}'
                resp = http_get_with_retry(url, auth_headers, throttle=self._throttle, timeout=10)
                if not resp or resp.status_code != 200:
                    logger.warning('Kalshi direct market pull stopped: page=%s status=%s', pages, getattr(resp, 'status_code', None))
                    break
                data = resp.json()
                raw = data.get('markets', [])
                if not raw:
                    break
                page_added = 0
                for m in raw:
                    yes_price = _dollars_from_raw(m, 'yes_ask')
                    no_price = _dollars_from_raw(m, 'no_ask')
                    if yes_price <= 0 or yes_price >= 1:
                        continue
                    close_time = _parse_dt_raw(m.get('close_time'))
                    market = Market(
                        id=m.get('ticker', ''),
                        exchange='kalshi',
                        question=m.get('title', ''),
                        yes_price=yes_price,
                        no_price=no_price,
                        volume=float(m.get('volume_fp', 0) or 0),
                        liquidity=_dollars_from_raw(m, 'liquidity'),
                        closes_at=close_time,
                        category=m.get('series_ticker', 'other'),
                        metadata={
                            'status': m.get('status', ''),
                            'source': 'direct_paginated',
                        },
                        yes_bid=_dollars_from_raw(m, 'yes_bid'),
                        no_bid=_dollars_from_raw(m, 'no_bid'),
                    )
                    if self._market_allowed(market):
                        markets.append(market)
                        page_added += 1
                        if len(markets) >= limit:
                            break
                logger.info('Kalshi direct market pull page=%s fetched=%s accepted=%s total=%s', pages, len(raw), page_added, len(markets))
                cursor = data.get('cursor')
                if not cursor:
                    break
            deduped = self._dedupe_and_filter_markets(markets, now=datetime.now(timezone.utc))
            logger.info('Kalshi direct market pull complete: pages=%s accepted=%s deduped=%s returning=%s', pages, len(markets), len(deduped), min(len(deduped), limit))
            return deduped[:limit]
        except Exception as e:
            logger.error(f'Error fetching direct markets: {e}')
            return []

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
                            resp = http_get_with_retry(url, auth_headers, throttle=self._throttle, timeout=5)
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
                                if self._market_allowed(market):
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
                    resp = http_get_with_retry(url, auth_headers, throttle=self._throttle, timeout=10)
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
                        if self._market_allowed(market):
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
                    events_resp = call_with_retry(lambda: self.client.get_events(**kwargs), throttle=self._throttle, kind="read")
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
                        mresp = call_with_retry(lambda: self.client.get_markets(event_ticker=event_ticker, limit=20), throttle=self._throttle, kind="read")
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

                        if self._market_allowed(market):
                            markets.append(market)

                        if len(markets) >= limit:
                            break

            deduped = self._dedupe_and_filter_markets(markets, now=now)
            logger.info(f"Fetched {len(deduped)} unique Kalshi markets (sorted by close time)")
            return deduped[:limit]

        except Exception as e:
            logger.error(f"Error fetching markets: {e}")
            return []

    def _dedupe_and_filter_markets(self, markets: list[Market], *, now: datetime) -> list[Market]:
        seen = set()
        deduped = []
        for m in markets:
            if m.id not in seen:
                seen.add(m.id)
                deduped.append(m)

        deduped.sort(key=lambda m: (
            (m.closes_at - now).total_seconds() if isinstance(m.closes_at, datetime) else float('inf')
        ))

        import re
        before = len(deduped)
        now_ts = datetime.now(timezone.utc)
        two_days_ago = now_ts - timedelta(days=2)
        fresh = []
        for m in deduped:
            if m.closes_at is None:
                logger.debug(f"Filtered market with no close time: {m.id}")
                continue
            if m.closes_at <= now_ts:
                logger.debug(f"Filtered closed market: {m.id} (closed {m.closes_at})")
                continue
            ticker_match = re.search(r'-(\d{6})-', m.id)
            if ticker_match:
                try:
                    yymmdd = ticker_match.group(1)
                    yy, mm, dd = int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6])
                    market_year = 2000 + yy if yy >= 90 else 2000 + yy
                    market_date = datetime(market_year, mm, dd, tzinfo=timezone.utc)
                    if market_date < two_days_ago:
                        logger.debug(f"Filtered stale ticker: {m.id} (ticker date {market_date.date()})")
                        continue
                except (ValueError, OverflowError):
                    pass
            fresh.append(m)
        deduped = fresh
        if before != len(deduped):
            logger.info(f"Filtered {before - len(deduped)} stale/closed markets (already resolved)")
        return deduped

    def get_market(self, market_id: str) -> Optional[Market]:
        try:
            resp = call_with_retry(lambda: self.client.get_market(ticker=market_id), throttle=self._throttle, kind="read")
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
                resp = http_get_with_retry(url, auth_headers, throttle=self._throttle, timeout=15)
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
            resp = http_get_with_retry(url, auth_headers, throttle=self._throttle, timeout=5)
            if not resp:
                return None
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
            resp = http_get_with_retry(url, auth_headers, throttle=self._throttle, timeout=8)
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
            resp = http_get_with_retry(url, auth_headers, throttle=self._throttle, timeout=8)
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

            resp = call_with_retry(lambda: self.client.create_order(**kwargs), throttle=self._throttle, kind="write")
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
            call_with_retry(lambda: self.client.cancel_order(order_id=order_id), throttle=self._throttle, kind="write")
            return True
        except Exception as e:
            logger.error(f"Cancel failed: {e}")
            return False

    def get_positions(self) -> list[Position]:
        try:
            resp = call_with_retry(self.client.get_positions, throttle=self._throttle, kind="read")
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
                    metadata={
                        "raw_position": pos,
                    },
                ))
            return result
        except Exception as e:
            logger.error(f"Error getting positions: {e}")
            return []

    def get_resting_orders(self) -> list[RestingOrder]:
        if not self.client:
            return []

        raw_orders = []
        try:
            resp = call_with_retry(lambda: self.client.get_orders(status="open", limit=200), throttle=self._throttle, kind="read")
            raw_orders = getattr(resp, "orders", []) or []
        except Exception as sdk_error:
            logger.debug(f"SDK open-orders fetch failed, trying raw API: {sdk_error}")
            try:
                auth_headers = self.client.kalshi_auth.create_auth_headers(
                    'GET', '/trade-api/v2/portfolio/orders'
                )
                url = f"{self.host}/portfolio/orders?status=open&limit=200"
                resp = http_get_with_retry(url, auth_headers, throttle=self._throttle, timeout=10)
                if resp and resp.status_code == 200:
                    raw_orders = resp.json().get("orders", []) or []
            except Exception as raw_error:
                logger.error(f"Error getting resting orders: {raw_error}")
                return []

        normalized = []
        for order in raw_orders:
            normalized_order = self._normalize_resting_order(order)
            if normalized_order is not None:
                normalized.append(normalized_order)
        return normalized

    def get_balance(self) -> float:
        try:
            resp = call_with_retry(self.client.get_balance, throttle=self._throttle, kind="read")
            return (getattr(resp, 'balance', 0) or 0) / 100
        except Exception as e:
            logger.error(f"Error getting balance: {e}")
            return 0

    def _normalize_resting_order(self, order) -> Optional[RestingOrder]:
        if isinstance(order, dict):
            order_id = order.get("order_id") or order.get("id") or ""
            market_id = order.get("ticker") or order.get("market_ticker") or ""
            side = str(order.get("side") or "YES").upper()
            status = str(order.get("status") or "open")
            requested = float(order.get("count") or order.get("requested_size") or 0)
            filled = float(order.get("filled_count") or order.get("filled_size") or 0)
            remaining = float(order.get("remaining_count") or order.get("remaining_size") or max(0.0, requested - filled))
            price_cents = order.get("yes_price") if side == "YES" else order.get("no_price")
            if price_cents is None:
                price_cents = order.get("price")
            price = float(price_cents or 0) / 100 if float(price_cents or 0) > 1 else float(price_cents or 0)
            created_at = _parse_dt_raw(order.get("created_time")) or datetime.now(timezone.utc)
            updated_at = _parse_dt_raw(order.get("updated_time"))
            question = order.get("title") or order.get("question") or market_id
        else:
            order_id = getattr(order, "order_id", "") or getattr(order, "id", "")
            market_id = getattr(order, "ticker", "") or getattr(order, "market_ticker", "")
            side = str(getattr(order, "side", "YES") or "YES").upper()
            status = str(getattr(order, "status", "open") or "open")
            requested = float(getattr(order, "count", 0) or getattr(order, "requested_size", 0) or 0)
            filled = float(getattr(order, "filled_count", 0) or getattr(order, "filled_size", 0) or 0)
            remaining = float(getattr(order, "remaining_count", 0) or getattr(order, "remaining_size", max(0.0, requested - filled)) or 0)
            price_cents = getattr(order, "yes_price", None) if side == "YES" else getattr(order, "no_price", None)
            if price_cents is None:
                price_cents = getattr(order, "price", None)
            price = float(price_cents or 0) / 100 if float(price_cents or 0) > 1 else float(price_cents or 0)
            created_at = _parse_dt(getattr(order, "created_time", None)) or datetime.now(timezone.utc)
            updated_at = _parse_dt(getattr(order, "updated_time", None))
            question = getattr(order, "title", "") or getattr(order, "question", "") or market_id

        if not order_id or not market_id or requested <= 0:
            return None

        direction = "YES" if side == "YES" else "NO"
        return RestingOrder(
            order_id=order_id,
            market_id=market_id,
            exchange="kalshi",
            side=direction,
            requested_size=requested,
            filled_size=filled,
            remaining_size=remaining,
            price=price,
            status=status,
            created_at=created_at,
            updated_at=updated_at,
            question=question,
            metadata={"raw_status": status},
        )

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
