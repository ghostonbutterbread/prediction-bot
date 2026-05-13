import tempfile
import unittest
from pathlib import Path

from scripts.partition_jsonl_by_month import partition_jsonl_by_month

from bot.file_ops import append_jsonl
from bot.prediction_lab_replay import (
    discover_prediction_lab_replay_datasets,
    explicit_prediction_lab_replay_window,
    select_prediction_lab_replay_window,
)


def _write_month(path: Path, month: str, *, rows: int = 1) -> Path:
    for index in range(rows):
        append_jsonl(
            path,
            {
                "market_id": f"KX-{month}-{index}",
                "observed_at": f"{month}-{index + 1:02d}T12:00:00+00:00",
                "decision_artifact": {"market_id": f"KX-{month}-{index}", "final_action": "SKIP"},
            },
        )
    return path


class PredictionLabReplayWindowSelectionTest(unittest.TestCase):
    def test_trailing_month_window_anchors_on_newest_available_dataset(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_month(root / "jan" / "market_snapshots.jsonl", "2026-01")
            _write_month(root / "mar" / "market_snapshots.jsonl", "2026-03")
            _write_month(root / "apr" / "market_snapshots.jsonl", "2026-04")

            window = select_prediction_lab_replay_window([root], months=2)

            self.assertEqual(window.selection_mode, "months")
            self.assertEqual(window.requested_months, 2)
            self.assertEqual(window.available_months, 3)
            self.assertEqual(window.selected_months, 2)
            self.assertEqual(window.anchor_month, "2026-04")
            self.assertEqual(window.selected_month_list, ("2026-03", "2026-04"))
            self.assertIsNone(window.fallback_reason)

    def test_requested_months_exceeding_available_falls_back_to_all_available(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_month(root / "feb" / "market_snapshots.jsonl", "2026-02")
            _write_month(root / "apr" / "market_snapshots.jsonl", "2026-04")

            window = select_prediction_lab_replay_window([root], months=6)

            self.assertEqual(window.selected_months, 2)
            self.assertEqual(window.selected_month_list, ("2026-02", "2026-04"))
            self.assertEqual(window.fallback_reason, "requested_window_exceeds_available_data")

    def test_months_all_selects_every_available_month(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_month(root / "jan" / "market_snapshots.jsonl", "2026-01")
            _write_month(root / "feb" / "market_snapshots.jsonl", "2026-02")
            _write_month(root / "mar" / "market_snapshots.jsonl", "2026-03")

            window = select_prediction_lab_replay_window([root], months="all")

            self.assertEqual(window.selection_mode, "months_all")
            self.assertEqual(window.requested_months, "all")
            self.assertEqual(window.selected_months, 3)
            self.assertEqual(window.selected_month_list, ("2026-01", "2026-02", "2026-03"))

    def test_overlapping_months_are_warned_and_deduped_by_month(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            first = _write_month(root / "first" / "market_snapshots.jsonl", "2026-05", rows=1)
            second = _write_month(root / "second" / "market_snapshots.jsonl", "2026-05", rows=2)

            window = select_prediction_lab_replay_window([root], months=1)

            self.assertEqual(window.selected_months, 1)
            self.assertEqual(window.datasets, (str(second),))
            self.assertIn("overlapping_replay_datasets_for_month:2026-05", " ".join(window.warnings))
            self.assertNotIn(str(first), window.datasets)

    def test_invalid_and_partial_datasets_warn_continue_and_recommend_backfill(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            valid = _write_month(root / "valid" / "market_snapshots.jsonl", "2026-01")
            partial = _write_month(root / "partial" / "market_snapshots.jsonl", "2026-02")
            append_jsonl(partial, {"market_id": "KX-MISSING-TIME", "decision_artifact": {"final_action": "SKIP"}})
            append_jsonl(root / "invalid" / "market_snapshots.jsonl", {"market_id": "KX-NO-TIME"})

            datasets = discover_prediction_lab_replay_datasets([root])
            qualities = {Path(dataset.path).parent.name: dataset.quality for dataset in datasets}
            window = select_prediction_lab_replay_window([root], months="all")

            self.assertEqual(qualities["valid"], "valid")
            self.assertEqual(qualities["partial"], "partial")
            self.assertEqual(qualities["invalid"], "invalid")
            self.assertEqual(window.selected_month_list, ("2026-01", "2026-02"))
            self.assertIn(str(valid), window.datasets)
            self.assertIn(str(partial), window.datasets)
            self.assertTrue(window.backfill_recommendation)
            self.assertIn("rows_missing_observed_timestamp", " ".join(window.warnings))

    def test_partitioned_monthly_shards_are_discovered_after_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "market_snapshots.jsonl"
            _write_month(source, "2026-04")
            _write_month(source, "2026-05")
            report = partition_jsonl_by_month(source, write=True)

            datasets = discover_prediction_lab_replay_datasets([Path(report["output_dir"])])
            window = select_prediction_lab_replay_window([Path(report["output_dir"])], months="all")

            self.assertEqual({Path(dataset.path).name for dataset in datasets}, {"market_snapshots-2026-04.jsonl", "market_snapshots-2026-05.jsonl"})
            self.assertEqual(window.selected_month_list, ("2026-04", "2026-05"))
            self.assertEqual(len(window.datasets), 2)
            self.assertTrue(all(Path(path).name.startswith("market_snapshots-2026-") for path in window.datasets))

    def test_partitioner_date_fields_are_replay_discoverable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "market_snapshots.jsonl"
            append_jsonl(
                source,
                {
                    "market_id": "KX-DECIDED",
                    "decision_type": "skip",
                    "decided_at": "2026-06-03T12:00:00+00:00",
                    "decision_artifact": {"market_id": "KX-DECIDED", "final_action": "SKIP"},
                },
            )
            report = partition_jsonl_by_month(source, write=True)

            window = select_prediction_lab_replay_window([Path(report["output_dir"])], months=1)

            self.assertEqual(window.selected_month_list, ("2026-06",))
            self.assertEqual(Path(window.datasets[0]).name, "market_snapshots-2026-06.jsonl")
            self.assertEqual(window.dataset_details[0].row_count, 1)

    def test_explicit_path_window_preserves_input_paths_without_discovery(self):
        paths = ["/tmp/prediction_lab/market_snapshots.jsonl", "/tmp/prediction_lab/predictions.jsonl"]

        window = explicit_prediction_lab_replay_window(paths)

        self.assertEqual(window.selection_mode, "explicit_path")
        self.assertEqual(window.datasets, tuple(paths))
        self.assertIsNone(window.fallback_reason)


class PredictionLabReplayCliDefaultRootsTest(unittest.TestCase):
    def test_default_replay_months_is_two_unless_config_overrides(self):
        from scripts.prediction_lab_replay import _default_replay_months

        self.assertEqual(_default_replay_months({"prediction_lab": {}}), 2)
        self.assertEqual(_default_replay_months({"prediction_lab": {"replay_default_months": 3}}), 3)

    def test_default_two_month_window_uses_two_one_month_shards(self):
        from scripts.prediction_lab_replay import _default_replay_months, _default_replay_roots

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir) / "data"
            active = base / "paper" / "prediction_lab" / "market_snapshots.jsonl"
            _write_month(active, "2026-03")
            _write_month(active, "2026-04")
            _write_month(active, "2026-05")
            report = partition_jsonl_by_month(active, write=True)
            roots = _default_replay_roots({"runtime": {"base_dir": str(base)}, "prediction_lab": {}})
            window = select_prediction_lab_replay_window(roots, months=_default_replay_months({"prediction_lab": {}}))

            self.assertEqual(roots, [Path(report["output_dir"])])
            self.assertEqual(window.requested_months, 2)
            self.assertEqual(window.selected_month_list, ("2026-04", "2026-05"))
            self.assertEqual({Path(path).name for path in window.datasets}, {"market_snapshots-2026-04.jsonl", "market_snapshots-2026-05.jsonl"})
            self.assertTrue(all(len(dataset.months) == 1 for dataset in window.dataset_details))

    def test_default_replay_roots_use_runtime_base_dir_prediction_lab_monthly_when_present(self):
        from scripts.prediction_lab_replay import _default_replay_roots

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            monthly = root / "data" / "paper" / "prediction_lab" / "monthly" / "market_snapshots"
            monthly.mkdir(parents=True)

            roots = _default_replay_roots({"runtime": {"base_dir": str(root / "data")}, "prediction_lab": {}})

            self.assertEqual(roots, [monthly])

    def test_default_replay_roots_fallback_to_active_market_snapshots_for_runtime_base_dir(self):
        from scripts.prediction_lab_replay import _default_replay_roots

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir) / "data" / "beta_shadow"

            roots = _default_replay_roots({"runtime": {"base_dir": str(base)}, "prediction_lab": {}})

            self.assertEqual(roots, [base / "paper" / "prediction_lab" / "market_snapshots.jsonl"])


if __name__ == "__main__":
    unittest.main()
