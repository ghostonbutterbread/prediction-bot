"""Kalshi exchange adapter."""

import os
import logging
from typing import Optional
from datetime import datetime, timezone

from kalshi_python_sync import Configuration, KalshiClient

from .base import BaseExchange, Market, Order, Position

logger = logging.getLogger(__name__)

KALSHI_DEMO = "https://demo-api.kalshi.co/trade-api/v2"
KALSHI_PROD = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiExchange(BaseExchange):
    name = "kalshi"

    def __init__(self, api_key_id: str, private_key_path: str, demo: bool = True):
        self.api_key_id = api_key_id
        self.private_key_path = private_key_path
        self.host = KALSHI_DEMO if demo else KALSHI_PROD
        self.client = None

    def connect(self) -> bool:
        try:
            with open(self.private_key_path, "r") as f:
                private_key = f.read()

            config = Configuration(host=self.host)
            config.api_key_id = self.api_key_id
            config.private_key_pem = private_key

            self.client = KalshiClient(config)

            # Test connection
            balance = self.client.get_balance()
            bal = balance.balance / 100 if hasattr(balance, 'balance') else 0
            logger.info(f"Kalshi connected! Balance: ${bal:.2f}")
            return True

        except Exception as e:
            logger.error(f"Kalshi connection failed: {e}")
            return False

    def get_markets(self, limit: int = 50, category: str = None) -> list[Market]:
        try:
            params = {"status": "open", "limit": limit}
            if category:
                params["category"] = category

            resp = self.client.get_events(limit=limit)
            events = resp.events if hasattr(resp, 'events') else []

            markets = []
            for event in events:
                if not hasattr(event, 'markets'):
                    continue

                for m in event.markets:
                    yes_price = getattr(m, 'yes_price', 0) / 100 if hasattr(m, 'yes_price') else 0
                    no_price = getattr(m, 'no_price', 0) / 100 if hasattr(m, 'no_price') else 0
                    volume = getattr(m, 'volume', 0) or 0

                    market = Market(
                        id=getattr(m, 'ticker', ''),
                        exchange="kalshi",
                        question=getattr(m, 'title', ''),
                        yes_price=yes_price,
                        no_price=no_price,
                        volume=volume,
                        liquidity=getattr(m, 'liquidity', 0) or 0,
                        closes_at=self._parse_time(getattr(m, 'close_time', None)),
                        category=getattr(event, 'category', 'other'),
                        metadata={
                            "event_ticker": getattr(event, 'ticker', ''),
                            "status": getattr(m, 'status', ''),
                        }
                    )
                    markets.append(market)

            logger.info(f"Fetched {len(markets)} Kalshi markets")
            return markets[:limit]

        except Exception as e:
            logger.error(f"Error fetching Kalshi markets: {e}")
            return []

    def get_market(self, market_id: str) -> Optional[Market]:
        try:
            resp = self.client.get_market(market_ticker=market_id)
            if not hasattr(resp, 'market'):
                return None

            m = resp.market
            return Market(
                id=getattr(m, 'ticker', ''),
                exchange="kalshi",
                question=getattr(m, 'title', ''),
                yes_price=(getattr(m, 'yes_price', 0) or 0) / 100,
                no_price=(getattr(m, 'no_price', 0) or 0) / 100,
                volume=getattr(m, 'volume', 0) or 0,
                liquidity=getattr(m, 'liquidity', 0) or 0,
                closes_at=self._parse_time(getattr(m, 'close_time', None)),
                category='',
                metadata={"status": getattr(m, 'status', '')}
            )
        except Exception as e:
            logger.error(f"Error fetching market {market_id}: {e}")
            return None

    def get_order_book(self, market_id: str) -> Optional[dict]:
        try:
            resp = self.client.get_market_order_book(market_ticker=market_id)
            if not hasattr(resp, 'order_book'):
                return None

            book = resp.order_book
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
            # Kalshi prices are in cents (0-100)
            price_cents = int(price * 100)
            action = "buy"
            side_map = {"YES": "yes", "NO": "no"}

            resp = self.client.create_order(
                ticker=market_id,
                client_order_id=f"bot_{datetime.now().timestamp()}",
                action=action,
                side=side_map.get(side, "yes"),
                count=int(size),
                type_="limit",
                yes_price=price_cents if side == "YES" else None,
                no_price=price_cents if side == "NO" else None,
            )

            order_id = getattr(resp, 'order', {}).get('order_id', '') if hasattr(resp, 'order') else ''

            order = Order(
                id=order_id,
                exchange="kalshi",
                market_id=market_id,
                side=side,
                price=price,
                size=size,
                status="submitted",
                created_at=datetime.now(timezone.utc),
            )
            logger.info(f"Kalshi order: {side} {size} @ ${price:.2f} on {market_id}")
            return order

        except Exception as e:
            logger.error(f"Kalshi order failed: {e}")
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
                result.append(Position(
                    market_id=getattr(p, 'ticker', ''),
                    exchange="kalshi",
                    question=getattr(p, 'title', ''),
                    side="YES" if getattr(p, 'position', 0) > 0 else "NO",
                    entry_price=0,
                    size=abs(getattr(p, 'position', 0)),
                    current_price=0,
                    pnl=getattr(p, 'realized_pnl', 0) or 0,
                    opened_at=datetime.now(timezone.utc),
                ))
            return result
        except Exception as e:
            logger.error(f"Error getting positions: {e}")
            return []

    def get_balance(self) -> float:
        try:
            resp = self.client.get_balance()
            return (resp.balance or 0) / 100
        except Exception as e:
            logger.error(f"Error getting balance: {e}")
            return 0

    def close(self):
        pass

    def _parse_time(self, ts) -> Optional[datetime]:
        if ts is None:
            return None
        if isinstance(ts, datetime):
            return ts
        try:
            return datetime.fromtimestamp(int(ts), tz=timezone.utc)
        except (ValueError, TypeError):
            return None
