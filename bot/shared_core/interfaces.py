"""Shared core dataclasses and adapter protocols.

The shared core should answer "should we trade?".
Adapters should answer "what does this environment currently look like?" and
"how do we execute in this environment?".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable


@dataclass(slots=True)
class AccountState:
    """Mode-agnostic account snapshot used by the shared decision path."""

    starting_balance: float
    current_balance: float
    available_cash: float
    reserved_capital: float
    total_exposure: float
    open_positions: int
    daily_pnl: float = 0.0
    drawdown_pct: float = 0.0
    consecutive_losses: int = 0
    consecutive_wins: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PositionState:
    """Mode-agnostic open position snapshot exposed by a state adapter."""

    position_id: str
    market_id: str
    question: str
    direction: str
    opened_at: str
    status: str = "open"
    entry_price: float | None = None
    position_size: float = 0.0
    reserved_capital: float = 0.0
    contracts: float | None = None
    current_price: float | None = None
    unrealized_pnl: float | None = None
    realized_pnl: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PaperSessionState:
    """Paper-mode session metadata that should not leak into shared logic."""

    session_id: str
    scan_count: int = 0
    traded_market_count: int = 0
    data_path: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OrderState:
    """Exchange-facing order lifecycle snapshot, mainly for live adapters."""

    order_id: str
    market_id: str
    direction: str
    status: str
    requested_size: float
    filled_size: float = 0.0
    remaining_size: float = 0.0
    limit_price: float | None = None
    average_fill_price: float | None = None
    created_at: str = ""
    updated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TradeContext:
    """Inputs the shared core needs to decide whether to take a trade."""

    exchange: str
    market_id: str
    question: str
    direction: str
    market_price: float | None
    yes_price: float | None
    no_price: float | None
    model_probability: float | None
    edge: float | None
    confidence: float | None
    account_state: AccountState
    source_context: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TradeDecision:
    """Shared-core decision result before any paper/live execution details."""

    action: str
    approved: bool
    reason_code: str
    reason: str = ""
    confidence: float = 0.0
    edge: float | None = None
    entry_price: float | None = None
    win_probability: float | None = None
    requested_position_size: float | None = None
    position_size: float | None = None
    risk_score: float = 0.0
    warnings: list[str] = field(default_factory=list)
    reasoning: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExecutionResult:
    """Execution outcome returned by a paper/live adapter."""

    accepted: bool
    action: str
    status: str
    message: str = ""
    trade_id: str = ""
    order_id: str = ""
    requested_size: float | None = None
    filled_size: float = 0.0
    remaining_size: float = 0.0
    fill_price: float | None = None
    reserved_capital_delta: float = 0.0
    available_cash_after: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ResolutionEvent:
    """Stable position-settlement event emitted by a resolution adapter."""

    position_id: str
    market_id: str
    outcome: str
    status: str
    resolved_at: str = ""
    pnl: float | None = None
    settlement_value: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class AccountStateProvider(Protocol):
    """Provides the latest account state for the shared core."""

    def get_account_state(self) -> AccountState:
        ...


@runtime_checkable
class StateAdapter(AccountStateProvider, Protocol):
    """Shared shape for state adapters in any environment."""

    def list_open_positions(self) -> Sequence[PositionState]:
        ...


@runtime_checkable
class PaperStateAdapter(StateAdapter, Protocol):
    """Paper-mode state source backed by simulator/session state."""

    def get_paper_session_state(self) -> PaperSessionState:
        ...


@runtime_checkable
class LiveStateAdapter(StateAdapter, Protocol):
    """Live-mode state source backed by exchange/API truth."""

    def list_resting_orders(self) -> Sequence[OrderState]:
        ...


@runtime_checkable
class ExecutionAdapter(Protocol):
    """Executes a shared decision in a concrete environment."""

    def execute(self, decision: TradeDecision, context: TradeContext) -> ExecutionResult:
        ...


@runtime_checkable
class PaperExecutionAdapter(ExecutionAdapter, Protocol):
    """Paper-mode execution boundary.

    Implementations should simulate a fill, reserve capital, and emit synthetic
    identifiers without reintroducing trade-decision logic.
    """


@dataclass(slots=True)
class CancelOrderResult:
    """Outcome of a live-order cancellation request."""

    accepted: bool
    order_id: str
    status: str
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class LiveExecutionAdapter(ExecutionAdapter, Protocol):
    """Live-mode execution boundary backed by exchange order APIs."""

    def get_order_status(self, order_id: str) -> OrderState | None:
        ...

    def cancel_order(self, order_id: str) -> CancelOrderResult:
        ...


@runtime_checkable
class ResolutionAdapter(Protocol):
    """Resolves open positions using the current environment's truth source."""

    def resolve_open_positions(self, settlement_source: Any | None = None) -> Sequence[ResolutionEvent]:
        ...
