"""Live-mode state and reconciliation adapters for the runner."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from bot.exchanges.base import BaseExchange, Position, RestingOrder
from bot.shared_core import AccountState, OrderState, PositionState, ResolutionEvent

logger = logging.getLogger(__name__)


class LiveRunnerHost(Protocol):
    config: dict
    risk: Any
    open_positions: list[Any]
    open_orders: list[dict]
    trade_history: list[dict]

    def _coerce_float(self, value, default: float = 0.0) -> float:
        ...


@dataclass(slots=True)
class LiveReconciliationSnapshot:
    exchange: str
    open_positions: list[Any]
    open_orders: list[dict]
    trade_history_rows: list[dict]
    reserved_capital: float
    available_cash: float
    partial_fills: int


class RunnerLiveStateAdapter:
    """Live state boundary backed by exchange truth and runner memory."""

    def __init__(self, host: LiveRunnerHost):
        self.host = host

    def get_account_state(self) -> AccountState:
        balance = self.host.risk.state.current_balance
        reserved = sum(position.size for position in self.host.open_positions) + sum(
            order.get("remaining_size", 0.0) for order in self.host.open_orders
        )
        available_cash = max(0.0, balance - reserved)
        tradable_cash = available_cash
        if self.host.risk.max_tradable_balance and self.host.risk.max_tradable_balance > 0:
            tradable_cash = min(tradable_cash, self.host.risk.max_tradable_balance)
        return AccountState(
            starting_balance=self.host.risk.state.starting_balance,
            current_balance=balance,
            available_cash=available_cash,
            reserved_capital=reserved,
            total_exposure=reserved,
            open_positions=len(self.host.open_positions),
            daily_pnl=self.host.risk.state.daily_pnl,
            drawdown_pct=self.host.risk.state.drawdown_pct,
            consecutive_losses=self.host.risk.state.consecutive_losses,
            consecutive_wins=self.host.risk.state.consecutive_wins,
            metadata={
                "mode": self.host.config.get("trading", {}).get("mode", "live"),
                "effective_tradable_cash": round(tradable_cash, 2),
                "resting_orders": len(self.host.open_orders),
            },
        )

    def list_open_positions(self) -> list[PositionState]:
        return [
            PositionState(
                position_id=position.order_id,
                market_id=position.market_id,
                question=position.question,
                direction=position.direction,
                opened_at=position.created_at,
                status="open",
                entry_price=position.price,
                position_size=position.size,
                reserved_capital=position.size,
                metadata={"source": "live_reconciled"},
            )
            for position in self.host.open_positions
        ]

    def list_resting_orders(self) -> list[OrderState]:
        return [
            OrderState(
                order_id=order["order_id"],
                market_id=order["market_id"],
                direction=order["direction"],
                status=order.get("status", "open"),
                requested_size=order.get("requested_size", 0.0),
                filled_size=order.get("filled_size", 0.0),
                remaining_size=order.get("remaining_size", 0.0),
                limit_price=order.get("price"),
                created_at=order.get("created_at", ""),
                metadata={"question": order.get("question", "")},
            )
            for order in self.host.open_orders
        ]


class RunnerLiveReconciliationAdapter:
    """Normalizes live exchange state into runner state."""

    def __init__(self, host: LiveRunnerHost):
        self.host = host

    def reconcile(self, exchange_name: str, exchange: BaseExchange) -> LiveReconciliationSnapshot:
        positions = getattr(exchange, "get_positions", lambda: [])() or []
        resting_orders = getattr(exchange, "get_resting_orders", lambda: [])() or []
        reconciled_positions = [p for idx, p in enumerate((self._normalize_position(exchange_name, p, i) for i, p in enumerate(positions, start=1)), start=1) if p]
        trade_history_rows = [
            {
                "timestamp": position.created_at,
                "market_id": position.market_id,
                "question": position.question,
                "direction": position.direction,
                "size": position.size,
                "price": position.price,
                "resolved": False,
                "order_id": position.order_id,
                "decision_reason": "reconciled_from_exchange",
                "reconciled": True,
            }
            for position in reconciled_positions
        ]
        reconciled_orders = [o for o in (self._normalize_resting_order(exchange_name, order) for order in resting_orders) if o]
        balance = self.host._coerce_float(getattr(exchange, "get_balance", lambda: 0.0)(), default=self.host.risk.state.current_balance)
        reserved_positions = sum(position.size for position in reconciled_positions)
        reserved_orders = sum(order.get("remaining_size", 0.0) for order in reconciled_orders)
        reserved_total = reserved_positions + reserved_orders
        available_cash = max(0.0, balance - reserved_total)
        partial_fills = sum(1 for order in reconciled_orders if order.get("filled_size", 0.0) > 0 and order.get("remaining_size", 0.0) > 0)
        return LiveReconciliationSnapshot(
            exchange=exchange_name,
            open_positions=reconciled_positions,
            open_orders=reconciled_orders,
            trade_history_rows=trade_history_rows,
            reserved_capital=round(reserved_total, 2),
            available_cash=round(available_cash, 2),
            partial_fills=partial_fills,
        )

    def settle(self, exchange_name: str, exchange: BaseExchange, open_positions: list[Any]) -> list[ResolutionEvent]:
        events: list[ResolutionEvent] = []
        for position in open_positions:
            market = getattr(exchange, "get_market", lambda market_id: None)(position.market_id)
            if market is None:
                continue
            outcome = market.metadata.get("result") or market.metadata.get("outcome")
            if market.close_price is None and not outcome:
                continue
            settlement_value = market.close_price
            if settlement_value is None:
                if str(outcome).upper() == "YES":
                    settlement_value = 1.0
                elif str(outcome).upper() == "NO":
                    settlement_value = 0.0
                else:
                    continue
            won = (position.direction == "BUY_YES" and settlement_value >= 1.0) or (
                position.direction == "BUY_NO" and settlement_value <= 0.0
            )
            pnl = round((settlement_value - position.price) * position.size if position.direction == "BUY_YES" else ((1.0 - settlement_value) - position.price) * position.size, 2)
            events.append(
                ResolutionEvent(
                    position_id=position.order_id,
                    market_id=position.market_id,
                    outcome="won" if won else "lost",
                    status="resolved",
                    resolved_at=datetime.now(timezone.utc).isoformat(),
                    pnl=pnl,
                    settlement_value=settlement_value,
                    metadata={"exchange": exchange_name, "question": position.question},
                )
            )
        return events

    def _normalize_position(self, exchange_name: str, position: Position, index: int):
        market_id = getattr(position, "market_id", "") or ""
        size = self.host._coerce_float(getattr(position, "size", 0.0), default=0.0)
        if not market_id or size <= 0:
            return None
        side = str(getattr(position, "side", "YES") or "YES").upper()
        direction = "BUY_NO" if side == "NO" else "BUY_YES"
        opened_at = getattr(position, "opened_at", None)
        created_at = opened_at.astimezone(timezone.utc).isoformat() if isinstance(opened_at, datetime) else datetime.now(timezone.utc).isoformat()
        entry_price = self.host._coerce_float(getattr(position, "entry_price", 0.0), default=0.0)
        from bot.runner import LivePosition
        return LivePosition(
            market_id=market_id,
            question=getattr(position, "question", "") or market_id,
            direction=direction,
            price=entry_price,
            size=size,
            order_id=f"reconciled:{exchange_name}:{market_id}:{index}",
            created_at=created_at,
        )

    def _normalize_resting_order(self, exchange_name: str, order: RestingOrder | dict):
        if isinstance(order, dict):
            order_id = order.get("order_id") or order.get("id") or ""
            market_id = order.get("market_id") or order.get("ticker") or ""
            side = str(order.get("side") or "YES").upper()
            requested_size = self.host._coerce_float(order.get("requested_size", order.get("count", 0.0)), 0.0)
            filled_size = self.host._coerce_float(order.get("filled_size", 0.0), 0.0)
            remaining_size = self.host._coerce_float(order.get("remaining_size", max(0.0, requested_size - filled_size)), max(0.0, requested_size - filled_size))
            price = self.host._coerce_float(order.get("price", 0.0), 0.0)
            status = str(order.get("status") or "open")
            question = order.get("question") or market_id
            created_at = order.get("created_at")
        else:
            order_id = order.order_id
            market_id = order.market_id
            side = str(order.side or "YES").upper()
            requested_size = self.host._coerce_float(order.requested_size, 0.0)
            filled_size = self.host._coerce_float(order.filled_size, 0.0)
            remaining_size = self.host._coerce_float(order.remaining_size, max(0.0, requested_size - filled_size))
            price = self.host._coerce_float(order.price, 0.0)
            status = str(order.status or "open")
            question = order.question or market_id
            created_at = order.created_at
        if not order_id or not market_id:
            return None
        if isinstance(created_at, datetime):
            created_at_text = created_at.astimezone(timezone.utc).isoformat()
        else:
            created_at_text = str(created_at or datetime.now(timezone.utc).isoformat())
        return {
            "order_id": str(order_id),
            "exchange": exchange_name,
            "market_id": str(market_id),
            "question": str(question),
            "direction": "BUY_NO" if side == "NO" else "BUY_YES",
            "status": status,
            "requested_size": requested_size,
            "filled_size": filled_size,
            "remaining_size": remaining_size,
            "price": price,
            "created_at": created_at_text,
        }
