import hashlib
import json
import shutil
import socket
import tempfile
import unittest
from pathlib import Path

from bot.weather.source_coverage_recovery_audit import audit_source_coverage_recovery


def _identity(suffix: str = "a") -> tuple[str, dict]:
    raw_hash = suffix * 64
    return hashlib.sha256(f"input:{suffix}".encode()).hexdigest(), {
        "shared_snapshot_id": f"snapshot-{suffix}",
        "shared_candidate_id": f"candidate-{suffix}",
        "market_id": f"KXTEST-{suffix}",
        "observed_at_utc": "2026-08-01T12:00:00Z",
        "raw_row_sha256": raw_hash,
    }


def _observation(suffix: str = "a", **overrides) -> dict:
    digest, key = _identity(suffix)
    row = {
        "schema_name": "unsettled_or_unusable_source_observation",
        "source_observation_id": f"sha256:source-{suffix}",
        "canonical_input_sha256": digest,
        "decision_key": key,
        "market_id": key["market_id"],
        "disposition_reason": "unavailable_source_implied_side",
        "market_date": "2026-08-03",
        "contract_shape": "tail",
        "question_side": "above",
        "forecast_temp_f": 75.0,
        "threshold": 70.0,
        "source_id": "nws",
        "target_identity": {
            "market_date": "2026-08-03",
            "source_target": "2026-08-03",
            "forecast_start": None,
            "forecast_end": None,
        },
    }
    row.update(overrides)
    return row


def _resolution(observation: dict, **overrides) -> dict:
    row = {
        "schema_name": "replay_finalized_outcome_binding",
        "canonical_input_sha256": observation["canonical_input_sha256"],
        "decision_key": observation["decision_key"],
        "market_id": observation["market_id"],
        "market_status": "finalized",
        "official_outcome": "YES",
        "settlement_ts": "2026-08-04T00:00:00Z",
        "resolution_id": "strict-resolution-1",
        "provenance": {"source": "fixture"},
    }
    row.update(overrides)
    return row


class SourceCoverageRecoveryAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.output = self.root / "derived"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _write_jsonl(self, path: Path, rows: list[dict]) -> Path:
        path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
        return path

    def _run(self, observations: list[dict], resolutions: list[dict]):
        return audit_source_coverage_recovery(
            observation_paths=[self._write_jsonl(self.root / "observations.jsonl", observations)],
            strict_resolution_path=self._write_jsonl(self.root / "resolutions.jsonl", resolutions),
            output_dir=self.output,
        )

    def _queue(self, result) -> list[dict]:
        return [json.loads(line) for line in result.coverage_queue_path.read_text(encoding="utf-8").splitlines()]

    def test_exact_identity_is_required_and_copies_only_resolution_provenance(self):
        observation = _observation()
        wrong = _resolution(observation)
        wrong["decision_key"] = {**wrong["decision_key"], "shared_snapshot_id": "other"}
        exact = _resolution(observation, resolution_id="strict-exact")

        result = self._run([observation], [wrong, exact])

        queue = self._queue(result)
        self.assertEqual(queue[0]["coverage_classification"], "exact_resolved")
        self.assertEqual(queue[0]["strict_resolution_reference"]["resolution_id"], "strict-exact")
        self.assertNotIn("official_outcome", queue[0])
        self.assertEqual(result.metadata["coverage_counts"]["exact_resolved"], 1)

    def test_missing_conflicting_and_invalid_settlement_are_fail_closed(self):
        missing = _observation("a")
        conflict = _observation("b")
        invalid = _observation("c")
        result = self._run(
            [missing, conflict, invalid],
            [
                _resolution(conflict, resolution_id="one"),
                _resolution(conflict, resolution_id="two"),
                _resolution(invalid, settlement_ts="not-a-timestamp"),
            ],
        )

        self.assertEqual(
            [row["coverage_classification"] for row in self._queue(result)],
            ["missing_exact_resolution", "exact_resolution_ambiguity_or_conflict", "invalid_settlement_timestamp"],
        )
        self.assertEqual(result.metadata["coverage_counts"], {
            "observations_seen": 3,
            "invalid_observations": 0,
            "exact_resolved": 0,
            "missing_exact_resolution": 1,
            "exact_resolution_ambiguity_or_conflict": 1,
            "invalid_settlement_timestamp": 1,
        })

    def test_resolution_without_explicit_finalized_status_is_not_accepted(self):
        observation = _observation()
        resolution = _resolution(observation)
        resolution.pop("market_status")

        result = self._run([observation], [resolution])

        self.assertEqual(self._queue(result)[0]["coverage_classification"], "missing_exact_resolution")
        self.assertEqual(result.metadata["resolution_counts"]["invalid_strict_resolution_records"], 1)

    def test_rejects_malformed_raw_row_hash_from_exact_identity(self):
        observation = _observation()
        observation["decision_key"] = {**observation["decision_key"], "raw_row_sha256": "not-a-sha256"}

        result = self._run([observation], [])

        self.assertEqual(self._queue(result), [])
        self.assertEqual(result.metadata["coverage_counts"]["invalid_observations"], 1)

    def test_date_mismatch_is_reported_without_timezone_or_forecast_fabrication(self):
        observation = _observation(
            target_identity={
                "market_date": "2026-08-03",
                "source_target": "2026-08-02",
                "forecast_start": "2026-08-02T10:00:00-05:00",
                "forecast_end": "2026-08-03T09:00:00-05:00",
            },
        )
        result = self._run([observation], [])

        alignment = [json.loads(line) for line in result.source_alignment_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(alignment[0]["target_alignment"], "source_target_date_mismatch")
        self.assertEqual(alignment[0]["market_target_shape"], "forecast_for_calendar_date")
        self.assertEqual(alignment[0]["source_target_shape"], "calendar_date")
        self.assertFalse(alignment[0]["recovery_possible_from_retained_fields"])
        self.assertEqual(alignment[0]["recovery_reason"], "no_exact_recorded_source_target_for_market_date")
        self.assertNotIn("recovered_forecast", alignment[0])
        self.assertFalse(alignment[0]["timezone_conversion_applied"])

    def test_audit_has_no_network_behavior(self):
        observation = _observation()
        original_connect = socket.socket.connect

        def fail_connect(*_args, **_kwargs):
            raise AssertionError("offline audit must not make network connections")

        socket.socket.connect = fail_connect
        try:
            result = self._run([observation], [_resolution(observation)])
        finally:
            socket.socket.connect = original_connect

        self.assertFalse(result.metadata["network_access"])
        self.assertTrue(result.metadata["non_mutating"])


if __name__ == "__main__":
    unittest.main()
