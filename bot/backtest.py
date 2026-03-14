"""Backtesting framework for prediction market strategies.

Tests strategy against historical market data to estimate profitability.
Supports:
- Loading historical market snapshots from Kalshi
- Running strategy against past prices
- Comparing predictions against actual outcomes
- Performance metrics (win rate, Sharpe, max drawdown, etc.)
"""

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from bot.exchanges.base import Market
from bot.strategies.enhanced import EnhancedStrategyEngine, KellySizer

logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    """A backtested trade."""
    timestamp: str
    market_id: str
    question: str
    direction: str
    market_price: float
    model_prob: float
    edge: float
    confidence: float
    position_size: float

    # Resolution
    outcome: Optional[str] = None  # "YES" or "NO"
    resolved: bool = False
    pnl: Optional[float] = None
    payout: Optional[float] = None


@dataclass
class BacktestResult:
    """Backtest performance summary."""
    start_date: str
    end_date: str
    total_trades: int
    wins: int
    losses: int
    unresolved: int
    win_rate: float
    total_pnl: float
    total_wagered: float
    roi: float
    avg_edge: float
    max_drawdown: float
    sharpe_ratio: float
    by_direction: dict = field(default_factory=dict)
    by_category: dict = field(default_factory=dict)
    trades: list = field(default_factory=list)


class Backtester:
    """Backtest prediction market strategies against historical data."""

    def __init__(self, config: dict = None):
        config = config or {}
        self.strategy = EnhancedStrategyEngine(config.get("strategy", {}))
        self.kelly = KellySizer(
            fraction=config.get("kelly_fraction", 0.5),
            max_bet_pct=config.get("max_position_pct", 0.10),
        )
        self.starting_balance = config.get("starting_balance", 1000)
        self.min_edge = config.get("strategy", {}).get("min_edge", 0.02)
        self.min_confidence = config.get("strategy", {}).get("min_confidence", 0.50)

    def load_historical_markets(self, exchange, days_back: int = 7,
                                 limit: int = 100) -> list[Market]:
        """Load currently active markets for backtesting.

        For true backtesting, we'd need historical snapshots.
        For now, we use current markets and simulate "what would the bot do right now."
        """
        return exchange.get_markets(limit=limit)

    def run(self, markets: list[Market], starting_balance: float = None) -> BacktestResult:
        """Run backtest against a list of markets.

        Note: This is a "live backtest" - it tests the strategy against current
        market prices. For true historical backtesting, we'd need resolved
        market data with outcomes.
        """
        balance = starting_balance or self.starting_balance
        initial_balance = balance
        trades = []
        peak_balance = balance
        max_drawdown = 0.0

        for market in markets:
            try:
                # Get order book proxy
                order_book = {
                    "best_yes_ask": market.yes_price,
                    "best_yes_bid": max(0, market.yes_price - 0.01),
                    "mid_yes": market.yes_price,
                    "spread": 0.01,
                    "spread_pct": (0.01 / market.yes_price * 100) if market.yes_price > 0 else 10,
                }

                signal = self.strategy.analyze_market(market, order_book)
                if not signal:
                    continue

                edge = signal.get("edge", 0)
                confidence = signal.get("confidence", 0)

                if edge < self.min_edge or confidence < self.min_confidence:
                    continue

                # Calculate position size
                direction = signal["direction"]
                if direction == "BUY_NO":
                    kelly_prob = 1 - signal["model_probability"]
                    kelly_price = 1 - market.yes_price
                else:
                    kelly_prob = signal["model_probability"]
                    kelly_price = market.yes_price

                size = self.kelly.calculate(kelly_prob, kelly_price, balance)

                if size <= 0 or size > balance:
                    continue

                trade = BacktestTrade(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    market_id=market.id,
                    question=market.question,
                    direction=direction,
                    market_price=market.yes_price,
                    model_prob=signal["model_probability"],
                    edge=edge,
                    confidence=confidence,
                    position_size=round(size, 2),
                )

                trades.append(trade)
                balance -= size  # Reserve for position

            except Exception as e:
                logger.debug(f"Backtest error for {market.id}: {e}")
                continue

        # Calculate metrics
        total_wagered = sum(t.position_size for t in trades)
        avg_edge = sum(t.edge for t in trades) / len(trades) if trades else 0

        # Direction breakdown
        by_dir = {}
        for t in trades:
            by_dir.setdefault(t.direction, 0)
            by_dir[t.direction] += 1

        return BacktestResult(
            start_date=datetime.now(timezone.utc).isoformat()[:10],
            end_date=datetime.now(timezone.utc).isoformat()[:10],
            total_trades=len(trades),
            wins=0,  # Can't determine without resolved markets
            losses=0,
            unresolved=len(trades),
            win_rate=0.0,
            total_pnl=0.0,
            total_wagered=round(total_wagered, 2),
            roi=0.0,
            avg_edge=round(avg_edge, 4),
            max_drawdown=0.0,
            sharpe_ratio=0.0,
            by_direction=by_dir,
            trades=[asdict(t) for t in trades],
        )

    def run_from_json(self, exchange, json_path: str) -> BacktestResult:
        """Run backtest against historical data from a JSON file.

        Expected format:
        [
            {
                "ticker": "KXELONMARS-99",
                "title": "Will Elon Musk visit Mars...",
                "yes_price": 0.10,
                "outcome": "NO",  # if resolved
                "volume": 55134,
                "timestamp": "2026-03-14T12:00:00Z"
            },
            ...
        ]
        """
        with open(json_path) as f:
            data = json.load(f)

        # Convert to Market objects
        markets = []
        for item in data:
            market = Market(
                id=item.get("ticker", ""),
                question=item.get("title", ""),
                yes_price=item.get("yes_price", 0),
                no_price=1 - item.get("yes_price", 0),
                volume=item.get("volume", 0),
                exchange=exchange.name,
                closes_at=None,
                status="open",
                category=item.get("category", ""),
            )
            markets.append(market)

        return self.run(markets)

    def print_report(self, result: BacktestResult):
        """Print backtest results."""
        print(f"""
{'='*60}
📊 Backtest Report
{'='*60}
Period:          {result.start_date} to {result.end_date}
Total Trades:    {result.total_trades}
Total Wagered:   ${result.total_wagered:.2f}

Avg Edge:        {result.avg_edge:.2%}
Direction:       {result.by_direction}

Status:          {result.unresolved} unresolved (need resolved markets for P&L)

💡 To get P&L data, run backtest against resolved markets
   (markets that have closed with known outcomes)
{'='*60}
""")


# Quick test
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    config = {
        "strategy": {
            "min_edge": 0.01,
            "min_confidence": 0.50,
        },
        "starting_balance": 1000,
    }

    from bot.exchanges.kalshi import KalshiExchange
    import os
    from dotenv import load_dotenv
    load_dotenv()

    api_key = os.getenv("KALSHI_API_KEY_ID")
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")

    exchange = KalshiExchange(api_key, key_path, demo=False)
    exchange.connect()

    bt = Backtester(config)
    markets = exchange.get_markets(limit=30)
    result = bt.run(markets)
    bt.print_report(result)
