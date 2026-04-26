"""Live-mode API-truth sync helpers for runner state."""

from __future__ import annotations

from typing import Any, Protocol

from bot.exchanges.base import BaseExchange


class LiveSyncHost(Protocol):
    open_positions: list[Any]
    open_orders: list[dict]
    risk: Any
    live_reconciliation: Any

    def _coerce_float(self, value, default: float = 0.0) -> float:
        ...


class RunnerLiveSync:
    """Refreshes local live state from exchange truth before/after actions."""

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

    def refresh_before_execution(self, exchange_name: str, exchange: BaseExchange) -> dict[str, float | int | bool]:
        try:
            snapshot = self.host.live_reconciliation.reconcile(exchange_name, exchange)
            self.host.open_positions = snapshot.open_positions
            self.host.open_orders = snapshot.open_orders
            self.host.risk.sync_account_state(
                current_balance=snapshot.reserved_capital + snapshot.available_cash,
                available_cash=snapshot.available_cash,
                reserved_capital=snapshot.reserved_capital,
                total_exposure=snapshot.reserved_capital,
                open_positions=len(snapshot.open_positions),
            )
            return {
                "balance": round(snapshot.reserved_capital + snapshot.available_cash, 2),
                "available_cash": round(snapshot.available_cash, 2),
                "reserved_capital": round(snapshot.reserved_capital, 2),
                "open_positions": len(snapshot.open_positions),
                "open_orders": len(snapshot.open_orders),
                "partial_fills": int(getattr(snapshot, "partial_fills", 0) or 0),
                "reconciliation_verdict": getattr(snapshot, "verdict", "safe") or "safe",
                "reconciliation_issues": list(getattr(snapshot, "issues", []) or []),
                "pre_trade_refresh": True,
            }
        except Exception:
            fallback = self.refresh_account_state_from_exchange(exchange)
            fallback.update({
                "open_positions": len(self.host.open_positions),
                "open_orders": len(self.host.open_orders),
                "partial_fills": sum(1 for order in self.host.open_orders if (order.get("filled_size", 0.0) or 0.0) > 0 and (order.get("remaining_size", 0.0) or 0.0) > 0),
                "reconciliation_verdict": "degraded",
                "reconciliation_issues": ["reconciliation_refresh_failed"],
                "pre_trade_refresh": False,
            })
            return fallback

    def refresh_after_execution(self, exchange: BaseExchange) -> dict[str, float]:
        return self.refresh_account_state_from_exchange(exchange)
