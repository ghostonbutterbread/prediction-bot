from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from typing import Any, Optional

from bot.decision_pipeline import DecisionPipelineEvaluator, build_fixed_opportunity_account_state
from bot.file_ops import append_jsonl, atomic_write_json, load_jsonl, locked_file, rewrite_jsonl
from bot.strategies.enhanced import EnhancedStrategyEngine, KellySizer
from bot.market_classification import apply_classification_metadata, classify_market_object
from bot.shared_core.resolution import detect_market_outcome
from bot.shared_core.weather_risk import (
    assess_weather_market_risk,
    build_weather_source_confidence_evidence,
    classify_weather_market,
)
from bot.weather import WeatherMarketCityMapper
from bot.weather.replay import ReplayFeeModel, score_replay_answer

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PredictionLabRunResult:
    run_id: str
    scanned_markets: int
    recorded_predictions: int
    group_counts: dict[str, int]
    series_counts: dict[str, int]
    ledger_path: str


class PredictionLab:
    def __init__(self, config: dict[str, Any]):
        self.config = config or {}
        self.lab_cfg = (self.config.get("prediction_lab", {}) or {})
        self.mode = str(self.lab_cfg.get("mode", "seed_and_watch") or "seed_and_watch").lower()
        self.paused = bool(self.lab_cfg.get("paused", False))
        self.observer_mode = bool(self.lab_cfg.get("observer_mode", False))
        strategy_cfg = dict((self.config.get("strategy", {}) or {}))
        if self.lab_cfg.get("disable_news", True):
            strategy_cfg["enable_news"] = False
        if self.lab_cfg.get("disable_social", True):
            strategy_cfg["enable_social"] = False
        if self.lab_cfg.get("disable_ai", True):
            strategy_cfg["enable_ai"] = False
        self.strategy = EnhancedStrategyEngine(strategy_cfg)
        self.groups = [str(group).strip().lower() for group in (self.lab_cfg.get("groups") or ["weather"]) if str(group).strip()]
        self.max_markets_per_run = int(self.lab_cfg.get("max_markets_per_run", 500) or 500)
        self.max_new_predictions_per_seed = int(self.lab_cfg.get("max_new_predictions_per_seed", 500) or 500)
        self.record_all_scored = bool(self.lab_cfg.get("record_all_scored", True))
        self.seed_daily_temp_first = bool(self.lab_cfg.get("seed_daily_temp_first", True))
        self.allow_non_weather = bool(self.lab_cfg.get("allow_non_weather", False))
        self.score_only = bool(self.lab_cfg.get("score_only", True))
        self.use_sizing_logic = bool(self.lab_cfg.get("use_sizing_logic", False))
        self.use_shared_pipeline = bool(self.lab_cfg.get("use_shared_pipeline", False))
        self.collector_interval_seconds = int(self.lab_cfg.get("collector_interval_seconds", 900) or 900)
        self.collector_record_market_snapshots = bool(self.lab_cfg.get("collector_record_market_snapshots", True))
        self.collector_record_predictions = bool(self.lab_cfg.get("collector_record_predictions", True))
        self.collector_fetch_mode = str(self.lab_cfg.get("collector_fetch_mode", "direct_markets") or "direct_markets").lower()
        self.collector_direct_page_size = int(self.lab_cfg.get("collector_direct_page_size", 200) or 200)
        self.collector_max_pages = int(self.lab_cfg.get("collector_max_pages", 10) or 10)
        self.send_telegram_updates = bool(self.lab_cfg.get("send_telegram_updates", False))
        self.telegram_summary_on_pause = bool(self.lab_cfg.get("telegram_summary_on_pause", False))
        self.min_confidence_to_record = float(self.lab_cfg.get("min_confidence_to_record", 0.0) or 0.0)
        self.min_edge_to_record = float(self.lab_cfg.get("min_edge_to_record", 0.0) or 0.0)
        self.hypothetical_mode = self._normalize_hypothetical_mode(self.lab_cfg.get("hypothetical_notional_mode", "flat"))
        self.flat_notional_usd = float(self.lab_cfg.get("flat_notional_usd", 10.0) or 10.0)
        fresh_wallet_bankroll = self.lab_cfg.get("fresh_wallet_bankroll_usd", 100.0)
        self.fresh_wallet_bankroll_usd = float(100.0 if fresh_wallet_bankroll is None else fresh_wallet_bankroll)
        economics_cfg = self.config.get("trade_economics", {}) or {}
        self.kelly = KellySizer(
            fee_rate=self.config.get("kalshi_fee_rate"),
            min_position_size_usd=economics_cfg.get("min_position_size_usd", 1.0),
            min_expected_net_profit_usd=economics_cfg.get("min_expected_net_profit_usd", 0.0),
        )
        self.decision_evaluator = (
            DecisionPipelineEvaluator(self.config, strategy=self.strategy, kelly_sizer=self.kelly)
            if self.use_shared_pipeline
            else None
        )
        self.experiment_id = str(self.lab_cfg.get("experiment_id") or "default")
        self.strategy_version = str(self.lab_cfg.get("strategy_version") or "v1")
        self.mapper = WeatherMarketCityMapper()
        self.data_dir = Path(self.config.get("data_dir", "data"))
        self.root_dir = self.data_dir / "prediction_lab"
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.predictions_path = self.root_dir / "predictions.jsonl"
        self.resolutions_path = self.root_dir / "resolutions.jsonl"
        self.market_snapshots_path = self.root_dir / "market_snapshots.jsonl"
        self.state_path = self.root_dir / "state.json"
        self.reports_dir = self.root_dir / "reports"
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.state = self._load_state()

    def run(self, exchange) -> PredictionLabRunResult:
        run_id = datetime.now(timezone.utc).strftime("plab_%Y%m%dT%H%M%SZ")
        self._validate_v1_groups()
        if not bool(self.lab_cfg.get("enabled", True)):
            self._update_state(mode=self.mode, paused=False, last_run_id=run_id, paused_reason="disabled")
            return PredictionLabRunResult(run_id=run_id, scanned_markets=0, recorded_predictions=0, group_counts={}, series_counts={}, ledger_path=str(self.predictions_path))

        if self.paused:
            self._update_state(mode=self.mode, paused=True, last_run_id=run_id, paused_reason="manual_pause")
            return PredictionLabRunResult(run_id=run_id, scanned_markets=0, recorded_predictions=0, group_counts={}, series_counts={}, ledger_path=str(self.predictions_path))

        if self.mode == "resolve_only":
            self._update_state(mode=self.mode, paused=False, last_run_id=run_id, paused_reason="resolve_only")
            return PredictionLabRunResult(run_id=run_id, scanned_markets=0, recorded_predictions=0, group_counts={}, series_counts={}, ledger_path=str(self.predictions_path))

        markets = self._get_candidate_markets(exchange)
        markets = self._prioritize_markets(markets)
        group_counts = Counter()
        series_counts = Counter()
        recorded = 0

        for market in markets:
            classification = apply_classification_metadata(market)
            market_group = classification.market_group if classification else "unknown"
            if not self._group_allowed(market_group):
                continue
            group_counts[market_group] += 1
            series = str((getattr(market, "metadata", {}) or {}).get("series") or getattr(market, "category", "unknown"))
            series_counts[series] += 1
            decision_artifact = None
            if self.use_shared_pipeline:
                decision_artifact = self._evaluate_shared_pipeline(market, exchange=exchange)
                signal = decision_artifact.get("strategy_signal") if isinstance(decision_artifact, dict) else None
            else:
                signal = self.strategy.analyze_market(market, None)
            if signal is None and not self.record_all_scored:
                continue
            if signal is None:
                signal = {
                    "market_id": market.id,
                    "exchange": market.exchange,
                    "direction": "SKIP",
                    "model_probability": None,
                    "market_price": getattr(market, "yes_price", None),
                    "yes_market_price": getattr(market, "yes_price", None),
                    "no_market_price": getattr(market, "no_price", None),
                    "edge": 0.0,
                    "confidence": 0.0,
                    "signals": {},
                    "question": market.question,
                }
                if decision_artifact is not None:
                    signal["skip_reason_code"] = decision_artifact.get("final_reason_code")

            confidence = float(signal.get("confidence", 0.0) or 0.0)
            edge = float(signal.get("edge", 0.0) or 0.0)
            if confidence < self.min_confidence_to_record or edge < self.min_edge_to_record:
                continue

            decision_type = self._decision_type(signal)
            if decision_artifact is not None and decision_artifact.get("final_action") == "SKIP":
                decision_type = "skip"
                signal = self._skip_safe_signal(signal, decision_artifact)
            prediction_recorded = False
            observation_mode = self._observation_semantics_enabled()
            should_record_prediction = (
                (decision_type in {"buy_yes", "buy_no"} or self.record_all_scored)
                and (not observation_mode or self.collector_record_predictions)
                and not self.score_only
            )
            if should_record_prediction:
                row = self._build_prediction_row(
                    run_id,
                    market,
                    signal,
                    decision_type=decision_type,
                    decision_artifact=decision_artifact,
                )
                prediction_recorded = self._append_prediction_if_absent(row)
                if prediction_recorded:
                    recorded += 1

            if observation_mode and self.collector_record_market_snapshots:
                with self._prediction_ledger_lock():
                    append_jsonl(
                        self.market_snapshots_path,
                        self._build_market_snapshot_row(
                            run_id,
                            market,
                            signal,
                            decision_type=decision_type,
                            prediction_recorded=prediction_recorded,
                            decision_artifact=decision_artifact,
                        ),
                    )

            if self.mode == "seed_and_watch" and recorded >= self.max_new_predictions_per_seed:
                break

        paused_reason = "seed_complete" if self.mode == "seed_and_watch" else None
        self._update_state(
            mode=self.mode,
            paused=False,
            last_run_id=run_id,
            last_collect_at=datetime.now(timezone.utc).isoformat(),
            open_prediction_count=self._count_open_predictions(),
            resolved_prediction_count=self._count_resolved_predictions(),
            paused_reason=paused_reason,
        )

        return PredictionLabRunResult(
            run_id=run_id,
            scanned_markets=len(markets),
            recorded_predictions=recorded,
            group_counts=dict(group_counts),
            series_counts=dict(series_counts),
            ledger_path=str(self.predictions_path),
        )

    def resolve_open_predictions(self, exchange) -> dict[str, Any]:
        with self._prediction_ledger_lock():
            rows = load_jsonl(self.predictions_path)
            existing_resolutions = load_jsonl(self.resolutions_path)
            resolved_keys = {self._prediction_identity(row) for row in existing_resolutions}
            unresolved = [row for row in rows if row.get("status") == "open"]
            resolved_count = 0
            correct = 0
            incorrect = 0
            skipped = 0
            pnl_total = 0.0

            for row in unresolved:
                market_id = row.get("market_id")
                if not market_id:
                    continue
                outcome = self._fetch_market_outcome(exchange, market_id)
                if outcome not in {"YES", "NO", "VOID"}:
                    continue
                identity = self._prediction_identity(row)
                if identity in resolved_keys:
                    row["status"] = "resolved"
                    continue
                if outcome == "VOID":
                    scored = {"is_correct": None, "net_pnl": 0.0, "gross_pnl": 0.0, "entry_price": None, "quoted_entry_price": None}
                    row["status"] = "voided"
                else:
                    action = self._stored_replay_action(row)
                    fee_model = ReplayFeeModel(profit_fee_rate=float(self.config.get("kalshi_fee_rate", 0.07) or 0.07))
                    position_size = self._stored_position_size(row)
                    scored = score_replay_answer(
                        action,
                        {
                            "replay_id": row.get("prediction_id"),
                            "market_id": market_id,
                            "outcome": outcome,
                            "prices": {
                                "yes_price": row.get("yes_market_price"),
                                "no_price": row.get("no_market_price"),
                            },
                        },
                        position_size=position_size,
                        fee_model=fee_model,
                    )
                    row["status"] = "resolved"
                row["resolution"] = {
                    "outcome": outcome,
                    "resolved_at": datetime.now(timezone.utc).isoformat(),
                    "is_correct": scored.get("is_correct"),
                    "net_pnl": scored.get("net_pnl"),
                    "gross_pnl": scored.get("gross_pnl"),
                    "position_size": scored.get("position_size"),
                    "contracts": scored.get("contracts"),
                    "fees_paid": scored.get("fees_paid"),
                    "entry_price": scored.get("entry_price"),
                    "quoted_entry_price": scored.get("quoted_entry_price"),
                }
                append_jsonl(self.resolutions_path, row)
                resolved_keys.add(identity)
                resolved_count += 1
                if scored.get("is_correct") is True:
                    correct += 1
                elif scored.get("is_correct") is False:
                    incorrect += 1
                else:
                    skipped += 1
                pnl_total += float(scored.get("net_pnl") or 0.0)

            if resolved_count:
                rewrite_jsonl(self.predictions_path, rows)
            open_prediction_count = self._count_open_predictions(rows)
            self._update_state(
                mode=self.mode,
                last_resolve_at=datetime.now(timezone.utc).isoformat(),
                open_prediction_count=open_prediction_count,
                resolved_prediction_count=self._count_resolved_predictions(rows),
                paused_reason=None if open_prediction_count else "no_open_predictions",
            )

        return {
            "resolved": resolved_count,
            "correct": correct,
            "incorrect": incorrect,
            "skipped": skipped,
            "net_pnl": round(pnl_total, 4),
        }

    def summarize(self) -> dict[str, Any]:
        with self._prediction_ledger_lock():
            rows = load_jsonl(self.predictions_path)
        resolved = [row for row in rows if row.get("status") == "resolved" and isinstance(row.get("resolution"), dict)]
        open_rows = [row for row in rows if row.get("status") == "open"]
        group_counts = Counter(row.get("group", "unknown") for row in rows)
        confidence_buckets = Counter()
        correct = 0
        incorrect = 0
        pnl_total = 0.0

        for row in resolved:
            conf = float(row.get("confidence", 0.0) or 0.0)
            bucket = f"{int(conf * 100 // 5 * 5):02d}-{int(conf * 100 // 5 * 5 + 4):02d}%"
            confidence_buckets[bucket] += 1
            is_correct = (row.get("resolution") or {}).get("is_correct")
            if is_correct is True:
                correct += 1
            elif is_correct is False:
                incorrect += 1
            pnl_total += float((row.get("resolution") or {}).get("net_pnl") or 0.0)

        return {
            "mode": self.mode,
            "state": self.state,
            "total_predictions": len(rows),
            "open_predictions": len(open_rows),
            "resolved_predictions": len(resolved),
            "correct": correct,
            "incorrect": incorrect,
            "accuracy": round(correct / len(resolved), 4) if resolved else None,
            "net_pnl": round(pnl_total, 4),
            "group_counts": dict(group_counts),
            "confidence_buckets": dict(confidence_buckets),
        }

    def _build_prediction_row(
        self,
        run_id: str,
        market,
        signal: dict[str, Any],
        *,
        decision_type: str,
        decision_artifact: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata = dict(getattr(market, "metadata", {}) or {})
        context = None
        if metadata.get("market_group") == "weather":
            try:
                context_obj = self.mapper.resolve(getattr(market, "question", ""), getattr(market, "category", ""))
                if context_obj is not None:
                    context = {
                        "city_id": context_obj.city_id,
                        "source_id": context_obj.primary_source_id,
                    }
            except Exception:
                context = None
        timestamp = datetime.now(timezone.utc).isoformat()
        row = {
            "prediction_id": f"{run_id}_{market.id}",
            "run_id": run_id,
            "timestamp": timestamp,
            "status": "open",
            "group": metadata.get("market_group", "unknown"),
            "series": metadata.get("series") or getattr(market, "category", "unknown"),
            "event_ticker": metadata.get("event_ticker"),
            "market_id": market.id,
            "question": getattr(market, "question", ""),
            "direction": signal.get("direction", "SKIP"),
            "decision_type": decision_type,
            "confidence": float(signal.get("confidence", 0.0) or 0.0),
            "edge": float(signal.get("edge", 0.0) or 0.0),
            "model_probability": signal.get("model_probability"),
            "market_price": signal.get("market_price"),
            "yes_market_price": signal.get("yes_market_price", getattr(market, "yes_price", None)),
            "no_market_price": signal.get("no_market_price", getattr(market, "no_price", None)),
            "signals": signal.get("signals", {}),
            "signal_details": signal.get("signal_details", {}),
            "weather_context": context,
            "experiment_id": self.experiment_id,
            "strategy_version": self.strategy_version,
            "hypothetical": self._build_hypothetical_metadata(market, signal),
            **self._observation_metadata(),
        }
        weather_risk = self._build_weather_risk_metadata(market, signal, weather_context=context)
        if weather_risk is not None:
            row["weather_risk"] = weather_risk
        if decision_artifact is not None:
            row["shared_pipeline"] = self._shared_pipeline_summary(decision_artifact)
            row["decision_artifact"] = decision_artifact
        return row

    def _build_market_snapshot_row(
        self,
        run_id: str,
        market,
        signal: dict[str, Any],
        *,
        decision_type: str,
        prediction_recorded: bool,
        decision_artifact: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata = dict(getattr(market, "metadata", {}) or {})
        timestamp = datetime.now(timezone.utc).isoformat()
        row = {
            "timestamp": timestamp,
            "observed_at": timestamp,
            "run_id": run_id,
            "snapshot_key": str(market.id),
            "market_id": market.id,
            "group": metadata.get("market_group", "unknown"),
            "series": metadata.get("series") or getattr(market, "category", "unknown"),
            "question": getattr(market, "question", ""),
            "yes_price": getattr(market, "yes_price", None),
            "no_price": getattr(market, "no_price", None),
            "confidence": signal.get("confidence"),
            "edge": signal.get("edge"),
            "direction": signal.get("direction"),
            "decision_type": decision_type,
            "recorded_prediction": prediction_recorded,
            "collector_interval_seconds": self.collector_interval_seconds,
            **self._observation_metadata(),
        }
        weather_risk = self._build_weather_risk_metadata(market, signal)
        if weather_risk is not None:
            row["weather_risk"] = weather_risk
        if decision_artifact is not None:
            row["shared_pipeline"] = self._shared_pipeline_summary(decision_artifact)
            row["decision_artifact"] = decision_artifact
        return row

    def _evaluate_shared_pipeline(self, market, *, exchange: Any | None = None) -> dict[str, Any]:
        if self.decision_evaluator is None:
            raise RuntimeError("Shared decision pipeline is not enabled")
        metadata = dict(getattr(market, "metadata", {}) or {})
        account_state = build_fixed_opportunity_account_state(self.fresh_wallet_bankroll_usd)
        order_book = self._fetch_order_book(exchange, market)
        artifact = self.decision_evaluator.evaluate(
            market,
            account_state=account_state,
            order_book=order_book,
            source_context={"market_metadata": metadata},
            mode="prediction_lab",
            config_snapshot=self.config,
        )
        return artifact.to_dict()

    @staticmethod
    def _fetch_order_book(exchange: Any | None, market: Any) -> dict[str, Any] | None:
        market_id = getattr(market, "id", None)
        if not market_id:
            return None
        order_book = None
        fetch_order_book = getattr(exchange, "get_order_book", None)
        if callable(fetch_order_book):
            try:
                order_book = fetch_order_book(market_id)
            except Exception as exc:
                logger.debug("Prediction Lab shared pipeline order book fetch failed for %s: %s", market_id, exc)
        if not PredictionLab._usable_order_book(order_book):
            fetch_bid_ask = getattr(exchange, "get_market_bid_ask", None)
            if callable(fetch_bid_ask):
                try:
                    order_book = fetch_bid_ask(market_id)
                except Exception as exc:
                    logger.debug("Prediction Lab shared pipeline bid/ask fallback failed for %s: %s", market_id, exc)
                    return None
        if not PredictionLab._usable_order_book(order_book):
            return None
        return dict(order_book)

    @staticmethod
    def _usable_order_book(order_book: Any) -> bool:
        if not isinstance(order_book, dict):
            return False
        book_fields = (
            "best_yes_ask",
            "best_yes_bid",
            "best_no_ask",
            "best_no_bid",
            "mid_yes",
            "spread",
            "spread_pct",
        )
        return any(order_book.get(field) is not None for field in book_fields)

    @staticmethod
    def _skip_safe_signal(signal: dict[str, Any], decision_artifact: dict[str, Any]) -> dict[str, Any]:
        safe_signal = dict(signal)
        raw_direction = safe_signal.get("direction")
        safe_signal["direction"] = "SKIP"
        safe_signal["skip_reason_code"] = decision_artifact.get("final_reason_code")
        safe_signal["final_reason_code"] = decision_artifact.get("final_reason_code")
        if raw_direction is not None:
            safe_signal["raw_strategy_direction"] = raw_direction
        return safe_signal

    @staticmethod
    def _shared_pipeline_summary(decision_artifact: dict[str, Any]) -> dict[str, Any]:
        return {
            "enabled": True,
            "final_action": decision_artifact.get("final_action"),
            "final_reason_code": decision_artifact.get("final_reason_code"),
            "execution_snapshot_source": decision_artifact.get("execution_snapshot_source"),
            "order_book_source": (decision_artifact.get("order_book_snapshot") or {}).get("source"),
        }

    def _build_weather_risk_metadata(
        self,
        market,
        signal: dict[str, Any],
        *,
        weather_context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        existing = dict(signal.get("weather_risk") or {}) if isinstance(signal.get("weather_risk"), dict) else {}
        question = str(getattr(market, "question", "") or "")
        market_id = str(getattr(market, "id", "") or "")
        market_metadata = dict(getattr(market, "metadata", {}) or {})
        if (
            market_metadata.get("market_group") != "weather"
            and classify_weather_market(question, market_id) == "unknown"
            and not existing
        ):
            return None

        weather_signal = {
            **dict(signal or {}),
            "market_id": market_id,
            "question": question,
        }
        if weather_signal.get("market_volume") is None:
            weather_signal["market_volume"] = getattr(market, "volume", None)
        live_details = (signal.get("signal_details") or {}).get("live") if isinstance(signal.get("signal_details"), dict) else None
        if isinstance(live_details, dict):
            live_data = live_details.get("data") if isinstance(live_details.get("data"), dict) else {}
            weather_signal.setdefault("weather", live_details)
            weather_signal.setdefault("data", live_data)
            for key in (
                "station_id",
                "station_cli",
                "source_agreement_score",
                "agreement",
                "weather_confidence",
                "weather_station_mapping",
            ):
                if weather_signal.get(key) in (None, "") and signal.get(key) not in (None, ""):
                    weather_signal[key] = signal.get(key)
        if weather_context:
            weather_signal.setdefault("weather_context", dict(weather_context))

        evidence = build_weather_source_confidence_evidence(weather_signal)
        normalized = self._normalize_weather_trade_signal(market, signal)
        weather_assessment = assess_weather_market_risk(
            {**weather_signal, **evidence},
            entry_price=normalized.get("entry_price"),
            win_probability=normalized.get("win_probability"),
        )
        return {
            **existing,
            **weather_assessment.to_dict(),
            "evidence": evidence,
        }

    @staticmethod
    def _normalize_weather_trade_signal(market, signal: dict[str, Any]) -> dict[str, float | None]:
        direction = str(signal.get("direction") or "SKIP").upper()
        yes_price = PredictionLab._coerce_unit_float(signal.get("yes_market_price", getattr(market, "yes_price", None)))
        no_price = PredictionLab._coerce_unit_float(signal.get("no_market_price", getattr(market, "no_price", None)))
        model_probability = PredictionLab._coerce_unit_float(signal.get("model_probability"))

        if direction == "BUY_NO":
            entry_price = no_price if no_price is not None else (1 - yes_price if yes_price is not None else None)
            win_probability = (1 - model_probability) if model_probability is not None else None
        elif direction == "BUY_YES":
            entry_price = yes_price
            win_probability = model_probability
        else:
            entry_price = None
            win_probability = None

        return {
            "entry_price": entry_price,
            "win_probability": win_probability,
        }

    def _build_hypothetical_metadata(self, market, signal: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize_weather_trade_signal(market, signal)
        entry_price = normalized.get("entry_price")
        win_probability = normalized.get("win_probability")
        direction = str(signal.get("direction") or "SKIP").upper()
        is_trade_direction = direction in {"BUY_YES", "BUY_NO"}

        if self.hypothetical_mode == "flat":
            approved_size = self.flat_notional_usd if is_trade_direction else 0.0
            return {
                "mode": "flat",
                "sizing_method": "flat",
                "notional_usd": self.flat_notional_usd,
                "position_size_usd": round(approved_size, 4),
                "approved_position_size_usd": round(approved_size, 4),
                "requested_position_size_usd": round(approved_size, 4),
                "entry_price": entry_price,
                "win_probability": win_probability,
                "bankroll_usd": None,
                "zero_reason": None if approved_size > 0 else "not_trade_direction",
                "reason_if_zero": None if approved_size > 0 else "not_trade_direction",
            }

        bankroll = max(0.0, self.fresh_wallet_bankroll_usd)
        requested_size = 0.0
        approved_size = 0.0
        zero_reason = None

        if not is_trade_direction:
            zero_reason = "not_trade_direction"
        elif bankroll <= 0:
            zero_reason = "non_positive_bankroll"
        elif entry_price is None:
            zero_reason = "missing_entry_price"
        elif not 0 < float(entry_price) < 1:
            zero_reason = "invalid_entry_price"
        elif win_probability is None:
            zero_reason = "missing_win_probability"
        elif not 0 <= float(win_probability) <= 1:
            zero_reason = "invalid_win_probability"
        else:
            requested_size = float(self.kelly.calculate(float(win_probability), float(entry_price), bankroll) or 0.0)
            if isfinite(requested_size) and requested_size > 0:
                approved_size = requested_size
            else:
                zero_reason = "kelly_zero_size"

        requested_size = round(requested_size if isfinite(requested_size) and requested_size > 0 else 0.0, 4)
        approved_size = round(approved_size if isfinite(approved_size) and approved_size > 0 else 0.0, 4)
        return {
            "mode": "fresh_kelly",
            "sizing_method": "fresh_wallet_kelly",
            "notional_usd": approved_size,
            "position_size_usd": approved_size,
            "approved_position_size_usd": approved_size,
            "requested_position_size_usd": requested_size,
            "entry_price": entry_price,
            "win_probability": win_probability,
            "bankroll_usd": round(bankroll, 4),
            "zero_reason": zero_reason,
            "reason_if_zero": zero_reason,
            "kelly": {
                "bankroll_usd": round(bankroll, 4),
                "requested_position_size_usd": requested_size,
                "approved_position_size_usd": approved_size,
                "entry_price": entry_price,
                "win_probability": win_probability,
                "zero_reason": zero_reason,
                "fraction": getattr(self.kelly, "fraction", None),
                "max_bet_pct": getattr(self.kelly, "max_bet_pct", None),
                "fee_rate": getattr(self.kelly, "fee_rate", None),
            },
        }

    def _stored_position_size(self, row: dict[str, Any]) -> float:
        if self._stored_replay_action(row) == "SKIP":
            return 0.0
        hypothetical = row.get("hypothetical") if isinstance(row.get("hypothetical"), dict) else {}
        for key in ("position_size_usd", "approved_position_size_usd", "notional_usd"):
            value = hypothetical.get(key)
            try:
                if value is not None:
                    return max(0.0, float(value))
            except (TypeError, ValueError):
                continue
        return max(0.0, self.flat_notional_usd)

    @staticmethod
    def _stored_replay_action(row: dict[str, Any]) -> str:
        decision_type = str(row.get("decision_type") or "").lower()
        if decision_type == "skip":
            return "SKIP"
        action = str(row.get("direction") or "SKIP").upper()
        if action in {"BUY_YES", "BUY_NO"}:
            return action
        return "SKIP"

    def _get_candidate_markets(self, exchange) -> list[Any]:
        direct_fetch = getattr(exchange, "get_markets_direct", None)
        if self._observation_semantics_enabled() and self.collector_fetch_mode == "direct_markets" and callable(direct_fetch):
            return direct_fetch(
                limit=self.max_markets_per_run,
                page_size=self.collector_direct_page_size,
                max_pages=self.collector_max_pages,
            )
        return exchange.get_markets(limit=self.max_markets_per_run)

    def _prioritize_markets(self, markets: list[Any]) -> list[Any]:
        def score(market: Any) -> tuple[int, float]:
            metadata = dict(getattr(market, "metadata", {}) or {})
            classification = classify_market_object(market)
            group = classification.market_group if classification else metadata.get("market_group", "unknown")
            family = classification.family if classification else metadata.get("market_family", "")
            is_daily_temp = family == "daily_temperature"
            close_ts = getattr(market, "closes_at", None)
            close_score = 0.0
            if hasattr(close_ts, "timestamp"):
                close_score = -close_ts.timestamp()
            if self._group_allowed(group) and group == "weather":
                return (2 if is_daily_temp and self.seed_daily_temp_first else 1, close_score)
            return (0, close_score)

        return sorted(markets, key=score, reverse=True)

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._default_state()
        try:
            with locked_file(self.state_path, "r") as fh:
                return json.load(fh)
        except Exception:
            return self._default_state()

    def _update_state(self, **updates: Any) -> None:
        if "pause_reason" in updates and "paused_reason" not in updates:
            updates["paused_reason"] = updates["pause_reason"]
        state_lock = self.root_dir / "prediction_lab.state.lock"
        with locked_file(state_lock, "a+"):
            current_state = self._load_state_unlocked()
            self.state = {**current_state, **self._observation_metadata(), **updates}
            atomic_write_json(self.state_path, self.state)

    def _count_open_predictions(self, rows: Optional[list[dict[str, Any]]] = None) -> int:
        rows = rows if rows is not None else load_jsonl(self.predictions_path)
        return sum(1 for row in rows if row.get("status") == "open")

    def _count_resolved_predictions(self, rows: Optional[list[dict[str, Any]]] = None) -> int:
        rows = rows if rows is not None else load_jsonl(self.predictions_path)
        return sum(1 for row in rows if row.get("status") == "resolved")

    def _load_state_unlocked(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._default_state()
        try:
            return json.loads(self.state_path.read_text())
        except Exception:
            return self._default_state()

    def storage_usage(self) -> dict[str, Any]:
        total_bytes = 0
        for path in (self.market_snapshots_path, self.predictions_path):
            if path.exists() and path.is_file():
                try:
                    total_bytes += path.stat().st_size
                except OSError:
                    continue
        cap_gb = float(self.lab_cfg.get("collection_storage_cap_gb", 0.0) or 0.0)
        cap_bytes = int(cap_gb * (1024**3)) if cap_gb > 0 else 0
        warning_threshold_pct = float(self.lab_cfg.get("collection_warning_threshold_pct", 90.0) or 90.0)
        pct_of_cap = (total_bytes / cap_bytes * 100.0) if cap_bytes > 0 else None
        return {
            "bytes": total_bytes,
            "gb": round(total_bytes / (1024**3), 6),
            "cap_bytes": cap_bytes,
            "cap_gb": cap_gb,
            "pct_of_cap": round(pct_of_cap, 2) if pct_of_cap is not None else None,
            "warning_threshold_pct": warning_threshold_pct,
            "warning_threshold_reached": bool(pct_of_cap is not None and pct_of_cap >= warning_threshold_pct),
            "over_cap": bool(cap_bytes > 0 and total_bytes >= cap_bytes),
        }

    def update_runtime_state(self, **updates: Any) -> None:
        self._update_state(**updates)

    def _default_state(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "run_state": "paused" if self.paused else "idle_watch",
            "pause_reason": "manual_pause" if self.paused else "none",
            "paused_reason": "manual_pause" if self.paused else "none",
            "paused": self.paused,
            "last_run_id": None,
            "last_collect_at": None,
            "last_resolve_at": None,
            "last_storage_check_at": None,
            "storage_usage_bytes": 0,
            "storage_usage_gb": 0.0,
            "warning_emitted": False,
            "open_prediction_count": 0,
            "resolved_prediction_count": 0,
            "active_group": self.groups[0] if self.groups else None,
            "last_error": None,
            "seed_complete": False,
            "experiment_id": self.experiment_id,
            "strategy_version": self.strategy_version,
            **self._observation_metadata(),
        }

    def _observation_metadata(self) -> dict[str, Any]:
        return {
            "observer_mode": self._observation_semantics_enabled(),
            "trading_enabled": False,
            "order_execution_enabled": False,
        }

    def _observation_semantics_enabled(self) -> bool:
        return self.observer_mode or self.mode == "collector"

    def _fetch_market_outcome(self, exchange, market_id: str) -> Optional[str]:
        fetch_raw = getattr(exchange, "_fetch_market_raw", None)
        if not callable(fetch_raw):
            return None
        raw = fetch_raw(market_id)
        if not isinstance(raw, dict):
            return None
        return detect_market_outcome(raw)

    def _group_allowed(self, group: str) -> bool:
        normalized = str(group or "unknown").lower()
        if normalized in self.groups:
            return True
        if normalized != "weather" and not self.allow_non_weather:
            return False
        return normalized in self.groups

    def _prediction_ledger_lock(self):
        return locked_file(self.root_dir / "prediction_lab.ledger.lock", "a+")

    def _validate_v1_groups(self) -> None:
        if len(self.groups) != 1:
            raise ValueError("Prediction Lab v1 collector requires exactly one configured group")

    def _prediction_identity(self, row: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(row.get("market_id") or ""),
            str(row.get("experiment_id") or self.experiment_id),
            str(row.get("strategy_version") or self.strategy_version),
        )

    def _append_prediction_if_absent(self, row: dict[str, Any]) -> bool:
        with self._prediction_ledger_lock():
            rows = load_jsonl(self.predictions_path)
            identity = self._prediction_identity(row)
            already_present = any(self._prediction_identity(existing) == identity for existing in rows)
            if already_present:
                return False
            append_jsonl(self.predictions_path, row)
            return True

    @staticmethod
    def _decision_type(signal: dict[str, Any]) -> str:
        direction = str(signal.get("direction") or "SKIP").upper()
        if direction == "BUY_YES":
            return "buy_yes"
        if direction == "BUY_NO":
            return "buy_no"
        return "skip"

    @staticmethod
    def _normalize_hypothetical_mode(value: Any) -> str:
        normalized = str(value or "flat").strip().lower()
        if normalized in {"fresh_kelly", "kelly"}:
            return "fresh_kelly"
        return "flat"

    @staticmethod
    def _coerce_unit_float(value: Any) -> float | None:
        try:
            if value is None:
                return None
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if 0 <= numeric <= 1:
            return numeric
        return None
