"""Live-mode state and reconciliation adapters for the runner."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from bot.exchanges.base import BaseExchange, Position, RestingOrder
from bot.shared_core import AccountState, OrderState, PositionState, ResolutionEvent
from bot.trade_audit import apply_execution_audit_contract, canonical_execution_status, normalize_outcome, trade_event_key

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
    verdict: str = "safe"
    issues: list[str] | None = None


class RunnerLiveStateAdapter:
    """Live state boundary backed by exchange truth and runner memory."""

    def __init__(self, host: LiveRunnerHost):
        self.host = host

    def get_account_state(self) -> AccountState:
        balance = self.host.risk.state.current_balance
        filled_reserved = sum(position.size for position in self.host.open_positions)
        pending_reserved = sum(order.get("remaining_size", 0.0) for order in self.host.open_orders)
        reserved = filled_reserved + pending_reserved
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
                "filled_event_exposure": round(filled_reserved, 2),
                "pending_event_exposure": round(pending_reserved, 2),
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
                metadata={"source": "live_reconciled", "event_key": getattr(position, "event_key", "")},
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
                metadata={"question": order.get("question", ""), "event_key": order.get("event_key", "")},
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
            apply_execution_audit_contract(
                {
                    "timestamp": position.created_at,
                    "trade_id": position.order_id,
                    "market_id": position.market_id,
                    "question": position.question,
                    "direction": position.direction,
                    "size": position.size,
                    "price": position.price,
                    "resolved": False,
                    "order_id": position.order_id,
                    "status": "filled",
                    "lifecycle_state": "filled_open",
                    "decision_reason": "reconciled_from_exchange",
                    "decision_reason_code": "reconciled_from_exchange",
                    "requested_size": position.size,
                    "approved_size": position.size,
                    "placed_size": position.size,
                    "filled_size": position.size,
                    "remaining_size": 0.0,
                    "reserved_capital": position.size,
                    "market_price": position.price,
                    "entry_price": position.price,
                    "fill_price": position.price,
                    "exchange": exchange_name,
                    "event_key": getattr(position, "event_key", "") or trade_event_key({"market_id": position.market_id, "question": position.question}),
                    "decision_trace": {},
                    "reconciled": True,
                }
            )
            for position in reconciled_positions
        ]
        reconciled_orders = [o for o in (self._normalize_resting_order(exchange_name, order) for order in resting_orders) if o]
        active_open_orders = [
            order for order in reconciled_orders
            if (self.host._coerce_float(order.get("remaining_size", 0.0), 0.0) or 0.0) > 0
        ]
        trade_history_rows.extend(
            apply_execution_audit_contract(
                {
                    "timestamp": order.get("created_at"),
                    "trade_id": order.get("order_id"),
                    "market_id": order.get("market_id"),
                    "question": order.get("question", ""),
                    "direction": order.get("direction"),
                    "order_id": order.get("order_id"),
                    "status": canonical_execution_status(
                        order.get("status"),
                        filled_size=order.get("filled_size"),
                        placed_size=(order.get("filled_size", 0.0) or 0.0) + (order.get("remaining_size", 0.0) or 0.0),
                        remaining_size=order.get("remaining_size"),
                    ),
                    "decision_reason": "reconciled_resting_order",
                    "decision_reason_code": "reconciled_resting_order",
                    "requested_size": order.get("requested_size", 0.0),
                    "approved_size": order.get("requested_size", 0.0),
                    "placed_size": (order.get("filled_size", 0.0) or 0.0) + (order.get("remaining_size", 0.0) or 0.0),
                    "filled_size": order.get("filled_size", 0.0),
                    "remaining_size": order.get("remaining_size", 0.0),
                    "reserved_capital": order.get("remaining_size", 0.0),
                    "market_price": order.get("price"),
                    "entry_price": order.get("price"),
                    "fill_price": order.get("price") if (order.get("filled_size", 0.0) or 0.0) > 0 else None,
                    "exchange": order.get("exchange", exchange_name),
                    "event_key": order.get("event_key") or trade_event_key({"market_id": order.get("market_id", ""), "question": order.get("question", "")}),
                    "decision_trace": {},
                    "reconciled": True,
                }
            )
            for order in reconciled_orders
        )
        balance = self.host._coerce_float(getattr(exchange, "get_balance", lambda: 0.0)(), default=self.host.risk.state.current_balance)
        reserved_positions = sum(position.size for position in reconciled_positions)
        reserved_orders = sum(order.get("remaining_size", 0.0) for order in active_open_orders)
        reserved_total = reserved_positions + reserved_orders
        raw_available_cash = balance - reserved_total
        available_cash = max(0.0, raw_available_cash)
        partial_fills = sum(1 for order in active_open_orders if order.get("filled_size", 0.0) > 0 and order.get("remaining_size", 0.0) > 0)
        verdict, issues = self._classify_reconciliation_verdict(
            reconciled_positions=reconciled_positions,
            active_open_orders=active_open_orders,
            balance=balance,
            raw_available_cash=raw_available_cash,
            partial_fills=partial_fills,
        )
        return LiveReconciliationSnapshot(
            exchange=exchange_name,
            open_positions=reconciled_positions,
            open_orders=active_open_orders,
            trade_history_rows=trade_history_rows,
            reserved_capital=round(reserved_total, 2),
            available_cash=round(available_cash, 2),
            partial_fills=partial_fills,
            verdict=verdict,
            issues=issues,
        )

    def _classify_reconciliation_verdict(
        self,
        *,
        reconciled_positions: list[Any],
        active_open_orders: list[dict],
        balance: float,
        raw_available_cash: float,
        partial_fills: int,
    ) -> tuple[str, list[str]]:
        issues: list[str] = []

        order_ids = [str(order.get("order_id") or "") for order in active_open_orders if str(order.get("order_id") or "")]
        if len(order_ids) != len(set(order_ids)):
            issues.append("duplicate_active_order_ids")

        if raw_available_cash < -0.01:
            issues.append("negative_available_cash_after_reconcile")

        local_order_ids = {str(order.get("order_id") or "") for order in getattr(self.host, "open_orders", []) if str(order.get("order_id") or "")}
        exchange_order_ids = set(order_ids)
        if local_order_ids and (local_order_ids - exchange_order_ids):
            issues.append("local_open_orders_missing_from_exchange")

        local_position_ids = {str(getattr(position, "order_id", "") or "") for position in getattr(self.host, "open_positions", []) if str(getattr(position, "order_id", "") or "")}
        exchange_position_ids = {str(getattr(position, "order_id", "") or "") for position in reconciled_positions if str(getattr(position, "order_id", "") or "")}
        if local_position_ids and (local_position_ids - exchange_position_ids):
            issues.append("local_positions_missing_from_exchange")

        local_entries = []
        for position in getattr(self.host, "open_positions", []):
            local_entries.append({
                "order_id": str(getattr(position, "order_id", "") or ""),
                "market_id": str(getattr(position, "market_id", "") or ""),
                "direction": str(getattr(position, "direction", "BUY_YES") or "BUY_YES").upper(),
            })
        for order in getattr(self.host, "open_orders", []):
            local_entries.append({
                "order_id": str(order.get("order_id") or ""),
                "market_id": str(order.get("market_id") or ""),
                "direction": str(order.get("direction", "BUY_YES") or "BUY_YES").upper(),
            })

        exchange_entries = []
        for position in reconciled_positions:
            exchange_entries.append({
                "order_id": str(getattr(position, "order_id", "") or ""),
                "market_id": str(getattr(position, "market_id", "") or ""),
                "direction": str(getattr(position, "direction", "BUY_YES") or "BUY_YES").upper(),
            })
        for order in active_open_orders:
            exchange_entries.append({
                "order_id": str(order.get("order_id") or ""),
                "market_id": str(order.get("market_id") or ""),
                "direction": str(order.get("direction", "BUY_YES") or "BUY_YES").upper(),
            })

        ambiguous_overlap = any(
            local_entry["market_id"]
            and local_entry["market_id"] == exchange_entry["market_id"]
            and local_entry["direction"] == exchange_entry["direction"]
            and local_entry["order_id"]
            and exchange_entry["order_id"]
            and local_entry["order_id"] != exchange_entry["order_id"]
            for local_entry in local_entries
            for exchange_entry in exchange_entries
        )
        if ambiguous_overlap:
            issues.append("ambiguous_local_exchange_duplicate_exposure")

        if partial_fills > 0:
            issues.append("partial_fill_exposure_present")
        if active_open_orders:
            issues.append("resting_orders_present")

        if any(issue in {"duplicate_active_order_ids", "negative_available_cash_after_reconcile", "ambiguous_local_exchange_duplicate_exposure"} for issue in issues):
            return "blocked", issues
        if issues:
            return "degraded", issues
        return "safe", issues

    def settle(self, exchange_name: str, exchange: BaseExchange, open_positions: list[Any]) -> list[ResolutionEvent]:
        events: list[ResolutionEvent] = []
        for position in open_positions:
            market = getattr(exchange, "get_market", lambda market_id: None)(position.market_id)
            if market is None:
                continue
            raw_outcome = market.metadata.get("result") or market.metadata.get("outcome")
            if market.close_price is None and not raw_outcome:
                continue
            settlement_value = market.close_price
            market_outcome = normalize_outcome(raw_outcome)
            if settlement_value is None:
                if market_outcome == "YES":
                    settlement_value = 1.0
                elif market_outcome == "NO":
                    settlement_value = 0.0
                else:
                    continue
            if market_outcome is None:
                if settlement_value >= 1.0:
                    market_outcome = "YES"
                elif settlement_value <= 0.0:
                    market_outcome = "NO"
                else:
                    continue
            won = (position.direction == "BUY_YES" and market_outcome == "YES") or (
                position.direction == "BUY_NO" and market_outcome == "NO"
            )
            exit_price = settlement_value
            pnl = round((settlement_value - position.price) * position.size if position.direction == "BUY_YES" else ((1.0 - settlement_value) - position.price) * position.size, 2)
            settled_cash_value = round(position.size + pnl, 4)
            events.append(
                ResolutionEvent(
                    position_id=position.order_id,
                    market_id=position.market_id,
                    outcome=market_outcome,
                    status="resolved",
                    resolved_at=datetime.now(timezone.utc).isoformat(),
                    pnl=pnl,
                    settlement_value=settled_cash_value,
                    metadata={
                        "exchange": exchange_name,
                        "question": position.question,
                        "resolution_result": "won" if won else "lost",
                        "resolution_type": "settled",
                        "exit_price": exit_price,
                    },
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
            event_key=trade_event_key({"market_id": market_id, "question": getattr(position, "question", "") or market_id}),
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
            "event_key": trade_event_key({"market_id": str(market_id), "question": str(question)}),
        }
