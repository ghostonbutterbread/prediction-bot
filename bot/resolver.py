"""
Trade Resolver — Checks open paper trades and computes hypothetical P&L.

For each unresolved trade:
1. Fetch current market state via exchange API
2. If market resolved: compute final P&L (YES wins = +size*(1-entry), NO wins = +size*(1-entry), 
   wrong side = -size*entry_price)
3. If market still open: compute unrealized P&L based on price movement
4. Update the trade record and session balance

Usage: standalone or integrated into simulator scan loop.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class TradeResolver:
    """
    Resolves simulated trades by checking market outcomes.
    
    P&L logic (per contract, where each contract pays $1 on win):
      BUY_YES at price P:
        - Market resolves YES → gross profit = (1 - P) per contract
        - Market resolves NO  → loss   = -P per contract
        - Still open, price now Q → unrealized = (Q - P) per contract
      
      BUY_NO at price P:
        - Market resolves NO  → gross profit = (1 - P) per contract  
        - Market resolves YES → loss   = -P per contract
        - Still open, NO price now Q → unrealized = (Q - P) per contract

    Kalshi fees: 7% of net profit on wins. No fee on losses.
    """

    KALSHI_FEE_RATE = 0.07  # 7% on profits

    def __init__(self, data_dir: str = "data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def resolve_session(self, session_id: str, exchange, risk_manager=None) -> dict:
        """
        Resolve all open trades in a simulation session.
        
        Args:
            session_id: The session ID to resolve.
            exchange: Exchange instance for fetching market data.
            risk_manager: Optional RiskManager instance. If provided, record_outcome
                         will be called for each resolved trade to keep risk state in sync.
        
        Returns summary: {resolved: N, still_open: N, total_pnl: float}
        """
        session_file = self.data_dir / f"sim_{session_id}.json"
        if not session_file.exists():
            logger.error(f"Session not found: {session_file}")
            return {"error": "session not found"}

        with open(session_file) as f:
            data = json.load(f)

        trades = data.get("trades", [])
        if not trades:
            return {"resolved": 0, "still_open": 0, "total_pnl": 0.0}

        resolved_count = 0
        still_open_count = 0

        for trade in trades:
            if trade.get("resolved"):
                continue  # Already resolved

            market_id = trade.get("market_id", "")
            direction = trade.get("direction", "")
            entry_price = self._normalize_entry_price(
                direction,
                trade.get("market_price"),
                trade.get("model_probability"),
            )
            position_size = self._coerce_float(trade.get("position_size"))

            if not market_id or entry_price is None or position_size <= 0:
                continue

            try:
                # Fetch current market state
                market = exchange.get_market(market_id)
                if market is None:
                    still_open_count += 1
                    continue

                market_status = str(market.metadata.get("status", "")).strip().lower() if market.metadata else ""
                current_yes_price, current_no_price = self._extract_market_prices(market)

                # Check if market is resolved/settled
                if market_status in ("settled", "resolved", "closed"):
                    # Market resolved — determine winner
                    outcome = self._determine_outcome(market)
                    if outcome == "UNKNOWN":
                        still_open_count += 1
                        continue
                    pnl = self._calculate_realized_pnl(
                        direction, entry_price, position_size, outcome
                    )

                    trade["resolved"] = True
                    trade["outcome"] = outcome
                    trade["pnl"] = round(pnl, 4)
                    trade["resolved_at"] = datetime.now(timezone.utc).isoformat()
                    trade["resolution_type"] = "settled"
                    trade["market_price"] = round(entry_price, 4)

                    resolved_count += 1

                    # Sync outcome to RiskManager so streaks/drawdown/daily_PnL update
                    if risk_manager is not None:
                        risk_manager.record_outcome(trade.get("id") or market_id, pnl)

                    logger.info(
                        f"  ✅ Resolved: {trade['question'][:50]}... | "
                        f"{direction} @ ${entry_price:.2f} | "
                        f"Outcome: {outcome} | P&L: ${pnl:+.4f}"
                    )

                elif self._is_market_closed(market):
                    # Market closed but not yet settled — keep tracking mark-to-market
                    # P&L, but do not realize it or close the position.
                    current_price = current_no_price if direction == "BUY_NO" else current_yes_price
                    if current_price is None:
                        still_open_count += 1
                        continue
                    pnl = self._calculate_unrealized_pnl(
                        direction, entry_price, current_price, position_size
                    )

                    trade["resolved"] = False
                    trade["outcome"] = "pending_settlement"
                    trade["resolution_type"] = "closed_unsettled"
                    trade["exit_price"] = current_price
                    trade["current_price"] = current_price
                    trade["unrealized_pnl"] = round(pnl, 4)
                    trade["price_delta"] = round(current_price - entry_price, 4)
                    trade["pnl"] = None
                    trade["resolved_at"] = None
                    still_open_count += 1

                    logger.info(
                        f"  ⏳ Closed (unsettled): {trade['question'][:50]}... | "
                        f"{direction} @ ${entry_price:.2f} → ${current_price:.2f} | "
                        f"Unrealized P&L: ${pnl:+.4f}"
                    )

                else:
                    # Still open — compute unrealized P&L
                    current_price = current_no_price if direction == "BUY_NO" else current_yes_price
                    if current_price is None:
                        still_open_count += 1
                        continue
                    pnl = self._calculate_unrealized_pnl(
                        direction, entry_price, current_price, position_size
                    )

                    trade["current_price"] = current_price
                    trade["unrealized_pnl"] = round(pnl, 4)
                    trade["price_delta"] = round(current_price - entry_price, 4)
                    trade["market_price"] = round(entry_price, 4)
                    still_open_count += 1

            except Exception as e:
                logger.debug(f"Error resolving {market_id}: {e}")
                still_open_count += 1
                continue

        # Update session data
        data["trades"] = trades
        data["last_resolved_at"] = datetime.now(timezone.utc).isoformat()

        # Recalculate balance
        total_realized_pnl = sum(
            self._coerce_float(t.get("pnl")) for t in trades if t.get("resolved")
        )
        data["balance"] = round(data.get("starting_balance", 100) + total_realized_pnl, 2)

        # Recalculate report
        data["report"] = self._build_report(data)

        # Save
        with open(session_file, "w") as f:
            json.dump(data, f, indent=2, default=str)

        summary = {
            "session_id": session_id,
            "resolved_this_pass": resolved_count,
            "still_open": still_open_count,
            "total_resolved": sum(1 for t in trades if t.get("resolved")),
            "total_trades": len(trades),
            "session_pnl": round(total_realized_pnl, 4),
            "balance": data["balance"],
        }

        logger.info(
            f"\n📊 Resolution Summary: "
            f"{resolved_count} newly resolved, "
            f"{still_open_count} still open | "
            f"Session P&L: ${total_realized_pnl:+.4f} | "
            f"Balance: ${data['balance']:.2f}"
        )

        return summary

    def _determine_outcome(self, market) -> str:
        """
        Determine market outcome from market data.
        Returns "YES" or "NO".
        """
        # Check metadata for resolution info
        if market.metadata:
            result = market.metadata.get("result")
            normalized = self._normalize_outcome_value(result)
            if normalized:
                return normalized

            result = market.metadata.get("outcome")
            normalized = self._normalize_outcome_value(result)
            if normalized:
                return normalized

        # Fallback: if one price is at/near $1.00, that side won
        yes_price, no_price = self._extract_market_prices(market)
        if yes_price is not None and yes_price >= 0.99:
            return "YES"
        if no_price is not None and no_price >= 0.99:
            return "NO"

        # Default: can't determine
        return "UNKNOWN"

    def _calculate_realized_pnl(
        self, direction: str, entry_price: float, size: float, outcome: str
    ) -> float:
        """
        Calculate realized P&L for a settled market (after Kalshi 7% fee on wins).
        
        Contracts purchased = size / entry_price (how many $1 contracts you bought)
        If you win: gross profit = contracts * (1 - entry_price), fee = 7% of gross
        If you lose: you paid entry_price per contract, so loss = -size
        """
        if size <= 0 or not (0 < entry_price < 1):
            return 0.0

        contracts = size / entry_price if entry_price > 0 else 0

        if direction == "BUY_YES":
            if outcome == "YES":
                gross = contracts * (1 - entry_price)
                fee = gross * self.KALSHI_FEE_RATE
                return gross - fee
            elif outcome == "NO":
                return -size  # Lost entire position
            else:
                return 0  # Unknown outcome
        else:  # BUY_NO
            if outcome == "NO":
                gross = contracts * (1 - entry_price)
                fee = gross * self.KALSHI_FEE_RATE
                return gross - fee
            elif outcome == "YES":
                return -size  # Lost entire position
            else:
                return 0

    def _calculate_unrealized_pnl(
        self, direction: str, entry_price: float, current_price: float, size: float
    ) -> float:
        """
        Calculate unrealized P&L based on current market price.
        
        contracts = size / entry_price
        P&L = contracts * (current_price - entry_price) for BUY_YES
        P&L = contracts * ((1 - current_price) - (1 - entry_price)) for BUY_NO
             = contracts * (entry_price - current_price) for BUY_NO
        """
        if size <= 0 or not (0 < entry_price < 1) or current_price is None:
            return 0.0

        contracts = size / entry_price if entry_price > 0 else 0

        if direction == "BUY_YES":
            return contracts * (current_price - entry_price)
        else:  # BUY_NO
            return contracts * (entry_price - current_price)

    def _build_report(self, data: dict) -> dict:
        """Rebuild the session report from current trade data."""
        trades = data.get("trades", [])
        total = len(trades)
        if total == 0:
            return {"session": data.get("session_id", ""), "total_trades": 0}

        resolved_trades = [t for t in trades if t.get("resolved")]
        wins = [t for t in resolved_trades if (t.get("pnl") or 0) > 0]
        losses = [t for t in resolved_trades if (t.get("pnl") or 0) < 0]

        edges = [t.get("edge", 0) for t in trades]
        confidences = [t.get("confidence", 0) for t in trades]
        sizes = [t.get("position_size", 0) for t in trades]
        pnls = [self._coerce_float(t.get("pnl")) for t in resolved_trades]

        by_direction = {}
        for t in trades:
            d = t.get("direction", "unknown")
            by_direction[d] = by_direction.get(d, 0) + 1

        return {
            "session": data.get("session_id", ""),
            "total_trades": total,
            "resolved_trades": len(resolved_trades),
            "open_trades": total - len(resolved_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(resolved_trades), 4) if resolved_trades else 0,
            "starting_balance": data.get("starting_balance", 100),
            "current_balance": data.get("balance", 100),
            "pnl": round(data.get("balance", 100) - data.get("starting_balance", 100), 2),
            "pnl_pct": round(
                (data.get("balance", 100) - data.get("starting_balance", 100))
                / data.get("starting_balance", 100) * 100, 2
            ),
            "avg_edge": round(sum(edges) / len(edges), 4) if edges else 0,
            "max_edge": round(max(edges), 4) if edges else 0,
            "avg_confidence": round(sum(confidences) / len(confidences), 4) if confidences else 0,
            "avg_position_size": round(sum(sizes) / len(sizes), 2) if sizes else 0,
            "total_exposure": round(sum(sizes), 2),
            "total_realized_pnl": round(sum(pnls), 4) if pnls else 0,
            "avg_pnl_per_trade": round(sum(pnls) / len(pnls), 4) if pnls else 0,
            "by_direction": by_direction,
        }

    def resolve_latest(self, exchange, risk_manager=None) -> dict:
        """Resolve the most recent session."""
        sessions = sorted(self.data_dir.glob("sim_*.json"), reverse=True)
        if not sessions:
            return {"error": "no sessions found"}

        session_id = sessions[0].stem.replace("sim_", "")
        return self.resolve_session(session_id, exchange, risk_manager)

    def resolve_all_open(self, exchange, risk_manager=None) -> list[dict]:
        """Resolve all sessions that have open trades."""
        results = []
        for session_file in sorted(self.data_dir.glob("sim_*.json"), reverse=True):
            with open(session_file) as f:
                data = json.load(f)

            open_trades = [t for t in data.get("trades", []) if not t.get("resolved")]
            if open_trades:
                session_id = session_file.stem.replace("sim_", "")
                result = self.resolve_session(session_id, exchange, risk_manager)
                results.append(result)

        return results

    def _coerce_float(self, value, default: float = 0.0) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    def _extract_market_prices(self, market) -> tuple[Optional[float], Optional[float]]:
        yes_price = self._coerce_float(getattr(market, "yes_price", None), default=None)
        no_price = self._coerce_float(getattr(market, "no_price", None), default=None)

        if yes_price is None and no_price is not None:
            yes_price = round(1 - no_price, 4)
        if no_price is None and yes_price is not None:
            no_price = round(1 - yes_price, 4)

        return yes_price, no_price

    def _normalize_outcome_value(self, value) -> Optional[str]:
        if isinstance(value, bool):
            return "YES" if value else "NO"
        if isinstance(value, (int, float)) and value in (0, 1):
            return "YES" if int(value) == 1 else "NO"
        if isinstance(value, str):
            normalized = value.strip().upper()
            aliases = {
                "YES": "YES",
                "NO": "NO",
                "TRUE": "YES",
                "FALSE": "NO",
                "WIN": "YES",
                "LOSE": "NO",
                "WON": "YES",
                "LOST": "NO",
                "1": "YES",
                "0": "NO",
            }
            return aliases.get(normalized)
        return None

    def _normalize_entry_price(
        self, direction: str, market_price, model_probability
    ) -> Optional[float]:
        raw_price = self._coerce_float(market_price, default=None)
        model_prob = self._coerce_float(model_probability, default=0.5)

        if raw_price is None or not (0 < raw_price < 1):
            return None
        if not (0 <= model_prob <= 1):
            return None

        if direction == "BUY_NO":
            same_side_edge = model_prob - raw_price
            flipped_edge = (1 - model_prob) - (1 - raw_price)
            return round((1 - raw_price) if flipped_edge > same_side_edge else raw_price, 4)

        return round(raw_price, 4)

    def _is_market_closed(self, market) -> bool:
        closes_at = getattr(market, "closes_at", None)
        if closes_at is None:
            return False
        if isinstance(closes_at, str):
            try:
                closes_at = datetime.fromisoformat(closes_at)
            except ValueError:
                return False
        if closes_at.tzinfo is None:
            closes_at = closes_at.replace(tzinfo=timezone.utc)
        return closes_at < datetime.now(timezone.utc)
