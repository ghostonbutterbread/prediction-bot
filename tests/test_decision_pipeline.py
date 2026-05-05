import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from bot.config import load_config
from bot.decision_pipeline import DecisionPipelineEvaluator, build_fixed_opportunity_account_state
from bot.prediction_lab import PredictionLab
from bot.risk import RiskDecision
from bot.strategies.enhanced import EnhancedStrategyEngine, StrategyTrace
from bot.strategies.signal_validator import ValidationResult


@dataclass
class FakeMarket:
    id: str = "mkt-1"
    exchange: str = "kalshi"
    question: str = "Will the high temperature in New York exceed 71 degrees?"
    yes_price: float = 0.4
    no_price: float = 0.6
    volume: int = 10_000
    category: str = "KXHIGHNY"
    metadata: dict = field(default_factory=dict)
    closes_at = None


class FixedKelly:
    def __init__(self, size: float = 10.0):
        self.size = size
        self.fee_rate = 0.07

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


class TracedStrategy:
    def __init__(self, signal: dict):
        self.signal = signal

    def analyze_market_with_trace(self, market, order_book=None):
        trace = StrategyTrace(
            raw_signals={"unit": {"predicted_prob": self.signal.get("model_probability")}},
            accepted_signals={"unit": {"predicted_prob": self.signal.get("model_probability")}},
            ensemble_signal=dict(self.signal),
        )
        return dict(self.signal), trace


class UntracedNoneStrategy:
    def analyze_market(self, market, order_book=None):
        return None


class FakeValidator:
    def validate_all(self, signals, market):
        return {
            "price": ValidationResult(True, 0.9, 0.55),
            "volume": ValidationResult(False, 0.0, 0.51, rejection_reason="unit_rejected"),
        }


class DecisionPipelineTests(unittest.TestCase):
    def test_strategy_trace_records_raw_validation_accepted_rejected_ensemble_and_skip_reason(self):
        strategy = EnhancedStrategyEngine({"enable_news": False, "enable_social": False, "enable_ai": False, "min_edge": 0.3})
        strategy.validator = FakeValidator()
        strategy._price_signal = lambda market, order_book=None: {
            "signal_type": "price",
            "predicted_prob": 0.55,
            "confidence": 0.9,
        }
        strategy._volume_signal = lambda market: {
            "signal_type": "volume",
            "predicted_prob": 0.51,
            "confidence": 0.8,
        }
        strategy._live_data_signal = lambda market: None
        strategy._time_signal = lambda market: None

        signal, trace = strategy.analyze_market_with_trace(FakeMarket(yes_price=0.5, no_price=0.5))

        self.assertIsNone(signal)
        self.assertEqual(set(trace.raw_signals), {"price", "volume"})
        self.assertTrue(trace.validation_results["price"]["accepted"])
        self.assertFalse(trace.validation_results["volume"]["accepted"])
        self.assertIn("price", trace.accepted_signals)
        self.assertEqual(trace.rejected_signals["volume"]["rejection_reason"], "unit_rejected")
        self.assertEqual(trace.ensemble_signal["direction"], "BUY_YES")
        self.assertEqual(trace.skip_reason_code, "edge_below_threshold")

    def test_untraced_none_strategy_is_labeled(self):
        evaluator = DecisionPipelineEvaluator(
            {"strategy": {"min_edge": 0.01, "min_confidence": 0.5}},
            strategy=UntracedNoneStrategy(),
            kelly_sizer=FixedKelly(),
            risk_policy=AllowRisk(),
        )

        artifact = evaluator.evaluate(FakeMarket()).to_dict()

        self.assertEqual(artifact["final_action"], "SKIP")
        self.assertEqual(artifact["final_reason_code"], "strategy_returned_none_untraced")
        self.assertEqual(artifact["strategy_trace"]["skip_reason_code"], "strategy_returned_none_untraced")
        self.assertIsNone(artifact["shared_core_decision"])

    def test_evaluator_returns_trace_snapshot_context_shared_decision_and_final_reason(self):
        signal = {
            "market_id": "mkt-1",
            "exchange": "kalshi",
            "question": "Will the test happen?",
            "direction": "BUY_YES",
            "model_probability": 0.7,
            "market_price": 0.4,
            "yes_market_price": 0.4,
            "no_market_price": 0.6,
            "edge": 0.3,
            "confidence": 0.9,
            "signals": {"unit": 0.7},
        }
        evaluator = DecisionPipelineEvaluator(
            {"strategy": {"min_edge": 0.01, "min_confidence": 0.5}, "max_entry_price": 0.7},
            strategy=TracedStrategy(signal),
            kelly_sizer=FixedKelly(10.0),
            risk_policy=AllowRisk(),
        )

        artifact = evaluator.evaluate(
            FakeMarket(),
            account_state=build_fixed_opportunity_account_state(100.0),
            order_book={"best_yes_ask": 0.41, "best_yes_bid": 0.4, "best_no_ask": 0.6, "best_no_bid": 0.59},
        ).to_dict()

        self.assertIn("unit", artifact["strategy_trace"]["raw_signals"])
        self.assertEqual(artifact["execution_snapshot_source"], "book")
        self.assertEqual(artifact["trade_context"]["market_id"], "mkt-1")
        self.assertEqual(artifact["trade_context"]["source_context"]["direction"], "BUY_YES")
        self.assertEqual(artifact["shared_core_decision"]["reason_code"], "approved")
        self.assertEqual(artifact["final_action"], "BUY_YES")
        self.assertEqual(artifact["final_reason_code"], "approved")

    def test_hidden_gem_rejection_and_missing_order_book_are_in_prediction_lab_artifact(self):
        signal = {
            "market_id": "KXHIGHNY-260506-T71",
            "exchange": "kalshi",
            "question": "Will the high temperature in New York exceed 71 degrees?",
            "direction": "BUY_YES",
            "model_probability": 0.10,
            "market_price": 0.04,
            "yes_market_price": 0.04,
            "no_market_price": 0.96,
            "edge": 0.06,
            "confidence": 0.9,
            "signals": {"unit": 0.10},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            lab = PredictionLab(
                {
                    "data_dir": tmpdir,
                    "prediction_lab": {
                        "enabled": True,
                        "mode": "collector",
                        "groups": ["weather"],
                        "use_shared_pipeline": True,
                        "score_only": False,
                    },
                    "strategy": {"min_edge": 0.01, "min_confidence": 0.5},
                }
            )
            lab.decision_evaluator = DecisionPipelineEvaluator(
                lab.config,
                strategy=TracedStrategy(signal),
                kelly_sizer=FixedKelly(10.0),
                risk_policy=AllowRisk(),
            )
            market = FakeMarket(
                id="KXHIGHNY-260506-T71",
                question="Will the high temperature in New York exceed 71 degrees?",
                yes_price=0.04,
                no_price=0.96,
                metadata={"market_group": "weather", "market_family": "daily_temperature"},
            )

            artifact = lab._evaluate_shared_pipeline(market)
            row = lab._build_market_snapshot_row(
                "run-1",
                market,
                signal,
                decision_type="skip",
                prediction_recorded=False,
                decision_artifact=artifact,
            )

        self.assertEqual(
            row["decision_artifact"]["shared_core_decision"]["reason_code"],
            "hidden_gem_probability_multiple_below_threshold",
        )
        self.assertEqual(row["decision_artifact"]["order_book_snapshot"]["source"], "missing")
        self.assertEqual(row["decision_artifact"]["source_context"]["source"], "provided")
        self.assertEqual(row["shared_pipeline"]["order_book_source"], "missing")
        self.assertEqual(row["decision_artifact"]["execution_snapshot_source"], "fallback")

    def test_prediction_lab_shared_pipeline_config_default_is_off(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.yaml"
            path.write_text("prediction_lab:\n  enabled: true\n")

            config = load_config(path)

        self.assertFalse(config["prediction_lab"]["use_shared_pipeline"])

    def test_repo_config_enables_shared_pipeline_without_disabling_observer_or_score_only(self):
        config = load_config(Path(__file__).resolve().parents[1] / "config.yaml")

        prediction_lab = config["prediction_lab"]
        self.assertTrue(prediction_lab["use_shared_pipeline"])
        self.assertEqual(prediction_lab["mode"], "collector")
        self.assertTrue(prediction_lab["observer_mode"])
        self.assertTrue(prediction_lab["score_only"])


if __name__ == "__main__":
    unittest.main()
