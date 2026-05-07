import unittest
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bot.shared_core import AccountState, TradeContext, build_trade_decision
from bot.simulator import Simulator
from bot.strategy_lanes import (
    CONFIDENCE_SLOW_PROFIT_LANE,
    EDGE_LANE,
    HIDDEN_GEM_LANE,
    normalize_strategy_lane_config,
)


ALLOWED_MARKET_ROUTE = {
    "allowed": True,
    "group": "weather",
    "family": "daily_temperature",
    "subcategory": "tail_high",
    "handler_id": "weather.daily_temperature.v1",
    "reason_code": "allowed_weather_daily_temperature",
}


class FakeExchange:
    def get_markets(self, limit=100):
        return [
            SimpleNamespace(
                id="KXHIGHNY-26APR29-T80",
                question="Will NYC high temperature be above 80 degrees?",
                yes_price=0.50,
                no_price=0.50,
                closes_at=None,
                metadata={"series_ticker": "KXHIGHNY", "event_ticker": "KXHIGHNY-26APR29"},
            )
        ]


class StrategyLaneTests(unittest.TestCase):
    def test_default_edge_lane_is_metadata_only_and_preserves_approval(self):
        context = self._context(
            market_price=0.40,
            model_probability=0.70,
            edge=0.30,
            confidence=0.90,
        )
        kelly_sizer, risk_policy = self._approving_dependencies(size=10.0)

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.50,
            max_entry_price=0.70,
        )

        self.assertTrue(decision.approved)
        lane = decision.reasoning["strategy_lane"]
        self.assertEqual(lane["lane_id"], EDGE_LANE)
        self.assertEqual(lane["reason_code"], "edge_lane_selected")
        self.assertFalse(lane["behavior_enabled"])
        self.assertFalse(lane["new_behavior_enabled"])
        self.assertEqual(decision.reasoning["thresholds"]["effective_min_edge"], 0.05)
        self.assertEqual(decision.reasoning["thresholds"]["effective_min_confidence"], 0.50)

    def test_default_hidden_gem_lane_preserves_current_hidden_gem_gate(self):
        context = self._context(
            market_price=0.03,
            model_probability=0.12,
            edge=0.09,
            confidence=0.90,
        )
        kelly_sizer, risk_policy = self._approving_dependencies(size=2.0)

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.50,
            max_entry_price=0.70,
        )

        self.assertTrue(decision.approved)
        self.assertEqual(decision.reasoning["strategy_lane"]["lane_id"], HIDDEN_GEM_LANE)
        self.assertEqual(decision.reasoning["strategy_lane"]["reason_code"], "hidden_gem_lane_selected")
        self.assertTrue(decision.reasoning["hidden_gem"]["triggered"])

    def test_lane_sizing_config_records_metadata_without_changing_size(self):
        context = self._context(
            market_price=0.40,
            model_probability=0.70,
            edge=0.30,
            confidence=0.90,
            metadata={
                "strategy_lanes": {
                    "enabled": False,
                    "sizing": {
                        "edge": {
                            "size_multiplier": 0.25,
                            "max_position_usd": 2.0,
                            "max_position_pct": 0.05,
                        },
                    },
                }
            },
        )
        kelly_sizer, risk_policy = self._approving_dependencies(size=10.0)

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.50,
            max_entry_price=0.70,
        )

        self.assertTrue(decision.approved)
        self.assertEqual(decision.requested_position_size, 10.0)
        self.assertEqual(decision.position_size, 10.0)
        risk_policy.check_trade.assert_called_once_with(
            context.source_context,
            10.0,
            available_cash=100.0,
        )
        lane_sizing = decision.reasoning["lane_sizing"]
        self.assertTrue(lane_sizing["configured"])
        self.assertTrue(lane_sizing["metadata_only"])
        self.assertFalse(lane_sizing["applied"])
        self.assertTrue(lane_sizing["would_adjust_size"])
        self.assertEqual(lane_sizing["metadata_adjusted_size"], 2.0)
        self.assertEqual(
            decision.reasoning["strategy_lane"]["evidence"]["lane_sizing"]["max_position_usd"],
            2.0,
        )

    def test_stable_lane_sizing_caps_preserve_requested_and_approved_size(self):
        context = self._context(
            market_price=0.40,
            model_probability=0.70,
            edge=0.30,
            confidence=0.90,
            metadata={
                "strategy_lanes": {
                    "enabled": False,
                    "sizing": {
                        "edge": {
                            "size_multiplier": 0.25,
                            "max_position_usd": 2.0,
                        },
                    },
                },
                "strategy_policy": {
                    "version": "stable",
                    "beta": {"mode": "enforce", "features": {"lane_sizing_caps": True}},
                },
            },
        )
        kelly_sizer, risk_policy = self._approving_dependencies(size=10.0)

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.50,
            max_entry_price=0.70,
        )

        self.assertTrue(decision.approved)
        self.assertEqual(decision.requested_position_size, 10.0)
        self.assertEqual(decision.position_size, 10.0)
        risk_policy.check_trade.assert_called_once_with(
            context.source_context,
            10.0,
            available_cash=100.0,
        )
        lane_sizing = decision.reasoning["lane_sizing"]
        self.assertTrue(lane_sizing["configured"])
        self.assertFalse(lane_sizing["active"])
        self.assertFalse(lane_sizing["enforced"])
        self.assertFalse(lane_sizing["applied"])
        self.assertTrue(lane_sizing["preserved_stable_size"])
        self.assertEqual(lane_sizing["beta_adjusted_size"], 2.0)

    def test_shadow_lane_sizing_caps_report_delta_without_changing_size(self):
        context = self._context(
            market_price=0.40,
            model_probability=0.70,
            edge=0.30,
            confidence=0.90,
            metadata={
                "strategy_lanes": {
                    "enabled": False,
                    "sizing": {
                        "edge": {
                            "size_multiplier": 0.25,
                            "max_position_usd": 2.0,
                        },
                    },
                },
                "strategy_policy": self._beta_policy("shadow", lane_sizing_caps=True),
            },
        )
        kelly_sizer, risk_policy = self._approving_dependencies(size=10.0)

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.50,
            max_entry_price=0.70,
        )

        self.assertTrue(decision.approved)
        self.assertEqual(decision.requested_position_size, 10.0)
        self.assertEqual(decision.position_size, 10.0)
        risk_policy.check_trade.assert_called_once_with(
            context.source_context,
            10.0,
            available_cash=100.0,
        )
        lane_sizing = decision.reasoning["lane_sizing"]
        self.assertTrue(lane_sizing["active"])
        self.assertTrue(lane_sizing["shadow"])
        self.assertFalse(lane_sizing["enforced"])
        self.assertFalse(lane_sizing["applied"])
        self.assertTrue(lane_sizing["differs_from_final"])
        self.assertEqual(lane_sizing["beta_adjusted_size"], 2.0)

    def test_enforce_lane_sizing_caps_apply_before_risk_policy(self):
        context = self._context(
            market_price=0.40,
            model_probability=0.70,
            edge=0.30,
            confidence=0.90,
            metadata={
                "strategy_lanes": {
                    "enabled": False,
                    "sizing": {
                        "edge": {
                            "size_multiplier": 0.25,
                            "max_position_usd": 2.0,
                        },
                    },
                },
                "strategy_policy": self._beta_policy("enforce", lane_sizing_caps=True),
            },
        )
        kelly_sizer = Mock()
        kelly_sizer.calculate.return_value = 10.0
        risk_policy = Mock()
        risk_policy.check_trade.return_value = SimpleNamespace(
            approved=True,
            reason="Approved",
            adjusted_size=2.0,
            risk_score=0.0,
            warnings=[],
        )

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.50,
            max_entry_price=0.70,
        )

        self.assertTrue(decision.approved)
        self.assertEqual(decision.requested_position_size, 2.0)
        self.assertEqual(decision.position_size, 2.0)
        risk_policy.check_trade.assert_called_once_with(
            context.source_context,
            2.0,
            available_cash=100.0,
        )
        lane_sizing = decision.reasoning["lane_sizing"]
        self.assertTrue(lane_sizing["active"])
        self.assertFalse(lane_sizing["shadow"])
        self.assertTrue(lane_sizing["enforced"])
        self.assertTrue(lane_sizing["applied"])
        self.assertFalse(lane_sizing["metadata_only"])
        self.assertEqual(lane_sizing["applied_size"], 2.0)

    def test_lane_sizing_caps_feature_off_preserves_size_even_in_enforce(self):
        context = self._context(
            market_price=0.40,
            model_probability=0.70,
            edge=0.30,
            confidence=0.90,
            metadata={
                "strategy_lanes": {
                    "enabled": False,
                    "sizing": {
                        "edge": {
                            "size_multiplier": 0.25,
                            "max_position_usd": 2.0,
                        },
                    },
                },
                "strategy_policy": self._beta_policy("enforce", lane_sizing_caps=False),
            },
        )
        kelly_sizer, risk_policy = self._approving_dependencies(size=10.0)

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.50,
            max_entry_price=0.70,
        )

        self.assertTrue(decision.approved)
        self.assertEqual(decision.requested_position_size, 10.0)
        self.assertEqual(decision.position_size, 10.0)
        risk_policy.check_trade.assert_called_once_with(
            context.source_context,
            10.0,
            available_cash=100.0,
        )
        lane_sizing = decision.reasoning["lane_sizing"]
        self.assertFalse(lane_sizing["active"])
        self.assertFalse(lane_sizing["enforced"])
        self.assertFalse(lane_sizing["applied"])
        self.assertEqual(lane_sizing["beta_adjusted_size"], 2.0)

    def test_slow_profit_config_does_not_change_defaults_until_enabled(self):
        context = self._context(
            market_price=0.50,
            model_probability=0.53,
            edge=0.03,
            confidence=0.95,
            metadata={
                "strategy_lanes": {
                    "enabled": False,
                    "enabled_lanes": ["edge", "hidden_gem", "confidence_slow_profit"],
                    "confidence_slow_profit": {
                        "enabled": True,
                        "min_edge": 0.02,
                        "min_confidence": 0.90,
                    },
                }
            },
        )
        kelly_sizer = Mock()
        risk_policy = Mock()

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.50,
            max_entry_price=0.70,
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason_code, "edge_below_threshold")
        self.assertEqual(decision.reasoning["strategy_lane"]["lane_id"], EDGE_LANE)
        self.assertFalse(decision.reasoning["strategy_lane"]["new_behavior_enabled"])
        kelly_sizer.calculate.assert_not_called()
        risk_policy.check_trade.assert_not_called()

    def test_explicit_slow_profit_lane_can_use_configured_thresholds(self):
        context = self._context(
            market_price=0.50,
            model_probability=0.53,
            edge=0.03,
            confidence=0.95,
            metadata={
                "strategy_lanes": {
                    "enabled": True,
                    "enabled_lanes": ["edge", "hidden_gem", "confidence_slow_profit"],
                    "confidence_slow_profit": {
                        "enabled": True,
                        "min_edge": 0.02,
                        "min_confidence": 0.90,
                    },
                },
                "strategy_policy": self._beta_policy("enforce", hidden_gem_lane_gates=True),
            },
        )
        kelly_sizer, risk_policy = self._approving_dependencies(size=4.0)

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.50,
            max_entry_price=0.70,
        )

        self.assertTrue(decision.approved)
        lane = decision.reasoning["strategy_lane"]
        self.assertEqual(lane["lane_id"], CONFIDENCE_SLOW_PROFIT_LANE)
        self.assertEqual(lane["reason_code"], "confidence_slow_profit_lane_selected")
        self.assertTrue(lane["new_behavior_enabled"])
        self.assertEqual(decision.reasoning["thresholds"]["effective_min_edge"], 0.02)
        self.assertEqual(decision.reasoning["thresholds"]["effective_min_confidence"], 0.90)

    def test_shadow_slow_profit_lane_records_beta_delta_without_admission(self):
        context = self._context(
            market_price=0.50,
            model_probability=0.53,
            edge=0.03,
            confidence=0.95,
            metadata={
                "strategy_lanes": {
                    "enabled": True,
                    "enabled_lanes": ["edge", "hidden_gem", "confidence_slow_profit"],
                    "confidence_slow_profit": {
                        "enabled": True,
                        "min_edge": 0.02,
                        "min_confidence": 0.90,
                    },
                },
                "strategy_policy": self._beta_policy("shadow", hidden_gem_lane_gates=True),
            },
        )
        kelly_sizer = Mock()
        risk_policy = Mock()

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.50,
            max_entry_price=0.70,
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason_code, "edge_below_threshold")
        lane = decision.reasoning["strategy_lane"]
        self.assertEqual(lane["lane_id"], EDGE_LANE)
        beta_gate = lane["evidence"]["beta_lane_gate"]
        self.assertTrue(beta_gate["beta_behavior_enabled"])
        self.assertFalse(beta_gate["beta_behavior_enforced"])
        self.assertEqual(beta_gate["lane_id"], CONFIDENCE_SLOW_PROFIT_LANE)
        self.assertTrue(beta_gate["differs_from_final"])
        kelly_sizer.calculate.assert_not_called()
        risk_policy.check_trade.assert_not_called()

    def test_enabled_lane_allowlist_can_fail_before_kelly_and_risk(self):
        context = self._context(
            market_price=0.03,
            model_probability=0.12,
            edge=0.09,
            confidence=0.90,
            metadata={
                "strategy_lanes": {
                    "enabled": True,
                    "enabled_lanes": ["edge"],
                },
                "strategy_policy": self._beta_policy("enforce", hidden_gem_lane_gates=True),
            },
        )
        kelly_sizer = Mock()
        risk_policy = Mock()

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.50,
            max_entry_price=0.70,
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason_code, "strategy_lane_disabled")
        self.assertEqual(decision.reasoning["strategy_lane"]["lane_id"], HIDDEN_GEM_LANE)
        kelly_sizer.calculate.assert_not_called()
        risk_policy.check_trade.assert_not_called()

    def test_config_normalization_keeps_slow_profit_off_by_default(self):
        config = normalize_strategy_lane_config(
            {
                "enabled": True,
                "enabled_lanes": "edge, hidden-gem, confidence/slow-profit",
                "confidence_slow_profit": {"min_edge": "0.02", "min_confidence": "0.9"},
            }
        )

        self.assertTrue(config["enabled"])
        self.assertIn(HIDDEN_GEM_LANE, config["enabled_lanes"])
        self.assertIn(CONFIDENCE_SLOW_PROFIT_LANE, config["enabled_lanes"])
        self.assertFalse(config["confidence_slow_profit"]["enabled"])
        self.assertEqual(config["confidence_slow_profit"]["min_edge"], 0.02)
        self.assertEqual(config["confidence_slow_profit"]["min_confidence"], 0.9)

    def test_config_normalization_records_known_lane_sizing_metadata(self):
        config = normalize_strategy_lane_config(
            {
                "sizing": {
                    "hidden-gem": {
                        "size_multiplier": "0.2",
                        "max_position_usd": "3",
                        "max_position_pct": "0.04",
                    },
                    "unsupported": {"max_position_usd": 100},
                },
            }
        )

        self.assertEqual(
            config["sizing"][HIDDEN_GEM_LANE],
            {"size_multiplier": 0.2, "max_position_usd": 3.0, "max_position_pct": 0.04},
        )
        self.assertNotIn("unsupported", config["sizing"])

    def test_explicit_slow_profit_enabled_adds_lane_to_allowlist(self):
        config = normalize_strategy_lane_config(
            {
                "enabled": True,
                "confidence_slow_profit": {
                    "enabled": True,
                    "min_edge": "0.02",
                    "min_confidence": "0.9",
                },
            }
        )

        self.assertIn(CONFIDENCE_SLOW_PROFIT_LANE, config["enabled_lanes"])

    def test_parent_disabled_slow_profit_keeps_default_allowlist(self):
        config = normalize_strategy_lane_config(
            {
                "enabled": False,
                "confidence_slow_profit": {
                    "enabled": True,
                    "min_edge": 0.02,
                    "min_confidence": 0.9,
                },
            }
        )

        self.assertEqual(config["enabled_lanes"], [EDGE_LANE, HIDDEN_GEM_LANE])

    def test_paper_pre_gate_allows_explicit_slow_profit_to_reach_shared_core(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sim = Simulator(
                {
                    "data_dir": tmpdir,
                    "enable_social": False,
                    "enable_time_decay_ranking": False,
                    "strategy": {
                        "min_edge": 0.05,
                        "min_confidence": 0.50,
                        "enable_news": False,
                        "enable_social": False,
                        "enable_ai": False,
                    },
                    "strategy_lanes": {
                        "enabled": True,
                        "confidence_slow_profit": {
                            "enabled": True,
                            "min_edge": 0.02,
                            "min_confidence": 0.90,
                        },
                    },
                    "strategy_policy": self._beta_policy("enforce", hidden_gem_lane_gates=True),
                }
            )
            signal = self._slow_profit_signal()

            self.assertIsNone(sim._trade_gate_reason(dict(signal)))
            with patch.object(sim.strategy, "analyze_market", return_value=signal), patch.object(
                sim.kelly,
                "calculate",
                return_value=4.0,
            ):
                result = sim.scan(FakeExchange())

            self.assertEqual(result["trades"], 1)
            self.assertNotIn("edge_below_threshold", result["blocked_reasons"])
            self.assertEqual(
                sim.trades[0].decision_trace["strategy_lane"]["lane_id"],
                CONFIDENCE_SLOW_PROFIT_LANE,
            )

    def test_paper_pre_gate_defaults_still_block_lower_edge_signal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sim = Simulator(
                {
                    "data_dir": tmpdir,
                    "enable_social": False,
                    "strategy": {
                        "min_edge": 0.05,
                        "min_confidence": 0.50,
                        "enable_news": False,
                        "enable_social": False,
                        "enable_ai": False,
                    },
                }
            )

            self.assertEqual(sim._trade_gate_reason(self._slow_profit_signal()), "edge_below_threshold")

    def _context(
        self,
        *,
        market_price: float,
        model_probability: float,
        edge: float,
        confidence: float,
        metadata: dict | None = None,
    ) -> TradeContext:
        merged_metadata = {"market_route": ALLOWED_MARKET_ROUTE}
        merged_metadata.update(metadata or {})
        return TradeContext(
            exchange="kalshi",
            market_id="KXHIGHNY-260506-T71",
            question="Will the high temperature in New York exceed 71 degrees?",
            direction="BUY_YES",
            market_price=market_price,
            yes_price=market_price,
            no_price=round(1 - market_price, 4),
            model_probability=model_probability,
            edge=edge,
            confidence=confidence,
            account_state=AccountState(
                starting_balance=100.0,
                current_balance=100.0,
                available_cash=100.0,
                reserved_capital=0.0,
                total_exposure=0.0,
                open_positions=0,
            ),
            source_context={
                "market_id": "KXHIGHNY-260506-T71",
                "question": "Will the high temperature in New York exceed 71 degrees?",
                "confidence": 0.90,
                "signals": {"live": model_probability, "price": model_probability},
            },
            metadata=merged_metadata,
        )

    def _approving_dependencies(self, *, size: float):
        kelly_sizer = Mock()
        kelly_sizer.calculate.return_value = size
        risk_policy = Mock()
        risk_policy.check_trade.return_value = SimpleNamespace(
            approved=True,
            reason="Approved",
            adjusted_size=size,
            risk_score=0.0,
            warnings=[],
        )
        return kelly_sizer, risk_policy

    def _slow_profit_signal(self) -> dict:
        return {
            "market_id": "KXHIGHNY-26APR29-T80",
            "question": "Will NYC high temperature be above 80 degrees?",
            "series_ticker": "KXHIGHNY",
            "event_ticker": "KXHIGHNY-26APR29",
            "exchange": "kalshi",
            "direction": "BUY_YES",
            "market_price": 0.50,
            "yes_price": 0.50,
            "no_price": 0.50,
            "model_probability": 0.53,
            "edge": 0.03,
            "confidence": 0.95,
            "signals": {},
        }

    def _beta_policy(
        self,
        mode: str,
        *,
        weather_hidden_gem_evidence_card: bool = False,
        bucket_distribution_scoring: bool = False,
        hidden_gem_lane_gates: bool = False,
        lane_sizing_caps: bool = False,
    ) -> dict:
        return {
            "version": "beta",
            "beta": {
                "mode": mode,
                "features": {
                    "weather_hidden_gem_evidence_card": weather_hidden_gem_evidence_card,
                    "bucket_distribution_scoring": bucket_distribution_scoring,
                    "hidden_gem_lane_gates": hidden_gem_lane_gates,
                    "lane_sizing_caps": lane_sizing_caps,
                },
            },
        }


if __name__ == "__main__":
    unittest.main()
