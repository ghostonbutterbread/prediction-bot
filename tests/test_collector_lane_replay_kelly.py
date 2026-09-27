import json
from itertools import permutations
import tempfile
import unittest
from pathlib import Path

from bot.collector_lane_replay_kelly import (
    replay_recorded_collector_lane_with_kelly,
    write_collector_lane_replay_kelly,
)
from scripts.collector_lane_replay_kelly import main as kelly_replay_main


class CollectorLaneReplayKellyTests(unittest.TestCase):
    def test_replays_recorded_decisions_chronologically_with_reserved_capital_and_later_settlement(self):
        result = replay_recorded_collector_lane_with_kelly(
            decisions=[
                {
                    "decision_id": "d1",
                    "market_id": "KXONE",
                    "observed_at": "2026-07-01T12:00:00+00:00",
                    "action": "BUY_YES",
                    "model_probability": 0.70,
                    "yes_price": 0.50,
                },
                {
                    "decision_id": "d2",
                    "market_id": "KXTWO",
                    "observed_at": "2026-07-01T13:00:00+00:00",
                    "action": "BUY_NO",
                    "model_probability": 0.30,
                    "no_price": 0.50,
                },
                {
                    "decision_id": "d3",
                    "market_id": "KXTHREE",
                    "observed_at": "2026-07-01T14:00:00+00:00",
                    "action": "BUY_YES",
                    "model_probability": 0.70,
                    "yes_price": 0.50,
                },
            ],
            resolutions=[
                {
                    "decision_id": "d1",
                    "market_id": "KXONE",
                    "outcome": "YES",
                    "settlement_ts": "2026-07-01T13:30:00+00:00",
                },
                {
                    "decision_id": "d2",
                    "market_id": "KXTWO",
                    "outcome": "YES",
                    "settlement_ts": "2026-07-01T15:00:00+00:00",
                },
                {
                    "decision_id": "d3",
                    "market_id": "KXTHREE",
                    "outcome": "YES",
                    "settlement_ts": "2026-07-01T16:00:00+00:00",
                },
            ],
            starting_balance_usd=100.0,
            kelly_fraction=0.5,
            max_bet_pct=0.10,
        )

        self.assertEqual(result["schema_name"], "collector_lane_replay_kelly")
        self.assertEqual(result["methodology"], "recorded_decision_sequential_synthetic_kelly_capacity_diagnostic")
        self.assertTrue(result["non_mutating"])
        self.assertFalse(result["paper_parity_claim"])
        self.assertEqual([row["decision_id"] for row in result["decision_rows"]], ["d1", "d2", "d3"])
        self.assertEqual([row["status"] for row in result["decision_rows"]], ["opened", "opened", "opened"])
        self.assertAlmostEqual(result["decision_rows"][0]["approved_stake_usd"], 10.0)
        self.assertAlmostEqual(result["decision_rows"][1]["available_cash_before_usd"], 90.0)
        self.assertAlmostEqual(result["decision_rows"][2]["available_cash_before_usd"], 101.0)
        self.assertAlmostEqual(result["summary"]["final_balance_usd"], 111.1)
        self.assertEqual(result["summary"]["settled_positions"], 3)
        self.assertEqual(result["summary"]["open_positions"], 0)

    def test_uses_complement_probability_for_recorded_buy_no(self):
        result = replay_recorded_collector_lane_with_kelly(
            decisions=[{
                "decision_id": "d-no", "market_id": "KXNO", "observed_at": "2026-07-01T12:00:00+00:00",
                "action": "BUY_NO", "model_probability": 0.30, "no_price": 0.50,
            }],
            resolutions=[],
            starting_balance_usd=100.0,
        )

        self.assertEqual(result["decision_rows"][0]["status"], "opened")
        self.assertAlmostEqual(result["decision_rows"][0]["approved_stake_usd"], 10.0)

    def test_subnormal_price_cannot_settle_infinite_synthetic_payout(self):
        result = replay_recorded_collector_lane_with_kelly(
            decisions=[{
                "decision_id": "tiny-quote", "market_id": "M1", "observed_at": "2026-07-01T12:00:00+00:00",
                "action": "BUY_NO", "model_probability": .2, "no_price": 5e-324,
            }],
            resolutions=[{
                "decision_id": "tiny-quote", "market_id": "M1", "outcome": "NO",
                "settlement_ts": "2026-07-02T12:00:00+00:00",
            }],
            starting_balance_usd=100.0,
        )
        self.assertEqual(result["decision_rows"][0]["reason_code"], "unrepresentable_economics")
        self.assertEqual(result["summary"]["opened_positions"], 0)
        self.assertEqual(result["summary"]["settled_positions"], 0)
        self.assertEqual(result["summary"]["final_balance_usd"], 100.0)

    def test_void_blocker_cannot_authorize_nonvoid_settlement(self):
        result = replay_recorded_collector_lane_with_kelly(
            decisions=[{
                "decision_id": "d1", "market_id": "M1", "observed_at": "2026-07-01T12:00:00+00:00",
                "action": "BUY_YES", "model_probability": .7, "yes_price": .5,
            }],
            resolutions=[{
                "lane_decision_id": "d1", "shared_candidate_id": "candidate-1", "run_id": "run-1",
                "market_id": "M1", "blocker": "void_resolution",
                "resolution": {"matched": True, "matched_by": "shared_candidate_id", "outcome": "YES",
                    "resolution_row_id": "resolution-1", "settlement_ts": "2026-07-02T12:00:00+00:00"},
            }],
        )
        self.assertEqual(result["summary"]["settled_positions"], 0)

    def test_consumes_existing_lane_decision_and_exact_resolution_receipt(self):
        result = replay_recorded_collector_lane_with_kelly(
            decisions=[{
                "decision_id": "lane-1", "shared_candidate_id": "candidate-1", "run_id": "run-1",
                "market_id": "KXONE", "observed_at": "2026-07-01T12:00:00+00:00",
                "action": "BUY_YES", "model_probability": 0.70, "entry_price": 0.50,
            }],
            resolutions=[{
                "lane_decision_id": "lane-1", "shared_candidate_id": "candidate-1", "run_id": "run-1",
                "market_id": "KXONE",
                "resolution": {
                    "matched": True, "matched_by": "shared_candidate_id", "outcome": "YES",
                    "settlement_ts": "2026-07-01T13:00:00+00:00", "resolution_row_id": "resolution-1",
                },
            }],
            starting_balance_usd=100.0,
        )

        self.assertEqual(result["summary"]["opened_positions"], 1)
        self.assertEqual(result["summary"]["settled_positions"], 1)
        self.assertAlmostEqual(result["summary"]["final_balance_usd"], 110.0)
        self.assertEqual(result["settlement_rows"][0]["resolution_receipt"]["resolution_row_id"], "resolution-1")

    def test_rejects_nested_receipt_missing_candidate_or_run_identity(self):
        result = replay_recorded_collector_lane_with_kelly(
            decisions=[{
                "decision_id": "lane-1", "market_id": "KXONE", "observed_at": "2026-07-01T12:00:00+00:00",
                "action": "BUY_YES", "model_probability": 0.70, "entry_price": 0.50,
            }],
            resolutions=[{
                "lane_decision_id": "lane-1", "market_id": "KXONE",
                "resolution": {
                    "matched": True, "matched_by": "shared_candidate_id", "outcome": "YES",
                    "settlement_ts": "2026-07-01T13:00:00+00:00", "resolution_row_id": "resolution-1",
                },
            }],
            starting_balance_usd=100.0,
        )

        self.assertEqual(result["summary"]["settled_positions"], 0)
        self.assertEqual(result["summary"]["open_positions"], 1)

    def test_nested_exact_receipt_cannot_supply_missing_decision_identity(self):
        receipt = {
            "lane_decision_id": "lane-1", "shared_candidate_id": "candidate-1", "run_id": "run-1",
            "market_id": "KXONE", "resolution": {
                "matched": True, "matched_by": "shared_candidate_id", "outcome": "YES",
                "settlement_ts": "2026-07-01T13:00:00+00:00", "resolution_row_id": "resolution-1",
            },
        }
        for missing in ("shared_candidate_id", "run_id", "both"):
            with self.subTest(missing=missing):
                decision = {
                    "decision_id": "lane-1", "shared_candidate_id": "candidate-1", "run_id": "run-1",
                    "market_id": "KXONE", "observed_at": "2026-07-01T12:00:00+00:00",
                    "action": "BUY_YES", "model_probability": 0.70, "entry_price": 0.50,
                }
                for field in (("shared_candidate_id", "run_id") if missing == "both" else (missing,)):
                    decision.pop(field)
                result = replay_recorded_collector_lane_with_kelly(
                    decisions=[decision], resolutions=[receipt], starting_balance_usd=100.0,
                )
                self.assertEqual(result["settlement_rows"], [])
                self.assertEqual(result["summary"]["ambiguous_resolution_decisions"], 0)
                self.assertEqual(result["summary"]["reserved_capital_usd"], 10.0)
                self.assertEqual(result["summary"]["final_balance_usd"], 100.0)

    def test_conflicting_exact_receipts_leave_synthetic_position_pending_in_either_order(self):
        decision = {
            "decision_id": "lane-1", "shared_candidate_id": "candidate-1", "run_id": "run-1",
            "market_id": "KXONE", "observed_at": "2026-07-01T12:00:00+00:00",
            "action": "BUY_YES", "model_probability": 0.70, "entry_price": 0.50,
        }
        receipts = [{
            "lane_decision_id": "lane-1", "shared_candidate_id": "candidate-1", "run_id": "run-1",
            "market_id": "KXONE", "resolution": {
                "matched": True, "matched_by": "shared_candidate_id", "outcome": outcome,
                "settlement_ts": "2026-07-01T13:00:00+00:00", "resolution_row_id": f"receipt-{outcome}",
            },
        } for outcome in ("YES", "NO")]

        for ordered_receipts in (receipts, list(reversed(receipts))):
            with self.subTest(first=ordered_receipts[0]["resolution"]["outcome"]):
                result = replay_recorded_collector_lane_with_kelly(
                    decisions=[decision], resolutions=ordered_receipts, starting_balance_usd=100.0,
                )
                self.assertEqual(result["settlement_rows"], [])
                self.assertEqual(result["summary"]["ambiguous_resolution_decisions"], 1)
                self.assertEqual(result["summary"]["open_positions"], 1)
                self.assertEqual(result["open_positions"][0]["resolution_blocker"], "conflicting_resolution_receipts")
                self.assertAlmostEqual(result["summary"]["final_balance_usd"], 100.0)

    def test_nonfinal_resolution_outcome_cannot_book_synthetic_loss(self):
        result = replay_recorded_collector_lane_with_kelly(
            decisions=[{
                "decision_id": "d1", "market_id": "KXONE", "observed_at": "2026-07-01T12:00:00+00:00",
                "action": "BUY_YES", "model_probability": 0.70, "yes_price": 0.50,
            }],
            resolutions=[{
                "decision_id": "d1", "market_id": "KXONE", "outcome": "PENDING",
                "settlement_ts": "2026-07-01T13:00:00+00:00",
            }],
            starting_balance_usd=100.0,
        )
        self.assertEqual(result["settlement_rows"], [])
        self.assertEqual(result["summary"]["open_positions"], 1)
        self.assertAlmostEqual(result["summary"]["final_balance_usd"], 100.0)

    def test_explicit_active_market_status_cannot_settle_synthetic_position(self):
        result = replay_recorded_collector_lane_with_kelly(
            decisions=[{
                "decision_id": "d1", "market_id": "KXONE", "observed_at": "2026-07-01T12:00:00+00:00",
                "action": "BUY_YES", "model_probability": 0.70, "yes_price": 0.50,
            }],
            resolutions=[{
                "decision_id": "d1", "market_id": "KXONE", "outcome": "YES",
                "market_status": "active", "settlement_ts": "2026-07-01T13:00:00+00:00",
            }],
            starting_balance_usd=100.0,
        )
        self.assertEqual(result["settlement_rows"], [])
        self.assertEqual(result["summary"]["open_positions"], 1)

    def test_disagreeing_nested_market_identity_cannot_settle_another_market(self):
        result = replay_recorded_collector_lane_with_kelly(
            decisions=[{
                "decision_id": "lane-1", "shared_candidate_id": "candidate-1", "run_id": "run-1",
                "market_id": "KXONE", "observed_at": "2026-07-01T12:00:00+00:00",
                "action": "BUY_YES", "model_probability": 0.70, "entry_price": 0.50,
            }],
            resolutions=[{
                "lane_decision_id": "lane-1", "shared_candidate_id": "candidate-1", "run_id": "run-1",
                "market_id": "KXONE", "resolution": {
                    "market_id": "KXTWO", "matched": True, "matched_by": "shared_candidate_id",
                    "outcome": "YES", "settlement_ts": "2026-07-01T13:00:00+00:00",
                    "resolution_row_id": "receipt-1",
                },
            }],
        )
        self.assertEqual(result["settlement_rows"], [])
        self.assertEqual(result["summary"]["open_positions"], 1)

    def test_disagreeing_nested_outcome_cannot_settle_synthetic_position(self):
        result = replay_recorded_collector_lane_with_kelly(
            decisions=[{
                "decision_id": "lane-1", "shared_candidate_id": "candidate-1", "run_id": "run-1",
                "market_id": "KXONE", "observed_at": "2026-07-01T12:00:00+00:00",
                "action": "BUY_YES", "model_probability": 0.70, "entry_price": 0.50,
            }],
            resolutions=[{
                "lane_decision_id": "lane-1", "shared_candidate_id": "candidate-1", "run_id": "run-1",
                "market_id": "KXONE", "outcome": "YES", "resolution": {
                    "matched": True, "matched_by": "shared_candidate_id", "outcome": "NO",
                    "settlement_ts": "2026-07-01T13:00:00+00:00", "resolution_row_id": "receipt-1",
                },
            }],
        )
        self.assertEqual(result["settlement_rows"], [])
        self.assertEqual(result["summary"]["open_positions"], 1)

    def test_equivalent_duplicate_receipts_settle_once_independent_of_input_order(self):
        decision = {
            "decision_id": "lane-1", "shared_candidate_id": "candidate-1", "run_id": "run-1",
            "market_id": "KXONE", "observed_at": "2026-07-01T12:00:00+00:00",
            "action": "BUY_YES", "model_probability": 0.70, "entry_price": 0.50,
        }
        receipts = [{
            "lane_decision_id": "lane-1", "shared_candidate_id": "candidate-1", "run_id": "run-1",
            "market_id": "KXONE", "market_status": "finalized",
            "outcome_known_at": known_at, "resolution": {
                "matched": True, "matched_by": "shared_candidate_id", "outcome": "YES",
                "settlement_ts": "2026-07-01T13:00:00+00:00", "resolution_row_id": receipt_id,
            },
        } for receipt_id, known_at in (
            ("receipt-b", "2026-07-01T14:00:00+00:00"),
            ("receipt-a", "2026-07-01T13:30:00+00:00"),
        )]
        forward = replay_recorded_collector_lane_with_kelly(decisions=[decision], resolutions=receipts)
        reversed_result = replay_recorded_collector_lane_with_kelly(decisions=[decision], resolutions=reversed(receipts))
        self.assertEqual(forward["settlement_rows"], reversed_result["settlement_rows"])
        self.assertEqual(forward["summary"]["settled_positions"], 1)
        self.assertEqual(forward["summary"]["ambiguous_resolution_decisions"], 0)
        self.assertEqual(forward["settlement_rows"][0]["available_at"], "2026-07-01T14:00:00+00:00")
        self.assertAlmostEqual(forward["summary"]["final_balance_usd"], 110.0)

    def test_disagreeing_nested_settlement_time_cannot_move_wallet_chronology(self):
        result = replay_recorded_collector_lane_with_kelly(
            decisions=[{
                "decision_id": "lane-1", "shared_candidate_id": "candidate-1", "run_id": "run-1",
                "market_id": "KXONE", "observed_at": "2026-07-01T12:00:00+00:00",
                "action": "BUY_YES", "model_probability": 0.70, "entry_price": 0.50,
            }],
            resolutions=[{
                "lane_decision_id": "lane-1", "shared_candidate_id": "candidate-1", "run_id": "run-1",
                "market_id": "KXONE", "settlement_ts": "2026-07-01T14:00:00+00:00",
                "resolution": {
                    "matched": True, "matched_by": "shared_candidate_id", "outcome": "YES",
                    "settlement_ts": "2026-07-01T13:00:00+00:00", "resolution_row_id": "receipt-1",
                },
            }],
        )
        self.assertEqual(result["settlement_rows"], [])
        self.assertEqual(result["summary"]["open_positions"], 1)

    def test_conflicting_settlement_times_cannot_change_intermediate_wallet_cash(self):
        decision = {
            "decision_id": "lane-1", "shared_candidate_id": "candidate-1", "run_id": "run-1",
            "market_id": "KXONE", "observed_at": "2026-07-01T12:00:00+00:00",
            "action": "BUY_YES", "model_probability": 0.70, "entry_price": 0.50,
        }
        receipts = [{
            "lane_decision_id": "lane-1", "shared_candidate_id": "candidate-1", "run_id": "run-1",
            "market_id": "KXONE", "resolution": {
                "matched": True, "matched_by": "shared_candidate_id", "outcome": "YES",
                "settlement_ts": settlement_ts, "resolution_row_id": f"receipt-{index}",
            },
        } for index, settlement_ts in enumerate(("2026-07-01T13:00:00+00:00", "2026-07-01T14:00:00+00:00"))]
        for ordered_receipts in (receipts, list(reversed(receipts))):
            result = replay_recorded_collector_lane_with_kelly(decisions=[decision], resolutions=ordered_receipts)
            self.assertEqual(result["settlement_rows"], [])
            self.assertEqual(result["summary"]["ambiguous_resolution_decisions"], 1)
            self.assertAlmostEqual(result["summary"]["final_balance_usd"], 100.0)

    def test_predecision_receipt_does_not_conflict_with_later_valid_authority(self):
        decision = {
            "decision_id": "d1", "market_id": "KXONE", "observed_at": "2026-07-01T12:00:00+00:00",
            "action": "BUY_YES", "model_probability": 0.70, "yes_price": 0.50,
        }
        early = {
            "decision_id": "d1", "market_id": "KXONE", "outcome": "NO",
            "settlement_ts": "2026-07-01T11:00:00+00:00",
        }
        later = {
            "decision_id": "d1", "market_id": "KXONE", "outcome": "YES",
            "settlement_ts": "2026-07-01T13:00:00+00:00",
        }
        for receipts in ([early, later], [later, early]):
            result = replay_recorded_collector_lane_with_kelly(
                decisions=[decision], resolutions=receipts, starting_balance_usd=100.0,
            )
            self.assertEqual(result["summary"]["ambiguous_resolution_decisions"], 0)
            self.assertEqual(result["summary"]["settled_positions"], 1)
            self.assertEqual(result["summary"]["final_balance_usd"], 110.0)

    def test_legacy_flat_duplicate_does_not_block_matching_exact_receipt(self):
        decision = {
            "decision_id": "lane-1", "shared_candidate_id": "candidate-1", "run_id": "run-1",
            "market_id": "KXONE", "observed_at": "2026-07-01T12:00:00+00:00",
            "action": "BUY_YES", "model_probability": 0.70, "entry_price": 0.50,
        }
        flat = {
            "decision_id": "lane-1", "market_id": "KXONE", "outcome": "YES",
            "settlement_ts": "2026-07-01T13:00:00+00:00",
        }
        exact = {
            "lane_decision_id": "lane-1", "shared_candidate_id": "candidate-1", "run_id": "run-1",
            "market_id": "KXONE", "resolution": {
                "matched": True, "matched_by": "shared_candidate_id", "outcome": "YES",
                "settlement_ts": "2026-07-01T13:00:00+00:00", "resolution_row_id": "receipt-exact",
            },
        }
        for receipts in ([flat, exact], [exact, flat]):
            result = replay_recorded_collector_lane_with_kelly(decisions=[decision], resolutions=receipts)
            self.assertEqual(result["summary"]["settled_positions"], 1)
            self.assertEqual(result["summary"]["ambiguous_resolution_decisions"], 0)
            self.assertEqual(result["settlement_rows"][0]["resolution_receipt"]["resolution_row_id"], "receipt-exact")

    def test_mismatched_partial_identity_cannot_change_exact_receipt_by_order(self):
        decision = {
            "decision_id": "lane-1", "shared_candidate_id": "candidate-1", "run_id": "run-1",
            "market_id": "KXONE", "observed_at": "2026-07-01T12:00:00+00:00",
            "action": "BUY_YES", "model_probability": 0.70, "entry_price": 0.50,
        }
        receipts = [
            {"decision_id": "lane-1", "market_id": "KXONE", "shared_candidate_id": "candidate-1",
             "outcome": "YES", "settlement_ts": "2026-07-01T13:00:00+00:00"},
            {"decision_id": "lane-1", "market_id": "KXONE", "run_id": "run-2",
             "outcome": "YES", "settlement_ts": "2026-07-01T13:00:00+00:00"},
            {"lane_decision_id": "lane-1", "market_id": "KXONE",
             "shared_candidate_id": "candidate-1", "run_id": "run-1", "resolution": {
                 "matched": True, "matched_by": "shared_candidate_id", "outcome": "YES",
                 "settlement_ts": "2026-07-01T13:00:00+00:00", "resolution_row_id": "receipt-exact",
             }},
        ]
        for ordered in permutations(receipts):
            with self.subTest(order=[r.get("run_id") for r in ordered]):
                result = replay_recorded_collector_lane_with_kelly(
                    decisions=[decision], resolutions=ordered, starting_balance_usd=100.0,
                )
                self.assertEqual(result["summary"]["settled_positions"], 1)
                self.assertEqual(result["summary"]["ambiguous_resolution_decisions"], 0)
                self.assertEqual(result["settlement_rows"][0]["resolution_receipt"]["resolution_row_id"], "receipt-exact")
                self.assertEqual(result["summary"]["final_balance_usd"], 110.0)

    def test_wrong_market_receipt_cannot_suppress_valid_exact_market_authority(self):
        decisions = [{
            "decision_id": "lane-1", "shared_candidate_id": "candidate-1", "run_id": "run-1",
            "market_id": "M1", "observed_at": "2026-07-01T12:00:00+00:00",
            "action": "BUY_YES", "model_probability": 0.70, "entry_price": 0.50,
        }, {
            "decision_id": "lane-2", "market_id": "M3",
            "observed_at": "2026-07-01T13:30:00+00:00", "action": "SKIP",
        }]
        def receipt(market_id, outcome, receipt_id):
            return {
                "lane_decision_id": "lane-1", "shared_candidate_id": "candidate-1", "run_id": "run-1",
                "market_id": market_id, "resolution": {
                    "matched": True, "matched_by": "shared_candidate_id", "outcome": outcome,
                    "settlement_ts": "2026-07-01T13:00:00+00:00", "resolution_row_id": receipt_id,
                },
            }
        exact = receipt("M1", "YES", "receipt-exact")
        wrong_market = receipt("M2", "NO", "receipt-other-market")
        duplicate = {"decision_id": "lane-1", "market_id": "M1", "outcome": "YES",
                     "settlement_ts": "2026-07-01T13:00:00+00:00"}
        for ordered in permutations((exact, wrong_market, duplicate)):
            with self.subTest(order=[r.get("market_id") for r in ordered]):
                result = replay_recorded_collector_lane_with_kelly(
                    decisions=decisions, resolutions=ordered, starting_balance_usd=100.0,
                )
                self.assertEqual(result["summary"]["settled_positions"], 1)
                self.assertEqual(result["summary"]["ambiguous_resolution_decisions"], 0)
                self.assertEqual(result["settlement_rows"][0]["resolution_receipt"]["resolution_row_id"], "receipt-exact")
                self.assertEqual(result["decision_rows"][1]["available_cash_before_usd"], 110.0)

    def test_conflicting_decision_id_aliases_cannot_settle_synthetic_pnl(self):
        decision = {
            "decision_id": "d1", "market_id": "KXONE", "observed_at": "2026-07-01T12:00:00+00:00",
            "action": "BUY_YES", "model_probability": 0.70, "yes_price": 0.50,
        }
        contradictory = {
            "decision_id": "d1", "lane_decision_id": "other", "market_id": "KXONE",
            "outcome": "YES", "settlement_ts": "2026-07-01T13:00:00+00:00",
        }
        result = replay_recorded_collector_lane_with_kelly(
            decisions=[decision], resolutions=[contradictory], starting_balance_usd=100.0,
        )
        self.assertEqual(result["settlement_rows"], [])
        self.assertEqual(result["summary"]["ambiguous_resolution_decisions"], 0)
        self.assertEqual(result["summary"]["reserved_capital_usd"], 10.0)
        self.assertEqual(result["summary"]["final_balance_usd"], 100.0)
        valid = {key: value for key, value in contradictory.items() if key != "lane_decision_id"}
        for receipts in ([contradictory, valid], [valid, contradictory]):
            result = replay_recorded_collector_lane_with_kelly(decisions=[decision], resolutions=receipts)
            self.assertEqual(result["summary"]["settled_positions"], 1)
            self.assertEqual(result["summary"]["ambiguous_resolution_decisions"], 0)

    def test_void_releases_reserved_capital_without_booking_pnl(self):
        result = replay_recorded_collector_lane_with_kelly(
            decisions=[{
                "decision_id": "d1", "market_id": "KXONE", "observed_at": "2026-07-01T12:00:00+00:00",
                "action": "BUY_YES", "model_probability": 0.70, "yes_price": 0.50,
            }],
            resolutions=[{
                "decision_id": "d1", "market_id": "KXONE", "outcome": "VOID",
                "settlement_ts": "2026-07-01T13:00:00+00:00",
            }],
            starting_balance_usd=100.0,
        )

        self.assertEqual(result["summary"]["void_positions"], 1)
        self.assertAlmostEqual(result["summary"]["final_balance_usd"], 100.0)
        self.assertIsNone(result["settlement_rows"][0]["pnl_usd"])

    def test_outcome_known_at_alone_is_not_a_settlement_timestamp(self):
        result = replay_recorded_collector_lane_with_kelly(
            decisions=[{
                "decision_id": "d1", "market_id": "KXONE", "observed_at": "2026-07-01T12:00:00+00:00",
                "action": "BUY_YES", "model_probability": 0.70, "yes_price": 0.50,
            }],
            resolutions=[{
                "decision_id": "d1", "market_id": "KXONE", "outcome": "YES",
                "outcome_known_at": "2026-07-01T13:00:00+00:00",
            }],
            starting_balance_usd=100.0,
        )
        self.assertEqual(result["settlement_rows"], [])
        self.assertEqual(result["summary"]["open_positions"], 1)
        self.assertAlmostEqual(result["summary"]["final_balance_usd"], 100.0)

    def test_secondary_outcome_clock_before_decision_cannot_settle(self):
        decision = {
            "decision_id": "d1", "market_id": "KXONE", "observed_at": "2026-07-01T12:00:00+00:00",
            "action": "BUY_YES", "model_probability": 0.70, "yes_price": 0.50,
        }
        receipt = {
            "decision_id": "d1", "market_id": "KXONE", "outcome": "YES",
            "settlement_ts": "2026-07-01T13:00:00+00:00",
            "outcome_known_at": "2026-07-01T11:00:00+00:00",
        }
        result = replay_recorded_collector_lane_with_kelly(
            decisions=[decision], resolutions=[receipt], starting_balance_usd=100.0,
        )
        self.assertEqual(result["settlement_rows"], [])
        self.assertEqual(result["summary"]["ambiguous_resolution_decisions"], 0)
        self.assertEqual(result["summary"]["reserved_capital_usd"], 10.0)
        self.assertEqual(result["summary"]["final_balance_usd"], 100.0)

    def test_later_outcome_known_time_delays_synthetic_capital_release(self):
        first = {
            "decision_id": "d1", "market_id": "KXONE", "observed_at": "2026-07-01T12:00:00+00:00",
            "action": "BUY_YES", "model_probability": 0.70, "yes_price": 0.50,
        }
        second = {
            "decision_id": "d2", "market_id": "KXTWO", "observed_at": "2026-07-01T13:30:00+00:00",
            "action": "BUY_YES", "model_probability": 0.70, "yes_price": 0.50,
        }
        receipt = {
            "decision_id": "d1", "market_id": "KXONE", "outcome": "YES",
            "settlement_ts": "2026-07-01T13:00:00+00:00",
            "outcome_known_at": "2026-07-01T14:00:00+00:00",
        }
        result = replay_recorded_collector_lane_with_kelly(
            decisions=[first, second], resolutions=[receipt], starting_balance_usd=100.0,
        )
        self.assertEqual(result["decision_rows"][1]["available_cash_before_usd"], 90.0)
        self.assertEqual(result["settlement_rows"][0]["settlement_ts"], "2026-07-01T13:00:00+00:00")
        self.assertEqual(result["settlement_rows"][0]["available_at"], "2026-07-01T14:00:00+00:00")

    def test_rejects_resolved_at_as_a_settlement_timestamp(self):
        result = replay_recorded_collector_lane_with_kelly(
            decisions=[{
                "decision_id": "d1", "market_id": "KXONE", "observed_at": "2026-07-01T12:00:00+00:00",
                "action": "BUY_YES", "model_probability": 0.70, "yes_price": 0.50,
            }],
            resolutions=[{
                "decision_id": "d1", "market_id": "KXONE", "outcome": "YES",
                "resolved_at": "2026-07-01T13:00:00+00:00",
            }],
            starting_balance_usd=100.0,
        )

        self.assertEqual(result["summary"]["settled_positions"], 0)
        self.assertEqual(result["summary"]["open_positions"], 1)

    def test_requires_exact_decision_identity_for_settlement(self):
        result = replay_recorded_collector_lane_with_kelly(
            decisions=[{
                "decision_id": "d1", "market_id": "KXONE", "observed_at": "2026-07-01T12:00:00+00:00",
                "action": "BUY_YES", "model_probability": 0.70, "yes_price": 0.50,
            }],
            resolutions=[{
                "market_id": "KXONE", "outcome": "YES", "settlement_ts": "2026-07-01T13:00:00+00:00",
            }],
            starting_balance_usd=100.0,
        )

        self.assertEqual(result["summary"]["settled_positions"], 0)
        self.assertEqual(result["summary"]["open_positions"], 1)
        self.assertAlmostEqual(result["summary"]["final_balance_usd"], 100.0)

    def test_writes_distinct_kelly_wallet_artifacts_without_mutating_input_ledgers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decisions_path = root / "collector_lane_decisions.jsonl"
            resolutions_path = root / "resolutions.jsonl"
            output_dir = root / "derived" / "collector_lane_replay_kelly"
            decisions_path.write_text(json.dumps({
                "decision_id": "d1", "market_id": "KXONE", "observed_at": "2026-07-01T12:00:00+00:00",
                "action": "BUY_YES", "model_probability": 0.70, "yes_price": 0.50,
            }) + "\n", encoding="utf-8")
            resolutions_path.write_text(json.dumps({
                "decision_id": "d1", "market_id": "KXONE", "outcome": "YES",
                "settlement_ts": "2026-07-01T13:00:00+00:00",
            }) + "\n", encoding="utf-8")

            result = write_collector_lane_replay_kelly(
                decision_path=decisions_path,
                resolution_paths=[resolutions_path],
                output_dir=output_dir,
                starting_balance_usd=100.0,
            )

            self.assertTrue(result["decision_rows_path"].is_file())
            self.assertTrue(result["settlement_rows_path"].is_file())
            self.assertEqual(result["summary"]["methodology"], "recorded_decision_sequential_synthetic_kelly_capacity_diagnostic")
            self.assertFalse(result["summary"]["paper_parity_claim"])
            settlements = [json.loads(line) for line in result["settlement_rows_path"].read_text(encoding="utf-8").splitlines()]
            self.assertEqual(settlements[0]["resolution_receipt"]["resolution_source_path"], str(resolutions_path))
            self.assertTrue(settlements[0]["resolution_receipt"]["source_row_sha256"])
            self.assertEqual(json.loads(decisions_path.read_text(encoding="utf-8"))["decision_id"], "d1")

    def test_skips_when_price_or_model_probability_is_missing_without_imputing(self):
        result = replay_recorded_collector_lane_with_kelly(
            decisions=[
                {
                    "decision_id": "missing-price",
                    "market_id": "KXONE",
                    "observed_at": "2026-07-01T12:00:00+00:00",
                    "action": "BUY_YES",
                    "model_probability": 0.70,
                },
                {
                    "decision_id": "missing-probability",
                    "market_id": "KXTWO",
                    "observed_at": "2026-07-01T13:00:00+00:00",
                    "action": "BUY_NO",
                    "no_price": 0.50,
                },
            ],
            resolutions=[],
            starting_balance_usd=100.0,
        )

        self.assertEqual(
            [row["reason_code"] for row in result["decision_rows"]],
            ["missing_decision_time_price", "missing_model_probability"],
        )
        self.assertEqual(result["summary"]["opened_positions"], 0)
        self.assertAlmostEqual(result["summary"]["final_balance_usd"], 100.0)


if __name__ == "__main__":
    unittest.main()
