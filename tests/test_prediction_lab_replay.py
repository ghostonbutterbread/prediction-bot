import tempfile
import unittest
from pathlib import Path

from bot.decision_pipeline import DecisionPipelineEvaluator
from bot.file_ops import append_jsonl
from bot.prediction_lab_replay import (
    LiveCurrentSourceForbiddenError,
    classify_order_book_mode,
    classify_source_mode,
    load_replay_artifacts,
    replay_recorded_artifacts,
)
from bot.risk import RiskDecision
from bot.strategies.enhanced import StrategyTrace


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
        "market_id": "KXUNIT-26APR29-YES",
        "exchange": "kalshi",
        "question": "Will the unit test pass?",
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
        "market_id": "KXUNIT-26APR29-YES",
        "mode": "prediction_lab",
        "observed_at": "2026-04-29T12:00:00+00:00",
        "as_of": "2026-04-29T12:00:00+00:00",
        "strategy_signal": _signal(),
        "source_context": {
            "source": "provided",
            "mode": "prediction_lab",
            "as_of": "2026-04-29T12:00:00+00:00",
            "data": {
                "market_metadata": {"market_group": "sports", "series": "unit"},
                "unit_source": {"value": 1},
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
        "execution_snapshot_source": "book",
        "final_action": "BUY_YES",
        "final_reason_code": "approved",
    }
    if artifact_patch:
        artifact.update(artifact_patch)
    row = {
        "timestamp": "2026-04-29T12:00:00+00:00",
        "observed_at": "2026-04-29T12:00:00+00:00",
        "market_id": "KXUNIT-26APR29-YES",
        "group": "sports",
        "series": "unit",
        "question": "Will the unit test pass?",
        "yes_market_price": 0.42,
        "no_market_price": 0.58,
        "direction": "BUY_YES",
        "decision_type": "buy_yes",
        "decision_artifact": artifact,
    }
    if row_patch:
        row.update(row_patch)
    return row


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

    def test_recorded_source_and_order_book_modes_are_labeled(self):
        row = _row()

        result = replay_recorded_artifacts([row], evaluator=self._evaluator(FixedSignalStrategy(_signal())))

        self.assertEqual(classify_source_mode(row["decision_artifact"], row), "recorded_as_of")
        self.assertEqual(classify_order_book_mode(row["decision_artifact"]), "recorded_book")
        self.assertEqual(result.rows[0].source_mode, "recorded_as_of")
        self.assertEqual(result.rows[0].order_book_mode, "recorded_book")
        self.assertEqual(result.rows[0].execution_snapshot_mode, "recorded_book")

    def test_metadata_only_source_context_is_not_recorded_as_of(self):
        row = _row(
            artifact_patch={
                "source_context": {
                    "source": "provided",
                    "mode": "prediction_lab",
                    "as_of": "2026-04-29T12:00:00+00:00",
                    "data": {"market_metadata": {"market_group": "sports", "series": "unit"}},
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
                    row_patch={"decision_type": "skip", "direction": "SKIP", "resolution": {"outcome": "YES"}},
                ),
                _row(artifact_patch={"final_action": "SKIP", "source_snapshots": [low_source]}, row_patch={"decision_type": "skip", "direction": "SKIP"}),
            ],
            evaluator=self._evaluator(strategy),
            require_recorded_source=True,
        )

        self.assertEqual(strategy.live_calls, 0)
        self.assertEqual([row.replayed_action for row in result.rows], ["BUY_YES", "SKIP"])
        self.assertEqual(result.summary["missed_wins"], 1)
        self.assertEqual(result.summary["over_filtered_wins"], 1)
        self.assertEqual(result.summary["outcomes"]["YES"], 1)

    def test_historical_replay_does_not_silently_use_live_current_source_data(self):
        evaluator = self._evaluator(LiveTouchingStrategy(_signal()))

        with self.assertRaises(LiveCurrentSourceForbiddenError):
            replay_recorded_artifacts([_row()], evaluator=evaluator)

    def test_broader_live_current_source_methods_are_guarded(self):
        for method_name in ("_news_signal", "_social_signal", "_ai_signal"):
            with self.subTest(method_name=method_name):
                evaluator = self._evaluator(CommonLiveMethodStrategy(method_name))
                with self.assertRaises(LiveCurrentSourceForbiddenError):
                    replay_recorded_artifacts([_row()], evaluator=evaluator)

    def test_live_current_policy_can_warn_and_skip_source_call(self):
        evaluator = self._evaluator(LiveTouchingStrategy(_signal()))

        result = replay_recorded_artifacts([_row()], evaluator=evaluator, live_source_policy="warn_skip")

        self.assertEqual(result.rows[0].replayed_action, "BUY_YES")
        self.assertIn("live_current_source_forbidden_for_historical_replay:KXUNIT-26APR29-YES", result.rows[0].warnings)

    def test_require_recorded_source_rejects_metadata_only_context(self):
        row = _row(
            artifact_patch={
                "source_context": {
                    "source": "provided",
                    "mode": "prediction_lab",
                    "data": {"market_metadata": {"market_group": "sports", "series": "unit"}},
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


if __name__ == "__main__":
    unittest.main()
