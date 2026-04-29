"""Risk management module — protects capital, enables scaling.

Sits between signal generation and trade execution:
Signals → Risk Check → Kelly Sizing → Execute/Reject

Core principles:
1. Capital preservation first, profits second
2. Small losses are fine, big losses are not
3. Scale position size with confidence AND bankroll health
4. Stop trading when the market isn't cooperating
5. Variable risk: scale limits with bankroll growth
"""

import json
import logging
import os
from math import isfinite
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional


STANDBY_REASON_MAX_POSITIONS = "max_positions"
STANDBY_REASON_TRADABLE_BALANCE = "tradable_balance"
STANDBY_REASON_CAPITAL_HEADROOM = "capital_headroom"

logger = logging.getLogger(__name__)


# ─── Risk Presets ────────────────────────────────────────────────────────────

PAPER_LIMITS = {
    "kelly_fraction": 0.50,      # Half-Kelly — aggressive for growth
    "max_bet_pct": 0.10,         # Max 10% per trade
    "max_exposure_pct": 0.40,    # Max 40% of bankroll at risk
    "daily_loss_limit_pct": 0.20, # Stop if down 20% today
    "max_drawdown_pct": 0.50,    # Pause if down 50% from peak
    "max_open_positions": 15,    # Max 15 concurrent trades
    "cooldown_after_losses": 4,   # Cooldown after 4 consecutive losses
}

LIVE_LIMITS = {
    "kelly_fraction": 0.25,      # Quarter-Kelly — conservative
    "max_bet_pct": 0.05,         # Max 5% per trade
    "max_exposure_pct": 0.25,    # Max 25% of bankroll at risk
    "daily_loss_limit_pct": 0.10, # Stop if down 10% today
    "max_drawdown_pct": 0.25,    # Pause if down 25% from peak
    "max_open_positions": 10,    # Max 10 concurrent trades
    "cooldown_after_losses": 3,   # Cooldown after 3 consecutive losses
}


def get_preset(is_live: bool) -> dict:
    """Return the appropriate risk preset based on mode."""
    return LIVE_LIMITS if is_live else PAPER_LIMITS


# ─── Dataclasses ─────────────────────────────────────────────────────────────

@dataclass
class RiskState:
    """Tracks current risk exposure across all positions."""
    # Bankroll tracking
    starting_balance: float = 100.0
    current_balance: float = 100.0  # Total equity = available_cash + reserved_capital
    peak_balance: float = 100.0
    available_cash: float = 100.0
    reserved_capital: float = 0.0

    # Session-level kill-switch tracking
    session_starting_balance: float = 100.0  # Balance at bot startup
    session_peak_balance: float = 100.0      # Highest balance this session
    max_drawdown_halt: bool = False           # Permanently halted by drawdown kill-switch
    trading_enabled: bool = True              # Operator pause/resume flag
    max_tradable_balance: float = 0.0         # 0 means unlimited/use available cash
    max_position_size_usd: float = 0.0        # 0 means no explicit hard dollar cap

    # Daily tracking
    daily_pnl: float = 0.0
    daily_trades: int = 0
    last_reset_date: str = ""

    # Position tracking
    open_positions: int = 0
    total_exposure: float = 0.0

    # Streak tracking
    consecutive_losses: int = 0
    consecutive_wins: int = 0

    # Cooldown
    cooldown_until: str = ""

    # Shared standby-mode state (paper first, live-ready shape)
    standby_active: bool = False
    standby_entered_at: str = ""
    standby_reason_codes: list[str] = field(default_factory=list)
    standby_blocked_scan_count: int = 0
    standby_unresolved_positions_at_entry: int = 0
    standby_exposure_at_entry: float = 0.0
    standby_available_cash_at_entry: float = 0.0
    standby_last_resume_at: str = ""
    standby_last_resume_reason: str = ""

    # History
    trade_history: list = field(default_factory=list)

    @property
    def total_pnl(self) -> float:
        return self.current_balance - self.starting_balance

    @property
    def total_pnl_pct(self) -> float:
        return (self.total_pnl / self.starting_balance) * 100 if self.starting_balance > 0 else 0

    @property
    def drawdown(self) -> float:
        """Current drawdown from peak."""
        if self.peak_balance <= 0:
            return 0
        return (self.peak_balance - self.current_balance) / self.peak_balance

    @property
    def drawdown_pct(self) -> float:
        return self.drawdown * 100

    @property
    def daily_pnl_pct(self) -> float:
        """Daily P&L as percentage of current balance (dynamic)."""
        pnl = self.daily_pnl or 0
        bal = self.current_balance or 100
        return (pnl / bal) * 100 if bal > 0 else 0

    @property
    def exposure_pct(self) -> float:
        """Current exposure as percentage of bankroll."""
        return (self.total_exposure / self.current_balance * 100) if self.current_balance > 0 else 0

    @property
    def win_rate(self) -> float:
        if not self.trade_history:
            return 0
        wins = sum(1 for t in self.trade_history if (t.get("pnl") or 0) > 0)
        return wins / len(self.trade_history)

    @property
    def is_in_cooldown(self) -> bool:
        if not self.cooldown_until:
            return False
        try:
            return datetime.now(timezone.utc) < datetime.fromisoformat(self.cooldown_until)
        except:
            return False


@dataclass
class RiskDecision:
    """Result of a risk check."""
    approved: bool
    reason: str = ""
    adjusted_size: float = 0.0
    original_size: float = 0.0
    risk_score: float = 0.0  # 0 = safe, 1 = very risky
    warnings: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def __bool__(self):
        return self.approved


class RiskManager:
    """
    Risk management for prediction market trading.

    Modes:
    - Paper (default): Aggressive limits for growth simulation
    - Live: Conservative limits for real money protection

    Rules (all configurable):
    1. Daily loss limit: stop trading if down X% today (vs current balance)
    2. Max drawdown: pause if total balance drops X% from peak
    3. Max open positions: limit concurrent exposure
    4. Max exposure: limit total dollars at risk simultaneously
    5. Correlation limit: max N bets on correlated markets
    6. Cooldown: skip scans after consecutive losses
    7. Position scaling: reduce size when bankroll is stressed
    8. Variable sizing: limits scale with bankroll growth
    """

    def __init__(self, config: dict = None):
        config = config or {}
        risk_cfg = config.get("risk", {}) or {}

        def config_value(key: str, default=None):
            if key in risk_cfg:
                return risk_cfg.get(key)
            return config.get(key, default)

        trading_cfg = config.get("trading", {}) or {}
        configured_mode = str(trading_cfg.get("mode", config.get("mode", "")) or "").strip().lower()
        if configured_mode in ("live", "paper"):
            self.is_live = configured_mode == "live"
        else:
            paper_mode = os.getenv("PAPER_MODE", "true").lower() == "true"
            self.is_live = not paper_mode
        preset = get_preset(self.is_live)

        # Resolve limits: env vars override preset, explicit config overrides both
        def resolve_float(key: str, default: float) -> float:
            env_key = key.upper()
            return float(os.getenv(env_key, config_value(key, preset.get(key, default))))

        def resolve_int(key: str, default: int) -> int:
            env_key = key.upper()
            return int(float(os.getenv(env_key, config_value(key, preset.get(key, default)))))

        self.kelly_fraction = resolve_float("kelly_fraction", preset["kelly_fraction"])
        self.max_bet_pct = resolve_float("max_bet_pct", preset["max_bet_pct"])
        self.max_exposure_pct = resolve_float("max_exposure_pct", preset["max_exposure_pct"])
        self.daily_loss_limit_pct = resolve_float("daily_loss_limit_pct", preset["daily_loss_limit_pct"])
        self.max_drawdown_pct = resolve_float("max_drawdown_pct", preset["max_drawdown_pct"])
        self.max_open_positions = resolve_int("max_open_positions", preset["max_open_positions"])
        self.cooldown_after_losses = resolve_int("cooldown_after_losses", preset["cooldown_after_losses"])
        self.max_tradable_balance = resolve_float("max_tradable_balance_usd", config_value("max_tradable_balance", 0.0))
        self.max_position_size_usd = resolve_float("max_position_size_usd", config_value("max_position_size_usd", 0.0))
        self.max_event_exposure_pct = resolve_float("max_event_exposure_pct", 0.10)
        self.max_event_positions = resolve_int("max_event_positions", 3)
        self.retrade_edge_premium = resolve_float("retrade_edge_premium", 0.01)
        self.retrade_confidence_premium = resolve_float("retrade_confidence_premium", 0.00)
        self.retrade_size_decay = resolve_float("retrade_size_decay", 0.65)
        self.min_retrade_net_edge = resolve_float("min_retrade_net_edge", 0.005)
        self.min_retrade_expected_profit_usd = resolve_float("min_retrade_expected_profit_usd", 0.0)
        self.strict_event_overlap = bool(config_value("strict_event_overlap", True))
        self.require_price_improvement_for_same_market_family = bool(
            config_value("require_price_improvement_for_same_market_family", False)
        )
        self.price_improvement_ticks = resolve_float("price_improvement_ticks", 0.03)
        if "enabled" in trading_cfg:
            self.trading_enabled = bool(trading_cfg.get("enabled"))
        elif "trading_enabled" in trading_cfg:
            self.trading_enabled = bool(trading_cfg.get("trading_enabled"))
        else:
            self.trading_enabled = bool(config.get("trading_enabled", True))

        # Session-level kill-switch: halt permanently if balance falls this far below
        # max(session_starting_balance, session_peak_balance).
        # Requires manual reset (delete data/risk_state.json or set FORCE_RESUME=true).
        self.max_session_drawdown_pct = float(
            os.getenv("MAX_DRAWDOWN_PCT", config_value("max_session_drawdown_pct", 0.20))
        )

        # Stress scaling — more lenient as bankroll grows
        self.stress_threshold = config_value("stress_threshold", 0.8)
        self.stress_reduction = config_value("stress_reduction", 0.3)
        self.min_position_size = float(config_value("min_position_size", 1.0))
        standby_cfg = config.get("standby_mode", {}) or {}
        self.standby_mode_enabled = bool(standby_cfg.get("enabled", not self.is_live))
        self.standby_blocked_scan_threshold = int(standby_cfg.get("blocked_scan_threshold", 3) or 3)
        self.standby_min_positions_resolved_to_resume = int(
            standby_cfg.get("min_positions_resolved_to_resume", 2) or 2
        )
        self.standby_min_exposure_reduction_pct = float(
            standby_cfg.get("min_exposure_reduction_pct", 0.10) or 0.10
        )
        self.standby_min_useful_trade_size_usd = float(
            standby_cfg.get("min_useful_trade_size_usd", 5.0) or 5.0
        )

        starting = config_value("starting_balance", 100.0)
        # State
        self.state = RiskState(
            starting_balance=starting,
            current_balance=starting,
            peak_balance=starting,
            session_starting_balance=starting,
            session_peak_balance=starting,
            trading_enabled=self.trading_enabled,
            max_tradable_balance=self.max_tradable_balance,
            max_position_size_usd=self.max_position_size_usd,
        )

        # Correlation groups (markets that move together)
        self._correlation_groups = self._build_correlation_groups()

        # Data path
        self.data_path = Path(config.get("data_dir", "data")) / "risk_state.json"
        self._load_state()

        # Allow operator to clear the kill-switch without deleting the state file
        if os.getenv("FORCE_RESUME", "").lower() in ("true", "1", "yes"):
            if self.state.max_drawdown_halt:
                logger.warning("FORCE_RESUME=true: clearing max-drawdown halt flag")
                self.manual_reset_drawdown_halt()

        self.state.trading_enabled = self.trading_enabled
        self.state.max_tradable_balance = self.max_tradable_balance
        self.state.max_position_size_usd = self.max_position_size_usd

        mode_label = "🔴 LIVE" if self.is_live else "🟡 PAPER"
        logger.info(
            f"{mode_label} risk mode | Kelly={self.kelly_fraction:.0%} "
            f"max_bet={self.max_bet_pct:.0%} daily_loss={self.daily_loss_limit_pct:.0%}"
        )

    def _build_correlation_groups(self) -> dict[str, str]:
        """Map market keywords to correlation groups."""
        return {
            "pope": "pope_election",
            "pontiff": "pope_election",
            "cardinal": "pope_election",
            "mars": "space",
            "spacex": "space",
            "elon": "space",
            "president": "us_politics",
            "election": "us_politics",
            "celsius": "climate",
            "degrees": "climate",
            "temperature": "climate",
            "climate": "climate",
            "become": "china_politics",
            "leader": "china_politics",
        }

    def _get_correlation_group(self, question: str) -> Optional[str]:
        """Determine which correlation group a market belongs to."""
        q_lower = question.lower()
        for keyword, group in self._correlation_groups.items():
            if keyword in q_lower:
                return group
        return None

    def check_trade(
        self,
        signal: dict,
        position_size: float,
        *,
        available_cash: Optional[float] = None,
    ) -> RiskDecision:
        """
        Check if a trade should be approved.

        Returns RiskDecision with approved/rejected + adjusted size.
        """
        warnings = []
        original_size = position_size

        try:
            position_size = float(position_size)
        except (TypeError, ValueError):
            return RiskDecision(
                approved=False,
                reason="Invalid position size",
                original_size=original_size,
                risk_score=1.0,
            )

        if not isfinite(position_size) or position_size <= 0:
            return RiskDecision(
                approved=False,
                reason="Non-positive position size",
                original_size=original_size,
                risk_score=1.0,
            )

        spendable_cash = self._coerce_float(
            self.state.available_cash if available_cash is None else available_cash,
            self.state.available_cash,
        )

        if spendable_cash < -0.01 or self.state.available_cash < -0.01:
            return RiskDecision(
                approved=False,
                reason="Negative available cash invariant breach",
                original_size=original_size,
                risk_score=1.0,
                metadata={"reason_code": "negative_available_cash_invariant"},
            )
        if self.state.reserved_capital < -0.01 or self.state.total_exposure < -0.01:
            return RiskDecision(
                approved=False,
                reason="Negative accounting invariant breach",
                original_size=original_size,
                risk_score=1.0,
                metadata={"reason_code": "negative_accounting_invariant"},
            )

        effective_tradable_cash = spendable_cash
        if self.max_tradable_balance and self.max_tradable_balance > 0:
            effective_tradable_cash = min(effective_tradable_cash, self.max_tradable_balance)

        # === Hard stops (reject immediately) ===

        if not self.state.trading_enabled:
            return RiskDecision(
                approved=False,
                reason="Trading paused by operator",
                original_size=original_size,
                risk_score=1.0,
                metadata={"reason_code": "trading_disabled"},
            )

        # 0. Session-level kill-switch (permanent halt until manual reset)
        if self.state.max_drawdown_halt:
            return RiskDecision(
                approved=False,
                reason="Session max-drawdown kill-switch active — manual reset required",
                risk_score=1.0,
            )

        # 1. Daily loss limit — relative to CURRENT balance (dynamic)
        if self.state.daily_pnl < 0:
            daily_loss_pct = abs(self.state.daily_pnl_pct)
            if daily_loss_pct >= self.daily_loss_limit_pct * 100:
                return RiskDecision(
                    approved=False,
                    reason=f"Daily loss limit hit ({daily_loss_pct:.1f}% / {self.daily_loss_limit_pct * 100:.0f}%)",
                    risk_score=1.0,
                )

        # 2. Max drawdown
        if self.state.drawdown_pct >= self.max_drawdown_pct * 100:
            return RiskDecision(
                approved=False,
                reason=f"Max drawdown hit ({self.state.drawdown_pct:.1f}% / {self.max_drawdown_pct * 100:.0f}%)",
                risk_score=1.0,
            )

        # 3. Max positions
        if self.state.open_positions >= self.max_open_positions:
            return RiskDecision(
                approved=False,
                reason=f"Max positions ({self.state.open_positions}/{self.max_open_positions})",
                risk_score=0.8,
            )

        # 4. Cooldown
        if self.state.is_in_cooldown:
            return RiskDecision(
                approved=False,
                reason=f"In cooldown (after {self.state.consecutive_losses} consecutive losses)",
                risk_score=0.9,
            )

        # === Soft limits (reduce size) ===

        risk_score = 0.0

        if self.max_position_size_usd and self.max_position_size_usd > 0 and position_size > self.max_position_size_usd:
            clipped_size = round(self.max_position_size_usd, 2)
            if clipped_size < self.min_position_size:
                return RiskDecision(
                    approved=False,
                    reason=f"Max position size below minimum (${clipped_size:.2f})",
                    risk_score=1.0,
                    metadata={"reason_code": "max_position_below_minimum"},
                )
            warnings.append(f"Hard position cap clipped size to ${clipped_size:.2f}")
            position_size = clipped_size
            risk_score += 0.2

        # 6. Correlation check
        question = signal.get("question", "")
        corr_group = self._get_correlation_group(question)
        if corr_group:
            correlated_count = sum(
                1 for t in self.state.trade_history[-self.max_open_positions:]
                if self._get_correlation_group(t.get("question", "")) == corr_group
                and not t.get("resolved", False)
            )
            if correlated_count >= 5:  # Max 5 correlated bets
                warnings.append(f"Correlation: {corr_group} ({correlated_count}/5)")
                position_size *= 0.5
                risk_score += 0.3

        # 7. Stress scaling (reduce size when near daily loss limit)
        if self.state.daily_pnl < 0:
            loss_used_pct = abs(self.state.daily_pnl_pct) / (self.daily_loss_limit_pct * 100)
            if loss_used_pct >= self.stress_threshold:
                reduction = self.stress_reduction
                warnings.append(f"Stress scaling: -{reduction:.0%} (loss limit {loss_used_pct * 100:.0f}% used)")
                position_size *= (1 - reduction)
                risk_score += 0.2

        # 8. Consecutive loss scaling
        if self.state.consecutive_losses >= 2:
            scale = 1.0 - (self.state.consecutive_losses * 0.15)
            scale = max(0.3, scale)
            warnings.append(f"Loss streak: {self.state.consecutive_losses} losses, sizing at {scale:.0%}")
            position_size *= scale
            risk_score += 0.1 * self.state.consecutive_losses

        # 9. Drawdown scaling
        drawdown_threshold = self.max_drawdown_pct * 50  # 50% of max drawdown
        if self.state.drawdown_pct > drawdown_threshold:
            scale = 1.0 - (self.state.drawdown_pct / (self.max_drawdown_pct * 100) * 0.5)
            scale = max(0.25, scale)
            warnings.append(f"Drawdown scaling: {scale:.0%} (drawdown {self.state.drawdown_pct:.1f}%)")
            position_size *= scale
            risk_score += 0.2

        # 10. Max exposure — preserve the guardrail, but take a smaller trade if
        # there is still meaningful headroom left.
        max_exposure_dollars = self.state.current_balance * self.max_exposure_pct
        remaining_headroom = max(0.0, max_exposure_dollars - self.state.total_exposure)
        projected_exposure = self.state.total_exposure + position_size
        projected_exposure_pct = (
            projected_exposure / self.state.current_balance * 100
            if self.state.current_balance > 0 else 0
        )
        if projected_exposure_pct > self.max_exposure_pct * 100:
            if remaining_headroom < self.min_position_size:
                return RiskDecision(
                    approved=False,
                    reason=(
                        f"Max exposure ({projected_exposure_pct:.1f}% / "
                        f"{self.max_exposure_pct * 100:.0f}% of ${self.state.current_balance:.2f})"
                    ),
                    risk_score=1.0,
                )
            clipped_size = round(min(position_size, remaining_headroom), 2)
            if clipped_size < self.min_position_size:
                return RiskDecision(
                    approved=False,
                    reason=f"Exposure headroom below minimum size (${remaining_headroom:.2f})",
                    risk_score=1.0,
                )
            warnings.append(
                f"Exposure headroom capped size to ${clipped_size:.2f} "
                f"(${remaining_headroom:.2f} remaining)"
            )
            position_size = clipped_size
            risk_score += 0.25

        if effective_tradable_cash < self.min_position_size:
            return RiskDecision(
                approved=False,
                reason=f"Tradable balance below minimum size (${effective_tradable_cash:.2f})",
                risk_score=1.0,
                metadata={"reason_code": "tradable_balance_below_minimum"},
            )

        if position_size > effective_tradable_cash:
            clipped_size = round(min(position_size, effective_tradable_cash), 2)
            if clipped_size < self.min_position_size:
                return RiskDecision(
                    approved=False,
                    reason=f"Tradable balance below minimum size (${effective_tradable_cash:.2f})",
                    risk_score=1.0,
                    metadata={"reason_code": "tradable_balance_below_minimum"},
                )
            warnings.append(
                f"Tradable balance capped size to ${clipped_size:.2f} "
                f"(${effective_tradable_cash:.2f} tradable)"
            )
            position_size = clipped_size
            risk_score += 0.2

        if spendable_cash < self.min_position_size:
            return RiskDecision(
                approved=False,
                reason=f"Available cash below minimum size (${spendable_cash:.2f})",
                risk_score=1.0,
            )

        if position_size > spendable_cash:
            clipped_size = round(min(position_size, spendable_cash), 2)
            if clipped_size < self.min_position_size:
                return RiskDecision(
                    approved=False,
                    reason=f"Available cash below minimum size (${spendable_cash:.2f})",
                    risk_score=1.0,
                )
            warnings.append(
                f"Available cash capped size to ${clipped_size:.2f} "
                f"(${spendable_cash:.2f} spendable)"
            )
            position_size = clipped_size
            risk_score += 0.2

        # Minimum position size: $1
        position_size = max(self.min_position_size, round(position_size, 2))
        risk_score = min(1.0, risk_score)

        return RiskDecision(
            approved=True,
            reason="Approved" + (f" (with {len(warnings)} warnings)" if warnings else ""),
            adjusted_size=position_size,
            original_size=original_size,
            risk_score=risk_score,
            warnings=warnings,
            metadata={
                "spendable_cash": round(spendable_cash, 2),
                "effective_tradable_cash": round(effective_tradable_cash, 2),
                "max_position_size_usd": round(self.max_position_size_usd, 2),
                "trading_enabled": self.state.trading_enabled,
            },
        )

    def record_trade(self, trade: dict):
        """Record a trade for risk tracking."""
        size = self._coerce_float(trade.get("position_size", trade.get("size")))
        if size <= 0:
            return
        reserved_capital = self._coerce_float(trade.get("reserved_capital"), size)
        trade_id = trade.get("id") or trade.get("trade_id") or ""
        self.state.trade_history.append({
            "trade_id": trade_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "question": trade.get("question", ""),
            "direction": trade.get("direction", ""),
            "size": size,
            "reserved_capital": round(reserved_capital, 2),
            "market_price": self._coerce_float(trade.get("market_price")),
            "resolved": False,
            "pnl": 0,
        })
        self.state.open_positions += 1
        self.state.total_exposure += size
        self.state.available_cash = round(self.state.available_cash - reserved_capital, 2)
        self.state.reserved_capital = round(self.state.reserved_capital + reserved_capital, 2)
        self.state.daily_trades += 1
        self._save_state()

    def sync_account_state(
        self,
        *,
        current_balance: Optional[float] = None,
        available_cash: Optional[float] = None,
        reserved_capital: Optional[float] = None,
        total_exposure: Optional[float] = None,
        open_positions: Optional[int] = None,
    ):
        """Synchronize risk state with a live account snapshot before shared checks."""
        if current_balance is not None:
            self.state.current_balance = self._coerce_float(current_balance, self.state.current_balance)
        if available_cash is not None:
            self.state.available_cash = round(self._coerce_float(available_cash, self.state.available_cash), 2)
        if reserved_capital is not None:
            self.state.reserved_capital = round(self._coerce_float(reserved_capital, self.state.reserved_capital), 2)
        if total_exposure is not None:
            self.state.total_exposure = round(self._coerce_float(total_exposure, self.state.total_exposure), 2)
        if open_positions is not None:
            self.state.open_positions = max(0, int(open_positions))

        self.state.peak_balance = max(self.state.peak_balance, self.state.current_balance)
        self.state.session_peak_balance = max(self.state.session_peak_balance, self.state.current_balance)
        self._save_state()

    def sync_with_trades(
        self,
        trades: list,
        *,
        current_balance: Optional[float] = None,
        starting_balance: Optional[float] = None,
        available_cash: Optional[float] = None,
        reserved_capital: Optional[float] = None,
    ):
        """Rebuild exposure state from the session file so risk and simulator stay aligned."""
        synced_history = []
        open_positions = 0
        total_exposure = 0.0
        derived_reserved_capital = 0.0

        for trade in trades or []:
            size = self._coerce_float(getattr(trade, "position_size", None))
            market_id = getattr(trade, "market_id", "") or ""
            if size <= 0 or not market_id:
                continue

            resolved = bool(getattr(trade, "resolved", False))
            reserved_amount = self._coerce_float(getattr(trade, "reserved_capital", None), size)
            record = {
                "trade_id": getattr(trade, "id", "") or market_id,
                "timestamp": getattr(trade, "timestamp", datetime.now(timezone.utc).isoformat()),
                "question": getattr(trade, "question", ""),
                "direction": getattr(trade, "direction", ""),
                "size": round(size, 2),
                "reserved_capital": round(reserved_amount, 2),
                "market_price": self._coerce_float(getattr(trade, "market_price", None)),
                "resolved": resolved,
                "pnl": self._coerce_float(getattr(trade, "pnl", None)),
            }
            synced_history.append(record)

            if not resolved:
                open_positions += 1
                total_exposure += size
                derived_reserved_capital += reserved_amount

        if starting_balance is not None:
            self.state.starting_balance = self._coerce_float(starting_balance, self.state.starting_balance)
            if self.state.session_starting_balance <= 0:
                self.state.session_starting_balance = self.state.starting_balance

        balance_value = self.state.current_balance
        if current_balance is not None:
            balance_value = self._coerce_float(current_balance, self.state.current_balance)
            self.state.current_balance = balance_value

        reserved_value = (
            round(self._coerce_float(reserved_capital, derived_reserved_capital), 2)
            if reserved_capital is not None
            else round(derived_reserved_capital, 2)
        )
        available_value = (
            round(self._coerce_float(available_cash, balance_value - reserved_value), 2)
            if available_cash is not None
            else round(balance_value - reserved_value, 2)
        )

        self.state.trade_history = synced_history
        self.state.open_positions = open_positions
        self.state.total_exposure = round(total_exposure, 2)
        self.state.reserved_capital = reserved_value
        self.state.available_cash = available_value
        self.state.peak_balance = max(
            self.state.peak_balance,
            self.state.current_balance,
            self.state.starting_balance,
        )
        self.state.session_peak_balance = max(
            self.state.session_peak_balance,
            self.state.current_balance,
            self.state.session_starting_balance,
        )
        self._save_state()

    def record_trade_result(self, trade_ref, pnl: float):
        """Backward-compatible alias for older runner calls."""
        self.record_outcome(trade_ref, pnl)

    def record_outcome(self, trade_ref, pnl: float):
        """Record the outcome of a resolved trade."""
        self.reset_daily()
        trade = self._find_trade_record(trade_ref)
        if trade is not None:
            if trade.get("resolved"):
                return

            trade["resolved"] = True
            trade["pnl"] = self._coerce_float(pnl)
            reserved_capital = self._coerce_float(trade.get("reserved_capital"), trade.get("size", 0))

            # Release exposure (approximate — full size released on resolve)
            self.state.total_exposure = max(0, self.state.total_exposure - trade.get("size", 0))
            self.state.reserved_capital = round(
                max(0, self.state.reserved_capital - reserved_capital),
                2,
            )
            self.state.available_cash = round(
                self.state.available_cash + reserved_capital + trade["pnl"],
                2,
            )

            # Update balance
            self.state.current_balance += trade["pnl"]
            self.state.daily_pnl += trade["pnl"]
            self.state.open_positions = max(0, self.state.open_positions - 1)

            # Update all-time and session peaks
            if self.state.current_balance > self.state.peak_balance:
                self.state.peak_balance = self.state.current_balance
            if self.state.current_balance > self.state.session_peak_balance:
                self.state.session_peak_balance = self.state.current_balance

            # Check session-level max drawdown kill-switch
            self._check_session_drawdown()

            # Update streaks
            if trade["pnl"] > 0:
                self.state.consecutive_wins += 1
                self.state.consecutive_losses = 0
                self.state.cooldown_until = ""
            elif trade["pnl"] < 0:
                self.state.consecutive_losses += 1
                self.state.consecutive_wins = 0

                # Trigger cooldown after N consecutive losses
                if self.state.consecutive_losses >= self.cooldown_after_losses:
                    cooldown_time = datetime.now(timezone.utc) + timedelta(minutes=self.cooldown_after_losses * 3)
                    self.state.cooldown_until = cooldown_time.isoformat()
                    logger.warning(
                        f"🛑 Cooldown triggered: {self.state.consecutive_losses} losses, "
                        f"pausing until {cooldown_time.strftime('%H:%M UTC')}"
                    )
            else:
                self.state.consecutive_wins = 0

            self._save_state()

    def _check_session_drawdown(self):
        """Check session-level max drawdown and trigger kill-switch if breached."""
        if self.state.max_drawdown_halt:
            return  # Already halted

        # Drawdown measured from max(session_start, session_peak) — whichever is higher
        high_water = max(self.state.session_starting_balance, self.state.session_peak_balance)
        if high_water <= 0:
            return

        drawdown = (high_water - self.state.current_balance) / high_water
        threshold = self.max_session_drawdown_pct

        if drawdown >= threshold:
            self.state.max_drawdown_halt = True
            logger.critical(
                f"🚨 SESSION MAX DRAWDOWN KILL-SWITCH TRIGGERED! "
                f"Balance dropped {drawdown:.1%} from high-water mark "
                f"(${self.state.current_balance:.2f} vs ${high_water:.2f}). "
                f"Threshold: {threshold:.0%}. All new trades HALTED. "
                f"To resume: delete data/risk_state.json or set FORCE_RESUME=true"
            )
            # Send alert via Telegram
            self._send_drawdown_alert(drawdown, high_water)
            self._save_state()

    def _send_drawdown_alert(self, drawdown: float, high_water: float):
        """Send a Telegram alert for the drawdown kill-switch."""
        try:
            import subprocess
            from pathlib import Path
            msg = (
                f"🚨 *MAX DRAWDOWN KILL-SWITCH TRIGGERED*\n\n"
                f"Balance: ${self.state.current_balance:.2f}\n"
                f"High-water mark: ${high_water:.2f}\n"
                f"Drawdown: {drawdown:.1%} (limit: {self.max_session_drawdown_pct:.0%})\n\n"
                f"All new trades are HALTED.\n"
                f"To resume: delete `data/risk_state.json` or set `FORCE_RESUME=true`"
            )
            scripts_dir = Path(__file__).parent.parent / "scripts"
            subprocess.run(
                ["python3", "send_alert.py", "-m", msg],
                cwd=str(scripts_dir),
                capture_output=True,
                timeout=30,
            )
        except Exception as e:
            logger.debug(f"Failed to send drawdown alert: {e}")

    def manual_reset_drawdown_halt(self):
        """Clear the permanent halt flag. Requires explicit call or FORCE_RESUME=true."""
        self.state.max_drawdown_halt = False
        self.state.session_starting_balance = self.state.current_balance
        self.state.session_peak_balance = self.state.current_balance
        self._save_state()
        logger.warning("⚠️  Max-drawdown halt manually cleared. Trading resumed.")

    def reset_daily(self):
        """Reset daily trackers. Call at start of each trading day."""
        today = datetime.now(timezone.utc).date().isoformat()
        if self.state.last_reset_date != today:
            self.state.daily_pnl = 0.0
            self.state.daily_trades = 0
            self.state.last_reset_date = today
            self._save_state()
            logger.info(f"📅 Daily reset — new trading day: {today}")

    def _estimate_useful_trade_capacity(self) -> float:
        spendable_cash = max(0.0, self._coerce_float(self.state.available_cash, 0.0))
        tradable_cash = spendable_cash
        if self.max_tradable_balance > 0:
            tradable_cash = min(tradable_cash, self.max_tradable_balance)

        max_exposure_dollars = max(0.0, self.state.current_balance * self.max_exposure_pct)
        exposure_headroom = max(0.0, max_exposure_dollars - self.state.total_exposure)
        capacity = min(tradable_cash, exposure_headroom)

        if self.max_position_size_usd > 0:
            capacity = min(capacity, self.max_position_size_usd)
        if self.state.open_positions >= self.max_open_positions:
            return 0.0
        return round(max(0.0, capacity), 2)

    def _capital_standby_reasons_from_blockers(self, blocked_reasons: dict[str, int] | None) -> list[str]:
        reasons = set()
        for key, count in (blocked_reasons or {}).items():
            if not count:
                continue
            normalized = str(key or "")
            if "max_positions" in normalized:
                reasons.add(STANDBY_REASON_MAX_POSITIONS)
            elif "tradable_balance" in normalized:
                reasons.add(STANDBY_REASON_TRADABLE_BALANCE)
            elif "exposure" in normalized or "capital" in normalized or "headroom" in normalized:
                reasons.add(STANDBY_REASON_CAPITAL_HEADROOM)
        return sorted(reasons)

    def reconcile_startup_standby(self) -> dict:
        """Re-assert standby after a restart if the restored portfolio is already extended."""
        useful_capacity = self._estimate_useful_trade_capacity()
        capital_reasons: list[str] = []

        if self.state.open_positions >= self.max_open_positions:
            capital_reasons.append(STANDBY_REASON_MAX_POSITIONS)

        spendable_cash = max(0.0, self._coerce_float(self.state.available_cash, 0.0))
        tradable_cash = spendable_cash
        if self.max_tradable_balance > 0:
            tradable_cash = min(tradable_cash, self.max_tradable_balance)
        if tradable_cash < self.min_position_size:
            capital_reasons.append(STANDBY_REASON_TRADABLE_BALANCE)

        max_exposure_dollars = max(0.0, self.state.current_balance * self.max_exposure_pct)
        exposure_headroom = max(0.0, max_exposure_dollars - self.state.total_exposure)
        if exposure_headroom < self.min_position_size or useful_capacity < self.standby_min_useful_trade_size_usd:
            capital_reasons.append(STANDBY_REASON_CAPITAL_HEADROOM)

        capital_reasons = sorted(set(capital_reasons))

        if not self.standby_mode_enabled or self.is_live:
            return {
                "standby_active": bool(self.state.standby_active),
                "reason_codes": list(self.state.standby_reason_codes),
                "useful_trade_capacity": useful_capacity,
                "asserted": False,
            }

        if self.state.standby_active:
            if not self.state.standby_reason_codes:
                self.state.standby_reason_codes = list(capital_reasons)
                self._save_state()
            return {
                "standby_active": True,
                "reason_codes": list(self.state.standby_reason_codes),
                "useful_trade_capacity": useful_capacity,
                "asserted": False,
            }

        if not capital_reasons:
            return {
                "standby_active": False,
                "reason_codes": [],
                "useful_trade_capacity": useful_capacity,
                "asserted": False,
            }

        self.state.standby_active = True
        self.state.standby_entered_at = self.state.standby_entered_at or datetime.now(timezone.utc).isoformat()
        self.state.standby_reason_codes = capital_reasons
        self.state.standby_blocked_scan_count = max(
            self.state.standby_blocked_scan_count,
            self.standby_blocked_scan_threshold,
        )
        self.state.standby_unresolved_positions_at_entry = max(
            int(self.state.standby_unresolved_positions_at_entry),
            int(self.state.open_positions),
        )
        self.state.standby_exposure_at_entry = max(
            self._coerce_float(self.state.standby_exposure_at_entry, 0.0),
            round(self.state.total_exposure, 2),
        )
        self.state.standby_available_cash_at_entry = round(self.state.available_cash, 2)
        self._save_state()
        return {
            "standby_active": True,
            "reason_codes": list(self.state.standby_reason_codes),
            "useful_trade_capacity": useful_capacity,
            "asserted": True,
        }

    def record_blocked_scan(self, blocked_reasons: dict[str, int] | None, *, trades_taken: int = 0) -> None:
        if not self.standby_mode_enabled or self.is_live:
            return
        if self.state.standby_active:
            return

        capital_reasons = self._capital_standby_reasons_from_blockers(blocked_reasons)
        if trades_taken > 0 or not capital_reasons:
            self.state.standby_blocked_scan_count = 0
            self._save_state()
            return

        self.state.standby_blocked_scan_count += 1
        if self.state.standby_blocked_scan_count < self.standby_blocked_scan_threshold:
            self._save_state()
            return

        self.state.standby_active = True
        self.state.standby_entered_at = datetime.now(timezone.utc).isoformat()
        self.state.standby_reason_codes = capital_reasons
        self.state.standby_unresolved_positions_at_entry = self.state.open_positions
        self.state.standby_exposure_at_entry = round(self.state.total_exposure, 2)
        self.state.standby_available_cash_at_entry = round(self.state.available_cash, 2)
        self._save_state()

    def clear_blocked_scan_streak(self) -> None:
        if self.state.standby_blocked_scan_count == 0:
            return
        self.state.standby_blocked_scan_count = 0
        self._save_state()

    def evaluate_standby_resume(self) -> dict:
        useful_capacity = self._estimate_useful_trade_capacity()
        if not self.standby_mode_enabled or not self.state.standby_active:
            return {
                "standby_active": False,
                "useful_trade_capacity": useful_capacity,
                "resumed": False,
            }

        positions_resolved = max(
            0,
            int(self.state.standby_unresolved_positions_at_entry) - int(self.state.open_positions),
        )
        entry_exposure = max(0.0, self._coerce_float(self.state.standby_exposure_at_entry, 0.0))
        current_exposure = max(0.0, self._coerce_float(self.state.total_exposure, 0.0))
        exposure_reduction_pct = 0.0
        if entry_exposure > 0:
            exposure_reduction_pct = max(0.0, (entry_exposure - current_exposure) / entry_exposure)
        positions_triggered = positions_resolved >= self.standby_min_positions_resolved_to_resume
        exposure_triggered = exposure_reduction_pct >= self.standby_min_exposure_reduction_pct
        capacity_ok = useful_capacity >= self.standby_min_useful_trade_size_usd

        if (positions_triggered or exposure_triggered) and capacity_ok:
            reason_bits = []
            if positions_triggered:
                reason_bits.append(f"positions_resolved={positions_resolved}")
            if exposure_triggered:
                reason_bits.append(f"exposure_reduction_pct={exposure_reduction_pct:.2f}")
            reason_bits.append(f"useful_trade_capacity=${useful_capacity:.2f}")
            self.state.standby_active = False
            self.state.standby_blocked_scan_count = 0
            self.state.standby_last_resume_at = datetime.now(timezone.utc).isoformat()
            self.state.standby_last_resume_reason = ",".join(reason_bits)
            self.state.standby_reason_codes = []
            self._save_state()
            return {
                "standby_active": False,
                "resumed": True,
                "resume_reason": self.state.standby_last_resume_reason,
                "positions_resolved": positions_resolved,
                "exposure_reduction_pct": round(exposure_reduction_pct, 4),
                "useful_trade_capacity": useful_capacity,
            }

        return {
            "standby_active": True,
            "resumed": False,
            "positions_resolved": positions_resolved,
            "exposure_reduction_pct": round(exposure_reduction_pct, 4),
            "useful_trade_capacity": useful_capacity,
            "standby_reason_codes": list(self.state.standby_reason_codes),
        }

    def get_status(self) -> dict:
        """Get current risk status summary."""
        return {
            "mode": "🔴 LIVE" if self.is_live else "🟡 PAPER",
            "trading_enabled": self.state.trading_enabled,
            "max_tradable_balance": f"${self.max_tradable_balance:.2f}" if self.max_tradable_balance > 0 else "unlimited",
            "max_position_size_usd": f"${self.max_position_size_usd:.2f}" if self.max_position_size_usd > 0 else "unlimited",
            "balance": f"${self.state.current_balance:.2f}",
            "available_cash": f"${self.state.available_cash:.2f}",
            "reserved_capital": f"${self.state.reserved_capital:.2f}",
            "pnl": f"${self.state.total_pnl:+.2f} ({self.state.total_pnl_pct:+.1f}%)",
            "drawdown": f"{self.state.drawdown_pct:.1f}%",
            "daily_pnl": f"${self.state.daily_pnl:+.2f} ({self.state.daily_pnl_pct:.1f}%)",
            "exposure": f"${self.state.total_exposure:.2f} ({self.state.exposure_pct:.1f}%)",
            "open_positions": self.state.open_positions,
            "win_rate": f"{self.state.win_rate:.1%}",
            "consecutive_losses": self.state.consecutive_losses,
            "cooldown": "YES" if self.state.is_in_cooldown else "no",
            "risk_headroom": f"{max(0, 100 - (abs(self.state.daily_pnl_pct) / (self.daily_loss_limit_pct * 100) * 100)):.0f}%",
            "standby_active": self.state.standby_active,
            "standby_entered_at": self.state.standby_entered_at or "",
            "standby_reason_codes": list(self.state.standby_reason_codes),
            "standby_blocked_scan_count": self.state.standby_blocked_scan_count,
            "standby_unresolved_positions_at_entry": self.state.standby_unresolved_positions_at_entry,
            "standby_exposure_at_entry": round(self.state.standby_exposure_at_entry, 2),
            "standby_available_cash_at_entry": round(self.state.standby_available_cash_at_entry, 2),
            "standby_last_resume_at": self.state.standby_last_resume_at or "",
            "standby_last_resume_reason": self.state.standby_last_resume_reason or "",
            "standby_useful_trade_capacity": f"${self._estimate_useful_trade_capacity():.2f}",
            "limits": {
                "kelly": f"{self.kelly_fraction:.0%}",
                "max_bet": f"{self.max_bet_pct:.0%}",
                "max_exposure": f"{self.max_exposure_pct * 100:.0f}%",
                "daily_loss": f"{self.daily_loss_limit_pct * 100:.0f}%",
                "max_drawdown": f"{self.max_drawdown_pct * 100:.0f}%",
            },
        }

    def _save_state(self):
        """Persist risk state to disk."""
        try:
            self.data_path.parent.mkdir(exist_ok=True)
            with open(self.data_path, "w") as f:
                json.dump(asdict(self.state), f, indent=2)
        except Exception as e:
            logger.debug(f"Failed to save risk state: {e}")

    def _load_state(self):
        """Load risk state from disk."""
        try:
            if self.data_path.exists():
                with open(self.data_path) as f:
                    data = json.load(f)
                self.state = RiskState(**data)
                logger.info(f"Risk state loaded: ${self.state.current_balance:.2f}, "
                           f"{self.state.open_positions} open positions")
        except Exception as e:
            logger.debug(f"Failed to load risk state: {e}")

    def _coerce_float(self, value, default: float = 0.0) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    def _find_trade_record(self, trade_ref):
        if isinstance(trade_ref, str) and trade_ref:
            for trade in self.state.trade_history:
                if trade.get("trade_id") == trade_ref:
                    return trade

        try:
            trade_idx = int(trade_ref)
        except (TypeError, ValueError):
            return None

        if 0 <= trade_idx < len(self.state.trade_history):
            return self.state.trade_history[trade_idx]
        return None
