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


class KalshiExchange(BaseExchange):
    name = "kalshi"

    def __init__(self, api_key_id: str, private_key_path: str, demo: bool = False):
        self.api_key_id = api_key_id
        self.private_key_path = private_key_path
        self.host = KALSHI_DEMO if demo else KALSHI_PROD
        self.client = None

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
            return True

        except Exception as e:
            logger.error(f"Kalshi connection failed: {e}")
            return False

    def get_markets(self, limit: int = 50, category: str = None) -> list[Market]:
        try:
            # Get events first, then fetch markets per event (gets better binary markets)
            events_resp = self.client.get_events(limit=20, status="open")
            events = getattr(events_resp, 'events', []) or []

            markets = []
            for event in events:
                if len(markets) >= limit:
                    break

                event_ticker = getattr(event, 'event_ticker', '')
                if not event_ticker:
                    continue

                try:
                    mresp = self.client.get_markets(event_ticker=event_ticker, limit=5)
                    raw_markets = getattr(mresp, 'markets', []) or []
                except Exception:
                    continue

                for m in raw_markets:
                    yes_price = _dollars(m, 'yes_ask_dollars')
                    no_price = _dollars(m, 'no_ask_dollars')

                    # Skip markets with no valid prices
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
                        }
                    )
                    markets.append(market)

                    if len(markets) >= limit:
                        break

            logger.info(f"Fetched {len(markets)} Kalshi markets")
            return markets[:limit]

        except Exception as e:
            logger.error(f"Error fetching markets: {e}")
            return []

    def get_market(self, market_id: str) -> Optional[Market]:
        try:
            resp = self.client.get_market(market_ticker=market_id)
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
                metadata={"status": getattr(m, 'status', '')}
            )
        except Exception as e:
            logger.error(f"Error fetching market {market_id}: {e}")
            return None

    def get_order_book(self, market_id: str) -> Optional[dict]:
        try:
            resp = self.client.get_market_order_book(market_ticker=market_id, depth=10)
            book = getattr(resp, 'order_book', None)
            if not book:
                return None

            yes_bids = getattr(book, 'yes', []) or []
            no_bids = getattr(book, 'no', []) or []

            best_yes = yes_bids[0].price / 100 if yes_bids else 0
            best_no = no_bids[0].price / 100 if no_bids else 0

            return {
                "yes_bids": [(b.price / 100, b.quantity) for b in yes_bids[:10]],
                "no_bids": [(b.price / 100, b.quantity) for b in no_bids[:10]],
                "best_yes": best_yes,
                "best_no": best_no,
                "mid_yes": best_yes,
                "spread": abs(best_yes - (1 - best_no)) if best_no else 0,
            }
        except Exception as e:
            logger.debug(f"Error getting order book for {market_id}: {e}")
            return None

    def place_order(self, market_id: str, side: str, price: float,
                    size: float) -> Optional[Order]:
        try:
            price_cents = int(price * 100)
            action = "buy"
            count = max(1, int(size))

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
            logger.error(f"Order failed: {e}")
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
