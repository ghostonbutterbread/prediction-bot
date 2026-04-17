import json
import tempfile
import unittest
from pathlib import Path

from bot.weather import WeatherRegistry
from bot.weather.analysis import WeatherSampleRecord, load_historical_csv_samples
from bot.weather.replay import (
    ReplayFeeModel,
    build_weather_replay_dataset,
    build_weather_replay_record,
    iter_weather_replay_records,
    score_replay_answer,
    score_replay_answers,
)
from scripts.weather_replay_quiz import main as replay_main


class WeatherReplayTests(unittest.TestCase):
    def test_build_weather_replay_record_hides_outcome_and_keeps_city_context(self):
        record = WeatherSampleRecord(
            sample_kind="historical_csv",
            source_path="memory.csv",
            observed_at="2026-03-25T05:59:00Z",
            resolved_at="2026-03-25T13:00:00Z",
            market_id="KXHIGHNY-26MAR25-T51",
            category="KXHIGHNY",
            question="Will the **high temp in NYC** be <51° on Mar 25, 2026?",
            yes_price=0.06,
            outcome="NO",
            metadata={
                "market_subtitle": "<51°",
                "yes_subtitle": "Yes",
                "starts_at": "2026-03-24T15:00:00Z",
            },
        )

        replay = build_weather_replay_record(record, registry=WeatherRegistry.from_file())

        self.assertNotIn("outcome", replay.quiz_payload)
        self.assertEqual(replay.quiz_payload["city_context"]["city_id"], "new_york_ny")
        self.assertEqual(replay.quiz_payload["prices"]["yes_price"], 0.06)
        self.assertEqual(replay.quiz_payload["prices"]["no_price"], 0.94)
        self.assertEqual(replay.quiz_payload["metadata"]["market_subtitle"], "<51°")
        self.assertEqual(replay.answer_key["outcome"], "NO")
        self.assertEqual(replay.answer_key["correct_action"], "BUY_NO")

    def test_iter_weather_replay_records_skips_unresolved_and_dataset_separates_keys(self):
        resolved = WeatherSampleRecord(
            sample_kind="historical_csv",
            source_path="memory.csv",
            observed_at="2026-04-14T00:00:00Z",
            resolved_at="2026-04-14T13:00:00Z",
            market_id="KXHIGHMIA-26APR14-T77",
            category="KXHIGHMIA",
            question="Will the maximum temperature be  <77° on Apr 14, 2026?",
            outcome="YES",
        )
        unresolved = WeatherSampleRecord(
            sample_kind="snapshot",
            source_path="memory.json",
            observed_at="2026-04-14T00:00:00Z",
            resolved_at=None,
            market_id="KXHIGHMIA-26APR15-T77",
            category="KXHIGHMIA",
            question="Will the maximum temperature be  <77° on Apr 15, 2026?",
            outcome=None,
        )

        replay_records = iter_weather_replay_records([resolved, unresolved], registry=WeatherRegistry.from_file())
        dataset = build_weather_replay_dataset([resolved, unresolved], registry=WeatherRegistry.from_file())

        self.assertEqual(len(replay_records), 1)
        self.assertEqual(dataset["summary"]["records"], 1)
        self.assertEqual(dataset["quiz_payloads"][0]["replay_id"], dataset["answer_keys"][0]["replay_id"])
        self.assertNotIn("outcome", dataset["quiz_payloads"][0])
        self.assertEqual(dataset["answer_keys"][0]["correct_action"], "BUY_YES")

    def test_score_replay_answer_supports_buy_yes_buy_no_and_skip(self):
        answer_key = {
            "replay_id": "wxr_test",
            "market_id": "mkt-1",
            "outcome": "NO",
            "prices": {"yes_price": 0.22, "no_price": 0.78},
        }

        buy_no = score_replay_answer("BUY_NO", answer_key)
        buy_yes = score_replay_answer("BUY_YES", answer_key)
        skip = score_replay_answer("SKIP", answer_key)

        self.assertTrue(buy_no["is_correct"])
        self.assertEqual(buy_no["points"], 1)
        self.assertAlmostEqual(buy_no["realized_return"], 0.22)
        self.assertAlmostEqual(buy_no["contracts"], 1.0)
        self.assertAlmostEqual(buy_no["position_size"], 0.78)
        self.assertAlmostEqual(buy_no["gross_pnl"], 0.22)
        self.assertAlmostEqual(buy_no["fees_paid"], 0.0154)
        self.assertAlmostEqual(buy_no["net_pnl"], 0.2046)
        self.assertFalse(buy_yes["is_correct"])
        self.assertEqual(buy_yes["points"], -1)
        self.assertAlmostEqual(buy_yes["realized_return"], -0.22)
        self.assertAlmostEqual(buy_yes["contracts"], 1.0)
        self.assertAlmostEqual(buy_yes["position_size"], 0.22)
        self.assertAlmostEqual(buy_yes["gross_pnl"], -0.22)
        self.assertAlmostEqual(buy_yes["fees_paid"], 0.0)
        self.assertAlmostEqual(buy_yes["net_pnl"], -0.22)
        self.assertIsNone(skip["is_correct"])
        self.assertEqual(skip["points"], 0)
        self.assertEqual(skip["realized_return"], 0.0)
        self.assertEqual(skip["position_size"], 0.0)
        self.assertEqual(skip["fees_paid"], 0.0)

    def test_score_replay_answer_supports_position_size_and_fee_model(self):
        answer_key = {
            "replay_id": "wxr_size",
            "market_id": "mkt-size",
            "outcome": "YES",
            "prices": {"yes_price": 0.25, "no_price": 0.75},
        }

        sized = score_replay_answer(
            "BUY_YES",
            answer_key,
            position_size=10.0,
            fee_model=ReplayFeeModel(0.10),
        )

        self.assertAlmostEqual(sized["entry_price"], 0.25)
        self.assertAlmostEqual(sized["contracts"], 40.0)
        self.assertAlmostEqual(sized["position_size"], 10.0)
        self.assertAlmostEqual(sized["gross_pnl"], 30.0)
        self.assertAlmostEqual(sized["fees_paid"], 3.0)
        self.assertAlmostEqual(sized["net_pnl"], 27.0)
        self.assertAlmostEqual(sized["fee_rate"], 0.10)

    def test_score_replay_answers_matches_replay_or_market_id_and_aggregates_pnl(self):
        answer_keys = [
            {
                "replay_id": "wxr_one",
                "market_id": "mkt-1",
                "outcome": "YES",
                "prices": {"yes_price": 0.25, "no_price": 0.75},
            },
            {
                "replay_id": "wxr_two",
                "market_id": "mkt-2",
                "outcome": "NO",
                "prices": {"yes_price": 0.20, "no_price": 0.80},
            },
            {
                "replay_id": "wxr_three",
                "market_id": "mkt-3",
                "outcome": "YES",
                "prices": {"yes_price": 0.40, "no_price": 0.60},
            },
            {
                "replay_id": "wxr_four",
                "market_id": "mkt-4",
                "outcome": "NO",
                "prices": {"yes_price": 0.55, "no_price": 0.45},
            },
        ]
        answers = [
            {"replay_id": "wxr_one", "action": "BUY_YES", "position_size": 10.0},
            {"market_id": "mkt-2", "direction": "BUY_NO", "contracts": 2},
            {"replay_id": "wxr_three", "action": "BUY_NO", "size": 3.0},
            {"market_id": "mkt-4", "direction": "SKIP"},
            {"replay_id": "missing", "action": "BUY_NO"},
        ]

        scored = score_replay_answers(answers, answer_keys)

        self.assertEqual(scored["summary"]["answers_scored"], 4)
        self.assertEqual(scored["summary"]["buys"], 3)
        self.assertEqual(scored["summary"]["correct"], 2)
        self.assertEqual(scored["summary"]["incorrect"], 1)
        self.assertEqual(scored["summary"]["skipped"], 1)
        self.assertAlmostEqual(scored["summary"]["win_rate"], 0.6667)
        self.assertAlmostEqual(scored["summary"]["gross_profit_total"], 30.4)
        self.assertAlmostEqual(scored["summary"]["gross_loss_total"], 3.0)
        self.assertAlmostEqual(scored["summary"]["net_pnl"], 27.4)
        self.assertAlmostEqual(scored["summary"]["fees_total"], 2.128)
        self.assertAlmostEqual(scored["summary"]["fee_adjusted_pnl"], 25.272)
        self.assertAlmostEqual(scored["summary"]["position_size_total"], 14.6)
        self.assertAlmostEqual(scored["summary"]["contracts_total"], 47.0)
        self.assertEqual(scored["summary"]["side_counts"]["BUY_YES"], 1)
        self.assertEqual(scored["summary"]["side_counts"]["BUY_NO"], 2)
        self.assertEqual(scored["summary"]["side_counts"]["SKIP"], 1)
        self.assertEqual(scored["summary"]["missing_answer_keys"], 1)
        self.assertEqual(scored["results"][0]["action"], "BUY_YES")
        self.assertAlmostEqual(scored["results"][0]["net_pnl"], 27.9)
        self.assertEqual(scored["results"][1]["action"], "BUY_NO")
        self.assertAlmostEqual(scored["results"][1]["net_pnl"], 0.372)
        self.assertEqual(scored["results"][2]["action"], "BUY_NO")
        self.assertAlmostEqual(scored["results"][2]["net_pnl"], -3.0)
        self.assertEqual(scored["results"][3]["action"], "SKIP")

    def test_score_replay_answers_rejects_invalid_position_size(self):
        answer_keys = [
            {
                "replay_id": "wxr_one",
                "market_id": "mkt-1",
                "outcome": "YES",
                "prices": {"yes_price": 0.25, "no_price": 0.75},
            }
        ]

        scored = score_replay_answers(
            [{"replay_id": "wxr_one", "action": "BUY_YES", "position_size": 0}],
            answer_keys,
        )

        self.assertEqual(scored["summary"]["answers_scored"], 0)
        self.assertEqual(scored["summary"]["invalid_answers"], 1)
        self.assertIn("position_size must be positive", scored["invalid_answers"][0]["error"])


class WeatherReplayScriptTests(unittest.TestCase):
    def test_script_emits_quiz_records_and_scores_answers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            history_path = root / "kalshi.csv"
            history_path.write_text(
                "\n".join(
                    [
                        "EVENT_TICKER,MARKET_TICKER,MARKET_TITLE,MARKET_SUBTITLE,YES_SUBTITLE,NO_SUBTITLE,RESULT,START_DT,END_DT,CLOSED_DT,INGESTION_DT,YES_PRICE",
                        'KXHIGHAUS-24DEC01,KXHIGHAUS-24DEC01-B70.5,"Will the **high temp in Austin** be 70-71° on Dec 1, 2024?",70-71°,Yes,No,yes,2024-11-30T15:00:00.000000Z,2024-12-02T05:59:00.000000Z,2024-12-02T13:00:47.453684Z,2026-03-07T19:59:14.000000Z,0.42',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            records = load_historical_csv_samples(history_path, one_per_series=True)
            dataset = build_weather_replay_dataset(records, registry=WeatherRegistry.from_file())
            answers_path = root / "answers.jsonl"
            answers_path.write_text(
                json.dumps(
                    {
                        "replay_id": dataset["answer_keys"][0]["replay_id"],
                        "action": "BUY_YES",
                        "position_size": 4.2,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            quiz_path = root / "quiz.jsonl"
            answer_key_path = root / "answer_key.jsonl"
            score_path = root / "scores.json"

            exit_code = replay_main(
                [
                    "--input",
                    str(history_path),
                    "--output",
                    str(quiz_path),
                    "--answer-key-output",
                    str(answer_key_path),
                    "--score-answers",
                    str(answers_path),
                    "--score-output",
                    str(score_path),
                    "--fee-rate",
                    "0.10",
                    "--max-records",
                    "5",
                ]
            )

            self.assertEqual(exit_code, 0)
            quiz_records = [json.loads(line) for line in quiz_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            answer_keys = [
                json.loads(line)
                for line in answer_key_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            score_report = json.loads(score_path.read_text(encoding="utf-8"))

            self.assertEqual(len(quiz_records), 1)
            self.assertEqual(len(answer_keys), 1)
            self.assertEqual(quiz_records[0]["question"], "Will the **high temp in Austin** be 70-71° on Dec 1, 2024?")
            self.assertNotIn("outcome", quiz_records[0])
            self.assertEqual(answer_keys[0]["outcome"], "YES")
            self.assertEqual(score_report["summary"]["answers_scored"], 1)
            self.assertEqual(score_report["summary"]["correct"], 1)
            self.assertAlmostEqual(score_report["summary"]["position_size_total"], 4.2)
            self.assertAlmostEqual(score_report["summary"]["fees_total"], 0.58)
            self.assertAlmostEqual(score_report["summary"]["fee_adjusted_pnl"], 5.22)
            self.assertAlmostEqual(score_report["summary"]["fee_model"]["profit_fee_rate"], 0.10)


if __name__ == "__main__":
    unittest.main()
