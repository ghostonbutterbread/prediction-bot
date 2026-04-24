"""Paper-mode state and execution adapters for the simulator."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from typing import Any, Callable, Protocol

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
from bot.trade_audit import calculate_contracts, enrich_trade_audit_fields, is_trade_effective_row, trade_event_key

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
            enrich_trade_audit_fields(trade_data)
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
        return {
            "session_id": self.host.session_id,
            "starting_balance": self.host.starting_balance,
            "balance": self.host.balance,
            "available_cash": self.host.available_cash,
            "reserved_capital": self.host.reserved_capital,
            "scan_count": self.host.scan_count,
            "max_entry_price": max_entry_price,
            "trades": [asdict(trade) for trade in self.host.trades],
            "report": report,
            "consecutive_daily_losses": consecutive_daily_losses,
            "last_loss_date": last_loss_date,
        }

    def save_session(self) -> Path:
        self.state_adapter.prune_ineffective_trades()
        for trade in self.host.trades:
            enrich_trade_audit_fields(trade.__dict__)
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
        if session_id:
            session_file = self.host.data_dir / f"sim_{session_id}.json"
            return session_file if session_file.exists() else None

        session_files = sorted(self.host.data_dir.glob("sim_*.json"), reverse=True)
        return session_files[0] if session_files else None


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

        resolver = TradeResolver(str(self.host.data_dir))
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
                    },
                )
            )
        return events
