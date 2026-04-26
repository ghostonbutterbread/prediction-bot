"""Live-mode execution boundary for the runner."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Protocol

from bot.exchanges.base import BaseExchange
from bot.shared_core import TradeContext, TradeDecision, build_execution_snapshot
from bot.trade_audit import (
    apply_execution_audit_contract,
    build_signal_snapshot,
    canonical_execution_status,
    infer_reserved_capital,
    trade_event_key,
)

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

    def _log_trade(self, signal: dict, order, decision, size: float, price: float, audit_row: dict | None = None):
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
        direction = str(signal.get("direction", "BUY_YES") or "BUY_YES").upper()
        execution_snapshot = build_execution_snapshot(signal, direction=direction)
        return TradeContext(
            exchange=signal.get("exchange", "unknown"),
            market_id=candidate_market_id,
            question=signal.get("question", ""),
            direction=direction,
            market_price=execution_snapshot.get("market_price"),
            yes_price=execution_snapshot.get("yes_price"),
            no_price=execution_snapshot.get("no_price"),
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
                    "best_yes_ask": execution_snapshot.get("best_yes_ask"),
                    "best_no_ask": execution_snapshot.get("best_no_ask"),
                    "best_yes_bid": execution_snapshot.get("best_yes_bid"),
                    "best_no_bid": execution_snapshot.get("best_no_bid"),
                    "estimated_fill_price": execution_snapshot.get("estimated_fill_price"),
                    "execution_snapshot_source": execution_snapshot.get("source"),
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

        initial_decision = decision
        initial_signal_snapshot = build_signal_snapshot(signal, direction=getattr(decision, "action", signal.get("direction", "BUY_YES")))
        market_id = signal["market_id"]
        side = "YES" if decision.action == "BUY_YES" else "NO"
        exchange_name = signal.get("exchange", "unknown")
        pre_trade_refresh = self.host.live_sync.refresh_before_execution(exchange_name, exchange)
        refresh_verdict = str(pre_trade_refresh.get("reconciliation_verdict") or "safe").lower()
        refresh_issues = list(pre_trade_refresh.get("reconciliation_issues") or [])
        strict_degraded = bool((((self.host.config.get("trading") or {}).get("live_reconciliation") or {}).get("block_on_degraded", False)))
        if refresh_verdict == "blocked" or (refresh_verdict == "degraded" and strict_degraded):
            reason_code = "reconciliation_state_blocked" if refresh_verdict == "blocked" else "reconciliation_state_degraded"
            reason = "Live reconciliation blocked order placement" if refresh_verdict == "blocked" else "Live reconciliation degraded state blocked by policy"
            gated_decision = SimpleNamespace(
                action=getattr(decision, "action", signal.get("direction", "BUY_YES")),
                approved=False,
                position_size=0.0,
                requested_position_size=float(getattr(decision, "requested_position_size", 0.0) or getattr(decision, "position_size", 0.0) or 0.0),
                entry_price=getattr(decision, "entry_price", signal.get("market_price")),
                win_probability=getattr(decision, "win_probability", signal.get("model_probability")),
                reason=reason,
                reason_code=reason_code,
                reasoning={"reconciliation_gate": {"verdict": refresh_verdict, "issues": refresh_issues, "pre_trade_refresh": dict(pre_trade_refresh)}},
            )
            self._append_rejected_trade_row(
                signal=signal,
                exchange=exchange,
                decision=gated_decision,
                initial_decision=initial_decision,
                initial_signal_snapshot=initial_signal_snapshot,
                execution_snapshot=None,
                status="rejected",
                message=reason,
                failure_stage="reconciliation",
                execution_revalidated=False,
                execution_revalidation_outcome=None,
            )
            return {
                "blocked_reason": reason_code,
                "decision": gated_decision,
                "reconciliation_issues": refresh_issues,
                "refresh": {"pre_trade_refresh": dict(pre_trade_refresh)},
            }

        bid_ask = None
        try:
            market_bid_ask = exchange.get_market_bid_ask(market_id)
            if market_bid_ask and market_bid_ask.get("best_yes_ask", 0) > 0:
                bid_ask = market_bid_ask
            else:
                logger.warning(f"No market price data for {market_id} - skipping")
                return None
        except Exception as e:
            logger.debug(f"Could not fetch market bid/ask for {market_id}: {e}")

        execution_snapshot = build_execution_snapshot(
            signal,
            direction=decision.action,
            bid_ask=bid_ask,
            fallback_to_signal_prices=True,
        )
        if execution_snapshot.get("market_price") is None:
            logger.warning(f"No valid market price for {market_id} - skipping")
            return None

        live_signal = dict(signal)
        live_signal.update({
            "yes_price": execution_snapshot.get("yes_price"),
            "no_price": execution_snapshot.get("no_price"),
            "best_yes_ask": execution_snapshot.get("best_yes_ask"),
            "best_no_ask": execution_snapshot.get("best_no_ask"),
            "best_yes_bid": execution_snapshot.get("best_yes_bid"),
            "best_no_bid": execution_snapshot.get("best_no_bid"),
            "market_price": execution_snapshot.get("market_price"),
        })

        strategy_cfg = self.host.config.get("strategy", {}) or {}
        live_context = self.build_trade_context(live_signal, exchange, self.host.config)
        live_context.metadata["pre_trade_refresh"] = dict(pre_trade_refresh)
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
            self._append_rejected_trade_row(
                signal=signal,
                exchange=exchange,
                decision=live_decision,
                initial_decision=initial_decision,
                initial_signal_snapshot=initial_signal_snapshot,
                execution_snapshot=execution_snapshot,
                status="rejected",
                message=live_decision.reason,
                failure_stage="revalidation",
                execution_revalidated=True,
                execution_revalidation_outcome="rejected",
            )
            return None

        price = max(0.01, min(float(live_decision.entry_price or 0.0), 0.99))
        size = float(live_decision.position_size or 0.0)

        if size < 1:
            logger.info(f"Position too small after shared risk controls: ${size:.2f}")
            self._append_rejected_trade_row(
                signal=signal,
                exchange=exchange,
                decision=live_decision,
                initial_decision=initial_decision,
                initial_signal_snapshot=initial_signal_snapshot,
                execution_snapshot=execution_snapshot,
                status="rejected",
                message="Position too small after shared risk controls",
                failure_stage="sizing",
                execution_revalidated=True,
                execution_revalidation_outcome="approved",
            )
            return None

        decision = live_decision
        order = exchange.place_order(market_id, side, price, size)
        if not order:
            self._append_rejected_trade_row(
                signal=signal,
                exchange=exchange,
                decision=decision,
                initial_decision=initial_decision,
                initial_signal_snapshot=initial_signal_snapshot,
                execution_snapshot=execution_snapshot,
                status="failed",
                message="Exchange did not return an order",
                failure_stage="placement",
                execution_revalidated=True,
                execution_revalidation_outcome="approved",
            )
            return None

        from bot.runner import LivePosition

        order_id = order.id if hasattr(order, "id") else str(order)
        order_status = canonical_execution_status(
            getattr(order, "status", None),
            filled_size=getattr(order, "filled_size", None),
            placed_size=size,
            remaining_size=getattr(order, "remaining_size", None),
        )
        filled_size = self.host._coerce_float(getattr(order, "filled_size", None), default=None)
        remaining_size = self.host._coerce_float(getattr(order, "remaining_size", None), default=None)
        if filled_size is None and remaining_size is None:
            filled_size = 0.0
            remaining_size = size
        elif filled_size is None:
            filled_size = max(0.0, size - float(remaining_size or 0.0))
        elif remaining_size is None:
            remaining_size = max(0.0, size - float(filled_size or 0.0))
        filled_size = max(0.0, min(float(filled_size or 0.0), size))
        remaining_size = max(0.0, min(float(remaining_size or 0.0), size))
        if filled_size + remaining_size > size:
            overflow = filled_size + remaining_size - size
            remaining_size = max(0.0, remaining_size - overflow)
        if order_status == "filled":
            filled_size = size
            remaining_size = 0.0
        elif order_status == "partial":
            if filled_size <= 0.0:
                filled_size = max(0.0, size - remaining_size)
            if remaining_size <= 0.0:
                remaining_size = max(0.0, size - filled_size)
        elif order_status == "placed":
            filled_size = 0.0
            remaining_size = size

        timestamp = datetime.now(timezone.utc).isoformat()
        if filled_size > 0:
            self.host.open_positions.append(
                LivePosition(
                    market_id=market_id,
                    question=signal.get("question", ""),
                    direction=decision.action,
                    price=price,
                    size=filled_size,
                    order_id=order_id,
                    created_at=timestamp,
                    event_key=trade_event_key(signal),
                )
            )
        if remaining_size > 0:
            self.host.open_orders.append(
                {
                    "order_id": order_id,
                    "exchange": exchange_name,
                    "market_id": market_id,
                    "question": signal.get("question", ""),
                    "direction": decision.action,
                    "status": getattr(order, "status", None) or ("partial" if filled_size > 0 else "open"),
                    "requested_size": float(decision.requested_position_size or size),
                    "filled_size": filled_size,
                    "remaining_size": remaining_size,
                    "price": price,
                    "created_at": timestamp,
                    "event_key": trade_event_key(signal),
                }
            )
        decision_trace = dict(getattr(decision, "reasoning", {}) or {})
        parity_mode = dict((decision_trace or {}).get("parity_mode", {}) or {})
        refresh = self.host.live_sync.refresh_after_execution(exchange)
        refresh["pre_trade_refresh"] = dict(pre_trade_refresh)
        available_cash_after_entry = refresh.get("available_cash", 0.0)
        executed_row = {
            "timestamp": timestamp,
            "trade_id": order_id,
            "market_id": market_id,
            "question": signal.get("question", ""),
            "direction": decision.action,
            "size": size,
            "price": price,
            "resolved": False,
            "order_id": order_id,
            "status": order_status,
            "failure_stage": None,
            "decision_reason": decision.reason,
            "decision_reason_code": decision.reason_code,
            "requested_size": decision.requested_position_size,
            "approved_size": decision.position_size,
            "placed_size": size,
            "filled_size": filled_size,
            "remaining_size": remaining_size,
            "market_price": execution_snapshot.get("market_price"),
            "entry_price": price,
            "fill_price": price if filled_size > 0 else None,
            "exchange": exchange_name,
            "model_probability": round(float(decision.win_probability or 0.0), 4),
            "edge": signal.get("edge", 0),
            "confidence": signal.get("confidence", 0),
            "signals": signal.get("signals", {}),
            "decision_trace": decision_trace,
            "parity_mode_enabled": bool(parity_mode.get("enabled", False)),
            "execution_revalidated": True,
            "execution_revalidation_outcome": "approved",
            "original_signal_snapshot": parity_mode.get("original_signal_snapshot") or initial_signal_snapshot,
            "execution_snapshot": parity_mode.get("execution_snapshot") or execution_snapshot,
            "original_decision_reason_code": parity_mode.get("original_decision_reason_code") or getattr(initial_decision, "reason_code", None),
            "execution_decision_reason_code": parity_mode.get("execution_decision_reason_code") or getattr(decision, "reason_code", None),
            "execution_snapshot_source": parity_mode.get("execution_snapshot_source") or execution_snapshot.get("source"),
            "estimated_fill_price": ((decision_trace or {}).get("retrade", {}) or {}).get("estimated_fill_price") or execution_snapshot.get("estimated_fill_price"),
            "slippage_estimate": ((decision_trace or {}).get("retrade", {}) or {}).get("slippage_estimate"),
            "reserved_capital": infer_reserved_capital(order_status, filled_size=filled_size, remaining_size=remaining_size),
            "available_cash_before": max(0.0, available_cash_after_entry + infer_reserved_capital(order_status, filled_size=filled_size, remaining_size=remaining_size)),
            "available_cash_after_entry": available_cash_after_entry,
            "event_key": trade_event_key(signal),
        }
        canonical_row = apply_execution_audit_contract(executed_row)
        self.host.trade_history.append(canonical_row)

        logger.info(
            f"✅ Trade executed: {side} ${size:.2f} @ ${price:.4f} on {exchange_name}/{market_id}"
        )
        self.host._log_trade(signal, order, decision, size, price, audit_row=canonical_row)
        balance_after = self.host._coerce_float(getattr(exchange, "get_balance", lambda: 0.0)(), default=0.0)
        reserved_capital = sum(position.size for position in self.host.open_positions) + sum(float(order.get("remaining_size", 0.0) or 0.0) for order in self.host.open_orders)
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
                "pre_trade_refresh": pre_trade_refresh,
            },
        )
        return {"order": order, "signal": signal, "decision": decision, "refresh": refresh}

    def _append_rejected_trade_row(
        self,
        *,
        signal: dict,
        exchange: BaseExchange,
        decision: TradeDecision,
        initial_decision: TradeDecision | None,
        initial_signal_snapshot: dict[str, Any] | None,
        execution_snapshot: dict[str, Any] | None,
        status: str,
        message: str,
        failure_stage: str,
        execution_revalidated: bool,
        execution_revalidation_outcome: str | None,
    ) -> None:
        decision_trace = dict(getattr(decision, "reasoning", {}) or {})
        parity_mode = dict((decision_trace or {}).get("parity_mode", {}) or {})
        balance = self.host._coerce_float(getattr(exchange, "get_balance", lambda: 0.0)(), default=0.0)
        reserved_capital = sum(position.size for position in self.host.open_positions)
        pending_capital = sum(float(order.get("remaining_size", 0.0) or 0.0) for order in self.host.open_orders)
        available_cash = max(0.0, balance - reserved_capital - pending_capital)
        timestamp = datetime.now(timezone.utc).isoformat()
        requested_size = float(getattr(decision, "requested_position_size", 0.0) or 0.0)
        fallback_trade_id = f"live-{failure_stage}:{signal.get('market_id', 'unknown')}:{timestamp}"
        rejected_row = {
            "timestamp": timestamp,
            "trade_id": fallback_trade_id,
            "market_id": signal.get("market_id", ""),
            "question": signal.get("question", ""),
            "direction": getattr(decision, "action", signal.get("direction", "BUY_YES")),
            "size": 0.0,
            "price": execution_snapshot.get("market_price") if execution_snapshot else signal.get("market_price"),
            "resolved": False,
            "order_id": None,
            "status": status,
            "lifecycle_state": f"{failure_stage}_rejected",
            "failure_stage": failure_stage,
            "decision_reason": getattr(decision, "reason", message),
            "decision_reason_code": getattr(decision, "reason_code", "unknown"),
            "requested_size": requested_size,
            "approved_size": float(getattr(decision, "position_size", 0.0) or 0.0),
            "placed_size": 0.0,
            "filled_size": 0.0,
            "remaining_size": 0.0,
            "market_price": execution_snapshot.get("market_price") if execution_snapshot else signal.get("market_price"),
            "entry_price": execution_snapshot.get("market_price") if execution_snapshot else signal.get("market_price"),
            "fill_price": None,
            "exchange": signal.get("exchange", "unknown"),
            "model_probability": round(float(getattr(decision, "win_probability", 0.0) or 0.0), 4),
            "edge": signal.get("edge", 0),
            "confidence": signal.get("confidence", 0),
            "signals": signal.get("signals", {}),
            "decision_trace": decision_trace,
            "parity_mode_enabled": bool(parity_mode.get("enabled", False)),
            "execution_revalidated": execution_revalidated,
            "execution_revalidation_outcome": execution_revalidation_outcome,
            "original_signal_snapshot": parity_mode.get("original_signal_snapshot") or initial_signal_snapshot,
            "execution_snapshot": parity_mode.get("execution_snapshot") or execution_snapshot,
            "original_decision_reason_code": parity_mode.get("original_decision_reason_code") or getattr(initial_decision, "reason_code", None),
            "execution_decision_reason_code": parity_mode.get("execution_decision_reason_code") or (getattr(decision, "reason_code", None) if execution_revalidated else None),
            "execution_snapshot_source": parity_mode.get("execution_snapshot_source") or ((execution_snapshot or {}).get("source") if execution_snapshot else None),
            "estimated_fill_price": ((decision_trace or {}).get("retrade", {}) or {}).get("estimated_fill_price") or ((execution_snapshot or {}).get("estimated_fill_price") if execution_snapshot else None),
            "slippage_estimate": ((decision_trace or {}).get("retrade", {}) or {}).get("slippage_estimate"),
            "reserved_capital": 0.0,
            "available_cash_before": available_cash,
            "available_cash_after_entry": available_cash,
            "event_key": trade_event_key(signal),
            "message": message,
        }
        canonical_row = apply_execution_audit_contract(rejected_row)
        self.host.trade_history.append(canonical_row)
        self.host._log_trade(signal, None, decision, 0.0, canonical_row.get("entry_price") or canonical_row.get("market_price") or 0.0, audit_row=canonical_row)
