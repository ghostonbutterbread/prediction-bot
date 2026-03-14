"""Main bot runner — multi-exchange prediction market bot."""

import time
import logging
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from bot.exchanges.base import BaseExchange, Market
from bot.exchanges.kalshi import KalshiExchange
from bot.strategies.enhanced import EnhancedStrategyEngine, KellySizer

logger = logging.getLogger(__name__)


class PredictionBot:
    """
    Multi-exchange prediction market trading bot.

    Architecture:
    - Exchange adapters (Kalshi, Polymarket, etc.)
    - News feed aggregation
    - Multi-signal strategy engine
    - Kelly Criterion position sizing
    - SQLite trade logging
    """

    def __init__(self, config: dict = None):
        config = config or {}
        self.config = config

        # Exchanges
        self.exchanges: dict[str, BaseExchange] = {}

        # Strategy
        self.strategy = EnhancedStrategyEngine(config.get("strategy", {}))
        self.kelly = KellySizer(
            fraction=config.get("kelly_fraction", 0.5),
            max_bet_pct=config.get("max_position_pct", 0.10),
        )

        # State
        self.running = False
        self.stats = {
            "scans": 0,
            "signals": 0,
            "trades": 0,
            "errors": 0,
        }

        # Trade log
        self.log_dir = Path(config.get("log_dir", "data"))
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def add_kalshi(self, api_key_id: str, private_key_path: str, demo: bool = True):
        """Add Kalshi exchange."""
        exchange = KalshiExchange(api_key_id, private_key_path, demo)
        self.exchanges["kalshi"] = exchange
        return exchange

    def connect_all(self) -> dict[str, bool]:
        """Connect to all configured exchanges."""
        results = {}
        for name, exchange in self.exchanges.items():
            try:
                results[name] = exchange.connect()
            except Exception as e:
                logger.error(f"Failed to connect to {name}: {e}")
                results[name] = False
        return results

    def scan_once(self) -> dict:
        """Run a single scan cycle across all exchanges."""
        logger.info(f"\n{'='*60}")
        logger.info(f"Scan #{self.stats['scans'] + 1} at {datetime.now(timezone.utc).isoformat()}")
        logger.info(f"{'='*60}")

        all_signals = []

        for exchange_name, exchange in self.exchanges.items():
            try:
                # 1. Fetch markets
                markets = exchange.get_markets(limit=30)
                if not markets:
                    continue

                logger.info(f"\n{exchange_name}: {len(markets)} markets")

                # 2. Analyze each market
                for market in markets:
                    try:
                        order_book = exchange.get_order_book(market.id)
                        signal = self.strategy.analyze_market(market, order_book)

                        if signal:
                            signal["exchange"] = exchange_name
                            all_signals.append(signal)

                    except Exception as e:
                        logger.debug(f"Error analyzing {market.id}: {e}")
                        continue

            except Exception as e:
                logger.error(f"Error scanning {exchange_name}: {e}")
                self.stats["errors"] += 1

        # Sort by edge
        all_signals.sort(key=lambda s: s["edge"], reverse=True)

        # Log top signals
        if all_signals:
            logger.info(f"\n📊 Top Signals:")
            for sig in all_signals[:5]:
                logger.info(
                    f"  {sig['direction']} | "
                    f"Edge: {sig['edge']:.2%} | "
                    f"Conf: {sig['confidence']:.2%} | "
                    f"Price: ${sig['market_price']:.2f} | "
                    f"[{sig['exchange']}]"
                )
        else:
            logger.info("  No signals this cycle")

        # Execute top signals (paper mode by default)
        trades = 0
        for sig in all_signals[:3]:
            if self._should_execute(sig):
                result = self._execute_signal(sig)
                if result:
                    trades += 1

        self.stats["scans"] += 1
        self.stats["signals"] += len(all_signals)
        self.stats["trades"] += trades

        self._log_scan(all_signals, trades)

        return {
            "markets_scanned": sum(
                len(exchange.get_markets(limit=5))
                for exchange in self.exchanges.values()
            ),
            "signals": len(all_signals),
            "trades": trades,
        }

    def run_loop(self, interval_seconds: int = 120, max_scans: int = None):
        """Continuous scan loop."""
        self.running = True
        logger.info(f"Bot started — scanning every {interval_seconds}s")

        count = 0
        while self.running:
            try:
                self.scan_once()
                count += 1

                if max_scans and count >= max_scans:
                    break

                time.sleep(interval_seconds)

            except KeyboardInterrupt:
                break
            except Exception as e:
                self.stats["errors"] += 1
                logger.error(f"Scan error: {e}", exc_info=True)
                time.sleep(interval_seconds)

        self.running = False
        logger.info(f"Bot stopped. Stats: {self.stats}")

    def _should_execute(self, signal: dict) -> bool:
        """Check if signal meets execution criteria."""
        strategy_cfg = self.config.get("strategy", {})
        min_conf = strategy_cfg.get("min_confidence", self.config.get("min_confidence", 0.50))
        min_edge = strategy_cfg.get("min_edge", self.config.get("min_edge", 0.02))
        return (
            signal["confidence"] >= min_conf and
            signal["edge"] >= min_edge
        )

    def _execute_signal(self, signal: dict) -> Optional[dict]:
        """Execute a trading signal."""
        exchange = self.exchanges.get(signal["exchange"])
        if not exchange:
            return None

        # Calculate position size using Kelly Criterion
        balance = exchange.get_balance()
        size = self.kelly.calculate(
            signal["model_probability"],
            signal["market_price"],
            balance,
        )

        if size < 1:
            logger.info(f"Position too small: ${size:.2f}")
            return None

        # Calculate price (slightly better than market)
        if signal["direction"] == "BUY_YES":
            price = min(signal["market_price"] + 0.01, 0.99)
            side = "YES"
        else:
            price = min((1 - signal["market_price"]) + 0.01, 0.99)
            side = "NO"

        # Place order
        order = exchange.place_order(signal["market_id"], side, price, size)

        if order:
            logger.info(
                f"✅ Trade executed: {side} ${size:.2f} @ ${price:.2f} "
                f"on {signal['exchange']}/{signal['market_id']}"
            )
            self._log_trade(signal, order)
            return {"order": order, "signal": signal}

        return None

    def _log_scan(self, signals: list, trades: int):
        """Log scan results to file."""
        log_file = self.log_dir / f"scans_{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        with open(log_file, "a") as f:
            f.write(json.dumps({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "signals": len(signals),
                "trades": trades,
                "top_signals": signals[:3],
            }) + "\n")

    def _log_trade(self, signal: dict, order):
        """Log trade to file."""
        log_file = self.log_dir / "trades.jsonl"
        with open(log_file, "a") as f:
            f.write(json.dumps({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "signal": signal,
                "order_id": order.id if hasattr(order, 'id') else str(order),
            }) + "\n")

    def stop(self):
        """Stop the bot."""
        self.running = False

    def close(self):
        """Clean up."""
        if hasattr(self.strategy, 'news'):
            self.strategy.news.close()
        for exchange in self.exchanges.values():
            exchange.close()
