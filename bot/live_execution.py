"""Live-mode execution boundary for the runner."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Protocol

from bot.exchanges.base import BaseExchange
from bot.shared_core import TradeContext, TradeDecision

logger = logging.getLogger(__name__)


class LiveExecutionHost(Protocol):
    open_positions: list[Any]
    open_orders: list[dict]
    trade_history: list[dict]
    risk: Any
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
        reserved_capital = sum(position.size for position in self.host.open_positions)
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

        return TradeContext(
            exchange=signal.get("exchange", "unknown"),
            market_id=signal.get("market_id", ""),
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
            metadata={"runner": "live"},
        )

    def execute(self, signal: dict, decision: TradeDecision, exchange: BaseExchange) -> dict | None:
        market_id = signal["market_id"]
        side = "YES" if decision.action == "BUY_YES" else "NO"

        try:
            market_bid_ask = exchange.get_market_bid_ask(market_id)
            if market_bid_ask and market_bid_ask.get("best_yes_ask", 0) > 0:
                yes_ask = market_bid_ask.get("best_yes_ask", 0)
                no_ask = market_bid_ask.get("best_no_ask", 0)
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

        price = yes_ask if side == "YES" else no_ask
        price = max(0.01, min(price, 0.99))
        size = float(decision.position_size or 0.0)

        if size < 1:
            logger.info(f"Position too small after shared risk controls: ${size:.2f}")
            return None

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
