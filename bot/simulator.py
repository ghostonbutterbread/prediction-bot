"""Simulation engine — paper trades with full audit trail."""

import logging
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dataclasses import dataclass, asdict, field
from types import SimpleNamespace
from typing import Any, Optional

from bot.config import ensure_mode_storage_dir
from bot.decision_pipeline import build_pre_execution_decision_artifact
from bot.file_ops import load_jsonl
from bot.market_router import DEFAULT_ALLOWED_MARKET_ROUTES
from bot.paper_shadow_lanes import (
    SOURCE_SCOREBOARD_LANE_ID,
    paper_shadow_lanes_enabled,
    requested_paper_shadow_lane_ids,
    write_paper_shadow_lane_decisions,
)
from bot.paper_wallets import BETA_PAPER_WALLET_ID, STABLE_PAPER_WALLET_ID
from bot.prediction_lab_shadow_delta import build_shadow_delta
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
    append_hypothetical_shadow_intent_row,
    build_trade_decision,
    build_execution_snapshot,
    normalize_trade_context,
    reason_to_key,
)
from bot.shared_core.decision import HIDDEN_GEM_ENTRY_PRICE_CAP
from bot.shared_market_feed import build_shared_market_candidate_row
from bot.strategy_lanes import select_strategy_lane
from bot.strategies.enhanced import EnhancedStrategyEngine, KellySizer, strategy_config_with_policy
from bot.parity_audit import normalize_parity_trade_row, summarize_normalized_rows
from bot.paper_decision_audit import append_paper_agent_run_once, append_paper_decision_audit
from bot.shared_market_runtime import (
    SharedMarketRuntimeManager,
    build_shared_market_snapshot_metadata,
    shared_snapshot_is_fresh,
)
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
    status: str = "filled"
    lifecycle_state: Optional[str] = None
    failure_stage: Optional[str] = None
    decision_reason: Optional[str] = None
    decision_reason_code: Optional[str] = None
    requested_size: Optional[float] = None
    approved_size: Optional[float] = None
    placed_size: Optional[float] = None
    filled_size: Optional[float] = None
    remaining_size: Optional[float] = None
    entry_price: Optional[float] = None
    parity_mode_enabled: bool = False
    execution_revalidated: bool = False
    execution_revalidation_outcome: Optional[str] = None
    original_signal_snapshot: Optional[dict] = None
    execution_snapshot: Optional[dict] = None
    market_route: Optional[dict] = None
    original_decision_reason_code: Optional[str] = None
    execution_decision_reason_code: Optional[str] = None
    execution_snapshot_source: Optional[str] = None
    decision_artifact: Optional[dict] = None

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
        scan_cfg = config.setdefault("scan", {})
        scan_cfg.setdefault("allowed_market_routes", list(DEFAULT_ALLOWED_MARKET_ROUTES))
        self.config = config
        self.strategy = EnhancedStrategyEngine(strategy_config_with_policy(config))
        economics_cfg = config.get("trade_economics", {}) or {}
        self.kelly = KellySizer(
            fee_rate=config.get("kalshi_fee_rate"),
            min_position_size_usd=economics_cfg.get("min_position_size_usd", 1.0),
            min_expected_net_profit_usd=economics_cfg.get("min_expected_net_profit_usd", 0.0),
        )

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
        self.single_trade_mode = bool(config.get("trading", {}).get("single_trade_mode", False))
        self.single_trade_completed = False
        self.parity_mode = dict(config.get("parity_mode", {}) or {})

        runtime_mode = str(config.get("trading", {}).get("mode", "paper"))
        self.runtime_mode = "live" if runtime_mode.strip().lower() == "live" else "paper"
        self.data_dir = ensure_mode_storage_dir(config.get("data_dir", "data"), self.runtime_mode)
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
            self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
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
        self._safe_append_agent_run()

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
        self.state_adapter.refresh_traded_markets()
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
        startup_standby = self.risk.reconcile_startup_standby()
        if startup_standby.get("asserted"):
            logger.info(
                "⏸️  Startup standby asserted | reasons=%s | useful_capacity=$%.2f",
                ",".join(startup_standby.get("reason_codes", [])) or "unknown",
                startup_standby.get("useful_trade_capacity", 0.0),
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
            status=t_data.get("status", "filled"),
            lifecycle_state=t_data.get("lifecycle_state"),
            failure_stage=t_data.get("failure_stage"),
            decision_reason=t_data.get("decision_reason"),
            decision_reason_code=t_data.get("decision_reason_code"),
            requested_size=self._coerce_float_or_none(t_data.get("requested_size")),
            approved_size=self._coerce_float_or_none(t_data.get("approved_size")),
            placed_size=self._coerce_float_or_none(t_data.get("placed_size")),
            filled_size=self._coerce_float_or_none(t_data.get("filled_size")),
            remaining_size=self._coerce_float_or_none(t_data.get("remaining_size")),
            entry_price=self._coerce_float_or_none(t_data.get("entry_price")),
            parity_mode_enabled=bool(t_data.get("parity_mode_enabled", False)),
            execution_revalidated=bool(t_data.get("execution_revalidated", False)),
            execution_revalidation_outcome=t_data.get("execution_revalidation_outcome"),
            original_signal_snapshot=t_data.get("original_signal_snapshot"),
            execution_snapshot=t_data.get("execution_snapshot"),
            market_route=t_data.get("market_route"),
            original_decision_reason_code=t_data.get("original_decision_reason_code"),
            execution_decision_reason_code=t_data.get("execution_decision_reason_code"),
            execution_snapshot_source=t_data.get("execution_snapshot_source"),
            decision_artifact=t_data.get("decision_artifact"),
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

        standby_status = self.risk.evaluate_standby_resume()
        if standby_status.get("standby_active"):
            logger.info(
                "⏸️  Standby active | reasons=%s | useful_capacity=$%.2f",
                ",".join(standby_status.get("standby_reason_codes", [])) or "unknown",
                standby_status.get("useful_trade_capacity", 0.0),
            )
            resolution_events = self.resolve_open_positions(exchange)
            if resolution_events:
                standby_status = self.risk.evaluate_standby_resume()
                if standby_status.get("resumed"):
                    logger.info("▶️  Standby cleared after resolutions | %s", standby_status.get("resume_reason", ""))
            self._save_session()
            return {
                "markets": 0,
                "signals": 0,
                "trades": 0,
                "balance": self.balance,
                "total_trades": len(self._effective_trades()),
                "blocked_reasons": {},
                "standby": {
                    "active": bool(standby_status.get("standby_active")),
                    "resumed": bool(standby_status.get("resumed")),
                    "reason_codes": list(standby_status.get("standby_reason_codes", [])),
                    "resume_reason": standby_status.get("resume_reason", ""),
                },
            }

        shared_market_context = self._begin_paper_shared_market_runtime()
        if shared_market_context and shared_market_context.get("skip_direct_fetch"):
            return self._paper_shared_market_skip_result(shared_market_context)

        scan_cfg = self.config.get("scan", {}) or {}
        markets_per_exchange = int(scan_cfg.get("markets_per_exchange", 100) or 100)
        try:
            markets = exchange.get_markets(limit=markets_per_exchange)
        except Exception:
            self._finish_paper_shared_market_runtime(shared_market_context)
            raise
        shared_market_publish_metadata = self._record_paper_shared_market_snapshot_metadata(
            shared_market_context,
            markets=markets,
            exchange=exchange,
        )
        self._finish_paper_shared_market_runtime(shared_market_context)
        if not markets:
            result = {"markets": 0, "signals": 0, "trades": 0}
            return self._finish_paper_shared_market_result(
                result,
                shared_market_context,
                publish_metadata=shared_market_publish_metadata,
            )
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
        shadow_source_scoreboard_inputs: dict[str, dict[str, Any]] = {}

        for market in markets:
            try:
                # Build order book from market-level bid/ask
                yes_price = self._coerce_float_or_none(getattr(market, "yes_price", None))
                no_price = self._coerce_float_or_none(getattr(market, "no_price", None))
                order_book = {
                    "best_yes_ask": yes_price,
                    "best_yes_bid": max(0, round(yes_price - 0.01, 4)) if yes_price is not None else None,
                    "best_no_ask": no_price,
                    "best_no_bid": max(0, round(no_price - 0.01, 4)) if no_price is not None else None,
                    "mid_yes": yes_price,
                    "spread": 0.01,
                    "spread_pct": (0.01 / yes_price * 100) if yes_price and yes_price > 0 else 10,
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
                    self._annotate_signal_shared_market_provenance(
                        signal,
                        shared_market_context,
                        publish_metadata=shared_market_publish_metadata,
                    )
                    self._prepare_shadow_source_scoreboard_signal(
                        signal,
                        market=market,
                        order_book=order_book,
                        inputs_by_shared_candidate_id=shadow_source_scoreboard_inputs,
                    )
                    signals_found.append(signal)

                    gate_reason = self._trade_gate_reason(signal)
                    should_trade = gate_reason is None
                    # DEBUG: log every signal
                    logger.info(f"  Signal: {signal.get('direction','')} edge={signal.get('edge',0):.3f} conf={signal.get('confidence',0):.3f} -> {should_trade}")

                    if should_trade:
                        if self.single_trade_mode and self.single_trade_completed:
                            blockers["single_trade_mode_completed"] += 1
                            continue
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
                                if self.single_trade_mode:
                                    self.single_trade_completed = True
                                    break
                    else:
                        signal["_blocked"] = gate_reason
                        blockers[gate_reason] += 1
                        self._append_shadow_intent_for_stable_skip(signal, gate_reason)

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
                    if self.single_trade_mode and self.single_trade_completed:
                        break
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
                        if self.single_trade_mode:
                            self.single_trade_completed = True
                            break

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

        self.risk.record_blocked_scan(dict(blockers), trades_taken=len(trades_taken))
        self._safe_write_shadow_source_scoreboard_lane_decisions(shadow_source_scoreboard_inputs)
        self._save_session()

        result = {
            "markets": len(markets),
            "signals": len(signals_found),
            "trades": len(trades_taken),
            "balance": self.balance,
            "total_trades": len(self._effective_trades()),
            "blocked_reasons": dict(blockers),
            "standby": {
                "active": bool(self.risk.state.standby_active),
                "reason_codes": list(self.risk.state.standby_reason_codes),
                "blocked_scan_count": self.risk.state.standby_blocked_scan_count,
            },
        }
        return self._finish_paper_shared_market_result(
            result,
            shared_market_context,
            publish_metadata=shared_market_publish_metadata,
        )

    def _paper_shared_market_config(self) -> dict[str, Any]:
        paper_cfg = dict(self.config.get("paper", {}) or {})
        paper_runtime_cfg = self.config.get("paper_runtime")
        if isinstance(paper_runtime_cfg, dict):
            paper_cfg.update(paper_runtime_cfg)
        return paper_cfg

    def _paper_shared_market_enabled(self) -> bool:
        if self.runtime_mode != "paper":
            return False
        paper_cfg = self._paper_shared_market_config()
        shared_market_cfg = self.config.get("shared_market", {}) or {}
        return bool(paper_cfg.get("shared_market_runtime_enabled", False)) and bool(
            shared_market_cfg.get("enabled", True)
        )

    def _paper_shared_market_instance_id(self) -> str:
        paper_cfg = self._paper_shared_market_config()
        instance_id = paper_cfg.get("shared_market_runtime_instance_id")
        if instance_id not in (None, ""):
            return str(instance_id)
        return f"paper:{self.data_dir.resolve()}"

    def _paper_shared_market_max_snapshot_age_seconds(self) -> int:
        paper_cfg = self._paper_shared_market_config()
        shared_market_cfg = self.config.get("shared_market", {}) or {}
        value = (
            paper_cfg.get("shared_market_max_snapshot_age_seconds")
            or shared_market_cfg.get("snapshot_ttl_seconds")
            or 1200
        )
        return max(1, int(value))

    def _paper_shared_market_desired_interval_seconds(self) -> int:
        paper_cfg = self._paper_shared_market_config()
        shared_market_cfg = self.config.get("shared_market", {}) or {}
        value = (
            paper_cfg.get("shared_market_desired_interval_seconds")
            or shared_market_cfg.get("default_interval_seconds")
            or 900
        )
        return max(1, int(value))

    def _begin_paper_shared_market_runtime(self) -> dict[str, Any] | None:
        if not self._paper_shared_market_enabled():
            return None

        manager = SharedMarketRuntimeManager(config=self.config)
        instance_id = self._paper_shared_market_instance_id()
        max_snapshot_age_seconds = self._paper_shared_market_max_snapshot_age_seconds()
        desired_interval_seconds = self._paper_shared_market_desired_interval_seconds()
        now = datetime.now(timezone.utc)
        manager.attach(
            runtime_kind="paper",
            instance_id=instance_id,
            can_publish=True,
            can_consume=True,
            desired_interval_seconds=desired_interval_seconds,
            max_snapshot_age_seconds=max_snapshot_age_seconds,
            now=now,
        )
        state = manager.acquire_publisher_lease(
            runtime_kind="paper",
            instance_id=instance_id,
            now=now,
        )
        publisher = state.get("publisher") if isinstance(state, dict) else None
        latest_snapshot = state.get("latest_snapshot") if isinstance(state, dict) else None
        owns_publisher = self._shared_market_publisher_is_self(
            publisher,
            runtime_kind="paper",
            instance_id=instance_id,
        )
        other_publisher = isinstance(publisher, dict) and not owns_publisher
        fresh_shared_snapshot = (
            other_publisher
            and self._shared_market_snapshot_matches_publisher(latest_snapshot, publisher)
            and shared_snapshot_is_fresh(
                latest_snapshot,
                max_snapshot_age_seconds=max_snapshot_age_seconds,
                now=now,
            )
        )
        # Paper can only skip its direct upstream fetch when the current shared
        # publisher has a fresh snapshot that matches the active lease. Missing,
        # stale, or mismatched snapshots must fall back to the legacy direct
        # paper scan path so paper does not silently stop observing markets.
        skip_direct_fetch = bool(fresh_shared_snapshot)
        if fresh_shared_snapshot:
            provenance = "shared"
            skip_reason = "fresh_shared_snapshot_owned_by_other_publisher"
        elif owns_publisher:
            provenance = "direct_publisher"
            skip_reason = None
        else:
            provenance = "direct_bypass"
            skip_reason = None

        return {
            "enabled": True,
            "manager": manager,
            "instance_id": instance_id,
            "state": state,
            "publisher": dict(publisher) if isinstance(publisher, dict) else None,
            "latest_snapshot": dict(latest_snapshot) if isinstance(latest_snapshot, dict) else None,
            "owns_publisher": owns_publisher,
            "fresh_shared_snapshot": bool(fresh_shared_snapshot),
            "skip_direct_fetch": skip_direct_fetch,
            "skip_reason": skip_reason,
            "provenance": provenance,
            "max_snapshot_age_seconds": max_snapshot_age_seconds,
            "desired_interval_seconds": desired_interval_seconds,
        }

    @staticmethod
    def _shared_market_publisher_is_self(
        publisher: dict[str, Any] | None,
        *,
        runtime_kind: str,
        instance_id: str,
    ) -> bool:
        if not isinstance(publisher, dict):
            return False
        return (
            str(publisher.get("runtime_kind") or "") == runtime_kind
            and str(publisher.get("instance_id") or "") == instance_id
        )

    @staticmethod
    def _shared_market_snapshot_matches_publisher(
        snapshot: dict[str, Any] | None,
        publisher: dict[str, Any] | None,
    ) -> bool:
        if not isinstance(snapshot, dict) or not isinstance(publisher, dict):
            return False
        return (
            str(snapshot.get("publisher_runtime") or "") == str(publisher.get("runtime_kind") or "")
            and str(snapshot.get("publisher_instance_id") or "") == str(publisher.get("instance_id") or "")
        )

    def _paper_shared_market_skip_result(self, context: dict[str, Any]) -> dict:
        snapshot = dict(context.get("latest_snapshot") or {})
        logger.info(
            "Skipping paper direct market fetch because shared market feed is owned by %s:%s",
            snapshot.get("publisher_runtime") or (context.get("publisher") or {}).get("runtime_kind"),
            snapshot.get("publisher_instance_id") or (context.get("publisher") or {}).get("instance_id"),
        )
        self.risk.record_blocked_scan({}, trades_taken=0)
        self._save_session()
        result = {
            "markets": int(snapshot.get("market_count") or snapshot.get("candidate_count") or 0),
            "signals": 0,
            "trades": 0,
            "balance": self.balance,
            "total_trades": len(self._effective_trades()),
            "blocked_reasons": {},
            "standby": {
                "active": bool(self.risk.state.standby_active),
                "reason_codes": list(self.risk.state.standby_reason_codes),
                "blocked_scan_count": self.risk.state.standby_blocked_scan_count,
            },
        }
        return self._finish_paper_shared_market_result(result, context)

    def _record_paper_shared_market_snapshot_metadata(
        self,
        context: dict[str, Any] | None,
        *,
        markets: list[Any] | tuple[Any, ...] | None,
        exchange: Any,
    ) -> dict[str, Any] | None:
        if not context or not context.get("owns_publisher"):
            return None
        manager = context.get("manager")
        if not isinstance(manager, SharedMarketRuntimeManager):
            return None
        observed_at = datetime.now(timezone.utc)
        metadata = build_shared_market_snapshot_metadata(
            snapshot_id=f"{self.session_id}:scan-{self.scan_count}",
            observed_at=observed_at,
            published_at=observed_at,
            publisher_runtime="paper",
            publisher_instance_id=str(context.get("instance_id") or ""),
            candidate_count=len(markets or []),
            market_count=len(markets or []),
            ttl_seconds=int(manager.snapshot_ttl_seconds),
            source_exchange=self._shared_market_source_exchange(exchange),
        )
        try:
            manager.record_snapshot_metadata(metadata, now=observed_at)
        except Exception as exc:
            logger.warning("failed to record paper shared market snapshot metadata: %s", exc)
            return None
        context["published_snapshot"] = metadata
        return metadata

    @staticmethod
    def _shared_market_source_exchange(exchange: Any) -> str:
        for attr in ("name", "exchange_name", "exchange"):
            value = getattr(exchange, attr, None)
            if value not in (None, ""):
                return str(value)
        return "kalshi"

    def _annotate_signal_shared_market_provenance(
        self,
        signal: dict,
        context: dict[str, Any] | None,
        *,
        publish_metadata: dict[str, Any] | None = None,
    ) -> None:
        if not context:
            return
        snapshot = publish_metadata or context.get("latest_snapshot") or {}
        signal["shared_market_provenance"] = self._paper_shared_market_provenance(context, snapshot=snapshot)

    def _paper_shared_market_provenance(
        self,
        context: dict[str, Any],
        *,
        snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot = dict(snapshot or {})
        publisher = dict(context.get("publisher") or {})
        return {
            "enabled": True,
            "source": str(context.get("provenance") or "direct_bypass"),
            "skip_reason": context.get("skip_reason"),
            "snapshot_id": snapshot.get("snapshot_id"),
            "snapshot_as_of": snapshot.get("observed_at") or snapshot.get("published_at"),
            "publisher_runtime": snapshot.get("publisher_runtime") or publisher.get("runtime_kind"),
            "publisher_instance_id": snapshot.get("publisher_instance_id") or publisher.get("instance_id"),
            "fresh_shared_snapshot": bool(context.get("fresh_shared_snapshot", False)),
            "owns_publisher": bool(context.get("owns_publisher", False)),
        }

    def _finish_paper_shared_market_result(
        self,
        result: dict,
        context: dict[str, Any] | None,
        *,
        publish_metadata: dict[str, Any] | None = None,
    ) -> dict:
        if context:
            snapshot = publish_metadata or context.get("latest_snapshot") or {}
            result = dict(result)
            result["shared_market"] = self._paper_shared_market_provenance(context, snapshot=snapshot)
        self._finish_paper_shared_market_runtime(context)
        return result

    def _finish_paper_shared_market_runtime(self, context: dict[str, Any] | None) -> None:
        if not context or context.get("_detached"):
            return
        manager = context.get("manager")
        instance_id = context.get("instance_id")
        if isinstance(manager, SharedMarketRuntimeManager) and instance_id not in (None, ""):
            try:
                manager.detach(runtime_kind="paper", instance_id=str(instance_id))
            except Exception as exc:
                logger.debug("paper shared market detach failed: %s", exc)
        context["_detached"] = True

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
        strategy_lane = select_strategy_lane(
            entry_price=market_price,
            win_probability=float(normalized["win_probability"]),
            edge=edge,
            confidence=confidence,
            min_edge=float(self.min_edge),
            min_confidence=float(self.min_confidence),
            hidden_gem_entry_price_cap=HIDDEN_GEM_ENTRY_PRICE_CAP,
            config=dict(self.config.get("strategy_lanes", {}) or {}),
            strategy_policy=dict(self.config.get("strategy_policy_normalized") or self.config.get("strategy_policy") or {}),
        )
        if not strategy_lane.allowed:
            return strategy_lane.reason_code
        if edge < strategy_lane.effective_min_edge:
            return "edge_below_threshold"
        if confidence < strategy_lane.effective_min_confidence:
            return "confidence_below_threshold"
        if market_price > self.max_entry_price:
            return "entry_price_above_cap"
        return None

    def _create_trade(self, signal: dict, blockers: Optional[Counter] = None) -> Optional[SimTrade]:
        if self.single_trade_mode and self.single_trade_completed:
            if blockers is not None:
                blockers["single_trade_mode_completed"] += 1
            return None

        context = self.state_adapter.build_trade_context(signal)
        decision = build_trade_decision(
            context,
            kelly_sizer=self.kelly,
            risk_policy=self.risk,
            min_edge=self.min_edge,
            min_confidence=self.min_confidence,
            max_entry_price=self.max_entry_price,
        )

        original_decision = decision
        original_context = context
        original_signal_snapshot = dict(signal)
        execution_snapshot = None
        execution_decision = None
        execution_revalidation_outcome = None

        if self.parity_mode.get("enabled"):
            execution_snapshot = build_execution_snapshot(
                signal,
                direction=str(signal.get("direction", "BUY_YES") or "BUY_YES").upper(),
                bid_ask={
                    "best_yes_ask": signal.get("best_yes_ask"),
                    "best_no_ask": signal.get("best_no_ask"),
                    "best_yes_bid": signal.get("best_yes_bid"),
                    "best_no_bid": signal.get("best_no_bid"),
                },
                fallback_to_signal_prices=bool(self.parity_mode.get("fallback_to_signal_prices", True)),
            )
            if execution_snapshot.get("source") == "missing" and self.parity_mode.get("require_book_prices"):
                if blockers is not None:
                    blockers["parity_book_prices_required"] += 1
                logger.info("  🛑 Parity revalidation skipped: book prices required but missing")
                return None

            context = self.state_adapter.build_trade_context_from_snapshot(signal, execution_snapshot=execution_snapshot)
            decision = build_trade_decision(
                context,
                kelly_sizer=self.kelly,
                risk_policy=self.risk,
                min_edge=self.min_edge,
                min_confidence=self.min_confidence,
                max_entry_price=self.max_entry_price,
            )
            execution_decision = decision
            execution_revalidation_outcome = "approved" if decision.approved else "rejected"

        decision.reasoning = dict(decision.reasoning or {})
        if isinstance(signal.get("shared_market_provenance"), dict):
            decision.reasoning["shared_market"] = dict(signal["shared_market_provenance"])
        decision.reasoning["parity_mode"] = {
            "enabled": bool(self.parity_mode.get("enabled")),
            "execution_revalidated": bool(self.parity_mode.get("enabled")),
            "execution_revalidation_outcome": execution_revalidation_outcome,
            "execution_snapshot_source": (execution_snapshot or {}).get("source"),
            "original_signal_snapshot": original_signal_snapshot if self.parity_mode.get("record_revalidation_snapshot", True) else None,
            "execution_snapshot": execution_snapshot if self.parity_mode.get("record_revalidation_snapshot", True) else None,
            "original_decision_reason_code": getattr(original_decision, "reason_code", None),
            "execution_decision_reason_code": getattr(execution_decision, "reason_code", None),
            "original_entry_price": getattr(original_decision, "entry_price", None),
            "execution_entry_price": getattr(execution_decision, "entry_price", None),
        }
        decision_artifact = build_pre_execution_decision_artifact(
            mode="paper_portfolio",
            context=context,
            decision=decision,
            signal=signal,
            execution_snapshot=execution_snapshot,
            config_snapshot=self.config,
        )
        self._append_shadow_intent_if_any(decision_artifact, signal)

        if not decision.approved:
            if blockers is not None:
                blockers[decision.reason_code] += 1
            logger.info(f"  🛑 Shared decision skipped: {decision.reason}")
            self._safe_append_agent_decision(signal, decision_artifact, accounting_mutated=False)
            if self.parity_mode.get("enabled"):
                rejected_trade = self._trade_from_execution_rejection(decision, context)
                rejected_trade.decision_artifact = decision_artifact
                return rejected_trade
            return None

        if decision.warnings:
            for w in decision.warnings:
                logger.debug(f"⚠️  {w}")

        result = self.execute(decision, context)
        if not result.accepted:
            if blockers is not None:
                blockers[result.metadata.get("reason_code", "execution_rejected")] += 1
            logger.info(f"  🛑 Paper execution skipped: {result.message or result.status}")
            self._safe_append_agent_decision(signal, decision_artifact, execution_result=result, accounting_mutated=False)
            return None

        trade = self._trade_from_execution_result(result)
        trade.decision_artifact = decision_artifact
        self._safe_append_agent_decision(
            signal,
            decision_artifact,
            execution_result=result,
            trade_id=trade.id,
            accounting_mutated=True,
        )
        enrich_trade_audit_fields(trade.__dict__)
        if self.single_trade_mode:
            self.single_trade_completed = True
        return trade

    def submit_paper_signal(self, signal: dict, blockers: Optional[Counter] = None, *, persist: bool = True) -> Optional[SimTrade]:
        """Public paper execution seam for callers that submit one prepared signal.

        This owns the simulator bookkeeping that used to be duplicated by
        callers: decision/execution, appending accepted trades, refreshing the
        traded market set, and optional persistence.
        """
        trade = self._create_trade(signal, blockers)
        if trade is None:
            if persist:
                self._save_session()
            return None
        self.trades.append(trade)
        market_id = str(signal.get("market_id") or getattr(trade, "market_id", "") or "")
        if market_id:
            self.traded_markets.add(market_id)
        if persist:
            self._save_session()
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
            event_key=metadata.get("event_key", ""),
            status="filled",
            lifecycle_state="filled_open",
            decision_reason=getattr(result, "message", None),
            decision_reason_code=(metadata.get("reason_code") or (metadata.get("decision_trace", {}) or {}).get("reason_code") or "approved"),
            requested_size=self._coerce_float_or_none(result.requested_size),
            approved_size=round(float(metadata.get("reserved_capital") or result.filled_size or 0.0), 2),
            placed_size=round(float(result.filled_size or 0.0), 2),
            filled_size=round(float(result.filled_size or 0.0), 2),
            remaining_size=self._coerce_float_or_none(result.remaining_size),
            entry_price=round(result.fill_price or 0.0, 4),
            parity_mode_enabled=bool(metadata.get("parity_mode_enabled", False)),
            execution_revalidated=bool(metadata.get("execution_revalidated", False)),
            execution_revalidation_outcome=metadata.get("execution_revalidation_outcome"),
            original_signal_snapshot=metadata.get("original_signal_snapshot"),
            execution_snapshot=metadata.get("execution_snapshot"),
            market_route=metadata.get("market_route"),
            original_decision_reason_code=metadata.get("original_decision_reason_code"),
            execution_decision_reason_code=metadata.get("execution_decision_reason_code"),
            execution_snapshot_source=metadata.get("execution_snapshot_source"),
            decision_artifact=metadata.get("decision_artifact"),
        )

    def _trade_from_execution_rejection(self, decision: TradeDecision, context: TradeContext) -> SimTrade:
        parity = dict((decision.reasoning or {}).get("parity_mode", {}) or {})
        requested_size = float(getattr(decision, "requested_position_size", 0.0) or 0.0)
        approved_size = float(getattr(decision, "position_size", 0.0) or 0.0)
        entry_price = round(float(decision.entry_price or context.market_price or 0.0), 4)
        trade_direction = decision.action if str(getattr(decision, "action", "")).startswith("BUY_") else (
            parity.get("execution_snapshot", {}) or {}
        ).get("direction") or context.source_context.get("direction") or context.direction
        return SimTrade(
            id=f"rejected_{self.session_id}_{len(self.trades) + 1:04d}",
            timestamp=datetime.now(timezone.utc).isoformat(),
            exchange=context.exchange,
            market_id=context.market_id,
            question=context.question,
            direction=trade_direction,
            model_probability=round(float(decision.win_probability or 0.0), 4),
            market_price=entry_price,
            edge=decision.edge or 0,
            confidence=decision.confidence or 0,
            position_size=0.0,
            signals=dict(context.source_context.get("signals", {}) or {}),
            decision_trace=dict(decision.reasoning or {}),
            category=context.metadata.get("category", ""),
            reserved_capital=0.0,
            available_cash_before=round(context.account_state.available_cash, 2),
            available_cash_after_entry=round(context.account_state.available_cash, 2),
            contracts=0.0,
            event_key=context.metadata.get("event_key", ""),
            status="rejected",
            lifecycle_state="revalidation_rejected" if parity.get("enabled") else "risk_check_rejected",
            failure_stage="revalidation" if parity.get("enabled") else "risk_check",
            decision_reason=decision.reason,
            decision_reason_code=decision.reason_code,
            requested_size=requested_size,
            approved_size=approved_size,
            placed_size=0.0,
            filled_size=0.0,
            remaining_size=0.0,
            entry_price=entry_price,
            parity_mode_enabled=bool(parity.get("enabled", False)),
            execution_revalidated=bool(parity.get("execution_revalidated", False)),
            execution_revalidation_outcome=parity.get("execution_revalidation_outcome"),
            original_signal_snapshot=parity.get("original_signal_snapshot"),
            execution_snapshot=parity.get("execution_snapshot"),
            market_route=context.metadata.get("market_route"),
            original_decision_reason_code=parity.get("original_decision_reason_code"),
            execution_decision_reason_code=parity.get("execution_decision_reason_code"),
            execution_snapshot_source=parity.get("execution_snapshot_source"),
            integrity_status="execution_rejected",
            integrity_errors=[decision.reason_code],
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

    def _append_shadow_intent_for_stable_skip(self, signal: dict, reason_code: str | None = None) -> dict | None:
        """Record beta-shadow evidence for stable paper signals blocked before trade creation.

        This is audit-only: it builds a pre-execution artifact and writes only to
        shadow_intents.jsonl. It must not append trades, reserve capital, or touch
        paper risk/PnL state.
        """

        try:
            context = self.state_adapter.build_trade_context(signal)
            decision = build_trade_decision(
                context,
                kelly_sizer=self.kelly,
                risk_policy=self.risk,
                min_edge=self.min_edge,
                min_confidence=self.min_confidence,
                max_entry_price=self.max_entry_price,
            )
            if decision.approved:
                decision = TradeDecision(
                    action="SKIP",
                    approved=False,
                    reason_code=reason_code or "stable_gate_skipped",
                    reason=reason_code or "Stable pre-trade gate skipped before paper trade creation",
                    confidence=decision.confidence,
                    edge=decision.edge,
                    entry_price=decision.entry_price,
                    win_probability=decision.win_probability,
                    requested_position_size=decision.requested_position_size,
                    position_size=decision.position_size,
                    risk_score=decision.risk_score,
                    warnings=list(decision.warnings or []),
                    reasoning=dict(decision.reasoning or {}),
                )
            decision_artifact = build_pre_execution_decision_artifact(
                mode="paper_portfolio",
                context=context,
                decision=decision,
                signal=signal,
                config_snapshot=self.config,
            )
            self._safe_append_agent_decision(signal, decision_artifact, accounting_mutated=False)
            return self._append_shadow_intent_if_any(decision_artifact, signal)
        except Exception as exc:
            logger.debug("paper_shadow_intent_stable_skip_failed market_id=%s error=%s", signal.get("market_id"), exc)
            return None

    def _shadow_source_scoreboard_enabled(self) -> bool:
        """Return true when this paper runtime should append source-scoreboard lane rows.

        This is intentionally shadow/reporting-only. It only gates whether we emit
        lane-decision provenance rows; it must not affect trade creation,
        balances, risk state, or paper accounting.
        """

        if self.runtime_mode != "paper" or not paper_shadow_lanes_enabled(self.config):
            return False
        lane_cfg = self.config.get("paper_shadow_lanes") if isinstance(self.config.get("paper_shadow_lanes"), dict) else {}
        enabled_lanes = lane_cfg.get("enabled_lanes") or lane_cfg.get("lanes") or []
        return SOURCE_SCOREBOARD_LANE_ID in set(requested_paper_shadow_lane_ids(enabled_lanes))

    def _prepare_shadow_source_scoreboard_signal(
        self,
        signal: dict,
        *,
        market: Any,
        order_book: dict[str, Any],
        inputs_by_shared_candidate_id: dict[str, dict[str, Any]],
    ) -> None:
        """Prepare a read-only shared-candidate input for source-scoreboard rows."""

        if not self._shadow_source_scoreboard_enabled():
            return
        try:
            def _shadow_float(value):
                try:
                    if value is None:
                        return None
                    return float(value)
                except (TypeError, ValueError):
                    return None

            observed_at = datetime.now(timezone.utc).isoformat()
            direction = str(signal.get("direction") or "BUY_YES").upper()
            execution_snapshot = build_execution_snapshot(
                signal,
                direction=direction,
                bid_ask=order_book,
                fallback_to_signal_prices=True,
            )
            # This is not an executed paper/live fill. It is the observed book
            # snapshot we would use later for hypothetical P&L replay.
            execution_snapshot.update(
                {
                    "source": "paper_shadow_hypothetical_book",
                    "as_of": observed_at,
                    "marker": "paper_shadow_source_scoreboard_hypothetical_execution",
                    "hypothetical": True,
                }
            )
            writer_signal = dict(signal)
            writer_signal.update(
                {
                    "execution_snapshot_source": execution_snapshot.get("source"),
                    "execution_snapshot_as_of": observed_at,
                    "execution_snapshot_marker": execution_snapshot.get("marker"),
                    "estimated_fill_price": execution_snapshot.get("estimated_fill_price"),
                    "best_yes_ask": execution_snapshot.get("best_yes_ask"),
                    "best_yes_bid": execution_snapshot.get("best_yes_bid"),
                    "best_no_ask": execution_snapshot.get("best_no_ask"),
                    "best_no_bid": execution_snapshot.get("best_no_bid"),
                    "order_book_source": "paper_scan_market_quote",
                }
            )
            decision_artifact = build_pre_execution_decision_artifact(
                mode="paper_shadow_observation",
                context=None,
                decision=TradeDecision(
                    action=direction if direction in {"BUY_YES", "BUY_NO"} else "SKIP",
                    approved=False,
                    reason_code="paper_shadow_observation_only",
                    reason="Paper shadow source-scoreboard observation only; no accounting mutation",
                    confidence=_shadow_float(signal.get("confidence")) or 0.0,
                    edge=_shadow_float(signal.get("edge")) or 0.0,
                    entry_price=_shadow_float(signal.get("market_price")) or 0.0,
                    win_probability=_shadow_float(signal.get("model_probability")) or 0.0,
                    requested_position_size=0.0,
                    position_size=0.0,
                    risk_score=0.0,
                    warnings=[],
                    reasoning={"paper_shadow_source_scoreboard": {"recommendation_only": True}},
                ),
                signal=writer_signal,
                order_book=order_book,
                execution_snapshot=execution_snapshot,
                config_snapshot=self.config,
                observed_at=datetime.fromisoformat(observed_at.replace("Z", "+00:00")),
            )
            shared_candidate = build_shared_market_candidate_row(
                run_id=f"{self.session_id}:scan-{self.scan_count}",
                market=market,
                signal=writer_signal,
                decision_artifact=decision_artifact,
                source_runtime="paper_shadow_source_scoreboard",
                provenance="paper_loop_shadow_observation",
                observed_at=observed_at,
                snapshot_as_of=observed_at,
                main_runtime="paper",
            )
            shared_candidate_id = str(shared_candidate.get("candidate_id") or "")
            if not shared_candidate_id:
                return
            writer_signal["shared_candidate_id"] = shared_candidate_id
            writer_signal["candidate_observed_at"] = observed_at
            writer_signal["decision_artifact"] = decision_artifact
            inputs_by_shared_candidate_id[shared_candidate_id] = {
                STABLE_PAPER_WALLET_ID: SimpleNamespace(signal=writer_signal, shared_candidate=shared_candidate)
            }
        except Exception as exc:
            logger.debug("prepare_shadow_source_scoreboard_signal_failed market_id=%s error=%s", signal.get("market_id"), exc)

    def _shadow_source_scoreboard_gate_reason(self, signal: dict[str, Any]) -> str | None:
        def _num(value, default=0.0):
            try:
                if value is None:
                    return default
                return float(value)
            except (TypeError, ValueError):
                return default

        edge = _num(signal.get("edge"))
        confidence = _num(signal.get("confidence"))
        entry_price = _num(signal.get("market_price", signal.get("entry_price")))
        if edge < float(getattr(self, "min_edge", 0.0) or 0.0):
            return "edge_below_threshold"
        if confidence < float(getattr(self, "min_confidence", 0.0) or 0.0):
            return "confidence_below_threshold"
        max_entry = float(getattr(self, "max_entry_price", 1.0) or 1.0)
        if entry_price > max_entry:
            return "entry_price_above_cap"
        return None

    def _safe_write_shadow_source_scoreboard_lane_decisions(
        self,
        inputs_by_shared_candidate_id: dict[str, dict[str, Any]],
    ) -> None:
        """Append source-scoreboard lane rows for the current paper scan.

        This synthesizes stable wallet decision rows from already-evaluated paper
        signals. It is deliberately warning-only: a scoreboard write failure must
        not block the paper loop or mutate paper portfolio state.
        """

        if not inputs_by_shared_candidate_id or not self._shadow_source_scoreboard_enabled():
            return
        try:
            candidate_dataset_path = str(self.data_dir / "source_scoreboard" / f"paper_loop_scan_{self.scan_count}.jsonl")
            stable_rows: list[dict[str, Any]] = []
            for shared_candidate_id, wallet_inputs in inputs_by_shared_candidate_id.items():
                candidate_input = wallet_inputs.get(STABLE_PAPER_WALLET_ID)
                signal = dict(getattr(candidate_input, "signal", {}) or {})
                gate_reason = signal.get("_blocked") or self._shadow_source_scoreboard_gate_reason(signal)
                action = str(signal.get("direction") or "SKIP").upper() if gate_reason is None else "SKIP"
                if action not in {"BUY_YES", "BUY_NO"}:
                    action = "SKIP"
                stable_rows.append(
                    {
                        "shared_candidate_id": shared_candidate_id,
                        "wallet_id": STABLE_PAPER_WALLET_ID,
                        "run_id": self.session_id,
                        "candidate_dataset_path": candidate_dataset_path,
                        "decision_role": "paper_shadow",
                        "decision_id": f"{self.session_id}:scan-{self.scan_count}:{shared_candidate_id}:stable",
                        "policy": "normal",
                        "market_id": signal.get("market_id"),
                        "observed_at": signal.get("candidate_observed_at") or signal.get("observed_at"),
                        "action": action,
                        "reason_code": gate_reason or "approved",
                        "reason": gate_reason or "Stable paper signal passed pre-trade gates",
                        "confidence": signal.get("confidence"),
                        "edge": signal.get("edge"),
                        "model_probability": signal.get("model_probability"),
                        "entry_price": signal.get("market_price") or signal.get("entry_price"),
                        "price": signal.get("market_price") or signal.get("entry_price"),
                        "requested_position_size_usd": 0.0,
                        "approved_position_size_usd": 0.0,
                    }
                )
            result = write_paper_shadow_lane_decisions(
                config=self.config,
                candidate_dataset_path=candidate_dataset_path,
                inputs_by_shared_candidate_id=inputs_by_shared_candidate_id,
                wallet_decision_rows={STABLE_PAPER_WALLET_ID: stable_rows, BETA_PAPER_WALLET_ID: []},
                wallet_runs={STABLE_PAPER_WALLET_ID: SimpleNamespace(session_id=self.session_id)},
                ledger_root=self.data_dir,
            )
            logger.info(
                "Wrote paper shadow source-scoreboard lane rows path=%s rows=%s lanes=%s",
                result.decision_path,
                result.rows_written,
                ",".join(result.lane_ids),
            )
        except Exception as exc:
            logger.warning("failed to write paper shadow source-scoreboard lane rows: %s", exc)

    def _safe_append_agent_run(self) -> None:
        if self.runtime_mode != "paper":
            return
        try:
            append_paper_agent_run_once(
                data_dir=self.data_dir,
                session_id=self.session_id,
                config=self.config,
                candidate_dataset_path=self.config.get("paper_candidate_dataset_path"),
            )
        except Exception as exc:
            logger.warning("failed to append paper agent run audit row: %s", exc)

    def _safe_append_agent_decision(
        self,
        signal: dict,
        decision_artifact: dict,
        *,
        execution_result: ExecutionResult | None = None,
        trade_id: str | None = None,
        accounting_mutated: bool = False,
    ) -> None:
        if self.runtime_mode != "paper":
            return
        try:
            append_paper_decision_audit(
                data_dir=self.data_dir,
                session_id=self.session_id,
                scan_count=self.scan_count,
                signal=signal,
                decision_artifact=decision_artifact,
                execution_result=execution_result,
                trade_id=trade_id,
                config=self.config,
                accounting_mutated=accounting_mutated,
            )
        except Exception as exc:
            logger.warning("failed to append paper agent decision audit row market_id=%s: %s", signal.get("market_id"), exc)

    def _append_shadow_intent_if_any(self, decision_artifact: dict, signal: dict) -> dict | None:
        market_id = str(signal.get("market_id") or decision_artifact.get("market_id") or "")
        run_id = f"{self.session_id}:scan-{self.scan_count}"
        shadow_delta = build_shadow_delta(
            decision_artifact,
            market_id,
            run_id,
            fallback_strategy_policy=self.config.get("strategy_policy_normalized") or self.config.get("strategy_policy") or {},
        )
        if shadow_delta is None:
            return None
        observed_at = decision_artifact.get("observed_at")
        return append_hypothetical_shadow_intent_row(
            self.data_dir / "shadow_intents.jsonl",
            {
                "market_id": market_id,
                "run_id": run_id,
                "timestamp": observed_at,
                "shadow_delta": shadow_delta,
            },
            runtime_mode="paper",
            recorded_at=observed_at,
        )

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
        canonical_trade_rows = []
        for trade in self.trades:
            trade_row = asdict(trade)
            enrich_trade_audit_fields(trade_row)
            canonical_trade_rows.append(trade_row)

        normalized_trade_rows = [
            normalize_parity_trade_row(row, source="paper") for row in canonical_trade_rows
        ]
        normalized_trade_summary = summarize_normalized_rows(normalized_trade_rows)
        parity_summary = {
            "parity_mode_enabled": bool(self.parity_mode.get("enabled")),
            "parity_revalidated_trades": normalized_trade_summary.get("execution_revalidated_rows", 0),
            "parity_rejected_trades": normalized_trade_summary.get("execution_rejected_rows", 0),
            "parity_fallback_trades": normalized_trade_summary.get("fallback_rows", 0),
            "snapshot_source_counts": normalized_trade_summary.get("snapshot_source_counts", {}),
            "lifecycle_state_counts": normalized_trade_summary.get("lifecycle_state_counts", {}),
            "invalid_contract_rows": normalized_trade_summary.get("invalid_contract_rows", 0),
            "top_execution_reason_codes": normalized_trade_summary.get("top_execution_reason_codes", []),
            "top_contract_issues": normalized_trade_summary.get("top_contract_issues", []),
            "normalized_trade_summary": normalized_trade_summary,
        }

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
            "parity_mode_enabled": parity_summary["parity_mode_enabled"],
            "parity_revalidated_trades": parity_summary["parity_revalidated_trades"],
            "parity_rejected_trades": parity_summary["parity_rejected_trades"],
            "parity_fallback_trades": parity_summary["parity_fallback_trades"],
            "parity_summary": parity_summary,
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
