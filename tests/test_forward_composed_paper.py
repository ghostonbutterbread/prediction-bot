"""The forward adapter consumes paired lane rows, never a resolved candidate."""
import unittest
import tempfile
import json
import hashlib
from pathlib import Path
from io import StringIO
from contextlib import redirect_stdout

from bot.risk import RiskDecision
from bot.composed_lane_wallet import evaluate_composed_intents
from bot.forward_composed_paper import compose_forward_rows, evaluate_forward_composition, observe_fresh_forward_snapshot


class FixedKelly:
    def calculate(self, win_probability, entry_price, bankroll):
        return 25.0


class ApprovingRisk:
    def check_trade(self, signal, position_size, *, available_cash=None):
        return RiskDecision(approved=True, adjusted_size=position_size, original_size=position_size)


def cohort(candidate="c1", market="M1", event="E1", side="BUY_YES", price=0.4):
    shared = {"shared_candidate_id": candidate, "shared_snapshot_id": "snapshot-1", "run_id": "run-1",
              "market_id": market, "observed_at": "2026-09-01T12:00:00+00:00"}
    control = {**shared, "policy": "control_stable", "decision_id": "control-" + candidate,
               "action": side, "model_probability": 0.8 if side == "BUY_YES" else 0.2,
               "confidence": 0.9, "entry_price": price, "question": "Will it happen?", "exchange": "kalshi",
               "market_route": {"allowed": True, "handler_id": "weather"}, "event_ticker": event}
    router = {**shared, "policy": "shadow_source_router", "decision_id": "router-" + candidate,
              "action": side, "provenance": {"source_router": {"direct_history": {"mode": "committed_index_direct_read"}}}}
    guard = {**shared, "policy": "shadow_fee_aware_loss_guard", "decision_id": "guard-" + candidate,
             "action": side, "entry_price": price,
             "provenance": {"payout_aware": {"selected_side_ask": price, "entry_price": price,
                                            "fee_rate": 0.07, "quote_not_fill": True}}}
    return control, router, guard


class ForwardCompositionTests(unittest.TestCase):
    def test_saved_guard_only_variant_changes_selection_without_rewriting_lanes(self):
        from bot.forward_composed_paper import validate_forward_combination
        from scripts.paper_shadow_lane_compose_replay import _load_config, validate_saved_combination
        base = validate_saved_combination(_load_config(Path(__file__).resolve().parent.parent /
            "lane_compositions" / "stable_router_guard_kelly.yaml"))
        guard_only = {**base, "name": "guard_only", "vetoes": [base["vetoes"][1]]}
        validate_forward_combination(guard_only)
        control, router, guard = cohort()
        router["provenance"] = {"source_router": {}}
        strict_pairs, strict_intents = compose_forward_rows([control, router, guard], combination=base)
        loose_pairs, loose_intents = compose_forward_rows([control, router, guard], combination=guard_only)
        self.assertEqual((strict_pairs[0]["combined_action"], strict_pairs[0]["reason_code"]),
                         ("SKIP", "router_history_unverified"))
        self.assertEqual(loose_pairs[0]["combined_action"], "BUY_YES")
        self.assertEqual(loose_pairs[0]["reason_code"], "guard_passed_without_router")
        self.assertFalse(any(row["lane_id"] == "forward_composed_paper" for row in strict_intents))
        self.assertTrue(any(row["lane_id"] == "forward_composed_paper" for row in loose_intents))

    def test_manual_observer_reads_saved_combination_and_reports_same_file_identity(self):
        from datetime import datetime, timezone
        from scripts.forward_composed_paper import main
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime.now(timezone.utc).isoformat()
            ledger = root / "lane_rows.jsonl"
            state = root / "state.json"
            saved = Path(__file__).resolve().parent.parent / "lane_compositions" / "stable_router_guard_kelly.yaml"
            ledger.write_text("".join(json.dumps({**row, "observed_at": now}) + "\n" for row in cohort()), encoding="utf-8")
            state.write_text(json.dumps({"publisher": {"runtime_kind": "collector", "instance_id": "primary"},
                "latest_snapshot": {"snapshot_id": "snapshot-1", "publisher_runtime": "collector",
                                    "publisher_instance_id": "primary", "published_at": now}}), encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--lane-decision-path", str(ledger),
                    "--shared-market-state-path", str(state),
                    "--composition-config", str(saved), "--enable-paper-observer"]), 0)
            report = json.loads(output.getvalue())
            self.assertEqual(report["composition_name"], "stable_router_guard_kelly")
            self.assertEqual(report["composition_sha256"], hashlib.sha256(saved.read_bytes()).hexdigest())
            self.assertEqual(report["combined_wallet"]["summary"]["opened_positions"], 1)

    def test_saved_replay_combination_routes_to_same_forward_wallet_seam(self):
        from scripts.paper_shadow_lane_compose_replay import _load_config, validate_saved_combination
        combination = validate_saved_combination(_load_config(
            Path(__file__).resolve().parent.parent / "lane_compositions" / "stable_router_guard_kelly.yaml"))
        result = evaluate_forward_composition(list(cohort()), [], starting_balance_usd=100,
            kelly_sizer=FixedKelly(), risk_policy=ApprovingRisk(), min_edge=0.01,
            min_confidence=0.5, max_entry_price=0.99, combination=combination)
        self.assertEqual(result["composition_name"], "stable_router_guard_kelly")
        self.assertEqual(result["combined_wallet"]["summary"]["opened_positions"], 1)
        self.assertEqual(result["combined_wallet"]["decision_rows"][0]["approved_stake_usd"], 10)
        unsupported = {**combination, "action_lane": "shadow_source_router"}
        with self.assertRaisesRegex(ValueError, "unsupported forward combination"):
            evaluate_forward_composition(list(cohort()), [], starting_balance_usd=100,
                kelly_sizer=FixedKelly(), risk_policy=ApprovingRisk(), min_edge=0.01,
                min_confidence=0.5, max_entry_price=0.99, combination=unsupported)

    def test_manual_cli_reads_forward_lane_ledger_without_writing_input_or_wallet(self):
        from datetime import datetime, timezone
        from scripts.forward_composed_paper import main
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            now = datetime.now(timezone.utc).isoformat()
            rows = [{**r, "observed_at": now} for r in cohort()]
            ledger = root / "lanes.jsonl"
            state = root / "state.json"
            ledger.write_text("".join(json.dumps(row) + "\n" for row in rows))
            state.write_text(json.dumps({"publisher": {"runtime_kind": "collector", "instance_id": "primary"},
                "latest_snapshot": {"snapshot_id": "snapshot-1", "publisher_runtime": "collector",
                                    "publisher_instance_id": "primary", "published_at": now}}))
            args = ["--lane-decision-path", str(ledger), "--shared-market-state-path", str(state)]
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(args), 0)
            self.assertEqual(json.loads(output.getvalue())["reason_code"], "observer_disabled")
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main([*args, "--enable-paper-observer"]), 0)
            report = json.loads(output.getvalue())
            self.assertEqual(report["combined_wallet"]["summary"]["opened_positions"], 1)
            self.assertEqual(len(list(root.iterdir())), 2)
            self.assertEqual(len(ledger.read_text().splitlines()), 3)

    def test_disabled_and_stale_shared_snapshots_never_reach_wallet(self):
        from datetime import datetime, timezone
        rows = list(cohort())
        state = {"publisher": {"runtime_kind": "collector", "instance_id": "primary"},
                 "latest_snapshot": {"snapshot_id": "snapshot-1", "publisher_runtime": "collector",
                                     "publisher_instance_id": "primary", "published_at": "2026-09-01T12:00:00+00:00"}}
        settings = dict(starting_balance_usd=100, kelly_sizer=FixedKelly(), risk_policy=ApprovingRisk(),
                        min_edge=0.01, min_confidence=0.5, max_entry_price=0.99)
        now = datetime(2026, 9, 1, 12, 0, 30, tzinfo=timezone.utc)
        self.assertEqual(observe_fresh_forward_snapshot(rows, [], state, enabled=False,
                         now=now, max_snapshot_age_seconds=60, **settings)["reason_code"], "observer_disabled")
        self.assertEqual(observe_fresh_forward_snapshot(rows, [], state, enabled=True,
                         now=datetime(2026, 9, 1, 13, tzinfo=timezone.utc), max_snapshot_age_seconds=60, **settings)["reason_code"], "shared_snapshot_stale_or_mismatched")
        bad = {**state, "publisher": {"runtime_kind": "collector", "instance_id": "other"}}
        self.assertEqual(observe_fresh_forward_snapshot(rows, [], bad, enabled=True,
                         now=now, max_snapshot_age_seconds=60, **settings)["reason_code"], "shared_snapshot_stale_or_mismatched")
        result = observe_fresh_forward_snapshot(rows, [], state, enabled=True,
                         now=now, max_snapshot_age_seconds=60, **settings)
        self.assertEqual(result["combined_wallet"]["summary"]["opened_positions"], 1)

    def test_paired_buy_connects_veto_guard_and_wallet_without_using_router_score_as_probability(self):
        rows = cohort()
        result = evaluate_forward_composition(list(rows), [], starting_balance_usd=100,
            kelly_sizer=FixedKelly(), risk_policy=ApprovingRisk(), min_edge=0.01,
            min_confidence=0.5, max_entry_price=0.99)
        self.assertEqual(result["pairs"][0]["combined_action"], "BUY_YES")
        self.assertEqual(result["pairs"][0]["control_action"], "BUY_YES")
        self.assertEqual(result["combined_wallet"]["decision_rows"][0]["status"], "opened")
        self.assertEqual(result["combined_wallet"]["decision_rows"][0]["approved_stake_usd"], 10)
        self.assertEqual(result["combined_wallet"]["open_positions"][0]["event_key"], "E1")
        self.assertEqual(result["combined_wallet"]["open_positions"][0]["entry_price"], 0.4)
        self.assertEqual(result["combined_wallet"]["open_positions"][0]["model_probability"], 0.8)
        self.assertEqual(result["control_wallet"]["decision_rows"][0]["status"], "opened")

    def test_disagreement_or_missing_strict_history_vetoes_without_side_flip(self):
        control, router, guard = cohort(side="BUY_NO")
        for changed, reason in [({**router, "action": "BUY_YES"}, "router_disagrees"),
                                ({**router, "provenance": {"source_router": {}}}, "router_history_unverified"),
                                ({**router, "action": "SKIP", "reason_code": "insufficient_history"}, "insufficient_history")]:
            with self.subTest(reason=reason):
                pair = compose_forward_rows([control, changed, guard])[0][0]
                self.assertEqual((pair["combined_action"], pair["reason_code"]), ("SKIP", reason))

    def test_guard_uses_same_side_quote_and_rejects_missing_or_future_book(self):
        control, router, guard = cohort(side="BUY_NO", price=0.6)
        for changed in ({**guard, "action": "SKIP", "reason_code": "executable_side_ask_unavailable"},
                        {**guard, "action": "BUY_YES"}):
            pair = compose_forward_rows([control, router, changed])[0][0]
            self.assertEqual(pair["combined_action"], "SKIP")
        self.assertEqual(compose_forward_rows([control, router, guard])[0][0]["combined_action"], "BUY_NO")

    def test_control_uses_recorded_ask_when_guard_estimates_higher_entry(self):
        control, router, guard = cohort(price=0.4)
        guard = {**guard, "entry_price": 0.45, "provenance": {"payout_aware": {
            "selected_side_ask": 0.4, "entry_price": 0.45,
            "fee_rate": 0.07, "quote_not_fill": True}}}
        result = evaluate_forward_composition([control, router, guard], [], starting_balance_usd=100,
            kelly_sizer=FixedKelly(), risk_policy=ApprovingRisk(), min_edge=0.01,
            min_confidence=0.5, max_entry_price=0.99)
        self.assertEqual(result["pairs"][0]["control_economics_status"], "quote_proxy")
        self.assertEqual(result["control_wallet"]["summary"]["opened_positions"], 1)
        self.assertEqual(result["control_wallet"]["open_positions"][0]["entry_price"], 0.4)
        self.assertEqual(result["combined_wallet"]["summary"]["opened_positions"], 1)
        self.assertEqual(result["combined_wallet"]["open_positions"][0]["entry_price"], 0.45)

    def test_reference_only_control_has_no_synthetic_economic_position(self):
        control, router, guard = cohort()
        guard = {**guard, "action": "SKIP", "reason_code": "executable_side_ask_unavailable",
                 "provenance": {}}
        result = evaluate_forward_composition([control, router, guard], [], starting_balance_usd=100,
            kelly_sizer=FixedKelly(), risk_policy=ApprovingRisk(), min_edge=0.01,
            min_confidence=0.5, max_entry_price=0.99)
        self.assertEqual(result["pairs"][0]["control_action"], "BUY_YES")
        self.assertEqual(result["pairs"][0]["reason_code"], "executable_side_ask_unavailable")
        self.assertEqual(result["control_wallet"]["summary"]["opened_positions"], 0)
        self.assertEqual(result["combined_wallet"]["summary"]["opened_positions"], 0)

    def test_legacy_control_without_route_is_coverage_not_a_wallet_crash(self):
        control, router, guard = cohort()
        control.pop("market_route")
        result = evaluate_forward_composition([control, router, guard], [], starting_balance_usd=100,
            kelly_sizer=FixedKelly(), risk_policy=ApprovingRisk(), min_edge=0.01,
            min_confidence=0.5, max_entry_price=0.99)
        self.assertEqual(result["pairs"][0]["wallet_input_status"], "wallet_inputs_unavailable")
        self.assertEqual(result["combined_wallet"]["decision_rows"], [])

    def test_real_lane_producer_carries_paired_wallet_inputs_and_book_guard(self):
        from types import SimpleNamespace
        from bot.paper_shadow_lanes import write_paper_shadow_lane_decisions
        from bot.file_ops import load_jsonl
        with tempfile.TemporaryDirectory() as tmpdir:
            stamp = "2026-09-01T12:00:00+00:00"
            candidate = {"candidate_id": "c1", "market_id": "M1", "shared_snapshot_id": "snapshot-1",
                "observed_at": stamp, "prices": {"order_book_snapshot": {"source": "book",
                    "market_id": "M1", "observed_at": stamp, "data": {"best_yes_ask": 0.4}}}}
            signal = {"direction": "BUY_YES", "market_id": "M1", "question": "Will it happen?",
                "exchange": "kalshi", "candidate_observed_at": stamp, "confidence": 0.9,
                "model_probability": 0.8, "best_yes_ask": 0.4, "market_route": {"allowed": True,
                "handler_id": "weather"}, "event_ticker": "E1"}
            stable = {"shared_candidate_id": "c1", "decision_role": "paper_shadow", "wallet_id": "stable_paper",
                "run_id": "run-1", "candidate_dataset_path": str(Path(tmpdir) / "candidates.jsonl"),
                "action": "BUY_YES", "market_id": "M1", "observed_at": stamp,
                "entry_price": 0.4, "decision_id": "stable-1", "confidence": 0.9}
            result = write_paper_shadow_lane_decisions(config={"paper_shadow_lanes": {"enabled": True,
                "definitions_dir": str(Path(__file__).resolve().parent.parent / "lanes"),
                "enabled_lanes": ["control_stable", "shadow_fee_aware_loss_guard"]}},
                candidate_dataset_path=stable["candidate_dataset_path"],
                inputs_by_shared_candidate_id={"c1": {"stable_paper": SimpleNamespace(
                    signal=signal, shared_candidate=candidate)}},
                wallet_decision_rows={"stable_paper": [stable]},
                wallet_runs={"stable_paper": SimpleNamespace(session_id="run-1")}, ledger_root=tmpdir)
            emitted = {r["policy"]: r for r in load_jsonl(Path(result.decision_path))}
            control, guard = emitted["control_stable"], emitted["shadow_fee_aware_loss_guard"]
            self.assertEqual(control["market_route"]["handler_id"], "weather")
            self.assertEqual(control["question"], "Will it happen?")
            self.assertEqual(control["event_ticker"], "E1")
            self.assertEqual(guard["action"], "BUY_YES")
            router = {**control, "policy": "shadow_source_router", "action": "BUY_YES",
                      "provenance": {"source_router": {"direct_history": {"mode": "committed_index_direct_read"}}}}
            pairs, intents = compose_forward_rows([control, router, guard])
            self.assertEqual(pairs[0]["combined_action"], "BUY_YES")
            self.assertEqual(len(intents), 2)

    def test_mismatched_snapshot_or_time_cannot_open_and_missing_component_is_explicit(self):
        control, router, guard = cohort()
        for changed in ({**router, "shared_snapshot_id": "other"},
                        {**router, "observed_at": "2026-09-01T12:01:00+00:00"}):
            pair = compose_forward_rows([control, changed, guard])[0][0]
            self.assertEqual((pair["combined_action"], pair["reason_code"]), ("SKIP", "missing_router"))
        pair = compose_forward_rows([control])[0][0]
        self.assertEqual(pair["reason_code"], "missing_router")

    def test_duplicate_event_cash_later_settlement_void_and_fee_assumption(self):
        one = cohort()
        two = tuple({**row, "observed_at": "2026-09-01T12:01:00+00:00"} for row in cohort("c2", "M2", "E1"))
        three = tuple({**row, "observed_at": "2026-09-01T12:02:00+00:00"} for row in cohort("c3", "M1", "E1"))
        intents = [next(i for i in compose_forward_rows(list(rows))[1] if i["lane_id"] == "forward_composed_paper") for rows in (one, two, three)]
        result = evaluate_composed_intents(intents=intents, resolutions=[
            {"decision_id": intents[0]["decision_id"], "shared_candidate_id": "c1", "run_id": "run-1",
             "market_id": "M1", "outcome": "YES", "settlement_ts": "2026-09-01T13:00:00+00:00",
             "authoritative": True, "resolution_row_sha256": "a" * 64}],
            starting_balance_usd=100, kelly_sizer=FixedKelly(), risk_policy=ApprovingRisk(),
            min_edge=0.01, min_confidence=0.5, max_entry_price=0.99, fee_rate=0.07, event_cap_pct=0.10)
        self.assertEqual([r["status"] for r in result["decision_rows"]], ["opened", "skipped", "skipped"])
        self.assertEqual(result["decision_rows"][1]["reason_code"], "event_exposure_limit_reached")
        self.assertEqual(result["decision_rows"][2]["reason_code"], "duplicate_market_id_open")
        self.assertEqual(result["settlement_rows"][0]["pnl_usd"], 13.95)
        self.assertEqual(result["settlement_rows"][0]["pnl_basis"], "quote_proxy_not_fill")
        self.assertEqual(result["summary"]["final_balance_usd"], 113.95)

    def test_outcome_known_later_keeps_cash_reserved_and_settled_market_cannot_rebuy(self):
        first = next(i for i in compose_forward_rows(list(cohort()))[1] if i["lane_id"] == "forward_composed_paper")
        second = {**first, "decision_id": "later", "shared_candidate_id": "c2",
                  "observed_at": "2026-09-01T13:30:00+00:00"}
        receipt = {"decision_id": first["decision_id"], "shared_candidate_id": "c1", "run_id": "run-1",
                   "market_id": "M1", "outcome": "YES", "settlement_ts": "2026-09-01T13:00:00+00:00",
                   "outcome_known_at": "2026-09-01T14:00:00+00:00", "authoritative": True,
                   "resolution_row_sha256": "a" * 64}
        kwargs = dict(intents=[first, second], resolutions=[receipt], starting_balance_usd=100,
                      kelly_sizer=FixedKelly(), risk_policy=ApprovingRisk(), min_edge=0.01,
                      min_confidence=0.5, max_entry_price=0.99, fee_rate=0.07, event_cap_pct=0.10)
        result = evaluate_composed_intents(**kwargs)
        self.assertEqual(result["decision_rows"][1]["reason_code"], "duplicate_market_id_open")
        self.assertEqual(result["decision_rows"][1]["available_cash_before_usd"], 90)
        later = {**second, "observed_at": "2026-09-01T14:30:00+00:00"}
        result = evaluate_composed_intents(**{**kwargs, "intents": [first, later]})
        self.assertEqual(result["decision_rows"][1]["reason_code"], "market_already_settled")
        self.assertEqual(result["decision_rows"][1]["available_cash_before_usd"], 113.95)

    def test_outcome_like_input_is_rejected_before_decisions(self):
        control, router, guard = cohort()
        with self.assertRaisesRegex(ValueError, "outcome-like"):
            compose_forward_rows([control, router, {**guard, "provenance": {"nested": {"official_outcome": "YES"}}}])


if __name__ == "__main__":
    unittest.main()
