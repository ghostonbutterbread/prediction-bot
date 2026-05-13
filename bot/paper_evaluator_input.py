"""Read-only shared-candidate inputs for future paper A/B evaluation."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from bot.file_ops import load_jsonl
from bot.paper_wallets import (
    BETA_PAPER_WALLET_ID,
    STABLE_PAPER_WALLET_ID,
    resolve_paper_wallet_contract,
)
from bot.shared_market_feed import SCHEMA_NAME, shared_candidate_from_market_snapshot_row, shared_candidate_id_from_row


@dataclass(frozen=True, slots=True)
class SharedCandidateSkip:
    row_index: int
    reason_code: str
    market_id: str | None = None
    shared_candidate_id: str | None = None


@dataclass(frozen=True, slots=True)
class SharedCandidateMarket:
    id: str
    exchange: str
    question: str
    category: str
    yes_price: float | int | None
    no_price: float | int | None
    volume: float | int | None
    liquidity: float | int | None
    metadata: dict[str, Any]
    closes_at: str | None = None


@dataclass(frozen=True, slots=True)
class PaperEvaluatorCandidateInput:
    wallet_id: str
    policy_id: str
    wallet_contract: dict[str, Any]
    shared_candidate_id: str
    candidate_dataset_path: str
    candidate_feed_read_only: bool
    observed_at: str | None
    signal: dict[str, Any]
    shared_candidate: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SharedCandidatePaperInputLoadResult:
    candidate_dataset_path: str
    loaded_row_count: int
    accepted_candidate_count: int
    skipped_rows: tuple[SharedCandidateSkip, ...]
    inputs_by_shared_candidate_id: dict[str, dict[str, PaperEvaluatorCandidateInput]]


def load_shared_candidate_paper_inputs(
    candidate_dataset_path: str | Path,
    *,
    config: Mapping[str, Any] | None = None,
    data_dir: str | Path | None = None,
    wallet_ids: tuple[str, ...] | list[str] | None = None,
    session_id: str | None = None,
) -> SharedCandidatePaperInputLoadResult:
    """Load one shared-candidate dataset and fan it out to stable/beta wallet inputs."""

    dataset_path = Path(candidate_dataset_path)
    resolved_wallet_ids = tuple(wallet_ids or (STABLE_PAPER_WALLET_ID, BETA_PAPER_WALLET_ID))
    wallet_contracts = {
        wallet_id: resolve_paper_wallet_contract(
            config,
            wallet_id=wallet_id,
            session_id=session_id,
            data_dir=data_dir,
        )
        for wallet_id in resolved_wallet_ids
    }
    canonical_wallet_contracts = {
        wallet_id: resolve_paper_wallet_contract(
            config,
            wallet_id=wallet_id,
            session_id=session_id,
            data_dir=data_dir,
        )
        for wallet_id in (STABLE_PAPER_WALLET_ID, BETA_PAPER_WALLET_ID)
    }
    _assert_candidate_dataset_is_separate_from_wallet_roots(
        dataset_path,
        {**canonical_wallet_contracts, **wallet_contracts}.values(),
    )

    rows = load_jsonl(dataset_path)
    inputs_by_candidate_id: dict[str, dict[str, PaperEvaluatorCandidateInput]] = {}
    skipped_rows: list[SharedCandidateSkip] = []

    for row_index, row in enumerate(rows, start=1):
        normalized, skip = _normalize_shared_candidate_row(row_index, row)
        if skip is not None:
            skipped_rows.append(skip)
            continue
        assert normalized is not None

        shared_candidate_id = normalized.shared_candidate_id
        if shared_candidate_id in inputs_by_candidate_id:
            skipped_rows.append(
                SharedCandidateSkip(
                    row_index=row_index,
                    reason_code="duplicate_shared_candidate_id",
                    market_id=normalized.market_id,
                    shared_candidate_id=shared_candidate_id,
                )
            )
            continue

        signal = _build_signal_from_normalized_candidate(
            normalized.shared_candidate,
            normalized.source_row,
            shared_candidate_id=shared_candidate_id,
            candidate_dataset_path=dataset_path,
        )
        inputs_by_candidate_id[shared_candidate_id] = {
            wallet_id: PaperEvaluatorCandidateInput(
                wallet_id=contract.wallet_id,
                policy_id=contract.policy_id,
                wallet_contract=contract.to_dict(),
                shared_candidate_id=shared_candidate_id,
                candidate_dataset_path=str(dataset_path),
                candidate_feed_read_only=True,
                observed_at=_optional_text(normalized.shared_candidate.get("observed_at")),
                signal=copy.deepcopy(signal),
                shared_candidate=copy.deepcopy(normalized.shared_candidate),
            )
            for wallet_id, contract in wallet_contracts.items()
        }

    return SharedCandidatePaperInputLoadResult(
        candidate_dataset_path=str(dataset_path),
        loaded_row_count=len(rows),
        accepted_candidate_count=len(inputs_by_candidate_id),
        skipped_rows=tuple(skipped_rows),
        inputs_by_shared_candidate_id=inputs_by_candidate_id,
    )


@dataclass(frozen=True, slots=True)
class _NormalizedSharedCandidateRow:
    source_row: dict[str, Any]
    shared_candidate: dict[str, Any]
    shared_candidate_id: str
    market_id: str | None


def _normalize_shared_candidate_row(
    row_index: int,
    row: Any,
) -> tuple[_NormalizedSharedCandidateRow | None, SharedCandidateSkip | None]:
    if not isinstance(row, dict):
        return None, SharedCandidateSkip(row_index=row_index, reason_code="invalid_row")

    if str(row.get("schema_name") or "") == SCHEMA_NAME:
        shared_candidate = dict(row)
        candidate_id = _optional_text(shared_candidate.get("candidate_id"))
        if candidate_id is None:
            return None, SharedCandidateSkip(
                row_index=row_index,
                reason_code="missing_usable_shared_candidate_id",
                market_id=_optional_text(shared_candidate.get("market_id")),
            )
        return (
            _NormalizedSharedCandidateRow(
                source_row=dict(row),
                shared_candidate=shared_candidate,
                shared_candidate_id=candidate_id,
                market_id=_optional_text(shared_candidate.get("market_id")),
            ),
            None,
        )

    direct_id = shared_candidate_id_from_row(row)
    embedded_shared = row.get("shared_candidate") if isinstance(row.get("shared_candidate"), dict) else None
    embedded_id = _optional_text(embedded_shared.get("candidate_id")) if embedded_shared else None
    if direct_id is not None and embedded_id is not None and str(direct_id) != embedded_id:
        return None, SharedCandidateSkip(
            row_index=row_index,
            reason_code="shared_candidate_id_mismatch",
            market_id=_candidate_market_id(row),
            shared_candidate_id=str(direct_id),
        )

    derived_shared_candidate = shared_candidate_from_market_snapshot_row(row) if _can_derive_candidate_id(row) else None
    derived_id = _optional_text(derived_shared_candidate.get("candidate_id")) if isinstance(derived_shared_candidate, dict) else None

    candidate_id = _optional_text(direct_id or embedded_id)
    if candidate_id is not None and derived_id is not None and candidate_id != derived_id:
        return None, SharedCandidateSkip(
            row_index=row_index,
            reason_code="shared_candidate_id_mismatch",
            market_id=_candidate_market_id(row),
            shared_candidate_id=candidate_id,
        )
    if candidate_id is None:
        if derived_shared_candidate is None:
            return None, SharedCandidateSkip(
                row_index=row_index,
                reason_code="missing_usable_shared_candidate_id",
                market_id=_candidate_market_id(row),
            )
        candidate_id = derived_id
        if candidate_id is None:
            return None, SharedCandidateSkip(
                row_index=row_index,
                reason_code="missing_usable_shared_candidate_id",
                market_id=_candidate_market_id(row),
            )

    if isinstance(embedded_shared, dict) and str(embedded_shared.get("schema_name") or "") == SCHEMA_NAME:
        shared_candidate = dict(embedded_shared)
    else:
        shared_candidate = dict(derived_shared_candidate or shared_candidate_from_market_snapshot_row(row))
    shared_candidate["candidate_id"] = candidate_id

    return (
        _NormalizedSharedCandidateRow(
            source_row=dict(row),
            shared_candidate=shared_candidate,
            shared_candidate_id=candidate_id,
            market_id=_candidate_market_id(row) or _optional_text(shared_candidate.get("market_id")),
        ),
        None,
    )


def _build_signal_from_normalized_candidate(
    shared_candidate: Mapping[str, Any],
    source_row: Mapping[str, Any],
    *,
    shared_candidate_id: str,
    candidate_dataset_path: Path,
) -> dict[str, Any]:
    decision = _mapping(shared_candidate.get("decision"))
    prices = _mapping(shared_candidate.get("prices"))
    evidence = _mapping(shared_candidate.get("evidence"))
    market = _mapping(shared_candidate.get("market"))
    artifact = _mapping(source_row.get("decision_artifact"))
    artifact_signal = _mapping(artifact.get("strategy_signal"))
    source_context = _mapping(artifact.get("source_context"))
    route = _mapping(market.get("route"))
    direction = _signal_direction(source_row, shared_candidate, artifact_signal)
    market_price = _coalesce(
        source_row.get("market_price"),
        prices.get("market_price"),
        _market_price_for_direction(direction, prices),
    )

    signal = dict(artifact_signal)
    signal.update(
        {
            "shared_candidate_id": shared_candidate_id,
            "candidate_dataset_path": str(candidate_dataset_path),
            "candidate_feed_read_only": True,
            "candidate_source_runtime": _optional_text(shared_candidate.get("source_runtime")),
            "candidate_provenance": _optional_text(shared_candidate.get("provenance")),
            "candidate_observed_at": _optional_text(shared_candidate.get("observed_at")),
            "snapshot_as_of": _optional_text(shared_candidate.get("snapshot_as_of")),
            "snapshot_ttl_seconds": shared_candidate.get("snapshot_ttl_seconds"),
            "market_id": _coalesce(source_row.get("market_id"), shared_candidate.get("market_id"), market.get("id"), ""),
            "question": _coalesce(source_row.get("question"), market.get("question"), ""),
            "exchange": _coalesce(source_row.get("exchange"), market.get("exchange"), "unknown"),
            "category": _coalesce(source_row.get("series"), market.get("category"), market.get("series"), ""),
            "group": _coalesce(source_row.get("group"), market.get("group")),
            "series": _coalesce(source_row.get("series"), market.get("series")),
            "event_ticker": _coalesce(source_row.get("event_ticker"), market.get("event_ticker")),
            "series_ticker": market.get("series_ticker"),
            "market_route": route or _mapping(source_row.get("market_route")) or _mapping(source_context.get("market_route")),
            "market_family": _coalesce(market.get("family"), route.get("family")),
            "direction": direction,
            "model_probability": _coalesce(source_row.get("model_probability"), decision.get("model_probability")),
            "market_price": market_price,
            "yes_market_price": _coalesce(source_row.get("yes_market_price"), prices.get("yes_market_price"), prices.get("yes_price")),
            "no_market_price": _coalesce(source_row.get("no_market_price"), prices.get("no_market_price"), prices.get("no_price")),
            "yes_price": _coalesce(prices.get("yes_price"), source_row.get("yes_price"), source_row.get("yes_market_price")),
            "no_price": _coalesce(prices.get("no_price"), source_row.get("no_price"), source_row.get("no_market_price")),
            "best_yes_bid": prices.get("best_yes_bid"),
            "best_yes_ask": prices.get("best_yes_ask"),
            "best_no_bid": prices.get("best_no_bid"),
            "best_no_ask": prices.get("best_no_ask"),
            "confidence": _coalesce(source_row.get("confidence"), decision.get("confidence")),
            "edge": _coalesce(source_row.get("edge"), decision.get("edge")),
            "signals": _coalesce(source_row.get("signals"), evidence.get("signals"), {}),
            "signal_details": _coalesce(source_row.get("signal_details"), {}),
            "station_id": _coalesce(artifact_signal.get("station_id"), evidence.get("station_id")),
            "source_as_of": _coalesce(artifact_signal.get("source_as_of"), shared_candidate.get("snapshot_as_of")),
            "source_mode": _coalesce(artifact_signal.get("source_mode"), evidence.get("source_mode")),
            "source_agreement_score": _coalesce(artifact_signal.get("source_agreement_score"), evidence.get("source_agreement_score")),
            "weather_confidence_score": _coalesce(artifact_signal.get("weather_confidence_score"), evidence.get("weather_confidence_score")),
            "distribution_probability": _coalesce(artifact_signal.get("distribution_probability"), evidence.get("distribution_probability")),
            "liquidity": _coalesce(artifact_signal.get("liquidity"), source_row.get("liquidity"), market.get("volume")),
        }
    )
    signal["_market"] = SharedCandidateMarket(
        id=str(_coalesce(market.get("id"), shared_candidate.get("market_id"), source_row.get("market_id"), "")),
        exchange=str(_coalesce(market.get("exchange"), source_row.get("exchange"), "unknown")),
        question=str(_coalesce(market.get("question"), source_row.get("question"), "")),
        category=str(_coalesce(market.get("category"), market.get("series"), source_row.get("series"), "")),
        yes_price=_coalesce(prices.get("yes_price"), prices.get("yes_market_price"), source_row.get("yes_price"), source_row.get("yes_market_price")),
        no_price=_coalesce(prices.get("no_price"), prices.get("no_market_price"), source_row.get("no_price"), source_row.get("no_market_price")),
        volume=_coalesce(market.get("volume"), source_row.get("volume")),
        liquidity=_coalesce(signal.get("liquidity"), market.get("volume"), source_row.get("volume")),
        closes_at=_optional_text(market.get("closes_at")),
        metadata={
            "market_group": _coalesce(market.get("group"), source_row.get("group")),
            "market_family": _coalesce(market.get("family"), route.get("family")),
            "series": _coalesce(market.get("series"), source_row.get("series")),
            "series_ticker": _optional_text(market.get("series_ticker")),
            "event_ticker": _optional_text(_coalesce(market.get("event_ticker"), source_row.get("event_ticker"))),
            "market_route": route or _mapping(source_row.get("market_route")) or _mapping(source_context.get("market_route")),
        },
    )
    return signal


def _assert_candidate_dataset_is_separate_from_wallet_roots(
    candidate_dataset_path: Path,
    contracts: Any,
) -> None:
    dataset = candidate_dataset_path.resolve(strict=False)
    for contract in contracts:
        wallet_root = contract.root_dir.resolve(strict=False)
        if dataset == wallet_root or wallet_root in dataset.parents:
            raise ValueError(
                "candidate dataset path must stay outside paper wallet roots "
                f"(dataset={dataset}, wallet_id={contract.wallet_id}, wallet_root={wallet_root})"
            )


def _can_derive_candidate_id(row: Mapping[str, Any]) -> bool:
    market_id = _candidate_market_id(row)
    observed_at = _optional_text(row.get("observed_at") or row.get("timestamp"))
    return market_id not in (None, "") and observed_at is not None


def _candidate_market_id(row: Mapping[str, Any]) -> str | None:
    market_id = _optional_text(row.get("market_id") or row.get("snapshot_key"))
    if market_id is not None:
        return market_id
    shared = row.get("shared_candidate")
    if isinstance(shared, dict):
        return _optional_text(shared.get("market_id"))
    return None


def _signal_direction(
    source_row: Mapping[str, Any],
    shared_candidate: Mapping[str, Any],
    artifact_signal: Mapping[str, Any],
) -> str:
    candidates = (
        artifact_signal.get("direction"),
        source_row.get("direction"),
        _mapping(shared_candidate.get("decision")).get("direction"),
        _mapping(shared_candidate.get("main_decision")).get("action"),
        _mapping(shared_candidate.get("normal_decision")).get("action"),
        _mapping(source_row.get("main_decision")).get("action"),
        _mapping(source_row.get("normal_decision")).get("action"),
        _mapping(source_row.get("decision_artifact")).get("final_action"),
    )
    for candidate in candidates:
        text = str(candidate or "").strip().upper()
        if text:
            return text
    return "SKIP"


def _market_price_for_direction(direction: str, prices: Mapping[str, Any]) -> Any:
    normalized = str(direction or "").upper()
    if normalized == "BUY_NO":
        return _coalesce(prices.get("no_market_price"), prices.get("no_price"))
    return _coalesce(prices.get("yes_market_price"), prices.get("yes_price"))


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _optional_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


__all__ = [
    "PaperEvaluatorCandidateInput",
    "SharedCandidatePaperInputLoadResult",
    "SharedCandidateSkip",
    "load_shared_candidate_paper_inputs",
]
