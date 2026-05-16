import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.agent_decision_ledger import build_agent_decision_rows_from_source_row, build_agent_run_id
from bot.decision_pipeline import DecisionPipelineEvaluator
from bot.file_ops import append_jsonl
from bot.prediction_lab_replay import (
    LiveCurrentSourceForbiddenError,
    ReplayArtifactInput,
    _market_from_record,
    build_replay_series_grid,
    classify_order_book_mode,
    classify_replay_row_quality,
    classify_source_mode,
    load_replay_artifacts,
    replay_from_paths,
    replay_recorded_artifacts,
    validate_prediction_lab_tables,
)
from bot.risk import RiskDecision
from bot.strategies.enhanced import StrategyTrace
from bot.strategies.signal_validator import SignalValidator


class FixedKelly:
    fee_rate = 0.07

    def __init__(self, size: float = 10.0):
        self.size = size

    def calculate(self, win_probability: float, entry_price: float, bankroll: float) -> float:
        return self.size


class AllowRisk:
    def check_trade(self, signal: dict, position_size: float, *, available_cash: float | None = None):
        return RiskDecision(
            approved=True,
            reason="Approved",
            adjusted_size=position_size,
            original_size=position_size,
        )


class DenyRisk:
    def check_trade(self, signal: dict, position_size: float, *, available_cash: float | None = None):
        return RiskDecision(
            approved=False,
            reason="Unit risk denial",
            adjusted_size=0.0,
            original_size=position_size,
            metadata={"reason_code": "risk_unit_denied"},
        )


class FixedSignalStrategy:
    def __init__(self, signal: dict | None, *, skip_reason_code: str | None = None):
        self.signal = signal
        self.skip_reason_code = skip_reason_code

    def analyze_market_with_trace(self, market, order_book=None):
        trace = StrategyTrace(
            raw_signals={"unit": {"provided": self.signal is not None}},
            accepted_signals={"unit": dict(self.signal)} if self.signal else {},
            rejected_signals={"unit": {"reason": self.skip_reason_code}} if self.signal is None else {},
            ensemble_signal=dict(self.signal) if self.signal else None,
            skip_reason_code=self.skip_reason_code,
        )
        return (dict(self.signal) if self.signal else None), trace


class LiveTouchingStrategy(FixedSignalStrategy):
    def _live_data_signal(self, market):
        return {"signal_type": "weather", "predicted_prob": 0.9, "confidence": 0.9}

    def analyze_market_with_trace(self, market, order_book=None):
        self._live_data_signal(market)
        return super().analyze_market_with_trace(market, order_book)


class RecordedLiveSourceStrategy:
    def __init__(self):
        self.live_calls = 0

    def _live_data_signal(self, market):
        self.live_calls += 1
        raise AssertionError("live source should not be called during recorded replay")

    def analyze_market_with_trace(self, market, order_book=None):
        source_signal = self._live_data_signal(market)
        if not source_signal:
            trace = StrategyTrace(skip_reason_code="no_recorded_live_source")
            return None, trace

        predicted = float(source_signal["predicted_prob"])
        confidence = float(source_signal.get("confidence", 0.9))
        yes_price = float(getattr(market, "yes_price", 0.0))
        no_price = float(getattr(market, "no_price", 1 - yes_price))
        no_prob = 1 - predicted
        yes_edge = predicted - yes_price
        no_edge = no_prob - no_price
        if yes_edge >= no_edge:
            direction = "BUY_YES"
            edge = yes_edge
            market_price = yes_price
        else:
            direction = "BUY_NO"
            edge = no_edge
            market_price = no_price
        signal = {
            "market_id": getattr(market, "id", ""),
            "exchange": getattr(market, "exchange", "kalshi"),
            "question": getattr(market, "question", ""),
            "direction": direction,
            "model_probability": round(predicted, 4),
            "market_price": round(market_price, 4),
            "yes_market_price": yes_price,
            "no_market_price": no_price,
            "edge": round(edge, 4),
            "confidence": confidence,
            "signals": {"live": predicted},
            "signal_details": {"live": dict(source_signal)},
        }
        trace = StrategyTrace(
            raw_signals={"live": dict(source_signal)},
            accepted_signals={"live": dict(source_signal)},
            ensemble_signal=dict(signal),
        )
        return signal, trace


class ReplayValidatingRecordedSourceStrategy:
    def __init__(self):
        self.validator = SignalValidator()

    def _live_data_signal(self, market):
        raise AssertionError("live source should not be called during recorded replay")

    def analyze_market_with_trace(self, market, order_book=None):
        source_signal = self._live_data_signal(market)
        validation = self.validator.validate_all({"live": source_signal}, market)["live"]
        trace = StrategyTrace(
            raw_signals={"live": dict(source_signal)},
            validation_results={
                "live": {
                    "accepted": validation.accepted,
                    "adjusted_confidence": validation.adjusted_confidence,
                    "adjusted_prob": validation.adjusted_prob,
                    "warnings": list(validation.warnings),
                    "rejection_reason": validation.rejection_reason,
                }
            },
        )
        if not validation.accepted:
            trace.rejected_signals["live"] = dict(source_signal)
            trace.skip_reason_code = "no_validated_signals"
            return None, trace

        predicted = float(validation.adjusted_prob)
        yes_price = float(getattr(market, "yes_price", 0.0))
        signal = {
            "market_id": getattr(market, "id", ""),
            "exchange": getattr(market, "exchange", "kalshi"),
            "question": getattr(market, "question", ""),
            "direction": "BUY_YES",
            "model_probability": round(predicted, 4),
            "market_price": yes_price,
            "yes_market_price": yes_price,
            "no_market_price": getattr(market, "no_price", None),
            "edge": round(predicted - yes_price, 4),
            "confidence": validation.adjusted_confidence,
            "signals": {"live": predicted},
            "signal_details": {"live": dict(source_signal)},
        }
        trace.accepted_signals["live"] = dict(source_signal)
        trace.ensemble_signal = dict(signal)
        return signal, trace


class CommonLiveMethodStrategy(FixedSignalStrategy):
    def __init__(self, method_name: str):
        super().__init__(_signal())
        self.method_name = method_name

    def _news_signal(self, market):
        raise AssertionError("news source should have been guarded")

    def _social_signal(self, market):
        raise AssertionError("social source should have been guarded")

    def _ai_signal(self, market):
        raise AssertionError("ai source should have been guarded")

    def analyze_market_with_trace(self, market, order_book=None):
        getattr(self, self.method_name)(market)
        return super().analyze_market_with_trace(market, order_book)


def _signal() -> dict:
    return {
        "market_id": "KXHIGHNY-26APR29-T80",
        "exchange": "kalshi",
        "question": "Will NYC high temperature be above 80 degrees?",
        "series_ticker": "KXHIGHNY",
        "event_ticker": "KXHIGHNY-26APR29",
        "direction": "BUY_YES",
        "model_probability": 0.72,
        "market_price": 0.42,
        "yes_market_price": 0.42,
        "no_market_price": 0.58,
        "edge": 0.30,
        "confidence": 0.9,
        "signals": {"unit": 0.72},
    }


def _row(*, artifact_patch: dict | None = None, row_patch: dict | None = None) -> dict:
    artifact = {
        "market_id": "KXHIGHNY-26APR29-T80",
        "mode": "prediction_lab",
        "observed_at": "2026-04-29T12:00:00+00:00",
        "as_of": "2026-04-29T12:00:00+00:00",
        "strategy_signal": _signal(),
        "source_context": {
            "source": "provided",
            "mode": "prediction_lab",
            "as_of": "2026-04-29T12:00:00+00:00",
            "data": {
                "market_metadata": {
                    "market_group": "weather",
                    "series": "daily_temperature",
                    "series_ticker": "KXHIGHNY",
                    "event_ticker": "KXHIGHNY-26APR29",
                },
                "unit_source": {"value": 1},
                "weather_source_snapshot": {
                    "mode": "recorded_as_of",
                    "source_name": "weather",
                    "signal_type": "weather",
                    "as_of": "2026-04-29T12:00:00+00:00",
                    "forecast": {"high": 84.0, "threshold": 80.0, "question_side": "above"},
                    "date_validation": {"ok": True, "reason": "matched", "market_date": "2026-04-29", "weather_date": "2026-04-29"},
                },
            },
        },
        "order_book_snapshot": {
            "source": "book",
            "data": {
                "best_yes_ask": 0.43,
                "best_yes_bid": 0.42,
                "best_no_ask": 0.59,
                "best_no_bid": 0.58,
            },
        },
        "pre_logic_order_book_snapshot": {
            "source": "book",
            "stage": "pre_logic",
            "data": {
                "best_yes_ask": 0.43,
                "best_yes_bid": 0.42,
                "best_no_ask": 0.59,
                "best_no_bid": 0.58,
            },
        },
        "post_logic_order_book_snapshot": {
            "source": "book",
            "stage": "post_logic",
            "data": {
                "best_yes_ask": 0.43,
                "best_yes_bid": 0.42,
                "best_no_ask": 0.59,
                "best_no_bid": 0.58,
            },
        },
        "decision_latency_ms": 12.5,
        "execution_feasibility": {
            "artifact_version": 1,
            "mode": "passive_snapshot_comparison",
            "feasible": True,
            "status": "feasible",
            "action": "BUY_YES",
            "side": "yes",
            "pre_logic_ask": 0.43,
            "post_logic_ask": 0.43,
            "ask_delta": 0.0,
            "max_slippage": 0.01,
            "max_elapsed_ms": 2000,
            "decision_latency_ms": 12.5,
            "elapsed_ms": 13.0,
            "same_market": True,
            "market_open": True,
            "same_market_open": True,
            "same_side_ask_present": True,
            "ask_unchanged": True,
            "ask_within_slippage": True,
            "quantity_check_available": False,
            "sufficient_quantity": None,
            "elapsed_within_threshold": True,
            "failed_checks": [],
            "mutates_paper_state": False,
        },
        "execution_snapshot_source": "book",
        "final_action": "BUY_YES",
        "final_reason_code": "approved",
    }
    if artifact_patch:
        artifact.update(artifact_patch)
    row = {
        "timestamp": "2026-04-29T12:00:00+00:00",
        "observed_at": "2026-04-29T12:00:00+00:00",
        "market_id": "KXHIGHNY-26APR29-T80",
        "group": "weather",
        "series": "daily_temperature",
        "question": "Will NYC high temperature be above 80 degrees?",
        "yes_market_price": 0.42,
        "no_market_price": 0.58,
        "direction": "BUY_YES",
        "decision_type": "buy_yes",
        "decision_artifact": artifact,
    }
    if row_patch:
        row.update(row_patch)
    return row


def _weather_row(*, artifact_patch: dict | None = None, row_patch: dict | None = None) -> dict:
    weather_snapshot = {
        "mode": "recorded_as_of",
        "source_name": "weather",
        "signal_type": "weather",
        "as_of": "2026-04-29T12:00:00+00:00",
        "predicted_prob": 0.72,
        "confidence": 0.9,
        "forecast": {"high": 84.0, "threshold": 80.0, "question_side": "above"},
        "date_validation": {"ok": True, "reason": "matched", "market_date": "2026-04-29", "weather_date": "2026-04-29"},
    }
    row = _row(
        artifact_patch={
            "source_context": {
                "source": "provided",
                "source_mode": "recorded_as_of",
                "as_of": "2026-04-29T12:00:00+00:00",
                "data": {
                    "market_metadata": {
                        "market_group": "weather",
                        "series": "daily_temperature",
                        "event_ticker": "KXHIGHNY-26APR29",
                    },
                    "weather_source_snapshot": weather_snapshot,
                },
            },
            "source_snapshots": [
                {
                    "mode": "recorded_as_of",
                    "source": "weather",
                    "method": "_live_data_signal",
                    "snapshot_ref": "source_context.data.weather_source_snapshot",
                }
            ],
        },
        row_patch={
            "market_id": "KXHIGHNY-26APR29-T80",
            "group": "weather",
            "series": "daily_temperature",
            "event_ticker": "KXHIGHNY-26APR29",
            "question": "Will NYC high temperature be above 80?",
        },
    )
    if artifact_patch:
        row["decision_artifact"].update(artifact_patch)
    if row_patch:
        row.update(row_patch)
    return row


def _source_reliability_ledger_row(
    index: int,
    *,
    known_after: str,
    correct: bool = True,
    source_id: str = "nws",
) -> dict:
    return {
        "schema_version": 1,
        "observation_id": f"ledger-{index}",
        "market_id": f"KXHIGHNY-26APR{index:02d}-T80",
        "shared_candidate_id": f"ledger-candidate-{index}",
        "source_id": source_id,
        "source_name": source_id,
        "city_id": "unknown",
        "market_kind": "high",
        "contract_shape": "tail",
        "observed_at": "2026-04-01T12:00:00+00:00",
        "market_date": "2026-04-01",
        "resolved_at": known_after,
        "known_after": known_after,
        "forecast_temp_f": 84.0 if correct else 76.0,
        "threshold": 80.0,
        "question_side": "above",
        "actual_temp_f": 85.0,
        "predicted_outcome": "YES" if correct else "NO",
        "actual_outcome": "YES",
        "direction_correct": correct,
        "absolute_error_f": 1.0 if correct else 9.0,
        "bias_f": -1.0 if correct else -9.0,
        "eligible_for_reliability": True,
        "exclusion_reason": None,
    }


def _hidden_gem_card(
    *,
    market_id: str = "KXHIGHNY-26APR29-T80",
    shape: str = "bucket",
    tier: str = "normal",
    entry_price: float | None = 0.03,
    distribution_probability: float | None = None,
    weather_reject: str | None = None,
    beta_reject: str | None = None,
) -> dict:
    card = {
        "artifact_version": 1,
        "lane": "hidden_gem",
        "market_id": market_id,
        "weather_shape": shape,
        "hidden_gem_tier": tier,
        "entry_price": entry_price,
        "reason_codes": {
            "weather_reject": weather_reject,
            "beta_reject": beta_reject,
            "resize": None,
        },
    }
    if shape == "bucket":
        card["bucket"] = {"distribution_probability": distribution_probability}
    return card


def _row_with_hidden_gem_card(
    *,
    market_id: str,
    card: dict,
    missing_book: bool = False,
) -> dict:
    artifact_patch = {
        "market_id": market_id,
        "shared_core_decision": {
            "approved": True,
            "reason_code": "approved",
            "reasoning": {"hidden_gem_evidence_card": card},
        },
    }
    if missing_book:
        artifact_patch.update(
            {
                "order_book_snapshot": {"source": "missing", "data": None},
                "pre_logic_order_book_snapshot": {"source": "missing", "data": None},
                "post_logic_order_book_snapshot": {"source": "missing", "data": None},
                "execution_snapshot": {"source": "missing"},
                "execution_snapshot_source": "missing",
            }
        )
    return _weather_row(
        artifact_patch=artifact_patch,
        row_patch={
            "market_id": market_id,
            "snapshot_key": market_id,
            "decision_type": "buy_yes",
            "direction": "BUY_YES",
        },
    )


class PredictionLabReplayTests(unittest.TestCase):
    def _evaluator(self, strategy, risk_policy=None):
        return DecisionPipelineEvaluator(
            {"strategy": {"min_edge": 0.01, "min_confidence": 0.5}, "max_entry_price": 0.7},
            strategy=strategy,
            kelly_sizer=FixedKelly(10.0),
            risk_policy=risk_policy or AllowRisk(),
        )

    def test_replay_loads_recorded_prediction_artifacts_and_produces_comparison_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "predictions.jsonl"
            append_jsonl(path, _row())

            records = load_replay_artifacts([path])
            result = replay_recorded_artifacts(records, evaluator=self._evaluator(FixedSignalStrategy(_signal())))

        self.assertEqual(result.summary["total"], 1)
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0].original_action, "BUY_YES")
        self.assertEqual(result.rows[0].replayed_action, "BUY_YES")
        self.assertFalse(result.rows[0].action_changed)
        self.assertEqual(result.rows[0].source_path, str(path))

    def test_replay_summary_includes_source_reliability_shadow_without_changing_actions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "predictions.jsonl"
            scoreboard_path = Path(tmpdir) / "source_scoreboard_by_slice.jsonl"
            row = _weather_row()
            row["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]["sources"] = [
                {"source_name": "nws", "forecast_high": 84.0}
            ]
            append_jsonl(path, row)
            append_jsonl(
                scoreboard_path,
                {
                    "source_id": "nws",
                    "source_name": "nws",
                    "city_id": "unknown",
                    "market_kind": "high",
                    "contract_shape": "tail",
                    "sample_count": 100,
                    "threshold_direction_accuracy": 0.95,
                },
            )

            result = replay_from_paths(
                [path],
                evaluator=self._evaluator(FixedSignalStrategy(_signal())),
                source_reliability_scoreboard=scoreboard_path,
            )

        self.assertEqual(result.rows[0].original_action, "BUY_YES")
        self.assertEqual(result.rows[0].replayed_action, "BUY_YES")
        self.assertFalse(result.rows[0].action_changed)
        summary = result.summary["source_reliability_shadow"]
        self.assertEqual(summary["evaluated_rows"], 1)
        self.assertEqual(summary["trusted_support_rows"], 1)
        self.assertEqual(summary["unchanged_rows"], 1)
        self.assertEqual(summary["action_counts"], {"BUY_YES": 1})
        self.assertEqual(result.source_reliability_shadow_rows[0]["replayed_action"], "BUY_YES")

    def test_rolling_source_reliability_ledger_is_as_of_per_candidate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "predictions.jsonl"
            ledger_path = Path(tmpdir) / "source_outcome_ledger.json"
            early_row = _weather_row(
                row_patch={
                    "observed_at": "2026-04-29T12:00:00+00:00",
                    "timestamp": "2026-04-29T12:00:00+00:00",
                },
                artifact_patch={"observed_at": "2026-04-29T12:00:00+00:00"},
            )
            late_row = _weather_row(
                row_patch={
                    "observed_at": "2026-05-01T12:00:00+00:00",
                    "timestamp": "2026-05-01T12:00:00+00:00",
                },
                artifact_patch={"observed_at": "2026-05-01T12:00:00+00:00"},
            )
            for row in (early_row, late_row):
                row["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]["sources"] = [
                    {"source_name": "nws", "forecast_high": 84.0}
                ]
                append_jsonl(path, row)

            ledger_rows = [
                _source_reliability_ledger_row(
                    index,
                    known_after=f"2026-04-28T{index // 60:02d}:{index % 60:02d}:00+00:00",
                )
                for index in range(99)
            ]
            ledger_rows.append(
                _source_reliability_ledger_row(
                    99,
                    known_after="2026-04-30T00:00:00+00:00",
                )
            )
            ledger_path.write_text(json.dumps(ledger_rows), encoding="utf-8")

            result = replay_from_paths(
                [path],
                evaluator=self._evaluator(FixedSignalStrategy(_signal())),
                source_reliability_ledger=ledger_path,
            )

        self.assertEqual([row.replayed_action for row in result.rows], ["BUY_YES", "BUY_YES"])
        shadow_rows = result.source_reliability_shadow_rows
        self.assertEqual(len(shadow_rows), 2)
        self.assertEqual(shadow_rows[0]["reliability_mode"], "rolling_as_of")
        self.assertEqual(shadow_rows[0]["as_of"], "2026-04-29T12:00:00+00:00")
        self.assertEqual(shadow_rows[0]["ledger_considered_rows"], 99)
        self.assertEqual(shadow_rows[0]["source_votes"][0]["reliability"]["sample_count"], 99)
        self.assertEqual(shadow_rows[0]["reliability_recommended_action"], "SKIP")
        self.assertEqual(shadow_rows[0]["reason_code"], "no_trusted_support")
        self.assertEqual(shadow_rows[1]["as_of"], "2026-05-01T12:00:00+00:00")
        self.assertEqual(shadow_rows[1]["ledger_considered_rows"], 100)
        self.assertEqual(shadow_rows[1]["source_votes"][0]["reliability"]["sample_count"], 100)
        self.assertEqual(shadow_rows[1]["reliability_recommended_action"], "BUY_YES")
        self.assertEqual(shadow_rows[1]["reason_code"], "trusted_support")

    def test_rolling_source_reliability_missing_candidate_timestamp_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "predictions.jsonl"
            ledger_path = Path(tmpdir) / "source_outcome_ledger.jsonl"
            row = _weather_row(
                row_patch={"observed_at": None, "timestamp": None},
                artifact_patch={"observed_at": None, "timestamp": None},
            )
            row["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]["sources"] = [
                {"source_name": "nws", "forecast_high": 84.0}
            ]
            append_jsonl(path, row)
            append_jsonl(
                ledger_path,
                _source_reliability_ledger_row(0, known_after="2026-04-28T00:00:00+00:00"),
            )

            result = replay_from_paths(
                [path],
                evaluator=self._evaluator(FixedSignalStrategy(_signal())),
                source_reliability_ledger=ledger_path,
            )

        shadow_row = result.source_reliability_shadow_rows[0]
        self.assertEqual(result.rows[0].replayed_action, "BUY_YES")
        self.assertEqual(shadow_row["reliability_mode"], "rolling_as_of")
        self.assertIsNone(shadow_row["as_of"])
        self.assertEqual(shadow_row["reliability_effect"], "unavailable")
        self.assertEqual(shadow_row["reason_code"], "missing_candidate_observed_at")
        self.assertEqual(shadow_row["ledger_considered_rows"], 0)
        self.assertEqual(shadow_row["replayed_action"], "BUY_YES")

    def test_source_reliability_scoreboard_and_ledger_are_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "predictions.jsonl"
            scoreboard_path = Path(tmpdir) / "source_scoreboard_by_slice.jsonl"
            ledger_path = Path(tmpdir) / "source_outcome_ledger.jsonl"
            append_jsonl(path, _weather_row())
            append_jsonl(scoreboard_path, {"source_id": "nws", "sample_count": 100})
            append_jsonl(
                ledger_path,
                _source_reliability_ledger_row(0, known_after="2026-04-28T00:00:00+00:00"),
            )

            with self.assertRaisesRegex(ValueError, "either source_reliability_scoreboard or source_reliability_ledger"):
                replay_from_paths(
                    [path],
                    evaluator=self._evaluator(FixedSignalStrategy(_signal())),
                    source_reliability_scoreboard=scoreboard_path,
                    source_reliability_ledger=ledger_path,
                )

    def test_replay_loader_rejects_shadow_delta_compact_review_export_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "shadow_review.jsonl"
            append_jsonl(
                path,
                {
                    "schema_version": 1,
                    "row_type": "prediction_lab_shadow_delta_compact_review",
                    "market_id": "KXHIGHNY-26APR29-T80",
                    "observed_at": "2026-04-29T12:00:00+00:00",
                    "shadow_delta": {"changed": True},
                },
            )

            with self.assertRaises(ValueError):
                load_replay_artifacts([path])

            validation = validate_prediction_lab_tables([path])

        self.assertFalse(validation.ok)
        self.assertEqual(validation.issues[0].code, "shadow_delta_review_not_replay_input")

    def test_replay_summary_counts_shadow_delta_once_without_adding_replay_rows(self):
        shadow_delta = {
            "schema_version": 1,
            "mode": "beta_shadow_delta",
            "status": "complete",
            "comparison_complete": True,
            "action_comparison_available": True,
            "stable": {"action": "SKIP", "selected_lane": "edge"},
            "shadow": {"action": "BUY_YES", "selected_lane": "hidden_gem"},
            "changed": True,
            "action_changed": True,
            "side_changed": True,
            "buy_decision_changed": True,
            "reason_changed": True,
            "size_changed": False,
            "lane_changed": True,
            "dedupe_key": "KXHIGHNY-26APR29-T80|run-1|beta-shadow",
            "evidence_sources": ["beta_lane_gate"],
        }
        prediction = _row(
            row_patch={
                "run_id": "run-1",
                "prediction_id": "p1",
                "recorded_prediction": True,
                "shadow_delta": shadow_delta,
            }
        )
        snapshot = _row(
            row_patch={
                "run_id": "run-1",
                "recorded_prediction": False,
                "shadow_delta": {**shadow_delta, "changed": False, "action_changed": False},
            }
        )

        result = replay_recorded_artifacts(
            [prediction, snapshot],
            evaluator=self._evaluator(FixedSignalStrategy(_signal())),
        )

        self.assertEqual(result.summary["input_total"], 2)
        self.assertEqual(result.summary["total"], 2)
        summary = result.summary["shadow_delta"]
        self.assertEqual(summary["total_shadow_delta_rows"], 2)
        self.assertEqual(summary["total_shadow_delta_opportunities"], 1)
        self.assertEqual(summary["deduped_duplicate_rows"], 1)
        self.assertEqual(summary["changed_rows"], 1)
        self.assertEqual(summary["action_changed"], 1)

    def test_changed_reason_and_action_are_reported(self):
        result = replay_recorded_artifacts(
            [_row()],
            evaluator=self._evaluator(FixedSignalStrategy(_signal()), risk_policy=DenyRisk()),
        )

        comparison = result.rows[0]
        self.assertEqual(comparison.original_action, "BUY_YES")
        self.assertEqual(comparison.replayed_action, "SKIP")
        self.assertEqual(comparison.original_reason_code, "approved")
        self.assertEqual(comparison.replayed_reason_code, "risk_unit_denied")
        self.assertTrue(comparison.action_changed)
        self.assertTrue(comparison.reason_changed)
        self.assertEqual(result.summary["action_changed"], 1)
        self.assertEqual(result.summary["reason_changed"], 1)

    def test_replay_summary_includes_original_strategy_lane_deltas(self):
        row = _row(
            artifact_patch={
                "final_action": "SKIP",
                "final_reason_code": "edge_below_threshold",
                "shared_core_decision": {
                    "approved": False,
                    "reason_code": "edge_below_threshold",
                    "reasoning": {
                        "strategy_lane": {
                            "lane_id": "edge",
                            "allowed": True,
                            "reason_code": "edge_lane_selected",
                            "evidence": {
                                "beta_lane_gate": {
                                    "lane_id": "confidence_slow_profit",
                                    "allowed": True,
                                    "reason_code": "confidence_slow_profit_lane_selected",
                                    "differs_from_final": True,
                                }
                            },
                        }
                    },
                },
            }
        )

        result = replay_recorded_artifacts(
            [row],
            evaluator=self._evaluator(FixedSignalStrategy(_signal())),
        )

        original = result.summary["strategy_lanes"]["original"]
        self.assertEqual(original["lane_rows"], 1)
        self.assertEqual(original["selected_lane_counts"], {"edge": 1})
        self.assertEqual(original["would_select_lane_counts"], {"confidence_slow_profit": 1})
        self.assertEqual(original["would_select_slow_profit_rows"], 1)
        self.assertEqual(original["slow_profit_differs_from_final_rows"], 1)

    def test_replay_summary_includes_original_and_replayed_lane_sizing_deltas(self):
        row = _row(
            artifact_patch={
                "shared_core_decision": {
                    "approved": True,
                    "reason_code": "approved",
                    "reasoning": {
                        "strategy_lane": {
                            "lane_id": "edge",
                            "allowed": True,
                            "reason_code": "edge_lane_selected",
                            "evidence": {},
                        },
                        "lane_sizing": {
                            "lane_id": "edge",
                            "configured": True,
                            "requested_size": 10.0,
                            "beta_adjusted_size": 4.0,
                            "would_adjust_size": True,
                            "applied": False,
                            "preserved_stable_size": True,
                        },
                    },
                },
            }
        )

        result = replay_recorded_artifacts(
            [row],
            config={
                "strategy": {"min_edge": 0.01, "min_confidence": 0.5},
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
                "max_entry_price": 0.7,
                "strategy_lanes": {
                    "sizing": {
                        "edge": {"max_position_usd": 2.0},
                    },
                },
                "strategy_policy": {
                    "version": "beta",
                    "beta": {
                        "mode": "enforce",
                        "features": {"lane_sizing_caps": True},
                    },
                },
            },
            evaluator=self._evaluator(FixedSignalStrategy(_signal())),
        )

        original = result.summary["strategy_lanes"]["original"]
        self.assertEqual(original["lane_sizing_rows"], 1)
        self.assertEqual(original["lane_sizing_configured_rows"], 1)
        self.assertEqual(original["lane_sizing_would_adjust_rows"], 1)
        self.assertEqual(original["lane_sizing_preserved_rows"], 1)
        self.assertEqual(original["lane_sizing_size_totals"]["requested"], 10.0)
        self.assertEqual(original["lane_sizing_size_totals"]["beta_adjusted"], 4.0)

        replayed = result.summary["strategy_lanes"]["replayed"]
        self.assertEqual(replayed["lane_sizing_rows"], 1)
        self.assertEqual(replayed["lane_sizing_configured_rows"], 1)
        self.assertEqual(replayed["lane_sizing_would_adjust_rows"], 1)
        self.assertEqual(replayed["lane_sizing_applied_rows"], 1)
        self.assertEqual(replayed["lane_sizing_selected_lane_counts"], {"edge": 1})
        self.assertEqual(replayed["lane_sizing_size_totals"]["requested"], 10.0)
        self.assertEqual(replayed["lane_sizing_size_totals"]["beta_adjusted"], 2.0)
        self.assertEqual(replayed["lane_sizing_size_totals"]["applied"], 2.0)

    def test_recorded_source_and_order_book_modes_are_labeled(self):
        row = _row()

        result = replay_recorded_artifacts([row], evaluator=self._evaluator(FixedSignalStrategy(_signal())))

        self.assertEqual(classify_source_mode(row["decision_artifact"], row), "recorded_as_of")
        self.assertEqual(classify_order_book_mode(row["decision_artifact"]), "recorded_book")
        self.assertEqual(result.rows[0].source_mode, "recorded_as_of")
        self.assertEqual(result.rows[0].order_book_mode, "recorded_book")
        self.assertEqual(result.rows[0].execution_snapshot_mode, "recorded_book")
        self.assertEqual(result.rows[0].category, "replay_grade_original")
        self.assertTrue(result.rows[0].include_in_strict)
        self.assertEqual(result.summary["quality_counts"]["replay_grade_original"], 1)
        self.assertEqual(result.summary["strict_row_count"], 1)

    def test_row_quality_classifies_fully_replay_grade_row(self):
        row = _row()

        quality = classify_replay_row_quality(row["decision_artifact"], row)

        self.assertEqual(quality.category, "replay_grade_original")
        self.assertEqual(quality.reasons, [])
        self.assertTrue(quality.is_replay_grade_strict)
        self.assertTrue(quality.include_in_strict)

    def test_buy_row_without_execution_feasibility_is_coverage_only(self):
        row = _row()
        row["decision_artifact"].pop("execution_feasibility", None)
        row["decision_artifact"].pop("post_logic_order_book_snapshot", None)

        quality = classify_replay_row_quality(row["decision_artifact"], row)

        self.assertEqual(quality.category, "coverage_only")
        self.assertIn("missing_execution_feasibility", quality.reasons)
        self.assertFalse(quality.include_in_strict)

    def test_backfilled_buy_row_without_execution_feasibility_stays_coverage_only(self):
        row = _row(artifact_patch={"source_provenance": "historical_replay_backfill"})
        row["decision_artifact"].pop("execution_feasibility", None)
        row["decision_artifact"].pop("post_logic_order_book_snapshot", None)

        quality = classify_replay_row_quality(row["decision_artifact"], row)

        self.assertEqual(quality.category, "coverage_only")
        self.assertIn("missing_execution_feasibility", quality.reasons)
        self.assertFalse(quality.is_replay_grade_strict)
        self.assertFalse(quality.include_in_strict)

    def test_row_quality_classifies_missing_weather_snapshot(self):
        row = _weather_row(
            artifact_patch={
                "source_context": {
                    "source": "provided",
                    "source_mode": "recorded_as_of",
                    "data": {
                        "market_metadata": {
                            "market_group": "weather",
                            "series": "daily_temperature",
                            "event_ticker": "KXHIGHNY-26APR29",
                        }
                    },
                },
                "source_snapshots": [],
            }
        )

        result = replay_recorded_artifacts([row], evaluator=self._evaluator(FixedSignalStrategy(_signal())))

        self.assertEqual(result.rows[0].category, "missing_weather_snapshot")
        self.assertIn("missing_weather_snapshot", result.rows[0].reasons)
        self.assertFalse(result.rows[0].include_in_strict)
        self.assertEqual(result.summary["excluded_row_count"], 1)

    def test_row_quality_classifies_missing_order_book(self):
        row = _row(
            artifact_patch={
                "order_book_snapshot": {"source": "missing", "data": None},
                "execution_snapshot_source": "missing",
            }
        )

        result = replay_recorded_artifacts([row], evaluator=self._evaluator(FixedSignalStrategy(_signal())))

        self.assertEqual(result.rows[0].category, "missing_order_book")
        self.assertIn("missing_order_book", result.rows[0].reasons)
        self.assertFalse(result.rows[0].include_in_strict)

    def test_row_quality_classifies_malformed_book_without_prices_as_missing_order_book(self):
        row = _row(
            artifact_patch={
                "order_book_snapshot": {"source": "book", "data": {"unrelated": "value"}},
                "execution_snapshot_source": "book",
            }
        )

        result = replay_recorded_artifacts([row], evaluator=self._evaluator(FixedSignalStrategy(_signal())))

        self.assertEqual(classify_order_book_mode(row["decision_artifact"]), "missing")
        self.assertEqual(result.rows[0].category, "missing_order_book")
        self.assertIn("missing_order_book", result.rows[0].reasons)

    def test_row_quality_classifies_zero_asks_as_missing_order_book(self):
        row = _row(
            artifact_patch={
                "order_book_snapshot": {
                    "source": "book",
                    "data": {"best_yes_ask": 0, "best_no_ask": None, "best_yes_bid": 0.40, "best_no_bid": 0.59},
                },
                "execution_snapshot": {"source": "book", "best_yes_ask": 0, "best_no_ask": None},
                "execution_snapshot_source": "book",
            }
        )

        result = replay_recorded_artifacts([row], evaluator=self._evaluator(FixedSignalStrategy(_signal())))

        self.assertEqual(classify_order_book_mode(row["decision_artifact"]), "missing")
        self.assertEqual(result.rows[0].category, "missing_order_book")
        self.assertIn("missing_order_book", result.rows[0].reasons)

    def test_row_quality_classifies_bid_only_book_as_missing_order_book(self):
        row = _row(
            artifact_patch={
                "order_book_snapshot": {"source": "book", "data": {"best_yes_bid": 0.40, "best_no_bid": 0.59}},
                "execution_snapshot": {"source": "book", "best_yes_bid": 0.40, "best_no_bid": 0.59},
                "execution_snapshot_source": "book",
            }
        )

        result = replay_recorded_artifacts([row], evaluator=self._evaluator(FixedSignalStrategy(_signal())))

        self.assertEqual(classify_order_book_mode(row["decision_artifact"]), "missing")
        self.assertEqual(result.rows[0].category, "missing_order_book")
        self.assertIn("missing_order_book", result.rows[0].reasons)

    def test_row_quality_classifies_date_unverified(self):
        row = _weather_row()
        row["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]["date_validation"] = {
            "ok": False,
            "reason": "missing_weather_date",
            "market_date": "2026-04-29",
            "weather_date": None,
        }

        result = replay_recorded_artifacts([row], evaluator=self._evaluator(FixedSignalStrategy(_signal())))

        self.assertEqual(result.rows[0].category, "date_unverified")
        self.assertTrue(any(reason.startswith("date_unverified") for reason in result.rows[0].reasons))
        self.assertFalse(result.rows[0].include_in_strict)

    def test_row_quality_classifies_ok_only_date_validation_as_date_unverified(self):
        row = _weather_row()
        row["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]["date_validation"] = {"ok": True}

        result = replay_recorded_artifacts([row], evaluator=self._evaluator(FixedSignalStrategy(_signal())))

        self.assertEqual(result.rows[0].category, "date_unverified")
        self.assertIn("date_unverified:missing_date_validation", result.rows[0].reasons)
        self.assertFalse(result.rows[0].include_in_strict)

    def test_row_quality_classifies_historical_replay_row_as_post_facto(self):
        row = _row(
            artifact_patch={
                "source_provenance": "historical_replay_backfill",
                "source_context": {
                    "source": "provided",
                    "source_mode": "historical_replay",
                    "data": {
                        "market_metadata": {"market_group": "weather", "series": "daily_temperature", "series_ticker": "KXHIGHNY", "event_ticker": "KXHIGHNY-26APR29"},
                        "unit_source": {"value": 1},
                        "weather_source_snapshot": {
                            "mode": "recorded_as_of",
                            "source_name": "weather",
                            "signal_type": "weather",
                            "as_of": "2026-04-29T12:00:00+00:00",
                            "forecast": {"high": 84.0, "threshold": 80.0, "question_side": "above"},
                            "date_validation": {"ok": True, "reason": "matched", "market_date": "2026-04-29", "weather_date": "2026-04-29"},
                        },
                    },
                },
            }
        )

        result = replay_recorded_artifacts([row], evaluator=self._evaluator(FixedSignalStrategy(_signal())))

        self.assertEqual(result.rows[0].source_mode, "historical_post_facto")
        self.assertEqual(result.rows[0].category, "historical_post_facto")
        self.assertIn("historical_post_facto", result.rows[0].reasons)
        self.assertFalse(result.rows[0].include_in_strict)

    def test_row_quality_classifies_nested_historical_replay_weather_signal_as_post_facto(self):
        row = _weather_row()
        weather_snapshot = row["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]
        weather_snapshot["source_signal"] = {
            "signal_type": "weather",
            "predicted_prob": 0.72,
            "confidence": 0.9,
            "data": {
                "historical_replay": True,
                "source_quality": "settlement_station_official_daily",
            },
        }

        result = replay_recorded_artifacts([row], evaluator=self._evaluator(FixedSignalStrategy(_signal())))

        self.assertEqual(result.rows[0].source_mode, "historical_post_facto")
        self.assertEqual(result.rows[0].category, "historical_post_facto")
        self.assertIn("historical_post_facto", result.rows[0].reasons)
        self.assertNotIn("missing_weather_snapshot", result.rows[0].reasons)
        self.assertFalse(result.rows[0].include_in_strict)

    def test_row_quality_policy_strict_drops_incomplete_rows_from_result_rows(self):
        good = _row()
        missing_book = _row(
            artifact_patch={
                "market_id": "KXHIGHNY-26APR30-T80",
                "order_book_snapshot": {"source": "missing", "data": None},
                "execution_snapshot_source": "missing",
            },
            row_patch={"market_id": "KXHIGHNY-26APR30-T80"},
        )

        result = replay_recorded_artifacts(
            [good, missing_book],
            evaluator=self._evaluator(FixedSignalStrategy(_signal())),
            row_quality_policy="strict",
        )

        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0].category, "replay_grade_original")
        self.assertEqual(result.summary["input_total"], 2)
        self.assertEqual(result.summary["total"], 1)
        self.assertEqual(result.summary["strict_row_count"], 1)
        self.assertEqual(result.summary["excluded_row_count"], 1)
        self.assertEqual(result.summary["excluded_reason_counts"]["missing_order_book"], 1)

    def test_series_grid_uses_coverage_rows_when_strict_policy_filters_result_rows(self):
        good = _weather_row()
        missing_book = _weather_row(
            artifact_patch={
                "market_id": "KXHIGHNY-26APR29-T81",
                "order_book_snapshot": {"source": "missing", "data": None},
                "execution_snapshot_source": "missing",
            },
            row_patch={"market_id": "KXHIGHNY-26APR29-T81"},
        )

        result = replay_recorded_artifacts(
            [good, missing_book],
            evaluator=self._evaluator(FixedSignalStrategy(_signal()), risk_policy=DenyRisk()),
            row_quality_policy="strict",
        )
        grid = build_replay_series_grid(result)

        self.assertEqual(len(result.rows), 1)
        self.assertEqual(len(result.all_rows), 2)
        self.assertEqual(result.summary["input_total"], 2)
        self.assertEqual(grid[0]["total_rows"], 2)
        self.assertEqual(grid[0]["strict_rows"], 1)
        self.assertEqual(grid[0]["excluded_rows"], 1)
        self.assertEqual(grid[0]["quality_counts"]["missing_order_book"], 1)

    def test_series_grid_counts_quality_and_exclusions(self):
        good = _weather_row()
        missing_book = _weather_row(
            artifact_patch={
                "market_id": "KXHIGHNY-26APR29-T81",
                "order_book_snapshot": {"source": "missing", "data": None},
                "execution_snapshot_source": "missing",
            },
            row_patch={"market_id": "KXHIGHNY-26APR29-T81"},
        )

        result = replay_recorded_artifacts(
            [good, missing_book],
            evaluator=self._evaluator(FixedSignalStrategy(_signal()), risk_policy=DenyRisk()),
        )
        grid = build_replay_series_grid(result)

        self.assertEqual(len(grid), 1)
        self.assertEqual(grid[0]["series"], "daily_temperature")
        self.assertEqual(grid[0]["event_ticker"], "KXHIGHNY-26APR29")
        self.assertEqual(grid[0]["total_rows"], 2)
        self.assertEqual(grid[0]["strict_rows"], 1)
        self.assertEqual(grid[0]["excluded_rows"], 1)
        self.assertEqual(grid[0]["quality_counts"]["replay_grade_original"], 1)
        self.assertEqual(grid[0]["quality_counts"]["missing_order_book"], 1)
        self.assertEqual(grid[0]["excluded_counts"]["missing_order_book"], 1)
        self.assertEqual(grid[0]["action_changed"], 2)
        self.assertEqual(grid[0]["reason_changed"], 2)

    def test_weather_hidden_gem_replay_comparison_reports_bridge_and_bucket_thresholds(self):
        bridge_reason = "weather_bucket_hidden_gem_missing_distribution_probability"
        bad_bucket = _row_with_hidden_gem_card(
            market_id="KXHIGHNY-26APR29-T80",
            card=_hidden_gem_card(
                market_id="KXHIGHNY-26APR29-T80",
                entry_price=0.03,
                distribution_probability=None,
                beta_reject=bridge_reason,
            ),
        )
        winner_skipped = _row_with_hidden_gem_card(
            market_id="KXHIGHNY-26APR29-T81",
            card=_hidden_gem_card(
                market_id="KXHIGHNY-26APR29-T81",
                entry_price=0.03,
                distribution_probability=None,
                beta_reject=bridge_reason,
            ),
        )
        supported_bucket = _row_with_hidden_gem_card(
            market_id="KXHIGHNY-26APR29-T82",
            card=_hidden_gem_card(
                market_id="KXHIGHNY-26APR29-T82",
                entry_price=0.01,
                distribution_probability=0.24,
            ),
        )

        result = replay_recorded_artifacts(
            [bad_bucket, winner_skipped, supported_bucket],
            evaluator=self._evaluator(FixedSignalStrategy(_signal()), risk_policy=DenyRisk()),
            resolution_records=[
                {"market_id": "KXHIGHNY-26APR29-T80", "resolution": {"outcome": "NO", "resolved_at": "2026-04-30T00:00:00+00:00"}},
                {"market_id": "KXHIGHNY-26APR29-T81", "resolution": {"outcome": "YES", "resolved_at": "2026-04-30T00:00:00+00:00"}},
            ],
        )

        comparison = result.summary["weather_hidden_gem_comparison"]
        strict = comparison["strict"]

        self.assertEqual(comparison["basis"], "artifact_derived_conservative")
        self.assertEqual(strict["rows"], 3)
        self.assertEqual(strict["hidden_gem_evidence_cards"]["card_rows"], 3)
        self.assertEqual(strict["bucket_distribution"]["bucket_rows"], 3)
        self.assertEqual(strict["bucket_distribution"]["with_distribution_probability"], 1)
        self.assertEqual(strict["bucket_distribution"]["without_distribution_probability"], 2)
        self.assertEqual(
            strict["bucket_distribution"]["threshold_slices"]["distribution_probability_gte_entry_plus_0_05"],
            {"pass": 1, "fail": 0},
        )
        self.assertEqual(
            strict["bucket_distribution"]["threshold_slices"]["distribution_probability_gte_3x_entry"],
            {"pass": 1, "fail": 0},
        )
        self.assertEqual(strict["comparators"]["hotfix_bridge"]["inferred_rejections"], 2)
        self.assertEqual(strict["comparators"]["hotfix_bridge"]["bad_bucket_buys_removed"], 1)
        self.assertEqual(strict["comparators"]["hotfix_bridge"]["winners_skipped"], 1)
        self.assertEqual(strict["comparators"]["evidence_card"]["rejections"], 2)

    def test_weather_hidden_gem_replay_comparison_separates_strict_from_coverage_rows(self):
        strict_row = _row_with_hidden_gem_card(
            market_id="KXHIGHNY-26APR29-T80",
            card=_hidden_gem_card(market_id="KXHIGHNY-26APR29-T80", entry_price=0.01, distribution_probability=0.24),
        )
        coverage_row = _row_with_hidden_gem_card(
            market_id="KXHIGHNY-26APR29-T81",
            card={"reason_codes": {}},
            missing_book=True,
        )
        legacy_no_card = _weather_row(
            artifact_patch={"market_id": "KXHIGHNY-26APR29-T82"},
            row_patch={"market_id": "KXHIGHNY-26APR29-T82", "decision_type": "buy_yes", "direction": "BUY_YES"},
        )

        result = replay_recorded_artifacts(
            [strict_row, coverage_row, legacy_no_card],
            evaluator=self._evaluator(FixedSignalStrategy(_signal())),
            row_quality_policy="strict",
        )

        comparison = result.summary["weather_hidden_gem_comparison"]

        self.assertEqual(len(result.rows), 2)
        self.assertEqual(result.summary["input_total"], 3)
        self.assertEqual(comparison["strict"]["rows"], 2)
        self.assertEqual(comparison["strict"]["card_rows"], 1)
        self.assertEqual(comparison["coverage"]["rows"], 3)
        self.assertEqual(comparison["coverage"]["coverage_only_rows"], 1)
        self.assertEqual(comparison["coverage"]["card_rows"], 2)
        self.assertEqual(comparison["coverage"]["no_card_rows"], 1)
        self.assertEqual(comparison["coverage"]["hidden_gem_evidence_cards"]["insufficient_data_rows"], 1)

    def test_replay_summary_includes_dual_policy_main_vs_shadow_aggregate(self):
        avoided_loss = _row(
            artifact_patch={"market_id": "KXHIGHNY-26APR29-T80"},
            row_patch={
                "run_id": "run-dual",
                "market_id": "KXHIGHNY-26APR29-T80",
                "shared_candidate_id": "candidate-1",
                "main_runtime": "prediction_lab",
                "main_decision": {"action": "BUY_YES", "runtime": "prediction_lab", "authoritative": True},
                "normal_decision": {"action": "BUY_YES", "size": 10.0},
                "shadow_decision": {"action": "SKIP", "size": 0.0},
                "decision_delta": {"action_changed": True, "size_changed": True},
            },
        )
        shadow_win = _row(
            artifact_patch={"market_id": "KXHIGHNY-26APR29-T81"},
            row_patch={
                "run_id": "run-dual",
                "market_id": "KXHIGHNY-26APR29-T81",
                "question": "Will NYC high temperature be above 81 degrees?",
                "yes_market_price": 0.75,
                "no_market_price": 0.25,
                "direction": "SKIP",
                "decision_type": "skip",
                "shared_candidate_id": "candidate-2",
                "main_runtime": "prediction_lab",
                "main_decision": {"action": "SKIP", "runtime": "prediction_lab", "authoritative": True},
                "normal_decision": {"action": "SKIP", "size": 0.0},
                "shadow_decision": {"action": "BUY_NO", "size": 5.0},
                "decision_delta": {"action_changed": True, "size_changed": True},
            },
        )
        unresolved = _row(
            artifact_patch={"market_id": "KXHIGHNY-26APR29-T82"},
            row_patch={
                "run_id": "run-dual",
                "market_id": "KXHIGHNY-26APR29-T82",
                "shared_candidate_id": "candidate-3",
                "main_runtime": "prediction_lab",
                "main_decision": {"action": "BUY_YES", "runtime": "prediction_lab", "authoritative": True},
                "normal_decision": {"action": "BUY_YES", "size": 4.0},
                "shadow_decision": {"action": "SKIP", "size": 0.0},
                "decision_delta": {"action_changed": True, "size_changed": True},
            },
        )

        result = replay_recorded_artifacts(
            [avoided_loss, shadow_win, unresolved],
            evaluator=self._evaluator(FixedSignalStrategy(_signal())),
            resolution_records=[
                {
                    "run_id": "run-dual",
                    "market_id": "KXHIGHNY-26APR29-T80",
                    "shared_candidate_id": "candidate-1",
                    "resolution": {"outcome": "NO", "resolved_at": "2026-04-30T00:00:00+00:00"},
                },
                {
                    "run_id": "run-dual",
                    "market_id": "KXHIGHNY-26APR29-T81",
                    "shared_candidate_id": "candidate-2",
                    "resolution": {"outcome": "NO", "resolved_at": "2026-04-30T00:00:00+00:00"},
                },
            ],
        )

        summary = result.summary["dual_policy_replay_comparison"]
        self.assertEqual(summary["schema_version"], 1)
        self.assertEqual(summary["eligible_rows"], 3)
        self.assertEqual(summary["authoritative_main"]["decision_path"], "main_decision")
        self.assertEqual(summary["authoritative_main"]["normal_policy_path"], "normal_decision")
        self.assertEqual(summary["authoritative_main"]["runtime_counts"], {"prediction_lab": 3})
        self.assertTrue(summary["authoritative_main"]["authoritative"])
        self.assertEqual(summary["hypothetical_shadow"]["decision_path"], "shadow_decision")
        self.assertTrue(summary["hypothetical_shadow"]["non_mutating"])
        self.assertEqual(summary["decision_columns"]["skipped_by_shadow"], 2)
        self.assertEqual(summary["decision_columns"]["shadow_only_buys"], 1)
        self.assertEqual(summary["pnl_comparison"]["normal_buy_shadow_skip"]["avoided_loss_count"], 1)
        self.assertEqual(summary["pnl_comparison"]["normal_skip_shadow_buy"]["shadow_win_count"], 1)
        self.assertEqual(summary["pnl_comparison"]["unresolved_rows"], 1)

    def test_replay_summary_can_join_optional_agent_decision_input_by_shared_candidate_id(self):
        replay_row = _row(
            row_patch={
                "run_id": "run-ledger",
                "shared_candidate_id": "candidate-1",
                "main_decision": {"action": "BUY_YES", "runtime": "prediction_lab", "policy": "stable", "authoritative": True},
                "normal_decision": {"action": "BUY_YES", "policy": "stable", "size": 10.0},
                "shadow_decision": {"action": "SKIP", "policy": "beta_shadow", "size": 0.0},
            }
        )
        matched_decisions = build_agent_decision_rows_from_source_row(
            replay_row,
            agent_run_id=build_agent_run_id(agent_id="prediction_lab", run_id="run-ledger"),
            agent_id="prediction_lab",
            runtime="prediction_lab",
            candidate_dataset_path=Path("/tmp/prediction_lab/market_snapshots.jsonl"),
        )
        unmatched_row = _row(
            row_patch={
                "run_id": "run-other",
                "market_id": "KXHIGHNY-26APR29-T81",
                "shared_candidate_id": "candidate-2",
                "main_decision": {"action": "SKIP", "runtime": "prediction_lab", "policy": "stable", "authoritative": True},
                "normal_decision": {"action": "SKIP", "policy": "stable", "size": 0.0},
            }
        )
        unmatched_decisions = build_agent_decision_rows_from_source_row(
            unmatched_row,
            agent_run_id=build_agent_run_id(agent_id="prediction_lab", run_id="run-other"),
            agent_id="prediction_lab",
            runtime="prediction_lab",
            candidate_dataset_path=Path("/tmp/prediction_lab/market_snapshots.jsonl"),
        )

        result = replay_recorded_artifacts(
            [replay_row],
            evaluator=self._evaluator(FixedSignalStrategy(_signal())),
            decision_records=[*matched_decisions, *unmatched_decisions],
        )

        coverage = result.summary["agent_decision_coverage"]
        self.assertEqual(coverage["total_rows"], 3)
        self.assertEqual(coverage["distinct_shared_candidate_ids"], 1)
        self.assertEqual(coverage["requested_shared_candidate_ids"], 1)
        self.assertEqual(coverage["matched_shared_candidate_ids"], 1)
        self.assertEqual(coverage["unmatched_input_rows"], 2)
        self.assertEqual(coverage["missing_shared_candidate_ids"], [])
        self.assertEqual(coverage["by_shared_candidate_id"], {"candidate-1": 3})
        self.assertEqual(coverage["by_agent_id"], {"prediction_lab": 3})
        self.assertEqual(coverage["by_policy"], {"stable": 2, "beta_shadow": 1})
        self.assertEqual(coverage["by_decision_role"], {"main": 1, "normal": 1, "shadow": 1})
        report = result.summary["agent_decision_report"]
        self.assertEqual(report["total_rows"], 3)
        self.assertEqual(report["coverage"], coverage)
        self.assertEqual(report["policy_drift"]["candidate_count_with_action_drift"], 1)
        self.assertEqual(report["overlap"]["candidate_count_with_multiple_policies"], 1)
        self.assertEqual(report["outcomes"]["unresolved_rows"], 3)

    def test_weather_hidden_gem_replay_comparison_is_card_derived_not_replay_action_derived(self):
        approved_card_risk_skipped = _row_with_hidden_gem_card(
            market_id="KXHIGHNY-26APR29-T80",
            card=_hidden_gem_card(
                market_id="KXHIGHNY-26APR29-T80",
                entry_price=0.01,
                distribution_probability=0.24,
            ),
        )
        bridge_rejected_original_bad_buy = _row_with_hidden_gem_card(
            market_id="KXHIGHNY-26APR29-T81",
            card=_hidden_gem_card(
                market_id="KXHIGHNY-26APR29-T81",
                entry_price=0.03,
                distribution_probability=None,
                beta_reject="weather_bucket_hidden_gem_missing_distribution_probability",
            ),
        )

        result = replay_recorded_artifacts(
            [approved_card_risk_skipped, bridge_rejected_original_bad_buy],
            evaluator=self._evaluator(FixedSignalStrategy(_signal()), risk_policy=DenyRisk()),
            resolution_records=[
                {"market_id": "KXHIGHNY-26APR29-T81", "resolution": {"outcome": "NO", "resolved_at": "2026-04-30T00:00:00+00:00"}},
            ],
        )

        strict = result.summary["weather_hidden_gem_comparison"]["strict"]

        self.assertEqual(strict["comparators"]["evidence_card"]["approvals"], 1)
        self.assertEqual(strict["comparators"]["evidence_card"]["rejections"], 1)
        self.assertEqual(strict["hidden_gem_evidence_cards"]["approved_cards"], 1)
        self.assertEqual(strict["hidden_gem_evidence_cards"]["rejected_cards"], 1)
        self.assertEqual(strict["comparators"]["hotfix_bridge"]["bad_bucket_buys_removed"], 1)

    def test_weather_hidden_gem_replay_comparison_excludes_non_weather_rows(self):
        weather_row = _row_with_hidden_gem_card(
            market_id="KXHIGHNY-26APR29-T80",
            card=_hidden_gem_card(market_id="KXHIGHNY-26APR29-T80", entry_price=0.01, distribution_probability=0.24),
        )
        sports_row = _row(
            artifact_patch={
                "market_id": "SPORTS-1",
                "source_context": {
                    "source": "provided",
                    "data": {"market_metadata": {"market_group": "sports", "series": "unit"}},
                },
            },
            row_patch={"market_id": "SPORTS-1", "group": "sports", "series": "unit"},
        )

        result = replay_recorded_artifacts(
            [weather_row, sports_row],
            evaluator=self._evaluator(FixedSignalStrategy(_signal())),
        )

        strict = result.summary["weather_hidden_gem_comparison"]["strict"]

        self.assertEqual(result.summary["input_total"], 2)
        self.assertEqual(strict["rows"], 1)
        self.assertEqual(strict["card_rows"], 1)
        self.assertEqual(strict["no_card_rows"], 0)
        self.assertEqual(strict["comparators"]["recorded_or_pre_hotfix_proxy"]["buy_rows"], 1)

    def test_metadata_only_source_context_is_not_recorded_as_of(self):
        row = _row(
            artifact_patch={
                "source_context": {
                    "source": "provided",
                    "mode": "prediction_lab",
                    "as_of": "2026-04-29T12:00:00+00:00",
                    "data": {"market_metadata": {"market_group": "sports", "series": "unit", "series_ticker": "KXHIGHNY"}},
                }
            }
        )

        result = replay_recorded_artifacts([row], evaluator=self._evaluator(FixedSignalStrategy(_signal())))

        self.assertEqual(classify_source_mode(row["decision_artifact"], row), "missing")
        self.assertEqual(result.rows[0].source_mode, "missing")
        self.assertIn("source_mode_missing_for_historical_replay", result.rows[0].warnings)

    def test_recorded_source_snapshot_changes_replayed_behavior_without_live_calls(self):
        strategy = RecordedLiveSourceStrategy()
        high_source = {
            "mode": "recorded_as_of",
            "source": "weather",
            "as_of": "2026-04-29T12:00:00+00:00",
            "signal": {"signal_type": "weather", "predicted_prob": 0.82, "confidence": 0.92},
        }
        low_source = {
            "mode": "recorded_as_of",
            "source": "weather",
            "as_of": "2026-04-29T12:00:00+00:00",
            "signal": {"signal_type": "weather", "predicted_prob": 0.425, "confidence": 0.92},
        }

        result = replay_recorded_artifacts(
            [
                _row(
                    artifact_patch={"final_action": "SKIP", "source_snapshots": [high_source]},
                    row_patch={"decision_type": "skip", "direction": "SKIP"},
                ),
                _row(artifact_patch={"final_action": "SKIP", "source_snapshots": [low_source]}, row_patch={"decision_type": "skip", "direction": "SKIP"}),
            ],
            evaluator=self._evaluator(strategy),
            require_recorded_source=True,
            resolution_records=[
                {
                    "market_id": "KXHIGHNY-26APR29-T80",
                    "resolution": {"outcome": "YES", "resolved_at": "2026-04-30T00:00:00+00:00"},
                }
            ],
        )

        self.assertEqual(strategy.live_calls, 0)
        self.assertEqual([row.replayed_action for row in result.rows], ["BUY_YES", "SKIP"])
        self.assertEqual(result.summary["missed_wins"], 1)
        self.assertEqual(result.summary["over_filtered_wins"], 1)
        self.assertEqual(result.summary["outcomes"]["YES"], 2)

    def test_replay_ignores_inline_resolution_until_resolution_ledger_is_joined(self):
        strategy = RecordedLiveSourceStrategy()
        source = {
            "mode": "recorded_as_of",
            "source": "weather",
            "as_of": "2026-04-29T12:00:00+00:00",
            "signal": {"signal_type": "weather", "predicted_prob": 0.82, "confidence": 0.92},
        }
        row = _row(
            artifact_patch={"final_action": "SKIP", "source_snapshots": [source]},
            row_patch={"decision_type": "skip", "direction": "SKIP", "resolution": {"outcome": "YES"}},
        )

        blind = replay_recorded_artifacts([row], evaluator=self._evaluator(strategy), require_recorded_source=True)
        scored = replay_recorded_artifacts(
            [row],
            evaluator=self._evaluator(strategy),
            require_recorded_source=True,
            resolution_records=[
                {
                    "market_id": "KXHIGHNY-26APR29-T80",
                    "resolution": {"outcome": "YES", "resolved_at": "2026-04-30T00:00:00+00:00"},
                }
            ],
        )

        self.assertIsNone(blind.rows[0].outcome)
        self.assertEqual(blind.summary["outcomes"], {"unknown": 1})
        self.assertEqual(scored.rows[0].outcome, "YES")
        self.assertEqual(scored.summary["missed_wins"], 1)

    def test_score_only_market_snapshots_can_be_replayed_and_scored_from_resolution_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_path = Path(tmpdir) / "market_snapshots.jsonl"
            resolution_path = Path(tmpdir) / "resolutions.jsonl"
            append_jsonl(
                snapshot_path,
                _row(
                    artifact_patch={"source_snapshots": [{"mode": "recorded_as_of", "source": "weather", "signal": {"signal_type": "weather", "predicted_prob": 0.82, "confidence": 0.92}}]},
                    row_patch={
                        "timestamp": "2026-04-29T12:00:00+00:00",
                        "observed_at": "2026-04-29T12:00:00+00:00",
                        "snapshot_key": "KXHIGHNY-26APR29-T80",
                        "recorded_prediction": False,
                    },
                ),
            )
            append_jsonl(
                resolution_path,
                {
                    "market_id": "KXHIGHNY-26APR29-T80",
                    "resolution": {"outcome": "YES", "resolved_at": "2026-04-30T00:00:00+00:00"},
                },
            )

            records = load_replay_artifacts([snapshot_path])
            result = replay_recorded_artifacts(
                records,
                evaluator=self._evaluator(RecordedLiveSourceStrategy()),
                resolution_paths=[resolution_path],
            )

        self.assertEqual(result.rows[0].outcome, "YES")
        self.assertEqual(result.summary["outcomes"]["YES"], 1)

    def test_replay_from_paths_honors_prediction_lab_source_disables_for_score_only_snapshots(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_path = Path(tmpdir) / "market_snapshots.jsonl"
            append_jsonl(
                snapshot_path,
                _row(
                    artifact_patch={
                        "source_snapshots": [
                            {
                                "mode": "recorded_as_of",
                                "source": "weather",
                                "method": "_live_data_signal",
                                "signal": {"signal_type": "weather", "predicted_prob": 0.82, "confidence": 0.92},
                            }
                        ]
                    },
                    row_patch={"recorded_prediction": False},
                ),
            )
            config = {
                "prediction_lab": {"score_only": True, "disable_news": True, "disable_social": True, "disable_ai": True},
                "strategy": {"min_edge": 0.01, "min_confidence": 0.5},
                "max_entry_price": 0.7,
            }

            with (
                patch("bot.strategies.enhanced.EnhancedStrategyEngine._news_signal", side_effect=AssertionError("news disabled")),
                patch("bot.strategies.enhanced.EnhancedStrategyEngine._social_signal", side_effect=AssertionError("social disabled")),
                patch("bot.strategies.enhanced.EnhancedStrategyEngine._ai_signal", side_effect=AssertionError("ai disabled")),
            ):
                result = replay_from_paths([snapshot_path], config=config)

        self.assertEqual(result.summary["input_total"], 1)
        self.assertEqual(result.all_rows[0].source_mode, "recorded_as_of")

    def test_prediction_lab_validator_flags_duplicates_and_outcome_leakage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "market_snapshots.jsonl"
            row = _row(row_patch={"resolution": {"outcome": "YES"}})
            append_jsonl(path, row)
            append_jsonl(path, row)

            validation = validate_prediction_lab_tables([path])
            payload = validation.to_dict()

        self.assertFalse(validation.ok)
        self.assertEqual(payload["issue_counts"]["outcome_leakage"], 2)
        self.assertEqual(payload["issue_counts"]["duplicate_identity"], 1)

    def test_prediction_lab_validator_allows_resolution_ledger_outcomes_but_not_input_outcomes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_path = Path(tmpdir) / "market_snapshots.jsonl"
            leaked_input_path = Path(tmpdir) / "leaked_market_snapshots.jsonl"
            resolution_path = Path(tmpdir) / "resolutions.jsonl"
            snapshot_row = _row(row_patch={"recorded_prediction": False})
            snapshot_row["decision_artifact"]["source_context"]["data"]["market_metadata"].update({"outcome": None, "result": ""})
            append_jsonl(snapshot_path, snapshot_row)
            append_jsonl(leaked_input_path, _row(row_patch={"market_id": "KXHIGHNY-26APR30-T80", "resolution": {"outcome": "YES"}}))
            append_jsonl(
                resolution_path,
                {
                    "market_id": "KXHIGHNY-26APR29-T80",
                    "resolution": {"outcome": "YES", "resolved_at": "2026-04-30T00:00:00+00:00"},
                },
            )

            ledger_validation = validate_prediction_lab_tables(
                [snapshot_path, resolution_path],
                resolution_paths=[resolution_path],
            ).to_dict()
            leaked_validation = validate_prediction_lab_tables([leaked_input_path], resolution_paths=[resolution_path]).to_dict()

        self.assertNotIn("outcome_leakage", ledger_validation["issue_counts"])
        self.assertEqual(ledger_validation["total_rows"], 2)
        self.assertEqual(leaked_validation["issue_counts"]["outcome_leakage"], 1)

    def test_prediction_lab_validator_recognizes_execution_feasibility_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            good_path = Path(tmpdir) / "good_market_snapshots.jsonl"
            old_path = Path(tmpdir) / "old_market_snapshots.jsonl"
            old_row = _row()
            old_row["decision_artifact"].pop("execution_feasibility", None)
            old_row["decision_artifact"].pop("post_logic_order_book_snapshot", None)
            append_jsonl(good_path, _row())
            append_jsonl(old_path, old_row)

            good = validate_prediction_lab_tables([good_path]).to_dict()
            old = validate_prediction_lab_tables([old_path]).to_dict()

        self.assertNotIn("execution_feasibility_not_strict", good["issue_counts"])
        self.assertNotIn("row_quality_coverage_only", good["issue_counts"])
        self.assertEqual(old["issue_counts"]["execution_feasibility_not_strict"], 1)
        self.assertEqual(old["issue_counts"]["row_quality_coverage_only"], 1)

    def test_nested_weather_source_context_snapshot_is_recorded_as_of_and_replayable(self):
        strategy = RecordedLiveSourceStrategy()
        weather_snapshot = {
            "mode": "recorded_as_of",
            "source_name": "weather",
            "signal_name": "live",
            "signal_type": "weather",
            "market_date": "2026-04-29",
            "target_forecast_date": "2026-04-29",
            "as_of": "2026-04-29T12:00:00+00:00",
            "predicted_prob": 0.82,
            "confidence": 0.92,
            "station_id": "KNYC",
            "station_cli": "NYC",
            "station_mapping": "exact",
            "settlement_source": "nws",
            "source_agreement_score": 0.94,
            "forecast": {"high": 84.0, "low": 65.0, "current": 73.0, "threshold": 80.0, "question_side": "above"},
            "sources": [
                {
                    "source_name": "nws",
                    "role": "settlement_primary",
                    "weight": 1.0,
                    "contribution": 1.0,
                    "forecast_high": 84.0,
                    "forecast_low": 65.0,
                    "current_forecast": 73.0,
                    "target_forecast_date": "2026-04-29",
                    "market_date": "2026-04-29",
                    "fetched_at": "2026-04-29T12:00:00+00:00",
                    "station_id": "KNYC",
                    "station_cli": "NYC",
                    "station_mapping": "exact",
                    "settlement_source": "nws",
                },
                {
                    "source_name": "open-meteo",
                    "role": "cross_validation",
                    "weight": None,
                    "contribution": None,
                    "weight_note": "not_recorded_by_weather_engine",
                    "forecast_high": 82.0,
                    "forecast_low": 64.0,
                    "target_forecast_date": "2026-04-29",
                    "market_date": "2026-04-29",
                    "fetched_at": "2026-04-29T12:00:00+00:00",
                },
            ],
            "gaps": {"nws_open_meteo_gap": 2.0},
            "source_signal": {
                "signal_type": "weather",
                "predicted_prob": 0.82,
                "confidence": 0.92,
                "source_timestamp": "2026-04-29T12:00:00+00:00",
                "data": {"forecast_high": 84.0, "forecast_low": 65.0, "current_temp": 73.0, "sources": ["nws", "open-meteo"]},
            },
        }
        row = _row(
            artifact_patch={
                "final_action": "SKIP",
                "source_context": {
                    "source": "provided",
                    "source_mode": "recorded_as_of",
                    "mode": "prediction_lab",
                    "as_of": "2026-04-29T12:00:00+00:00",
                    "data": {
                        "market_metadata": {
                            "market_group": "weather",
                            "series": "daily_temperature",
                            "event_ticker": "KXHIGHNY-26APR29",
                        },
                        "weather_source_snapshot": weather_snapshot,
                    },
                },
                "source_snapshots": [],
            },
            row_patch={"group": "weather", "series": "daily_temperature", "decision_type": "skip", "direction": "SKIP", "volume": 1000},
        )

        result = replay_recorded_artifacts([row], evaluator=self._evaluator(strategy), require_recorded_source=True)

        self.assertEqual(strategy.live_calls, 0)
        self.assertEqual(classify_source_mode(row["decision_artifact"], row), "recorded_as_of")
        self.assertEqual(result.rows[0].source_mode, "recorded_as_of")
        self.assertEqual(result.rows[0].replayed_action, "BUY_YES")
        nested = result.rows[0].original_artifact["source_context"]["data"]["weather_source_snapshot"]
        self.assertEqual(nested["sources"][0]["weight"], 1.0)
        self.assertIsNone(nested["sources"][1]["weight"])
        self.assertEqual(nested["target_forecast_date"], "2026-04-29")

    def test_source_snapshot_ref_prefers_nested_weather_snapshot_over_legacy_signal(self):
        strategy = RecordedLiveSourceStrategy()
        weather_snapshot = {
            "mode": "recorded_as_of",
            "source_name": "weather",
            "signal_type": "weather",
            "as_of": "2026-04-29T12:00:00+00:00",
            "predicted_prob": 0.82,
            "confidence": 0.92,
            "forecast": {"high": 84.0, "low": 65.0, "actual_temp_used": 84.0, "predicted_temp": 84.0, "threshold": 80.0, "question_side": "above"},
            "source_signal": {
                "signal_type": "weather",
                "predicted_prob": 0.82,
                "confidence": 0.92,
                "data": {"actual_temp_used": 84.0},
            },
        }
        row = _row(
            artifact_patch={
                "final_action": "SKIP",
                "source_context": {
                    "source": "provided",
                    "source_mode": "recorded_as_of",
                    "data": {
                        "market_metadata": {
                            "market_group": "weather",
                            "series": "daily_temperature",
                            "event_ticker": "KXHIGHNY-26APR29",
                        },
                        "weather_source_snapshot": weather_snapshot,
                    },
                },
                "source_snapshots": [
                    {
                        "mode": "recorded_as_of",
                        "source": "weather",
                        "method": "_live_data_signal",
                        "snapshot_ref": "source_context.data.weather_source_snapshot",
                        "signal": {"signal_type": "weather", "predicted_prob": 0.12, "confidence": 0.92},
                    }
                ],
            },
            row_patch={"group": "weather", "series": "daily_temperature", "decision_type": "skip", "direction": "SKIP", "volume": 1000},
        )

        result = replay_recorded_artifacts([row], evaluator=self._evaluator(strategy), require_recorded_source=True)

        self.assertEqual(strategy.live_calls, 0)
        self.assertEqual(result.rows[0].replayed_action, "BUY_YES")
        replayed_signal = result.rows[0].replayed_artifact["strategy_signal"]["signal_details"]["live"]
        self.assertEqual(replayed_signal["predicted_prob"], 0.82)

    def test_nested_weather_snapshot_without_source_signal_reconstructs_live_data_shape(self):
        weather_snapshot = {
            "mode": "recorded_as_of",
            "source_name": "weather",
            "signal_type": "weather",
            "as_of": "2026-04-29T12:00:00+00:00",
            "source_timestamp": "2026-04-29T11:59:00+00:00",
            "ttl_seconds": 600,
            "predicted_prob": 0.82,
            "confidence": 0.92,
            "station_id": "KNYC",
            "station_cli": "NYC",
            "station_mapping": "exact",
            "settlement_source": "nws",
            "source_agreement_score": 0.94,
            "target_forecast_date": "2026-04-30",
            "forecast_date": "2026-04-29",
            "date_validation": {"ok": False, "reason": "unit_mismatch", "market_date": "2026-04-30", "weather_date": None},
            "forecast": {
                "high": 84.0,
                "low": 65.0,
                "current": 73.0,
                "actual_temp_used": 84.0,
                "predicted_temp": 84.0,
                "threshold": 80.0,
                "question_side": "above",
            },
            "sources": [
                {
                    "source_name": "nws",
                    "role": "settlement_primary",
                    "weight": 1.0,
                    "forecast_high": 84.0,
                    "forecast_low": 65.0,
                    "current_forecast": 73.0,
                    "station_id": "KNYC",
                    "station_cli": "NYC",
                }
            ],
        }
        row = _row(
            artifact_patch={
                "final_action": "SKIP",
                "source_context": {
                    "source": "provided",
                    "source_mode": "recorded_as_of",
                    "data": {
                        "market_metadata": {
                            "market_group": "weather",
                            "series": "daily_temperature",
                            "event_ticker": "KXHIGHNY-26APR29",
                        },
                        "weather_source_snapshot": weather_snapshot,
                    },
                },
                "source_snapshots": [
                    {
                        "mode": "recorded_as_of",
                        "source": "weather",
                        "method": "_live_data_signal",
                        "snapshot_ref": "source_context.data.weather_source_snapshot",
                    }
                ],
            },
            row_patch={"group": "weather", "series": "daily_temperature", "decision_type": "skip", "direction": "SKIP", "volume": 1000},
        )

        result = replay_recorded_artifacts(
            [row],
            evaluator=self._evaluator(ReplayValidatingRecordedSourceStrategy()),
            require_recorded_source=True,
        )

        self.assertEqual(result.rows[0].replayed_action, "BUY_YES")
        replayed_signal = result.rows[0].replayed_artifact["strategy_signal"]["signal_details"]["live"]
        data = replayed_signal["data"]
        self.assertEqual(replayed_signal["source_timestamp"], "2026-04-29T11:59:00+00:00")
        self.assertEqual(replayed_signal["ttl_seconds"], 600)
        self.assertEqual(data["forecast_high"], 84.0)
        self.assertEqual(data["forecast_low"], 65.0)
        self.assertEqual(data["current_temp"], 73.0)
        self.assertEqual(data["actual_temp_used"], 84.0)
        self.assertEqual(data["predicted_temp"], 84.0)
        self.assertEqual(data["threshold"], 80.0)
        self.assertEqual(data["sources"], ["nws"])
        self.assertEqual(data["source_details"][0]["source_name"], "nws")
        self.assertEqual(data["station_id"], "KNYC")
        self.assertEqual(data["date_validation"]["reason"], "unit_mismatch")
        self.assertEqual(data["target_forecast_date"], "2026-04-30")
        self.assertEqual(data["weather_date"], "2026-04-29")

    def test_recorded_source_staleness_is_checked_against_artifact_as_of(self):
        row = _row(
            artifact_patch={
                "as_of": None,
                "final_action": "SKIP",
                "source_snapshots": [
                    {
                        "mode": "recorded_as_of",
                        "source": "weather",
                        "as_of": "2026-04-29T12:00:00+00:00",
                        "signal": {
                            "signal_type": "weather",
                            "predicted_prob": 0.82,
                            "confidence": 0.92,
                            "source_timestamp": "2026-04-29T11:59:00+00:00",
                            "ttl_seconds": 600,
                            "question_side": "above",
                            "data": {"actual_temp_used": 84.0},
                        },
                    }
                ],
            },
            row_patch={"decision_type": "skip", "direction": "SKIP", "volume": 1000},
        )

        result = replay_recorded_artifacts(
            [row],
            evaluator=self._evaluator(ReplayValidatingRecordedSourceStrategy()),
            require_recorded_source=True,
        )

        self.assertEqual(result.rows[0].replayed_action, "BUY_YES")
        live_validation = result.rows[0].replayed_artifact["strategy_trace"]["validation_results"]["live"]
        self.assertTrue(live_validation["accepted"])
        self.assertFalse(any("stale" in warning.lower() for warning in live_validation["warnings"]))

    def test_historical_replay_does_not_silently_use_live_current_source_data(self):
        evaluator = self._evaluator(LiveTouchingStrategy(_signal()))
        live_row = _row(artifact_patch={"source_context": {"source": "live_current", "data": {}}})

        with self.assertRaises(LiveCurrentSourceForbiddenError):
            replay_recorded_artifacts([live_row], evaluator=evaluator)

    def test_broader_live_current_source_methods_are_guarded(self):
        for method_name in ("_news_signal", "_social_signal", "_ai_signal"):
            with self.subTest(method_name=method_name):
                evaluator = self._evaluator(CommonLiveMethodStrategy(method_name))
                with self.assertRaises(LiveCurrentSourceForbiddenError):
                    replay_recorded_artifacts([_row()], evaluator=evaluator)

    def test_live_current_policy_can_warn_and_skip_source_call(self):
        evaluator = self._evaluator(LiveTouchingStrategy(_signal()))
        live_row = _row(artifact_patch={"source_context": {"source": "live_current", "data": {}}})

        result = replay_recorded_artifacts([live_row], evaluator=evaluator, live_source_policy="warn_skip")

        self.assertEqual(result.rows[0].replayed_action, "BUY_YES")
        self.assertIn("live_current_source_forbidden_for_historical_replay:KXHIGHNY-26APR29-T80", result.rows[0].warnings)

    def test_require_recorded_source_rejects_metadata_only_context(self):
        row = _row(
            artifact_patch={
                "source_context": {
                    "source": "provided",
                    "mode": "prediction_lab",
                    "data": {"market_metadata": {"market_group": "sports", "series": "unit", "series_ticker": "KXHIGHNY"}},
                }
            }
        )

        with self.assertRaises(LiveCurrentSourceForbiddenError):
            replay_recorded_artifacts([row], evaluator=self._evaluator(FixedSignalStrategy(_signal())), require_recorded_source=True)

    def test_recorded_execution_snapshot_overrides_reconstructed_fallback_price(self):
        expensive_signal = _signal()
        expensive_signal.update(
            {
                "model_probability": 0.95,
                "market_price": 0.80,
                "yes_market_price": 0.80,
                "no_market_price": 0.20,
                "edge": 0.15,
            }
        )
        row = _row(
            artifact_patch={
                "strategy_signal": expensive_signal,
                "order_book_snapshot": {"source": "missing", "data": None},
                "execution_snapshot_source": "fallback",
                "execution_snapshot": {
                    "source": "fallback",
                    "direction": "BUY_YES",
                    "market_price": 0.42,
                    "yes_price": 0.42,
                    "no_price": 0.58,
                    "best_yes_ask": 0.42,
                    "best_no_ask": 0.58,
                    "best_yes_bid": 0.41,
                    "best_no_bid": 0.57,
                    "estimated_fill_price": 0.42,
                },
            }
        )

        result = replay_recorded_artifacts([row], evaluator=self._evaluator(FixedSignalStrategy(expensive_signal)))

        self.assertEqual(result.rows[0].replayed_action, "BUY_YES")
        self.assertEqual(result.rows[0].execution_snapshot_mode, "signal_price_fallback")
        self.assertEqual(result.rows[0].replayed_artifact["execution_snapshot"]["market_price"], 0.42)
        self.assertEqual(result.rows[0].replayed_artifact["execution_snapshot_source"], "fallback")

    def test_replay_market_prefers_embedded_shared_candidate_market_evidence(self):
        row = {
            "run_id": "run-1",
            "market_id": "KX-LEGACY",
            "shared_candidate": {
                "schema_name": "shared_market_candidate",
                "candidate_id": "candidate-1",
                "market_id": "KX-SHARED",
                "market": {
                    "id": "KX-SHARED",
                    "exchange": "kalshi",
                    "question": "Will shared evidence be preferred?",
                    "series": "KXSHARED",
                    "group": "weather",
                    "event_ticker": "KXSHARED-26MAY",
                    "volume": 321,
                },
                "prices": {"yes_price": 0.37, "no_price": 0.63},
            },
        }
        record = ReplayArtifactInput(row=row, artifact={"final_action": "SKIP"})

        market = _market_from_record(record)

        self.assertEqual(market.id, "KX-LEGACY")
        self.assertEqual(market.question, "Will shared evidence be preferred?")
        self.assertEqual(market.yes_price, 0.37)
        self.assertEqual(market.no_price, 0.63)
        self.assertEqual(market.volume, 321)
        self.assertEqual(market.metadata["series"], "KXSHARED")
        self.assertEqual(market.metadata["event_ticker"], "KXSHARED-26MAY")


if __name__ == "__main__":
    unittest.main()
