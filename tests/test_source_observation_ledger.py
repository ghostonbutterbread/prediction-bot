import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from bot.weather.source_observation_ledger import (
    is_eligible_for_future_history,
    materialize_source_observation_ledger,
)
from bot.weather.source_performance_materializer import materialize_source_performance_once


def _input(*, market_id: str = "KXHIGHSEA-26AUG03-T70", raw_hash: str = "a" * 64) -> dict:
    observed_at = "2026-08-01T12:00:00+00:00"
    return {
        "schema_name": "replay_decision_input",
        "schema_version": 1,
        "canonical_input_sha256": hashlib.sha256(f"input:{raw_hash}".encode()).hexdigest(),
        "decision_key": {
            "shared_snapshot_id": f"snapshot-{raw_hash}",
            "shared_candidate_id": f"candidate-{raw_hash}",
            "market_id": market_id,
            "observed_at_utc": observed_at,
            "raw_row_sha256": raw_hash,
        },
        "market_id": market_id,
        "observed_at": observed_at,
        "market": {
            "question": "Will Seattle high temperature be above 70°?",
            "market_metadata": {"event_ticker": "KXHIGHSEA-26AUG03"},
            # These must never cross into the source-observation artifacts.
            "yes_price": 0.42,
            "no_price": 0.58,
        },
        "source_inputs": {
            "recorded_as_of": "2026-08-01T11:55:00+00:00",
            "source_context": {
                "as_of": "2026-08-01T11:55:00+00:00",
                "data": {
                    "weather_source_snapshot": {
                        "market_id": market_id,
                        "question": "Will Seattle high temperature be above 70°?",
                        "market_date": "2026-08-03",
                        "station_resolution": {"city_id": "seattle_wa", "city": "Seattle"},
                        "sources": [
                            {"source_id": "nws", "source_name": "NWS", "forecast_high": 75.0, "as_of": "2026-08-01T11:54:00+00:00", "target_forecast_date": "2026-08-03"},
                            {"source_id": "open_meteo", "source_name": "Open-Meteo", "forecast_high": 65.0, "as_of": "2026-08-01T11:53:00+00:00", "target_forecast_date": "2026-08-03"},
                            {"source_id": "station", "source_name": "Station", "forecast_target": "current_observation", "as_of": "2026-08-01T11:52:00+00:00"},
                        ],
                    },
                },
            },
        },
    }


def _outcome(record: dict, *, outcome: str = "YES", settlement_ts: str = "2026-08-04T00:00:00+00:00", resolution_id: str = "resolution-1") -> dict:
    return {
        "schema_name": "replay_finalized_outcome_binding",
        "schema_version": 1,
        "market_status": "finalized",
        "canonical_input_sha256": record["canonical_input_sha256"],
        "decision_key": record["decision_key"],
        "market_id": record["market_id"],
        "official_outcome": outcome,
        "settlement_ts": settlement_ts,
        "resolution_id": resolution_id,
        "resolved_at": "2026-08-07T00:00:00+00:00",
    }


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


class SourceObservationLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _run(self, inputs: list[dict], outcomes: list[dict]) -> tuple[dict, dict[str, list[dict]]]:
        inputs_path = self.root / "inputs.jsonl"
        outcomes_path = self.root / "outcomes.jsonl"
        output_dir = self.root / "derived"
        _write_jsonl(inputs_path, inputs)
        _write_jsonl(outcomes_path, outcomes)
        result = materialize_source_observation_ledger(
            replay_inputs_path=inputs_path, finalized_outcomes_path=outcomes_path, output_dir=output_dir,
        )
        artifacts = {
            path.stem: [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            for path in (result.pending_path, result.settled_path, result.unsettled_path)
        }
        return result.metadata, artifacts

    def test_records_every_captured_source_without_router_selection_and_scores_later(self) -> None:
        record = _input()
        metadata, artifacts = self._run([record], [_outcome(record)])

        pending = artifacts["pending_source_observations"]
        settled = artifacts["settled_source_correctness"]
        unusable = artifacts["unsettled_or_unusable_source_observations"]
        self.assertEqual([row["source_id"] for row in pending], ["nws", "open_meteo", "station"])
        self.assertEqual({row["source_id"]: row["direction_correct"] for row in settled}, {"nws": True, "open_meteo": False})
        self.assertEqual(unusable[0]["source_id"], "station")
        self.assertEqual(unusable[0]["disposition_reason"], "unavailable_source_implied_side")
        self.assertTrue(all(row["eligible_for_reliability"] for row in settled))
        self.assertTrue(all(row["known_after"] == row["settlement_ts"] for row in settled))
        self.assertEqual(metadata["counts"]["pending"], 3)
        self.assertEqual(metadata["counts"]["settled"], 2)
        self.assertEqual(metadata["counts"]["unsettled_or_unusable"], 1)

    def test_pending_has_no_outcomes_or_action_price_stake_data(self) -> None:
        record = _input()
        _, artifacts = self._run([record], [_outcome(record)])

        encoded = json.dumps(artifacts["pending_source_observations"], sort_keys=True).lower()
        for forbidden in ("official_outcome", "settlement", "resolved", "action", "price", "stake", "wallet"):
            self.assertNotIn(forbidden, encoded)

    def test_exact_outcome_identity_is_required(self) -> None:
        record = _input()
        wrong = _outcome(record)
        wrong["decision_key"] = dict(wrong["decision_key"], shared_snapshot_id="another-snapshot")
        _, artifacts = self._run([record], [wrong])

        self.assertEqual(artifacts["settled_source_correctness"], [])
        self.assertEqual(len(artifacts["unsettled_or_unusable_source_observations"]), 3)
        self.assertEqual(
            {row["disposition_reason"] for row in artifacts["unsettled_or_unusable_source_observations"]},
            {"missing_exact_authoritative_outcome"},
        )

    def test_duplicate_inputs_and_identical_outcomes_do_not_inflate_history(self) -> None:
        record = _input()
        metadata, artifacts = self._run([record, record], [_outcome(record), _outcome(record)])

        self.assertEqual(len(artifacts["pending_source_observations"]), 3)
        self.assertEqual(len(artifacts["settled_source_correctness"]), 2)
        self.assertGreater(metadata["counts"]["duplicate_input_source_observations"], 0)
        self.assertGreater(metadata["counts"]["duplicate_outcomes"], 0)

    def test_distinct_targets_for_one_source_remain_distinct_evidence(self) -> None:
        record = _input()
        sources = record["source_inputs"]["source_context"]["data"]["weather_source_snapshot"]["sources"]
        sources.append({
            "source_id": "nws", "source_name": "NWS", "forecast_high": 75.0,
            "as_of": "2026-08-01T11:54:00+00:00", "target_forecast_date": "2026-08-04",
        })

        _, artifacts = self._run([record], [_outcome(record)])

        nws_rows = [row for row in artifacts["pending_source_observations"] if row["source_id"] == "nws"]
        self.assertEqual(len(nws_rows), 2)
        self.assertEqual(len({row["source_observation_id"] for row in nws_rows}), 2)

    def test_conflicting_exact_outcomes_fail_closed_to_unusable(self) -> None:
        record = _input()
        _, artifacts = self._run([record], [_outcome(record, outcome="YES"), _outcome(record, outcome="NO")])

        self.assertEqual(artifacts["settled_source_correctness"], [])
        self.assertEqual(
            {row["disposition_reason"] for row in artifacts["unsettled_or_unusable_source_observations"]},
            {"conflicting_exact_authoritative_outcome"},
        )

    def test_history_eligibility_uses_settlement_time_strictly_not_retrieval_time(self) -> None:
        settled = {
            "eligible_for_source_history": True,
            "settlement_ts": "2026-08-04T00:00:00+00:00",
            "resolution_resolved_at": "2026-08-07T00:00:00+00:00",
        }
        self.assertTrue(is_eligible_for_future_history(settled, "2026-08-05T00:00:00+00:00"))
        self.assertFalse(is_eligible_for_future_history(settled, "2026-08-04T00:00:00+00:00"))


class SourcePerformanceSettlementChronologyTests(unittest.TestCase):
    def test_materializer_uses_earlier_authoritative_settlement_not_later_resolution_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = root / "snapshot.jsonl"
            resolution = root / "resolution.jsonl"
            output = root / "performance.jsonl"
            _write_jsonl(snapshot, [{
                "market_id": "KXHIGHSEA-26AUG03-T70",
                "question": "Will Seattle high temperature be above 70°?",
                "observed_at": "2026-08-01T12:00:00+00:00",
                "weather_source_snapshot": {
                    "station_resolution": {"city_id": "seattle_wa"},
                    "sources": [{"source_id": "nws", "source_name": "NWS", "forecast_high": 75.0}],
                },
            }])
            _write_jsonl(resolution, [{
                "market_id": "KXHIGHSEA-26AUG03-T70", "market_status": "finalized", "kalshi_result": "yes",
                "settlement_ts": "2026-08-04T00:00:00+00:00", "resolved_at": "2026-08-07T00:00:00+00:00",
                "resolution_id": "strict-1",
            }])

            materialize_source_performance_once(
                cohort_id="test", snapshot_paths=[snapshot], resolution_path=resolution, output_path=output,
            )

            row = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(row["known_after"], "2026-08-04T00:00:00+00:00")
            self.assertEqual(row["settlement_ts"], "2026-08-04T00:00:00+00:00")
