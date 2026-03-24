"""Simulation engine — paper trades with full audit trail."""

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dataclasses import dataclass, asdict
from math import isfinite
from typing import Optional

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
    category: str = ""      # market category (e.g., KXSHIBA, KXNFLX) for correlation tracking

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

    def __init__(self, config: dict = None, load_from: str = None):
        """
        Args:
            config: Strategy/risk config dict
            load_from: Optional session_id to load. If None, loads the latest session from data_dir.
                       If no session found, starts fresh.
        """
        config = config or {}
        self.strategy = EnhancedStrategyEngine(config.get("strategy", {}))
        self.kelly = KellySizer()

        # Risk management
        from bot.risk import RiskManager
        self.risk = RiskManager(config)

        self.starting_balance = config.get("starting_balance", 100.0)
        self.balance = self.starting_balance
        strategy_cfg = config.get("strategy", {})
        self.min_edge = config.get("min_edge", strategy_cfg.get("min_edge", 0.01))
        self.min_confidence = config.get("min_confidence", strategy_cfg.get("min_confidence", 0.50))
        self.max_entry_price = config.get("max_entry_price", 0.70)
        self.enable_time_decay_ranking = config.get("enable_time_decay_ranking", True)

        self.data_dir = Path(config.get("data_dir", "data"))
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Storage
        self.traded_markets: set = set()
        self.rolling_win_rate: float = 0.0
        self.rolling_win_count: int = 0
        self.rolling_loss_count: int = 0
        self.rolling_window: int = 50
        self.scan_count: int = 0

        # Loss streak tracking (per calendar day)
        self.last_loss_date: Optional[str] = None  # YYYY-MM-DD of last losing day
        self.consecutive_daily_losses: int = 0

        # Try to load an existing session
        loaded = self._load_session(load_from)
        if not loaded:
            # Fresh session
            self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.trades: list[SimTrade] = []
            logger.info(f"Simulator started — session {self.session_id}")
            logger.info(f"Starting balance: ${self.starting_balance:.2f}")

        # Social Feed
        self.social_feed = None
        if config.get("enable_social", True):
            try:
                from bot.feeds.twitter import SocialFeed
                self.social_feed = SocialFeed(config)
                logger.info("🐦 Social feed enabled")
            except Exception:
                pass

    def _load_session(self, session_id: str = None) -> bool:
        """
        Load a session from disk. If session_id is None, loads the most recent session.
        Returns True if a session was loaded, False if none found.
        """
        if session_id:
            session_files = [self.data_dir / f"sim_{session_id}.json"]
            if not session_files[0].exists():
                return False
        else:
            session_files = sorted(self.data_dir.glob("sim_*.json"), reverse=True)
            if not session_files:
                return False

        session_file = session_files[0]
        try:
            with open(session_file) as f:
                data = json.load(f)

            self.session_id = data.get("session_id", datetime.now().strftime("%Y%m%d_%H%M%S"))
            self.starting_balance = data.get("starting_balance", 100.0)
            self.balance = data.get("balance", self.starting_balance)
            self.scan_count = data.get("scan_count", 0)
            self.max_entry_price = data.get("max_entry_price", 0.70)
            self.consecutive_daily_losses = data.get("consecutive_daily_losses", 0)
            self.last_loss_date = data.get("last_loss_date")

            # Reconstruct SimTrade objects
            self.trades = []
            for idx, t_data in enumerate(data.get("trades", []), start=1):
                self.trades.append(SimTrade(
                    id=t_data.get("id", f"sim_{self.session_id}_{idx:04d}"),
                    timestamp=t_data.get("timestamp", datetime.now(timezone.utc).isoformat()),
                    exchange=t_data.get("exchange", "unknown"),
                    market_id=t_data.get("market_id", ""),
                    question=t_data.get("question", ""),
                    direction=t_data.get("direction", "BUY_YES"),
                    model_probability=t_data.get("model_probability", 0.5),
                    market_price=t_data.get("market_price", 0.5),
                    edge=t_data.get("edge", 0),
                    confidence=t_data.get("confidence", 0),
                    position_size=t_data.get("position_size", 0),
                    signals=t_data.get("signals", {}),
                    resolved=t_data.get("resolved", False),
                    outcome=t_data.get("outcome"),
                    pnl=t_data.get("pnl"),
                    resolved_at=t_data.get("resolved_at"),
                ))

            self.traded_markets = {t.market_id for t in self.trades}
            self.rolling_win_rate = 0.0
            self.rolling_win_count = 0
            self.rolling_loss_count = 0

            logger.info(f"Loaded session {self.session_id} — {len(self.trades)} trades, balance ${self.balance:.2f}")
            return True

        except Exception as e:
            logger.error(f"Failed to load session {session_file}: {e}")
            return False

    def scan(self, exchange) -> dict:
        """Run a simulation scan on an exchange."""
        self.scan_count += 1
        self.risk.reset_daily()  # Reset daily trackers if new day
        logger.info(f"\n{'='*60}")
        logger.info(f"Sim Scan #{self.scan_count} at {datetime.now(timezone.utc).strftime('%H:%M:%S')}")

        markets = exchange.get_markets(limit=100)
        if not markets:
            return {"markets": 0, "signals": 0, "trades": 0}
        
        # === SPORTS MODE: Analyze sports markets + injury sniper ===
        sports_trades = []
        try:
            from bot.strategies.sports import MarketFilter, QuickBetStrategy
            from bot.strategies.injury_sniper import InjurySniper
            
            sports_markets = MarketFilter.filter_sports(markets, max_hours=48)
            if sports_markets:
                logger.info(f"🏀 Found {len(sports_markets)} sports markets (closing within 48h)")
                
                # 1. Quick bet strategy (player props, totals, outcomes)
                qb = QuickBetStrategy()
                for sm in sports_markets:
                    if sm.market_id in self.traded_markets:
                        continue
                    sig = qb.analyze_market(sm)
                    if sig.get("should_trade"):
                        trade = self._create_trade(sig)
                        if trade:
                            self.trades.append(trade)
                            sports_trades.append(trade)
                            self.traded_markets.add(sm.market_id)
                
                # 2. Injury sniper (star player injuries → bet against team)
                sniper = InjurySniper()
                # for tweet in tweets: Replace THIS
                #     injury_signals = sniper.scan_markets_for_injuries(markets)
                injury_signals = []# Replace THIS
                for market in markets : # Added this to show the code I was trying to implement.
                    if MarketFilter.classify_market(market.title) in MarketFilter.SPORTS_KEYWORDS:
                        injury_signal = self.analyze_sports_signal(market, sniper)
                        if injury_signal:
                            injury_signals.append(injury_signal) # Append the new signal
                # for sig in injury_signals: - Delete this old code
                    #trade = self._create_trade(sig)
                    # if trade:
                    #   self.trades.append(trade)
                    #   sports_trades.append(trade)
                    #  self.traded_markets.add(mid) - Delete this old code
            

        except Exception as e:
            logger.debug(f"Sports analysis error: {e}")

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

        # Start social media tasks
        try:
            if social_feed:
                # Check for injury alerts in feed (Twitter API)
                alerts = social_feed.scan()
                if alerts:
                    injury_signals = []
                    for alert in alerts:
                        sig = InjurySniper.scan_text(alert.source_text, alert.source)
                        if sig:
                            injury_signals.append(sig)
                    for sig in injury_signals:
                        trade = self._create_trade(sig)
                        if trade:
                            self.trades.append(trade)
                            sports_trades.append(trade)
        except Exception as e:
                logger.debug(f"Social feed analysis error: {e}")
                
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
                        # Dedup: skip if we already traded this market
                        market_id = signal.get("market_id", "")
                        if market_id in self.traded_markets:
                            logger.debug(f"  Skipping duplicate: {market_id}")
                            continue
                        # Attach market object for time-decay scoring
                        signal["_market"] = market

                        if self.enable_time_decay_ranking:
                            # Queue for time-decay ranking — don't trade immediately
                            logger.debug(f"  ⏳ Queued for time-decay ranking: {market_id}")
                        else:
                            # Legacy mode: trade immediately
                            trade = self._create_trade(signal)
                            if trade:
                                self.trades.append(trade)
                                trades_taken.append(trade)
                                self.traded_markets.add(market_id)

            except Exception as e:
                logger.debug(f"Error analyzing {market.id}: {e}")
                continue

        # === TIME-DECAY RANKING PHASE ===
        # Rank all queued signals by: edge × confidence / days_to_resolve
        # Then take the best opportunities (respecting Kelly sizing + risk management)
        if self.enable_time_decay_ranking:
            scored_signals = []
            for sig in signals_found:
                if not self._should_trade(sig):
                    continue
                market_id = sig.get("market_id", "")
                if market_id in self.traded_markets:
                    continue
                market = sig.get("_market")
                if not market:
                    continue
                score = self._compute_time_adjusted_score(sig, market)
                if score > 0:
                    scored_signals.append((score, sig, market))

            if scored_signals:
                # Sort by time-adjusted score descending (best opportunities first)
                scored_signals.sort(key=lambda x: x[0], reverse=True)

                # Log the full ranking
                logger.info(f"\n🏆 TIME-DECAY RANKING ({len(scored_signals)} opportunities):")
                for rank, (score, sig, mkt) in enumerate(scored_signals[:10], 1):
                    hours_left = "?"
                    if mkt.closes_at:
                        h = max(0, (mkt.closes_at - datetime.now(timezone.utc)).total_seconds() / 3600)
                        hours_left = f"{h:.1f}h"
                    logger.info(
                        f"  #{rank} | Score: {score:.6f} | "
                        f"{sig.get('direction','')} | Edge: {sig.get('edge',0):.2%} | "
                        f"Conf: {sig.get('confidence',0):.2%} | "
                        f"Resolves: {hours_left} | ${sig.get('market_price', 0):.2f}"
                    )

                # Take trades from top-ranked signals (Kelly + risk decides if we actually can)
                for score, sig, market in scored_signals:
                    market_id = sig.get("market_id", "")
                    if market_id in self.traded_markets:
                        continue
                    trade = self._create_trade(sig)
                    if trade:
                        self.trades.append(trade)
                        trades_taken.append(trade)
                        self.traded_markets.add(market_id)
                        idx = scored_signals.index((score, sig, market)) + 1
                        logger.info(
                            f"  ✅ SELECTED | #{idx} | {trade.direction} | "
                            f"Score: {score:.6f} | Edge: {trade.edge:.2%} | "
                            f"Size: ${trade.position_size:.2f}"
                        )

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

        # Calculate rolling win rate from RESOLVED trades only
        resolved_trades = [t for t in self.trades if t.resolved and t.pnl is not None]
        if resolved_trades:
            recent = resolved_trades[-self.rolling_window:]
            wins = sum(1 for t in recent if t.pnl > 0)
            self.rolling_win_rate = wins / len(recent) if recent else 0.0
        else:
            self.rolling_win_rate = 0.0

        # Log risk status
        status = self.risk.get_status()
        logger.info(
            f"📊 Risk: balance={status['balance']} pnl={status['pnl']} "
            f"drawdown={status['drawdown']} positions={status['open_positions']} "
            f"streak={self.risk.state.consecutive_losses}L/{self.risk.state.consecutive_wins}W "
            f"Rolling Win Rate = {self.rolling_win_rate:.1f}%"
        )

        # Resolve open trades every 10 scans
        if self.scan_count % 10 == 0:
            try:
                # Persist current state before resolution so the resolver works on the
                # full trade set, then reload the updated session into memory.
                self._save_session()
                from bot.resolver import TradeResolver
                resolver = TradeResolver(str(self.data_dir))
                resolve_result = resolver.resolve_session(self.session_id, exchange, self.risk)
                self._load_session(self.session_id)
                if resolve_result.get("resolved_this_pass", 0) > 0:
                    logger.info(
                        f"🔄 Resolved {resolve_result['resolved_this_pass']} trades | "
                        f"Session P&L: ${resolve_result['session_pnl']:+.4f}"
                    )
            except Exception as e:
                logger.debug(f"Resolution pass error: {e}")

        self._save_session()

        return {
            "markets": len(markets),
            "signals": len(signals_found),
            "trades": len(trades_taken),
            "balance": self.balance,
            "total_trades": len(self.trades),
        }

    def _should_trade(self, signal: dict) -> bool:
        normalized = self._normalize_trade_terms(signal)
        if normalized is None:
            return False

        try:
            edge = float(signal.get("edge", 0) or 0)
            confidence = float(signal.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            return False

        market_price = normalized["entry_price"]
        return (
            edge >= self.min_edge and
            confidence >= self.min_confidence and
            market_price <= self.max_entry_price  # entry price cap — no tail risk
        )

    def _create_trade(self, signal: dict) -> Optional[SimTrade]:
        normalized = self._normalize_trade_terms(signal)
        if normalized is None:
            return None

        direction = normalized["direction"]
        entry_price = normalized["entry_price"]
        win_probability = normalized["win_probability"]

        size = self.kelly.calculate(win_probability, entry_price, self.balance)
        if size <= 0:
            logger.info(f"  🛑 Kelly rejected: size={size:.2f} (wp={win_probability:.3f}, ep={entry_price:.3f}, bal={self.balance})")
            return None

        # === Risk Management Check ===
        risk_decision = self.risk.check_trade(signal, size)

        if not risk_decision.approved:
            logger.info(f"  🛑 Risk rejected: {risk_decision.reason}")
            return None

        if risk_decision.warnings:
            for w in risk_decision.warnings:
                logger.debug(f"⚠️  {w}")

        # Use risk-adjusted size
        size = risk_decision.adjusted_size

        # Extract category: use signal's category, or derive from market ID
        # Weather markets: KXHIGHNY-26MAR19-T43 -> KXHIGHNY
        # Split market ID to get the base ticker (city+type prefix)
        raw_category = signal.get("category", "") or ""
        if not raw_category:
            market_id = signal.get("market_id", "")
            # Pattern: KXHIGHNY-26MAR19-T43 -> KXHIGHNY
            parts = market_id.split("-")
            if len(parts) >= 2:
                raw_category = parts[0]  # e.g. KXHIGHNY, KXLOWTCHI

        trade = SimTrade(
            id=f"sim_{self.session_id}_{len(self.trades)+1:04d}",
            timestamp=datetime.now(timezone.utc).isoformat(),
            exchange=signal.get("exchange", "unknown"),
            market_id=signal.get("market_id", ""),
            question=signal.get("question", ""),
            direction=direction,
            model_probability=round(win_probability, 4),
            market_price=round(entry_price, 4),
            edge=signal.get("edge", 0),
            confidence=signal.get("confidence", 0),
            position_size=round(size, 2),
            category=raw_category,
            signals=signal.get("signals", {}),
        )

        # Record with risk manager
        self.risk.record_trade({
            "id": trade.id,
            "question": trade.question,
            "direction": trade.direction,
            "position_size": trade.position_size,
            "market_price": trade.market_price,
        })

        return trade

    def _normalize_trade_terms(self, signal: dict) -> Optional[dict]:
        """Normalize a signal into the purchased contract price and win probability."""
        try:
            raw_price = float(signal.get("market_price", 0) or 0)
            model_prob = float(signal.get("model_probability", 0.5) or 0.5)
        except (TypeError, ValueError):
            return None

        if not (isfinite(raw_price) and isfinite(model_prob)):
            return None
        if not (0 < raw_price < 1):
            return None
        if not (0 <= model_prob <= 1):
            return None

        direction = str(signal.get("direction", "BUY_YES") or "BUY_YES").upper()
        if direction == "BUY_NO":
            same_side_edge = model_prob - raw_price
            flipped_edge = (1 - model_prob) - (1 - raw_price)
            if flipped_edge > same_side_edge:
                entry_price = 1 - raw_price
                win_probability = 1 - model_prob
            else:
                entry_price = raw_price
                win_probability = model_prob
        else:
            direction = "BUY_YES"
            entry_price = raw_price
            win_probability = model_prob

        if not (0 < entry_price < 1):
            return None
        if not (0 <= win_probability <= 1):
            return None

        return {
            "direction": direction,
            "entry_price": entry_price,
            "win_probability": win_probability,
        }

    def _compute_time_adjusted_score(self, signal: dict, market) -> float:
        """
        Compute a time-decay adjusted score for a signal.

        Score = edge × confidence / max(days_to_resolve, 0.5) × correlation_multiplier

        This prioritizes:
        - High edge opportunities
        - High confidence signals
        - Faster-resolving markets (better capital efficiency)
        - Uncorrelated categories (reduces exposure to single-event clusters)

        Minimum 0.5 days prevents same-day markets from getting infinite scores.
        """
        try:
            edge = float(signal.get("edge", 0) or 0)
            confidence = float(signal.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

        if edge <= 0 or confidence <= 0:
            return 0.0

        # Calculate days to resolve
        if market.closes_at:
            now = datetime.now(timezone.utc)
            hours_left = (market.closes_at - now).total_seconds() / 3600
            if hours_left <= 0:
                days = 0.1  # Already closed, near-zero time value
            elif hours_left < 1:
                days = 0.5  # Resolves within hours — minimum cap
            else:
                days = hours_left / 24
        else:
            days = 7  # Default to 1 week if unknown

        days = max(days, 0.5)  # Hard floor at 0.5 days
        base_score = (edge * confidence) / days

        # === CORRELATION GUARD ===
        # Reduce score for markets in categories we already have open positions in
        # This prevents double-exposure to the same event cluster (e.g., two SHIBA markets)
        correlation_multiplier = 1.0
        market_category = getattr(market, 'category', None) or ''

        if market_category:
            # Count how many open trades we already have in this category
            open_in_category = sum(
                1 for t in self.trades
                if not t.resolved and getattr(t, 'category', None) == market_category
            )
            if open_in_category > 0:
                # Progressive penalty: 50% reduction for 1st correlation, 80% for 2nd, block 3rd+
                if open_in_category >= 2:
                    logger.debug(f"  🚫 Correlation blocked: {market_category} ({open_in_category} existing positions)")
                    return 0.0  # Skip — too correlated
                penalty = 0.5 ** open_in_category  # 1st correlation = 0.5x, 2nd = 0.25x
                correlation_multiplier = penalty
                logger.debug(f"  ⚠️  Correlation penalty: {market_category} ({open_in_category} existing) → {penalty:.0%}")

        score = base_score * correlation_multiplier
        return round(score, 6)

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
            "max_entry_price": self.max_entry_price,
            "trades": [asdict(t) for t in self.trades],
            "report": self.report(),
            "consecutive_daily_losses": self.consecutive_daily_losses,
            "last_loss_date": self.last_loss_date,
        }
        with open(session_file, "w") as f:
            json.dump(data, f, indent=2, default=str)

    def check_daily_loss_streak(self) -> tuple[bool, int]:
        """
        Call after each scan to track consecutive losing days.
        Returns (should_alert, consecutive_daily_losses).
        Alert is triggered when 2+ consecutive calendar days end with net losses.
        """
        today = datetime.now(timezone.utc).date()
        original_streak = self.consecutive_daily_losses
        original_last_loss_date = self.last_loss_date

        if not hasattr(self, "_last_check_date"):
            self._last_check_date = today
        if not hasattr(self, "_last_counted_loss_date"):
            self._last_counted_loss_date = (
                datetime.fromisoformat(self.last_loss_date).date()
                if self.last_loss_date else None
            )

        if self._last_check_date == today:
            return self.consecutive_daily_losses >= 2, self.consecutive_daily_losses

        evaluated_day = self._last_check_date
        day_gap = (today - evaluated_day).days
        prior_daily_pnl = self.risk.state.daily_pnl

        if day_gap > 1:
            self.consecutive_daily_losses = 0
            self.last_loss_date = None
            self._last_counted_loss_date = None
            self._last_check_date = today
            if (
                self.consecutive_daily_losses != original_streak or
                self.last_loss_date != original_last_loss_date
            ):
                self._save_session()
            return False, self.consecutive_daily_losses

        if prior_daily_pnl < 0:
            if self._last_counted_loss_date != evaluated_day:
                previous_loss_day = (
                    self._last_counted_loss_date
                    if hasattr(self, "_last_counted_loss_date") else None
                )
                if previous_loss_day and (evaluated_day - previous_loss_day).days == 1:
                    self.consecutive_daily_losses += 1
                else:
                    self.consecutive_daily_losses = 1

                self.last_loss_date = evaluated_day.isoformat()
                self._last_counted_loss_date = evaluated_day
        else:
            self.consecutive_daily_losses = 0
            self.last_loss_date = None
            self._last_counted_loss_date = None

        self._last_check_date = today
        if (
            self.consecutive_daily_losses != original_streak or
            self.last_loss_date != original_last_loss_date
        ):
            self._save_session()
        return self.consecutive_daily_losses >= 2, self.consecutive_daily_losses
