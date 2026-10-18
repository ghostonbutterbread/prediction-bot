"""Opt-in research caps exercised through real composition, Kelly and risk."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

from bot.forward_composed_paper import compose_forward_rows, evaluate_forward_composition
from bot.risk import RiskManager
from bot.strategies.enhanced import KellySizer


CAPS = {"weak": 0.01, "strong": 0.03}  # Example research policy, not calibrated odds.
SCRATCH = os.environ["TMPDIR"]


def cohort(grade: Any = "WEAK_YES", side="BUY_YES"):
    identity = {"shared_candidate_id": "candidate", "shared_snapshot_id": "snapshot",
                "run_id": "run", "market_id": "KXHIGHNY-26OCT18-T80",
                "observed_at": "2026-10-18T12:00:00+00:00"}
    baseline = {**identity, "policy": "control_stable", "decision_id": "control",
                "action": side, "model_probability": 0.8 if side == "BUY_YES" else 0.2,
                "confidence": 0.9, "entry_price": 0.4,
                "question": "Will NYC high temperature be above 80 degrees?", "exchange": "kalshi",
                "market_route": {"allowed": True, "handler_id": "weather.daily_temperature.v1"},
                "event_ticker": "KXHIGHNY-26OCT18"}
    router = {**identity, "policy": "shadow_source_router", "decision_id": "router",
              "action": side, "provenance": {"source_router": {
                  "source_grade": grade,
                  "direct_history": {"mode": "committed_index_direct_read"},
                  # Valid historical diagnostics must never reach the intent.
                  "source_observations": [{"prior_outcome": "NO", "rank": 0.99}]}}}
    guard = {**identity, "policy": "shadow_fee_aware_loss_guard", "decision_id": "guard",
             "action": side, "entry_price": 0.4,
             "provenance": {"payout_aware": {"selected_side_ask": 0.4, "entry_price": 0.4,
                                            "quote_not_fill": True}}}
    return [baseline, router, guard]


class RouterSourceStrengthTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.env = patch.dict(os.environ, {"TMPDIR": SCRATCH}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        for target in ("socket.socket.connect", "socket.create_connection"):
            guard = patch(target, side_effect=AssertionError("network forbidden"))
            guard.start()
            self.addCleanup(guard.stop)

    def risk(self, caps=CAPS, **settings):
        config = {"data_dir": str(self.root), "starting_balance": 100.0,
                  "risk": {"min_position_size": 0.01, **settings}}
        if caps is not None:
            config["risk"]["source_router_position_caps"] = copy.deepcopy(caps)
        return RiskManager(config)

    def evaluate(self, rows, *, risk=None, kelly=None, combination=None):
        return evaluate_forward_composition(
            rows, [], starting_balance_usd=100, risk_policy=risk or self.risk(),
            kelly_sizer=kelly or KellySizer(fraction=0.5, max_bet_pct=0.1, fee_rate=0.07),
            min_edge=0.01, min_confidence=0.5, max_entry_price=0.99,
            combination=combination)

    def persistent_case(self, rows, *, account=None, name="wallet"):
        from datetime import datetime
        from bot.persistent_composed_paper import consume_persistent_composed_paper
        baseline, router, guard = rows
        picker = {**baseline, "policy": "shadow_pre_risk_picker", "decision_id": "picker",
                  "provenance": {"picker_intent_source": "decision_artifact.strategy_signal"}}
        lanes = [{**baseline, "action": "SKIP"}, picker, router,
                 {**guard, "policy": "shadow_pre_risk_fee_aware_loss_guard"}]
        state = {"publisher": {"runtime_kind": "collector", "instance_id": "owner"},
                 "latest_snapshot": {"snapshot_id": "snapshot", "publisher_runtime": "collector",
                                     "publisher_instance_id": "owner", "published_at": baseline["observed_at"]}}
        selection = Path(__file__).resolve().parents[1] / "lane_compositions" / "pre_risk_router_guard_kelly.yaml"
        root = self.root / name / "paper"
        params: dict[str, Any] = dict(saved_config_path=selection, lane_rows=lanes, cohort_id="new-test-cohort",
                      publisher_state=state, paper_root=root,
                      protected_roots={key: self.root / key / "paper" for key in ("stable", "beta", "live")},
                      account_config=account or {"starting_balance": 100.0,
                          "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
                          "risk": {"source_router_position_caps": CAPS, "kelly_fraction": 0.5, "max_bet_pct": 0.1}},
                      enabled=True, now=datetime.fromisoformat(baseline["observed_at"]))
        return root, params, consume_persistent_composed_paper

    def test_final_capped_stake_rechecks_real_kelly_economics(self):
        for side, grade in (("BUY_YES", "WEAK_YES"), ("BUY_NO", "WEAK_NO")):
            for kwargs, code in (({"min_expected_net_profit_usd": 1.0}, "final_expected_net_profit_below_minimum"),
                                 ({"min_position_size_usd": 2.0}, "final_position_size_below_minimum")):
                with self.subTest(side=side, economics=kwargs):
                    kelly = KellySizer(fraction=0.5, max_bet_pct=0.1, fee_rate=0.07, **kwargs)
                    self.assertEqual(kelly.calculate(0.8, 0.4, 100), 10.0)
                    report = self.evaluate(cohort(grade, side), kelly=kelly)
                    row = report["combined_wallet"]["decision_rows"][0]
                    self.assertEqual(row["status"], "skipped")
                    self.assertEqual(row["approved_stake_usd"], 0)
                    self.assertEqual(row["reason_code"], code)
                    self.assertEqual(row["shared_core_reasoning"]["final_trade_economics"]["position_size_usd"], 1.0)
                    self.assertAlmostEqual(row["shared_core_reasoning"]["final_trade_economics"]["expected_net_profit_usd"], 0.916)
                    self.assertEqual(report["control_wallet"]["decision_rows"][0]["status"], "opened")
                    self.assertEqual(report["combined_wallet"]["open_positions"], [])

    def test_no_config_restart_accepts_legacy_outcome_free_signal_fingerprint(self):
        import bot.persistent_composed_paper as consumer
        root, params, consume = self.persistent_case(cohort())
        del params["account_config"]["risk"]["source_router_position_caps"]
        original = consumer._prepare_signal
        def legacy_signal(*args, **kwargs):
            signal = original(*args, **kwargs)
            signal.pop("router_selection", None)
            signal.pop("router_decision_metadata", None)
            return signal
        with patch.object(consumer, "_prepare_signal", side_effect=legacy_signal):
            self.assertEqual(consume(**params)["decisions"][0]["status"], "opened")
        self.assertEqual(consume(**params)["decisions"][0]["status"], "already_processed")
        session = json.loads(next(root.glob("sim_*.json")).read_text())
        self.assertEqual(len(session["trades"]), 1)
        self.assertEqual(session["trades"][0]["position_size"], 10)

    def test_persistent_final_economics_skip_is_journaled_without_reserving_capital(self):
        root, params, consume = self.persistent_case(cohort("WEAK_NO", "BUY_NO"))
        params["account_config"]["trade_economics"] = {"min_expected_net_profit_usd": 1.0}
        report = consume(**params)
        row = report["decisions"][0]
        self.assertEqual((row["status"], row["reason_code"]), ("skipped", "final_expected_net_profit_below_minimum"))
        session = json.loads(next(root.glob("sim_*.json")).read_text())
        self.assertEqual(session["trades"], [])
        self.assertEqual((session["reserved_capital"], session["available_cash"]), (0, 100))
        restarted = consume(**params)
        self.assertEqual(restarted["decisions"][0]["status"], "already_processed")
        self.assertEqual(restarted["decisions"][0]["reason_code"], row["reason_code"])

    def test_final_cap_preserves_configured_retrade_profit_floor(self):
        from bot.composed_lane_wallet import build_composed_trade_context
        from bot.shared_core import AccountState, build_trade_decision
        intent = next(i for i in compose_forward_rows(cohort())[1] if i["lane_id"] == "forward_composed_paper")
        intent["event_snapshot"] = {"event_position_count_before": 1, "event_exposure_before": 1.0,
                                    "max_event_exposure_pct": 0.1}
        context, _ = build_composed_trade_context(intent, AccountState(100, 100, 99, 1, 1, 1))
        context.metadata["retrade_policy"] = {"min_retrade_expected_profit_usd": 0.5}
        decision = build_trade_decision(context, kelly_sizer=KellySizer(fee_rate=0.07), risk_policy=self.risk(),
                                        min_edge=0.01, min_confidence=0.5, max_entry_price=0.99)
        self.assertGreater(decision.reasoning["retrade"]["expected_profit_usd"], 0.5)
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason_code, "final_retrade_expected_profit_below_threshold")
        self.assertLess(decision.reasoning["retrade"]["final_cost_summary"]["expected_profit_usd"], 0.5)

    def test_persistent_no_side_cap_survives_receipt_reload_and_restart(self):
        from bot.simulator import Simulator
        for grade, stake in (("WEAK_NO", 1.0), ("STRONG_NO", 3.0)):
            with self.subTest(grade=grade):
                root, params, consume = self.persistent_case(cohort(grade, "BUY_NO"), name=grade)
                report = consume(**params)
                self.assertEqual(report["decisions"][0]["status"], "opened")
                session = json.loads(next(root.glob("sim_*.json")).read_text())
                self.assertEqual(len(session["trades"]), 1)
                trade = session["trades"][0]
                self.assertEqual(trade["position_size"], stake)
                self.assertEqual(trade["direction"], "BUY_NO")
                self.assertEqual(trade["model_probability"], 0.8)  # Stored selected-side probability.
                cap = trade["decision_trace"]["risk"]["metadata"]["source_router_position_cap"]
                self.assertEqual(cap["source_grade"], grade)
                self.assertEqual(cap["selection"]["decision_id"], "router")
                self.assertNotIn("prior_outcome", json.dumps(trade["decision_trace"]))
                self.assertEqual((session["reserved_capital"], session["available_cash"]), (stake, 100 - stake))
                self.assertEqual(consume(**params)["decisions"][0]["status"], "already_processed")
                reloaded = Simulator({**params["account_config"], "data_dir": str(root),
                    "trading": {"mode": "paper"}, "paper_wallets": {"allow_legacy_parent_session_lookup": False}})
                self.assertEqual(len(reloaded.trades), 1)
                self.assertEqual(reloaded.available_cash, 100 - stake)
                self.assertEqual(reloaded.risk.state.total_exposure, stake)
                self.assertEqual(reloaded.risk.data_path, root / "risk_state.json")

    def test_grade_matrix_no_config_and_guard_only_preserve_control(self):
        for side in ("BUY_YES", "BUY_NO"):
            for strength, stake in (("WEAK", 1.0), ("STRONG", 3.0)):
                with self.subTest(side=side, strength=strength):
                    rows = cohort(strength + "_" + side.removeprefix("BUY_"), side)
                    report = self.evaluate(rows)
                    self.assertEqual(report["combined_wallet"]["decision_rows"][0]["approved_stake_usd"], stake)
                    normalized = report["combined_wallet"]["decision_rows"][0]["shared_core_reasoning"]["normalized"]
                    self.assertAlmostEqual(normalized["win_probability"], 0.8)
                    off = self.evaluate(rows, risk=self.risk(caps=None))
                    self.assertEqual(off["combined_wallet"]["decision_rows"][0]["approved_stake_usd"], 10)
                    self.assertNotIn("source_router_position_cap", off["combined_wallet"]["decision_rows"][0]["shared_core_reasoning"]["risk"]["metadata"])
        guard_only = {"base_lane": "control_stable", "action_lane": "control_stable",
                      "sizing_lane": "control_stable", "price_lane": "shadow_fee_aware_loss_guard",
                      "fallback_to_base": False, "vetoes": [{"lane": "shadow_fee_aware_loss_guard",
                                                            "mode": "require_agreement", "required": True}]}
        report = self.evaluate(cohort(), combination=guard_only)
        self.assertEqual(report["combined_wallet"]["decision_rows"][0]["approved_stake_usd"], 10)

    def test_existing_action_and_veto_provenance_fence_missing_grade(self):
        from bot.composed_lane_wallet import evaluate_composed_intents
        for provenance in (
                {"action_lane": "shadow_source_router", "source_decision_id": "router-action"},
                {"action_lane": "control_stable", "vetoes": [{"lane": "shadow_source_router", "decision_id": "router-veto"}]}):
            for grade in (None, "STRONG_NO"):
                with self.subTest(provenance=provenance, grade=grade):
                    intent = next(i for i in compose_forward_rows(cohort("STRONG_NO", "BUY_NO"))[1]
                                  if i["lane_id"] == "control_stable")
                    intent["provenance"] = provenance
                    if grade is not None:
                        intent["source_context"]["router_decision_metadata"] = {"source_grade": grade}
                    report = evaluate_composed_intents(intents=[intent], resolutions=[], starting_balance_usd=100,
                        kelly_sizer=KellySizer(fee_rate=0.07), risk_policy=self.risk(),
                        min_edge=0.01, min_confidence=0.5, max_entry_price=0.99)
                    row = report["decision_rows"][0]
                    if grade is None:
                        self.assertEqual((row["status"], row["reason_code"]), ("skipped", "source_router_cap_evidence_invalid"))
                    else:
                        self.assertEqual(row["approved_stake_usd"], 3)
                        cap = row["shared_core_reasoning"]["risk"]["metadata"]["source_router_position_cap"]
                        self.assertEqual(cap["selection"]["policy"], "shadow_source_router")

    def test_missing_unknown_or_malformed_selected_grade_never_bypasses_caps(self):
        for grade in (None, "", "UNKNOWN", "weak_yes", [], {"outcome": "YES"}, 0.99, True):
            with self.subTest(grade=grade):
                report = self.evaluate(cohort(grade))
                row = report["combined_wallet"]["decision_rows"][0]
                self.assertEqual((row["status"], row["reason_code"]), ("skipped", "source_router_cap_evidence_invalid"))
                self.assertEqual(report["control_wallet"]["decision_rows"][0]["approved_stake_usd"], 10)
        # Explicit producer marker still fences a missing evidence envelope.
        decision = self.risk().check_trade({"router_selection": {"role": "action"}}, 10)
        self.assertFalse(decision.approved)
        self.assertEqual(decision.metadata["reason_code"], "source_router_cap_evidence_invalid")
        # An omitted strength in a partial policy is not an unlimited category.
        decision = self.risk(caps={"weak": 0.01}).check_trade(
            {"router_decision_metadata": {"source_grade": "STRONG_YES"}}, 10)
        self.assertFalse(decision.approved)

    def test_router_caps_intersect_kelly_cash_exposure_event_and_position_limits(self):
        signal = {"router_decision_metadata": {"source_grade": "STRONG_YES"}}
        for requested, settings, cash, exposure, expected in (
                (0.55, {}, 100, 0, 0.55),
                (10, {"max_position_size_usd": 0.75}, 100, 0, 0.75),
                (10, {}, 0.85, 0, 0.85),
                (10, {"max_tradable_balance_usd": 0.65}, 100, 0, 0.65),
                (10, {"max_exposure_pct": 0.4}, 100, 39.1, 0.9),
                (10, {}, 100, 0, 3.0)):
            with self.subTest(requested=requested, settings=settings, cash=cash, exposure=exposure):
                risk = self.risk(**settings)
                risk.state.total_exposure = exposure
                decision = risk.check_trade(signal, requested, available_cash=cash)
                self.assertTrue(decision.approved)
                self.assertAlmostEqual(decision.adjusted_size, expected)
                self.assertLessEqual(decision.adjusted_size, requested)
                self.assertEqual(decision.metadata["source_router_position_cap"]["bankroll"], 100)
        from bot.composed_lane_wallet import build_composed_trade_context
        from bot.shared_core import AccountState, build_trade_decision
        intent = next(i for i in compose_forward_rows(cohort("STRONG_YES"))[1] if i["lane_id"] == "forward_composed_paper")
        intent["event_snapshot"] = {"max_event_exposure_pct": 0.02}
        context, _ = build_composed_trade_context(intent, AccountState(100, 100, 100, 0, 0, 0))
        decision = build_trade_decision(context, kelly_sizer=KellySizer(fee_rate=0.07), risk_policy=self.risk(),
                                        min_edge=0.01, min_confidence=0.5, max_entry_price=0.99)
        self.assertTrue(decision.approved)
        self.assertEqual(decision.position_size, 2)
        for cap in (0, 0.00009, 0.00999):
            decision = self.risk(caps={"weak": cap}, min_position_size=1).check_trade(
                {"router_decision_metadata": {"source_grade": "WEAK_YES"}}, 10)
            self.assertFalse(decision.approved)
            self.assertEqual(decision.metadata["reason_code"], "source_router_cap_below_minimum")

    def test_capped_custom_sizer_does_not_gain_real_kelly_minimums(self):
        class CustomKelly:
            def calculate(self, probability, price, cash):
                return 0.5
        report = self.evaluate(cohort(), kelly=CustomKelly())
        self.assertEqual(report["combined_wallet"]["decision_rows"][0]["approved_stake_usd"], 0.5)
        self.assertNotIn("final_trade_economics", report["combined_wallet"]["decision_rows"][0]["shared_core_reasoning"])

    def test_synthetic_wallet_caps_follow_current_bankroll_without_mutating_risk(self):
        from bot.composed_lane_wallet import evaluate_composed_intents
        first = next(i for i in compose_forward_rows(cohort())[1] if i["lane_id"] == "forward_composed_paper")
        second = {**first, "decision_id": "second", "market_id": "KXHIGHNY-26OCT19-T80",
                  "shared_candidate_id": "second", "observed_at": "2026-10-18T14:00:00+00:00"}
        resolution = {key: first[key] for key in ("decision_id", "market_id", "shared_candidate_id", "run_id")}
        resolution.update(outcome="NO", settlement_ts="2026-10-18T13:00:00+00:00",
                          authoritative=True, resolution_row_sha256="a" * 64)
        risk = self.risk()
        before = copy.deepcopy(risk.state)
        report = evaluate_composed_intents(intents=[first, second], resolutions=[resolution],
            starting_balance_usd=100, kelly_sizer=KellySizer(fee_rate=0.07, min_position_size_usd=0.01),
            risk_policy=risk, min_edge=0.01, min_confidence=0.5, max_entry_price=0.99)
        self.assertEqual([row["approved_stake_usd"] for row in report["decision_rows"]], [1.0, 0.99])
        self.assertEqual(report["decision_rows"][1]["shared_core_reasoning"]["risk"]["metadata"]["source_router_position_cap"]["bankroll"], 99)
        self.assertEqual(risk.state, before)
        self.assertFalse((self.root / "risk_state.json").exists())

    def test_config_rejects_malformed_research_caps_before_state_io(self):
        invalid = [None, {}, [], True, {"WEAK": 0.01}, {"weak": "0.01"},
                   {"weak": True}, {"weak": float("nan")}, {"weak": float("inf")},
                   {"weak": -0.01}, {"strong": 1.01}, {"weak": 10 ** 400}]
        for caps in invalid:
            with self.subTest(caps=caps), patch.object(RiskManager, "_load_state", side_effect=AssertionError("state IO")):
                with self.assertRaisesRegex(ValueError, "source_router_position_caps"):
                    RiskManager({"data_dir": str(self.root), "risk": {"source_router_position_caps": caps}})

    def test_weak_cap_reaches_real_forward_wallet_without_capping_control(self):
        rows = cohort()
        result = self.evaluate(rows)
        combined = result["combined_wallet"]["decision_rows"][0]
        control = result["control_wallet"]["decision_rows"][0]
        self.assertEqual(combined["status"], "opened")
        self.assertEqual(combined["approved_stake_usd"], 1.0)
        self.assertEqual(control["approved_stake_usd"], 10.0)
        self.assertEqual(result["combined_wallet"]["open_positions"][0]["model_probability"], 0.8)
        cap = combined["shared_core_reasoning"]["risk"]["metadata"]["source_router_position_cap"]
        self.assertEqual((cap["source_grade"], cap["strength"], cap["cap_fraction"], cap["cap_usd"]),
                         ("WEAK_YES", "weak", 0.01, 1.0))
        self.assertEqual(cap["selection"]["decision_id"], "router")
        self.assertTrue(any("weak" in warning.lower() for warning in combined["shared_core_reasoning"]["risk"]["warnings"]))
        _, intents = compose_forward_rows(rows)
        self.assertNotIn("prior_outcome", json.dumps(intents))
        self.assertNotIn("source_observations", json.dumps(intents))
        self.assertNotIn("rank", json.dumps(intents))
        self.assertNotIn("router_decision_metadata", intents[0]["source_context"])


if __name__ == "__main__":
    unittest.main()
