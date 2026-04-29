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
from bot.trade_audit import (
    apply_execution_audit_contract,
    build_risk_block_audit_row,
    build_scan_candidate_summary,
    canonical_execution_status,
    enrich_trade_audit_fields,
)
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
        self.reconciliation_gate: dict[str, dict] = {}
        self.startup_reconciliation_status: dict[str, dict] = {}
        self.live_runtime_state: dict[str, object] = {
            "state": "safe",
            "reason": "startup",
            "issues": [],
            "recovery_state": "ready",
            "exchange_states": {},
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.live_failure_streaks: dict[str, dict[str, object]] = {}
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
                else:
                    issues = ["exchange_connect_failed", "startup_reconciliation_not_run"]
                    self._apply_reconciliation_runtime_state(name, "blocked", issues, source="startup_recovery")
                    self._record_startup_reconciliation_status(
                        name,
                        {
                            "status": "blocked",
                            "runtime_state": "blocked",
                            "reconciliation_verdict": "blocked",
                            "reconciliation_issues": issues,
                        },
                        source="startup_recovery",
                        completed=False,
                    )
                    self._log_lifecycle_event(
                        "reconciliation_failed",
                        {"exchange": name, "status": "connect_failed", "reconciliation_issues": issues},
                    )
            except Exception as e:
                logger.error(f"Failed to connect to {name}: {e}")
                issues = ["exchange_connect_failed", "startup_reconciliation_not_run"]
                self._apply_reconciliation_runtime_state(name, "blocked", issues, source="startup_recovery")
                self._record_startup_reconciliation_status(
                    name,
                    {
                        "status": "blocked",
                        "runtime_state": "blocked",
                        "reconciliation_verdict": "blocked",
                        "reconciliation_issues": issues,
                        "error": str(e),
                    },
                    source="startup_recovery",
                    completed=False,
                )
                self._log_lifecycle_event(
                    "reconciliation_failed",
                    {"exchange": name, "error": str(e), "reconciliation_issues": issues},
                )
                results[name] = False
        return results

    def _reconcile_exchange_state(self, exchange_name: str, exchange: BaseExchange) -> dict:
        try:
            snapshot = self.live_reconciliation.reconcile(exchange_name, exchange)
        except Exception as e:
            logger.error(f"Failed to fetch exchange state for reconciliation from {exchange_name}: {e}")
            issues = ["reconciliation_refresh_failed"]
            self._apply_reconciliation_runtime_state(exchange_name, "blocked", issues, source="startup_reconciliation")
            self._record_live_failure(exchange_name, "reconciliation_state_blocked", issues=issues, runtime_state="blocked")
            summary = {
                "exchange": exchange_name,
                "open_positions": 0,
                "open_orders": 0,
                "trade_history_loaded": 0,
                "status": "blocked",
                "runtime_state": "blocked",
                "reconciliation_verdict": "blocked",
                "reconciliation_issues": issues,
            }
            self._record_startup_reconciliation_status(
                exchange_name,
                summary,
                source="startup_reconciliation",
                completed=False,
            )
            self._log_lifecycle_event(
                "reconciliation_failed",
                {"exchange": exchange_name, "error": str(e), "reconciliation_issues": issues, "runtime_state": "blocked"},
            )
            return summary

        self.open_positions = snapshot.open_positions
        self.open_orders = snapshot.open_orders
        corrected_trade_ids = self._apply_reconciliation_trade_history_corrections(
            exchange_name,
            snapshot,
            source="startup_reconciliation",
        )
        snapshot_rows = [
            row for row in snapshot.trade_history_rows
            if str(row.get("trade_id") or row.get("order_id") or "") not in corrected_trade_ids
        ]
        self.trade_history = [trade for trade in self.trade_history if not trade.get("reconciled")] + snapshot_rows

        balance = self._coerce_float(getattr(snapshot, "balance", None), default=self.risk.state.current_balance)
        self.risk.sync_account_state(
            current_balance=balance,
            available_cash=snapshot.available_cash,
            reserved_capital=snapshot.reserved_capital,
            total_exposure=snapshot.reserved_capital,
            open_positions=len(self.open_positions),
        )

        verdict = getattr(snapshot, "verdict", "safe") or "safe"
        issues = list(getattr(snapshot, "issues", []) or [])
        invariant_issues: list[str] = []
        if verdict != "blocked":
            invariant_issues = self._enforce_live_runtime_invariants(exchange_name, source="startup_reconciliation")
            if invariant_issues:
                verdict = "blocked"
                issues = list(dict.fromkeys(issues + invariant_issues))

        if verdict == "blocked":
            self._apply_reconciliation_runtime_state(exchange_name, verdict, issues, source="startup_reconciliation")
            failure_reason = "runtime_invariant_violation" if "runtime_invariant_violation" in set(issues) else "reconciliation_state_blocked"
            if failure_reason != "runtime_invariant_violation":
                self._record_live_failure(
                    exchange_name,
                    failure_reason,
                    issues=issues,
                    runtime_state="blocked",
                )
        else:
            self._apply_reconciliation_runtime_state(exchange_name, verdict, issues, source="startup_reconciliation")
            if verdict == "safe":
                self._clear_live_failure_streak(exchange_name)

        summary = {
            "exchange": exchange_name,
            "open_positions": len(snapshot.open_positions),
            "open_orders": len(snapshot.open_orders),
            "trade_history_loaded": len(snapshot.trade_history_rows),
            "balance": getattr(snapshot, "balance", balance),
            "filled_exposure": getattr(snapshot, "filled_exposure", 0.0),
            "pending_exposure": getattr(snapshot, "pending_exposure", 0.0),
            "reserved_capital": snapshot.reserved_capital,
            "available_cash": snapshot.available_cash,
            "partial_fills": snapshot.partial_fills,
            "reconciliation_verdict": verdict,
            "reconciliation_issues": issues,
            "reconciliation_severity": getattr(snapshot, "severity", "none"),
            "reconciliation_action": getattr(snapshot, "action", "log_only"),
            "reconciliation_corrections": list(getattr(snapshot, "correction_events", []) or []),
            "runtime_state": self._live_runtime_state_for_exchange(exchange_name).get("state", verdict),
            "status": verdict,
        }
        self._record_reconciliation_snapshot(exchange_name, snapshot, source="startup_reconciliation")
        self._record_startup_reconciliation_status(
            exchange_name,
            summary,
            source="startup_reconciliation",
            completed=True,
        )
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
            "mode_label": status.get("mode_label", "live" if self._is_live_mode() else "normal paper"),
            "risk_preset_mode": status.get("risk_preset_mode", "live" if self._is_live_mode() else "paper"),
            "parity_comparison_mode": status.get("parity_comparison_mode", "production"),
            "blocked_last_scan": sum(self.last_block_reasons.values()),
            "signals_considered": self.lifecycle_counters.get("signals_considered", 0),
            "trades_executed": self.lifecycle_counters.get("trades_executed", 0),
            "blocked_total": self.lifecycle_counters.get("blocked_total", 0),
            "runner_errors": self.lifecycle_counters.get("errors", 0),
            "live_failure_streaks": {
                exchange: {
                    "count": int(details.get("count", 0) or 0),
                    "last_reason": details.get("last_reason", ""),
                    "issues": list(details.get("issues") or []),
                    "recovery_state": details.get("recovery_state", "clear_after_safe_reconciliation"),
                    "max_failures": details.get("max_failures"),
                }
                for exchange, details in self.live_failure_streaks.items()
            },
            "reconciliation_gate": {
                exchange: {
                    "verdict": details.get("verdict", "unknown"),
                    "issues": list(details.get("issues") or []),
                    "reason": details.get("reason", ""),
                    "recovery_state": details.get("recovery_state", "requires_safe_reconciliation"),
                }
                for exchange, details in self.reconciliation_gate.items()
            },
            "startup_reconciliation": {
                exchange: {
                    "completed": bool(details.get("completed")),
                    "source": details.get("source", ""),
                    "status": details.get("status", "pending"),
                    "runtime_state": details.get("runtime_state", "pending"),
                    "reconciliation_verdict": details.get("reconciliation_verdict", "pending"),
                    "reconciliation_issues": list(details.get("reconciliation_issues") or []),
                    "updated_at": details.get("updated_at", ""),
                }
                for exchange, details in {
                    **{
                        name: {
                            "completed": False,
                            "source": "not_run",
                            "status": "pending",
                            "runtime_state": "pending",
                            "reconciliation_verdict": "pending",
                            "reconciliation_issues": ["startup_reconciliation_not_run"],
                            "updated_at": "",
                        }
                        for name in self.exchanges.keys()
                        if self._is_live_mode()
                    },
                    **self.startup_reconciliation_status,
                }.items()
            },
            "live_runtime_state": {
                "state": self.live_runtime_state.get("state", "safe"),
                "reason": self.live_runtime_state.get("reason", ""),
                "issues": list(self.live_runtime_state.get("issues") or []),
                "recovery_state": self.live_runtime_state.get("recovery_state", "ready"),
                "updated_at": self.live_runtime_state.get("updated_at", ""),
            },
            "live_exchange_states": {
                exchange: {
                    "state": details.get("state", "safe"),
                    "reason": details.get("reason", ""),
                    "issues": list(details.get("issues") or []),
                    "recovery_state": details.get("recovery_state", "ready"),
                    "updated_at": details.get("updated_at", ""),
                }
                for exchange, details in dict(self.live_runtime_state.get("exchange_states") or {}).items()
            },
            "safety_pause": self._safety_pause_status(),
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

    def _live_safety_config(self) -> dict:
        trading_cfg = self.config.get("trading") or {}
        live_safety = trading_cfg.get("live_safety") or {}
        max_critical = max(1, int(live_safety.get("max_consecutive_critical_failures", 2) or 2))
        return {
            "enabled": bool(live_safety.get("enabled", True)),
            "max_consecutive_critical_failures": max_critical,
            "max_consecutive_reconciliation_mismatches": max(
                1,
                int(live_safety.get("max_consecutive_reconciliation_mismatches", max_critical) or max_critical),
            ),
        }

    @staticmethod
    def _live_runtime_priority(state: str | None) -> int:
        return {"safe": 0, "degraded": 1, "blocked": 2}.get(str(state or "safe").lower(), 1)

    @staticmethod
    def _normalize_live_runtime_state(state: str | None) -> str:
        state = str(state or "safe").lower()
        return state if state in {"safe", "degraded", "blocked"} else "degraded"

    def _live_runtime_state_for_exchange(self, exchange_name: str) -> dict:
        exchange_states = dict(self.live_runtime_state.get("exchange_states") or {})
        return dict(exchange_states.get(exchange_name) or {"state": "safe", "reason": "", "issues": []})

    def _block_on_degraded_runtime(self) -> bool:
        return bool((((self.config.get("trading") or {}).get("live_reconciliation") or {}).get("block_on_degraded", False)))

    def _is_live_mode(self) -> bool:
        return str((self.config.get("trading") or {}).get("mode", "paper")).strip().lower() == "live"

    def _record_startup_reconciliation_status(
        self,
        exchange_name: str,
        summary: dict,
        *,
        source: str,
        completed: bool,
    ):
        if not exchange_name:
            return
        self.startup_reconciliation_status[exchange_name] = {
            "completed": bool(completed),
            "source": source,
            "status": summary.get("status", summary.get("reconciliation_verdict", "unknown")),
            "runtime_state": summary.get("runtime_state", summary.get("status", "unknown")),
            "reconciliation_verdict": summary.get("reconciliation_verdict", summary.get("status", "unknown")),
            "reconciliation_issues": list(summary.get("reconciliation_issues") or []),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _ensure_live_startup_reconciled_before_trade(self, exchange_name: str, exchange: BaseExchange) -> dict | None:
        if not self._is_live_mode() or not exchange_name:
            return None
        gate = self.reconciliation_gate.get(exchange_name) or {}
        if str(gate.get("verdict") or "").lower() == "blocked":
            return None
        status = self.startup_reconciliation_status.get(exchange_name) or {}
        if status.get("completed"):
            return None
        return self._reconcile_exchange_state(exchange_name, exchange)

    @staticmethod
    def _requires_manual_review(issues: list[str] | None = None, recovery_state: str | None = None) -> bool:
        if str(recovery_state or "") == "manual_review_required":
            return True
        issue_set = {str(issue) for issue in (issues or [])}
        return "runtime_invariant_violation" in issue_set

    def _apply_reconciliation_runtime_state(
        self,
        exchange_name: str,
        verdict: str | None,
        issues: list[str] | None,
        *,
        source: str,
    ):
        verdict = self._normalize_live_runtime_state(verdict)
        issues = list(dict.fromkeys(issues or []))
        existing_gate = dict(self.reconciliation_gate.get(exchange_name) or {})
        if verdict == "blocked":
            recovery_state = "manual_review_required" if self._requires_manual_review(issues) else "requires_safe_reconciliation"
            self.reconciliation_gate[exchange_name] = {
                "verdict": "blocked",
                "issues": issues,
                "reason": "runtime_invariant_violation" if recovery_state == "manual_review_required" else f"{source}_blocked",
                "recovery_state": recovery_state,
            }
        elif verdict == "degraded":
            verdict = self._record_reconciliation_mismatch(exchange_name, issues, source=source)
            if verdict == "blocked":
                gate = self.reconciliation_gate.get(exchange_name, {})
                issues = list(gate.get("issues") or issues)
        else:
            gate_requires_manual = self._requires_manual_review(
                list(existing_gate.get("issues") or []),
                str(existing_gate.get("recovery_state") or ""),
            )
            if gate_requires_manual:
                verdict = "blocked"
                issues = list(dict.fromkeys(list(existing_gate.get("issues") or []) + issues))
                self.reconciliation_gate[exchange_name] = existing_gate
            else:
                self.reconciliation_gate.pop(exchange_name, None)
                entry = self.live_failure_streaks.get(exchange_name) or {}
                if str(entry.get("last_reason") or "") in {
                    "reconciliation_state_degraded",
                    "reconciliation_state_blocked",
                    "runtime_invariant_violation",
                }:
                    self._clear_live_failure_streak(exchange_name)
        runtime_reason = f"{source}_{verdict}"
        if verdict == "blocked" and self.reconciliation_gate.get(exchange_name, {}).get("reason"):
            runtime_reason = str(self.reconciliation_gate[exchange_name].get("reason"))
        self._update_live_runtime_state(
            exchange_name,
            verdict,
            reason=runtime_reason,
            issues=issues,
            details={"source": source, "reconciliation_verdict": verdict},
        )

    def _record_reconciliation_snapshot(self, exchange_name: str, snapshot, *, source: str):
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "exchange": exchange_name,
            "verdict": getattr(snapshot, "verdict", "safe"),
            "severity": getattr(snapshot, "severity", "none"),
            "action": getattr(snapshot, "action", "log_only"),
            "issues": list(getattr(snapshot, "issues", []) or []),
            "state_flags": list(getattr(snapshot, "state_flags", []) or []),
            "balance": round(self._coerce_float(getattr(snapshot, "balance", 0.0), 0.0), 2),
            "available_cash": round(self._coerce_float(getattr(snapshot, "available_cash", 0.0), 0.0), 2),
            "reserved_capital": round(self._coerce_float(getattr(snapshot, "reserved_capital", 0.0), 0.0), 2),
            "filled_exposure": round(self._coerce_float(getattr(snapshot, "filled_exposure", 0.0), 0.0), 2),
            "pending_exposure": round(self._coerce_float(getattr(snapshot, "pending_exposure", 0.0), 0.0), 2),
            "open_positions": len(getattr(snapshot, "open_positions", []) or []),
            "open_orders": len(getattr(snapshot, "open_orders", []) or []),
            "partial_fills": int(getattr(snapshot, "partial_fills", 0) or 0),
            "corrections": list(getattr(snapshot, "correction_events", []) or []),
        }
        log_file = self.log_dir / "reconciliation.jsonl"
        with open(log_file, "a") as f:
            f.write(json.dumps(payload) + "\n")

    def _apply_reconciliation_trade_history_corrections(self, exchange_name: str, snapshot, *, source: str) -> set[str]:
        exchange_rows: dict[str, dict] = {}
        for row in getattr(snapshot, "trade_history_rows", []) or []:
            row_id = str(row.get("trade_id") or row.get("order_id") or "")
            if row_id:
                exchange_rows[row_id] = row

        matched_ids: set[str] = set()
        corrected_ids: set[str] = set()
        if not exchange_rows or not self.trade_history:
            return matched_ids

        now = datetime.now(timezone.utc).isoformat()
        correction_fields = (
            "status",
            "lifecycle_state",
            "placed_size",
            "filled_size",
            "remaining_size",
            "reserved_capital",
            "fill_price",
            "entry_price",
            "price",
            "market_price",
            "direction",
            "exchange",
            "event_key",
        )
        for index, trade in enumerate(list(self.trade_history)):
            if trade.get("reconciled"):
                continue
            row_id = str(trade.get("order_id") or trade.get("trade_id") or "")
            exchange_row = exchange_rows.get(row_id)
            if not row_id or not exchange_row:
                continue

            matched_ids.add(row_id)
            changed_fields: list[str] = []
            before: dict[str, object] = {}
            for field_name in correction_fields:
                if field_name not in exchange_row:
                    continue
                local_value = trade.get(field_name)
                exchange_value = exchange_row.get(field_name)
                if self._reconciliation_values_equal(local_value, exchange_value):
                    continue
                before[field_name] = local_value
                trade[field_name] = exchange_value
                changed_fields.append(field_name)

            if not changed_fields:
                continue

            trade["reconciliation_corrected"] = True
            trade["reconciliation_corrected_at"] = now
            trade["reconciliation_source"] = source
            trade["reconciliation_exchange"] = exchange_name
            trade["reconciliation_previous_values"] = before
            trade["reconciliation_corrected_fields"] = changed_fields
            trade["reconciliation_contract"] = {
                "verdict": getattr(snapshot, "verdict", "safe"),
                "severity": getattr(snapshot, "severity", "none"),
                "action": getattr(snapshot, "action", "log_only"),
                "issues": list(getattr(snapshot, "issues", []) or []),
            }
            self.trade_history[index] = apply_execution_audit_contract(trade)
            corrected_ids.add(row_id)

        if corrected_ids:
            self._log_lifecycle_event(
                "reconciliation_corrections_applied",
                {
                    "exchange": exchange_name,
                    "source": source,
                    "corrected_trade_ids": sorted(corrected_ids),
                    "correction_events": [
                        event for event in list(getattr(snapshot, "correction_events", []) or [])
                        if str(event.get("order_id") or event.get("position_id") or "") in corrected_ids
                    ],
                },
            )
        return matched_ids

    @staticmethod
    def _reconciliation_values_equal(left, right) -> bool:
        if isinstance(left, (int, float)) or isinstance(right, (int, float)):
            try:
                return round(float(left or 0.0), 4) == round(float(right or 0.0), 4)
            except (TypeError, ValueError):
                return False
        return left == right

    def _live_runtime_invariant_issues(self) -> list[str]:
        issues: list[str] = []
        risk_state = self.risk.state
        available_cash = self._coerce_float(getattr(risk_state, "available_cash", 0.0), 0.0)
        reserved_capital = self._coerce_float(getattr(risk_state, "reserved_capital", 0.0), 0.0)
        total_exposure = self._coerce_float(getattr(risk_state, "total_exposure", 0.0), 0.0)
        current_balance = self._coerce_float(getattr(risk_state, "current_balance", 0.0), 0.0)

        if available_cash < -0.01:
            issues.append("negative_available_cash_runtime")
        if reserved_capital < -0.01:
            issues.append("negative_reserved_capital_runtime")
        if total_exposure < -0.01:
            issues.append("negative_total_exposure_runtime")

        filled_exposure = 0.0
        position_ids: list[str] = []
        for position in self.open_positions:
            size = self._coerce_float(getattr(position, "size", 0.0), 0.0)
            if size < -0.01:
                issues.append("negative_open_position_exposure")
                continue
            filled_exposure += max(0.0, size)
            position_id = str(getattr(position, "order_id", "") or "")
            if position_id:
                position_ids.append(position_id)

        pending_exposure = 0.0
        order_ids: list[str] = []
        open_order_intents: list[tuple[str, str, str]] = []
        for order in self.open_orders:
            remaining_size = self._coerce_float(order.get("remaining_size", 0.0), 0.0)
            if remaining_size < -0.01:
                issues.append("negative_open_order_exposure")
                continue
            status = canonical_execution_status(
                order.get("status"),
                filled_size=order.get("filled_size"),
                placed_size=order.get("placed_size"),
                remaining_size=remaining_size,
            )
            if status in {"rejected", "failed", "canceled", "resolved", "filled"} or remaining_size <= 0.0:
                continue
            pending_exposure += remaining_size
            order_id = str(order.get("order_id") or "")
            if order_id:
                order_ids.append(order_id)
            open_order_intents.append((
                str(order.get("exchange") or ""),
                str(order.get("market_id") or ""),
                str(order.get("direction") or "BUY_YES").upper(),
            ))

        if len(order_ids) != len(set(order_ids)) or len(position_ids) != len(set(position_ids)):
            issues.append("duplicate_live_exposure")
        meaningful_intents = [intent for intent in open_order_intents if intent[1]]
        if len(meaningful_intents) != len(set(meaningful_intents)):
            issues.append("duplicate_live_exposure")

        expected_reserved = round(filled_exposure + pending_exposure, 2)
        if current_balance - expected_reserved < -0.01:
            issues.append("negative_available_cash_from_open_exposure")
        if abs(round(reserved_capital, 2) - expected_reserved) > 0.01:
            issues.append("reserved_capital_open_exposure_mismatch")
        if abs(round(total_exposure, 2) - expected_reserved) > 0.01:
            issues.append("total_exposure_open_exposure_mismatch")

        for trade in self.trade_history:
            status = canonical_execution_status(
                trade.get("status"),
                filled_size=trade.get("filled_size"),
                placed_size=trade.get("placed_size"),
                remaining_size=trade.get("remaining_size"),
            )
            if status != "canceled":
                continue
            filled_size = self._coerce_float(trade.get("filled_size", 0.0), 0.0)
            remaining_size = self._coerce_float(trade.get("remaining_size", 0.0), 0.0)
            lifecycle_state = str(trade.get("lifecycle_state") or "")
            if remaining_size > 0.01:
                issues.append("impossible_canceled_order_remaining_exposure")
            if filled_size > 0.01 and lifecycle_state != "canceled_partial":
                issues.append("canceled_partial_lifecycle_mismatch")

        return sorted(set(issues))

    def _enforce_live_runtime_invariants(self, exchange_name: str | None, *, source: str) -> list[str]:
        issues = self._live_runtime_invariant_issues()
        if not issues:
            return []
        exchange_key = exchange_name or "runtime"
        gate_issues = list(dict.fromkeys(["runtime_invariant_violation"] + issues))
        self.reconciliation_gate[exchange_key] = {
            "verdict": "blocked",
            "issues": gate_issues,
            "reason": "runtime_invariant_violation",
            "recovery_state": "manual_review_required",
        }
        self._update_live_runtime_state(
            exchange_key,
            "blocked",
            reason="runtime_invariant_violation",
            issues=gate_issues,
            details={"source": source, "invariant_issues": issues},
        )
        self._record_live_failure(
            exchange_key,
            "runtime_invariant_violation",
            issues=issues,
            runtime_state="blocked",
        )
        self._log_lifecycle_event(
            "live_safety_pause",
            {
                "exchange": exchange_key,
                "last_reason": "runtime_invariant_violation",
                "issues": gate_issues,
                "recovery_state": "manual_review_required",
                "runtime_state": "blocked",
                "source": source,
            },
        )
        return gate_issues

    def _update_live_runtime_state(
        self,
        exchange_name: str,
        state: str | None,
        *,
        reason: str,
        issues: list[str] | None = None,
        details: dict | None = None,
    ):
        if not exchange_name:
            return
        state = self._normalize_live_runtime_state(state)
        issues = list(dict.fromkeys(issues or []))
        now = datetime.now(timezone.utc).isoformat()
        exchange_states = dict(self.live_runtime_state.get("exchange_states") or {})
        previous_exchange = dict(exchange_states.get(exchange_name) or {})
        exchange_states[exchange_name] = {
            "state": state,
            "reason": reason,
            "issues": issues,
            "recovery_state": self._recovery_state_for_runtime(state, issues),
            "updated_at": now,
            "details": details or {},
        }

        aggregate_state = "safe"
        for entry in exchange_states.values():
            entry_state = self._normalize_live_runtime_state(entry.get("state"))
            if self._live_runtime_priority(entry_state) > self._live_runtime_priority(aggregate_state):
                aggregate_state = entry_state

        aggregate_issues: list[str] = []
        for entry in exchange_states.values():
            if self._normalize_live_runtime_state(entry.get("state")) == "safe":
                continue
            aggregate_issues.extend(list(entry.get("issues") or []))
        aggregate_issues = list(dict.fromkeys(aggregate_issues))

        previous_state = str(self.live_runtime_state.get("state") or "safe")
        self.live_runtime_state = {
            "state": aggregate_state,
            "reason": reason,
            "issues": aggregate_issues,
            "recovery_state": self._recovery_state_for_runtime(aggregate_state, aggregate_issues),
            "exchange_states": exchange_states,
            "updated_at": now,
        }

        changed = (
            previous_state != aggregate_state
            or previous_exchange.get("state") != state
            or previous_exchange.get("reason") != reason
            or list(previous_exchange.get("issues") or []) != issues
        )
        if changed:
            self._log_lifecycle_event(
                "live_runtime_state_changed",
                {
                    "exchange": exchange_name,
                    "state": aggregate_state,
                    "exchange_state": state,
                    "reason": reason,
                    "issues": aggregate_issues,
                    "exchange_issues": issues,
                    "details": details or {},
                },
            )

    def _live_failure_runtime_state(self, reason_code: str, count: int, threshold: int) -> str:
        immediate_block_reasons = {
            "reconciliation_state_blocked",
            "runtime_invariant_violation",
            "duplicate_live_intent_open",
            "duplicate_submission_suspected",
            "placement_confirmation_uncertain",
            "live_identity_mismatch",
        }
        if reason_code in immediate_block_reasons or count >= threshold:
            return "blocked"
        return "degraded"

    @staticmethod
    def _is_critical_live_block(reason_code: str | None) -> bool:
        critical_codes = {
            "execution_failed",
            "reconciliation_state_blocked",
            "reconciliation_state_degraded",
            "runtime_invariant_violation",
            "duplicate_live_intent_open",
            "duplicate_submission_suspected",
            "placement_confirmation_uncertain",
            "live_identity_mismatch",
        }
        return str(reason_code or "") in critical_codes

    @staticmethod
    def _recovery_state_for_runtime(state: str | None, issues: list[str] | None = None) -> str:
        state = PredictionBot._normalize_live_runtime_state(state)
        issue_set = set(issues or [])
        if state == "blocked":
            if "runtime_invariant_violation" in issue_set:
                return "manual_review_required"
            return "requires_safe_reconciliation"
        if state == "degraded":
            return "retry_limited_until_safe_reconciliation"
        return "ready"

    def _safety_pause_status(self) -> dict[str, object]:
        exchange_states = dict(self.live_runtime_state.get("exchange_states") or {})
        blocked_exchanges = sorted(
            exchange
            for exchange, details in exchange_states.items()
            if self._normalize_live_runtime_state(details.get("state")) == "blocked"
        )
        degraded_exchanges = sorted(
            exchange
            for exchange, details in exchange_states.items()
            if self._normalize_live_runtime_state(details.get("state")) == "degraded"
        )
        gate_exchanges = sorted(self.reconciliation_gate.keys())
        active = bool(blocked_exchanges or gate_exchanges or self.live_runtime_state.get("state") == "blocked")
        issues = list(dict.fromkeys(
            list(self.live_runtime_state.get("issues") or [])
            + [
                issue
                for details in self.reconciliation_gate.values()
                for issue in list(details.get("issues") or [])
            ]
        ))
        reason = str(self.live_runtime_state.get("reason") or "")
        return {
            "active": active,
            "state": self.live_runtime_state.get("state", "safe"),
            "reason": reason,
            "issues": issues,
            "blocked_exchanges": sorted(set(blocked_exchanges + gate_exchanges)),
            "degraded_exchanges": degraded_exchanges,
            "recovery_state": self.live_runtime_state.get("recovery_state", self._recovery_state_for_runtime(self.live_runtime_state.get("state"), issues)),
            "retry_allowed": not active and not self._block_on_degraded_runtime(),
            "recovery_hint": (
                "manual_review_then_safe_reconciliation_required"
                if active and "runtime_invariant_violation" in issues
                else "safe_reconciliation_clears_pause"
                if active
                else "none"
            ),
        }

    def _record_reconciliation_mismatch(self, exchange_name: str, issues: list[str], *, source: str) -> str:
        cfg = self._live_safety_config()
        if not cfg.get("enabled") or not exchange_name:
            return "degraded"
        low_only_issues = {"partial_fill_exposure_present", "resting_orders_present"}
        if not issues or set(issues).issubset(low_only_issues):
            return "degraded"

        threshold = int(cfg["max_consecutive_reconciliation_mismatches"])
        entry = self.live_failure_streaks.get(exchange_name, {"count": 0, "last_reason": "", "issues": []})
        entry["count"] = int(entry.get("count", 0) or 0) + 1
        entry["last_reason"] = "reconciliation_state_degraded"
        entry["issues"] = list(dict.fromkeys(list(entry.get("issues") or []) + issues + ["reconciliation_state_degraded"]))
        entry["max_failures"] = threshold
        entry["recovery_state"] = "clear_after_safe_reconciliation"
        self.live_failure_streaks[exchange_name] = entry
        if int(entry["count"]) < threshold:
            return "degraded"

        gate_issues = list(dict.fromkeys(list(entry.get("issues") or []) + ["repeated_reconciliation_mismatches_threshold_reached"]))
        self.reconciliation_gate[exchange_name] = {
            "verdict": "blocked",
            "issues": gate_issues,
            "reason": "repeated_reconciliation_mismatches_threshold_reached",
            "recovery_state": "requires_safe_reconciliation",
        }
        self._update_live_runtime_state(
            exchange_name,
            "blocked",
            reason="repeated_reconciliation_mismatches_threshold_reached",
            issues=gate_issues,
            details={"source": source, "failure_count": int(entry["count"]), "max_failures": threshold},
        )
        self._log_lifecycle_event(
            "live_safety_pause",
            {
                "exchange": exchange_name,
                "failure_count": int(entry["count"]),
                "last_reason": "reconciliation_state_degraded",
                "issues": gate_issues,
                "recovery_state": "requires_safe_reconciliation",
                "runtime_state": "blocked",
            },
        )
        return "blocked"

    def _record_live_failure(
        self,
        exchange_name: str,
        reason_code: str,
        *,
        issues: list[str] | None = None,
        runtime_state: str | None = None,
    ):
        cfg = self._live_safety_config()
        if not cfg.get("enabled") or not exchange_name or not self._is_critical_live_block(reason_code):
            return
        entry = self.live_failure_streaks.get(exchange_name, {"count": 0, "last_reason": "", "issues": []})
        entry["count"] = int(entry.get("count", 0) or 0) + 1
        entry["last_reason"] = reason_code
        entry["issues"] = list(dict.fromkeys((issues or []) + ([reason_code] if reason_code else [])))
        entry["max_failures"] = cfg["max_consecutive_critical_failures"]
        entry["recovery_state"] = "manual_review_required" if reason_code == "runtime_invariant_violation" else "clear_after_safe_reconciliation"
        self.live_failure_streaks[exchange_name] = entry
        failure_state = runtime_state or self._live_failure_runtime_state(
            reason_code,
            int(entry["count"]),
            int(cfg["max_consecutive_critical_failures"]),
        )
        failure_issues = list(entry.get("issues") or [])
        if failure_state == "blocked":
            self.reconciliation_gate[exchange_name] = {
                "verdict": "blocked",
                "issues": failure_issues,
                "reason": reason_code,
                "recovery_state": "manual_review_required" if reason_code == "runtime_invariant_violation" else "requires_safe_reconciliation",
            }
        self._update_live_runtime_state(
            exchange_name,
            failure_state,
            reason=reason_code,
            issues=failure_issues,
            details={"failure_count": int(entry["count"]), "max_failures": cfg["max_consecutive_critical_failures"]},
        )
        if entry["count"] >= cfg["max_consecutive_critical_failures"]:
            gate_issues = list(dict.fromkeys(list(entry.get("issues") or []) + ["repeated_live_failures_threshold_reached"]))
            gate_reason = reason_code if reason_code == "runtime_invariant_violation" else "repeated_live_failures_threshold_reached"
            self.reconciliation_gate[exchange_name] = {
                "verdict": "blocked",
                "issues": gate_issues,
                "reason": gate_reason,
                "recovery_state": "manual_review_required" if reason_code == "runtime_invariant_violation" else "requires_safe_reconciliation",
            }
            self._update_live_runtime_state(
                exchange_name,
                "blocked",
                reason=gate_reason,
                issues=gate_issues,
                details={"failure_count": int(entry["count"]), "max_failures": cfg["max_consecutive_critical_failures"]},
            )
            self._log_lifecycle_event(
                "live_safety_pause",
                {
                    "exchange": exchange_name,
                    "failure_count": entry["count"],
                    "last_reason": reason_code,
                    "issues": gate_issues,
                    "recovery_state": "requires_safe_reconciliation",
                    "runtime_state": "blocked",
                },
            )

    def _clear_live_failure_streak(self, exchange_name: str):
        if not exchange_name:
            return
        self.live_failure_streaks.pop(exchange_name, None)

    def _process_signal(self, signal: dict) -> Optional[dict]:
        exchange_name = signal.get("exchange")
        exchange = self.exchanges.get(exchange_name)
        if not exchange:
            return None

        if self.single_trade_mode and self.single_trade_completed:
            return {"blocked_reason": "single_trade_mode_completed"}

        gate = self.reconciliation_gate.get(exchange_name or "") or {}
        if (gate.get("verdict") or "") == "blocked":
            gate_reason = str(gate.get("reason") or "reconciliation_state_blocked")
            blocked_reason = "runtime_invariant_violation" if gate_reason == "runtime_invariant_violation" else "reconciliation_state_blocked"
            return {
                "blocked_reason": blocked_reason,
                "decision": None,
                "reconciliation_issues": list(gate.get("issues") or []),
                "recovery_state": gate.get("recovery_state", "requires_safe_reconciliation"),
            }

        runtime_state = self._live_runtime_state_for_exchange(exchange_name or "")
        if runtime_state.get("state") == "blocked":
            return {
                "blocked_reason": str(runtime_state.get("reason") or "live_runtime_state_blocked"),
                "decision": None,
                "reconciliation_issues": list(runtime_state.get("issues") or []),
                "runtime_state": runtime_state,
            }
        if runtime_state.get("state") == "degraded" and self._block_on_degraded_runtime():
            return {
                "blocked_reason": "reconciliation_state_degraded",
                "decision": None,
                "reconciliation_issues": list(runtime_state.get("issues") or []),
                "runtime_state": runtime_state,
            }

        self._ensure_live_startup_reconciled_before_trade(exchange_name or "", exchange)

        gate = self.reconciliation_gate.get(exchange_name or "") or {}
        if (gate.get("verdict") or "") == "blocked":
            gate_reason = str(gate.get("reason") or "reconciliation_state_blocked")
            blocked_reason = "runtime_invariant_violation" if gate_reason == "runtime_invariant_violation" else "reconciliation_state_blocked"
            return {
                "blocked_reason": blocked_reason,
                "decision": None,
                "reconciliation_issues": list(gate.get("issues") or []),
                "recovery_state": gate.get("recovery_state", "requires_safe_reconciliation"),
            }

        runtime_state = self._live_runtime_state_for_exchange(exchange_name or "")
        if runtime_state.get("state") == "blocked":
            return {
                "blocked_reason": str(runtime_state.get("reason") or "live_runtime_state_blocked"),
                "decision": None,
                "reconciliation_issues": list(runtime_state.get("issues") or []),
                "runtime_state": runtime_state,
            }
        if runtime_state.get("state") == "degraded" and self._block_on_degraded_runtime():
            return {
                "blocked_reason": "reconciliation_state_degraded",
                "decision": None,
                "reconciliation_issues": list(runtime_state.get("issues") or []),
                "runtime_state": runtime_state,
            }

        invariant_issues = self._enforce_live_runtime_invariants(exchange_name or "", source="pre_signal")
        if invariant_issues:
            return {
                "blocked_reason": "runtime_invariant_violation",
                "decision": None,
                "reconciliation_issues": invariant_issues,
                "recovery_state": "manual_review_required",
                "runtime_state": self._live_runtime_state_for_exchange(exchange_name or ""),
            }

        gate = self.reconciliation_gate.get(exchange_name or "") or {}
        if (gate.get("verdict") or "") == "blocked":
            gate_reason = str(gate.get("reason") or "reconciliation_state_blocked")
            blocked_reason = "runtime_invariant_violation" if gate_reason == "runtime_invariant_violation" else "reconciliation_state_blocked"
            return {
                "blocked_reason": blocked_reason,
                "decision": None,
                "reconciliation_issues": list(gate.get("issues") or []),
                "recovery_state": gate.get("recovery_state", "requires_safe_reconciliation"),
            }

        runtime_state = self._live_runtime_state_for_exchange(exchange_name or "")
        if runtime_state.get("state") == "blocked":
            return {
                "blocked_reason": str(runtime_state.get("reason") or "live_runtime_state_blocked"),
                "decision": None,
                "reconciliation_issues": list(runtime_state.get("issues") or []),
                "runtime_state": runtime_state,
            }
        if runtime_state.get("state") == "degraded" and self._block_on_degraded_runtime():
            return {
                "blocked_reason": "reconciliation_state_degraded",
                "decision": None,
                "reconciliation_issues": list(runtime_state.get("issues") or []),
                "runtime_state": runtime_state,
            }

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
            if result.get("blocked_reason"):
                self._record_live_failure(
                    exchange_name or "",
                    result.get("blocked_reason"),
                    issues=result.get("reconciliation_issues", []),
                )
                return {
                    "blocked_reason": result.get("blocked_reason"),
                    "decision": result.get("decision") or decision,
                    "reconciliation_issues": result.get("reconciliation_issues", []),
                    "recovery_state": result.get("recovery_state"),
                }
            self._clear_live_failure_streak(exchange_name or "")
            return {"order": result, "decision": decision}
        self._record_live_failure(exchange_name or "", "execution_failed")
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
                    if isinstance(event.metadata, dict):
                        if event.metadata.get("resolution_result"):
                            trade["resolution_result"] = event.metadata.get("resolution_result")
                        if event.metadata.get("resolution_type"):
                            trade["resolution_type"] = event.metadata.get("resolution_type")
                        if event.metadata.get("exit_price") is not None:
                            trade["exit_price"] = event.metadata.get("exit_price")
                    fee_rate = getattr(getattr(self, "kelly", None), "fee_rate", None)
                    if fee_rate is None:
                        fee_rate = 0.07
                    enrich_trade_audit_fields(
                        trade,
                        fee_rate=float(fee_rate),
                    )
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
            "live_failure_streaks": {
                exchange: int(details.get("count", 0) or 0)
                for exchange, details in self.live_failure_streaks.items()
            },
            "reconciliation_gate": {
                exchange: {
                    "verdict": details.get("verdict", "unknown"),
                    "issues": list(details.get("issues") or []),
                    "reason": details.get("reason", ""),
                    "recovery_state": details.get("recovery_state", "requires_safe_reconciliation"),
                }
                for exchange, details in self.reconciliation_gate.items()
            },
            "live_runtime_state": {
                "state": self.live_runtime_state.get("state", "safe"),
                "reason": self.live_runtime_state.get("reason", ""),
                "issues": list(self.live_runtime_state.get("issues") or []),
                "recovery_state": self.live_runtime_state.get("recovery_state", "ready"),
            },
            "live_exchange_states": {
                exchange: {
                    "state": details.get("state", "safe"),
                    "reason": details.get("reason", ""),
                    "issues": list(details.get("issues") or []),
                    "recovery_state": details.get("recovery_state", "ready"),
                }
                for exchange, details in dict(self.live_runtime_state.get("exchange_states") or {}).items()
            },
            "safety_pause": self._safety_pause_status(),
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
            "reconciliation_failed": "reconciliation_events",
            "live_runtime_state_changed": "status_events",
            "live_safety_pause": "status_events",
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
