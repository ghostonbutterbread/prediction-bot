import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from bot.replay_outcome_binding import bind_replay_finalized_outcomes
from bot.weather.collector_source_router_replay import _strict_outcome_index


ROOT = Path(__file__).resolve().parents[1]
DERIVED_ROOT = ROOT / "data" / "derived_reports"


def replay_input(market_id: str, marker: str) -> dict:
    decision_key = {
        "shared_snapshot_id": f"snapshot-{marker}",
        "shared_candidate_id": f"candidate-{marker}",
        "market_id": market_id,
        "observed_at_utc": f"2026-08-0{marker[-1]}T12:00:00+00:00",
        "raw_row_sha256": marker * 64,
    }
    record = {
        "schema_name": "replay_decision_input",
        "schema_version": 1,
        "decision_key": decision_key,
        "market_id": market_id,
    }
    record["canonical_input_sha256"] = hashlib.sha256(
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return record


def strict_resolution(market_id: str, *, result: str | None = "yes", outcome: str | None = None) -> dict:
    row = {
        "schema_name": "scoreboard_resolution_backfill",
        "schema_version": 1,
        "resolution_id": f"resolution-{market_id}",
        "market_id": market_id,
        "requested_market_id": market_id,
        "returned_market_id": market_id,
        "market_status": "finalized",
        "settlement_ts": "2026-08-10T01:02:03.000000Z",
    }
    if result is not None:
        row["kalshi_result"] = result
    if outcome is not None:
        row["resolution"] = {"outcome": outcome}
    return row


class ReplayOutcomeBindingTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.inputs_path = Path(self.tempdir.name) / "replay_decision_inputs.jsonl"
        self.resolutions_path = Path(self.tempdir.name) / "strict_resolutions.jsonl"
        self.output_dir = Path(tempfile.mkdtemp(prefix="test_replay_outcome_binding_", dir=DERIVED_ROOT))

    def tearDown(self):
        shutil.rmtree(self.output_dir, ignore_errors=True)
        self.tempdir.cleanup()

    def _write(self, inputs: list[dict], resolutions: list[dict]) -> None:
        self.inputs_path.write_text("".join(json.dumps(row) + "\n" for row in inputs), encoding="utf-8")
        self.resolutions_path.write_text("".join(json.dumps(row) + "\n" for row in resolutions), encoding="utf-8")

    def _outcomes(self, result) -> list[dict]:
        return [json.loads(line) for line in result.outcomes_path.read_text(encoding="utf-8").splitlines()]

    def test_normalizes_kalshi_and_resolution_outcomes_to_official_values(self):
        first = replay_input("KXYES", "a")
        second = replay_input("KXNO", "b")
        self._write(
            [first, second],
            [strict_resolution("KXYES", result="YES"), strict_resolution("KXNO", result=None, outcome="no")],
        )

        result = bind_replay_finalized_outcomes(
            replay_inputs_path=self.inputs_path, strict_resolutions_path=self.resolutions_path, output_dir=self.output_dir,
        )

        self.assertEqual([row["official_outcome"] for row in self._outcomes(result)], ["YES", "NO"])
        self.assertTrue(result.metadata["outcome_available_only_after_sealed_decision"])
        self.assertFalse(result.metadata["network_access"])
        self.assertTrue(result.metadata["non_mutating"])

    def test_binding_does_not_mutate_replay_inputs_and_records_raw_resolution_provenance(self):
        record = replay_input("KXONE", "c")
        resolution = strict_resolution("KXONE")
        self._write([record], [resolution])
        original = self.inputs_path.read_bytes()

        result = bind_replay_finalized_outcomes(
            replay_inputs_path=self.inputs_path, strict_resolutions_path=self.resolutions_path, output_dir=self.output_dir,
        )

        self.assertEqual(self.inputs_path.read_bytes(), original)
        row = self._outcomes(result)[0]
        self.assertEqual(row["canonical_input_sha256"], record["canonical_input_sha256"])
        self.assertEqual(row["decision_key"], record["decision_key"])
        self.assertEqual(row["provenance"]["strict_resolution_source_line_number"], 1)
        self.assertEqual(
            row["provenance"]["raw_resolution_row_sha256"],
            hashlib.sha256(json.dumps(resolution).encode("utf-8")).hexdigest(),
        )

    def test_binding_rejects_an_input_mutated_after_its_canonical_hash_was_recorded(self):
        record = replay_input("KXMUTATED", "h")
        record["source_inputs"] = {"source_context": {"source": "edited-after-export"}}
        self._write([record], [strict_resolution("KXMUTATED")])

        result = bind_replay_finalized_outcomes(
            replay_inputs_path=self.inputs_path, strict_resolutions_path=self.resolutions_path, output_dir=self.output_dir,
        )

        self.assertEqual(self._outcomes(result), [])
        self.assertEqual(result.metadata["binding_counts"]["invalid"], 1)

    def test_duplicate_market_resolution_is_ambiguous_and_blocks_every_binding(self):
        first = replay_input("KXDUP", "d")
        second = replay_input("KXDUP", "e")
        duplicate = strict_resolution("KXDUP")
        duplicate["resolution_id"] = "resolution-KXDUP-second"
        self._write([first, second], [strict_resolution("KXDUP"), duplicate])

        result = bind_replay_finalized_outcomes(
            replay_inputs_path=self.inputs_path, strict_resolutions_path=self.resolutions_path, output_dir=self.output_dir,
        )

        self.assertEqual(self._outcomes(result), [])
        unbound = [json.loads(line) for line in result.unbound_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["binding_reason"] for row in unbound], ["ambiguous_market_id_resolution"] * 2)
        self.assertEqual(result.metadata["binding_counts"]["ambiguous"], 2)

    def test_reobservations_keep_input_order_and_share_one_unambiguous_resolution(self):
        first = replay_input("KXREOBS", "1")
        second = replay_input("KXREOBS", "2")
        missing = replay_input("KXMISSING", "3")
        self._write([first, second, missing], [strict_resolution("KXREOBS", result="no")])

        result = bind_replay_finalized_outcomes(
            replay_inputs_path=self.inputs_path, strict_resolutions_path=self.resolutions_path, output_dir=self.output_dir,
        )

        rows = self._outcomes(result)
        self.assertEqual([row["canonical_input_sha256"] for row in rows], [first["canonical_input_sha256"], second["canonical_input_sha256"]])
        self.assertEqual([row["market_id"] for row in rows], ["KXREOBS", "KXREOBS"])
        self.assertEqual(result.metadata["binding_counts"], {
            "candidate": 3, "bound": 2, "unbound": 1, "ambiguous": 0, "invalid": 0,
        })
        self.assertEqual(
            json.loads(result.unbound_path.read_text(encoding="utf-8"))["binding_reason"], "missing_authoritative_resolution",
        )

    def test_produced_rows_satisfy_current_strict_replay_identity_contract(self):
        record = replay_input("KXIDENTITY", "f")
        self._write([record], [strict_resolution("KXIDENTITY", outcome="YES")])

        result = bind_replay_finalized_outcomes(
            replay_inputs_path=self.inputs_path, strict_resolutions_path=self.resolutions_path, output_dir=self.output_dir,
        )

        index, stats = _strict_outcome_index(self._outcomes(result))
        self.assertEqual(stats["accepted_rows"], 1)
        self.assertEqual(len(index), 1)

    def test_cli_writes_hash_verified_artifacts(self):
        self._write([replay_input("KXCLI", "9")], [strict_resolution("KXCLI")])

        completed = subprocess.run(
            [
                sys.executable, "scripts/bind_replay_finalized_outcomes.py",
                "--replay-inputs", str(self.inputs_path),
                "--strict-resolutions", str(self.resolutions_path), "--output-dir", str(self.output_dir),
            ], cwd=ROOT, check=True, capture_output=True, text=True,
        )

        self.assertIn("bound=1", completed.stdout)
        metadata = json.loads((self.output_dir / "run_metadata.json").read_text(encoding="utf-8"))
        outcomes_path = self.output_dir / "finalized_replay_outcomes.jsonl"
        self.assertEqual(
            metadata["output_artifacts"][outcomes_path.name]["sha256"], hashlib.sha256(outcomes_path.read_bytes()).hexdigest(),
        )
