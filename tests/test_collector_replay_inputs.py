import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from bot.collector_replay_inputs import export_collector_replay_inputs


ROOT = Path(__file__).resolve().parents[1]
DERIVED_ROOT = ROOT / "data" / "derived_reports"


def _legacy_collector_row(*, market_id: str = "KXHIGHNY-26AUG12-T80") -> dict:
    """A legacy collector row with only decision-time evidence."""
    return {
        "market_id": market_id,
        "observed_at": "2026-08-12T15:04:05Z",
        "question": "Will NYC high temperature exceed 80F?",
        "yes_price": 0.42,
        "no_price": 0.58,
        "recorded_prediction": {
            "action": "BUY_YES",
            "model_probability": 0.99,
            "position_size": 100.0,
        },
        "final_outcome": "YES",
        "decision_artifact": {
            "source_context": {
                "source": "provided",
                "mode": "prediction_lab",
                "as_of": "2026-08-12T15:00:00+00:00",
                "data": {
                    "market_metadata": {"market_group": "weather"},
                    "weather_source_snapshot": {
                        "source_name": "weather",
                        "as_of": "2026-08-12T15:00:00+00:00",
                        "forecast": {"high": 84.0, "threshold": 80.0},
                    },
                },
            },
            "execution_snapshot": {"best_yes_ask": 0.43},
        },
    }


def _contains_forbidden_payload(value: object) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).lower().replace("_", "")
            if any(token in normalized for token in ("outcome", "resolution", "action", "probability", "positionsize")):
                return True
            if _contains_forbidden_payload(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_payload(item) for item in value)
    return False


class CollectorReplayInputExportTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.archive_path = Path(self.tempdir.name) / "collector.jsonl"
        self.output_dir = Path(tempfile.mkdtemp(prefix="test_collector_replay_inputs_", dir=DERIVED_ROOT))

    def tearDown(self):
        shutil.rmtree(self.output_dir, ignore_errors=True)
        self.tempdir.cleanup()

    def _write_archive(self, rows: list[dict]) -> None:
        self.archive_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8",
        )

    def test_exports_legacy_rows_in_order_without_outcomes_or_recorded_decisions(self):
        first = _legacy_collector_row(market_id="KXFIRST")
        rejected = _legacy_collector_row(market_id="KXMISSING")
        del rejected["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]
        reobservation = _legacy_collector_row(market_id="KXFIRST")
        self._write_archive([first, rejected, reobservation])

        result = export_collector_replay_inputs(
            source_archive=self.archive_path, output_dir=self.output_dir,
        )

        emitted = [json.loads(line) for line in result.records_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([record["market_id"] for record in emitted], ["KXFIRST", "KXFIRST"])
        self.assertEqual(len(emitted), 2)
        self.assertTrue(all(record["input_mode"] == "legacy_sanitized_v1" for record in emitted))
        self.assertTrue(all(not _contains_forbidden_payload(record) for record in emitted))
        metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["selected_row_count"], 3)
        self.assertEqual(metadata["accepted_row_count"], 2)
        self.assertEqual(metadata["rejected_row_count"], 1)
        self.assertEqual(
            metadata["rejection_counts"],
            [{
                "code": "missing_required_field",
                "path": "decision_artifact.source_context.data.weather_source_snapshot",
                "count": 1,
            }],
        )
        self.assertFalse(metadata["for_forward_paper_validation_only"])
        self.assertEqual(metadata["research_status"], "research_only")
        self.assertTrue(metadata["offline"])
        self.assertTrue(metadata["non_mutating"])
        self.assertEqual(
            metadata["source_archive"]["sha256"],
            hashlib.sha256(self.archive_path.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            metadata["output_artifacts"]["replay_decision_inputs.jsonl"]["sha256"],
            hashlib.sha256(result.records_path.read_bytes()).hexdigest(),
        )

    def test_sanitized_export_preserves_explicit_forecast_target_mapping(self):
        row = _legacy_collector_row()
        snapshot = row["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]
        snapshot["sources"] = [{
            "source_id": "nws", "source_name": "NWS", "forecast_high": 84.0,
            "source_evidence_version": 1, "evidence_type": "forecast",
            "forecast_availability": "available", "scoreable_forecast": True,
            "market_target_date": "2026-08-12", "source_target_date": "2026-08-12",
            "source_as_of": "2026-08-12T15:00:00+00:00",
            "target_mapping": {
                "market_target_date": "2026-08-12", "source_target_date": "2026-08-12",
                "mapping": "exact_source_local_nws_period",
                "source_period_start": "2026-08-12T06:00:00-04:00",
                "source_period_end": "2026-08-12T18:00:00-04:00",
            },
        }]
        self._write_archive([row])

        result = export_collector_replay_inputs(source_archive=self.archive_path, output_dir=self.output_dir)

        record = json.loads(result.records_path.read_text(encoding="utf-8").strip())
        source = record["source_inputs"]["source_context"]["data"]["weather_source_snapshot"]["sources"][0]
        self.assertEqual(source["source_target_date"], "2026-08-12")
        self.assertEqual(source["target_mapping"]["mapping"], "exact_source_local_nws_period")
        self.assertEqual(source["source_as_of"], "2026-08-12T15:00:00+00:00")

    def test_refuses_nonempty_derived_output_directory(self):
        self._write_archive([_legacy_collector_row()])
        (self.output_dir / "existing.json").write_text("{}", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "new or empty"):
            export_collector_replay_inputs(source_archive=self.archive_path, output_dir=self.output_dir)

    def test_rejects_nonpositive_or_combined_selection_limits(self):
        self._write_archive([_legacy_collector_row()])

        with self.assertRaisesRegex(ValueError, "accepted_limit must be positive"):
            export_collector_replay_inputs(
                source_archive=self.archive_path,
                output_dir=self.output_dir,
                accepted_limit=0,
            )
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            export_collector_replay_inputs(
                source_archive=self.archive_path,
                output_dir=self.output_dir,
                max_rows=0,
                accepted_limit=1,
            )

    def test_max_rows_zero_remains_an_empty_prefix_selection(self):
        self._write_archive([_legacy_collector_row()])

        result = export_collector_replay_inputs(
            source_archive=self.archive_path,
            output_dir=self.output_dir,
            max_rows=0,
        )

        self.assertEqual(result.records_path.read_bytes(), b"")
        self.assertEqual(result.metadata["selection_mode"], "archive_order")
        self.assertEqual(result.metadata["selected_row_count"], 0)
        self.assertEqual(result.metadata["accepted_row_count"], 0)
        self.assertEqual(result.metadata["rejected_row_count"], 0)

    def test_accepted_limit_walks_back_past_rejected_rows_and_emits_chronologically(self):
        oldest_accepted = _legacy_collector_row(market_id="KXOLDEST")
        rejected_interior = _legacy_collector_row(market_id="KXREJECTEDINTERIOR")
        del rejected_interior["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]
        newest_accepted = _legacy_collector_row(market_id="KXNEWEST")
        rejected_newest = _legacy_collector_row(market_id="KXREJECTEDNEWEST")
        del rejected_newest["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]
        self._write_archive([oldest_accepted, rejected_interior, newest_accepted, rejected_newest])

        result = export_collector_replay_inputs(
            source_archive=self.archive_path,
            output_dir=self.output_dir,
            accepted_limit=2,
        )

        emitted = [json.loads(line) for line in result.records_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([record["market_id"] for record in emitted], ["KXOLDEST", "KXNEWEST"])
        metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["selection_mode"], "newest_accepted")
        self.assertEqual(metadata["accepted_limit"], 2)
        self.assertIsNone(metadata["max_rows"])
        self.assertEqual(metadata["selected_row_count"], 4)
        self.assertEqual(metadata["inspected_row_count"], 4)
        self.assertEqual(metadata["accepted_row_count"], 2)
        self.assertEqual(metadata["rejected_row_count"], 2)

    def test_cli_writes_hash_verified_artifacts_with_bounded_input(self):
        self._write_archive([_legacy_collector_row(market_id="KXONE"), _legacy_collector_row(market_id="KXTWO")])

        completed = subprocess.run(
            [
                sys.executable,
                "scripts/export_collector_replay_inputs.py",
                "--collector-archive", str(self.archive_path),
                "--output-dir", str(self.output_dir),
                "--max-rows", "1",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("accepted=1", completed.stdout)
        metadata = json.loads((self.output_dir / "run_metadata.json").read_text(encoding="utf-8"))
        records_path = self.output_dir / "replay_decision_inputs.jsonl"
        self.assertEqual(metadata["selected_row_count"], 1)
        self.assertEqual(metadata["accepted_row_count"], 1)
        self.assertEqual(metadata["source_archive"]["sha256"], hashlib.sha256(self.archive_path.read_bytes()).hexdigest())
        self.assertEqual(
            metadata["output_artifacts"]["replay_decision_inputs.jsonl"]["sha256"],
            hashlib.sha256(records_path.read_bytes()).hexdigest(),
        )
        self.assertEqual(metadata["replay_input"], {
            "schema_name": "replay_decision_input",
            "schema_version": 1,
            "input_modes": {"legacy_sanitized_v1": 1},
        })

    def test_cli_accepted_limit_selects_newest_usable_record(self):
        oldest = _legacy_collector_row(market_id="KXOLD")
        rejected_newest = _legacy_collector_row(market_id="KXREJECTED")
        del rejected_newest["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]
        newest = _legacy_collector_row(market_id="KXNEW")
        self._write_archive([oldest, rejected_newest, newest])

        completed = subprocess.run(
            [
                sys.executable,
                "scripts/export_collector_replay_inputs.py",
                "--collector-archive", str(self.archive_path),
                "--output-dir", str(self.output_dir),
                "--accepted-limit", "1",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("accepted=1", completed.stdout)
        metadata = json.loads((self.output_dir / "run_metadata.json").read_text(encoding="utf-8"))
        emitted = [json.loads(line) for line in (self.output_dir / "replay_decision_inputs.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([record["market_id"] for record in emitted], ["KXNEW"])
        self.assertEqual(metadata["selection_mode"], "newest_accepted")
        self.assertEqual(metadata["accepted_limit"], 1)
        self.assertEqual(metadata["inspected_row_count"], 1)

    def test_cli_rejects_combined_selection_limits(self):
        self._write_archive([_legacy_collector_row()])

        completed = subprocess.run(
            [
                sys.executable,
                "scripts/export_collector_replay_inputs.py",
                "--collector-archive", str(self.archive_path),
                "--output-dir", str(self.output_dir),
                "--max-rows", "0",
                "--accepted-limit", "1",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("not allowed with argument", completed.stderr)
