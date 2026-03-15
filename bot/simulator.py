"""Simulation engine — paper trades with full audit trail."""

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional

from bot.exchanges.base import Market
from bot.strategies.enhanced import EnhancedStrategyEngine, KellySizer

logger = logging.getLogger(__name__)


@dataclass
class SimTrade:
    """A simulated trade."""
    id: str
    timestamp: str
    exchange: str
    market_id: str
    question: str
    direction: str          # BUY_YES or BUY_NO
    model_probability: float
    market_price: float
    edge: float
    confidence: float
    position_size: float    # dollars
    signals: dict           # individual signal breakdown

    # Resolution (filled in later)
    resolved: bool = False
    outcome: Optional[str] = None  # "YES" or "NO"
    pnl: Optional[float] = None
    resolved_at: Optional[str] = None


@dataclass
class SimSession:
    """A simulation session."""
    session_id: str
    started_at: str
    starting_balance: float
    trades: list
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    win_rate: float = 0.0


class Simulator:
    """
    Paper trading simulator with full audit trail.

    Features:
    - Logs every signal the bot would have acted on
    - Tracks hypothetical P&L
    - Records reasoning (which signals fired, confidence, edge)
    - Resolves trades when markets close
    - Generates performance reports
    """

    def __init__(self, config: dict = None):
        config = config or {}
        self.strategy = EnhancedStrategyEngine(config.get("strategy", {}))
        self.kelly = KellySizer(
            fraction=config.get("kelly_fraction", 0.5),
            max_bet_pct=config.get("max_position_pct", 0.10),
        )

        # Risk management
        from bot.risk import RiskManager
        self.risk = RiskManager(config)

        self.starting_balance = config.get("starting_balance", 100.0)
        self.balance = self.starting_balance
        strategy_cfg = config.get("strategy", {})
        # Also check top-level config for env var overrides
        self.min_edge = config.get("min_edge", strategy_cfg.get("min_edge", 0.01))
        self.min_confidence = config.get("min_confidence", strategy_cfg.get("min_confidence", 0.50))

        # Storage
        self.data_dir = Path(config.get("data_dir", "data"))
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.trades: list[SimTrade] = []
        self.scan_count = 0

        logger.info(f"Simulator started — session {self.session_id}")
        logger.info(f"Starting balance: ${self.starting_balance:.2f}")

    def scan(self, exchange) -> dict:
        """Run a simulation scan on an exchange."""
        self.scan_count += 1
        logger.info(f"\n{'='*60}")
        logger.info(f"Sim Scan #{self.scan_count} at {datetime.now(timezone.utc).strftime('%H:%M:%S')}")

        markets = exchange.get_markets(limit=30)
        if not markets:
            return {"markets": 0, "signals": 0, "trades": 0}

        # Write snapshot and run AI analysis (every 5th scan)
        if self.scan_count % 5 == 0:
            from bot.feeds.ai_signal import write_snapshot
            write_snapshot(markets, self.session_id)

            # Run AI analyzer as subprocess (Ghost's analysis)
            import subprocess
            try:
                subprocess.Popen(
                    ["python3", "-m", "bot.ai_analyzer"],
                    cwd=str(Path(__file__).parent.parent),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                logger.info("🤖 AI analyzer spawned (subprocess)")
            except Exception as e:
                logger.debug(f"Failed to spawn AI analyzer: {e}")

        logger.info(f"Analyzing {len(markets)} markets...")

        signals_found = []
        trades_taken = []

        for market in markets:
            try:
                # Build order book from market-level bid/ask
                order_book = {
                    "best_yes_ask": market.yes_price,
                    "best_yes_bid": max(0, market.yes_price - 0.01),
                    "mid_yes": market.yes_price,
                    "spread": 0.01,
                    "spread_pct": (0.01 / market.yes_price * 100) if market.yes_price > 0 else 10,
                }

                try:
                    signal = self.strategy.analyze_market(market, order_book)
                except Exception as e:
                    logger.debug(f"Strategy error for {market.id}: {e}")
                    continue

                if signal is None:
                    continue

                if signal:
                    signal["market_id"] = market.id
                    signal["question"] = market.question
                    signals_found.append(signal)

                    should_trade = self._should_trade(signal)
                    # DEBUG: log every signal
                    logger.info(f"  Signal: {signal.get('direction','')} edge={signal.get('edge',0):.3f} conf={signal.get('confidence',0):.3f} -> {should_trade}")

                    if should_trade:
                        trade = self._create_trade(signal)
                        self.trades.append(trade)
                        trades_taken.append(trade)

            except Exception as e:
                logger.debug(f"Error analyzing {market.id}: {e}")
                continue

        # Log results
        if trades_taken:
            logger.info(f"\n📝 Would take {len(trades_taken)} trades:")
            for t in trades_taken:
                logger.info(
                    f"  {t.direction} | "
                    f"Edge: {t.edge:.2%} | "
                    f"Conf: {t.confidence:.2%} | "
                    f"Price: ${t.market_price:.2f} | "
                    f"Size: ${t.position_size:.2f}"
                )
        else:
            logger.info(f"  No trades this scan ({len(signals_found)} signals, none met thresholds)")

        # Log risk status
        status = self.risk.get_status()
        logger.info(
            f"📊 Risk: balance={status['balance']} pnl={status['pnl']} "
            f"drawdown={status['drawdown']} positions={status['open_positions']} "
            f"streak={self.risk.state.consecutive_losses}L/{self.risk.state.consecutive_wins}W"
        )

        self._save_session()

        return {
            "markets": len(markets),
            "signals": len(signals_found),
            "trades": len(trades_taken),
            "balance": self.balance,
            "total_trades": len(self.trades),
        }

    def _should_trade(self, signal: dict) -> bool:
        return (
            signal.get("edge", 0) >= self.min_edge and
            signal.get("confidence", 0) >= self.min_confidence
        )

    def _create_trade(self, signal: dict) -> Optional[SimTrade]:
        model_prob = signal.get("model_probability", 0.5)
        market_price = signal.get("market_price", 0.5)
        direction = signal.get("direction", "BUY_YES")

        # Kelly needs probability of the BET winning, not YES probability
        if direction == "BUY_NO":
            kelly_prob = 1 - model_prob
            kelly_price = 1 - market_price
        else:
            kelly_prob = model_prob
            kelly_price = market_price

        size = self.kelly.calculate(kelly_prob, kelly_price, self.balance)

        # === Risk Management Check ===
        risk_decision = self.risk.check_trade(signal, size)

        if not risk_decision.approved:
            logger.debug(f"🛑 Risk rejected: {risk_decision.reason}")
            return None

        if risk_decision.warnings:
            for w in risk_decision.warnings:
                logger.debug(f"⚠️  {w}")

        # Use risk-adjusted size
        size = risk_decision.adjusted_size

        trade = SimTrade(
            id=f"sim_{self.session_id}_{len(self.trades)+1:04d}",
            timestamp=datetime.now(timezone.utc).isoformat(),
            exchange=signal.get("exchange", "unknown"),
            market_id=signal.get("market_id", ""),
            question=signal.get("question", ""),
            direction=signal.get("direction", ""),
            model_probability=signal.get("model_probability", 0),
            market_price=signal.get("market_price", 0),
            edge=signal.get("edge", 0),
            confidence=signal.get("confidence", 0),
            position_size=round(size, 2),
            signals=signal.get("signals", {}),
        )

        # Record with risk manager
        self.risk.record_trade({
            "question": trade.question,
            "direction": trade.direction,
            "position_size": trade.position_size,
            "market_price": trade.market_price,
        })

        return trade

    def report(self) -> dict:
        """Generate performance report."""
        total = len(self.trades)
        if total == 0:
            return {
                "session": self.session_id,
                "total_trades": 0,
                "message": "No trades yet. Run more scans.",
            }

        # Stats
        edges = [t.edge for t in self.trades]
        confidences = [t.confidence for t in self.trades]
        sizes = [t.position_size for t in self.trades]

        by_direction = {}
        for t in self.trades:
            by_direction[t.direction] = by_direction.get(t.direction, 0) + 1

        by_exchange = {}
        for t in self.trades:
            by_exchange[t.exchange] = by_exchange.get(t.exchange, 0) + 1

        return {
            "session": self.session_id,
            "started_at": self.trades[0].timestamp if self.trades else None,
            "total_trades": total,
            "starting_balance": self.starting_balance,
            "current_balance": self.balance,
            "pnl": round(self.balance - self.starting_balance, 2),
            "pnl_pct": round((self.balance - self.starting_balance) / self.starting_balance * 100, 2),
            "avg_edge": round(sum(edges) / len(edges), 4),
            "max_edge": round(max(edges), 4),
            "avg_confidence": round(sum(confidences) / len(confidences), 4),
            "avg_position_size": round(sum(sizes) / len(sizes), 2),
            "total_exposure": round(sum(sizes), 2),
            "by_direction": by_direction,
            "by_exchange": by_exchange,
            "scans_run": self.scan_count,
        }

    def get_open_trades(self) -> list[dict]:
        """Get all unresolved trades."""
        return [asdict(t) for t in self.trades if not t.resolved]

    def get_all_trades(self) -> list[dict]:
        """Get all trades."""
        return [asdict(t) for t in self.trades]

    def print_report(self):
        """Print formatted report to console."""
        r = self.report()

        print(f"\n{'='*60}")
        print(f"📊 Simulation Report — Session {r['session']}")
        print(f"{'='*60}")

        if r["total_trades"] == 0:
            print("No trades yet. Run more scans.")
            return

        print(f"""
Starting Balance:  ${r['starting_balance']:.2f}
Current Balance:   ${r['current_balance']:.2f}
P&L:               ${r['pnl']:+.2f} ({r['pnl_pct']:+.1f}%)

Total Trades:      {r['total_trades']}
Scans Run:         {r['scans_run']}

Avg Edge:          {r['avg_edge']:.2%}
Max Edge:          {r['max_edge']:.2%}
Avg Confidence:    {r['avg_confidence']:.2%}
Avg Position:      ${r['avg_position_size']:.2f}
Total Exposure:    ${r['total_exposure']:.2f}

Direction Breakdown:""")

        for direction, count in r.get("by_direction", {}).items():
            print(f"  {direction}: {count}")

        print("\nExchange Breakdown:")
        for exchange, count in r.get("by_exchange", {}).items():
            print(f"  {exchange}: {count}")

        print(f"{'='*60}\n")

    def _save_session(self):
        """Save session data to disk."""
        session_file = self.data_dir / f"sim_{self.session_id}.json"
        data = {
            "session_id": self.session_id,
            "starting_balance": self.starting_balance,
            "balance": self.balance,
            "scan_count": self.scan_count,
            "trades": [asdict(t) for t in self.trades],
            "report": self.report(),
        }
        with open(session_file, "w") as f:
            json.dump(data, f, indent=2, default=str)
