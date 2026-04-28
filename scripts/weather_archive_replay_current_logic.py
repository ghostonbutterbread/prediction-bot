#!/usr/bin/env python3
"""Offline replay of current price-only weather logic against local archives."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.config import load_config  # noqa: E402
from bot.shared_core import AccountState, TradeContext, build_trade_decision  # noqa: E402
from bot.shared_core.weather_risk import (  # noqa: E402
    assess_weather_market_risk,
    build_weather_source_confidence_evidence,
)
from bot.strategies.enhanced import EnhancedStrategyEngine  # noqa: E402
from bot.weather.replay import ReplayFeeModel, score_replay_answer  # noqa: E402


DEFAULT_KALSHI = "data/historical/kalshi.csv"
DEFAULT_OHLCV = "data/historical/ohlcv_30days.csv"
DEFAULT_SUMMARY = "data/summaries/weather_archive_replay_current_logic_summary.json"
DEFAULT_ROWS = "data/summaries/weather_archive_replay_current_logic_rows.csv"
TEMPERATURE_PREFIXES = ("KXHIGH", "KXHIGHT", "KXLOW", "KXLOWT")
RESOLVED_OUTCOMES = {"YES", "NO"}


@dataclass(slots=True)
class ArchivedMarket:
    market_ticker: str
    question: str
    market_subtitle: str
    yes_subtitle: str
    no_subtitle: str
    result: str
    end_dt: str
    family: str


class PriceOnlyReplayEngine(EnhancedStrategyEngine):
    """Current strategy engine constrained to price-only offline logic."""

    def _live_data_signal(self, market) -> None:  # type: ignore[override]
        return None

    def _volume_signal(self, market) -> None:  # type: ignore[override]
        return None

    def _time_signal(self, market) -> None:  # type: ignore[override]
        return None

    def analyze_price_only_market(self, market) -> tuple[dict[str, Any] | None, str | None, list[str], dict[str, Any]]:
        price_signal = self._price_signal(market, None)
        if not price_signal:
            return None, "no_price_signal", [], {}

        validation = self.validator.validate(price_signal, market, "price")
        predicted_prob = float(price_signal.get("predicted_prob", 0.5) or 0.5)
        confidence = float(price_signal.get("confidence", 0.5) or 0.5)
        yes_price = float(market.yes_price)
        no_price = float(market.no_price)
        yes_edge = predicted_prob - yes_price
        no_edge = (1.0 - predicted_prob) - no_price
        best_direction = "BUY_YES" if yes_edge >= no_edge else "BUY_NO"
        best_edge = yes_edge if yes_edge >= no_edge else no_edge
        raw_details = {
            "price_predicted_prob": round(predicted_prob, 4),
            "price_confidence": round(confidence, 4),
            "price_yes_edge": round(yes_edge, 4),
            "price_no_edge": round(no_edge, 4),
            "price_best_direction": best_direction,
            "price_best_edge": round(best_edge, 4),
        }
        if not validation.accepted:
            return None, validation.rejection_reason or "price_signal_rejected", list(validation.warnings), raw_details

        predicted_prob = float(validation.adjusted_prob)
        confidence = float(validation.adjusted_confidence)
        yes_edge = predicted_prob - yes_price
        no_edge = (1.0 - predicted_prob) - no_price

        if yes_edge >= no_edge:
            direction = "BUY_YES"
            edge = yes_edge
            entry_price = yes_price
        else:
            direction = "BUY_NO"
            edge = no_edge
            entry_price = no_price

        if edge < self.min_edge:
            return None, "edge_below_threshold", list(validation.warnings), raw_details
        if confidence < self.min_confidence:
            return None, "confidence_below_threshold", list(validation.warnings), raw_details

        return {
            "market_id": market.id,
            "exchange": market.exchange,
            "direction": direction,
            "model_probability": round(predicted_prob, 4),
            "market_price": round(entry_price, 4),
            "yes_market_price": round(yes_price, 4),
            "no_market_price": round(no_price, 4),
            "edge": round(edge, 4),
            "confidence": round(confidence, 4),
            "signals": {"price": round(predicted_prob, 4)},
            "question": market.question,
            "warnings": list(validation.warnings),
        }, None, list(validation.warnings), raw_details


class FixedNotionalSizer:
    def __init__(self, notional_usd: float):
        self.notional_usd = float(notional_usd)

    def calculate(self, win_probability: float, entry_price: float, bankroll: float) -> float:
        return round(max(0.0, self.notional_usd), 4)


class PassThroughRiskPolicy:
    def check_trade(self, signal: dict[str, Any], position_size: float, *, available_cash: float | None = None):
        return SimpleNamespace(
            approved=True,
            reason="Approved",
            adjusted_size=round(float(position_size or 0.0), 4),
            risk_score=0.0,
            warnings=[],
            metadata={"effective_tradable_cash": available_cash},
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kalshi", default=DEFAULT_KALSHI, help="Resolved Kalshi archive CSV.")
    parser.add_argument("--ohlcv", default=DEFAULT_OHLCV, help="Daily OHLCV archive CSV.")
    parser.add_argument("--summary-output", default=DEFAULT_SUMMARY, help="Summary JSON output path.")
    parser.add_argument("--rows-output", default=DEFAULT_ROWS, help="Detailed rows CSV output path.")
    parser.add_argument("--limit", type=int, default=0, help="Optional cap on joined OHLCV rows to replay.")
    parser.add_argument("--flat-notional-usd", type=float, default=None, help="Flat notional to score per approved trade.")
    parser.add_argument("--progress-every", type=int, default=2000, help="Progress log interval while replaying.")
    return parser.parse_args()


def _normalized_outcome(value: Any) -> str | None:
    normalized = str(value or "").strip().upper()
    if normalized in RESOLVED_OUTCOMES:
        return normalized
    return None


def _to_float(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _load_replay_config() -> dict[str, Any]:
    try:
        return load_config()
    except Exception:
        return {}


def _temperature_family(ticker: str) -> str | None:
    normalized = str(ticker or "").upper()
    if normalized.startswith(("KXHIGH", "KXHIGHT")):
        return "high-series"
    if normalized.startswith(("KXLOW", "KXLOWT")):
        return "low-series"
    return None


def _volume_proxy(row: dict[str, str]) -> float:
    n_value = _to_float(row.get("n"), 0.0) or 0.0
    b_value = _to_float(row.get("b"), 0.0) or 0.0
    s_value = _to_float(row.get("s"), 0.0) or 0.0
    if n_value > 0:
        return float(n_value)
    if b_value > 0 or s_value > 0:
        return float(b_value + s_value)
    return 0.0


def _load_resolved_temperature_markets(path: Path) -> dict[str, ArchivedMarket]:
    resolved: dict[str, ArchivedMarket] = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            ticker = str(row.get("MARKET_TICKER") or "")
            family = _temperature_family(ticker)
            if family is None:
                continue
            outcome = _normalized_outcome(row.get("RESULT"))
            if outcome is None:
                continue
            resolved[ticker] = ArchivedMarket(
                market_ticker=ticker,
                question=str(row.get("MARKET_TITLE") or "").strip(),
                market_subtitle=str(row.get("MARKET_SUBTITLE") or "").strip(),
                yes_subtitle=str(row.get("YES_SUBTITLE") or "").strip(),
                no_subtitle=str(row.get("NO_SUBTITLE") or "").strip(),
                result=outcome,
                end_dt=str(row.get("END_DT") or "").strip(),
                family=family,
            )
    return resolved


def _build_market_like(archive_market: ArchivedMarket, ohlcv_row: dict[str, str]):
    yes_price = _to_float(ohlcv_row.get("o"))
    if yes_price is None or yes_price <= 0 or yes_price >= 1:
        return None
    return SimpleNamespace(
        id=archive_market.market_ticker,
        exchange="kalshi",
        question=archive_market.question,
        yes_price=round(float(yes_price), 4),
        no_price=round(1.0 - float(yes_price), 4),
        volume=_volume_proxy(ohlcv_row),
        category=archive_market.market_ticker.split("-", 1)[0],
        closes_at=None,
        metadata={"series": archive_market.market_ticker.split("-", 1)[0]},
    )


def _build_weather_signal_context(
    archive_market: ArchivedMarket,
    market_like,
    signal: dict[str, Any] | None,
    ohlcv_row: dict[str, str],
) -> dict[str, Any]:
    context = {
        "market_id": archive_market.market_ticker,
        "ticker": archive_market.market_ticker,
        "question": archive_market.question,
        "title": archive_market.question,
        "market_volume": float(market_like.volume),
        "volume": float(market_like.volume),
        "_market": {"volume": float(market_like.volume)},
        "ohlcv_date": str(ohlcv_row.get("d") or ""),
        "ohlcv_open": market_like.yes_price,
        "ohlcv_close": _to_float(ohlcv_row.get("c")),
        "ohlcv_high": _to_float(ohlcv_row.get("h")),
        "ohlcv_low": _to_float(ohlcv_row.get("l")),
        "ohlcv_n": _to_float(ohlcv_row.get("n"), 0.0) or 0.0,
        "ohlcv_b": _to_float(ohlcv_row.get("b"), 0.0) or 0.0,
        "ohlcv_s": _to_float(ohlcv_row.get("s"), 0.0) or 0.0,
    }
    if signal:
        context.update(
            {
                "direction": signal.get("direction"),
                "model_probability": signal.get("model_probability"),
                "confidence": signal.get("confidence"),
                "signals": signal.get("signals") or {},
            }
        )
    return context


def _build_trade_context(
    signal: dict[str, Any],
    *,
    account_state: AccountState,
    weather_signal: dict[str, Any],
) -> TradeContext:
    return TradeContext(
        exchange=str(signal.get("exchange") or "kalshi"),
        market_id=str(signal.get("market_id") or ""),
        question=str(signal.get("question") or ""),
        direction=str(signal.get("direction") or "BUY_YES"),
        market_price=_to_float(signal.get("market_price")),
        yes_price=_to_float(signal.get("yes_market_price")),
        no_price=_to_float(signal.get("no_market_price")),
        model_probability=_to_float(signal.get("model_probability")),
        edge=_to_float(signal.get("edge")),
        confidence=_to_float(signal.get("confidence")),
        account_state=account_state,
        source_context=weather_signal,
        metadata={},
    )


def _safe_round(value: Any, digits: int = 4) -> float | None:
    number = _to_float(value)
    if number is None:
        return None
    return round(number, digits)


def _normalize_reason_code(reason: str | None) -> str:
    normalized = str(reason or "").strip()
    if not normalized:
        return "unknown_skip_reason"
    lowered = normalized.lower()
    if lowered.startswith("market too thin"):
        return "market_too_thin_no_quoted_book"
    return normalized


def _score_action(
    *,
    action: str,
    outcome: str,
    yes_price: float,
    no_price: float,
    market_id: str,
    flat_notional_usd: float,
    fee_rate: float,
) -> dict[str, Any]:
    return score_replay_answer(
        action,
        {
            "replay_id": f"{market_id}:{action}",
            "market_id": market_id,
            "outcome": outcome,
            "prices": {
                "yes_price": yes_price,
                "no_price": no_price,
            },
        },
        position_size=flat_notional_usd,
        fee_model=ReplayFeeModel(profit_fee_rate=fee_rate),
    )


def _summarize_group(rows: list[dict[str, Any]]) -> dict[str, Any]:
    side_counts = Counter(str(row.get("action") or "SKIP") for row in rows)
    skip_reasons = Counter(str(row.get("reason_code") or "") for row in rows if str(row.get("action")) == "SKIP")
    return {
        "rows": len(rows),
        "accepted_signals": sum(1 for row in rows if bool(row.get("approved"))),
        "buy_yes": side_counts.get("BUY_YES", 0),
        "buy_no": side_counts.get("BUY_NO", 0),
        "skip": side_counts.get("SKIP", 0),
        "gross_pnl": round(sum(float(row.get("gross_pnl") or 0.0) for row in rows), 4),
        "fees_paid": round(sum(float(row.get("fees_paid") or 0.0) for row in rows), 4),
        "net_pnl": round(sum(float(row.get("net_pnl") or 0.0) for row in rows), 4),
        "flat_notional_total": round(sum(float(row.get("flat_notional_usd") or 0.0) for row in rows if bool(row.get("approved"))), 4),
        "avg_edge": round(
            sum(float(row.get("edge") or 0.0) for row in rows if row.get("edge") is not None)
            / max(1, sum(1 for row in rows if row.get("edge") is not None)),
            4,
        ),
        "avg_confidence": round(
            sum(float(row.get("confidence") or 0.0) for row in rows if row.get("confidence") is not None)
            / max(1, sum(1 for row in rows if row.get("confidence") is not None)),
            4,
        ),
        "skip_reasons": dict(skip_reasons),
    }


def _group_rows(rows: list[dict[str, Any]], key_fn) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[key_fn(row)].append(row)
    return {key: _summarize_group(group) for key, group in sorted(grouped.items())}


def run_replay(args: argparse.Namespace) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    config = _load_replay_config()
    strategy_cfg = dict((config.get("strategy", {}) or {}))
    strategy_cfg["enable_news"] = False
    strategy_cfg["enable_social"] = False
    strategy_cfg["enable_ai"] = False

    prediction_lab_cfg = dict((config.get("prediction_lab", {}) or {}))
    flat_notional_usd = float(
        args.flat_notional_usd
        if args.flat_notional_usd is not None
        else prediction_lab_cfg.get("flat_notional_usd", 10.0)
    )
    fee_rate = float(config.get("kalshi_fee_rate", 0.07) or 0.07)
    max_entry_price = float(config.get("max_entry_price", 0.70) or 0.70)
    min_edge = float(strategy_cfg.get("min_edge", 0.035) or 0.035)
    min_confidence = float(strategy_cfg.get("min_confidence", 0.50) or 0.50)

    engine = PriceOnlyReplayEngine(strategy_cfg)
    account_state = AccountState(
        starting_balance=1_000_000_000.0,
        current_balance=1_000_000_000.0,
        available_cash=1_000_000_000.0,
        reserved_capital=0.0,
        total_exposure=0.0,
        open_positions=0,
    )
    kelly_sizer = FixedNotionalSizer(flat_notional_usd)
    risk_policy = PassThroughRiskPolicy()

    kalshi_path = PROJECT_ROOT / args.kalshi
    ohlcv_path = PROJECT_ROOT / args.ohlcv
    resolved_markets = _load_resolved_temperature_markets(kalshi_path)

    rows: list[dict[str, Any]] = []
    processed = 0
    joined = 0
    matched_tickers = 0
    seen_joined_tickers: set[str] = set()

    with ohlcv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for ohlcv_row in reader:
            processed += 1
            ticker = str(ohlcv_row.get("t") or "")
            archive_market = resolved_markets.get(ticker)
            if archive_market is None:
                continue

            market_like = _build_market_like(archive_market, ohlcv_row)
            if market_like is None:
                continue

            joined += 1
            if ticker not in seen_joined_tickers:
                seen_joined_tickers.add(ticker)
                matched_tickers += 1

            signal, strategy_reason, signal_warnings, raw_price_details = engine.analyze_price_only_market(market_like)
            weather_signal = _build_weather_signal_context(archive_market, market_like, signal, ohlcv_row)
            weather_evidence = build_weather_source_confidence_evidence(weather_signal)

            weather_assessment = assess_weather_market_risk(
                {**weather_signal, **weather_evidence},
                entry_price=(signal or {}).get("market_price"),
                win_probability=None,
            )

            action = "SKIP"
            approved = False
            decision = None
            decision_reason = _normalize_reason_code(strategy_reason or "strategy_rejected")
            decision_reason_detail = strategy_reason or "strategy_rejected"
            position_size = 0.0
            if signal is not None:
                context = _build_trade_context(
                    signal,
                    account_state=account_state,
                    weather_signal={**weather_signal, **weather_evidence},
                )
                decision = build_trade_decision(
                    context,
                    kelly_sizer=kelly_sizer,
                    risk_policy=risk_policy,
                    min_edge=min_edge,
                    min_confidence=min_confidence,
                    max_entry_price=max_entry_price,
                )
                approved = bool(decision.approved)
                if approved:
                    action = decision.action
                    decision_reason = decision.reason_code
                    decision_reason_detail = decision.reason
                    position_size = flat_notional_usd
                else:
                    decision_reason = _normalize_reason_code(decision.reason_code)
                    decision_reason_detail = decision.reason

            scored = _score_action(
                action=action,
                outcome=archive_market.result,
                yes_price=market_like.yes_price,
                no_price=market_like.no_price,
                market_id=archive_market.market_ticker,
                flat_notional_usd=flat_notional_usd,
                fee_rate=fee_rate,
            )

            row = {
                "market_ticker": archive_market.market_ticker,
                "ohlcv_date": str(ohlcv_row.get("d") or ""),
                "family": archive_market.family,
                "shape": (
                    decision.reasoning["weather_risk"]["shape"]
                    if decision and isinstance(decision.reasoning.get("weather_risk"), dict)
                    else weather_assessment.shape
                ),
                "action": action,
                "approved": approved,
                "reason_code": decision_reason,
                "reason_detail": decision_reason_detail,
                "result": archive_market.result,
                "question": archive_market.question,
                "market_subtitle": archive_market.market_subtitle,
                "yes_subtitle": archive_market.yes_subtitle,
                "no_subtitle": archive_market.no_subtitle,
                "yes_price_open": market_like.yes_price,
                "no_price_open": market_like.no_price,
                "edge": _safe_round((signal or {}).get("edge", raw_price_details.get("price_best_edge"))),
                "confidence": _safe_round((signal or {}).get("confidence", raw_price_details.get("price_confidence"))),
                "model_probability": _safe_round((signal or {}).get("model_probability", raw_price_details.get("price_predicted_prob"))),
                "price_best_direction": raw_price_details.get("price_best_direction"),
                "price_yes_edge": _safe_round(raw_price_details.get("price_yes_edge")),
                "price_no_edge": _safe_round(raw_price_details.get("price_no_edge")),
                "flat_notional_usd": flat_notional_usd if approved else 0.0,
                "gross_pnl": _safe_round(scored.get("gross_pnl")),
                "fees_paid": _safe_round(scored.get("fees_paid")),
                "net_pnl": _safe_round(scored.get("net_pnl")),
                "quoted_entry_price": _safe_round(scored.get("quoted_entry_price")),
                "weather_shape": weather_assessment.shape,
                "weather_probability_multiple": _safe_round(weather_assessment.probability_multiple),
                "weather_hidden_gem_tier": weather_assessment.hidden_gem_tier,
                "weather_should_skip": weather_assessment.should_skip,
                "weather_reason_code": weather_assessment.reason_code,
                "weather_size_multiplier": _safe_round(weather_assessment.size_multiplier),
                "weather_max_position_usd": _safe_round(weather_assessment.max_position_usd),
                "weather_station_mapping": weather_evidence.get("weather_station_mapping"),
                "weather_station_city_code": (weather_evidence.get("weather_station_resolution") or {}).get("city_code"),
                "weather_station_id": (weather_evidence.get("weather_station_resolution") or {}).get("station_id"),
                "weather_confidence_score": _safe_round(weather_evidence.get("weather_confidence_score")),
                "source_agreement_score": _safe_round(weather_evidence.get("source_agreement_score")),
                "distribution_probability": _safe_round(weather_evidence.get("distribution_probability")),
                "volume_proxy": _safe_round(market_like.volume),
                "ohlcv_n": _safe_round(ohlcv_row.get("n")),
                "ohlcv_b": _safe_round(ohlcv_row.get("b")),
                "ohlcv_s": _safe_round(ohlcv_row.get("s")),
                "signal_warnings": "|".join(signal_warnings),
                "weather_flags": "|".join(weather_assessment.flags),
                "decision_weather_size_before": (
                    _safe_round(
                        (
                            (decision.reasoning.get("weather_risk") or {}).get("requested_size_before_weather_limits")
                            if decision
                            else None
                        )
                    )
                ),
                "decision_weather_size_after": (
                    _safe_round(
                        (
                            (decision.reasoning.get("weather_risk") or {}).get("requested_size_after_weather_limits")
                            if decision
                            else None
                        )
                    )
                ),
                "decision_position_size": _safe_round(getattr(decision, "position_size", position_size)),
                "decision_requested_position_size": _safe_round(getattr(decision, "requested_position_size", position_size)),
            }
            rows.append(row)

            if args.progress_every > 0 and joined % args.progress_every == 0:
                print(f"processed={processed} joined={joined}", file=sys.stderr)

            if args.limit and joined >= args.limit:
                break

    overall = _summarize_group(rows)
    overall["distinct_joined_markets"] = matched_tickers
    overall["joined_rows"] = joined
    overall["processed_ohlcv_rows"] = processed
    overall["resolved_temperature_markets"] = len(resolved_markets)

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": {
            "kalshi": str(kalshi_path.relative_to(PROJECT_ROOT)),
            "ohlcv": str(ohlcv_path.relative_to(PROJECT_ROOT)),
        },
        "parameters": {
            "flat_notional_usd": flat_notional_usd,
            "fee_rate": fee_rate,
            "strategy_min_edge": min_edge,
            "strategy_min_confidence": min_confidence,
            "max_entry_price": max_entry_price,
            "limit": int(args.limit or 0),
        },
        "coverage": {
            "resolved_temperature_markets": len(resolved_markets),
            "distinct_joined_markets": matched_tickers,
            "joined_rows": joined,
            "processed_ohlcv_rows": processed,
        },
        "overall": overall,
        "by_shape": _group_rows(rows, lambda row: str(row.get("shape") or "unknown")),
        "by_family": _group_rows(rows, lambda row: str(row.get("family") or "unknown")),
        "by_action": _group_rows(rows, lambda row: str(row.get("action") or "SKIP")),
        "by_shape_family_action": _group_rows(
            rows,
            lambda row: f"{row.get('shape') or 'unknown'}|{row.get('family') or 'unknown'}|{row.get('action') or 'SKIP'}",
        ),
        "caveats": [
            "Offline replay using only local archive CSVs; no network calls or live forecasts are used.",
            "Price-only strategy replay: news, social, AI, live weather, volume, and time signals are disabled.",
            "Weather-risk evidence is limited to deterministic local metadata helpers, so exceptional hidden-gem checks will often stay conservative.",
            "Entry price uses the OHLCV daily open quote as an approximation, not a true live execution price.",
            "Each joined OHLCV row is treated as an independent snapshot; this is not a full historic bot execution timeline.",
            "Primary PnL uses flat notional per approved trade; weather sizing outputs are recorded as metadata, not as the primary scoring method.",
        ],
    }
    return summary, rows


def write_rows_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else []
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    summary, rows = run_replay(args)

    summary_output = PROJECT_ROOT / args.summary_output
    rows_output = PROJECT_ROOT / args.rows_output
    summary_output.parent.mkdir(parents=True, exist_ok=True)
    summary_output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    write_rows_csv(rows, rows_output)

    print(f"Wrote {summary_output.relative_to(PROJECT_ROOT)}")
    print(f"Wrote {rows_output.relative_to(PROJECT_ROOT)}")
    print(
        "Replay totals: "
        f"joined_rows={summary['overall']['joined_rows']}, "
        f"buy_yes={summary['overall']['buy_yes']}, "
        f"buy_no={summary['overall']['buy_no']}, "
        f"skip={summary['overall']['skip']}, "
        f"net_pnl={summary['overall']['net_pnl']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
