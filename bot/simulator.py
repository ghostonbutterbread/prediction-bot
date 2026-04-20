"""Simulation engine — paper trades with full audit trail."""

import logging
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import Optional

from bot.paper_adapters import (
    LoadedPaperSession,
    SimulatorPaperExecutionAdapter,
    SimulatorPaperResolutionAdapter,
    SimulatorPaperSessionStore,
    SimulatorPaperStateAdapter,
)
from bot.shared_core import (
    AccountState,
    ExecutionResult,
    PaperSessionState,
    PositionState,
    ResolutionEvent,
    TradeContext,
    TradeDecision,
    build_trade_decision,
    normalize_trade_context,
    reason_to_key,
)
from bot.strategies.enhanced import EnhancedStrategyEngine, KellySizer
from bot.trade_audit import (
    enrich_trade_audit_fields,
    summarize_event_performance,
)

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
    decision_trace: dict = field(default_factory=dict)
    category: str = ""      # market category (e.g., KXSHIBA, KXNFLX) for correlation tracking
    reserved_capital: Optional[float] = None
    available_cash_before: Optional[float] = None
    available_cash_after_entry: Optional[float] = None
    settlement_value: Optional[float] = None

    # Resolution (filled in later)
    resolved: bool = False
    outcome: Optional[str] = None  # "YES" or "NO"
    pnl: Optional[float] = None
    resolved_at: Optional[str] = None
    resolution_type: Optional[str] = None
    current_price: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    price_delta: Optional[float] = None
    contracts: Optional[float] = None
    gross_pnl: Optional[float] = None
    fee_paid: Optional[float] = None
    net_pnl: Optional[float] = None
    expected_pnl: Optional[float] = None
    exit_price: Optional[float] = None
    event_key: str = ""
    integrity_status: str = "ok"
    integrity_errors: list[str] = field(default_factory=list)


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
        self.config = config
        self.strategy = EnhancedStrategyEngine(config.get("strategy", {}))
        self.kelly = KellySizer()

        # Risk management
        from bot.risk import RiskManager
        self.risk = RiskManager(config)

        self.starting_balance = config.get("starting_balance", 100.0)
        self.balance = self.starting_balance  # Total equity = available cash + reserved capital
        self.available_cash = self.starting_balance
        self.reserved_capital = 0.0
        strategy_cfg = config.get("strategy", {})
        self.min_edge = config.get("min_edge", strategy_cfg.get("min_edge", 0.01))
        self.min_confidence = config.get("min_confidence", strategy_cfg.get("min_confidence", 0.50))
        self.max_entry_price = config.get("max_entry_price", 0.70)
        self.enable_time_decay_ranking = config.get("enable_time_decay_ranking", True)

        self.data_dir = Path(config.get("data_dir", "data"))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_adapter = SimulatorPaperStateAdapter(self)
        self.session_store = SimulatorPaperSessionStore(self, self.state_adapter)
        self.execution_adapter = SimulatorPaperExecutionAdapter(self)
        self.resolution_adapter = SimulatorPaperResolutionAdapter(self, self.state_adapter, self.session_store)

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
        try:
            loaded = self.session_store.load_session(
                session_id,
                trade_factory=self._hydrate_trade,
                max_entry_price_default=self.max_entry_price,
            )
            if loaded is None:
                return False

            self._apply_loaded_session(loaded)

            logger.info(
                f"Loaded session {self.session_id} — {len(self.trades)} trades, "
                f"equity ${self.balance:.2f} | available ${self.available_cash:.2f} | "
                f"reserved ${self.reserved_capital:.2f}"
            )
            if loaded.discarded_rows:
                logger.warning(
                    f"Discarded {loaded.discarded_rows} zero-sized or malformed trade rows from session {self.session_id}"
                )
            return True

        except Exception as e:
            logger.error(f"Failed to load session {session_id or 'latest'}: {e}")
            return False

    def _apply_loaded_session(self, loaded: LoadedPaperSession) -> None:
        self.session_id = loaded.session_id
        self.starting_balance = loaded.starting_balance
        self.balance = loaded.balance
        self.available_cash = loaded.available_cash
        self.reserved_capital = loaded.reserved_capital
        self.scan_count = loaded.scan_count
        self.max_entry_price = loaded.max_entry_price
        self.consecutive_daily_losses = loaded.consecutive_daily_losses
        self.last_loss_date = loaded.last_loss_date
        self.trades = loaded.trades
        self.traded_markets = loaded.traded_markets
        self.rolling_win_rate = 0.0
        self.rolling_win_count = 0
        self.rolling_loss_count = 0
        self.risk.sync_with_trades(
            self.trades,
            current_balance=self.balance,
            starting_balance=self.starting_balance,
            available_cash=self.available_cash,
            reserved_capital=self.reserved_capital,
        )

    def _hydrate_trade(self, t_data: dict, index: int) -> SimTrade:
        return SimTrade(
            id=t_data.get("id", f"sim_loaded_{index:04d}"),
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
            decision_trace=dict(t_data.get("decision_trace", {}) or {}),
            category=t_data.get("category", ""),
            reserved_capital=self._coerce_float_or_none(t_data.get("reserved_capital")),
            available_cash_before=self._coerce_float_or_none(t_data.get("available_cash_before")),
            available_cash_after_entry=self._coerce_float_or_none(t_data.get("available_cash_after_entry")),
            settlement_value=self._coerce_float_or_none(t_data.get("settlement_value")),
            resolved=t_data.get("resolved", False),
            outcome=t_data.get("outcome"),
            pnl=t_data.get("pnl"),
            resolved_at=t_data.get("resolved_at"),
            resolution_type=t_data.get("resolution_type"),
            current_price=self._coerce_float_or_none(t_data.get("current_price")),
            unrealized_pnl=self._coerce_float_or_none(t_data.get("unrealized_pnl")),
            price_delta=self._coerce_float_or_none(t_data.get("price_delta")),
            contracts=self._coerce_float_or_none(t_data.get("contracts")),
            gross_pnl=self._coerce_float_or_none(t_data.get("gross_pnl")),
            fee_paid=self._coerce_float_or_none(t_data.get("fee_paid")),
            net_pnl=self._coerce_float_or_none(t_data.get("net_pnl")),
            expected_pnl=self._coerce_float_or_none(t_data.get("expected_pnl")),
            exit_price=self._coerce_float_or_none(t_data.get("exit_price")),
            event_key=t_data.get("event_key", ""),
            integrity_status=t_data.get("integrity_status", "ok"),
            integrity_errors=list(t_data.get("integrity_errors", []) or []),
        )

    def scan(self, exchange) -> dict:
        """Run a simulation scan on an exchange."""
        self.scan_count += 1
        self.risk.reset_daily()  # Reset daily trackers if new day
        logger.info(f"\n{'='*60}")
        logger.info(f"Sim Scan #{self.scan_count} at {datetime.now(timezone.utc).strftime('%H:%M:%S')}")

        markets = exchange.get_markets(limit=100)
        if not markets:
            return {"markets": 0, "signals": 0, "trades": 0}
        blockers = Counter()
        
        # === SPORTS MODE: Analyze sports markets + injury sniper ===
        # (sports_trades currently unused — populated but never consumed)
        sports_trades = []
        try:
            from bot.strategies.sports import MarketFilter, QuickBetStrategy

            sports_markets = MarketFilter.filter_sports(markets, max_hours=48)
            if sports_markets:
                logger.info(f"🏀 Found {len(sports_markets)} sports markets (closing within 48h)")

                qb = QuickBetStrategy()
                for sm in sports_markets:
                    if sm.id in self.traded_markets:
                        continue
                    sig = qb.analyze_market(sm)
                    if sig and sig.get("should_trade"):
                        trade = self._create_trade(sig, blockers)
                        if trade:
                            self.trades.append(trade)
                            sports_trades.append(trade)
                            self.traded_markets.add(sm.id)

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
            if self.social_feed:
                # Check for injury alerts in feed (Twitter API)
                alerts = self.social_feed.scan()
                if alerts:
                    injury_signals = []
                    for alert in alerts:
                        sig = InjurySniper.scan_text(alert.source_text, alert.source)
                        if sig:
                            injury_signals.append(sig)
                    for sig in injury_signals:
                        trade = self._create_trade(sig, blockers)
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

                    gate_reason = self._trade_gate_reason(signal)
                    should_trade = gate_reason is None
                    # DEBUG: log every signal
                    logger.info(f"  Signal: {signal.get('direction','')} edge={signal.get('edge',0):.3f} conf={signal.get('confidence',0):.3f} -> {should_trade}")

                    if should_trade:
                        # Dedup: skip if we already traded this market
                        market_id = signal.get("market_id", "")
                        if market_id in self.traded_markets:
                            blockers["duplicate_market"] += 1
                            logger.debug(f"  Skipping duplicate: {market_id}")
                            continue
                        # Attach market object for time-decay scoring
                        signal["_market"] = market

                        if self.enable_time_decay_ranking:
                            # Queue for time-decay ranking — don't trade immediately
                            logger.debug(f"  ⏳ Queued for time-decay ranking: {market_id}")
                        else:
                            # Legacy mode: trade immediately
                            trade = self._create_trade(signal, blockers)
                            if trade:
                                self.trades.append(trade)
                                trades_taken.append(trade)
                                self.traded_markets.add(market_id)
                    else:
                        signal["_blocked"] = gate_reason
                        blockers[gate_reason] += 1

            except Exception as e:
                logger.debug(f"Error analyzing {market.id}: {e}")
                continue

        # === TIME-DECAY RANKING PHASE ===
        # Rank all queued signals by: edge × confidence / days_to_resolve
        # Then take the best opportunities (respecting Kelly sizing + risk management)
        if self.enable_time_decay_ranking:
            scored_signals = []
            for sig in signals_found:
                gate_reason = sig.get("_blocked") or self._trade_gate_reason(sig)
                if gate_reason is not None:
                    continue
                market_id = sig.get("market_id", "")
                if market_id in self.traded_markets:
                    blockers["duplicate_market"] += 1
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
                        blockers["duplicate_market"] += 1
                        continue
                    trade = self._create_trade(sig, blockers)
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
            blocker_summary = self._format_blockers(blockers)
            if blocker_summary:
                logger.info(f"  Top blockers: {blocker_summary}")

        # Calculate rolling win rate from RESOLVED trades only
        resolved_trades = [
            t for t in self._effective_trades()
            if t.resolved and t.pnl is not None and t.integrity_status == "ok"
        ]
        if resolved_trades:
            recent = resolved_trades[-self.rolling_window:]
            wins = sum(1 for t in recent if t.pnl > 0)
            self.rolling_win_rate = wins / len(recent) * 100 if recent else 0.0
        else:
            self.rolling_win_rate = 0.0

        # Log risk status
        status = self.risk.get_status()
        logger.info(
            f"📊 Risk: balance={status['balance']} pnl={status['pnl']} "
            f"available={status['available_cash']} reserved={status['reserved_capital']} "
            f"drawdown={status['drawdown']} positions={status['open_positions']} "
            f"streak={self.risk.state.consecutive_losses}L/{self.risk.state.consecutive_wins}W "
            f"Rolling Win Rate = {self.rolling_win_rate:.1f}%"
        )

        # Auto-resolve open positions every scan.
        # The resolver only settles positions where the market result field is
        # populated — it will NOT close positions on "closed" status alone.
        open_count = sum(1 for t in self.trades if not t.resolved)
        if open_count > 0:
            try:
                resolution_events = self.resolve_open_positions(exchange)
                if resolution_events:
                    resolve_result = dict(self.resolution_adapter.last_summary)
                    logger.info(
                        f"🔄 Resolved {len(resolution_events)} trades | "
                        f"Session P&L: ${resolve_result.get('session_pnl', self.balance - self.starting_balance):+.4f}"
                    )
            except Exception as e:
                logger.debug(f"Resolution pass error: {e}")

        self._save_session()

        return {
            "markets": len(markets),
            "signals": len(signals_found),
            "trades": len(trades_taken),
            "balance": self.balance,
            "total_trades": len(self._effective_trades()),
            "blocked_reasons": dict(blockers),
        }

    def _should_trade(self, signal: dict) -> bool:
        return self._trade_gate_reason(signal) is None

    def _trade_gate_reason(self, signal: dict) -> Optional[str]:
        normalized = self._normalize_trade_terms(signal)
        if normalized is None:
            return "invalid_signal"

        try:
            edge = float(signal.get("edge", 0) or 0)
            confidence = float(signal.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            return "invalid_signal"

        market_price = normalized["entry_price"]
        if edge < self.min_edge:
            return "edge_below_threshold"
        if confidence < self.min_confidence:
            return "confidence_below_threshold"
        if market_price > self.max_entry_price:
            return "entry_price_above_cap"
        return None

    def _create_trade(self, signal: dict, blockers: Optional[Counter] = None) -> Optional[SimTrade]:
        context = self.state_adapter.build_trade_context(signal)
        decision = build_trade_decision(
            context,
            kelly_sizer=self.kelly,
            risk_policy=self.risk,
            min_edge=self.min_edge,
            min_confidence=self.min_confidence,
            max_entry_price=self.max_entry_price,
        )

        if not decision.approved:
            if blockers is not None:
                blockers[decision.reason_code] += 1
            logger.info(f"  🛑 Shared decision skipped: {decision.reason}")
            return None

        if decision.warnings:
            for w in decision.warnings:
                logger.debug(f"⚠️  {w}")

        result = self.execute(decision, context)
        if not result.accepted:
            if blockers is not None:
                blockers[result.metadata.get("reason_code", "execution_rejected")] += 1
            logger.info(f"  🛑 Paper execution skipped: {result.message or result.status}")
            return None

        trade = self._trade_from_execution_result(result)
        enrich_trade_audit_fields(trade.__dict__)
        return trade

    def _trade_from_execution_result(self, result: ExecutionResult) -> SimTrade:
        metadata = dict(result.metadata or {})
        return SimTrade(
            id=result.trade_id,
            timestamp=metadata.get("timestamp", datetime.now(timezone.utc).isoformat()),
            exchange=metadata.get("exchange", "unknown"),
            market_id=metadata.get("market_id", ""),
            question=metadata.get("question", ""),
            direction=result.action,
            model_probability=metadata.get("model_probability", 0.5),
            market_price=round(result.fill_price or 0.0, 4),
            edge=metadata.get("edge", 0),
            confidence=metadata.get("confidence", 0),
            position_size=round(result.filled_size, 2),
            signals=metadata.get("signals", {}),
            decision_trace=dict(metadata.get("decision_trace", {}) or {}),
            category=metadata.get("category", ""),
            reserved_capital=self._coerce_float_or_none(metadata.get("reserved_capital")),
            available_cash_before=self._coerce_float_or_none(metadata.get("available_cash_before")),
            available_cash_after_entry=self._coerce_float_or_none(metadata.get("available_cash_after_entry")),
            contracts=self._coerce_float_or_none(metadata.get("contracts")),
        )

    def _effective_trades(self) -> list[SimTrade]:
        return self.state_adapter.effective_trades()

    def _prune_ineffective_trades(self):
        self.state_adapter.prune_ineffective_trades()

    def _is_trade_effective(self, trade: SimTrade) -> bool:
        return self.state_adapter.is_trade_effective(trade)

    def _is_trade_row_effective(self, trade_data: dict) -> bool:
        return self.state_adapter.is_trade_row_effective(trade_data)

    def _coerce_float_or_none(self, value) -> Optional[float]:
        return self.state_adapter.coerce_float_or_none(value)

    def _trade_reserved_amount(self, trade: SimTrade) -> float:
        return self.state_adapter.trade_reserved_amount(trade)

    def _refresh_capital_state(self):
        self.state_adapter.refresh_capital_state()

    def _reason_key(self, reason: str) -> str:
        return reason_to_key(reason, prefix="risk")

    def _format_blockers(self, blockers: Counter, limit: int = 4) -> str:
        if not blockers:
            return ""
        return ", ".join(f"{key}={count}" for key, count in blockers.most_common(limit))

    def _normalize_trade_terms(self, signal: dict) -> Optional[dict]:
        return normalize_trade_context(self.state_adapter.build_trade_context(signal))

    def get_account_state(self) -> AccountState:
        """Return the current paper account snapshot for the shared core."""
        return self.state_adapter.get_account_state()

    def list_open_positions(self) -> list[PositionState]:
        """Return paper positions in a mode-agnostic adapter shape."""
        return self.state_adapter.list_open_positions()

    def get_paper_session_state(self) -> PaperSessionState:
        """Return paper-only session metadata for adapter consumers."""
        return self.state_adapter.get_paper_session_state()

    def _build_trade_context(self, signal: dict) -> TradeContext:
        """Map the current paper signal into the shared trade context."""
        return self.state_adapter.build_trade_context(signal)

    def execute(self, decision: TradeDecision, context: TradeContext) -> ExecutionResult:
        """Execute a shared decision through the paper execution adapter."""

        return self.execution_adapter.execute(decision, context)

    def resolve_open_positions(self, settlement_source=None) -> list[ResolutionEvent]:
        """Resolve paper positions through the paper resolution adapter."""

        return self.resolution_adapter.resolve_open_positions(settlement_source)

    def _apply_fill_slippage(self, entry_price: float, size: float,
                              signal: dict, direction: str) -> float:
        return self.execution_adapter.apply_fill_slippage(entry_price, size, signal, direction)

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
        self._refresh_capital_state()
        effective_trades = self._effective_trades()
        total = len(effective_trades)
        if total == 0:
            return {
                "session": self.session_id,
                "total_trades": 0,
                "message": "No trades yet. Run more scans.",
            }

        # Stats
        edges = [t.edge for t in effective_trades]
        confidences = [t.confidence for t in effective_trades]
        sizes = [t.position_size for t in effective_trades]

        by_direction = {}
        for t in effective_trades:
            by_direction[t.direction] = by_direction.get(t.direction, 0) + 1

        by_exchange = {}
        for t in effective_trades:
            by_exchange[t.exchange] = by_exchange.get(t.exchange, 0) + 1

        resolved_positions = [t for t in effective_trades if t.resolved]
        trusted_resolved = [
            t for t in resolved_positions if t.integrity_status == "ok" and t.pnl is not None
        ]
        event_summary = summarize_event_performance([asdict(t) for t in trusted_resolved])

        return {
            "session": self.session_id,
            "started_at": effective_trades[0].timestamp if effective_trades else None,
            "total_trades": total,
            "resolved_trades": len(resolved_positions),
            "trusted_resolved_trades": len(trusted_resolved),
            "invalid_resolved_trades": len(resolved_positions) - len(trusted_resolved),
            "starting_balance": self.starting_balance,
            "current_balance": self.balance,
            "total_equity": self.balance,
            "available_cash": round(self.available_cash, 2),
            "reserved_capital": round(self.reserved_capital, 2),
            "pnl": round(self.balance - self.starting_balance, 2),
            "pnl_pct": round((self.balance - self.starting_balance) / self.starting_balance * 100, 2),
            "avg_edge": round(sum(edges) / len(edges), 4),
            "max_edge": round(max(edges), 4),
            "avg_confidence": round(sum(confidences) / len(confidences), 4),
            "avg_position_size": round(sum(sizes) / len(sizes), 2),
            "total_exposure": round(self.reserved_capital, 2),
            "by_direction": by_direction,
            "by_exchange": by_exchange,
            "resolved_events": event_summary["resolved_events"],
            "event_win_rate": event_summary["win_rate"],
            "scans_run": self.scan_count,
        }

    def get_open_trades(self) -> list[dict]:
        """Get all unresolved trades."""
        return [asdict(t) for t in self._effective_trades() if not t.resolved]

    def get_all_trades(self) -> list[dict]:
        """Get all trades."""
        return [asdict(t) for t in self._effective_trades()]

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
Total Equity:      ${r['current_balance']:.2f}
Available Cash:    ${r['available_cash']:.2f}
Reserved Capital:  ${r['reserved_capital']:.2f}
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
        self.session_store.save_session()

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
