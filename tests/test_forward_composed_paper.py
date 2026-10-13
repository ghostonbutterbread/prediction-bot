"""The forward adapter consumes paired lane rows, never a resolved candidate."""
import unittest
import tempfile
import json
from pathlib import Path
from io import StringIO
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

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
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main([*args, "--enable-paper-observer", "--require-pre-risk-picker"]), 0)
            self.assertEqual(json.loads(output.getvalue())["reason_code"], "pre_risk_picker_lane_missing")
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
        required = observe_fresh_forward_snapshot(rows, [], state, enabled=True,
                         require_pre_risk_picker=True, now=now, max_snapshot_age_seconds=60, **settings)
        self.assertEqual(required["reason_code"], "pre_risk_picker_lane_missing")

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

    def test_recorded_pre_risk_buys_survive_wallet_risk_rejection_through_loader_lanes_and_observer(self):
        from bot.file_ops import load_jsonl
        from bot.paper_evaluator_input import load_shared_candidate_paper_inputs
        from bot.paper_shadow_lanes import write_paper_shadow_lane_decisions
        from bot.shared_market_feed import SCHEMA_NAME

        stamp = "2026-09-01T12:00:00+00:00"
        with tempfile.TemporaryDirectory() as tmpdir:
            dataset = Path(tmpdir) / "collector_candidates.jsonl"
            for candidate_id, market_id, final_action in (("c1", "M1", "BUY_YES"), ("c2", "M2", "SKIP"),
                                                          ("c3", "M3", "SKIP"), ("c4", "M4", "SKIP"),
                                                          ("c5", "M5", "SKIP"), ("c6", "M6", "SKIP"),
                                                          ("c7", "M7", "SKIP"), ("c8", "M8", "SKIP")):
                book_market = "OTHER" if candidate_id == "c4" else market_id
                book_time = "2026-09-01T12:01:00+00:00" if candidate_id == "c5" else stamp
                ask = 0.1 if candidate_id == "c7" else 0.4
                candidate = {"schema_name": SCHEMA_NAME, "candidate_id": candidate_id,
                    "shared_snapshot_id": "snapshot-1", "run_id": "run-1", "market_id": market_id,
                    "observed_at": stamp, "market": {"id": market_id, "question": "Will it happen?",
                    "exchange": "kalshi", "event_ticker": market_id,
                    "route": {"allowed": True, "handler_id": "weather"}},
                    "prices": {"order_book_snapshot": {"source": "book", "market_id": book_market,
                    "observed_at": book_time, "data": {"best_yes_ask": ask}}, "best_yes_ask": ask},
                    "decision": {"direction": final_action}}
                row = {"shared_candidate_id": candidate_id, "shared_snapshot_id": "snapshot-1",
                    "run_id": "run-1", "market_id": market_id, "observed_at": stamp,
                    "direction": final_action, "confidence": 0.9, "model_probability": 0.8,
                    "shared_candidate": candidate,
                    "decision_artifact": {"final_action": final_action,
                    "execution_snapshot": {"estimated_fill_price": 0.55} if candidate_id == "c8" else {},
                    "strategy_trace": {"direction": "BUY_YES"},
                    "strategy_signal": None if candidate_id == "c3" else (
                        {"direction": "SKIP", "market_id": market_id,
                         "recorded_pre_risk_picker": {"action": "BUY_YES", "market_id": market_id,
                             "source": "decision_artifact.strategy_signal", "confidence": 0.9,
                             "model_probability": 0.8}}
                        if candidate_id == "c6" else {"direction": "BUY_YES", "market_id": market_id,
                        "model_probability": 0.51 if candidate_id == "c8" else 0.8,
                        "confidence": 0.9, "question": "Will it happen?",
                        "exchange": "kalshi", "market_route": {"allowed": True, "handler_id": "weather"}})}}
                with dataset.open("a") as handle:
                    handle.write(json.dumps(row) + "\n")
            loaded = load_shared_candidate_paper_inputs(dataset, config={}, data_dir=Path(tmpdir) / "wallets",
                wallet_ids=["stable_paper"])
            self.assertEqual(loaded.accepted_candidate_count, 8)
            stable_rows = [{"shared_candidate_id": cid, "wallet_id": "stable_paper",
                "run_id": "stable-run", "candidate_dataset_path": str(dataset),
                "decision_role": "paper_shadow", "decision_id": "stable-" + cid,
                "market_id": mid, "observed_at": stamp, "action": action,
                "entry_price": 0.4 if action == "BUY_YES" else None,
                "reason_code": "approved" if action == "BUY_YES" else "risk_rejected"}
                for cid, mid, action in (("c1", "M1", "BUY_YES"), ("c2", "M2", "SKIP"),
                                         ("c3", "M3", "SKIP"), ("c4", "M4", "SKIP"),
                                         ("c5", "M5", "SKIP"), ("c6", "M6", "SKIP"), ("c7", "M7", "SKIP"),
                                         ("c8", "M8", "SKIP"))]
            config = {"paper_shadow_lanes": {"enabled": True,
                "definitions_dir": str(Path(__file__).resolve().parents[1] / "lanes"),
                "enabled_lanes": ["control_stable", "shadow_pre_risk_picker",
                    "shadow_source_router", "shadow_pre_risk_fee_aware_loss_guard"],
                "shadow_source_router": {"parameters": {"collector_replay_index_path": "fixture-index",
                    "collector_replay_manifest_path": "fixture-manifest",
                    "strict_resolutions_path": "fixture-resolutions"}}}}
            direct = SimpleNamespace(scorecard_rows=[], bucket_coverage={}, counts={},
                index_manifest_sha256="index", resolution_sha256="resolution", archive_receipts=[])
            with patch("bot.weather.source_router_direct_history.load_direct_strict_source_history", return_value=direct), \
                 patch("bot.weather.source_confidence.build_source_confidence_row", return_value={"source_direction": "YES"}):
                written = write_paper_shadow_lane_decisions(config=config, candidate_dataset_path=dataset,
                    inputs_by_shared_candidate_id=loaded.inputs_by_shared_candidate_id,
                    wallet_decision_rows={"stable_paper": stable_rows},
                    wallet_runs={"stable_paper": SimpleNamespace(session_id="stable-run")}, ledger_root=tmpdir)
            rows = load_jsonl(Path(written.decision_path))
            by_candidate = {(r["shared_candidate_id"], r["policy"]): r for r in rows}
            self.assertEqual(len(rows), 32)
            self.assertEqual(by_candidate["c2", "control_stable"]["action"], "SKIP")
            self.assertEqual(by_candidate["c2", "shadow_pre_risk_picker"]["action"], "BUY_YES")
            self.assertEqual(by_candidate["c2", "shadow_source_router"]["action"], "BUY_YES")
            self.assertEqual(by_candidate["c2", "shadow_pre_risk_fee_aware_loss_guard"]["action"], "BUY_YES")
            self.assertEqual(by_candidate["c8", "shadow_pre_risk_picker"]["action"], "BUY_YES")
            self.assertEqual(by_candidate["c8", "shadow_pre_risk_fee_aware_loss_guard"]["action"], "SKIP")
            self.assertEqual(by_candidate["c8", "shadow_pre_risk_fee_aware_loss_guard"]["reason_code"],
                             "fee_aware_edge_below_floor")
            self.assertEqual(by_candidate["c8", "shadow_pre_risk_fee_aware_loss_guard"]["provenance"]["payout_aware"]["entry_price"], 0.55)
            self.assertEqual(by_candidate["c3", "shadow_pre_risk_picker"]["action"], "SKIP")
            self.assertEqual(by_candidate["c6", "shadow_pre_risk_picker"]["action"], "SKIP")
            for cid in ("c4", "c5"):
                self.assertEqual(by_candidate[cid, "shadow_pre_risk_fee_aware_loss_guard"]["reason_code"],
                    "executable_side_ask_unavailable")
                self.assertIsNone(by_candidate[cid, "shadow_pre_risk_fee_aware_loss_guard"].get("entry_price"))
            from datetime import datetime, timezone
            state = {"publisher": {"runtime_kind": "collector", "instance_id": "primary"},
                     "latest_snapshot": {"snapshot_id": "snapshot-1", "publisher_runtime": "collector",
                         "publisher_instance_id": "primary", "published_at": stamp}}
            settings = dict(starting_balance_usd=100, kelly_sizer=FixedKelly(), risk_policy=ApprovingRisk(),
                min_edge=0.01, min_confidence=0.5, max_entry_price=0.99)
            now = datetime(2026, 9, 1, 12, 0, 30, tzinfo=timezone.utc)
            self.assertEqual(observe_fresh_forward_snapshot(rows, [], state, now=now, **settings)["reason_code"],
                "observer_disabled")
            self.assertEqual(by_candidate["c7", "shadow_pre_risk_fee_aware_loss_guard"]["reason_code"],
                "executable_side_ask_below_floor")
            result = observe_fresh_forward_snapshot(rows, [], state, enabled=True, now=now, **settings)
            pairs = {r["shared_candidate_id"]: r for r in result["pairs"]}
            self.assertEqual({cid: p["combined_action"] for cid, p in pairs.items()},
                {"c1": "BUY_YES", "c2": "BUY_YES", "c3": "SKIP", "c4": "SKIP", "c5": "SKIP", "c6": "SKIP", "c7": "SKIP", "c8": "SKIP"})
            self.assertEqual(pairs["c2"]["stable_wallet_action"], "SKIP")
            self.assertEqual(pairs["c3"]["reason_code"], "picker_intent_not_recorded")
            self.assertEqual(result["control_wallet"]["summary"]["opened_positions"], 4,
                             result["control_wallet"]["decision_rows"])
            self.assertEqual(next(p for p in result["control_wallet"]["open_positions"] if p["market_id"] == "M8")["entry_price"], 0.4)
            self.assertEqual(next(p for p in result["control_wallet"]["open_positions"] if p["market_id"] == "M7")["entry_price"], 0.1)
            self.assertEqual(result["combined_wallet"]["summary"]["opened_positions"], 2)
            self.assertEqual(result["candidate_universe"], "recorded_pre_risk_picker_intents")
            incomplete = evaluate_forward_composition(
                [row for row in rows if not (row["shared_candidate_id"] == "c2" and row["policy"] == "shadow_pre_risk_picker")],
                [], starting_balance_usd=100, kelly_sizer=FixedKelly(), risk_policy=ApprovingRisk(),
                min_edge=0.01, min_confidence=0.5, max_entry_price=0.99)
            self.assertEqual(next(p for p in incomplete["pairs"] if p["shared_candidate_id"] == "c2")["reason_code"],
                "missing_picker_intent")
            self.assertEqual(incomplete["control_wallet"]["summary"]["opened_positions"], 3)
            required = observe_fresh_forward_snapshot(
                [row for row in rows if not (row["shared_candidate_id"] == "c2" and row["policy"] == "shadow_pre_risk_picker")],
                [], state, enabled=True, require_pre_risk_picker=True, now=now, **settings)
            self.assertEqual(required["reason_code"], "pre_risk_picker_lane_incomplete")

    def test_outcome_like_input_is_rejected_before_decisions(self):
        control, router, guard = cohort()
        with self.assertRaisesRegex(ValueError, "outcome-like"):
            compose_forward_rows([control, router, {**guard, "provenance": {"nested": {"official_outcome": "YES"}}}])


if __name__ == "__main__":
    unittest.main()
