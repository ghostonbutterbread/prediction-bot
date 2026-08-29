import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from bot.auto_source_router_promotion import auto_populate_source_router_history
from scripts.weather_source_router_replay import _load_router_ledger_rows


ROOT = Path(__file__).resolve().parents[1]
DERIVED_ROOT = ROOT / "data" / "derived_reports"


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _collector_row(*, strict: bool = True, market_id: str = "KXHIGHSEA-26AUG03-T70") -> dict:
    source = {
        "source_id": "nws", "source_name": "NWS", "source_location_city": "Seattle",
        "forecast_measurement_kind": "high", "contract_shape": "tail", "question_side": "above",
        "forecast_high": 75.0, "source_as_of": "2026-08-01T11:54:00+00:00",
        "source_evidence_version": 1, "evidence_type": "forecast", "scoreable_forecast": True,
        "target_mapping": {
            "market_target_date": "2026-08-03", "source_target_date": "2026-08-03",
            "source_timezone": "America/Los_Angeles",
        },
    }
    if not strict:
        source.pop("source_location_city")
    return {
        "market_id": market_id,
        "observed_at": "2026-08-01T12:00:00+00:00",
        "question": "Will Seattle high temperature be above 70°?",
        "yes_price": 0.42, "no_price": 0.58,
        "decision_artifact": {"source_context": {"source": "provided", "mode": "prediction_lab", "as_of": "2026-08-01T11:55:00+00:00", "data": {
            "market_metadata": {"event_ticker": "KXHIGHSEA-26AUG03", "city": "Seattle", "city_id": "seattle_wa", "market_kind": "high", "contract_shape": "threshold"},
            "weather_source_snapshot": {
                "market_id": market_id, "question": "Will Seattle high temperature be above 70°?",
                "market_date": "2026-08-03", "station_resolution": {"city_id": "seattle_wa", "city": "Seattle"},
                "sources": [source],
            },
        }}},
    }


def _strict_resolution(market_id: str) -> dict:
    return {
        "market_id": market_id, "requested_market_id": market_id, "returned_market_id": market_id,
        "market_status": "finalized", "kalshi_result": "yes", "settlement_ts": "2026-08-04T00:00:00Z",
        "resolution_id": "authoritative-resolution-1",
    }


class AutoSourceRouterPromotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.archive = self.root / "immutable_snapshots.jsonl"
        self.resolutions = self.root / "authoritative_resolutions.jsonl"
        DERIVED_ROOT.mkdir(parents=True, exist_ok=True)
        self.output_root = Path(tempfile.mkdtemp(prefix="test_auto_source_router_", dir=DERIVED_ROOT))

    def tearDown(self) -> None:
        shutil.rmtree(self.output_root, ignore_errors=True)
        self.tempdir.cleanup()

    def run_pipeline(self, rows: list[dict], resolutions: list[dict]):
        _write_jsonl(self.archive, rows)
        _write_jsonl(self.resolutions, resolutions)
        return auto_populate_source_router_history(
            collector_snapshots_path=self.archive,
            strict_resolutions_path=self.resolutions,
            output_root=self.output_root,
        )

    def test_no_resolutions_writes_no_router_history(self) -> None:
        result = self.run_pipeline([_collector_row()], [])

        self.assertEqual(result.status, "no_router_history")
        self.assertEqual(result.counts["eligible"], 0)
        self.assertEqual(result.history_path.read_text(encoding="utf-8"), "")
        self.assertEqual(result.counts["unresolved"], 1)

    def test_exact_finalized_settlement_creates_strict_history_with_provenance_and_chronology(self) -> None:
        row = _collector_row()
        result = self.run_pipeline([row], [_strict_resolution(row["market_id"])])

        [history] = [json.loads(line) for line in result.history_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(result.status, "history_ready")
        self.assertEqual(result.counts["eligible"], 1)
        self.assertTrue(history["eligible_for_source_history"])
        self.assertEqual(history["settlement_ts"], "2026-08-04T00:00:00Z")
        self.assertEqual(history["known_after"], history["settlement_ts"])
        self.assertIn("source_record_sha256", history["source_provenance"])
        self.assertIn("strict_resolution_source_sha256", history["resolution_provenance"])
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["runtime_consumption"]["history_ledger_path"], str(result.history_path))
        self.assertEqual(manifest["chronology"]["availability_field"], "settlement_ts")

    def test_generated_history_is_accepted_by_the_existing_router_history_loader(self) -> None:
        row = _collector_row()
        result = self.run_pipeline([row], [_strict_resolution(row["market_id"])])

        loaded, stats = _load_router_ledger_rows(
            [], history_ledger_paths=[result.history_path], source_paths=[], decision_paths=[],
        )

        self.assertEqual(stats["history_ledger_rows"], 1)
        self.assertTrue(loaded[0]["source_router_history_only"])
        self.assertEqual(loaded[0]["settlement_ts"], loaded[0]["known_after"])

    def test_cli_reports_required_promotion_counts(self) -> None:
        row = _collector_row()
        _write_jsonl(self.archive, [row])
        _write_jsonl(self.resolutions, [_strict_resolution(row["market_id"])])
        completed = subprocess.run(
            [
                sys.executable, "scripts/auto_populate_source_router_history.py",
                "--collector-snapshots", str(self.archive),
                "--strict-resolutions", str(self.resolutions),
                "--output-root", str(self.output_root),
            ],
            cwd=ROOT, check=True, capture_output=True, text=True,
        )

        self.assertIn("status=history_ready", completed.stdout)
        for name in ("pending=", "unresolved=", "invalid=", "eligible=", "collapsed="):
            self.assertIn(name, completed.stdout)

    def test_repeat_invocation_reuses_the_same_generation_without_rewriting_history(self) -> None:
        row = _collector_row()
        first = self.run_pipeline([row], [_strict_resolution(row["market_id"])])
        before = first.history_path.read_bytes()
        second = self.run_pipeline([row], [_strict_resolution(row["market_id"])])

        self.assertTrue(second.reused)
        self.assertEqual(first.generation_dir, second.generation_dir)
        self.assertEqual(before, second.history_path.read_bytes())

    def test_malformed_ambiguous_and_unproven_evidence_stays_out_of_router_history(self) -> None:
        unproven = _collector_row(strict=False)
        ambiguous = _strict_resolution(unproven["market_id"])
        result = self.run_pipeline([unproven], [ambiguous, dict(ambiguous, resolution_id="conflict")])

        self.assertEqual(result.status, "no_router_history")
        self.assertEqual(result.counts["eligible"], 0)
        self.assertGreater(result.counts["unresolved"], 0)
        self.assertEqual(result.history_path.read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    unittest.main()
