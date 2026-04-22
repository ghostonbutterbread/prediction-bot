"""Live-mode execution boundary for the runner."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Protocol

from bot.exchanges.base import BaseExchange
from bot.shared_core import TradeContext, TradeDecision
from bot.trade_audit import trade_event_key

logger = logging.getLogger(__name__)


class LiveExecutionHost(Protocol):
    open_positions: list[Any]
    open_orders: list[dict]
    trade_history: list[dict]
    risk: Any
    kelly: Any
    config: dict
    live_sync: Any
    single_trade_mode: bool
    single_trade_completed: bool

    def _coerce_float(self, value, default: float = 0.0) -> float:
        ...

    def _log_trade(self, signal: dict, order, decision, size: float, price: float):
        ...


class RunnerLiveExecutionAdapter:
    """Executes approved shared-core decisions against a live exchange."""

    def __init__(self, host: LiveExecutionHost):
        self.host = host

    def build_trade_context(self, signal: dict, exchange: BaseExchange, config: dict) -> TradeContext:
        from bot.shared_core import AccountState

        balance = self.host._coerce_float(getattr(exchange, "get_balance", lambda: 0.0)(), default=0.0)
        reserved_positions = sum(position.size for position in self.host.open_positions)
        reserved_orders = sum(float(order.get("remaining_size", 0.0) or 0.0) for order in self.host.open_orders)
        reserved_capital = reserved_positions + reserved_orders
        available_cash = max(0.0, balance - reserved_capital)
        total_exposure = reserved_capital
        effective_tradable_cash = available_cash
        if self.host.risk.max_tradable_balance and self.host.risk.max_tradable_balance > 0:
            effective_tradable_cash = min(effective_tradable_cash, self.host.risk.max_tradable_balance)

        account_state = AccountState(
            starting_balance=self.host.risk.state.starting_balance,
            current_balance=balance,
            available_cash=available_cash,
            reserved_capital=reserved_capital,
            total_exposure=total_exposure,
            open_positions=len(self.host.open_positions),
            daily_pnl=self.host.risk.state.daily_pnl,
            drawdown_pct=self.host.risk.state.drawdown_pct,
            consecutive_losses=self.host.risk.state.consecutive_losses,
            consecutive_wins=self.host.risk.state.consecutive_wins,
            metadata={
                "effective_tradable_cash": round(effective_tradable_cash, 2),
                "mode": config.get("trading", {}).get("mode", "paper"),
            },
        )

        self.host.risk.sync_account_state(
            current_balance=balance,
            available_cash=available_cash,
            reserved_capital=reserved_capital,
            total_exposure=total_exposure,
            open_positions=len(self.host.open_positions),
        )

        event_key = trade_event_key(signal)
        candidate_market_id = str(signal.get("market_id", "") or "")
        candidate_direction = str(signal.get("direction", "BUY_YES") or "BUY_YES").upper()
        event_entries: list[dict[str, Any]] = []
        filled_event_entries: list[dict[str, Any]] = []
        pending_event_entries: list[dict[str, Any]] = []
        for position in self.host.open_positions:
            position_event_key = getattr(position, "event_key", "") or trade_event_key(
                {"market_id": getattr(position, "market_id", ""), "question": getattr(position, "question", "")}
            )
            if position_event_key != event_key:
                continue
            entry = {
                "market_id": position.market_id,
                "direction": str(position.direction or "BUY_YES").upper(),
                "reserved_capital": float(position.size or 0.0),
                "entry_price": float(position.price or 0.0),
                "family_key": self._market_family_key(position.market_id),
                "lifecycle_state": "filled",
            }
            event_entries.append(entry)
            filled_event_entries.append(entry)
        for order in self.host.open_orders:
            order_event_key = order.get("event_key") or trade_event_key(order)
            if order_event_key != event_key:
                continue
            entry = {
                "market_id": order.get("market_id", ""),
                "direction": str(order.get("direction", "BUY_YES") or "BUY_YES").upper(),
                "reserved_capital": float(order.get("remaining_size", 0.0) or 0.0),
                "entry_price": float(order.get("price", 0.0) or 0.0),
                "family_key": self._market_family_key(order.get("market_id", "")),
                "lifecycle_state": "pending",
            }
            event_entries.append(entry)
            pending_event_entries.append(entry)
        same_market_entries = [entry for entry in event_entries if entry["market_id"] == candidate_market_id]
        candidate_family_key = self._market_family_key(candidate_market_id)
        same_family_entries = [entry for entry in event_entries if entry["family_key"] == candidate_family_key]
        event_entry_prices = [entry["entry_price"] for entry in same_market_entries if 0 < entry["entry_price"] < 1]
        return TradeContext(
            exchange=signal.get("exchange", "unknown"),
            market_id=candidate_market_id,
            question=signal.get("question", ""),
            direction=signal.get("direction", "BUY_YES"),
            market_price=signal.get("market_price"),
            yes_price=signal.get("yes_price", signal.get("market_price")),
            no_price=signal.get("no_price"),
            model_probability=signal.get("model_probability"),
            edge=signal.get("edge"),
            confidence=signal.get("confidence"),
            account_state=account_state,
            source_context=dict(signal),
            metadata={
                "runner": "live",
                "event_key": event_key,
                "market_family_key": candidate_family_key,
                "event_snapshot": {
                    "event_key": event_key,
                    "candidate_family_key": candidate_family_key,
                    "event_position_count_before": len(event_entries),
                    "event_entries_count": len(event_entries),
                    "event_exposure_before": round(sum(entry["reserved_capital"] for entry in event_entries), 2),
                    "filled_event_exposure_before": round(sum(entry["reserved_capital"] for entry in filled_event_entries), 2),
                    "pending_event_exposure_before": round(sum(entry["reserved_capital"] for entry in pending_event_entries), 2),
                    "filled_event_position_count_before": len(filled_event_entries),
                    "pending_event_position_count_before": len(pending_event_entries),
                    "max_event_exposure_pct": self.host.risk.max_event_exposure_pct,
                    "held_market_ids": [entry["market_id"] for entry in event_entries],
                    "same_event_directions": [entry["direction"] for entry in event_entries],
                    "same_family_markets": [entry["market_id"] for entry in same_family_entries],
                    "same_family_positions": list(same_family_entries),
                    "opposite_side_detected": any(entry["direction"] != candidate_direction for entry in event_entries),
                    "event_entry_prices": event_entry_prices,
                    "best_same_market_entry_price": min(event_entry_prices) if event_entry_prices else None,
                    "best_same_family_entry_price": min((entry["entry_price"] for entry in same_family_entries if 0 < entry["entry_price"] < 1), default=None),
                    "liquidity": self.host._coerce_float(signal.get("liquidity"), default=0.0) or None,
                    "best_yes_ask": self.host._coerce_float(signal.get("best_yes_ask", signal.get("yes_price")), default=0.0) or None,
                    "best_no_ask": self.host._coerce_float(signal.get("best_no_ask", signal.get("no_price")), default=0.0) or None,
                    "best_yes_bid": self.host._coerce_float(signal.get("best_yes_bid"), default=0.0) or None,
                    "best_no_bid": self.host._coerce_float(signal.get("best_no_bid"), default=0.0) or None,
                    "estimated_fill_price": self.host._coerce_float(signal.get("no_price" if candidate_direction == "BUY_NO" else "yes_price"), default=0.0) or None,
                },
                "retrade_policy": {
                    "max_event_exposure_pct": self.host.risk.max_event_exposure_pct,
                    "max_event_positions": self.host.risk.max_event_positions,
                    "retrade_edge_premium": self.host.risk.retrade_edge_premium,
                    "retrade_confidence_premium": self.host.risk.retrade_confidence_premium,
                    "retrade_size_decay": self.host.risk.retrade_size_decay,
                    "strict_event_overlap": self.host.risk.strict_event_overlap,
                    "min_retrade_net_edge": self.host.risk.min_retrade_net_edge,
                    "min_retrade_expected_profit_usd": getattr(self.host.risk, "min_retrade_expected_profit_usd", 0.0),
                    "require_price_improvement_for_same_market_family": self.host.risk.require_price_improvement_for_same_market_family,
                    "price_improvement_ticks": self.host.risk.price_improvement_ticks,
                    "fee_rate": getattr(getattr(self.host, "kelly", None), "fee_rate", 0.07),
                },
            },
        )

    @staticmethod
    def _market_family_key(market_id: str) -> str:
        market_id = str(market_id or "")
        if not market_id:
            return ""
        parts = market_id.split("-")
        return "-".join(parts[:-1]) if len(parts) > 1 else market_id

    def execute(self, signal: dict, decision: TradeDecision, exchange: BaseExchange) -> dict | None:
        from bot.shared_core import build_trade_decision

        market_id = signal["market_id"]
        side = "YES" if decision.action == "BUY_YES" else "NO"

        try:
            market_bid_ask = exchange.get_market_bid_ask(market_id)
            if market_bid_ask and market_bid_ask.get("best_yes_ask", 0) > 0:
                yes_ask = market_bid_ask.get("best_yes_ask", 0)
                no_ask = market_bid_ask.get("best_no_ask", 0)
                yes_bid = market_bid_ask.get("best_yes_bid", 0)
                no_bid = market_bid_ask.get("best_no_bid", 0)
            else:
                logger.warning(f"No market price data for {market_id} - skipping")
                return None
        except Exception as e:
            logger.debug(f"Could not fetch market bid/ask for {market_id}: {e}")
            yes_ask = signal.get("market_price", 0.50)
            if yes_ask <= 0 or yes_ask >= 1:
                logger.warning(f"No valid market price for {market_id} - skipping")
                return None
            no_ask = 1 - yes_ask
            yes_bid = max(0.0, yes_ask - 0.01)
            no_bid = max(0.0, no_ask - 0.01)

        live_signal = dict(signal)
        live_signal["yes_price"] = yes_ask
        live_signal["no_price"] = no_ask
        live_signal["best_yes_ask"] = yes_ask
        live_signal["best_no_ask"] = no_ask
        live_signal["best_yes_bid"] = yes_bid
        live_signal["best_no_bid"] = no_bid
        live_signal["market_price"] = yes_ask if decision.action == "BUY_YES" else no_ask

        strategy_cfg = self.host.config.get("strategy", {}) or {}
        live_context = self.build_trade_context(live_signal, exchange, self.host.config)
        live_decision = build_trade_decision(
            live_context,
            kelly_sizer=self.host.kelly,
            risk_policy=self.host.risk,
            min_edge=strategy_cfg.get("min_edge", self.host.config.get("min_edge", 0.02)),
            min_confidence=strategy_cfg.get("min_confidence", self.host.config.get("min_confidence", 0.50)),
            max_entry_price=self.host.config.get("max_entry_price", 0.70),
        )
        if not live_decision.approved:
            logger.info(f"🛑 Live revalidation skipped: {live_decision.reason}")
            return None

        price = max(0.01, min(float(live_decision.entry_price or 0.0), 0.99))
        size = float(live_decision.position_size or 0.0)

        if size < 1:
            logger.info(f"Position too small after shared risk controls: ${size:.2f}")
            return None

        decision = live_decision
        order = exchange.place_order(market_id, side, price, size)
        if not order:
            return None

        from bot.runner import LivePosition

        order_id = order.id if hasattr(order, "id") else str(order)
        self.host.open_positions.append(
            LivePosition(
                market_id=market_id,
                question=signal.get("question", ""),
                direction=decision.action,
                price=price,
                size=size,
                order_id=order_id,
                created_at=datetime.now(timezone.utc).isoformat(),
                event_key=trade_event_key(signal),
            )
        )
        self.host.trade_history.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "market_id": market_id,
                "question": signal.get("question", ""),
                "direction": decision.action,
                "size": size,
                "price": price,
                "resolved": False,
                "order_id": order_id,
                "decision_reason": decision.reason,
                "event_key": trade_event_key(signal),
                "decision_trace": dict(getattr(decision, "reasoning", {}) or {}),
            }
        )
        refresh = self.host.live_sync.refresh_after_execution(exchange)

        logger.info(
            f"✅ Trade executed: {side} ${size:.2f} @ ${price:.4f} on {signal['exchange']}/{market_id}"
        )
        self.host._log_trade(signal, order, decision, size, price)
        balance_after = self.host._coerce_float(getattr(exchange, "get_balance", lambda: 0.0)(), default=0.0)
        reserved_capital = sum(position.size for position in self.host.open_positions)
        self.host._log_lifecycle_event(
            "trade_placed",
            {
                "exchange": signal.get("exchange", "unknown"),
                "market_id": market_id,
                "question": signal.get("question", ""),
                "direction": decision.action,
                "size": size,
                "price": price,
                "confidence": signal.get("confidence"),
                "edge": signal.get("edge"),
                "mode": "single_trade" if self.host.single_trade_mode else "normal",
                "balance_after": balance_after,
                "reserved_capital": reserved_capital,
                "available_cash": max(0.0, balance_after - reserved_capital),
                "tradable_cap": getattr(self.host.risk, "max_tradable_balance", None),
            },
        )
        return {"order": order, "signal": signal, "decision": decision, "refresh": refresh}
