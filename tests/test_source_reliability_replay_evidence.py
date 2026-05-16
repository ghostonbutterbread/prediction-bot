import json
import tempfile
import unittest
from pathlib import Path

from scripts.source_reliability_replay_evidence import main as evidence_main


def _weather_buy_row(**updates):
    row = {
        "timestamp": "2026-05-14T12:00:00+00:00",
        "observed_at": "2026-05-14T12:00:00+00:00",
        "resolved_at": "2026-05-16T13:00:00+00:00",
        "market_id": "KXHIGHSEA-26MAY15-T70",
        "group": "weather",
        "series": "daily_temperature",
        "question": "Will Seattle high temperature be above 70 degrees on May 15, 2026?",
        "direction": "BUY_YES",
        "decision_type": "buy_yes",
        "actual_temp_used": 73.0,
        "decision_artifact": {
            "market_id": "KXHIGHSEA-26MAY15-T70",
            "observed_at": "2026-05-14T12:00:00+00:00",
            "strategy_signal": {
                "market_id": "KXHIGHSEA-26MAY15-T70",
                "exchange": "kalshi",
                "direction": "BUY_YES",
                "model_probability": 0.74,
                "market_price": 0.44,
                "yes_market_price": 0.44,
                "no_market_price": 0.58,
                "edge": 0.30,
                "confidence": 0.88,
            },
            "source_context": {
                "source": "provided",
                "source_mode": "recorded_as_of",
                "as_of": "2026-05-14T12:00:00+00:00",
                "data": {
                    "market_metadata": {
                        "market_group": "weather",
                        "series": "daily_temperature",
                        "event_ticker": "KXHIGHSEA-26MAY15",
                    },
                    "weather_source_snapshot": {
                        "mode": "recorded_as_of",
                        "source_name": "weather",
                        "signal_type": "weather",
                        "as_of": "2026-05-14T12:00:00+00:00",
                        "target_forecast_date": "2026-05-15",
                        "station_resolution": {"city_id": "seattle_wa", "city": "Seattle"},
                        "forecast": {
                            "high": 72.0,
                            "actual_temp_used": 73.0,
                            "threshold": 70.0,
                            "question_side": "above",
                        },
                        "sources": [{"source_name": "nws", "forecast_high": 72.0}],
                        "date_validation": {
                            "ok": True,
                            "reason": "dates_match",
                            "market_date": "2026-05-15",
                            "weather_date": "2026-05-15",
                        },
                    },
                },
            },
            "source_snapshots": [
                {
                    "mode": "recorded_as_of",
                    "source": "weather",
                    "snapshot_ref": "source_context.data.weather_source_snapshot",
                }
            ],
            "order_book_snapshot": {
                "source": "book",
                "data": {"best_yes_ask": 0.44, "best_no_ask": 0.58},
            },
            "execution_snapshot": {
                "source": "book",
                "best_yes_ask": 0.44,
                "best_no_ask": 0.58,
            },
            "execution_snapshot_source": "book",
            "execution_feasibility": {
                "feasible": True,
                "status": "feasible",
                "same_market_open": True,
                "same_side_ask_present": True,
                "ask_within_slippage": True,
                "elapsed_within_threshold": True,
                "sufficient_quantity": None,
            },
            "final_action": "BUY_YES",
            "final_reason_code": "approved",
        },
    }
    row.update(updates)
    return row


class SourceReliabilityReplayEvidenceTests(unittest.TestCase):
    def test_buy_interest_row_is_selected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            input_path = root / "market_snapshots.jsonl"
            slice_path = root / "slice.jsonl"
            summary_path = root / "summary.json"
            input_path.write_text(json.dumps(_weather_buy_row()) + "\n", encoding="utf-8")

            exit_code = evidence_main(
                [
                    "--input",
                    str(input_path),
                    "--slice-output",
                    str(slice_path),
                    "--summary-output",
                    str(summary_path),
                    "--limit",
                    "10",
                    "--max-slice-rows",
                    "5",
                ]
            )

            self.assertEqual(exit_code, 0)
            selected_rows = [json.loads(line) for line in slice_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(selected_rows), 1)
            self.assertEqual(selected_rows[0]["market_id"], "KXHIGHSEA-26MAY15-T70")
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["mode"], "offline_shadow_report_only")
            self.assertEqual(summary["selected_rows"], 1)
            self.assertEqual(summary["reason_counts"]["selected"], 1)
            self.assertEqual(summary["selected_action_counts"]["BUY_YES"], 1)

    def test_skip_reasons_count_non_weather_missing_timestamp_and_order_book(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            input_path = root / "market_snapshots.jsonl"
            slice_path = root / "slice.jsonl"
            summary_path = root / "summary.json"
            missing_timestamp = _weather_buy_row(timestamp=None, observed_at=None)
            missing_timestamp["decision_artifact"]["observed_at"] = None
            missing_timestamp["decision_artifact"]["source_context"]["as_of"] = None
            missing_order_book = _weather_buy_row(market_id="KXHIGHSEA-26MAY15-T71")
            missing_order_book["decision_artifact"]["order_book_snapshot"] = {"source": "book", "data": {}}
            missing_order_book["decision_artifact"]["execution_snapshot"] = {"source": "book"}
            rows = [
                {"timestamp": "2026-05-14T12:00:00+00:00", "group": "sports", "decision_artifact": {}},
                missing_timestamp,
                missing_order_book,
            ]
            input_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

            exit_code = evidence_main(
                [
                    "--input",
                    str(input_path),
                    "--slice-output",
                    str(slice_path),
                    "--summary-output",
                    str(summary_path),
                ]
            )

            self.assertEqual(exit_code, 0)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["selected_rows"], 0)
            self.assertEqual(summary["reason_counts"]["non_weather"], 1)
            self.assertEqual(summary["reason_counts"]["missing_timestamp"], 1)
            self.assertEqual(
                summary["reason_counts"]["missing_or_unusable_order_book:missing/missing"],
                1,
            )

    def test_ledger_output_is_written_for_selected_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            input_path = root / "market_snapshots.jsonl"
            slice_path = root / "slice.jsonl"
            summary_path = root / "summary.json"
            ledger_path = root / "ledger.jsonl"
            input_path.write_text(json.dumps(_weather_buy_row()) + "\n", encoding="utf-8")

            exit_code = evidence_main(
                [
                    "--input",
                    str(input_path),
                    "--slice-output",
                    str(slice_path),
                    "--summary-output",
                    str(summary_path),
                    "--ledger-output",
                    str(ledger_path),
                ]
            )

            self.assertEqual(exit_code, 0)
            ledger_rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(ledger_rows), 1)
            self.assertEqual(ledger_rows[0]["source_id"], "nws")
            self.assertTrue(ledger_rows[0]["eligible_for_reliability"])
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["ledger_rows"], 1)
            self.assertEqual(summary["ledger_output"], str(ledger_path))


if __name__ == "__main__":
    unittest.main()
