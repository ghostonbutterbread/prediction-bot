from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from bot.file_ops import append_jsonl, atomic_write_json, load_jsonl, locked_file, rewrite_jsonl
from bot.strategies.enhanced import EnhancedStrategyEngine
from bot.market_classification import apply_classification_metadata, classify_market_object
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
        self.collector_record_market_snapshots = bool(self.lab_cfg.get("collector_record_market_snapshots", True))
        self.collector_record_predictions = bool(self.lab_cfg.get("collector_record_predictions", True))
        self.send_telegram_updates = bool(self.lab_cfg.get("send_telegram_updates", False))
        self.telegram_summary_on_pause = bool(self.lab_cfg.get("telegram_summary_on_pause", False))
        self.min_confidence_to_record = float(self.lab_cfg.get("min_confidence_to_record", 0.0) or 0.0)
        self.min_edge_to_record = float(self.lab_cfg.get("min_edge_to_record", 0.0) or 0.0)
        self.hypothetical_mode = str(self.lab_cfg.get("hypothetical_notional_mode", "flat") or "flat").lower()
        self.flat_notional_usd = float(self.lab_cfg.get("flat_notional_usd", 10.0) or 10.0)
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
        if self.mode == "resolve_only":
            self._update_state(mode=self.mode, last_run_id=run_id, paused_reason="resolve_only")
            return PredictionLabRunResult(run_id=run_id, scanned_markets=0, recorded_predictions=0, group_counts={}, series_counts={}, ledger_path=str(self.predictions_path))

        markets = exchange.get_markets(limit=self.max_markets_per_run)
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

            confidence = float(signal.get("confidence", 0.0) or 0.0)
            edge = float(signal.get("edge", 0.0) or 0.0)
            if confidence < self.min_confidence_to_record or edge < self.min_edge_to_record:
                continue

            decision_type = self._decision_type(signal)
            if self.mode == "collector" and self.collector_record_market_snapshots:
                append_jsonl(self.market_snapshots_path, self._build_market_snapshot_row(run_id, market, signal, decision_type=decision_type))

            should_record_prediction = (
                decision_type in {"buy_yes", "buy_no"}
                and (self.mode != "collector" or self.collector_record_predictions)
                and not self.score_only
            )
            if should_record_prediction:
                row = self._build_prediction_row(run_id, market, signal, decision_type=decision_type)
                append_jsonl(self.predictions_path, row)
                recorded += 1

            if self.mode == "seed_and_watch" and recorded >= self.max_new_predictions_per_seed:
                break

        paused_reason = "seed_complete" if self.mode == "seed_and_watch" else None
        self._update_state(
            mode=self.mode,
            last_run_id=run_id,
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
                if outcome not in {"YES", "NO"}:
                    continue
                action = row.get("direction", "SKIP")
                fee_model = ReplayFeeModel(profit_fee_rate=float(self.config.get("kalshi_fee_rate", 0.07) or 0.07))
                position_size = float((row.get("hypothetical") or {}).get("notional_usd", self.flat_notional_usd) or self.flat_notional_usd)
                if self.use_sizing_logic:
                    position_size = position_size
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
                    "entry_price": scored.get("entry_price"),
                    "quoted_entry_price": scored.get("quoted_entry_price"),
                }
                append_jsonl(self.resolutions_path, row)
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

    def _build_prediction_row(self, run_id: str, market, signal: dict[str, Any], *, decision_type: str) -> dict[str, Any]:
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
        return {
            "prediction_id": f"{run_id}_{market.id}",
            "run_id": run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
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
            "weather_context": context,
            "hypothetical": {
                "mode": self.hypothetical_mode,
                "notional_usd": self.flat_notional_usd,
            },
        }

    def _build_market_snapshot_row(self, run_id: str, market, signal: dict[str, Any], *, decision_type: str) -> dict[str, Any]:
        metadata = dict(getattr(market, "metadata", {}) or {})
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
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
            "recorded_prediction": decision_type in {"buy_yes", "buy_no"},
        }

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
            return {
                "mode": self.mode,
                "last_run_id": None,
                "paused_reason": None,
                "open_prediction_count": 0,
                "resolved_prediction_count": 0,
            }
        try:
            with locked_file(self.state_path, "r") as fh:
                return json.load(fh)
        except Exception:
            return {
                "mode": self.mode,
                "last_run_id": None,
                "paused_reason": None,
                "open_prediction_count": 0,
                "resolved_prediction_count": 0,
            }

    def _update_state(self, **updates: Any) -> None:
        self.state = {**self.state, **updates}
        atomic_write_json(self.state_path, self.state)

    def _count_open_predictions(self, rows: Optional[list[dict[str, Any]]] = None) -> int:
        rows = rows if rows is not None else load_jsonl(self.predictions_path)
        return sum(1 for row in rows if row.get("status") == "open")

    def _count_resolved_predictions(self, rows: Optional[list[dict[str, Any]]] = None) -> int:
        rows = rows if rows is not None else load_jsonl(self.predictions_path)
        return sum(1 for row in rows if row.get("status") == "resolved")

    def _fetch_market_outcome(self, exchange, market_id: str) -> Optional[str]:
        fetch_raw = getattr(exchange, "_fetch_market_raw", None)
        if not callable(fetch_raw):
            return None
        raw = fetch_raw(market_id)
        if not isinstance(raw, dict):
            return None
        result = str(raw.get("result") or raw.get("settlement_value") or "").upper()
        if result in {"YES", "NO"}:
            return result
        close_price = raw.get("close_price")
        if close_price in (1, 1.0, "1", "1.0"):
            return "YES"
        if close_price in (0, 0.0, "0", "0.0"):
            return "NO"
        status = str(raw.get("status") or "").lower()
        if status == "settled":
            yes_sub_title = str(raw.get("subtitle") or "")
            if "yes" in yes_sub_title.lower():
                return "YES"
        return None

    def _group_allowed(self, group: str) -> bool:
        normalized = str(group or "unknown").lower()
        if normalized in self.groups:
            return True
        if normalized != "weather" and not self.allow_non_weather:
            return False
        return normalized in self.groups

    def _prediction_ledger_lock(self):
        return locked_file(self.predictions_path.with_suffix(".lock"), "a+")

    @staticmethod
    def _decision_type(signal: dict[str, Any]) -> str:
        direction = str(signal.get("direction") or "SKIP").upper()
        if direction == "BUY_YES":
            return "buy_yes"
        if direction == "BUY_NO":
            return "buy_no"
        return "skip"
