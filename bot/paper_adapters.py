"""Paper-mode state and execution adapters for the simulator."""

from __future__ import annotations

import json
import logging
import fcntl
import hashlib
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from bot.shared_core import (
    AccountState,
    ExecutionResult,
    PaperSessionState,
    PositionState,
    ResolutionEvent,
    TradeContext,
    TradeDecision,
    build_execution_snapshot,
)
from bot.market_router import route_market
from bot.trade_audit import calculate_contracts, enrich_trade_audit_fields, is_trade_effective_row, trade_event_key
from bot.trade_audit import calculate_realized_accounting

logger = logging.getLogger(__name__)


class PaperSimulatorHost(Protocol):
    """Minimal simulator surface the paper adapters depend on."""

    session_id: str
    starting_balance: float
    balance: float
    available_cash: float
    reserved_capital: float
    scan_count: int
    traded_markets: set[str]
    data_dir: Path
    trades: list[Any]
    risk: Any
    kelly: Any
    config: dict[str, Any]


class PaperPersistenceHost(PaperSimulatorHost, Protocol):
    """Simulator surface needed for paper persistence and reloading."""

    max_entry_price: float
    consecutive_daily_losses: int
    last_loss_date: str | None

    def report(self) -> dict[str, Any]:
        ...

    def _hydrate_trade(self, t_data: dict[str, Any], index: int) -> Any:
        ...

    def _apply_loaded_session(self, loaded: "LoadedPaperSession") -> None:
        ...

    def _load_session(self, session_id: str | None = None) -> bool:
        ...


@dataclass(slots=True)
class LoadedPaperSession:
    """State reconstructed from a persisted paper session."""

    session_id: str
    starting_balance: float
    balance: float
    available_cash: float
    reserved_capital: float
    scan_count: int
    max_entry_price: float
    consecutive_daily_losses: int
    last_loss_date: str | None
    trades: list[Any]
    traded_markets: set[str]
    discarded_rows: int = 0


class SimulatorPaperSessionStore:
    """Paper-only persistence boundary for simulator session snapshots."""

    def __init__(self, host: PaperPersistenceHost, state_adapter: "SimulatorPaperStateAdapter"):
        self.host = host
        self.state_adapter = state_adapter

    def load_session(
        self,
        session_id: str | None,
        *,
        trade_factory: Callable[[dict[str, Any], int], Any],
        max_entry_price_default: float,
    ) -> LoadedPaperSession | None:
        session_file = self._resolve_session_file(session_id)
        if session_file is None:
            return None

        with open(session_file, encoding="utf-8") as handle:
            data = json.load(handle)

        loaded_session_id = data.get("session_id", datetime.now().strftime("%Y%m%d_%H%M%S"))
        trades: list[Any] = []
        discarded = 0
        for idx, raw_trade in enumerate(data.get("trades", []), start=1):
            trade_data = dict(raw_trade)
            enrich_trade_audit_fields(trade_data, fee_rate=self.host.kelly.fee_rate)
            if not self.state_adapter.is_trade_row_effective(trade_data):
                discarded += 1
                continue
            trades.append(trade_factory(trade_data, idx))

        balance = data.get("balance", data.get("starting_balance", 100.0))
        raw_reserved_capital = self.state_adapter.coerce_float_or_none(data.get("reserved_capital"))
        if raw_reserved_capital is None:
            raw_reserved_capital = round(
                sum(
                    self.state_adapter.trade_reserved_amount(trade)
                    for trade in trades
                    if not getattr(trade, "resolved", False)
                ),
                2,
            )

        raw_available_cash = self.state_adapter.coerce_float_or_none(data.get("available_cash"))
        if raw_available_cash is None:
            raw_available_cash = balance - raw_reserved_capital

        effective_trades = [trade for trade in trades if self.state_adapter.is_trade_effective(trade)]
        traded_markets = {
            getattr(trade, "market_id", "")
            for trade in effective_trades
            if getattr(trade, "market_id", "") and not getattr(trade, "resolved", False)
        }

        return LoadedPaperSession(
            session_id=loaded_session_id,
            starting_balance=data.get("starting_balance", 100.0),
            balance=balance,
            available_cash=round(raw_available_cash, 2),
            reserved_capital=round(raw_reserved_capital, 2),
            scan_count=data.get("scan_count", 0),
            max_entry_price=data.get("max_entry_price", max_entry_price_default),
            consecutive_daily_losses=data.get("consecutive_daily_losses", 0),
            last_loss_date=data.get("last_loss_date"),
            trades=trades,
            traded_markets=traded_markets,
            discarded_rows=discarded,
        )

    def build_session_payload(
        self,
        *,
        max_entry_price: float,
        consecutive_daily_losses: int,
        last_loss_date: str | None,
        report: dict[str, Any],
    ) -> dict[str, Any]:
        trade_rows = []
        for trade in self.host.trades:
            trade_row = asdict(trade)
            enrich_trade_audit_fields(trade_row, fee_rate=self.host.kelly.fee_rate)
            trade_rows.append(trade_row)
        return {
            "session_id": self.host.session_id,
            "starting_balance": self.host.starting_balance,
            "balance": self.host.balance,
            "available_cash": self.host.available_cash,
            "reserved_capital": self.host.reserved_capital,
            "scan_count": self.host.scan_count,
            "max_entry_price": max_entry_price,
            "trades": trade_rows,
            "report": report,
            "consecutive_daily_losses": consecutive_daily_losses,
            "last_loss_date": last_loss_date,
        }

    def save_session(self) -> Path:
        self.state_adapter.prune_ineffective_trades()
        for trade in self.host.trades:
            enrich_trade_audit_fields(trade.__dict__, fee_rate=self.host.kelly.fee_rate)
        self.state_adapter.refresh_capital_state()
        self.host.risk.sync_with_trades(
            self.host.trades,
            current_balance=self.host.balance,
            starting_balance=self.host.starting_balance,
            available_cash=self.host.available_cash,
            reserved_capital=self.host.reserved_capital,
        )

        session_file = self.host.data_dir / f"sim_{self.host.session_id}.json"
        data = self.build_session_payload(
            max_entry_price=self.host.max_entry_price,
            consecutive_daily_losses=self.host.consecutive_daily_losses,
            last_loss_date=self.host.last_loss_date,
            report=self.host.report(),
        )
        with open(session_file, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, default=str)
        return session_file

    def _resolve_session_file(self, session_id: str | None) -> Path | None:
        search_dirs = [self.host.data_dir]
        if self._allow_legacy_parent_session_lookup():
            search_dirs.append(self.host.data_dir.parent)

        if session_id:
            for directory in search_dirs:
                session_file = directory / f"sim_{session_id}.json"
                if session_file.exists():
                    return session_file
            return None

        session_files: list[Path] = []
        for directory in search_dirs:
            session_files.extend(directory.glob("sim_*.json"))
        session_files = sorted(session_files, key=lambda path: path.stat().st_mtime, reverse=True)
        return session_files[0] if session_files else None

    def _allow_legacy_parent_session_lookup(self) -> bool:
        if self.host.data_dir.name not in {"paper", "live"}:
            return False
        paper_wallets = getattr(self.host, "config", {}).get("paper_wallets") if isinstance(getattr(self.host, "config", {}), dict) else None
        if not isinstance(paper_wallets, dict):
            return True
        if paper_wallets.get("allow_legacy_parent_session_lookup") is True:
            return True
        return False


class SimulatorPaperStateAdapter:
    """Paper state boundary backed by simulator/session state."""

    def __init__(self, host: PaperSimulatorHost):
        self.host = host

    def get_account_state(self) -> AccountState:
        open_positions = sum(1 for trade in self.effective_trades() if not getattr(trade, "resolved", False))
        total_exposure = round(
            sum(self.trade_reserved_amount(trade) for trade in self.host.trades if not getattr(trade, "resolved", False)),
            2,
        )
        tradable_cash = round(self.host.available_cash, 2)
        if self.host.risk.max_tradable_balance and self.host.risk.max_tradable_balance > 0:
            tradable_cash = round(min(tradable_cash, self.host.risk.max_tradable_balance), 2)

        return AccountState(
            starting_balance=round(self.host.starting_balance, 2),
            current_balance=round(self.host.balance, 2),
            available_cash=round(self.host.available_cash, 2),
            reserved_capital=round(self.host.reserved_capital, 2),
            total_exposure=total_exposure,
            open_positions=open_positions,
            daily_pnl=round(self.host.risk.state.daily_pnl, 2),
            drawdown_pct=round(self.host.risk.state.drawdown_pct, 4),
            consecutive_losses=self.host.risk.state.consecutive_losses,
            consecutive_wins=self.host.risk.state.consecutive_wins,
            metadata={
                "mode": "paper",
                "session_id": self.host.session_id,
                "effective_tradable_cash": tradable_cash,
                "trading_enabled": self.host.risk.state.trading_enabled,
                "max_tradable_balance": round(self.host.risk.max_tradable_balance, 2),
                "max_position_size_usd": round(self.host.risk.max_position_size_usd, 2),
                "standby_active": self.host.risk.state.standby_active,
                "standby_reason_codes": list(self.host.risk.state.standby_reason_codes),
                "standby_blocked_scan_count": self.host.risk.state.standby_blocked_scan_count,
            },
        )

    def list_open_positions(self) -> list[PositionState]:
        positions: list[PositionState] = []
        for trade in self.effective_trades():
            if getattr(trade, "resolved", False):
                continue
            positions.append(
                PositionState(
                    position_id=getattr(trade, "id", ""),
                    market_id=getattr(trade, "market_id", ""),
                    question=getattr(trade, "question", ""),
                    direction=getattr(trade, "direction", ""),
                    opened_at=getattr(trade, "timestamp", ""),
                    status="open",
                    entry_price=self.coerce_float_or_none(getattr(trade, "market_price", None)),
                    position_size=round(self.coerce_float_or_none(getattr(trade, "position_size", None)) or 0.0, 2),
                    reserved_capital=round(self.trade_reserved_amount(trade), 2),
                    contracts=self.coerce_float_or_none(getattr(trade, "contracts", None)),
                    current_price=self.coerce_float_or_none(getattr(trade, "current_price", None)),
                    unrealized_pnl=self.coerce_float_or_none(getattr(trade, "unrealized_pnl", None)),
                    realized_pnl=self.coerce_float_or_none(getattr(trade, "net_pnl", None)),
                    metadata={
                        "exchange": getattr(trade, "exchange", ""),
                        "category": getattr(trade, "category", ""),
                        "event_key": getattr(trade, "event_key", "") or trade_event_key(getattr(trade, "__dict__", {})),
                    },
                )
            )
        return positions

    def get_paper_session_state(self) -> PaperSessionState:
        return PaperSessionState(
            session_id=self.host.session_id,
            scan_count=self.host.scan_count,
            traded_market_count=len(self.host.traded_markets),
            data_path=str(self.host.data_dir),
            metadata={
                "mode": "paper",
                "starting_balance": round(self.host.starting_balance, 2),
                "standby_active": self.host.risk.state.standby_active,
                "standby_reason_codes": list(self.host.risk.state.standby_reason_codes),
                "standby_blocked_scan_count": self.host.risk.state.standby_blocked_scan_count,
                "standby_entered_at": self.host.risk.state.standby_entered_at,
            },
        )

    def build_trade_context(self, signal: dict[str, Any]) -> TradeContext:
        return self.build_trade_context_from_snapshot(signal, execution_snapshot=None)

    def build_trade_context_from_snapshot(
        self,
        signal: dict[str, Any],
        *,
        execution_snapshot: dict[str, Any] | None,
    ) -> TradeContext:
        direction = str(signal.get("direction", "BUY_YES") or "BUY_YES").upper()
        market_price, yes_price, no_price = self._resolve_signal_prices(signal, direction)
        account_state = self.get_account_state()
        event_key = trade_event_key(signal)
        event_rows = self._same_event_trade_rows(event_key)
        event_exposure = round(sum(row["reserved_capital"] for row in event_rows), 2)
        filled_event_rows = [row for row in event_rows if row["lifecycle_state"] == "filled"]
        pending_event_rows = [row for row in event_rows if row["lifecycle_state"] != "filled"]
        current_balance = round(account_state.current_balance, 2)
        max_event_exposure = round(current_balance * self.host.risk.max_event_exposure_pct, 2)
        candidate_market_id = str(signal.get("market_id", "") or "")
        candidate_direction = direction
        candidate_family_key = self._market_family_key(candidate_market_id)
        same_market_rows = [row for row in event_rows if row["market_id"] == candidate_market_id]
        same_family_rows = [row for row in event_rows if row["family_key"] == candidate_family_key]
        event_entry_prices = [row["entry_price"] for row in same_market_rows if row["entry_price"] is not None]
        best_yes_ask, best_no_ask, best_yes_bid, best_no_bid = self._resolve_book_prices(signal, market_price, yes_price, no_price)
        snapshot = execution_snapshot
        if snapshot is None:
            snapshot = {
                "source": "signal",
                "direction": direction,
                "market_price": market_price,
                "yes_price": yes_price,
                "no_price": no_price,
                "best_yes_ask": best_yes_ask,
                "best_no_ask": best_no_ask,
                "best_yes_bid": best_yes_bid,
                "best_no_bid": best_no_bid,
                "estimated_fill_price": market_price,
            }
        market_price = snapshot.get("market_price")
        yes_price = snapshot.get("yes_price")
        no_price = snapshot.get("no_price")
        best_yes_ask = snapshot.get("best_yes_ask")
        best_no_ask = snapshot.get("best_no_ask")
        best_yes_bid = snapshot.get("best_yes_bid")
        best_no_bid = snapshot.get("best_no_bid")
        liquidity = self._resolve_signal_liquidity(signal)
        host_config = getattr(self.host, "config", {}) or {}
        market_route = route_market(signal.get("_market") or signal, host_config).to_dict()
        return TradeContext(
            exchange=str(signal.get("exchange", "unknown") or "unknown"),
            market_id=candidate_market_id,
            question=str(signal.get("question", "") or ""),
            direction=direction,
            market_price=market_price,
            yes_price=yes_price,
            no_price=no_price,
            model_probability=self.coerce_float_or_none(signal.get("model_probability")),
            edge=self.coerce_float_or_none(signal.get("edge")),
            confidence=self.coerce_float_or_none(signal.get("confidence")),
            account_state=account_state,
            source_context=dict(signal),
            metadata={
                "category": signal.get("category", ""),
                "market_route": market_route,
                "market_route_enforcement": "stable_required",
                "market_route_required": self._market_route_enforcement_enabled(host_config),
                "strategy_lanes": dict(host_config.get("strategy_lanes", {}) or {}),
                "strategy_policy_normalized": dict(host_config.get("strategy_policy_normalized", {}) or {}),
                "strategy_policy": dict(host_config.get("strategy_policy", {}) or {}),
                "event_key": event_key,
                "market_family_key": candidate_family_key,
                "event_snapshot": {
                    "event_key": event_key,
                    "candidate_family_key": candidate_family_key,
                    "event_position_count_before": len(event_rows),
                    "event_entries_count": len(event_rows),
                    "event_exposure_before": event_exposure,
                    "filled_event_exposure_before": round(sum(row["reserved_capital"] for row in filled_event_rows), 2),
                    "pending_event_exposure_before": round(sum(row["reserved_capital"] for row in pending_event_rows), 2),
                    "filled_event_position_count_before": len(filled_event_rows),
                    "pending_event_position_count_before": len(pending_event_rows),
                    "max_event_exposure_pct": self.host.risk.max_event_exposure_pct,
                    "held_market_ids": [row["market_id"] for row in event_rows],
                    "same_event_directions": [row["direction"] for row in event_rows],
                    "same_family_markets": [row["market_id"] for row in same_family_rows],
                    "same_family_positions": list(same_family_rows),
                    "opposite_side_detected": any(row["direction"] != candidate_direction for row in event_rows),
                    "event_entry_prices": event_entry_prices,
                    "best_same_market_entry_price": min(event_entry_prices) if event_entry_prices else None,
                    "best_same_family_entry_price": min((row["entry_price"] for row in same_family_rows if row["entry_price"] is not None), default=None),
                    "liquidity": liquidity,
                    "best_yes_ask": best_yes_ask,
                    "best_no_ask": best_no_ask,
                    "best_yes_bid": best_yes_bid,
                    "best_no_bid": best_no_bid,
                    "estimated_fill_price": snapshot.get("estimated_fill_price"),
                    "estimated_slippage": self._estimate_signal_slippage(signal, 0.0),
                    "execution_snapshot_source": snapshot.get("source"),
                },
                "retrade_policy": self._retrade_policy_metadata(),
            },
        )

    @staticmethod
    def _market_route_enforcement_enabled(config: dict[str, Any]) -> bool:
        """Market routing is stable route safety; policy flags do not disable it."""
        return True

    def effective_trades(self) -> list[Any]:
        return [trade for trade in self.host.trades if self.is_trade_effective(trade)]

    def prune_ineffective_trades(self) -> None:
        effective = self.effective_trades()
        if len(effective) != len(self.host.trades):
            logger.warning(
                "Pruned %s zero-sized or malformed trades from session %s",
                len(self.host.trades) - len(effective),
                self.host.session_id,
            )
            self.host.trades = effective
        self.refresh_traded_markets()

    def refresh_traded_markets(self) -> None:
        self.host.traded_markets = {
            getattr(trade, "market_id", "")
            for trade in self.effective_trades()
            if getattr(trade, "market_id", "") and not getattr(trade, "resolved", False)
        }

    def refresh_capital_state(self) -> None:
        self.host.reserved_capital = round(
            sum(self.trade_reserved_amount(trade) for trade in self.host.trades if not getattr(trade, "resolved", False)),
            2,
        )
        self.host.available_cash = round(self.host.balance - self.host.reserved_capital, 2)

    def is_trade_effective(self, trade: Any) -> bool:
        try:
            trade_data = asdict(trade)
        except TypeError:
            trade_data = dict(trade)
        return self.is_trade_row_effective(trade_data)

    @staticmethod
    def is_trade_row_effective(trade_data: dict[str, Any]) -> bool:
        return is_trade_effective_row(trade_data)

    @staticmethod
    def coerce_float_or_none(value: Any) -> float | None:
        try:
            if value is None:
                return None
            value = float(value)
        except (TypeError, ValueError):
            return None
        return value if isfinite(value) else None

    def trade_reserved_amount(self, trade: Any) -> float:
        reserved = self.coerce_float_or_none(getattr(trade, "reserved_capital", None))
        if reserved is not None and reserved >= 0:
            return round(reserved, 2)
        size = self.coerce_float_or_none(getattr(trade, "position_size", None))
        if size is not None and size > 0:
            return round(size, 2)
        return 0.0

    def _same_event_trade_rows(self, event_key: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for trade in self.effective_trades():
            if getattr(trade, "resolved", False):
                continue
            trade_row = {
                "event_key": trade_event_key(getattr(trade, "__dict__", {})),
                "market_id": getattr(trade, "market_id", ""),
                "direction": str(getattr(trade, "direction", "BUY_YES") or "BUY_YES").upper(),
                "reserved_capital": self.trade_reserved_amount(trade),
                "entry_price": self.coerce_float_or_none(getattr(trade, "market_price", None)),
                "lifecycle_state": "filled",
            }
            trade_row["family_key"] = self._market_family_key(trade_row["market_id"])
            if trade_row["event_key"] == event_key:
                rows.append(trade_row)
        return rows

    def _retrade_policy_metadata(self) -> dict[str, Any]:
        return {
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
        }

    def _estimate_signal_slippage(self, signal: dict[str, Any], size: float) -> float:
        liquidity = self._resolve_signal_liquidity(signal)
        if not liquidity or liquidity <= 0 or size <= 0:
            return 0.0
        return round(min((size / max(liquidity, 1.0)) * 0.10, 0.03), 6)

    def _resolve_signal_liquidity(self, signal: dict[str, Any]) -> float | None:
        market = signal.get("_market")
        liquidity = self.coerce_float_or_none(signal.get("liquidity"))
        if liquidity is None and market:
            liquidity = self.coerce_float_or_none(getattr(market, "liquidity", None))
        return liquidity

    def _resolve_book_prices(
        self,
        signal: dict[str, Any],
        market_price: float | None,
        yes_price: float | None,
        no_price: float | None,
    ) -> tuple[float | None, float | None, float | None, float | None]:
        market = signal.get("_market")
        best_yes_ask = self.coerce_float_or_none(signal.get("best_yes_ask"))
        best_no_ask = self.coerce_float_or_none(signal.get("best_no_ask"))
        best_yes_bid = self.coerce_float_or_none(signal.get("best_yes_bid"))
        best_no_bid = self.coerce_float_or_none(signal.get("best_no_bid"))
        if market:
            best_yes_ask = best_yes_ask if best_yes_ask is not None else self.coerce_float_or_none(getattr(market, "yes_price", None))
            best_no_ask = best_no_ask if best_no_ask is not None else self.coerce_float_or_none(getattr(market, "no_price", None))
        if best_yes_ask is None:
            best_yes_ask = yes_price if yes_price is not None else market_price
        if best_no_ask is None:
            best_no_ask = no_price if no_price is not None else (round(1 - best_yes_ask, 4) if best_yes_ask is not None else None)
        if best_yes_bid is None and best_yes_ask is not None:
            best_yes_bid = max(0.0, round(best_yes_ask - 0.01, 4))
        if best_no_bid is None and best_no_ask is not None:
            best_no_bid = max(0.0, round(best_no_ask - 0.01, 4))
        return best_yes_ask, best_no_ask, best_yes_bid, best_no_bid

    @staticmethod
    def _market_family_key(market_id: str) -> str:
        market_id = str(market_id or "")
        if not market_id:
            return ""
        parts = market_id.split("-")
        return "-".join(parts[:-1]) if len(parts) > 1 else market_id

    def _resolve_signal_prices(
        self,
        signal: dict[str, Any],
        direction: str,
    ) -> tuple[float | None, float | None, float | None]:
        market = signal.get("_market")
        raw_market_price = self.coerce_float_or_none(signal.get("market_price"))
        explicit_yes_price = self.coerce_float_or_none(signal.get("yes_price"))
        explicit_no_price = self.coerce_float_or_none(signal.get("no_price"))
        if explicit_no_price is None:
            explicit_no_price = self.coerce_float_or_none(signal.get("no_market_price"))

        market_yes_price = self.coerce_float_or_none(getattr(market, "yes_price", None)) if market else None
        market_no_price = self.coerce_float_or_none(getattr(market, "no_price", None)) if market else None

        yes_price = explicit_yes_price if explicit_yes_price is not None else market_yes_price
        no_price = explicit_no_price if explicit_no_price is not None else market_no_price

        if direction == "BUY_NO":
            if no_price is None and raw_market_price is not None and explicit_yes_price is None and market_yes_price is None:
                no_price = raw_market_price
            if yes_price is None and no_price is not None:
                yes_price = round(1 - no_price, 4)
            if no_price is None and yes_price is not None:
                no_price = round(1 - yes_price, 4)
            market_price = no_price if no_price is not None else raw_market_price
            return market_price, yes_price, no_price

        if yes_price is None:
            yes_price = raw_market_price
        if no_price is None and yes_price is not None:
            no_price = round(1 - yes_price, 4)
        if yes_price is None and no_price is not None:
            yes_price = round(1 - no_price, 4)
        market_price = yes_price if yes_price is not None else raw_market_price
        return market_price, yes_price, no_price


class SimulatorPaperExecutionAdapter:
    """Paper execution boundary that simulates fills and reserves cash."""

    def __init__(self, host: PaperSimulatorHost):
        self.host = host

    def execute(self, decision: TradeDecision, context: TradeContext) -> ExecutionResult:
        route = context.metadata.get("market_route") if isinstance(context.metadata, dict) else None
        if not isinstance(route, dict) or not route.get("allowed") or not route.get("handler_id"):
            reason_code = str((route or {}).get("reason_code") or "missing_market_route")
            return ExecutionResult(
                accepted=False,
                action=decision.action,
                status="rejected",
                message=f"Market route rejected: {reason_code}",
                requested_size=decision.requested_position_size,
                metadata={"reason_code": reason_code, "market_route": route},
            )

        if not decision.approved:
            return ExecutionResult(
                accepted=False,
                action=decision.action,
                status="rejected",
                message=decision.reason,
                requested_size=decision.requested_position_size,
                metadata={"reason_code": decision.reason_code},
            )

        size = round(float(decision.position_size or 0.0), 2)
        if size <= 0:
            return ExecutionResult(
                accepted=False,
                action=decision.action,
                status="rejected",
                message="Paper execution received non-positive size",
                requested_size=decision.requested_position_size,
            )

        source_signal = dict(context.source_context or {})
        trade_id = f"sim_{self.host.session_id}_{len(self.host.trades) + 1:04d}"
        available_cash_before = round(self.host.available_cash, 2)
        fill_price = self.apply_fill_slippage(
            float(decision.entry_price or 0.0),
            size,
            source_signal,
            decision.action,
        )
        reserved_delta = round(size, 2)
        available_cash_after = round(available_cash_before - reserved_delta, 2)
        timestamp = datetime.now(timezone.utc).isoformat()
        category = self._derive_category(source_signal)
        contracts = round(calculate_contracts(fill_price, size), 4)

        self.host.risk.record_trade(
            {
                "id": trade_id,
                "question": context.question,
                "direction": decision.action,
                "position_size": size,
                "reserved_capital": reserved_delta,
                "market_price": fill_price,
            }
        )

        self.host.available_cash = available_cash_after
        self.host.reserved_capital = round(self.host.reserved_capital + reserved_delta, 2)

        return ExecutionResult(
            accepted=True,
            action=decision.action,
            status="filled",
            message="Paper trade filled",
            trade_id=trade_id,
            requested_size=decision.requested_position_size,
            filled_size=size,
            remaining_size=0.0,
            fill_price=fill_price,
            reserved_capital_delta=reserved_delta,
            available_cash_after=available_cash_after,
            metadata={
                "timestamp": timestamp,
                "exchange": context.exchange,
                "market_id": context.market_id,
                "question": context.question,
                "model_probability": round(float(decision.win_probability or 0.0), 4),
                "edge": source_signal.get("edge", 0),
                "confidence": source_signal.get("confidence", 0),
                "signals": source_signal.get("signals", {}),
                "decision_trace": decision.reasoning,
                "market_route": context.metadata.get("market_route"),
                "parity_mode_enabled": bool((decision.reasoning or {}).get("parity_mode", {}).get("enabled", False)),
                "execution_revalidated": bool((decision.reasoning or {}).get("parity_mode", {}).get("execution_revalidated", False)),
                "execution_revalidation_outcome": (decision.reasoning or {}).get("parity_mode", {}).get("execution_revalidation_outcome"),
                "original_signal_snapshot": (decision.reasoning or {}).get("parity_mode", {}).get("original_signal_snapshot"),
                "execution_snapshot": (decision.reasoning or {}).get("parity_mode", {}).get("execution_snapshot"),
                "original_decision_reason_code": (decision.reasoning or {}).get("parity_mode", {}).get("original_decision_reason_code"),
                "execution_decision_reason_code": (decision.reasoning or {}).get("parity_mode", {}).get("execution_decision_reason_code"),
                "execution_snapshot_source": (decision.reasoning or {}).get("parity_mode", {}).get("execution_snapshot_source"),
                "category": category,
                "contracts": contracts,
                "reserved_capital": reserved_delta,
                "available_cash_before": available_cash_before,
                "available_cash_after_entry": available_cash_after,
                "event_key": context.metadata.get("event_key", ""),
                "retrade": bool((decision.reasoning or {}).get("retrade", {}).get("enabled", False)),
                "event_position_count_before": (decision.reasoning or {}).get("retrade", {}).get("event_position_count_before", 0),
            },
        )

    def apply_fill_slippage(self, entry_price: float, size: float, signal: dict[str, Any], direction: str) -> float:
        market = signal.get("_market")
        liquidity = getattr(market, "liquidity", 0) if market else 0

        if liquidity <= 0 or size <= 0:
            return round(entry_price, 4)

        consumption_pct = size / max(liquidity, 1.0)
        if consumption_pct < 0.05:
            return round(entry_price, 4)

        slippage = min(consumption_pct * 0.10, 0.03)
        fill_price = min(entry_price + slippage, 0.99)
        if fill_price != entry_price:
            logger.debug(
                "  📉 Slippage: %.3f → %.3f (size=$%.2f, liquidity=$%.2f, consumption=%.1f%%, side=%s)",
                entry_price,
                fill_price,
                size,
                liquidity,
                consumption_pct * 100,
                direction,
            )
        return round(fill_price, 4)

    @staticmethod
    def _derive_category(signal: dict[str, Any]) -> str:
        raw_category = str(signal.get("category", "") or "")
        if raw_category:
            return raw_category

        market_id = str(signal.get("market_id", "") or "")
        parts = market_id.split("-")
        return parts[0] if len(parts) >= 2 else ""


class SimulatorPaperResolutionAdapter:
    """Paper resolution boundary backed by TradeResolver + persisted sessions."""

    def __init__(
        self,
        host: PaperPersistenceHost,
        state_adapter: SimulatorPaperStateAdapter,
        session_store: SimulatorPaperSessionStore,
    ):
        self.host = host
        self.state_adapter = state_adapter
        self.session_store = session_store
        self.last_summary: dict[str, Any] = {}

    def resolve_open_positions(self, settlement_source: Any | None = None) -> list[ResolutionEvent]:
        if (self.host.data_dir / "composed_binding.json").exists():
            raise ValueError("composed paper positions require exact resolution; generic market resolver is unsafe")
        open_positions = {
            getattr(trade, "id", ""): trade
            for trade in self.state_adapter.effective_trades()
            if not getattr(trade, "resolved", False) and getattr(trade, "id", "")
        }
        if not open_positions or settlement_source is None:
            self.last_summary = {
                "session_id": self.host.session_id,
                "resolved_this_pass": 0,
                "still_open": len(open_positions),
                "total_trades": len(self.host.trades),
            }
            return []

        self.session_store.save_session()

        from bot.resolver import TradeResolver

        resolver = TradeResolver(str(self.host.data_dir), fee_rate=self.host.kelly.fee_rate)
        self.last_summary = resolver.resolve_session(
            self.host.session_id,
            settlement_source,
            self.host.risk,
        )

        loaded = self.session_store.load_session(
            self.host.session_id,
            trade_factory=self.host._hydrate_trade,
            max_entry_price_default=self.host.max_entry_price,
        )
        if loaded is None:
            return []

        self.host._apply_loaded_session(loaded)

        events: list[ResolutionEvent] = []
        for trade in self.host.trades:
            trade_id = getattr(trade, "id", "")
            if trade_id not in open_positions or not getattr(trade, "resolved", False):
                continue
            events.append(
                ResolutionEvent(
                    position_id=trade_id,
                    market_id=getattr(trade, "market_id", ""),
                    outcome=str(getattr(trade, "outcome", "") or ""),
                    status=str(getattr(trade, "resolution_type", "settled") or "settled"),
                    resolved_at=str(getattr(trade, "resolved_at", "") or ""),
                    pnl=self.state_adapter.coerce_float_or_none(getattr(trade, "pnl", None)),
                    settlement_value=self.state_adapter.coerce_float_or_none(
                        getattr(trade, "settlement_value", None)
                    ),
                    metadata={
                        "direction": getattr(trade, "direction", ""),
                        "question": getattr(trade, "question", ""),
                        "integrity_status": getattr(trade, "integrity_status", ""),
                        "settlement_ts": getattr(trade, "settlement_ts", None),
                        "outcome_known_at": getattr(trade, "outcome_known_at", None),
                    },
                )
            )
        return events

    def resolve_configured_composed_positions(
        self, *, intents: Sequence[Mapping[str, Any]], feed_config: Mapping[str, Any],
        output_dir: str | Path, as_of: str | datetime,
    ) -> list[ResolutionEvent]:
        """Configured-feed paper entry point; never take an outcome from a decision.

        No network refresh or account enrollment occurs here. A separately
        owned resolution feed must already have published its latest state.
        """
        from bot.composed_resolution_binding import bind_composed_finalized_outcomes
        from bot.resolution_feed import normalize_resolution_feed_config

        if getattr(self.host, "runtime_mode", None) != "paper" or not (self.host.data_dir / "composed_binding.json").is_file():
            raise ValueError("configured settlement requires a bound composed paper account")
        target = Path(output_dir).resolve()
        if target == self.host.data_dir or self.host.data_dir in target.parents:
            raise ValueError("resolution binding output must be separate from paper account")
        feed = normalize_resolution_feed_config(feed_config)
        source = Path(feed["central_output_dir"]) / "latest_resolutions.jsonl"
        artifacts = bind_composed_finalized_outcomes(
            intents=intents, strict_resolutions_path=source,
            output_dir=target, feed_config=feed_config,
        )
        return self.resolve_exact_positions(
            binding_path=artifacts.bindings_path,
            strict_resolutions_path=artifacts.frozen_resolutions_path, as_of=as_of,
        )

    def resolve_exact_positions(
        self, *, binding_path: str | Path, strict_resolutions_path: str | Path, as_of: str | datetime,
    ) -> list[ResolutionEvent]:
        """Settle only exact composed positions backed by verified strict source bytes.

        This is an explicit paper-only call; no market API or generic resolver is used.
        An interrupted dual-file commit leaves a durable unsafe marker, which
        fences subsequent paper entries until an operator reconciles both files.
        """
        if getattr(self.host, "runtime_mode", None) != "paper" or not (self.host.data_dir / "composed_binding.json").is_file():
            raise ValueError("exact settlement requires a bound composed paper account")
        cutoff = _exact_timestamp(as_of)
        unsafe = self.host.data_dir / "composed_settlement_unsafe.json"
        with (self.host.data_dir / "composed.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if unsafe.exists():
                raise ValueError("composed settlement unsafe: reconcile session and risk state manually")
            # The lock serializes writers, but does not refresh an already
            # constructed Simulator. Refuse a stale instance before reading
            # evidence or creating a recovery marker; otherwise a second
            # writer can erase the first writer's settled P&L and latched halt.
            try:
                disk_risk = json.loads(self.host.risk.data_path.read_text())
                disk_session = json.loads((self.host.data_dir / f"sim_{self.host.session_id}.json").read_text())
                expected_session = self.session_store.build_session_payload(
                    max_entry_price=self.host.max_entry_price,
                    consecutive_daily_losses=self.host.consecutive_daily_losses,
                    last_loss_date=self.host.last_loss_date, report={},
                )
                disk_session.pop("report", None)
                expected_session.pop("report", None)
                if disk_risk != asdict(self.host.risk.state) or disk_session != expected_session:
                    raise ValueError("stale composed account state; reload before settlement")
            except (OSError, TypeError, json.JSONDecodeError) as exc:
                raise ValueError("stale composed account state; reload before settlement") from exc
            trades = self.state_adapter.effective_trades()
            ids = [trade.id for trade in trades]
            if any(not isinstance(trade_id, str) or not trade_id for trade_id in ids) or len(ids) != len(set(ids)):
                raise ValueError("duplicate or missing composed trade id")
            risk_rows = self.host.risk.state.trade_history
            if not all(isinstance(row, dict) for row in risk_rows):
                raise ValueError("composed risk trade records disagree with session")
            risk_by_id = {row.get("trade_id"): row for row in risk_rows}
            if (len(risk_by_id) != len(risk_rows) or set(risk_by_id) != set(ids)
                    or any(risk_by_id[trade.id].get("resolved") is not trade.resolved for trade in trades)):
                raise ValueError("composed risk trade records disagree with session")
            source_path = Path(strict_resolutions_path).resolve(strict=True)
            bindings = Path(binding_path).resolve(strict=True)
            if source_path == bindings or self.host.data_dir in source_path.parents or self.host.data_dir in bindings.parents:
                raise ValueError("resolution evidence must be separate from the paper account")
            receipt_rows = []
            with bindings.open("rb") as stream:
                for line in stream:
                    try:
                        receipt = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise ValueError("invalid exact binding row") from exc
                    if not isinstance(receipt, dict):
                        raise ValueError("invalid exact binding row")
                    receipt_rows.append(receipt)

            needed_lines = {receipt.get("strict_resolution_source_line_number") for receipt in receipt_rows
                            if type(receipt.get("strict_resolution_source_line_number")) is int}
            open_markets = {trade.market_id for trade in self.state_adapter.effective_trades() if not trade.resolved}
            source_rows: dict[int, tuple[dict[str, Any], str]] = {}
            market_counts: dict[str, int] = {}
            digest = hashlib.sha256()
            source_length = 0
            with source_path.open("rb") as stream:
                source_stat = os.fstat(stream.fileno())
                for line_number, line in enumerate(stream, start=1):
                    source_length = line_number
                    digest.update(line)
                    try:
                        row = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise ValueError("invalid full strict resolution source") from exc
                    if not isinstance(row, dict):
                        raise ValueError("invalid strict resolution source row")
                    market = row.get("market_id")
                    if isinstance(market, str) and market in open_markets:
                        market_counts[market] = market_counts.get(market, 0) + 1
                    if line_number in needed_lines:
                        source_rows[line_number] = (row, hashlib.sha256(line.rstrip(b"\r\n")).hexdigest())
                if not _same_source_stat(source_stat, os.fstat(stream.fileno())):
                    raise ValueError("strict resolution source changed during read")
            if not _same_source_stat(source_stat, source_path.stat()):
                raise ValueError("strict resolution source changed during read")
            source_hash = digest.hexdigest()

            by_identity: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
            for receipt in receipt_rows:
                key = tuple(receipt.get(field) for field in _EXACT_IDENTITY_FIELDS)
                if any(not isinstance(value, str) or not value for value in key):
                    raise ValueError("incomplete exact resolution identity")
                by_identity.setdefault(key, []).append(receipt)

            pending: list[tuple[Any, dict[str, Any], str, datetime, datetime]] = []
            for trade in self.state_adapter.effective_trades():
                if trade.resolved:
                    continue
                signals = trade.signals if isinstance(trade.signals, dict) else {}
                key = (signals.get("composed_decision_id"), signals.get("shared_candidate_id"),
                       signals.get("run_id"), trade.market_id)
                matches = by_identity.get(key, [])
                if not matches or len(matches) != 1 or market_counts.get(trade.market_id) != 1:
                    continue
                receipt = matches[0]
                try:
                    line_number = receipt["strict_resolution_source_line_number"]
                    if type(line_number) is not int or not 1 <= line_number <= source_length:
                        continue
                    row, raw_hash = source_rows[line_number]
                    if receipt.get("strict_resolution_source_sha256") != source_hash or receipt.get("raw_resolution_row_sha256") != raw_hash:
                        continue
                    if any(prior.resolved and prior.market_id == trade.market_id
                           and (prior.resolution_row_sha256 != raw_hash or prior.outcome != receipt.get("outcome"))
                           for prior in self.host.trades):
                        continue
                    if (row.get("market_id") != trade.market_id or row.get("requested_market_id") != trade.market_id
                            or row.get("returned_market_id") != trade.market_id or not row.get("resolution_id")):
                        continue
                    outcome = _exact_outcome(row, receipt)
                    settled = _exact_clock(row, receipt, "settlement_ts", required=True)
                    known = _exact_clock(row, receipt, "outcome_known_at", required=False) or settled
                    if settled <= _exact_timestamp(trade.timestamp) or max(settled, known) > cutoff:
                        continue
                    price = self.state_adapter.coerce_float_or_none(trade.market_price)
                    size = self.state_adapter.coerce_float_or_none(trade.position_size)
                    reserved = self.state_adapter.trade_reserved_amount(trade)
                    if (trade.direction not in {"BUY_YES", "BUY_NO"} or price is None or not 0 < price < 1
                            or size is None or size <= 0 or reserved <= 0 or not isfinite(size / price)
                            or self.host.risk._find_trade_record(trade.id) is None
                            or self.host.risk._find_trade_record(trade.id).get("resolved")):
                        continue
                    pending.append((trade, receipt, outcome, settled, known))
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue

            if not pending:
                return []
            # Mutable latest-feed paths must not be booked if they changed
            # between the streamed evidence read and the accounting preflight.
            if source_hash != _stream_sha256(source_path):
                raise ValueError("strict resolution source changed before accounting")
            session_path = self.host.data_dir / f"sim_{self.host.session_id}.json"
            preimage_dir = Path(tempfile.mkdtemp(prefix="composed_preimage_", dir=self.host.data_dir))
            backups = {}
            for original in (self.host.risk.data_path, session_path):
                if not original.is_file() or original.parent != self.host.data_dir:
                    raise ValueError("composed settlement requires saved risk and session state")
                backup = preimage_dir / original.name
                with original.open("rb") as reader, backup.open("xb") as writer:
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
                    writer.flush()
                    os.fsync(writer.fileno())
                backups[original.name] = _stream_sha256(backup)
            _fsync_directory(preimage_dir)
            _fsync_directory(self.host.data_dir)
            with unsafe.open("x", encoding="utf-8") as handle:
                json.dump({"status": "unsafe_until_dual_write_verified", "trade_ids": [t.id for t, *_ in pending],
                           "preimage_dir": preimage_dir.name, "preimage_sha256": backups}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_directory(self.host.data_dir)
            events: list[ResolutionEvent] = []
            # Do not remove the unsafe marker on any failure: even a partial
            # write or an exception in risk's swallowed save must remain fenced.
            for trade, receipt, outcome, settled, known in pending:
                reserved = self.state_adapter.trade_reserved_amount(trade)
                accounting = calculate_realized_accounting(
                    trade.direction, trade.market_price, trade.position_size, outcome,
                    fee_rate=trade.fee_rate if trade.fee_rate is not None else self.host.kelly.fee_rate,
                )
                pnl = round(accounting["net_pnl"], 4)
                self.host.risk.record_outcome(trade.id, pnl, void=outcome == "VOID", suppress_alert=True)
                trade.resolved = True
                trade.outcome = outcome
                trade.pnl = None if outcome == "VOID" else pnl
                trade.resolution_type = "void_resolution" if outcome == "VOID" else "settled"
                trade.resolved_at = datetime.now(timezone.utc).isoformat()
                trade.settlement_ts = settled.isoformat()
                trade.outcome_known_at = known.isoformat() if known != settled else None
                trade.resolution_identity = dict(zip(_EXACT_IDENTITY_FIELDS, (
                    trade.signals["composed_decision_id"], trade.signals["shared_candidate_id"],
                    trade.signals["run_id"], trade.market_id,
                )))
                trade.resolution_row_sha256 = receipt["raw_resolution_row_sha256"]
                trade.resolution_source_sha256 = source_hash
                trade.resolution_source_path = str(source_path)
                trade.resolution_source_line_number = receipt["strict_resolution_source_line_number"]
                trade.exit_price = None if outcome == "VOID" else (1.0 if outcome == "YES" else 0.0)
                trade.settlement_value = round(reserved + pnl, 4)
                self.host.balance = round(self.host.balance + pnl, 2)
                events.append(ResolutionEvent(
                    position_id=trade.id, market_id=trade.market_id, outcome=outcome,
                    status=trade.resolution_type, resolved_at=trade.resolved_at,
                    pnl=None if outcome == "VOID" else pnl,
                    settlement_value=trade.settlement_value,
                    metadata={"settlement_ts": trade.settlement_ts, "outcome_known_at": trade.outcome_known_at,
                              "resolution_row_sha256": trade.resolution_row_sha256},
                ))
            self.session_store.save_session()
            saved = json.loads((self.host.data_dir / f"sim_{self.host.session_id}.json").read_text())
            risk_saved = json.loads(self.host.risk.data_path.read_text())
            if (source_hash != _stream_sha256(source_path)
                    or {row["id"] for row in saved.get("trades", []) if row.get("resolved")} < {t.id for t, *_ in pending}
                    or saved.get("balance") != self.host.balance
                    or saved.get("reserved_capital") != self.host.reserved_capital
                    or risk_saved != asdict(self.host.risk.state)):
                raise RuntimeError("composed settlement dual-write verification failed; account is unsafe")
            for path in (self.host.risk.data_path, self.host.data_dir / f"sim_{self.host.session_id}.json"):
                with path.open("rb") as handle:
                    os.fsync(handle.fileno())
            _fsync_directory(self.host.data_dir)
            unsafe.unlink()
            _fsync_directory(self.host.data_dir)
            shutil.rmtree(preimage_dir)
            return events

    def recover_interrupted_exact_settlement(self, *, acknowledge_no_external_writes: bool = False) -> None:
        """Rollback the account pair explicitly; use standalone API if construction fails."""
        recover_interrupted_composed_paper_account(
            paper_root=self.host.data_dir,
            acknowledge_no_external_writes=acknowledge_no_external_writes,
        )
        self.host.risk._load_state()
        if not self.host._load_session(self.host.session_id):
            raise RuntimeError("composed recovery restored files but Simulator reload failed")


def recover_interrupted_composed_paper_account(
    *, paper_root: str | Path, acknowledge_no_external_writes: bool = False,
) -> None:
    """Offline rollback of a fenced bound PAPER account, without constructing Simulator.

    This is deliberately manual. Confirm exclusive account ownership and no
    external writes before acknowledging; a torn JSON risk/session file may
    prevent normal Simulator construction. The saved preimages are immutable
    inputs to this recovery, never fabricated from partially settled state.
    """
    if not acknowledge_no_external_writes:
        raise ValueError("explicit no-external-writes acknowledgement required for composed recovery")
    root = Path(paper_root)
    if (root.is_symlink() or not root.is_dir()
            or not (root / "composed_binding.json").is_file()):
        raise ValueError("recovery requires bound composed paper account")
    binding = json.loads((root / "composed_binding.json").read_text())
    if (not isinstance(binding, dict) or binding.get("schema_name") != "persistent_composed_paper_binding"
            or binding.get("paper_only") is not True):
        raise ValueError("recovery requires bound composed paper account")
    unsafe = root / "composed_settlement_unsafe.json"
    with (root / "composed.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        marker = json.loads(unsafe.read_text())
        name = marker.get("preimage_dir")
        expected = marker.get("preimage_sha256")
        sessions = list(root.glob("sim_*.json"))
        if (len(sessions) != 1 or not isinstance(name, str)
                or not name.startswith("composed_preimage_") or Path(name).name != name
                or not isinstance(expected, dict)
                or set(expected) != {"risk_state.json", sessions[0].name}):
            raise ValueError("unsafe marker has no verified composed recovery preimage")
        preimage_dir = root / name
        if not preimage_dir.is_dir() or preimage_dir.is_symlink():
            raise ValueError("composed recovery preimage is unavailable")
        for filename, digest in expected.items():
            backup = preimage_dir / filename
            if (backup.is_symlink() or not backup.is_file() or not isinstance(digest, str)
                    or _stream_sha256(backup) != digest):
                raise ValueError("composed recovery preimage hash mismatch")
        # Each replace is atomic. If interrupted between them, the durable
        # marker remains and the same preimages make recovery retryable.
        for filename in ("risk_state.json", sessions[0].name):
            path = root / filename
            descriptor, temporary = tempfile.mkstemp(prefix=filename + ".restore.", dir=root)
            try:
                with os.fdopen(descriptor, "wb") as writer, (preimage_dir / filename).open("rb") as reader:
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
                    writer.flush()
                    os.fsync(writer.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        _fsync_directory(root)
        if any(_stream_sha256(root / filename) != digest for filename, digest in expected.items()):
            raise RuntimeError("composed recovery verification failed; unsafe marker retained")
        risk = json.loads((root / "risk_state.json").read_text())
        session = json.loads(sessions[0].read_text())
        if (not isinstance(risk, dict) or not isinstance(session, dict)
                or session.get("session_id") != sessions[0].stem.removeprefix("sim_")
                or not isinstance(session.get("trades"), list)
                or not isinstance(risk.get("trade_history"), list)
                or risk.get("current_balance") != session.get("balance")
                or risk.get("reserved_capital") != session.get("reserved_capital")):
            raise ValueError("composed recovery preimages disagree; unsafe marker retained")
        unsafe.unlink()
        _fsync_directory(root)
        shutil.rmtree(preimage_dir)


_EXACT_IDENTITY_FIELDS = ("composed_decision_id", "shared_candidate_id", "run_id", "market_id")


def _same_source_stat(first: os.stat_result, second: os.stat_result) -> bool:
    return all(getattr(first, name) == getattr(second, name)
               for name in ("st_dev", "st_ino", "st_size", "st_mtime_ns"))


def _stream_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _exact_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("exact settlement requires timezone-aware timestamps")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("exact settlement requires timezone-aware timestamps")
    return parsed.astimezone(timezone.utc)


def _exact_assertions(row: dict[str, Any], field: str) -> list[Any]:
    values = []
    for container in (row, row.get("resolution"), row.get("market_outcome"), row.get("metadata")):
        if isinstance(container, dict):
            for alias in ({"outcome": ("outcome", "official_outcome", "kalshi_result", "result", "settlement_value", "market_result", "winning_side"),
                           "status": ("market_status", "status", "resolution_status"),
                           "settlement_ts": ("settlement_ts",),
                           "outcome_known_at": ("outcome_known_at",)}[field]):
                if alias in container and container[alias] not in (None, ""):
                    values.append(container[alias])
    return values


def _exact_outcome(source: dict[str, Any], binding: dict[str, Any]) -> str:
    source_outcomes = _exact_assertions(source, "outcome")
    binding_outcomes = _exact_assertions(binding, "outcome")
    statuses = _exact_assertions(source, "status") + _exact_assertions(binding, "status")
    if (not source_outcomes or not binding_outcomes
            or any(str(value).lower() != "finalized" for value in statuses)
            or not statuses or str(source.get("market_status") or "").lower() != "finalized"):
        raise ValueError("nonfinal or missing authoritative outcome")
    normalized = {str(value).upper() for value in source_outcomes + binding_outcomes}
    if len(normalized) != 1 or not normalized <= {"YES", "NO", "VOID"}:
        raise ValueError("contradictory resolution outcome")
    return normalized.pop()


def _exact_clock(source: dict[str, Any], binding: dict[str, Any], field: str, *, required: bool) -> datetime | None:
    source_values = _exact_assertions(source, field)
    binding_values = _exact_assertions(binding, field)
    if required and (not source_values or not binding_values):
        raise ValueError("missing authoritative settlement clock")
    if binding_values and not source_values:
        raise ValueError("binding supplied unverified outcome clock")
    values = [_exact_timestamp(value) for value in source_values + binding_values]
    if len(set(values)) > 1:
        raise ValueError("contradictory resolution clock")
    return values[0] if values else None
