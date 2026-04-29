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

    def _apply_reconciliation_runtime_state(self, exchange_name: str, verdict: str | None, issues: list[str] | None, *, source: str):
        ...

    def _record_reconciliation_snapshot(self, exchange_name: str, snapshot: Any, *, source: str):
        ...

    def _apply_reconciliation_trade_history_corrections(self, exchange_name: str, snapshot: Any, *, source: str) -> set[str]:
        ...

    def _enforce_live_runtime_invariants(self, exchange_name: str | None, *, source: str) -> list[str]:
        ...


class RunnerLiveSync:
    """Refreshes local live state from exchange truth before/after actions."""

    def __init__(self, host: LiveSyncHost):
        self.host = host

    def refresh_account_state_from_exchange(self, exchange: BaseExchange, exchange_name: str | None = None) -> dict[str, float]:
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
        invariant_checker = getattr(self.host, "_enforce_live_runtime_invariants", None)
        if callable(invariant_checker):
            invariant_checker(exchange_name or getattr(exchange, "name", None) or "", source="account_refresh")
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
                current_balance=getattr(snapshot, "balance", snapshot.reserved_capital + snapshot.available_cash),
                available_cash=snapshot.available_cash,
                reserved_capital=snapshot.reserved_capital,
                total_exposure=snapshot.reserved_capital,
                open_positions=len(snapshot.open_positions),
            )
            verdict = getattr(snapshot, "verdict", "safe") or "safe"
            issues = list(getattr(snapshot, "issues", []) or [])
            severity = getattr(snapshot, "severity", "none") or "none"
            action = getattr(snapshot, "action", "log_only") or "log_only"
            recorder = getattr(self.host, "_record_reconciliation_snapshot", None)
            if callable(recorder):
                recorder(exchange_name, snapshot, source="pre_trade_reconciliation")
            correction_applier = getattr(self.host, "_apply_reconciliation_trade_history_corrections", None)
            if callable(correction_applier):
                correction_applier(exchange_name, snapshot, source="pre_trade_reconciliation")
            updater = getattr(self.host, "_apply_reconciliation_runtime_state", None)
            if callable(updater):
                updater(exchange_name, verdict, issues, source="pre_trade_reconciliation")
                gate = getattr(self.host, "reconciliation_gate", {}) or {}
                exchange_gate = gate.get(exchange_name) if isinstance(gate, dict) else None
                if isinstance(exchange_gate, dict) and str(exchange_gate.get("verdict") or "").lower() == "blocked":
                    verdict = "blocked"
                    severity = "high"
                    action = "block"
                    issues = list(dict.fromkeys(list(exchange_gate.get("issues") or issues)))
            invariant_checker = getattr(self.host, "_enforce_live_runtime_invariants", None)
            invariant_issues = []
            if callable(invariant_checker):
                invariant_issues = invariant_checker(exchange_name, source="pre_trade_reconciliation")
            if invariant_issues:
                verdict = "blocked"
                severity = "high"
                action = "block"
                issues = list(dict.fromkeys(issues + invariant_issues))
            return {
                "balance": round(getattr(snapshot, "balance", snapshot.reserved_capital + snapshot.available_cash), 2),
                "available_cash": round(snapshot.available_cash, 2),
                "reserved_capital": round(snapshot.reserved_capital, 2),
                "filled_exposure": round(getattr(snapshot, "filled_exposure", 0.0), 2),
                "pending_exposure": round(getattr(snapshot, "pending_exposure", 0.0), 2),
                "open_positions": len(snapshot.open_positions),
                "open_orders": len(snapshot.open_orders),
                "partial_fills": int(getattr(snapshot, "partial_fills", 0) or 0),
                "reconciliation_verdict": verdict,
                "reconciliation_issues": issues,
                "reconciliation_severity": severity,
                "reconciliation_action": action,
                "reconciliation_corrections": list(getattr(snapshot, "correction_events", []) or []),
                "pre_trade_refresh": True,
            }
        except Exception:
            fallback = self.refresh_account_state_from_exchange(exchange, exchange_name=exchange_name)
            issues = ["reconciliation_refresh_failed"]
            updater = getattr(self.host, "_apply_reconciliation_runtime_state", None)
            if callable(updater):
                updater(exchange_name, "blocked", issues, source="pre_trade_reconciliation")
            fallback.update({
                "open_positions": len(self.host.open_positions),
                "open_orders": len(self.host.open_orders),
                "partial_fills": sum(1 for order in self.host.open_orders if (order.get("filled_size", 0.0) or 0.0) > 0 and (order.get("remaining_size", 0.0) or 0.0) > 0),
                "reconciliation_verdict": "blocked",
                "reconciliation_issues": issues,
                "reconciliation_severity": "high",
                "reconciliation_action": "block",
                "reconciliation_corrections": [],
                "pre_trade_refresh": False,
            })
            return fallback

    def refresh_after_execution(self, exchange: BaseExchange, exchange_name: str | None = None) -> dict[str, float]:
        return self.refresh_account_state_from_exchange(exchange, exchange_name=exchange_name)
