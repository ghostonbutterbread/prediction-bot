import json
import tempfile
import unittest
from pathlib import Path

from scripts.partition_jsonl_by_month import partition_jsonl_by_month


class PartitionJsonlByMonthTests(unittest.TestCase):
    def _write_lines(self, path: Path, lines: list[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(lines), encoding="utf-8")

    def _json_line(self, row: dict) -> str:
        return json.dumps(row, sort_keys=True) + "\n"

    def test_dry_run_routes_months_unknown_and_writes_no_outputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "events.jsonl"
            output_root = Path(tmpdir) / "out"
            self._write_lines(
                source,
                [
                    self._json_line({"id": "observed", "observed_at": "2026-05-12T10:00:00Z"}),
                    self._json_line({"id": "timestamp", "timestamp": "2026-06-01T01:00:00-04:00"}),
                    self._json_line(
                        {
                            "id": "fallback",
                            "created_at": "not-a-date",
                            "recorded_at": "2026-07-02T00:00:00+00:00",
                        }
                    ),
                    self._json_line({"id": "unknown", "started_at": "not-a-date"}),
                    "{bad json\n",
                    "\n",
                ],
            )

            manifest = partition_jsonl_by_month(source, output_root=output_root)

            self.assertFalse(output_root.exists())
            self.assertTrue(manifest["dry_run"])
            self.assertEqual(manifest["lines_read"], 6)
            self.assertEqual(manifest["rows_read"], 5)
            self.assertEqual(manifest["rows_partitioned"], 4)
            self.assertEqual(manifest["bad_json_rows"], 1)
            self.assertEqual(manifest["skipped_bad_json_rows"], 1)
            self.assertEqual(manifest["blank_lines"], 1)
            self.assertEqual(manifest["unknown_date_rows"], 1)
            self.assertEqual(manifest["warnings_count"], 2)
            self.assertEqual(
                manifest["shard_counts"],
                {"2026-05": 1, "2026-06": 1, "2026-07": 1, "unknown": 1},
            )
            self.assertEqual(manifest["date_field_usage_counts"]["observed_at"], 1)
            self.assertEqual(manifest["date_field_usage_counts"]["timestamp"], 1)
            self.assertEqual(manifest["date_field_usage_counts"]["recorded_at"], 1)
            self.assertEqual(manifest["date_field_usage_counts"]["created_at"], 0)
            self.assertEqual(manifest["date_field_usage_counts"]["unknown"], 1)

    def test_write_outputs_shards_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "events.jsonl"
            output_root = Path(tmpdir) / "monthly" / "events"
            may = {"id": "may", "observed_at": "2026-05-12T10:00:00Z"}
            june = {"id": "june", "timestamp": "2026-06-01T01:00:00+00:00"}
            unknown = {"id": "unknown"}
            self._write_lines(source, [self._json_line(may), self._json_line(june), self._json_line(unknown)])

            manifest = partition_jsonl_by_month(source, output_root=output_root, write=True)

            may_path = output_root / "events-2026-05.jsonl"
            june_path = output_root / "events-2026-06.jsonl"
            unknown_path = output_root / "events-unknown.jsonl"
            manifest_path = output_root / "events-partition-manifest.json"
            may_rows = [json.loads(line) for line in may_path.read_text(encoding="utf-8").splitlines()]
            june_rows = [json.loads(line) for line in june_path.read_text(encoding="utf-8").splitlines()]
            unknown_rows = [json.loads(line) for line in unknown_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(may_rows, [may])
            self.assertEqual(june_rows, [june])
            self.assertEqual(unknown_rows, [unknown])
            written_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertFalse(manifest["dry_run"])
            self.assertTrue(manifest["shards_written"])
            self.assertEqual(
                written_manifest["shard_counts"],
                {"2026-05": 1, "2026-06": 1, "unknown": 1},
            )
            self.assertEqual(written_manifest["manifest_path"], str(manifest_path))

    def test_default_output_root_is_sibling_monthly_stem(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "ledger.jsonl"
            self._write_lines(source, [self._json_line({"observed_at": "2026-05-01T00:00:00Z"})])

            manifest = partition_jsonl_by_month(source, write=True)

            expected_dir = Path(tmpdir) / "monthly" / "ledger"
            self.assertEqual(manifest["output_dir"], str(expected_dir))
            self.assertTrue((expected_dir / "ledger-2026-05.jsonl").exists())
            self.assertTrue((expected_dir / "ledger-partition-manifest.json").exists())

    def test_refuses_existing_outputs_without_force_and_force_replaces(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "events.jsonl"
            output_root = Path(tmpdir) / "out"
            self._write_lines(source, [self._json_line({"id": "first", "observed_at": "2026-05-01T00:00:00Z"})])
            partition_jsonl_by_month(source, output_root=output_root, write=True)
            self._write_lines(
                source,
                [self._json_line({"id": "second", "observed_at": "2026-05-02T00:00:00Z"})],
            )

            with self.assertRaises(FileExistsError):
                partition_jsonl_by_month(source, output_root=output_root, write=True)

            manifest = partition_jsonl_by_month(source, output_root=output_root, write=True, force=True)

            shard_rows = [
                json.loads(line)
                for line in (output_root / "events-2026-05.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(shard_rows, [{"id": "second", "observed_at": "2026-05-02T00:00:00Z"}])
            self.assertTrue(manifest["force"])
            self.assertEqual(manifest["shard_counts"], {"2026-05": 1})

    def test_max_rows_limits_nonblank_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "events.jsonl"
            self._write_lines(
                source,
                [
                    "\n",
                    self._json_line({"id": 1, "observed_at": "2026-05-01T00:00:00Z"}),
                    self._json_line({"id": 2, "observed_at": "2026-06-01T00:00:00Z"}),
                    self._json_line({"id": 3, "observed_at": "2026-07-01T00:00:00Z"}),
                ],
            )

            manifest = partition_jsonl_by_month(source, max_rows=2)

            self.assertTrue(manifest["truncated_by_max_rows"])
            self.assertEqual(manifest["rows_read"], 2)
            self.assertEqual(manifest["blank_lines"], 1)
            self.assertEqual(manifest["shard_counts"], {"2026-05": 1, "2026-06": 1})


if __name__ == "__main__":
    unittest.main()
