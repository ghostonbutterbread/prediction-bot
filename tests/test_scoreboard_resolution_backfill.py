import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from bot.file_ops import load_jsonl
from bot.scoreboard_resolution_backfill import (
    backfill_scoreboard_resolutions,
    extract_market_refs,
    normalized_market_outcome,
)

ROOT = Path(__file__).resolve().parent.parent


class FakeHttpError(Exception):
    def __init__(self, code: int, retry_after: str | None = None):
        super().__init__(f"HTTP Error {code}")
        self.code = code
        self.headers = {"Retry-After": retry_after} if retry_after is not None else {}


class ScoreboardResolutionBackfillTests(unittest.TestCase):
    def _write_jsonl(self, path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def test_extracts_nested_market_ids_from_lane_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "lane.jsonl"
            self._write_jsonl(
                path,
                [
                    {
                        "shared_candidate_id": "candidate-1",
                        "run_id": "run-1",
                        "market_id": "KXHIGHSEA-26MAY17-T70",
                        "provenance": {
                            "source_scoreboard": {
                                "future_pnl_inputs": {
                                    "market_id": "KXLOWSEA-26MAY17-B55.5",
                                }
                            }
                        },
                    }
                ],
            )

            refs, report = extract_market_refs([path])

        self.assertEqual(report["rows_read"], 1)
        self.assertEqual([ref.market_id for ref in refs], ["KXHIGHSEA-26MAY17-T70", "KXLOWSEA-26MAY17-B55.5"])
        self.assertTrue(all(ref.shared_candidate_id == "candidate-1" for ref in refs))
        self.assertTrue(all(ref.run_id == "run-1" for ref in refs))

    def test_normalizes_kalshi_result_and_settlement_value(self):
        self.assertEqual(normalized_market_outcome({"result": "yes"})[0], "YES")
        self.assertEqual(normalized_market_outcome({"settlement_value_dollars": "0.0000"})[0], "NO")
        self.assertEqual(normalized_market_outcome({"status": "closed"})[0], None)

    def test_backfill_writes_only_resolved_rows_by_default_without_mutating_inputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "lane.jsonl"
            output_path = Path(tmpdir) / "out.jsonl"
            self._write_jsonl(
                input_path,
                [
                    {"market_id": "KXHIGHSEA-26MAY17-T70"},
                    {"market_id": "KXLOWSEA-26MAY17-B55.5"},
                ],
            )
            before = input_path.read_bytes()

            def fetch(market_id: str):
                if market_id.endswith("T70"):
                    return {
                        "ticker": market_id,
                        "status": "finalized",
                        "result": "yes",
                        "settlement_value_dollars": "1.0000",
                    }
                return {"ticker": market_id, "status": "closed", "result": ""}

            result = backfill_scoreboard_resolutions(
                [input_path],
                output_path=output_path,
                fetch_market=fetch,
                fetched_at="2026-05-20T00:00:00+00:00",
            )
            rows = load_jsonl(output_path)
            after = input_path.read_bytes()

        self.assertEqual(after, before)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["market_id"], "KXHIGHSEA-26MAY17-T70")
        self.assertEqual(rows[0]["resolution"]["outcome"], "YES")
        self.assertTrue(rows[0]["non_mutating"])
        self.assertEqual(result.report["markets_requested"], 2)
        self.assertEqual(result.report["resolution_rows_written"], 1)
        self.assertEqual(result.report["unresolved_market_count"], 1)

    def test_backfill_retries_429_before_marking_fetch_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "lane.jsonl"
            output_path = Path(tmpdir) / "out.jsonl"
            self._write_jsonl(input_path, [{"market_id": "KXHIGHSEA-26MAY17-T70"}])
            calls = []
            sleeps = []

            def fetch(market_id: str):
                calls.append(market_id)
                if len(calls) == 1:
                    raise FakeHttpError(429, retry_after="0.25")
                return {
                    "ticker": market_id,
                    "status": "finalized",
                    "result": "yes",
                    "settlement_value_dollars": "1.0000",
                }

            result = backfill_scoreboard_resolutions(
                [input_path],
                output_path=output_path,
                fetch_market=fetch,
                fetched_at="2026-05-20T00:00:00+00:00",
                sleep_fn=sleeps.append,
            )
            rows = load_jsonl(output_path)

        self.assertEqual(calls, ["KXHIGHSEA-26MAY17-T70", "KXHIGHSEA-26MAY17-T70"])
        self.assertEqual(sleeps, [0.25])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["resolution"]["outcome"], "YES")
        self.assertEqual(result.report["fetch_error_count"], 0)
        self.assertEqual(result.report["retryable_fetch_error_count"], 1)

    def test_backfill_reports_429_after_retry_budget_is_exhausted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "lane.jsonl"
            output_path = Path(tmpdir) / "out.jsonl"
            self._write_jsonl(input_path, [{"market_id": "KXHIGHSEA-26MAY17-T70"}])
            sleeps = []

            result = backfill_scoreboard_resolutions(
                [input_path],
                output_path=output_path,
                fetch_market=lambda market_id: (_ for _ in ()).throw(FakeHttpError(429)),
                fetched_at="2026-05-20T00:00:00+00:00",
                max_fetch_attempts=2,
                retry_delay_seconds=0,
                sleep_fn=sleeps.append,
            )
            rows = load_jsonl(output_path)

        self.assertEqual(rows, [])
        self.assertEqual(sleeps, [])
        self.assertEqual(result.report["fetch_error_count"], 1)
        self.assertEqual(result.report["fetch_error_samples"][0]["attempts"], 2)
        self.assertEqual(result.report["fetch_error_samples"][0]["retryable"], True)
        self.assertEqual(result.report["retryable_fetch_error_count"], 1)

    def test_include_unresolved_writes_null_outcome_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "lane.jsonl"
            output_path = Path(tmpdir) / "out.jsonl"
            self._write_jsonl(input_path, [{"market_id": "KXHIGHSEA-26MAY17-T70"}])

            backfill_scoreboard_resolutions(
                [input_path],
                output_path=output_path,
                fetch_market=lambda market_id: {"ticker": market_id, "status": "closed"},
                include_unresolved=True,
                fetched_at="2026-05-20T00:00:00+00:00",
            )
            rows = load_jsonl(output_path)

        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["resolution"]["outcome"])

    def test_cli_rejects_wallet_like_output_path_before_fetching(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "lane.jsonl"
            self._write_jsonl(input_path, [{"market_id": "KXHIGHSEA-26MAY17-T70"}])

            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "scoreboard_resolution_backfill.py"),
                    str(input_path),
                    "--output",
                    "data/paper/risk_state.json",
                    "--max-markets",
                    "0",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("derived backfill output", completed.stderr)

    def test_cli_smoke_writes_empty_derived_outputs_with_zero_market_limit(self):
        output_path = ROOT / "data" / "summaries" / "test_scoreboard_resolution_backfill.jsonl"
        report_path = ROOT / "data" / "summaries" / "test_scoreboard_resolution_backfill.report.json"
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                input_path = Path(tmpdir) / "lane.jsonl"
                self._write_jsonl(input_path, [{"market_id": "KXHIGHSEA-26MAY17-T70"}])
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "scripts" / "scoreboard_resolution_backfill.py"),
                        str(input_path),
                        "--output",
                        str(output_path.relative_to(ROOT)),
                        "--report-output",
                        str(report_path.relative_to(ROOT)),
                        "--max-markets",
                        "0",
                    ],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    check=True,
                )

            self.assertIn("requested=0", completed.stdout)
            self.assertEqual(load_jsonl(output_path), [])
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["unique_markets_found"], 1)
            self.assertEqual(report["markets_requested"], 0)
        finally:
            output_path.unlink(missing_ok=True)
            report_path.unlink(missing_ok=True)

    def test_unified_backfill_cli_runs_scoreboard_resolution_kind(self):
        output_path = ROOT / "data" / "summaries" / "test_unified_scoreboard_resolution_backfill.jsonl"
        report_path = ROOT / "data" / "summaries" / "test_unified_scoreboard_resolution_backfill.report.json"
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                input_path = Path(tmpdir) / "lane.jsonl"
                self._write_jsonl(input_path, [{"market_id": "KXHIGHSEA-26MAY17-T70"}])
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "scripts" / "backfill.py"),
                        "--kind",
                        "scoreboard-resolutions",
                        "--lane",
                        "shadow_source_scoreboard",
                        str(input_path),
                        "--output",
                        str(output_path.relative_to(ROOT)),
                        "--report-output",
                        str(report_path.relative_to(ROOT)),
                        "--max-markets",
                        "0",
                    ],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    check=True,
                )

            self.assertIn("kind=scoreboard-resolutions", completed.stdout)
            self.assertIn("lane=shadow_source_scoreboard", completed.stdout)
            self.assertEqual(load_jsonl(output_path), [])
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["unique_markets_found"], 1)
            self.assertEqual(report["markets_requested"], 0)
        finally:
            output_path.unlink(missing_ok=True)
            report_path.unlink(missing_ok=True)

    def test_unified_backfill_cli_rejects_wallet_like_scoreboard_output_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "lane.jsonl"
            self._write_jsonl(input_path, [{"market_id": "KXHIGHSEA-26MAY17-T70"}])

            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "backfill.py"),
                    "--kind",
                    "scoreboard-resolutions",
                    str(input_path),
                    "--output",
                    "data/paper/risk_state.json",
                    "--max-markets",
                    "0",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("derived backfill output", completed.stderr)
