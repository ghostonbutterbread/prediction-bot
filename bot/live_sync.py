"""Live-mode API-truth sync helpers for runner state."""

from __future__ import annotations

from typing import Any, Protocol

from bot.exchanges.base import BaseExchange


class LiveSyncHost(Protocol):
    open_positions: list[Any]
    open_orders: list[dict]
    risk: Any

    def _coerce_float(self, value, default: float = 0.0) -> float:
        ...


class RunnerLiveSync:
    """Refreshes local live state from exchange truth after actions."""

    def __init__(self, host: LiveSyncHost):
        self.host = host

    def refresh_account_state_from_exchange(self, exchange: BaseExchange) -> dict[str, float]:
        balance = self.host._coerce_float(getattr(exchange, "get_balance", lambda: 0.0)(), default=self.host.risk.state.current_balance)
        reserved_positions = sum(position.size for position in self.host.open_positions)
        reserved_orders = sum(order.get("remaining_size", 0.0) for order in self.host.open_orders)
        reserved_total = round(reserved_positions + reserved_orders, 2)
        available_cash = round(max(0.0, balance - reserved_total), 2)
        self.host.risk.sync_account_state(
            current_balance=balance,
            available_cash=available_cash,
            reserved_capital=reserved_total,
            total_exposure=reserved_total,
            open_positions=len(self.host.open_positions),
        )
        return {
            "balance": round(balance, 2),
            "available_cash": available_cash,
            "reserved_capital": reserved_total,
        }

    def refresh_after_execution(self, exchange: BaseExchange) -> dict[str, float]:
        return self.refresh_account_state_from_exchange(exchange)
