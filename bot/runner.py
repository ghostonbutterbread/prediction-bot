"""Main bot runner, multi-exchange prediction market bot."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from bot.config import ensure_mode_storage_dir, load_config
from bot.exchanges.base import BaseExchange
from bot.live_adapters import RunnerLiveReconciliationAdapter, RunnerLiveStateAdapter
from bot.live_execution import RunnerLiveExecutionAdapter
from bot.live_sync import RunnerLiveSync
from bot.notifications import build_notification, normalize_verbosity
from bot.risk import RiskManager
from bot.telegram_notifier import TelegramNotifier
from bot.shared_core import AccountState, TradeContext, build_trade_decision
from bot.parity_audit import normalize_parity_trade_row, summarize_normalized_rows
from bot.status import build_snapshot, summarize_log_storage
from bot.trade_audit import apply_execution_audit_contract, build_risk_block_audit_row, build_scan_candidate_summary
from bot.strategies.enhanced import EnhancedStrategyEngine, KellySizer

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class LivePosition:
    market_id: str
    question: str
    direction: str
    price: float
    size: float
    order_id: str
    created_at: str
    event_key: str = ""


class PredictionBot:
    """Multi-exchange prediction market trading bot."""

    def __init__(self, config: dict = None):
        config = config or {}
        self.config = config
        self._config_path = Path(config.get("_config_path", "config.yaml")) if config.get("_config_path") else None
        self._config_last_mtime: float | None = None

        self.exchanges: dict[str, BaseExchange] = {}
        self.strategy = EnhancedStrategyEngine(config.get("strategy", {}))
        self.live_state = RunnerLiveStateAdapter(self)
        self.live_reconciliation = RunnerLiveReconciliationAdapter(self)
        self.live_sync = RunnerLiveSync(self)
        self.live_execution = RunnerLiveExecutionAdapter(self)
        economics_cfg = config.get("trade_economics", {}) or {}
        self.kelly = KellySizer(
            fee_rate=config.get("kalshi_fee_rate"),
            min_position_size_usd=economics_cfg.get("min_position_size_usd", 1.0),
            min_expected_net_profit_usd=economics_cfg.get("min_expected_net_profit_usd", 0.0),
        )
        self.risk = RiskManager(config)

        self.running = False
        self.stats = {
            "scans": 0,
            "signals": 0,
            "trades": 0,
            "errors": 0,
            "blocked": 0,
        }
        self.last_block_reasons: dict[str, int] = {}
        self.open_positions: list[LivePosition] = []
        self.open_orders: list[dict] = []
        self.trade_history: list[dict] = []
        self.lifecycle_counters = {
            "signals_considered": 0,
            "trades_executed": 0,
            "blocked_total": 0,
            "errors": 0,
        }
        self.lifecycle_block_reasons: dict[str, int] = {}
        self.lifecycle_started_at = datetime.now(timezone.utc)
        self._last_hourly_summary_key: str | None = None
        self.single_trade_mode = bool(config.get("trading", {}).get("single_trade_mode", False))
        self.single_trade_completed = False
        self.alerts = config.get("alerts", {}) or {}
        self.verbosity_level = normalize_verbosity((config.get("verbosity", {}) or {}).get("level", "normal"))
        self.telegram_notifier = TelegramNotifier(self.alerts)

        runtime_mode = str(self.config.get("trading", {}).get("mode", "paper"))
        self.log_dir = ensure_mode_storage_dir(config.get("log_dir", "data"), runtime_mode)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        if self._config_path and self._config_path.exists():
            self._config_last_mtime = self._config_path.stat().st_mtime

        self._log_lifecycle_event(
            "startup",
            {
                "mode": self.config.get("trading", {}).get("mode", "paper"),
                "trading_enabled": bool(
                    self.config.get("trading", {}).get(
                        "enabled",
                        self.config.get("trading_enabled", self.config.get("trading", {}).get("trading_enabled", True)),
                    )
                ),
                "exchange_count": len(self.exchanges),
            },
        )

    def add_kalshi(self, api_key_id: str, private_key_path: str, demo: bool = True):
        from bot.exchanges.kalshi import KalshiExchange

        exchange = KalshiExchange(api_key_id, private_key_path, demo)
        self.exchanges["kalshi"] = exchange
        return exchange

    def connect_all(self) -> dict[str, bool]:
        results = {}
        for name, exchange in self.exchanges.items():
            try:
                results[name] = exchange.connect()
                if results[name]:
                    self._reconcile_exchange_state(name, exchange)
            except Exception as e:
                logger.error(f"Failed to connect to {name}: {e}")
                results[name] = False
        return results

    def _reconcile_exchange_state(self, exchange_name: str, exchange: BaseExchange) -> dict:
        try:
            snapshot = self.live_reconciliation.reconcile(exchange_name, exchange)
        except Exception as e:
            logger.error(f"Failed to fetch exchange state for reconciliation from {exchange_name}: {e}")
            self._log_lifecycle_event(
                "reconciliation_failed",
                {"exchange": exchange_name, "error": str(e)},
            )
            return {"exchange": exchange_name, "open_positions": 0, "open_orders": 0, "trade_history_loaded": 0, "status": "error"}

        self.open_positions = snapshot.open_positions
        self.open_orders = snapshot.open_orders
        self.trade_history = [trade for trade in self.trade_history if not trade.get("reconciled")] + snapshot.trade_history_rows

        balance = self._coerce_float(getattr(exchange, "get_balance", lambda: 0.0)(), default=self.risk.state.current_balance)
        self.risk.sync_account_state(
            current_balance=balance,
            available_cash=snapshot.available_cash,
            reserved_capital=snapshot.reserved_capital,
            total_exposure=snapshot.reserved_capital,
            open_positions=len(self.open_positions),
        )

        summary = {
            "exchange": exchange_name,
            "open_positions": len(snapshot.open_positions),
            "open_orders": len(snapshot.open_orders),
            "trade_history_loaded": len(snapshot.trade_history_rows),
            "reserved_capital": snapshot.reserved_capital,
            "available_cash": snapshot.available_cash,
            "partial_fills": snapshot.partial_fills,
            "status": "ok",
        }
        self._log_lifecycle_event("reconciliation_completed", summary)
        return summary


    def scan_once(self) -> dict:
        logger.info(f"\n{'='*60}")
        logger.info(f"Scan #{self.stats['scans'] + 1} at {datetime.now(timezone.utc).isoformat()}")
        logger.info(f"{'='*60}")

        all_signals = []
        blocked_reasons: dict[str, int] = {}
        markets_scanned = 0
        scan_cfg = self.config.get("scan", {}) or {}
        markets_per_exchange = int(scan_cfg.get("markets_per_exchange", 30) or 30)
        summary_sample_per_exchange = int(scan_cfg.get("summary_sample_per_exchange", 5) or 5)

        allowed_market_groups = [str(group).strip().lower() for group in (scan_cfg.get("allowed_market_groups") or []) if str(group).strip()]

        for exchange_name, exchange in self.exchanges.items():
            try:
                setter = getattr(exchange, "set_allowed_market_groups", None)
                if callable(setter):
                    setter(allowed_market_groups)
                markets = exchange.get_markets(limit=markets_per_exchange)
                if not markets:
                    continue

                markets_scanned += len(markets)
                logger.info(f"\n{exchange_name}: {len(markets)} markets")

                for market in markets:
                    try:
                        order_book = exchange.get_order_book(market.id)
                        signal = self.strategy.analyze_market(market, order_book)
                        if signal:
                            signal["exchange"] = exchange_name
                            signal["question"] = getattr(market, "question", signal.get("question", ""))
                            signal["yes_price"] = getattr(market, "yes_price", signal.get("market_price"))
                            signal["no_price"] = getattr(market, "no_price", None)
                            signal["market_group"] = (getattr(market, "metadata", {}) or {}).get("market_group", "unknown")
                            all_signals.append(signal)
                    except Exception as e:
                        logger.debug(f"Error analyzing {market.id}: {e}")
                        continue
            except Exception as e:
                logger.error(f"Error scanning {exchange_name}: {e}")
                self.stats["errors"] += 1

        all_signals.sort(key=lambda s: s.get("edge", 0), reverse=True)

        if all_signals:
            logger.info("\n📊 Top Signals:")
            for sig in all_signals[:5]:
                logger.info(
                    f"  {sig['direction']} | "
                    f"Edge: {sig['edge']:.2%} | "
                    f"Conf: {sig['confidence']:.2%} | "
                    f"Price: ${sig['market_price']:.2f} | "
                    f"[{sig['exchange']}]"
                )
        else:
            logger.info("  No signals this cycle")

        max_candidates = 1 if self.single_trade_mode else 3
        trades = 0
        for sig in all_signals[:max_candidates]:
            result = self._process_signal(sig)
            if result is None:
                continue
            if result.get("blocked_reason"):
                blocked_reasons[result["blocked_reason"]] = blocked_reasons.get(result["blocked_reason"], 0) + 1
                self.stats["blocked"] += 1
                self._log_risk_block_event(sig, result)
            elif result.get("order"):
                trades += 1
                if self.single_trade_mode:
                    self.single_trade_completed = True

        self.stats["scans"] += 1
        self.stats["signals"] += len(all_signals)
        self.stats["trades"] += trades
        self.last_block_reasons = blocked_reasons
        self.lifecycle_counters["signals_considered"] += len(all_signals)
        self.lifecycle_counters["trades_executed"] += trades
        blocked_total = sum(blocked_reasons.values())
        self.lifecycle_counters["blocked_total"] += blocked_total
        for reason, count in blocked_reasons.items():
            self.lifecycle_block_reasons[reason] = self.lifecycle_block_reasons.get(reason, 0) + count

        self._sync_resolved_positions()
        if self.single_trade_mode and self.single_trade_completed:
            self._log_lifecycle_event(
                "single_trade_completed",
                {
                    "open_positions": len(self.open_positions),
                    "open_orders": len(self.open_orders),
                    "behavior": "no_new_entries_continue_resolution_tracking",
                },
            )
        self._log_scan(all_signals, trades, blocked_reasons)
        self._emit_hourly_summary_if_due()

        return {
            "markets_scanned": markets_scanned,
            "signals": len(all_signals),
            "trades": trades,
            "blocked_reasons": blocked_reasons,
        }

    def run_loop(self, interval_seconds: int = 120, max_scans: int = None):
        self.running = True
        logger.info(f"Bot started, scanning every {interval_seconds}s")

        count = 0
        while self.running:
            try:
                self.reload_runtime_controls_if_needed()
                self.scan_once()
                count += 1

                if max_scans and count >= max_scans:
                    break

                time.sleep(interval_seconds)

            except KeyboardInterrupt:
                break
            except Exception as e:
                self.stats["errors"] += 1
                self.lifecycle_counters["errors"] += 1
                logger.error(f"Scan error: {e}", exc_info=True)
                time.sleep(interval_seconds)

        self.running = False
        logger.info(f"Bot stopped. Stats: {self.stats}")

    def build_status_snapshot(self, *, reason: str = "status", scan_num: int | None = None):
        status = self.risk.get_status()
        total_trades = len(self.trade_history)
        resolved_trades = sum(1 for trade in self.trade_history if trade.get("resolved"))
        open_trades = max(0, len(self.open_positions))
        balance_value = self._coerce_float(status.get("balance"), default=self.risk.state.current_balance)
        pnl_text = status.get("pnl", "$0.00 (+0.0%)")
        pnl_value, pnl_pct = self._parse_pnl_fields(pnl_text)
        normalized_trade_summary = summarize_normalized_rows([
            normalize_parity_trade_row(trade, source="live") for trade in self.trade_history
        ])
        extra = {
            "source": self.config.get("trading", {}).get("mode", "paper"),
            "blocked_last_scan": sum(self.last_block_reasons.values()),
            "signals_considered": self.lifecycle_counters.get("signals_considered", 0),
            "trades_executed": self.lifecycle_counters.get("trades_executed", 0),
            "blocked_total": self.lifecycle_counters.get("blocked_total", 0),
            "runner_errors": self.lifecycle_counters.get("errors", 0),
            "filled_event_exposure": round(sum(getattr(position, "size", 0.0) for position in self.open_positions), 2),
            "pending_event_exposure": round(sum(float(order.get("remaining_size", 0.0) or 0.0) for order in self.open_orders), 2),
            "normalized_trade_summary": normalized_trade_summary,
            "parity_summary": {
                "parity_mode_enabled": bool((self.config.get("parity_mode", {}) or {}).get("enabled", False)),
                "parity_candidates": normalized_trade_summary.get("parity_candidates", 0),
                "parity_enabled_rows": normalized_trade_summary.get("parity_enabled_rows", 0),
                "execution_revalidated_rows": normalized_trade_summary.get("execution_revalidated_rows", 0),
                "execution_rejected_rows": normalized_trade_summary.get("execution_rejected_rows", 0),
                "fallback_rows": normalized_trade_summary.get("fallback_rows", 0),
                "missing_snapshot_rows": normalized_trade_summary.get("missing_snapshot_rows", 0),
                "snapshot_source_counts": normalized_trade_summary.get("snapshot_source_counts", {}),
                "lifecycle_state_counts": normalized_trade_summary.get("lifecycle_state_counts", {}),
                "invalid_contract_rows": normalized_trade_summary.get("invalid_contract_rows", 0),
                "top_contract_issues": normalized_trade_summary.get("top_contract_issues", []),
                "top_execution_reason_codes": normalized_trade_summary.get("top_execution_reason_codes", []),
            },
        }
        if self.last_block_reasons:
            extra["top_blockers"] = ", ".join(
                f"{name}:{count}" for name, count in sorted(self.last_block_reasons.items(), key=lambda item: item[1], reverse=True)[:3]
            )
        storage_summary = summarize_log_storage(self.config, project_root=Path(__file__).resolve().parent.parent)
        if storage_summary and ((self.config.get("storage", {}) or {}).get("logs", {}) or {}).get("report_in_status", True):
            extra["log_storage"] = storage_summary
        return build_snapshot(
            mode=status.get("mode", "unknown"),
            trading_enabled=bool(status.get("trading_enabled")),
            tradable_cap=status.get("max_tradable_balance", "unlimited"),
            max_position_size=status.get("max_position_size_usd", "unlimited"),
            balance=balance_value,
            available_cash=status.get("available_cash", "$0.00"),
            reserved_capital=status.get("reserved_capital", "$0.00"),
            exposure=status.get("exposure", "$0.00 (0.0%)"),
            pnl=pnl_value,
            pnl_pct=pnl_pct,
            win_rate_pct=self._parse_percent(status.get("win_rate", "0%")),
            total_trades=total_trades,
            open_trades=open_trades,
            resolved_trades=resolved_trades,
            scan_num=scan_num if scan_num is not None else self.stats.get("scans", 0),
            session_id="live-runner",
            extra=extra,
        )

    def _process_signal(self, signal: dict) -> Optional[dict]:
        exchange = self.exchanges.get(signal.get("exchange"))
        if not exchange:
            return None

        if self.single_trade_mode and self.single_trade_completed:
            return {"blocked_reason": "single_trade_mode_completed"}

        context = self.live_execution.build_trade_context(signal, exchange, self.config)
        strategy_cfg = self.config.get("strategy", {})
        decision = build_trade_decision(
            context,
            kelly_sizer=self.kelly,
            risk_policy=self.risk,
            min_edge=strategy_cfg.get("min_edge", self.config.get("min_edge", 0.02)),
            min_confidence=strategy_cfg.get("min_confidence", self.config.get("min_confidence", 0.50)),
            max_entry_price=self.config.get("max_entry_price", 0.70),
        )

        if not decision.approved:
            logger.info(f"🛑 Shared decision skipped: {decision.reason}")
            return {"blocked_reason": decision.reason_code, "decision": decision}

        result = self.live_execution.execute(signal, decision, exchange)
        if result:
            return {"order": result, "decision": decision}
        return {"blocked_reason": "execution_failed", "decision": decision}


    def _sync_resolved_positions(self):
        for exchange_name, exchange in self.exchanges.items():
            try:
                resolution_events = self.live_reconciliation.settle(exchange_name, exchange, self.open_positions)
            except Exception as e:
                logger.debug(f"Resolution sync failed for {exchange_name}: {e}")
                continue
            if not resolution_events:
                continue

            resolved_ids = {event.position_id for event in resolution_events}
            self.open_positions = [position for position in self.open_positions if position.order_id not in resolved_ids]
            for trade in self.trade_history:
                if trade.get("order_id") in resolved_ids and not trade.get("resolved"):
                    event = next((evt for evt in resolution_events if evt.position_id == trade.get("order_id")), None)
                    if event is None:
                        continue
                    trade["status"] = "resolved"
                    trade["lifecycle_state"] = "resolved_position"
                    trade["resolved"] = True
                    trade["resolved_at"] = event.resolved_at
                    trade["pnl"] = event.pnl
                    trade["settlement_value"] = event.settlement_value
                    trade["outcome"] = event.outcome
                    trade["resolution_outcome"] = event.outcome
                    if isinstance(event.metadata, dict) and event.metadata.get("resolution_result"):
                        trade["resolution_result"] = event.metadata.get("resolution_result")
                    apply_execution_audit_contract(trade)
                    self.risk.record_trade_result(trade.get("order_id"), event.pnl or 0.0)
            total_pnl = sum(float(event.pnl or 0.0) for event in resolution_events)
            self._log_lifecycle_event(
                "positions_resolved",
                {
                    "exchange": exchange_name,
                    "count": len(resolution_events),
                    "markets": [event.market_id for event in resolution_events],
                    "realized_pnl": total_pnl,
                    "balance_after": self.risk.state.current_balance,
                },
            )

    def _log_scan(self, signals: list, trades: int, blocked_reasons: dict[str, int]):
        timestamp = datetime.now(timezone.utc).isoformat()
        log_file = self.log_dir / f"scans_{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        market_groups: dict[str, int] = {}
        for signal in signals:
            group = str(signal.get("market_group") or "unknown")
            market_groups[group] = market_groups.get(group, 0) + 1
        candidate_summaries = [
            build_scan_candidate_summary(signal, timestamp=timestamp, rank=index)
            for index, signal in enumerate(signals[:3], start=1)
        ]
        with open(log_file, "a") as f:
            f.write(json.dumps({
                "timestamp": timestamp,
                "signals": len(signals),
                "trades": trades,
                "blocked_reasons": blocked_reasons,
                "market_groups": market_groups,
                "top_signals": signals[:3],
                "candidate_summaries": candidate_summaries,
            }) + "\n")

    def _log_trade(self, signal: dict, order, decision, size: float, price: float, audit_row: dict | None = None):
        log_file = self.log_dir / "trades.jsonl"
        payload = audit_row
        if payload is None:
            payload = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "signal": signal,
                "decision": {
                    "reason": decision.reason,
                    "reason_code": decision.reason_code,
                    "requested_position_size": decision.requested_position_size,
                    "position_size": decision.position_size,
                },
                "order_id": order.id if hasattr(order, 'id') else str(order),
                "fill_price": price,
                "size": size,
            }
        else:
            payload = apply_execution_audit_contract(dict(payload))
        with open(log_file, "a") as f:
            f.write(json.dumps(payload) + "\n")

    def _log_risk_block_event(self, signal: dict, result: dict):
        decision = result.get("decision")
        payload = build_risk_block_audit_row(
            signal,
            decision=decision,
            blocked_reason=result.get("blocked_reason", "unknown"),
            timestamp=datetime.now(timezone.utc).isoformat(),
            available_cash=self.risk.state.available_cash,
        )
        log_file = self.log_dir / "risk_blocks.jsonl"
        with open(log_file, "a") as f:
            f.write(json.dumps(payload) + "\n")

    def _emit_hourly_summary_if_due(self):
        now = datetime.now(timezone.utc)
        hour_key = now.strftime("%Y-%m-%dT%H")
        if self._last_hourly_summary_key == hour_key:
            return
        self._last_hourly_summary_key = hour_key

        summary = {
            "timestamp": now.isoformat(),
            "started_at": self.lifecycle_started_at.isoformat(),
            "mode": self.config.get("trading", {}).get("mode", "paper"),
            "scans": self.stats.get("scans", 0),
            "signals_considered": self.lifecycle_counters.get("signals_considered", 0),
            "trades_executed": self.lifecycle_counters.get("trades_executed", 0),
            "blocked_total": self.lifecycle_counters.get("blocked_total", 0),
            "top_blockers": dict(sorted(self.lifecycle_block_reasons.items(), key=lambda item: item[1], reverse=True)[:5]),
            "errors": self.lifecycle_counters.get("errors", 0),
            "open_positions": len(self.open_positions),
        }
        log_file = self.log_dir / "hourly_summary.jsonl"
        with open(log_file, "a") as f:
            f.write(json.dumps(summary) + "\n")
        self._log_lifecycle_event("hourly_summary", summary)

    def reload_runtime_controls_if_needed(self) -> bool:
        if not self._config_path or not self._config_path.exists():
            return False

        try:
            mtime = self._config_path.stat().st_mtime
        except OSError:
            return False

        if self._config_last_mtime is not None and mtime <= self._config_last_mtime:
            return False

        previous_mode = str(self.config.get("trading", {}).get("mode", "paper")).lower()
        previous_enabled = bool(
            self.config.get("trading", {}).get(
                "enabled",
                self.config.get("trading_enabled", self.config.get("trading", {}).get("trading_enabled", True)),
            )
        )

        reloaded = load_config(self._config_path)
        if self._config_path:
            reloaded["_config_path"] = str(self._config_path)
        if "trading" not in reloaded:
            reloaded["trading"] = {}
        if "enabled" in reloaded["trading"]:
            reloaded["trading_enabled"] = bool(reloaded["trading"]["enabled"])
        self.config = reloaded
        self._config_last_mtime = mtime

        self.risk = RiskManager(self.config)

        self.single_trade_mode = bool(self.config.get("trading", {}).get("single_trade_mode", self.single_trade_mode))
        self.alerts = self.config.get("alerts", {}) or {}
        self.verbosity_level = normalize_verbosity((self.config.get("verbosity", {}) or {}).get("level", self.verbosity_level))
        self.telegram_notifier = TelegramNotifier(self.alerts)

        current_mode = str(self.config.get("trading", {}).get("mode", previous_mode)).lower()
        self.log_dir = ensure_mode_storage_dir(self.config.get("log_dir", self.log_dir), current_mode)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        current_enabled = bool(
            self.config.get("trading", {}).get(
                "enabled",
                self.config.get("trading_enabled", self.config.get("trading", {}).get("trading_enabled", True)),
            )
        )

        if current_mode != previous_mode:
            self._log_lifecycle_event(
                "mode_changed",
                {"from": previous_mode, "to": current_mode},
            )

        if current_enabled != previous_enabled:
            self._log_lifecycle_event(
                "trading_resumed" if current_enabled else "trading_paused",
                {
                    "mode": current_mode,
                    "open_positions": len(self.open_positions),
                    "open_orders": len(self.open_orders),
                    "behavior": "leave_resting_orders_untouched",
                },
            )

        logger.info(
            "Reloaded runtime controls: mode=%s enabled=%s",
            current_mode,
            current_enabled,
        )
        return True

    def _notify_event(self, event_type: str, details: dict | None = None):
        if not self.alerts.get("enabled", True):
            return
        category_map = {
            "trade_placed": "trade_events",
            "single_trade_completed": "single_trade_events",
            "positions_resolved": "resolution_events",
            "reconciliation_completed": "reconciliation_events",
            "hourly_summary": "status_events",
        }
        category = category_map.get(event_type)
        if category and not self.alerts.get(category, False):
            return
        message = build_notification(event_type, details or {}, verbosity=self.verbosity_level)
        if not message:
            return
        notifications_file = self.log_dir / "notifications.jsonl"
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event_type,
            "verbosity": self.verbosity_level,
            "message": message,
        }
        with open(notifications_file, "a") as f:
            f.write(json.dumps(payload) + "\n")
        self.telegram_notifier.send(message)

    def _log_lifecycle_event(self, event_type: str, details: dict | None = None):
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event_type,
            "details": details or {},
        }
        log_file = self.log_dir / "lifecycle.jsonl"
        with open(log_file, "a") as f:
            f.write(json.dumps(payload) + "\n")
        self._notify_event(event_type, details)

    def stop(self):
        self.running = False
        self._log_lifecycle_event(
            "stop_requested",
            {
                "mode": self.config.get("trading", {}).get("mode", "paper"),
                "scans": self.stats.get("scans", 0),
                "trades": self.stats.get("trades", 0),
            },
        )

    def close(self):
        self._log_lifecycle_event(
            "shutdown",
            {
                "mode": self.config.get("trading", {}).get("mode", "paper"),
                "scans": self.stats.get("scans", 0),
                "signals": self.stats.get("signals", 0),
                "trades": self.stats.get("trades", 0),
                "errors": self.stats.get("errors", 0),
                "blocked": self.stats.get("blocked", 0),
                "open_positions": len(self.open_positions),
            },
        )
        if hasattr(self.strategy, 'news'):
            self.strategy.news.close()
        for exchange in self.exchanges.values():
            exchange.close()

    @staticmethod
    def _coerce_float(value, default: float = 0.0) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _parse_percent(value) -> float:
        text = str(value or "0").strip().replace("%", "")
        try:
            return float(text)
        except ValueError:
            return 0.0

    @staticmethod
    def _parse_pnl_fields(pnl_text: str) -> tuple[float, float]:
        text = str(pnl_text or "").strip()
        if not text:
            return 0.0, 0.0
        if "(" not in text:
            return PredictionBot._coerce_float(text.replace("$", "")), 0.0
        pnl_part, pct_part = text.split("(", 1)
        pnl_value = PredictionBot._coerce_float(pnl_part.replace("$", "").strip())
        pnl_pct = PredictionBot._parse_percent(pct_part.replace(")", "").strip())
        return pnl_value, pnl_pct
