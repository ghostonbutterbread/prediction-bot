"""Thin market decision evaluator shared by research-oriented modes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from bot.risk import RiskManager
from bot.shared_core import AccountState, TradeContext, build_execution_snapshot, build_trade_decision
from bot.strategies.enhanced import EnhancedStrategyEngine, KellySizer, StrategyTrace
from bot.trade_audit import trade_event_key


@dataclass(slots=True)
class DecisionPipelineInput:
    market: Any
    account_state: AccountState
    order_book: dict[str, Any] | None = None
    source_context: dict[str, Any] = field(default_factory=dict)
    execution_snapshot: dict[str, Any] | None = None
    mode: str = "paper_lab"
    config_snapshot: dict[str, Any] = field(default_factory=dict)
    as_of: datetime | None = None


@dataclass(slots=True)
class DecisionPipelineResult:
    market_id: str
    mode: str
    observed_at: str
    as_of: str | None
    strategy_trace: dict[str, Any]
    strategy_signal: dict[str, Any] | None
    source_context: dict[str, Any]
    order_book: dict[str, Any] | None
    order_book_snapshot: dict[str, Any]
    execution_snapshot: dict[str, Any] | None
    execution_snapshot_source: str | None
    trade_context: dict[str, Any] | None
    shared_core_decision: dict[str, Any] | None
    final_action: str
    final_reason_code: str | None
    final_reason: str | None
    warnings: list[str]
    config_hash: str
    logic_version: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DecisionPipelineEvaluator:
    """Evaluate one market through strategy trace plus shared-core decision."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        strategy: Any | None = None,
        kelly_sizer: Any | None = None,
        risk_policy: Any | None = None,
    ):
        self.config = config or {}
        self.strategy = strategy or EnhancedStrategyEngine(self.config.get("strategy", {}) or {})
        economics_cfg = self.config.get("trade_economics", {}) or {}
        self.kelly_sizer = kelly_sizer or KellySizer(
            fee_rate=self.config.get("kalshi_fee_rate"),
            min_position_size_usd=economics_cfg.get("min_position_size_usd", 1.0),
            min_expected_net_profit_usd=economics_cfg.get("min_expected_net_profit_usd", 0.0),
        )
        self.risk_policy = risk_policy or RiskManager(self.config)

    def evaluate(
        self,
        market: Any,
        *,
        account_state: AccountState | None = None,
        order_book: dict[str, Any] | None = None,
        source_context: dict[str, Any] | None = None,
        execution_snapshot: dict[str, Any] | None = None,
        mode: str = "paper_lab",
        config_snapshot: dict[str, Any] | None = None,
        as_of: datetime | None = None,
    ) -> DecisionPipelineResult:
        pipeline_input = DecisionPipelineInput(
            market=market,
            account_state=account_state or build_fixed_opportunity_account_state(),
            order_book=order_book,
            source_context=dict(source_context or {}),
            execution_snapshot=dict(execution_snapshot) if isinstance(execution_snapshot, dict) else None,
            mode=mode,
            config_snapshot=dict(config_snapshot or self.config),
            as_of=as_of,
        )
        return self.run(pipeline_input)

    def run(self, pipeline_input: DecisionPipelineInput) -> DecisionPipelineResult:
        observed_at = datetime.now(timezone.utc).isoformat()
        signal, trace = self._analyze_with_trace(pipeline_input.market, pipeline_input.order_book)
        source_context = build_source_snapshot_envelope(
            pipeline_input.source_context,
            mode=pipeline_input.mode,
            as_of=pipeline_input.as_of,
        )
        order_book_snapshot = build_order_book_snapshot(pipeline_input.order_book)
        warnings = list(trace.warnings)

        if signal is None:
            reason_code = trace.skip_reason_code or "strategy_returned_none_untraced"
            if reason_code == "strategy_returned_none_untraced":
                trace.skip_reason_code = reason_code
            return DecisionPipelineResult(
                market_id=str(getattr(pipeline_input.market, "id", "")),
                mode=pipeline_input.mode,
                observed_at=observed_at,
                as_of=_iso_or_none(pipeline_input.as_of),
                strategy_trace=trace.to_dict(),
                strategy_signal=None,
                source_context=source_context,
                order_book=pipeline_input.order_book,
                order_book_snapshot=order_book_snapshot,
                execution_snapshot=None,
                execution_snapshot_source=None,
                trade_context=None,
                shared_core_decision=None,
                final_action="SKIP",
                final_reason_code=reason_code,
                final_reason=reason_code,
                warnings=warnings,
                config_hash=_hash_config(pipeline_input.config_snapshot),
                logic_version=_logic_version(pipeline_input.config_snapshot),
            )

        normalized_signal = _normalize_signal_prices(signal, pipeline_input.market)
        if isinstance(pipeline_input.execution_snapshot, dict) and pipeline_input.execution_snapshot:
            execution_snapshot = dict(pipeline_input.execution_snapshot)
        else:
            execution_snapshot = build_execution_snapshot(
                normalized_signal,
                direction=str(normalized_signal.get("direction", "BUY_YES") or "BUY_YES").upper(),
                bid_ask=_bid_ask_from_order_book(pipeline_input.order_book),
            )
        context = self._build_trade_context(
            pipeline_input,
            normalized_signal,
            source_context=source_context,
            execution_snapshot=execution_snapshot,
        )
        decision = build_trade_decision(
            context,
            kelly_sizer=self.kelly_sizer,
            risk_policy=self.risk_policy,
            min_edge=_threshold(self.config, "min_edge", 0.01),
            min_confidence=_threshold(self.config, "min_confidence", 0.50),
            max_entry_price=float(self.config.get("max_entry_price", 0.70) or 0.70),
        )
        decision_dict = asdict(decision)
        return DecisionPipelineResult(
            market_id=context.market_id,
            mode=pipeline_input.mode,
            observed_at=observed_at,
            as_of=_iso_or_none(pipeline_input.as_of),
            strategy_trace=trace.to_dict(),
            strategy_signal=normalized_signal,
            source_context=source_context,
            order_book=pipeline_input.order_book,
            order_book_snapshot=order_book_snapshot,
            execution_snapshot=execution_snapshot,
            execution_snapshot_source=execution_snapshot.get("source"),
            trade_context=asdict(context),
            shared_core_decision=decision_dict,
            final_action=decision.action if decision.approved else "SKIP",
            final_reason_code=decision.reason_code,
            final_reason=decision.reason,
            warnings=warnings + list(decision.warnings or []),
            config_hash=_hash_config(pipeline_input.config_snapshot),
            logic_version=_logic_version(pipeline_input.config_snapshot),
        )

    def _analyze_with_trace(self, market: Any, order_book: dict[str, Any] | None) -> tuple[dict[str, Any] | None, StrategyTrace]:
        traced = getattr(self.strategy, "analyze_market_with_trace", None)
        if callable(traced):
            signal, trace = traced(market, order_book)
            if not isinstance(trace, StrategyTrace):
                trace = _coerce_trace(trace)
            return signal, trace

        signal = self.strategy.analyze_market(market, order_book)
        trace = StrategyTrace()
        if signal is None:
            trace.skip_reason_code = "strategy_returned_none_untraced"
        return signal, trace

    def _build_trade_context(
        self,
        pipeline_input: DecisionPipelineInput,
        signal: dict[str, Any],
        *,
        source_context: dict[str, Any],
        execution_snapshot: dict[str, Any],
    ) -> TradeContext:
        market = pipeline_input.market
        direction = str(signal.get("direction", "BUY_YES") or "BUY_YES").upper()
        event_key = trade_event_key(signal)
        source_signal = dict(signal)
        source_signal["source_snapshot"] = source_context
        metadata = dict(getattr(market, "metadata", {}) or {})
        return TradeContext(
            exchange=str(signal.get("exchange") or getattr(market, "exchange", "unknown") or "unknown"),
            market_id=str(signal.get("market_id") or getattr(market, "id", "") or ""),
            question=str(signal.get("question") or getattr(market, "question", "") or ""),
            direction=direction,
            market_price=execution_snapshot.get("market_price"),
            yes_price=execution_snapshot.get("yes_price"),
            no_price=execution_snapshot.get("no_price"),
            model_probability=signal.get("model_probability"),
            edge=signal.get("edge"),
            confidence=signal.get("confidence"),
            account_state=pipeline_input.account_state,
            source_context=source_signal,
            metadata={
                "runner": "decision_pipeline",
                "mode": pipeline_input.mode,
                "category": metadata.get("category", getattr(market, "category", "")),
                "event_key": event_key,
                "market_family_key": _market_family_key(str(signal.get("market_id") or getattr(market, "id", ""))),
                "event_snapshot": _empty_event_snapshot(event_key, signal, execution_snapshot),
                "retrade_policy": _retrade_policy_metadata(self.risk_policy, self.kelly_sizer),
            },
        )


def build_fixed_opportunity_account_state(bankroll_usd: float = 100.0) -> AccountState:
    bankroll = max(0.0, float(bankroll_usd or 0.0))
    return AccountState(
        starting_balance=bankroll,
        current_balance=bankroll,
        available_cash=bankroll,
        reserved_capital=0.0,
        total_exposure=0.0,
        open_positions=0,
        metadata={
            "mode": "paper_lab",
            "effective_tradable_cash": bankroll,
            "source": "fixed_opportunity",
        },
    )


def build_source_snapshot_envelope(
    source_context: dict[str, Any] | None,
    *,
    mode: str,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    return {
        "source": "provided" if source_context else "missing",
        "mode": mode,
        "as_of": _iso_or_none(as_of),
        "data": dict(source_context or {}),
    }


def build_order_book_snapshot(order_book: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "source": "book" if order_book else "missing",
        "data": dict(order_book) if isinstance(order_book, dict) else None,
    }


def _normalize_signal_prices(signal: dict[str, Any], market: Any) -> dict[str, Any]:
    normalized = dict(signal)
    yes_price = normalized.get("yes_price", normalized.get("yes_market_price", getattr(market, "yes_price", None)))
    no_price = normalized.get("no_price", normalized.get("no_market_price", getattr(market, "no_price", None)))
    normalized["yes_price"] = yes_price
    normalized["no_price"] = no_price
    normalized.setdefault("yes_market_price", yes_price)
    normalized.setdefault("no_market_price", no_price)
    normalized.setdefault("market_price", yes_price)
    normalized.setdefault("market_id", getattr(market, "id", ""))
    normalized.setdefault("exchange", getattr(market, "exchange", "unknown"))
    normalized.setdefault("question", getattr(market, "question", ""))
    return normalized


def _bid_ask_from_order_book(order_book: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(order_book, dict):
        return None
    return {
        "best_yes_ask": order_book.get("best_yes_ask"),
        "best_no_ask": order_book.get("best_no_ask"),
        "best_yes_bid": order_book.get("best_yes_bid"),
        "best_no_bid": order_book.get("best_no_bid"),
    }


def _empty_event_snapshot(
    event_key: str,
    signal: dict[str, Any],
    execution_snapshot: dict[str, Any],
) -> dict[str, Any]:
    market_id = str(signal.get("market_id", "") or "")
    return {
        "event_key": event_key,
        "candidate_family_key": _market_family_key(market_id),
        "event_position_count_before": 0,
        "event_entries_count": 0,
        "event_exposure_before": 0.0,
        "filled_event_exposure_before": 0.0,
        "pending_event_exposure_before": 0.0,
        "filled_event_position_count_before": 0,
        "pending_event_position_count_before": 0,
        "held_market_ids": [],
        "same_event_directions": [],
        "same_family_markets": [],
        "same_family_positions": [],
        "opposite_side_detected": False,
        "event_entry_prices": [],
        "liquidity": signal.get("liquidity"),
        "best_yes_ask": execution_snapshot.get("best_yes_ask"),
        "best_no_ask": execution_snapshot.get("best_no_ask"),
        "best_yes_bid": execution_snapshot.get("best_yes_bid"),
        "best_no_bid": execution_snapshot.get("best_no_bid"),
        "estimated_fill_price": execution_snapshot.get("estimated_fill_price"),
        "execution_snapshot_source": execution_snapshot.get("source"),
    }


def _retrade_policy_metadata(risk_policy: Any, kelly_sizer: Any) -> dict[str, Any]:
    fields = (
        "max_event_exposure_pct",
        "max_event_positions",
        "retrade_edge_premium",
        "retrade_confidence_premium",
        "retrade_size_decay",
        "strict_event_overlap",
        "min_retrade_net_edge",
        "min_retrade_expected_profit_usd",
        "require_price_improvement_for_same_market_family",
        "price_improvement_ticks",
    )
    metadata = {field_name: getattr(risk_policy, field_name) for field_name in fields if hasattr(risk_policy, field_name)}
    metadata["fee_rate"] = getattr(kelly_sizer, "fee_rate", 0.07)
    return metadata


def _coerce_trace(value: Any) -> StrategyTrace:
    if isinstance(value, dict):
        return StrategyTrace(
            raw_signals=dict(value.get("raw_signals") or {}),
            validation_results=dict(value.get("validation_results") or {}),
            accepted_signals=dict(value.get("accepted_signals") or {}),
            rejected_signals=dict(value.get("rejected_signals") or {}),
            ensemble_signal=value.get("ensemble_signal") if isinstance(value.get("ensemble_signal"), dict) else None,
            skip_reason_code=value.get("skip_reason_code"),
            warnings=list(value.get("warnings") or []),
        )
    return StrategyTrace(warnings=["invalid_strategy_trace_shape"])


def _threshold(config: dict[str, Any], key: str, default: float) -> float:
    strategy = config.get("strategy", {}) or {}
    return float(strategy.get(key, config.get(key, default)) or default)


def _logic_version(config: dict[str, Any]) -> str:
    prediction_lab = config.get("prediction_lab", {}) or {}
    return str(prediction_lab.get("strategy_version") or config.get("logic_version") or "v1")


def _hash_config(config: dict[str, Any]) -> str:
    try:
        payload = json.dumps(config, sort_keys=True, default=str)
    except TypeError:
        payload = str(config)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _market_family_key(market_id: str) -> str:
    parts = str(market_id or "").split("-")
    return "-".join(parts[:-1]) if len(parts) > 1 else str(market_id or "")


def _iso_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()
