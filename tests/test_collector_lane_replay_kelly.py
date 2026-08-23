import json
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
