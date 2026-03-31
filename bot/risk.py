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
    current_balance: float = 100.0
    peak_balance: float = 100.0

    # Session-level kill-switch tracking
    session_starting_balance: float = 100.0  # Balance at bot startup
    session_peak_balance: float = 100.0      # Highest balance this session
    max_drawdown_halt: bool = False           # Permanently halted by drawdown kill-switch

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

        # Detect mode: live = not demo
        self.is_live = os.getenv("KALSHI_USE_DEMO", "true").lower() == "false"
        preset = get_preset(self.is_live)

        # Resolve limits: env vars override preset, explicit config overrides both
        def resolve_float(key: str, default: float) -> float:
            env_key = key.upper()
            return float(os.getenv(env_key, config.get(key, preset.get(key, default))))

        def resolve_int(key: str, default: int) -> int:
            env_key = key.upper()
            return int(float(os.getenv(env_key, config.get(key, preset.get(key, default)))))

        self.kelly_fraction = resolve_float("kelly_fraction", preset["kelly_fraction"])
        self.max_bet_pct = resolve_float("max_bet_pct", preset["max_bet_pct"])
        self.max_exposure_pct = resolve_float("max_exposure_pct", preset["max_exposure_pct"])
        self.daily_loss_limit_pct = resolve_float("daily_loss_limit_pct", preset["daily_loss_limit_pct"])
        self.max_drawdown_pct = resolve_float("max_drawdown_pct", preset["max_drawdown_pct"])
        self.max_open_positions = resolve_int("max_open_positions", preset["max_open_positions"])
        self.cooldown_after_losses = resolve_int("cooldown_after_losses", preset["cooldown_after_losses"])

        # Session-level kill-switch: halt permanently if balance falls this far below
        # max(session_starting_balance, session_peak_balance).
        # Requires manual reset (delete data/risk_state.json or set FORCE_RESUME=true).
        self.max_session_drawdown_pct = float(
            os.getenv("MAX_DRAWDOWN_PCT", config.get("max_session_drawdown_pct", 0.20))
        )

        # Stress scaling — more lenient as bankroll grows
        self.stress_threshold = config.get("stress_threshold", 0.8)
        self.stress_reduction = config.get("stress_reduction", 0.3)

        starting = config.get("starting_balance", 100.0)
        # State
        self.state = RiskState(
            starting_balance=starting,
            current_balance=starting,
            peak_balance=starting,
            session_starting_balance=starting,
            session_peak_balance=starting,
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

    def check_trade(self, signal: dict, position_size: float) -> RiskDecision:
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

        # === Hard stops (reject immediately) ===

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

        # 4. Max exposure — total dollars at risk
        projected_exposure = self.state.total_exposure + position_size
        projected_exposure_pct = (projected_exposure / self.state.current_balance * 100) if self.state.current_balance > 0 else 0
        if projected_exposure_pct > self.max_exposure_pct * 100:
            return RiskDecision(
                approved=False,
                reason=f"Max exposure ({projected_exposure_pct:.1f}% / {self.max_exposure_pct * 100:.0f}% of ${self.state.current_balance:.2f})",
                risk_score=1.0,
            )

        # 5. Cooldown
        if self.state.is_in_cooldown:
            return RiskDecision(
                approved=False,
                reason=f"In cooldown (after {self.state.consecutive_losses} consecutive losses)",
                risk_score=0.9,
            )

        # === Soft limits (reduce size) ===

        risk_score = 0.0

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

        # Minimum position size: $1
        position_size = max(1.0, round(position_size, 2))
        risk_score = min(1.0, risk_score)

        return RiskDecision(
            approved=True,
            reason="Approved" + (f" (with {len(warnings)} warnings)" if warnings else ""),
            adjusted_size=position_size,
            original_size=original_size,
            risk_score=risk_score,
            warnings=warnings,
        )

    def record_trade(self, trade: dict):
        """Record a trade for risk tracking."""
        size = self._coerce_float(trade.get("position_size"))
        if size <= 0:
            return
        self.state.trade_history.append({
            "trade_id": trade.get("id", ""),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "question": trade.get("question", ""),
            "direction": trade.get("direction", ""),
            "size": size,
            "market_price": self._coerce_float(trade.get("market_price")),
            "resolved": False,
            "pnl": 0,
        })
        self.state.open_positions += 1
        self.state.total_exposure += size
        self.state.daily_trades += 1
        self._save_state()

    def record_outcome(self, trade_ref, pnl: float):
        """Record the outcome of a resolved trade."""
        self.reset_daily()
        trade = self._find_trade_record(trade_ref)
        if trade is not None:
            if trade.get("resolved"):
                return

            trade["resolved"] = True
            trade["pnl"] = self._coerce_float(pnl)

            # Release exposure (approximate — full size released on resolve)
            self.state.total_exposure = max(0, self.state.total_exposure - trade.get("size", 0))

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

    def get_status(self) -> dict:
        """Get current risk status summary."""
        return {
            "mode": "🔴 LIVE" if self.is_live else "🟡 PAPER",
            "balance": f"${self.state.current_balance:.2f}",
            "pnl": f"${self.state.total_pnl:+.2f} ({self.state.total_pnl_pct:+.1f}%)",
            "drawdown": f"{self.state.drawdown_pct:.1f}%",
            "daily_pnl": f"${self.state.daily_pnl:+.2f} ({self.state.daily_pnl_pct:.1f}%)",
            "exposure": f"${self.state.total_exposure:.2f} ({self.state.exposure_pct:.1f}%)",
            "open_positions": self.state.open_positions,
            "win_rate": f"{self.state.win_rate:.1%}",
            "consecutive_losses": self.state.consecutive_losses,
            "cooldown": "YES" if self.state.is_in_cooldown else "no",
            "risk_headroom": f"{max(0, 100 - (abs(self.state.daily_pnl_pct) / (self.daily_loss_limit_pct * 100) * 100)):.0f}%",
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
