import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from bot.shared_core import AccountState, TradeContext, build_trade_decision


ALLOWED_MARKET_ROUTE = {
    "allowed": True,
    "group": "weather",
    "family": "daily_temperature",
    "subcategory": "tail_high",
    "handler_id": "weather.daily_temperature.v1",
    "reason_code": "allowed_weather_daily_temperature",
}


class SharedCoreDecisionTests(unittest.TestCase):
    def test_build_trade_decision_uses_buy_no_price_and_available_cash_snapshot(self):
        account_state = AccountState(
            starting_balance=100.0,
            current_balance=100.0,
            available_cash=25.0,
            reserved_capital=75.0,
            total_exposure=75.0,
            open_positions=3,
        )
        context = TradeContext(
            exchange="kalshi",
            market_id="market-1",
            question="Will test settle NO?",
            direction="BUY_NO",
            market_price=0.41,
            yes_price=0.41,
            no_price=0.63,
            model_probability=0.28,
            edge=0.12,
            confidence=0.87,
            account_state=account_state,
            source_context={"question": "Will test settle NO?"},
            metadata={"market_route": ALLOWED_MARKET_ROUTE},
        )
        kelly_sizer = Mock()
        kelly_sizer.calculate.return_value = 12.0
        risk_policy = Mock()
        risk_policy.check_trade.return_value = SimpleNamespace(
            approved=True,
            reason="Approved (with 1 warnings)",
            adjusted_size=9.5,
            risk_score=0.2,
            warnings=["Available cash capped size to $9.50 ($25.00 spendable)"],
        )

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertTrue(decision.approved)
        self.assertEqual(decision.action, "BUY_NO")
        self.assertAlmostEqual(decision.entry_price, 0.63)
        self.assertAlmostEqual(decision.win_probability, 0.72)
        self.assertAlmostEqual(decision.position_size, 9.5)
        self.assertEqual(decision.reason_code, "approved")
        self.assertEqual(decision.reasoning["account_state"]["available_cash"], 25.0)
        kelly_sizer.calculate.assert_called_once_with(0.72, 0.63, 25.0)
        risk_policy.check_trade.assert_called_once_with(
            {"question": "Will test settle NO?"},
            12.0,
            available_cash=25.0,
        )

    def test_build_trade_decision_allows_current_hidden_gem_below_fifty_percent(self):
        account_state = AccountState(
            starting_balance=100.0,
            current_balance=100.0,
            available_cash=25.0,
            reserved_capital=0.0,
            total_exposure=0.0,
            open_positions=0,
        )
        context = TradeContext(
            exchange="kalshi",
            market_id="hidden-gem-1",
            question="Will cheap market settle YES?",
            direction="BUY_YES",
            market_price=0.03,
            yes_price=0.03,
            no_price=0.97,
            model_probability=0.12,
            edge=0.09,
            confidence=0.9,
            account_state=account_state,
            source_context={"question": "Will cheap market settle YES?"},
            metadata={"market_route": ALLOWED_MARKET_ROUTE},
        )
        kelly_sizer = Mock()
        kelly_sizer.calculate.return_value = 2.0
        risk_policy = Mock()
        risk_policy.check_trade.return_value = SimpleNamespace(
            approved=True,
            reason="Approved",
            adjusted_size=2.0,
            risk_score=0.1,
            warnings=[],
        )

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertTrue(decision.approved)
        self.assertEqual(decision.reason_code, "approved")
        self.assertTrue(decision.reasoning["hidden_gem"]["triggered"])

    def test_build_trade_decision_does_not_apply_weather_exceptional_gate_to_non_weather_market(self):
        account_state = AccountState(
            starting_balance=100.0,
            current_balance=100.0,
            available_cash=100.0,
            reserved_capital=0.0,
            total_exposure=0.0,
            open_positions=0,
        )
        context = TradeContext(
            exchange="kalshi",
            market_id="CHEAP-NONWEATHER-1",
            question="Will a non-weather event happen?",
            direction="BUY_YES",
            market_price=0.03,
            yes_price=0.03,
            no_price=0.97,
            model_probability=0.48,
            edge=0.45,
            confidence=0.9,
            account_state=account_state,
            source_context={"market_id": "CHEAP-NONWEATHER-1", "question": "Will a non-weather event happen?"},
            metadata={"market_route": ALLOWED_MARKET_ROUTE},
        )
        kelly_sizer = Mock()
        kelly_sizer.calculate.return_value = 2.0
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
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertTrue(decision.approved)
        self.assertNotIn("weather_risk", decision.reasoning)
        self.assertEqual(decision.reason_code, "approved")

    def test_build_trade_decision_applies_weather_bucket_size_cap(self):
        account_state = AccountState(
            starting_balance=100.0,
            current_balance=100.0,
            available_cash=100.0,
            reserved_capital=0.0,
            total_exposure=0.0,
            open_positions=0,
        )
        context = TradeContext(
            exchange="kalshi",
            market_id="KXHIGHMIA-26APR26-B82.5",
            question="Will the high temp in Miami be 82-83° on Apr 26?",
            direction="BUY_YES",
            market_price=0.22,
            yes_price=0.22,
            no_price=0.78,
            model_probability=0.70,
            edge=0.48,
            confidence=0.9,
            account_state=account_state,
            source_context={
                "market_id": "KXHIGHMIA-26APR26-B82.5",
                "question": "Will the high temp in Miami be 82-83° on Apr 26?",
                "market_volume": 1000,
            },
            metadata={
                "market_route": ALLOWED_MARKET_ROUTE,
                "strategy_policy": self._beta_policy("enforce", bucket_distribution_scoring=True),
            },
        )
        kelly_sizer = Mock()
        kelly_sizer.calculate.return_value = 100.0
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
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertTrue(decision.approved)
        self.assertEqual(decision.reasoning["weather_risk"]["shape"], "bucket")
        self.assertEqual(decision.reasoning["weather_risk"]["requested_size_after_weather_limits"], 2.0)
        risk_policy.check_trade.assert_called_once_with(
            context.source_context,
            2.0,
            available_cash=100.0,
        )

    def test_build_trade_decision_rejects_exceptional_hidden_gem_without_weather_evidence(self):
        account_state = AccountState(
            starting_balance=100.0,
            current_balance=100.0,
            available_cash=100.0,
            reserved_capital=0.0,
            total_exposure=0.0,
            open_positions=0,
        )
        context = TradeContext(
            exchange="kalshi",
            market_id="KXHIGHTSEA-26APR26-T64",
            question="Will the maximum temperature be <64° on Apr 26?",
            direction="BUY_YES",
            market_price=0.03,
            yes_price=0.03,
            no_price=0.97,
            model_probability=0.48,
            edge=0.45,
            confidence=0.9,
            account_state=account_state,
            source_context={
                "market_id": "KXHIGHTSEA-26APR26-T64",
                "question": "Will the maximum temperature be <64° on Apr 26?",
                "market_volume": 700,
                "weather_station_mapping": "inferred",
            },
            metadata={
                "market_route": ALLOWED_MARKET_ROUTE,
                "strategy_policy": self._beta_policy("enforce", weather_hidden_gem_evidence_card=True),
            },
        )
        kelly_sizer = Mock()
        risk_policy = Mock()

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason_code, "weather_extreme_disagreement_without_perfect_evidence")
        self.assertEqual(decision.reasoning["weather_risk"]["hidden_gem_tier"], "exceptional")
        kelly_sizer.calculate.assert_not_called()
        risk_policy.check_trade.assert_not_called()

    def test_build_trade_decision_rejects_bucket_hidden_gem_without_distribution_support(self):
        account_state = AccountState(
            starting_balance=100.0,
            current_balance=100.0,
            available_cash=100.0,
            reserved_capital=0.0,
            total_exposure=0.0,
            open_positions=0,
        )
        context = TradeContext(
            exchange="kalshi",
            market_id="KXHIGHMIA-26APR26-B82.5",
            question="Will the high temp in Miami be 82-83° on Apr 26?",
            direction="BUY_YES",
            market_price=0.03,
            yes_price=0.03,
            no_price=0.97,
            model_probability=0.15,
            edge=0.12,
            confidence=0.9,
            account_state=account_state,
            source_context={
                "market_id": "KXHIGHMIA-26APR26-B82.5",
                "question": "Will the high temp in Miami be 82-83° on Apr 26?",
                "market_volume": 900,
                "station_id": "KMIA",
                "signals": {"live": 0.15, "price": 0.14},
            },
            metadata={
                "market_route": ALLOWED_MARKET_ROUTE,
                "strategy_policy": self._beta_policy("enforce", bucket_distribution_scoring=True),
            },
        )
        kelly_sizer = Mock()
        risk_policy = Mock()

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason_code, "weather_bucket_hidden_gem_missing_distribution_probability")
        self.assertEqual(decision.reasoning["weather_risk"]["shape"], "bucket")
        kelly_sizer.calculate.assert_not_called()
        risk_policy.check_trade.assert_not_called()

    def test_enforce_policy_rejects_bucket_hidden_gem_below_distribution_thresholds(self):
        context = self._bucket_missing_distribution_context(
            self._beta_policy("enforce", bucket_distribution_scoring=True)
        )
        context.source_context.update(
            {
                "distribution_probability": 0.07,
                "weather_station_mapping": "exact",
                "weather_confidence_score": 0.94,
                "source_agreement_score": 0.9,
            }
        )
        kelly_sizer, risk_policy = self._approving_dependencies(size=2.0)

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertFalse(decision.approved)
        self.assertEqual(
            decision.reason_code,
            "weather_bucket_hidden_gem_distribution_probability_below_entry_plus_buffer",
        )
        self.assertEqual(decision.reasoning["weather_risk"]["beta_gate"]["feature"], "bucket_distribution_scoring")
        kelly_sizer.calculate.assert_not_called()
        risk_policy.check_trade.assert_not_called()

    def test_shadow_policy_records_bucket_distribution_threshold_delta_but_preserves_final_action(self):
        context = self._bucket_missing_distribution_context(
            self._beta_policy("shadow", bucket_distribution_scoring=True)
        )
        context.source_context.update(
            {
                "distribution_probability": 0.07,
                "weather_station_mapping": "exact",
                "weather_confidence_score": 0.94,
                "source_agreement_score": 0.9,
            }
        )
        kelly_sizer, risk_policy = self._approving_dependencies(size=2.0)

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertTrue(decision.approved)
        gate = decision.reasoning["weather_risk"]["beta_gate"]
        self.assertTrue(gate["active"])
        self.assertTrue(gate["shadow"])
        self.assertFalse(gate["enforced"])
        self.assertEqual(
            gate["reason_code"],
            "weather_bucket_hidden_gem_distribution_probability_below_entry_plus_buffer",
        )
        self.assertEqual(
            decision.reasoning["hidden_gem_evidence_card"]["reason_codes"]["beta_reject"],
            "weather_bucket_hidden_gem_distribution_probability_below_entry_plus_buffer",
        )
        risk_policy.check_trade.assert_called_once_with(context.source_context, 2.0, available_cash=100.0)

    def test_enforce_policy_rejects_bucket_hidden_gem_with_weak_source_station_quality(self):
        context = self._bucket_missing_distribution_context(
            self._beta_policy("enforce", bucket_distribution_scoring=True)
        )
        context.source_context.update(
            {
                "distribution_probability": 0.24,
                "weather_station_mapping": "inferred",
                "weather_confidence_score": 0.94,
                "source_agreement_score": 0.9,
            }
        )
        kelly_sizer, risk_policy = self._approving_dependencies(size=2.0)

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertFalse(decision.approved)
        self.assertEqual(
            decision.reason_code,
            "weather_bucket_hidden_gem_source_station_quality_below_minimum",
        )
        gate = decision.reasoning["weather_risk"]["beta_gate"]
        self.assertEqual(gate["feature"], "bucket_distribution_scoring")
        self.assertTrue(gate["enforced"])
        kelly_sizer.calculate.assert_not_called()
        risk_policy.check_trade.assert_not_called()

    def test_shadow_policy_records_bucket_source_station_delta_but_preserves_final_action(self):
        context = self._bucket_missing_distribution_context(
            self._beta_policy("shadow", bucket_distribution_scoring=True)
        )
        context.source_context.update(
            {
                "distribution_probability": 0.24,
                "weather_station_mapping": "exact",
                "weather_confidence_score": 0.94,
                "source_agreement_score": 0.6,
            }
        )
        kelly_sizer, risk_policy = self._approving_dependencies(size=2.0)

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertTrue(decision.approved)
        gate = decision.reasoning["weather_risk"]["beta_gate"]
        self.assertTrue(gate["active"])
        self.assertTrue(gate["shadow"])
        self.assertFalse(gate["enforced"])
        self.assertEqual(
            gate["reason_code"],
            "weather_bucket_hidden_gem_source_station_quality_below_minimum",
        )
        card = decision.reasoning["hidden_gem_evidence_card"]
        self.assertTrue(card["beta_deltas"]["rejection_differs_from_final"])
        self.assertEqual(
            card["reason_codes"]["beta_reject"],
            "weather_bucket_hidden_gem_source_station_quality_below_minimum",
        )
        risk_policy.check_trade.assert_called_once_with(context.source_context, 2.0, available_cash=100.0)

    def test_stable_policy_preserves_bucket_source_station_quality_rejection(self):
        context = self._bucket_missing_distribution_context()
        context.source_context.update(
            {
                "distribution_probability": 0.24,
                "weather_station_mapping": "inferred",
                "weather_confidence_score": 0.94,
                "source_agreement_score": 0.9,
            }
        )
        kelly_sizer, risk_policy = self._approving_dependencies(size=2.0)

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertTrue(decision.approved)
        gate = decision.reasoning["weather_risk"]["beta_gate"]
        self.assertEqual(
            gate["reason_code"],
            "weather_bucket_hidden_gem_source_station_quality_below_minimum",
        )
        self.assertFalse(gate["active"])
        self.assertFalse(gate["enforced"])
        self.assertTrue(gate["preserved_stable_action"])
        risk_policy.check_trade.assert_called_once_with(context.source_context, 2.0, available_cash=100.0)

    def test_stable_policy_records_weather_beta_rejection_but_preserves_final_action(self):
        context = self._bucket_missing_distribution_context()
        kelly_sizer, risk_policy = self._approving_dependencies(size=2.0)

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertTrue(decision.approved)
        gate = decision.reasoning["weather_risk"]["beta_gate"]
        self.assertTrue(gate["would_reject"])
        self.assertFalse(gate["active"])
        self.assertFalse(gate["enforced"])
        self.assertTrue(gate["preserved_stable_action"])
        risk_policy.check_trade.assert_called_once_with(context.source_context, 2.0, available_cash=100.0)

    def test_stable_policy_preserves_bucket_distribution_threshold_rejection(self):
        context = self._bucket_missing_distribution_context()
        context.source_context.update(
            {
                "distribution_probability": 0.07,
                "weather_station_mapping": "exact",
                "weather_confidence_score": 0.94,
                "source_agreement_score": 0.9,
            }
        )
        kelly_sizer, risk_policy = self._approving_dependencies(size=2.0)

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertTrue(decision.approved)
        gate = decision.reasoning["weather_risk"]["beta_gate"]
        self.assertEqual(
            gate["reason_code"],
            "weather_bucket_hidden_gem_distribution_probability_below_entry_plus_buffer",
        )
        self.assertFalse(gate["active"])
        self.assertFalse(gate["enforced"])
        risk_policy.check_trade.assert_called_once_with(context.source_context, 2.0, available_cash=100.0)

    def test_enforce_policy_without_bucket_feature_preserves_bucket_distribution_threshold_rejection(self):
        context = self._bucket_missing_distribution_context(
            self._beta_policy("enforce", weather_hidden_gem_evidence_card=True)
        )
        context.source_context.update(
            {
                "distribution_probability": 0.07,
                "weather_station_mapping": "exact",
                "weather_confidence_score": 0.94,
                "source_agreement_score": 0.9,
            }
        )
        kelly_sizer, risk_policy = self._approving_dependencies(size=2.0)

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertTrue(decision.approved)
        gate = decision.reasoning["weather_risk"]["beta_gate"]
        self.assertEqual(gate["feature"], "bucket_distribution_scoring")
        self.assertFalse(gate["active"])
        self.assertFalse(gate["enforced"])
        risk_policy.check_trade.assert_called_once_with(context.source_context, 2.0, available_cash=100.0)

    def test_enforce_policy_without_bucket_feature_preserves_bucket_source_station_quality_rejection(self):
        context = self._bucket_missing_distribution_context(
            self._beta_policy("enforce", weather_hidden_gem_evidence_card=True)
        )
        context.source_context.update(
            {
                "distribution_probability": 0.24,
                "weather_station_mapping": "exact",
                "weather_confidence_score": 0.94,
                "source_agreement_score": 0.6,
            }
        )
        kelly_sizer, risk_policy = self._approving_dependencies(size=2.0)

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertTrue(decision.approved)
        gate = decision.reasoning["weather_risk"]["beta_gate"]
        self.assertEqual(gate["feature"], "bucket_distribution_scoring")
        self.assertEqual(
            gate["reason_code"],
            "weather_bucket_hidden_gem_source_station_quality_below_minimum",
        )
        self.assertFalse(gate["active"])
        self.assertFalse(gate["enforced"])
        risk_policy.check_trade.assert_called_once_with(context.source_context, 2.0, available_cash=100.0)

    def test_hidden_gem_weather_decision_includes_evidence_card(self):
        route = {**ALLOWED_MARKET_ROUTE, "subcategory": "bucket"}
        context = TradeContext(
            exchange="kalshi",
            market_id="KXHIGHMIA-26APR26-B82.5",
            question="Will the high temp in Miami be 82-83° on Apr 26?",
            direction="BUY_YES",
            market_price=0.03,
            yes_price=0.03,
            no_price=0.97,
            model_probability=0.24,
            edge=0.21,
            confidence=0.9,
            account_state=AccountState(
                starting_balance=100.0,
                current_balance=100.0,
                available_cash=100.0,
                reserved_capital=0.0,
                total_exposure=0.0,
                open_positions=0,
            ),
            source_context={
                "market_id": "KXHIGHMIA-26APR26-B82.5",
                "question": "Will the high temp in Miami be 82-83° on Apr 26?",
                "source_mode": "recorded_as_of",
                "market_volume": 900,
                "liquidity": 1200,
                "station_id": "KMIA",
                "source_agreement_score": 0.91,
                "weather_confidence_score": 0.88,
                "source_timestamp": "2026-05-06T12:00:00+00:00",
                "distribution_probability": 0.24,
                "forecast_mean": 82.8,
                "forecast_spread": 1.6,
                "bucket_center": 82.5,
                "bucket_width": 1.0,
                "distance_to_center": 0.3,
            },
            metadata={"market_route": route},
        )
        kelly_sizer, risk_policy = self._approving_dependencies(size=2.0)

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertTrue(decision.approved)
        card = decision.reasoning["hidden_gem_evidence_card"]
        self.assertEqual(card["lane"], "hidden_gem")
        self.assertEqual(card["market_route"]["family"], "daily_temperature")
        self.assertEqual(card["weather_shape"], "bucket")
        self.assertEqual(card["entry_price"], 0.03)
        self.assertEqual(card["source_mode"], "recorded")
        self.assertEqual(card["station_mapping_quality"], "exact")
        self.assertEqual(card["source_agreement_score"], 0.91)
        self.assertEqual(card["market_volume"], 900.0)
        self.assertEqual(card["liquidity"], 1200.0)
        self.assertEqual(card["bucket"]["distribution_probability"], 0.24)
        self.assertEqual(card["bucket"]["forecast_mean"], 82.8)
        self.assertEqual(card["bucket"]["bucket_center"], 82.5)
        self.assertFalse(card["beta_gate"]["active"])

    def test_hidden_gem_evidence_card_represents_missing_fields_safely(self):
        route = {**ALLOWED_MARKET_ROUTE, "subcategory": "bucket"}
        context = self._bucket_missing_distribution_context()
        context.metadata["market_route"] = route
        context.source_context.pop("station_id", None)
        context.source_context.pop("market_volume", None)
        kelly_sizer, risk_policy = self._approving_dependencies(size=2.0)

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertTrue(decision.approved)
        card = decision.reasoning["hidden_gem_evidence_card"]
        self.assertEqual(card["source_mode"], "missing")
        self.assertFalse(card["volume_known"])
        self.assertIsNone(card["market_volume"])
        self.assertIsNone(card["liquidity"])
        self.assertIsNone(card["bucket"]["distribution_probability"])
        self.assertIsNone(card["bucket"]["forecast"])
        self.assertEqual(card["reason_codes"]["weather_reject"], "weather_bucket_hidden_gem_missing_distribution_probability")
        self.assertFalse(card["beta_gate"]["active"])

    def test_shadow_policy_records_weather_beta_delta_but_preserves_final_action(self):
        context = self._bucket_missing_distribution_context(
            self._beta_policy("shadow", bucket_distribution_scoring=True)
        )
        kelly_sizer, risk_policy = self._approving_dependencies(size=2.0)

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertTrue(decision.approved)
        gate = decision.reasoning["weather_risk"]["beta_gate"]
        self.assertTrue(gate["active"])
        self.assertTrue(gate["shadow"])
        self.assertFalse(gate["enforced"])
        self.assertTrue(gate["differs_from_final"])
        self.assertEqual(gate["reason_code"], "weather_bucket_hidden_gem_missing_distribution_probability")
        card = decision.reasoning["hidden_gem_evidence_card"]
        self.assertTrue(card["beta_gate"]["shadow"])
        self.assertTrue(card["beta_deltas"]["rejection_differs_from_final"])
        self.assertEqual(card["reason_codes"]["beta_reject"], "weather_bucket_hidden_gem_missing_distribution_probability")

    def test_enforce_policy_without_relevant_feature_preserves_final_action(self):
        context = self._bucket_missing_distribution_context(
            self._beta_policy("enforce", weather_hidden_gem_evidence_card=True)
        )
        kelly_sizer, risk_policy = self._approving_dependencies(size=2.0)

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertTrue(decision.approved)
        gate = decision.reasoning["weather_risk"]["beta_gate"]
        self.assertEqual(gate["feature"], "bucket_distribution_scoring")
        self.assertFalse(gate["active"])
        self.assertFalse(gate["enforced"])

    def test_build_trade_decision_rejects_tail_hidden_gem_when_live_probability_disagrees(self):
        account_state = AccountState(
            starting_balance=100.0,
            current_balance=100.0,
            available_cash=100.0,
            reserved_capital=0.0,
            total_exposure=0.0,
            open_positions=0,
        )
        context = TradeContext(
            exchange="kalshi",
            market_id="KXHIGHTSEA-26APR26-T64",
            question="Will the maximum temperature be <64° on Apr 26?",
            direction="BUY_YES",
            market_price=0.04,
            yes_price=0.04,
            no_price=0.96,
            model_probability=0.16,
            edge=0.12,
            confidence=0.9,
            account_state=account_state,
            source_context={
                "market_id": "KXHIGHTSEA-26APR26-T64",
                "question": "Will the maximum temperature be <64° on Apr 26?",
                "market_volume": 900,
                "source_agreement_score": 0.88,
                "signal_details": {
                    "live": {
                        "signal_type": "weather",
                        "predicted_prob": 0.11,
                        "confidence": 0.62,
                    }
                },
            },
            metadata={
                "market_route": ALLOWED_MARKET_ROUTE,
                "strategy_policy": self._beta_policy("enforce", weather_hidden_gem_evidence_card=True),
            },
        )
        kelly_sizer = Mock()
        risk_policy = Mock()

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason_code, "weather_tail_hidden_gem_live_probability_mismatch")
        self.assertIn("tail_directional_mismatch", decision.reasoning["weather_risk"]["flags"])
        kelly_sizer.calculate.assert_not_called()
        risk_policy.check_trade.assert_not_called()

    def test_build_trade_decision_allows_exceptional_hidden_gem_with_helper_derived_weather_evidence(self):
        account_state = AccountState(
            starting_balance=100.0,
            current_balance=100.0,
            available_cash=100.0,
            reserved_capital=0.0,
            total_exposure=0.0,
            open_positions=0,
        )
        context = TradeContext(
            exchange="kalshi",
            market_id="KXLOWTOKC-26APR27-T67",
            question="Will the minimum temperature be >67° on Apr 27?",
            direction="BUY_YES",
            market_price=0.02,
            yes_price=0.02,
            no_price=0.98,
            model_probability=0.38,
            edge=0.36,
            confidence=0.95,
            account_state=account_state,
            source_context={
                "market_id": "KXLOWTOKC-26APR27-T67",
                "question": "Will the minimum temperature be >67° on Apr 27?",
                "market_volume": 4500,
                "station_id": "KOKC",
                "distribution_probability": 0.28,
                "signals": {"live": 0.38, "price": 0.37},
                "confidence": 0.95,
            },
            metadata={"market_route": ALLOWED_MARKET_ROUTE},
        )
        kelly_sizer = Mock()
        kelly_sizer.calculate.return_value = 10.0
        risk_policy = Mock()
        risk_policy.check_trade.return_value = SimpleNamespace(
            approved=True,
            reason="Approved",
            adjusted_size=1.0,
            risk_score=0.0,
            warnings=[],
        )

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertTrue(decision.approved)
        self.assertEqual(decision.reasoning["weather_risk"]["hidden_gem_tier"], "exceptional")
        self.assertTrue(decision.reasoning["weather_risk"]["evidence_perfect"])
        self.assertEqual(decision.reasoning["weather_risk"]["evidence"]["weather_station_mapping"], "exact")
        self.assertGreaterEqual(decision.reasoning["weather_risk"]["evidence"]["source_agreement_score"], 0.95)
        risk_policy.check_trade.assert_called_once()

    def test_build_trade_decision_rejects_non_hidden_gem_at_or_below_fifty_percent(self):
        account_state = AccountState(
            starting_balance=100.0,
            current_balance=100.0,
            available_cash=25.0,
            reserved_capital=0.0,
            total_exposure=0.0,
            open_positions=0,
        )
        context = TradeContext(
            exchange="kalshi",
            market_id="market-1",
            question="Will regular market settle YES?",
            direction="BUY_YES",
            market_price=0.22,
            yes_price=0.22,
            no_price=0.78,
            model_probability=0.50,
            edge=0.28,
            confidence=0.9,
            account_state=account_state,
            source_context={"question": "Will regular market settle YES?"},
            metadata={"market_route": ALLOWED_MARKET_ROUTE},
        )
        kelly_sizer = Mock()
        risk_policy = Mock()

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason_code, "win_probability_below_non_hidden_gem_floor")
        kelly_sizer.calculate.assert_not_called()
        risk_policy.check_trade.assert_not_called()

    def test_build_trade_decision_rejects_duplicate_same_event_market(self):
        account_state = AccountState(
            starting_balance=100.0,
            current_balance=100.0,
            available_cash=40.0,
            reserved_capital=10.0,
            total_exposure=10.0,
            open_positions=1,
        )
        context = TradeContext(
            exchange="kalshi",
            market_id="KXHIGHNY-26APR16-T70",
            question="Will NYC high be below 70?",
            direction="BUY_YES",
            market_price=0.4,
            yes_price=0.4,
            no_price=0.6,
            model_probability=0.7,
            edge=0.2,
            confidence=0.9,
            account_state=account_state,
            source_context={"market_id": "KXHIGHNY-26APR16-T70"},
            metadata={
                "market_route": ALLOWED_MARKET_ROUTE,
                "event_snapshot": {
                    "event_key": "KXHIGHNY-26APR16",
                    "event_position_count_before": 1,
                    "event_exposure_before": 10.0,
                    "held_market_ids": ["KXHIGHNY-26APR16-T70"],
                }
            },
        )

        decision = build_trade_decision(
            context,
            kelly_sizer=Mock(),
            risk_policy=Mock(),
            min_edge=0.05,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason_code, "duplicate_market_id_open")

    def test_build_trade_decision_applies_retrade_decay_and_headroom(self):
        account_state = AccountState(
            starting_balance=100.0,
            current_balance=100.0,
            available_cash=40.0,
            reserved_capital=10.0,
            total_exposure=10.0,
            open_positions=1,
        )
        context = TradeContext(
            exchange="kalshi",
            market_id="KXHIGHNY-26APR16-T72",
            question="Will NYC high be below 72?",
            direction="BUY_YES",
            market_price=0.4,
            yes_price=0.4,
            no_price=0.6,
            model_probability=0.7,
            edge=0.2,
            confidence=0.9,
            account_state=account_state,
            source_context={"market_id": "KXHIGHNY-26APR16-T72"},
            metadata={
                "market_route": ALLOWED_MARKET_ROUTE,
                "event_snapshot": {
                    "event_key": "KXHIGHNY-26APR16",
                    "event_position_count_before": 2,
                    "event_exposure_before": 8.0,
                    "held_market_ids": ["KXHIGHNY-26APR16-T70", "KXHIGHNY-26APR16-T71"],
                },
                "retrade_policy": {
                    "max_event_exposure_pct": 0.10,
                    "max_event_positions": 3,
                    "retrade_edge_premium": 0.01,
                    "retrade_confidence_premium": 0.0,
                    "retrade_size_decay": 0.5,
                    "min_retrade_net_edge": 0.005,
                    "fee_rate": 0.07,
                },
            },
        )
        kelly_sizer = Mock()
        kelly_sizer.calculate.return_value = 20.0
        risk_policy = Mock()
        risk_policy.check_trade.return_value = SimpleNamespace(
            approved=True,
            reason="Approved",
            adjusted_size=2.0,
            risk_score=0.1,
            warnings=[],
        )

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertTrue(decision.approved)
        self.assertEqual(decision.requested_position_size, 2.0)
        self.assertEqual(decision.position_size, 2.0)
        self.assertAlmostEqual(decision.reasoning["retrade"]["size_decay_applied"], 0.25)

    def test_build_trade_decision_rejects_unviable_retrade_after_costs(self):
        account_state = AccountState(
            starting_balance=100.0,
            current_balance=100.0,
            available_cash=40.0,
            reserved_capital=10.0,
            total_exposure=10.0,
            open_positions=1,
        )
        context = TradeContext(
            exchange="kalshi",
            market_id="KXHIGHNY-26APR16-T72",
            question="Will NYC high be below 72?",
            direction="BUY_YES",
            market_price=0.55,
            yes_price=0.55,
            no_price=0.45,
            model_probability=0.58,
            edge=0.02,
            confidence=0.9,
            account_state=account_state,
            source_context={"market_id": "KXHIGHNY-26APR16-T72", "liquidity": 20.0},
            metadata={
                "market_route": ALLOWED_MARKET_ROUTE,
                "event_snapshot": {
                    "event_key": "KXHIGHNY-26APR16",
                    "event_position_count_before": 1,
                    "event_exposure_before": 2.0,
                    "held_market_ids": ["KXHIGHNY-26APR16-T70"],
                },
                "retrade_policy": {
                    "retrade_edge_premium": 0.0,
                    "retrade_size_decay": 1.0,
                    "min_retrade_net_edge": 0.01,
                    "fee_rate": 0.07,
                },
            },
        )
        kelly_sizer = Mock()
        kelly_sizer.calculate.return_value = 10.0
        risk_policy = Mock()
        risk_policy.check_trade.return_value = SimpleNamespace(
            approved=True,
            reason="Approved",
            adjusted_size=10.0,
            risk_score=0.1,
            warnings=[],
        )

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.01,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason_code, "retrade_net_edge_below_threshold")

    def test_build_trade_decision_rejects_same_family_retrade_without_price_improvement(self):
        account_state = AccountState(
            starting_balance=100.0,
            current_balance=100.0,
            available_cash=40.0,
            reserved_capital=10.0,
            total_exposure=10.0,
            open_positions=1,
        )
        context = TradeContext(
            exchange="kalshi",
            market_id="KXHIGHNY-26APR16-T72",
            question="Will NYC high be below 72?",
            direction="BUY_YES",
            market_price=0.41,
            yes_price=0.41,
            no_price=0.59,
            model_probability=0.7,
            edge=0.2,
            confidence=0.9,
            account_state=account_state,
            source_context={"market_id": "KXHIGHNY-26APR16-T72"},
            metadata={
                "market_route": ALLOWED_MARKET_ROUTE,
                "event_snapshot": {
                    "event_key": "KXHIGHNY-26APR16",
                    "candidate_family_key": "KXHIGHNY-26APR16",
                    "event_position_count_before": 1,
                    "event_exposure_before": 5.0,
                    "same_family_markets": ["KXHIGHNY-26APR16-T70"],
                    "best_same_family_entry_price": 0.42,
                    "best_yes_ask": 0.41,
                },
                "retrade_policy": {
                    "strict_event_overlap": False,
                    "require_price_improvement_for_same_market_family": True,
                    "price_improvement_ticks": 0.03,
                },
            },
        )

        decision = build_trade_decision(
            context,
            kelly_sizer=Mock(),
            risk_policy=Mock(),
            min_edge=0.05,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason_code, "same_family_price_not_improved")

    def test_build_trade_decision_tracks_slippage_aware_expected_profit(self):
        account_state = AccountState(
            starting_balance=100.0,
            current_balance=100.0,
            available_cash=40.0,
            reserved_capital=10.0,
            total_exposure=10.0,
            open_positions=1,
        )
        context = TradeContext(
            exchange="kalshi",
            market_id="KXHIGHNY-26APR16-T72",
            question="Will NYC high be below 72?",
            direction="BUY_YES",
            market_price=0.40,
            yes_price=0.40,
            no_price=0.60,
            model_probability=0.75,
            edge=0.15,
            confidence=0.9,
            account_state=account_state,
            source_context={"market_id": "KXHIGHNY-26APR16-T72", "liquidity": 100.0},
            metadata={
                "market_route": ALLOWED_MARKET_ROUTE,
                "event_snapshot": {
                    "event_key": "KXHIGHNY-26APR16",
                    "event_position_count_before": 1,
                    "event_exposure_before": 2.0,
                    "held_market_ids": ["KXHIGHNY-26APR16-T70"],
                    "best_yes_ask": 0.40,
                    "liquidity": 100.0,
                },
                "retrade_policy": {
                    "retrade_edge_premium": 0.0,
                    "retrade_size_decay": 1.0,
                    "min_retrade_net_edge": 0.01,
                    "min_retrade_expected_profit_usd": 0.25,
                    "fee_rate": 0.07,
                },
            },
        )
        kelly_sizer = Mock()
        kelly_sizer.calculate.return_value = 10.0
        risk_policy = Mock()
        risk_policy.check_trade.return_value = SimpleNamespace(
            approved=True,
            reason="Approved",
            adjusted_size=10.0,
            risk_score=0.1,
            warnings=[],
        )

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.01,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertTrue(decision.approved)
        self.assertIn("expected_profit_usd", decision.reasoning["retrade"])
        self.assertGreater(decision.reasoning["retrade"]["estimated_slippage"], 0.0)
        self.assertGreater(decision.reasoning["retrade"]["estimated_fill_price"], 0.40)

    def test_build_trade_decision_normalizes_risk_rejection_reason_code(self):
        account_state = AccountState(
            starting_balance=100.0,
            current_balance=100.0,
            available_cash=25.0,
            reserved_capital=0.0,
            total_exposure=0.0,
            open_positions=0,
        )
        context = TradeContext(
            exchange="kalshi",
            market_id="market-2",
            question="Will test settle YES?",
            direction="BUY_YES",
            market_price=0.4,
            yes_price=0.4,
            no_price=0.6,
            model_probability=0.7,
            edge=0.2,
            confidence=0.9,
            account_state=account_state,
            source_context={"question": "Will test settle YES?"},
            metadata={"market_route": ALLOWED_MARKET_ROUTE},
        )
        kelly_sizer = Mock()
        kelly_sizer.calculate.return_value = 10.0
        risk_policy = Mock()
        risk_policy.check_trade.return_value = SimpleNamespace(
            approved=False,
            reason="Max positions (15/15)",
            adjusted_size=0.0,
            risk_score=0.8,
            warnings=[],
        )

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.action, "SKIP")
        self.assertEqual(decision.reason, "Max positions (15/15)")
        self.assertEqual(decision.reason_code, "risk_max_positions_15_15")

    def test_build_trade_decision_fails_closed_when_market_route_missing(self):
        account_state = AccountState(
            starting_balance=100.0,
            current_balance=100.0,
            available_cash=25.0,
            reserved_capital=0.0,
            total_exposure=0.0,
            open_positions=0,
        )
        context = TradeContext(
            exchange="kalshi",
            market_id="KXPRIMEENGCONSUMPTION-30-WIND",
            question="Will wind power account for at least 30% of prime energy consumption?",
            direction="BUY_YES",
            market_price=0.4,
            yes_price=0.4,
            no_price=0.6,
            model_probability=0.7,
            edge=0.2,
            confidence=0.9,
            account_state=account_state,
            source_context={"question": "Will wind power account for at least 30%?"},
            metadata={"market_route_required": True},
        )

        decision = build_trade_decision(
            context,
            kelly_sizer=Mock(),
            risk_policy=Mock(),
            min_edge=0.05,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason_code, "missing_market_route")
        self.assertEqual(decision.reasoning["market_route_enforcement"], "stable_required")
        self.assertTrue(decision.reasoning["market_route_required"])

    def test_build_trade_decision_blocks_disallowed_market_route(self):
        account_state = AccountState(
            starting_balance=100.0,
            current_balance=100.0,
            available_cash=25.0,
            reserved_capital=0.0,
            total_exposure=0.0,
            open_positions=0,
        )
        context = TradeContext(
            exchange="kalshi",
            market_id="KXPRIMEENGCONSUMPTION-30-WIND",
            question="Will wind power account for at least 30% of prime energy consumption?",
            direction="BUY_YES",
            market_price=0.4,
            yes_price=0.4,
            no_price=0.6,
            model_probability=0.7,
            edge=0.2,
            confidence=0.9,
            account_state=account_state,
            source_context={"question": "Will wind power account for at least 30%?"},
            metadata={
                "market_route_required": True,
                "market_route": {"allowed": False, "reason_code": "unknown_market_route"},
            },
        )

        decision = build_trade_decision(
            context,
            kelly_sizer=Mock(),
            risk_policy=Mock(),
            min_edge=0.05,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason_code, "unknown_market_route")
        self.assertEqual(decision.reasoning["market_route_enforcement"], "stable_required")

    def _bucket_missing_distribution_context(self, strategy_policy: dict | None = None) -> TradeContext:
        metadata = {"market_route": ALLOWED_MARKET_ROUTE}
        if strategy_policy is not None:
            metadata["strategy_policy"] = strategy_policy
        return TradeContext(
            exchange="kalshi",
            market_id="KXHIGHMIA-26APR26-B82.5",
            question="Will the high temp in Miami be 82-83° on Apr 26?",
            direction="BUY_YES",
            market_price=0.03,
            yes_price=0.03,
            no_price=0.97,
            model_probability=0.15,
            edge=0.12,
            confidence=0.9,
            account_state=AccountState(
                starting_balance=100.0,
                current_balance=100.0,
                available_cash=100.0,
                reserved_capital=0.0,
                total_exposure=0.0,
                open_positions=0,
            ),
            source_context={
                "market_id": "KXHIGHMIA-26APR26-B82.5",
                "question": "Will the high temp in Miami be 82-83° on Apr 26?",
                "market_volume": 900,
                "station_id": "KMIA",
                "signals": {"live": 0.15, "price": 0.14},
            },
            metadata=metadata,
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

    def _beta_policy(
        self,
        mode: str,
        *,
        weather_hidden_gem_evidence_card: bool = False,
        bucket_distribution_scoring: bool = False,
        hidden_gem_lane_gates: bool = False,
    ) -> dict:
        return {
            "version": "beta",
            "beta": {
                "mode": mode,
                "features": {
                    "weather_hidden_gem_evidence_card": weather_hidden_gem_evidence_card,
                    "bucket_distribution_scoring": bucket_distribution_scoring,
                    "hidden_gem_lane_gates": hidden_gem_lane_gates,
                },
            },
        }


if __name__ == "__main__":
    unittest.main()
