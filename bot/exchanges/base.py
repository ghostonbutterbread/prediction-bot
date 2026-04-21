"""Base exchange interface — all exchanges implement this."""

from abc import ABC, abstractmethod
from typing import Optional
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Market:
    """Represents a prediction market."""
    id: str
    exchange: str
    question: str
    yes_price: float       # YES ask price (what you pay to buy YES)
    no_price: float        # NO ask price (what you pay to buy NO)
    volume: float
    liquidity: float
    closes_at: Optional[datetime]
    category: str
    metadata: dict
    close_price: Optional[float] = None  # Settlement price: 1.0=YES, 0.0=NO, None=not settled
    yes_bid: Optional[float] = None      # Best YES bid price
    no_bid: Optional[float] = None       # Best NO bid price


@dataclass
class Order:
    """Represents an order."""
    id: str
    exchange: str
    market_id: str
    side: str  # "YES" or "NO"
    price: float
    size: float
    status: str
    created_at: datetime


@dataclass
class Position:
    """Represents an open position."""
    market_id: str
    exchange: str
    question: str
    side: str
    entry_price: float
    size: float
    current_price: float
    pnl: float
    opened_at: datetime
    metadata: dict = field(default_factory=dict)


@dataclass
class RestingOrder:
    """Represents an open or partially-filled order."""
    order_id: str
    market_id: str
    exchange: str
    side: str
    requested_size: float
    filled_size: float
    remaining_size: float
    price: float
    status: str
    created_at: datetime
    updated_at: Optional[datetime] = None
    question: str = ""
    metadata: dict = field(default_factory=dict)


class BaseExchange(ABC):
    """Abstract base class for exchange adapters."""

    name: str = "base"

    @abstractmethod
    def connect(self) -> bool:
        """Authenticate and connect to the exchange."""
        pass

    @abstractmethod
    def get_markets(self, limit: int = 50, category: str = None) -> list[Market]:
        """Fetch active markets."""
        pass

    @abstractmethod
    def get_market(self, market_id: str) -> Optional[Market]:
        """Get a single market by ID."""
        pass

    @abstractmethod
    def get_order_book(self, market_id: str) -> Optional[dict]:
        """Get order book for a market."""
        pass

    @abstractmethod
    def place_order(self, market_id: str, side: str, price: float,
                    size: float) -> Optional[Order]:
        """Place a limit order."""
        pass

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order."""
        pass

    @abstractmethod
    def get_positions(self) -> list[Position]:
        """Get current open positions."""
        pass

    @abstractmethod
    def get_balance(self) -> float:
        """Get account balance."""
        pass

    def get_resting_orders(self) -> list[RestingOrder]:
        """Get currently open or partially-filled orders."""
        return []

    @abstractmethod
    def close(self):
        """Clean up resources."""
        pass
