"""Thin market decision evaluator shared by research-oriented modes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from math import isfinite
from typing import Any

from bot.risk import RiskDecision, RiskManager
from bot.shared_core import AccountState, TradeContext, build_execution_snapshot, build_trade_decision
from bot.strategies.enhanced import EnhancedStrategyEngine, KellySizer, StrategyTrace
from bot.trade_audit import trade_event_key

PAPER_LAB_MODE = "paper_lab"
OPPORTUNITY_MODE = "opportunity"
FIXED_OPPORTUNITY_ACCOUNT_SOURCE = "fixed_opportunity"
PRE_EXECUTION_ARTIFACT_VERSION = 1


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
    account_state_snapshot: dict[str, Any]
    opportunity_mode: dict[str, Any] | None
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
        signal, trace = self._analyze_with_trace(pipeline_input.market, pipeline_input.order_book, as_of=pipeline_input.as_of)
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
                account_state_snapshot=_account_state_snapshot(pipeline_input.account_state),
                opportunity_mode=_opportunity_mode_metadata(pipeline_input.account_state, pipeline_input.mode),
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
            account_state_snapshot=_account_state_snapshot(pipeline_input.account_state),
            opportunity_mode=_opportunity_mode_metadata(pipeline_input.account_state, pipeline_input.mode, decision_dict),
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

    def _analyze_with_trace(
        self,
        market: Any,
        order_book: dict[str, Any] | None,
        *,
        as_of: datetime | None = None,
    ) -> tuple[dict[str, Any] | None, StrategyTrace]:
        validator = getattr(self.strategy, "validator", None)
        previous_as_of = getattr(validator, "as_of", None) if validator is not None else None
        if validator is not None and as_of is not None and hasattr(validator, "as_of"):
            validator.as_of = as_of
        try:
            return self._analyze_with_trace_inner(market, order_book)
        finally:
            if validator is not None and as_of is not None and hasattr(validator, "as_of"):
                validator.as_of = previous_as_of

    def _analyze_with_trace_inner(self, market: Any, order_book: dict[str, Any] | None) -> tuple[dict[str, Any] | None, StrategyTrace]:
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


def build_pre_execution_decision_artifact(
    *,
    mode: str,
    context: TradeContext | None,
    decision: Any,
    signal: dict[str, Any] | None = None,
    order_book: dict[str, Any] | None = None,
    source_context: dict[str, Any] | None = None,
    execution_snapshot: dict[str, Any] | None = None,
    config_snapshot: dict[str, Any] | None = None,
    observed_at: datetime | None = None,
    as_of: datetime | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Build the shared paper/live pre-execution decision artifact envelope.

    The helper is intentionally serialization-only. It must not decide whether
    paper fills or live places orders; those remain adapter concerns.
    """

    observed_at = observed_at or datetime.now(timezone.utc)
    signal_snapshot = dict(signal or {})
    context_source = dict(source_context or (context.source_context if context is not None else signal_snapshot) or {})
    market_id = str(
        (context.market_id if context is not None else None)
        or signal_snapshot.get("market_id")
        or ""
    )
    account_state_snapshot = _account_state_snapshot(context.account_state) if context is not None else None
    decision_dict = _decision_snapshot(decision)
    approved = bool(decision_dict.get("approved"))
    final_action = str(decision_dict.get("action") or signal_snapshot.get("direction") or "SKIP")
    if not approved:
        final_action = "SKIP"
    artifact_warnings = list(warnings or [])
    artifact_warnings.extend(list(decision_dict.get("warnings") or []))
    artifact = {
        "artifact_version": PRE_EXECUTION_ARTIFACT_VERSION,
        "artifact_kind": "pre_execution_decision",
        "market_id": market_id,
        "mode": mode,
        "observed_at": _iso_or_none(observed_at),
        "as_of": _iso_or_none(as_of),
        "strategy_trace": {},
        "strategy_signal": signal_snapshot or None,
        "source_context": build_source_snapshot_envelope(context_source, mode=mode, as_of=as_of),
        "account_state_snapshot": account_state_snapshot,
        "opportunity_mode": _opportunity_mode_metadata(context.account_state, mode, decision_dict) if context is not None else None,
        "order_book": dict(order_book) if isinstance(order_book, dict) else None,
        "order_book_snapshot": build_order_book_snapshot(order_book),
        "execution_snapshot": dict(execution_snapshot) if isinstance(execution_snapshot, dict) else None,
        "execution_snapshot_source": (execution_snapshot or {}).get("source") if isinstance(execution_snapshot, dict) else None,
        "trade_context": asdict(context) if context is not None else None,
        "shared_core_decision": decision_dict,
        "final_action": final_action,
        "final_reason_code": decision_dict.get("reason_code"),
        "final_reason": decision_dict.get("reason") or decision_dict.get("reason_code"),
        "warnings": artifact_warnings,
        "config_hash": _hash_config(config_snapshot or {}),
        "logic_version": _logic_version(config_snapshot or {}),
    }
    return _json_safe(artifact)


@dataclass(slots=True)
class FixedOpportunityAccountStateProvider:
    """Provides a fresh isolated bankroll snapshot for one Paper Lab opportunity."""

    bankroll_usd: float = 100.0
    mode: str = PAPER_LAB_MODE

    def get_account_state(self) -> AccountState:
        return build_fixed_opportunity_account_state(self.bankroll_usd, mode=self.mode)


@dataclass(slots=True)
class FixedOpportunityRiskPolicy:
    """Stateless Prediction Lab risk policy backed only by the opportunity bankroll."""

    bankroll_usd: float = 100.0
    max_bet_pct: float = 0.10
    max_position_size_usd: float = 0.0
    min_position_size_usd: float = 1.0
    max_tradable_balance_usd: float = 0.0
    max_event_exposure_pct: float = 0.10
    max_event_positions: int = 3
    retrade_edge_premium: float = 0.01
    retrade_confidence_premium: float = 0.0
    retrade_size_decay: float = 0.65
    strict_event_overlap: bool = True
    min_retrade_net_edge: float = 0.005
    min_retrade_expected_profit_usd: float = 0.0
    require_price_improvement_for_same_market_family: bool = False
    price_improvement_ticks: float = 0.03

    def check_trade(self, signal: dict[str, Any], position_size: float, *, available_cash: float | None = None) -> RiskDecision:
        original_size = position_size
        try:
            requested_size = float(position_size)
        except (TypeError, ValueError):
            return RiskDecision(approved=False, reason="Invalid position size", original_size=original_size, risk_score=1.0)
        if not isfinite(requested_size) or requested_size <= 0:
            return RiskDecision(approved=False, reason="Non-positive position size", original_size=original_size, risk_score=1.0)

        spendable_cash = self.bankroll_usd if available_cash is None else float(available_cash or 0.0)
        if self.max_tradable_balance_usd and self.max_tradable_balance_usd > 0:
            spendable_cash = min(spendable_cash, self.max_tradable_balance_usd)
        if spendable_cash < self.min_position_size_usd:
            return RiskDecision(
                approved=False,
                reason=f"Opportunity bankroll below minimum size (${spendable_cash:.2f})",
                original_size=original_size,
                risk_score=1.0,
                metadata={"reason_code": "opportunity_bankroll_below_minimum", "risk_policy": "fixed_opportunity"},
            )

        warnings: list[str] = []
        adjusted_size = requested_size
        max_bet_size = max(0.0, spendable_cash * max(0.0, self.max_bet_pct))
        if max_bet_size and adjusted_size > max_bet_size:
            adjusted_size = round(max_bet_size, 2)
            warnings.append(f"Opportunity max bet capped size to ${adjusted_size:.2f}")
        if self.max_position_size_usd and self.max_position_size_usd > 0 and adjusted_size > self.max_position_size_usd:
            adjusted_size = round(self.max_position_size_usd, 2)
            warnings.append(f"Opportunity max position capped size to ${adjusted_size:.2f}")
        if adjusted_size > spendable_cash:
            adjusted_size = round(spendable_cash, 2)
            warnings.append(f"Opportunity bankroll capped size to ${adjusted_size:.2f}")
        if adjusted_size < self.min_position_size_usd:
            return RiskDecision(
                approved=False,
                reason=f"Opportunity adjusted size below minimum (${adjusted_size:.2f})",
                original_size=original_size,
                adjusted_size=adjusted_size,
                risk_score=1.0,
                warnings=warnings,
                metadata={"reason_code": "opportunity_adjusted_size_below_minimum", "risk_policy": "fixed_opportunity"},
            )
        return RiskDecision(
            approved=True,
            reason="Approved",
            adjusted_size=adjusted_size,
            original_size=original_size,
            warnings=warnings,
            metadata={"reason_code": "approved", "risk_policy": "fixed_opportunity"},
        )


def build_fixed_opportunity_account_state(bankroll_usd: float = 100.0, *, mode: str = PAPER_LAB_MODE) -> AccountState:
    bankroll = max(0.0, float(bankroll_usd or 0.0))
    return AccountState(
        starting_balance=bankroll,
        current_balance=bankroll,
        available_cash=bankroll,
        reserved_capital=0.0,
        total_exposure=0.0,
        open_positions=0,
        metadata={
            "mode": mode,
            "paper_lab_mode": PAPER_LAB_MODE,
            "opportunity_mode": OPPORTUNITY_MODE,
            "account_state_provider": FIXED_OPPORTUNITY_ACCOUNT_SOURCE,
            "effective_tradable_cash": bankroll,
            "source": FIXED_OPPORTUNITY_ACCOUNT_SOURCE,
            "isolated_bankroll": True,
            "mutates_portfolio_account": False,
        },
    )


def build_fixed_opportunity_risk_policy(config: dict[str, Any] | None = None, *, bankroll_usd: float = 100.0) -> FixedOpportunityRiskPolicy:
    config = config or {}
    economics_cfg = config.get("trade_economics", {}) or {}
    return FixedOpportunityRiskPolicy(
        bankroll_usd=float(bankroll_usd or 0.0),
        max_bet_pct=_risk_config_float(config, "max_bet_pct", 0.10),
        max_position_size_usd=_risk_config_float(config, "max_position_size_usd", _risk_config_float(config, "max_position_size", 0.0)),
        min_position_size_usd=float(economics_cfg.get("min_position_size_usd", config.get("min_position_size", 1.0)) or 1.0),
        max_tradable_balance_usd=_risk_config_float(config, "max_tradable_balance_usd", _risk_config_float(config, "max_tradable_balance", 0.0)),
        max_event_exposure_pct=_risk_config_float(config, "max_event_exposure_pct", 0.10),
        max_event_positions=int(_risk_config_float(config, "max_event_positions", 3)),
        retrade_edge_premium=_risk_config_float(config, "retrade_edge_premium", 0.01),
        retrade_confidence_premium=_risk_config_float(config, "retrade_confidence_premium", 0.0),
        retrade_size_decay=_risk_config_float(config, "retrade_size_decay", 0.65),
        strict_event_overlap=bool(_risk_config_value(config, "strict_event_overlap", True)),
        min_retrade_net_edge=_risk_config_float(config, "min_retrade_net_edge", 0.005),
        min_retrade_expected_profit_usd=_risk_config_float(config, "min_retrade_expected_profit_usd", 0.0),
        require_price_improvement_for_same_market_family=bool(
            _risk_config_value(config, "require_price_improvement_for_same_market_family", False)
        ),
        price_improvement_ticks=_risk_config_float(config, "price_improvement_ticks", 0.03),
    )


def _account_state_snapshot(account_state: AccountState) -> dict[str, Any]:
    return {
        "starting_balance": round(float(account_state.starting_balance), 4),
        "current_balance": round(float(account_state.current_balance), 4),
        "available_cash": round(float(account_state.available_cash), 4),
        "reserved_capital": round(float(account_state.reserved_capital), 4),
        "total_exposure": round(float(account_state.total_exposure), 4),
        "open_positions": int(account_state.open_positions),
        "metadata": dict(account_state.metadata or {}),
    }


def _decision_snapshot(decision: Any) -> dict[str, Any]:
    if hasattr(decision, "__dataclass_fields__"):
        snapshot = asdict(decision)
    elif isinstance(decision, dict):
        snapshot = dict(decision)
    else:
        snapshot = {
            "action": getattr(decision, "action", None),
            "approved": getattr(decision, "approved", None),
            "reason_code": getattr(decision, "reason_code", None),
            "reason": getattr(decision, "reason", ""),
            "confidence": getattr(decision, "confidence", 0.0),
            "edge": getattr(decision, "edge", None),
            "entry_price": getattr(decision, "entry_price", None),
            "win_probability": getattr(decision, "win_probability", None),
            "requested_position_size": getattr(decision, "requested_position_size", None),
            "position_size": getattr(decision, "position_size", None),
            "risk_score": getattr(decision, "risk_score", 0.0),
            "warnings": list(getattr(decision, "warnings", []) or []),
            "reasoning": dict(getattr(decision, "reasoning", {}) or {}),
        }
    snapshot["action"] = snapshot.get("action") or "SKIP"
    snapshot["approved"] = bool(snapshot.get("approved", False))
    snapshot["reason_code"] = snapshot.get("reason_code") or "unknown"
    snapshot["reason"] = snapshot.get("reason") or snapshot["reason_code"]
    snapshot["warnings"] = list(snapshot.get("warnings") or [])
    snapshot["reasoning"] = dict(snapshot.get("reasoning") or {})
    return snapshot


def _opportunity_mode_metadata(
    account_state: AccountState,
    mode: str,
    decision: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    metadata = dict(account_state.metadata or {})
    if metadata.get("account_state_provider") != FIXED_OPPORTUNITY_ACCOUNT_SOURCE and metadata.get("source") != FIXED_OPPORTUNITY_ACCOUNT_SOURCE:
        return None
    shared_core_kelly = ((decision or {}).get("reasoning") or {}).get("kelly")
    return {
        "mode": OPPORTUNITY_MODE,
        "paper_lab_mode": PAPER_LAB_MODE,
        "runner_mode": mode,
        "account_state_provider": FIXED_OPPORTUNITY_ACCOUNT_SOURCE,
        "bankroll_usd": round(float(metadata.get("effective_tradable_cash", account_state.available_cash) or 0.0), 4),
        "isolated_bankroll": True,
        "mutates_portfolio_account": False,
        "kelly": dict(shared_core_kelly) if isinstance(shared_core_kelly, dict) else None,
    }


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


def _risk_config_value(config: dict[str, Any], key: str, default: Any) -> Any:
    risk_cfg = config.get("risk", {}) or {}
    if key in risk_cfg:
        return risk_cfg.get(key)
    return config.get(key, default)


def _risk_config_float(config: dict[str, Any], key: str, default: float) -> float:
    try:
        return float(_risk_config_value(config, key, default))
    except (TypeError, ValueError):
        return float(default)


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


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, datetime):
        return _iso_or_none(value)
    return str(value)
