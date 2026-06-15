"""Read-only market resolution backfill for paper shadow scoreboard replay."""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from bot.file_ops import atomic_write_json, rewrite_jsonl

KALSHI_MARKET_API = "https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}"
USER_AGENT = "prediction-bot-scoreboard-resolution-backfill/1.0"
SCHEMA_NAME = "scoreboard_resolution_backfill"
SCHEMA_VERSION = 1
REPORT_SCHEMA_NAME = "scoreboard_resolution_backfill_report"
REPORT_SCHEMA_VERSION = 1
MARKET_TICKER_RE = re.compile(r"\bKX[A-Z0-9]+-\d{2}[A-Z]{3}\d{2}(?:-[A-Z0-9.]+)?\b")
MARKET_ID_KEYS = {
    "market_id",
    "ticker",
    "market_ticker",
    "market",
}


@dataclass(frozen=True)
class MarketRef:
    market_id: str
    source_path: str
    line_number: int
    source_key: str
    shared_candidate_id: str | None = None
    run_id: str | None = None


@dataclass(frozen=True)
class ScoreboardResolutionBackfillResult:
    output_path: Path
    report_path: Path
    resolution_rows: list[dict[str, Any]]
    report: dict[str, Any]


MarketFetcher = Callable[[str], Mapping[str, Any] | None]
SleepFn = Callable[[float], None]


def backfill_scoreboard_resolutions(
    input_paths: Iterable[str | Path],
    *,
    output_path: str | Path,
    report_path: str | Path | None = None,
    fetch_market: MarketFetcher | None = None,
    include_unresolved: bool = False,
    max_markets: int | None = None,
    fetched_at: str | datetime | None = None,
    max_fetch_attempts: int = 3,
    retry_delay_seconds: float = 1.0,
    sleep_fn: SleepFn | None = None,
) -> ScoreboardResolutionBackfillResult:
    """Fetch finalized outcomes for market ids found in historical scoreboard inputs.

    The function only writes derived resolution/report artifacts. It never edits the
    input JSONL files or paper wallet/accounting state.
    """

    paths = [Path(path) for path in input_paths]
    if not paths:
        raise ValueError("at least one input path is required")
    output = Path(output_path)
    report = Path(report_path) if report_path is not None else output.with_suffix(output.suffix + ".report.json")
    resolved_at = _iso_timestamp(fetched_at or datetime.now(timezone.utc))
    fetch = fetch_market or fetch_kalshi_market

    refs, load_report = extract_market_refs(paths)
    unique_refs = _unique_refs(refs)
    if max_markets is not None:
        unique_refs = unique_refs[: max(0, int(max_markets))]

    resolution_rows: list[dict[str, Any]] = []
    fetch_errors: list[dict[str, Any]] = []
    retryable_fetch_errors: list[dict[str, Any]] = []
    source_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    outcome_counts: Counter[str] = Counter()
    unresolved_count = 0
    attempts_by_market: Counter[str] = Counter()
    pending_refs = list(unique_refs)
    sleep = sleep_fn or time.sleep
    max_attempts = max(1, int(max_fetch_attempts or 1))
    base_retry_delay = max(0.0, float(retry_delay_seconds or 0.0))

    while pending_refs:
        retry_refs: list[MarketRef] = []
        retry_after_values: list[float] = []
        for ref in pending_refs:
            attempts_by_market[ref.market_id] += 1
            attempt = attempts_by_market[ref.market_id]
            try:
                market = dict(fetch(ref.market_id) or {})
            except Exception as exc:
                retry_after = _retry_after_seconds(exc)
                if _is_retryable_fetch_error(exc) and attempt < max_attempts:
                    retryable_fetch_errors.append(
                        {
                            "market_id": ref.market_id,
                            "attempt": attempt,
                            "error": repr(exc),
                            "retry_after_seconds": retry_after,
                        }
                    )
                    retry_refs.append(ref)
                    if retry_after is not None:
                        retry_after_values.append(retry_after)
                    continue
                fetch_errors.append(
                    {
                        "market_id": ref.market_id,
                        "error": repr(exc),
                        "attempts": attempt,
                        "retryable": _is_retryable_fetch_error(exc),
                    }
                )
                continue

            outcome, source, metadata = normalized_market_outcome(market)
            source_counts[source] += 1
            status_counts[str(market.get("status") or "unknown")] += 1
            if outcome:
                outcome_counts[outcome] += 1
            else:
                unresolved_count += 1
                if not include_unresolved:
                    continue

            resolution_rows.append(
                build_backfill_resolution_row(
                    ref,
                    market=market,
                    outcome=outcome,
                    source=source,
                    metadata=metadata,
                    resolved_at=resolved_at,
                )
            )

        if not retry_refs:
            break
        retry_delay = max(retry_after_values) if retry_after_values else base_retry_delay
        if retry_delay > 0:
            sleep(retry_delay)
        pending_refs = retry_refs

    summary = {
        "schema_name": REPORT_SCHEMA_NAME,
        "schema_version": REPORT_SCHEMA_VERSION,
        "mode": "derived_read_only",
        "mutates_input_ledgers": False,
        "mutates_wallet_state": False,
        "input_paths": [str(path) for path in paths],
        "output_path": str(output),
        "report_path": str(report),
        "input_rows_read": load_report["rows_read"],
        "invalid_json_rows": load_report["invalid_json_rows"],
        "non_object_rows": load_report["non_object_rows"],
        "missing_input_paths": load_report["missing_input_paths"],
        "market_refs_found": len(refs),
        "unique_markets_found": len(_unique_refs(refs)),
        "markets_requested": len(unique_refs),
        "resolution_rows_written": len(resolution_rows),
        "resolved_market_count": sum(1 for row in resolution_rows if _row_outcome(row) is not None),
        "unresolved_market_count": unresolved_count,
        "fetch_error_count": len(fetch_errors),
        "fetch_error_samples": fetch_errors[:10],
        "retryable_fetch_error_count": len(retryable_fetch_errors),
        "retryable_fetch_error_samples": retryable_fetch_errors[:10],
        "max_fetch_attempts": max_attempts,
        "by_status": dict(sorted(status_counts.items())),
        "by_outcome": dict(sorted(outcome_counts.items())),
        "by_resolution_source": dict(sorted(source_counts.items())),
        "include_unresolved": bool(include_unresolved),
        "fetched_at": resolved_at,
    }

    rewrite_jsonl(output, resolution_rows)
    atomic_write_json(report, summary)
    return ScoreboardResolutionBackfillResult(
        output_path=output,
        report_path=report,
        resolution_rows=resolution_rows,
        report=summary,
    )


def extract_market_refs(input_paths: Iterable[str | Path]) -> tuple[list[MarketRef], dict[str, Any]]:
    refs: list[MarketRef] = []
    rows_read = 0
    invalid_json_rows = 0
    non_object_rows = 0
    missing_input_paths: list[str] = []

    for raw_path in input_paths:
        path = Path(raw_path)
        if not path.exists():
            missing_input_paths.append(str(path))
            continue
        with path.open("r", encoding="utf-8") as fh:
            for line_number, line in enumerate(fh, 1):
                stripped = line.strip()
                if not stripped:
                    continue
                rows_read += 1
                try:
                    row = json.loads(stripped)
                except json.JSONDecodeError:
                    invalid_json_rows += 1
                    continue
                if not isinstance(row, dict):
                    non_object_rows += 1
                    continue
                refs.extend(_market_refs_from_row(row, source_path=str(path), line_number=line_number))

    return refs, {
        "rows_read": rows_read,
        "invalid_json_rows": invalid_json_rows,
        "non_object_rows": non_object_rows,
        "missing_input_paths": missing_input_paths,
    }


def fetch_kalshi_market(market_id: str) -> Mapping[str, Any] | None:
    req = urllib.request.Request(
        KALSHI_MARKET_API.format(ticker=market_id),
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        payload = json.load(response)
    market = payload.get("market") if isinstance(payload, dict) else None
    return market if isinstance(market, Mapping) else None


def _is_retryable_fetch_error(exc: Exception) -> bool:
    status = getattr(exc, "code", None) or getattr(exc, "status", None) or getattr(exc, "status_code", None)
    try:
        status_int = int(status)
    except (TypeError, ValueError):
        status_int = None
    return status_int == 429


def _retry_after_seconds(exc: Exception) -> float | None:
    headers = getattr(exc, "headers", None)
    retry_after = headers.get("Retry-After") if headers is not None and hasattr(headers, "get") else None
    if retry_after in (None, ""):
        return None
    try:
        return max(0.0, float(retry_after))
    except (TypeError, ValueError):
        return None


def normalized_market_outcome(market: Mapping[str, Any]) -> tuple[str | None, str, dict[str, Any]]:
    result = str(market.get("result") or "").strip().upper()
    if result in {"YES", "NO", "VOID"}:
        return result, "kalshi_result", {}

    settlement_value = market.get("settlement_value_dollars")
    if settlement_value not in (None, ""):
        try:
            return ("YES" if float(settlement_value) >= 0.5 else "NO"), "kalshi_settlement_value", {}
        except (TypeError, ValueError):
            pass

    return None, "unresolved", {"status": market.get("status"), "result": market.get("result")}


def build_backfill_resolution_row(
    ref: MarketRef,
    *,
    market: Mapping[str, Any],
    outcome: str | None,
    source: str,
    metadata: Mapping[str, Any] | None,
    resolved_at: str,
) -> dict[str, Any]:
    market_id = str(market.get("ticker") or market.get("market_id") or ref.market_id)
    row = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "resolution_id": _resolution_id(ref, outcome=outcome, source=source),
        "market_id": market_id,
        "backfill_source_path": ref.source_path,
        "backfill_source_line_number": ref.line_number,
        "backfill_source_key": ref.source_key,
        "backfill_mode": "derived_read_only",
        "non_mutating": True,
        "market_status": market.get("status"),
        "kalshi_result": market.get("result"),
        "settlement_value_dollars": market.get("settlement_value_dollars"),
        "close_time": market.get("close_time"),
        "expiration_time": market.get("expiration_time"),
        "resolved_at": resolved_at,
        "resolution": {
            "outcome": outcome,
            "resolved_at": resolved_at if outcome else None,
            "source": source,
            "metadata": dict(metadata or {}),
        },
    }
    if ref.shared_candidate_id:
        row["shared_candidate_id"] = ref.shared_candidate_id
    if ref.run_id:
        row["run_id"] = ref.run_id
    return row


def _market_refs_from_row(row: Mapping[str, Any], *, source_path: str, line_number: int) -> list[MarketRef]:
    candidates: list[tuple[str, str]] = []
    _collect_market_candidates(row, candidates, path=())
    shared_candidate_id = _optional_text(row.get("shared_candidate_id"))
    run_id = _optional_text(row.get("run_id"))
    refs = [
        MarketRef(
            market_id=market_id,
            source_path=source_path,
            line_number=line_number,
            source_key=source_key,
            shared_candidate_id=shared_candidate_id,
            run_id=run_id,
        )
        for market_id, source_key in candidates
    ]
    return _dedupe_refs_for_row(refs)


def _collect_market_candidates(value: Any, candidates: list[tuple[str, str]], *, path: tuple[str, ...]) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            next_path = (*path, key_text)
            if key_text in MARKET_ID_KEYS and isinstance(child, str):
                for market_id in _extract_market_ids(child):
                    candidates.append((market_id, ".".join(next_path)))
            elif key_text.endswith("market_id") and isinstance(child, str):
                for market_id in _extract_market_ids(child):
                    candidates.append((market_id, ".".join(next_path)))
            _collect_market_candidates(child, candidates, path=next_path)
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            _collect_market_candidates(child, candidates, path=(*path, str(idx)))


def _extract_market_ids(value: str) -> list[str]:
    matches = [match.group(0) for match in MARKET_TICKER_RE.finditer(value)]
    if matches:
        return matches
    value = value.strip()
    if value.startswith("KX") and "-" in value:
        return [value]
    return []


def _dedupe_refs_for_row(refs: Iterable[MarketRef]) -> list[MarketRef]:
    seen: set[str] = set()
    out: list[MarketRef] = []
    for ref in refs:
        if ref.market_id in seen:
            continue
        seen.add(ref.market_id)
        out.append(ref)
    return out


def _unique_refs(refs: Iterable[MarketRef]) -> list[MarketRef]:
    seen: set[str] = set()
    out: list[MarketRef] = []
    for ref in refs:
        if ref.market_id in seen:
            continue
        seen.add(ref.market_id)
        out.append(ref)
    return out


def _resolution_id(ref: MarketRef, *, outcome: str | None, source: str) -> str:
    digest = hashlib.sha256(
        "|".join(
            [
                ref.market_id,
                ref.shared_candidate_id or "",
                ref.run_id or "",
                outcome or "",
                source,
            ]
        ).encode("utf-8")
    ).hexdigest()[:20]
    return f"scoreboard_resolution_backfill:{digest}"


def _row_outcome(row: Mapping[str, Any]) -> str | None:
    resolution = row.get("resolution") if isinstance(row.get("resolution"), Mapping) else {}
    outcome = str(resolution.get("outcome") or row.get("outcome") or "").upper()
    return outcome if outcome in {"YES", "NO", "VOID"} else None


def _optional_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _iso_timestamp(value: str | datetime) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


__all__ = [
    "MarketRef",
    "ScoreboardResolutionBackfillResult",
    "backfill_scoreboard_resolutions",
    "build_backfill_resolution_row",
    "extract_market_refs",
    "fetch_kalshi_market",
    "normalized_market_outcome",
]
