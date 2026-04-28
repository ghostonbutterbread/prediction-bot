"""Shared core interfaces and thin decision path."""

from .decision import build_trade_decision, normalize_trade_context, reason_to_key
from .execution_snapshot import build_execution_snapshot
from .interfaces import (
    AccountState,
    AccountStateProvider,
    CancelOrderResult,
    ExecutionAdapter,
    ExecutionResult,
    LiveExecutionAdapter,
    LiveStateAdapter,
    TradeContext,
    TradeDecision,
    OrderState,
    PaperExecutionAdapter,
    PaperSessionState,
    PaperStateAdapter,
    PositionState,
    ResolutionAdapter,
    ResolutionEvent,
    StateAdapter,
)
from .weather_risk import (
    apply_weather_size_limits,
    assess_weather_market_risk,
    build_weather_source_confidence_evidence,
    classify_weather_market,
)

__all__ = [
    "AccountState",
    "AccountStateProvider",
    "CancelOrderResult",
    "ExecutionAdapter",
    "ExecutionResult",
    "LiveExecutionAdapter",
    "LiveStateAdapter",
    "OrderState",
    "PaperExecutionAdapter",
    "PaperSessionState",
    "PaperStateAdapter",
    "PositionState",
    "ResolutionAdapter",
    "ResolutionEvent",
    "StateAdapter",
    "TradeContext",
    "TradeDecision",
    "apply_weather_size_limits",
    "assess_weather_market_risk",
    "build_execution_snapshot",
    "build_weather_source_confidence_evidence",
    "build_trade_decision",
    "classify_weather_market",
    "normalize_trade_context",
    "reason_to_key",
]
