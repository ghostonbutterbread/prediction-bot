from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .analysis import WeatherSampleRecord, normalized_market_type
from .market_mapping import WeatherMarketCityMapper
from .registry import WeatherRegistry


VALID_REPLAY_ACTIONS = ("BUY_YES", "BUY_NO", "SKIP")
RESOLVED_OUTCOMES = {"YES", "NO"}
DEFAULT_REPLAY_FEE_RATE = 0.07


@dataclass(frozen=True)
class ReplayFeeModel:
    """Conservative replay execution model for weather trades.

    profit_fee_rate:
        Fee charged on positive gross profit.
    notional_fee_rate:
        Fee charged on entry notional for every filled trade.
    slippage_bps:
        Entry slippage in basis points applied against the trader.
    late_entry_penalty_rate:
        Conservative penalty applied to the remaining upside when replaying a
        trade as if entry happened later than the captured quote.
    """

    profit_fee_rate: float = DEFAULT_REPLAY_FEE_RATE
    notional_fee_rate: float = 0.0
    slippage_bps: float = 0.0
    late_entry_penalty_rate: float = 0.0

    def __post_init__(self) -> None:
        if self.profit_fee_rate < 0:
            raise ValueError("profit_fee_rate must be non-negative")
        if self.notional_fee_rate < 0:
            raise ValueError("notional_fee_rate must be non-negative")
        if self.slippage_bps < 0:
            raise ValueError("slippage_bps must be non-negative")
        if not 0 <= self.late_entry_penalty_rate < 1:
            raise ValueError("late_entry_penalty_rate must be between 0 and 1")

    def apply_entry_adjustments(self, entry_price: float) -> float:
        adjusted_price = float(entry_price)
        if self.slippage_bps:
            adjusted_price += adjusted_price * (self.slippage_bps / 10_000)
        if self.late_entry_penalty_rate:
            adjusted_price += (1 - adjusted_price) * self.late_entry_penalty_rate
        return round(min(max(adjusted_price, 0.0001), 0.9999), 4)

    def calculate_fees(self, gross_profit: float, position_size: float) -> dict[str, float]:
        notional_fee = round(max(position_size, 0.0) * self.notional_fee_rate, 4)
        profit_fee = 0.0
        if gross_profit > 0:
            profit_fee = round(gross_profit * self.profit_fee_rate, 4)
        total_fee = round(notional_fee + profit_fee, 4)
        return {
            "notional_fee": notional_fee,
            "profit_fee": profit_fee,
            "total_fee": total_fee,
        }


@dataclass(frozen=True)
class WeatherReplayRecord:
    replay_id: str
    quiz_payload: dict[str, Any]
    answer_key: dict[str, Any]


def build_weather_replay_record(
    record: WeatherSampleRecord,
    *,
    mapper: WeatherMarketCityMapper | None = None,
    registry: WeatherRegistry | None = None,
) -> WeatherReplayRecord:
    outcome = _normalized_outcome(record.outcome)
    if outcome not in RESOLVED_OUTCOMES:
        raise ValueError("weather replay requires a resolved YES/NO outcome")

    registry = registry or WeatherRegistry.from_file()
    mapper = mapper or WeatherMarketCityMapper(registry)
    mapped = mapper.resolve(record.question, record.category)
    city_context = _build_city_context(mapped.city_id, mapped.primary_source_id, registry) if mapped else None
    prices = _build_price_context(record.yes_price, record.no_price, record.volume)
    replay_id = _build_replay_id(record)

    payload = {
        "replay_id": replay_id,
        "market_id": record.market_id,
        "series_ticker": record.category or None,
        "question": record.question,
        "prices": prices,
        "city_context": city_context,
        "timestamps": {
            "observed_at": record.observed_at,
            "resolved_at": record.resolved_at,
        },
        "metadata": _compact_metadata(record),
    }
    answer_key = {
        "replay_id": replay_id,
        "market_id": record.market_id,
        "outcome": outcome,
        "correct_action": "BUY_YES" if outcome == "YES" else "BUY_NO",
        "resolved_at": record.resolved_at,
        "prices": prices,
    }
    return WeatherReplayRecord(replay_id=replay_id, quiz_payload=payload, answer_key=answer_key)


def iter_weather_replay_records(
    records: Iterable[WeatherSampleRecord],
    *,
    mapper: WeatherMarketCityMapper | None = None,
    registry: WeatherRegistry | None = None,
    skip_unresolved: bool = True,
) -> list[WeatherReplayRecord]:
    registry = registry or WeatherRegistry.from_file()
    mapper = mapper or WeatherMarketCityMapper(registry)
    replay_records: list[WeatherReplayRecord] = []
    for record in records:
        if skip_unresolved and _normalized_outcome(record.outcome) not in RESOLVED_OUTCOMES:
            continue
        replay_records.append(
            build_weather_replay_record(
                record,
                mapper=mapper,
                registry=registry,
            )
        )
    return replay_records


def build_weather_replay_dataset(
    records: Iterable[WeatherSampleRecord],
    *,
    mapper: WeatherMarketCityMapper | None = None,
    registry: WeatherRegistry | None = None,
    skip_unresolved: bool = True,
) -> dict[str, Any]:
    replay_records = iter_weather_replay_records(
        records,
        mapper=mapper,
        registry=registry,
        skip_unresolved=skip_unresolved,
    )
    quiz_payloads = [record.quiz_payload for record in replay_records]
    answer_keys = [record.answer_key for record in replay_records]
    mapped_count = sum(1 for record in quiz_payloads if record.get("city_context"))
    return {
        "summary": {
            "records": len(quiz_payloads),
            "mapped_cities": mapped_count,
            "with_prices": sum(
                1
                for record in quiz_payloads
                if record.get("prices", {}).get("yes_price") is not None
                or record.get("prices", {}).get("no_price") is not None
            ),
        },
        "quiz_payloads": quiz_payloads,
        "answer_keys": answer_keys,
    }


def score_replay_answer(
    action: str,
    answer_key: Mapping[str, Any],
    *,
    contracts: float | None = None,
    position_size: float | None = None,
    fee_model: ReplayFeeModel | None = None,
) -> dict[str, Any]:
    normalized_action = str(action or "SKIP").strip().upper()
    if normalized_action not in VALID_REPLAY_ACTIONS:
        raise ValueError(f"invalid replay action '{action}'")

    outcome = _normalized_outcome(answer_key.get("outcome"))
    if outcome not in RESOLVED_OUTCOMES:
        raise ValueError("answer key must contain a resolved YES/NO outcome")

    correct_action = "BUY_YES" if outcome == "YES" else "BUY_NO"
    prices = answer_key.get("prices", {}) if isinstance(answer_key.get("prices"), Mapping) else {}
    yes_price = _float_or_none(prices.get("yes_price"))
    no_price = _float_or_none(prices.get("no_price"))
    if yes_price is None and no_price is not None and 0 <= no_price <= 1:
        yes_price = round(1 - no_price, 4)
    if no_price is None and yes_price is not None and 0 <= yes_price <= 1:
        no_price = round(1 - yes_price, 4)
    fee_model = fee_model or ReplayFeeModel()

    if normalized_action == "SKIP":
        return {
            "replay_id": answer_key.get("replay_id"),
            "market_id": answer_key.get("market_id"),
            "action": normalized_action,
            "outcome": outcome,
            "correct_action": correct_action,
            "is_correct": None,
            "points": 0,
            "quoted_entry_price": None,
            "entry_price": None,
            "contracts": 0.0,
            "position_size": 0.0,
            "fee_rate": fee_model.profit_fee_rate,
            "notional_fee_rate": fee_model.notional_fee_rate,
            "slippage_bps": fee_model.slippage_bps,
            "late_entry_penalty_rate": fee_model.late_entry_penalty_rate,
            "realized_return": 0.0,
            "gross_pnl": 0.0,
            "fees_paid": 0.0,
            "fee_breakdown": {"notional_fee": 0.0, "profit_fee": 0.0, "total_fee": 0.0},
            "net_pnl": 0.0,
        }

    quoted_entry_price = yes_price if normalized_action == "BUY_YES" else no_price
    entry_price = fee_model.apply_entry_adjustments(quoted_entry_price) if quoted_entry_price is not None else None
    is_correct = normalized_action == correct_action
    realized_return = None
    resolved_contracts = None
    resolved_position_size = None
    gross_pnl = None
    fees_paid = 0.0
    fee_breakdown = {"notional_fee": 0.0, "profit_fee": 0.0, "total_fee": 0.0}
    net_pnl = None
    if entry_price is not None:
        resolved_contracts, resolved_position_size = _resolve_position(
            entry_price,
            contracts=contracts,
            position_size=position_size,
        )
        realized_return = round((1 - entry_price) if is_correct else -entry_price, 4)
        gross_pnl = round(realized_return * resolved_contracts, 4)
        fee_breakdown = fee_model.calculate_fees(gross_pnl, resolved_position_size)
        fees_paid = fee_breakdown["total_fee"]
        net_pnl = round(gross_pnl - fees_paid, 4)

    return {
        "replay_id": answer_key.get("replay_id"),
        "market_id": answer_key.get("market_id"),
        "action": normalized_action,
        "outcome": outcome,
        "correct_action": correct_action,
        "is_correct": is_correct,
        "points": 1 if is_correct else -1,
        "quoted_entry_price": quoted_entry_price,
        "entry_price": entry_price,
        "contracts": resolved_contracts,
        "position_size": resolved_position_size,
        "fee_rate": fee_model.profit_fee_rate,
        "notional_fee_rate": fee_model.notional_fee_rate,
        "slippage_bps": fee_model.slippage_bps,
        "late_entry_penalty_rate": fee_model.late_entry_penalty_rate,
        "realized_return": realized_return,
        "gross_pnl": gross_pnl,
        "fees_paid": fees_paid,
        "fee_breakdown": fee_breakdown,
        "net_pnl": net_pnl,
    }


def score_replay_answers(
    answers: Iterable[Mapping[str, Any]],
    answer_keys: Iterable[Mapping[str, Any]],
    *,
    fee_model: ReplayFeeModel | None = None,
) -> dict[str, Any]:
    fee_model = fee_model or ReplayFeeModel()
    answer_keys_by_replay_id: dict[str, Mapping[str, Any]] = {}
    answer_keys_by_market_id: dict[str, Mapping[str, Any]] = {}
    for answer_key in answer_keys:
        replay_id = str(answer_key.get("replay_id", "") or "")
        market_id = str(answer_key.get("market_id", "") or "")
        if replay_id:
            answer_keys_by_replay_id[replay_id] = answer_key
        if market_id and market_id not in answer_keys_by_market_id:
            answer_keys_by_market_id[market_id] = answer_key

    scored: list[dict[str, Any]] = []
    missing_answer_keys: list[dict[str, Any]] = []
    invalid_answers: list[dict[str, Any]] = []
    by_action: Counter[str] = Counter()

    for answer in answers:
        action = _answer_action(answer)
        replay_id = str(answer.get("replay_id", "") or "")
        market_id = str(answer.get("market_id", "") or "")
        answer_key = answer_keys_by_replay_id.get(replay_id) if replay_id else None
        if answer_key is None and market_id:
            answer_key = answer_keys_by_market_id.get(market_id)
        if answer_key is None:
            missing_answer_keys.append({"replay_id": replay_id or None, "market_id": market_id or None})
            continue
        try:
            result = score_replay_answer(
                action,
                answer_key,
                contracts=_answer_contracts(answer),
                position_size=_answer_position_size(answer),
                fee_model=fee_model,
            )
        except ValueError as exc:
            invalid_answers.append(
                {
                    "replay_id": replay_id or answer_key.get("replay_id"),
                    "market_id": market_id or answer_key.get("market_id"),
                    "error": str(exc),
                }
            )
            continue
        by_action[result["action"]] += 1
        scored.append(result)

    buy_count = sum(1 for result in scored if result["is_correct"] is not None)
    correct_count = sum(1 for result in scored if result["is_correct"] is True)
    gross_profit_total = round(
        sum(max(result["gross_pnl"] or 0.0, 0.0) for result in scored),
        4,
    )
    gross_loss_total = round(
        sum(abs(min(result["gross_pnl"] or 0.0, 0.0)) for result in scored),
        4,
    )
    net_pnl = round(
        sum(result["gross_pnl"] or 0.0 for result in scored),
        4,
    )
    fees_total = round(
        sum(result["fees_paid"] or 0.0 for result in scored),
        4,
    )
    fee_adjusted_pnl = round(
        sum(result["net_pnl"] or 0.0 for result in scored),
        4,
    )
    side_counts = {action: by_action.get(action, 0) for action in VALID_REPLAY_ACTIONS}
    total_realized_return = round(
        sum(result["realized_return"] or 0.0 for result in scored),
        4,
    )
    return {
        "summary": {
            "answers_scored": len(scored),
            "buys": buy_count,
            "correct": correct_count,
            "incorrect": sum(1 for result in scored if result["is_correct"] is False),
            "skipped": sum(1 for result in scored if result["action"] == "SKIP"),
            "accuracy": round(correct_count / buy_count, 4) if buy_count else None,
            "win_rate": round(correct_count / buy_count, 4) if buy_count else None,
            "points_total": sum(result["points"] for result in scored),
            "realized_return_total": total_realized_return,
            "gross_profit_total": gross_profit_total,
            "gross_loss_total": gross_loss_total,
            "net_pnl": net_pnl,
            "fees_total": fees_total,
            "fee_adjusted_pnl": fee_adjusted_pnl,
            "position_size_total": round(sum(result["position_size"] or 0.0 for result in scored), 4),
            "contracts_total": round(sum(result["contracts"] or 0.0 for result in scored), 4),
            "fee_model": {
                "profit_fee_rate": fee_model.profit_fee_rate,
                "notional_fee_rate": fee_model.notional_fee_rate,
                "slippage_bps": fee_model.slippage_bps,
                "late_entry_penalty_rate": fee_model.late_entry_penalty_rate,
                "notes": "Conservative replay model: optional entry-notional fees, entry slippage, late-entry penalty, plus profit fees on positive gross profit.",
            },
            "side_counts": side_counts,
            "by_action": side_counts,
            "missing_answer_keys": len(missing_answer_keys),
            "invalid_answers": len(invalid_answers),
        },
        "results": scored,
        "missing_answer_keys": missing_answer_keys,
        "invalid_answers": invalid_answers,
    }


def _build_replay_id(record: WeatherSampleRecord) -> str:
    payload = json.dumps(
        [
            record.sample_kind,
            record.market_id,
            record.category,
            record.question,
            record.observed_at,
            record.resolved_at,
        ],
        sort_keys=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
    return f"wxr_{digest}"


def _build_city_context(
    city_id: str,
    primary_source_id: str | None,
    registry: WeatherRegistry,
) -> dict[str, Any]:
    city = registry.get_city(city_id)
    return {
        "city_id": city["city_id"],
        "city": city["city"],
        "state": city["state"],
        "timezone": city["timezone"],
        "default_market_types": list(city.get("default_market_types", [])),
        "primary_source_id": primary_source_id,
    }


def _build_price_context(
    yes_price: float | None,
    no_price: float | None,
    volume: float | None,
) -> dict[str, Any]:
    normalized_yes = _float_or_none(yes_price)
    normalized_no = _float_or_none(no_price)
    if normalized_yes is None and normalized_no is not None and 0 <= normalized_no <= 1:
        normalized_yes = round(1 - normalized_no, 4)
    if normalized_no is None and normalized_yes is not None and 0 <= normalized_yes <= 1:
        normalized_no = round(1 - normalized_yes, 4)
    return {
        "yes_price": normalized_yes,
        "no_price": normalized_no,
        "volume": _float_or_none(volume),
    }


def _compact_metadata(record: WeatherSampleRecord) -> dict[str, Any]:
    metadata = {
        "sample_kind": record.sample_kind,
        "source_path": record.source_path,
        "market_type": normalized_market_type(record.question),
    }
    for key in ("market_subtitle", "yes_subtitle", "no_subtitle", "starts_at", "ingested_at"):
        value = record.metadata.get(key) if isinstance(record.metadata, Mapping) else None
        if value is not None:
            metadata[key] = value
    return metadata


def _answer_action(answer: Mapping[str, Any]) -> str:
    for key in ("action", "direction", "recommendation"):
        value = answer.get(key)
        if value:
            return str(value)
    return "SKIP"


def _normalized_outcome(value: object) -> str | None:
    if value is None or value == "":
        return None
    return str(value).strip().upper()


def _float_or_none(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _positive_float(value: object, *, label: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"invalid numeric value for {label}")
    if normalized <= 0:
        raise ValueError(f"{label} must be positive")
    return normalized


def _answer_contracts(answer: Mapping[str, Any]) -> float | None:
    for key in ("contracts", "contract_count", "quantity", "qty"):
        if key in answer and answer.get(key) not in (None, ""):
            return _positive_float(answer.get(key), label=key)
    return None


def _answer_position_size(answer: Mapping[str, Any]) -> float | None:
    for key in ("position_size", "size", "amount", "stake"):
        if key in answer and answer.get(key) not in (None, ""):
            return _positive_float(answer.get(key), label=key)
    return None


def _resolve_position(
    entry_price: float,
    *,
    contracts: float | None,
    position_size: float | None,
) -> tuple[float, float]:
    if not (0 < entry_price < 1):
        raise ValueError("entry_price must be between 0 and 1")

    # Backward-compatible default: a bare replay answer represents one contract.
    if contracts is None and position_size is None:
        contracts = 1.0
        position_size = round(entry_price, 4)
    elif contracts is None and position_size is not None:
        contracts = round(position_size / entry_price, 4)
    elif contracts is not None and position_size is None:
        position_size = round(contracts * entry_price, 4)
    else:
        implied_position_size = round(contracts * entry_price, 4)
        if abs(implied_position_size - position_size) > 0.01:
            raise ValueError("contracts and position_size disagree for the entry price")
        position_size = round(position_size, 4)
        contracts = round(contracts, 4)

    return round(contracts, 4), round(position_size, 4)
