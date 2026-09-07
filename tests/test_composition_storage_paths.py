"""Composition diagnostics keep data outside their source checkout."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import paper_shadow_lane_compose_replay as compose
from scripts import paper_shadow_lane_composition_sweep as sweep


class CompositionStoragePathTests(unittest.TestCase):
    def test_default_outputs_follow_current_collector_root_not_import_time_root(self):
        for _ in range(2):
            with tempfile.TemporaryDirectory() as tmp, patch.dict(
                os.environ, {"PREDICTION_BOT_COLLECTOR_ROOT": tmp}
            ):
                expected = Path(tmp) / "data" / "derived_reports" / "lane_compositions"
                self.assertEqual(compose._output_dir(None, "fixture").parent, expected)
                self.assertEqual(sweep._sweep_output_dir(None).parent, expected)

    def test_both_cli_text_modes_accept_persistent_derived_outputs(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"PREDICTION_BOT_COLLECTOR_ROOT": tmp}
        ):
            root = Path(tmp)
            lanes = root / "empty-lanes.jsonl"
            lanes.write_text("")
            config = root / "composition.json"
            config.write_text(json.dumps({"composition": {"name": "fixture", "base_lane": "control_stable"}}))
            for module in (compose, sweep):
                with self.subTest(module=module.__name__):
                    output = root / "data" / "derived_reports" / module.__name__.rsplit(".", 1)[-1]
                    with contextlib.redirect_stdout(io.StringIO()) as stdout:
                        result = module.main([
                            "--lane-decision-path", str(lanes),
                            "--composition-config", str(config),
                            "--output-dir", str(output),
                        ])
                    self.assertEqual(result, 0)
                    self.assertTrue((output / "summary.json").is_file())
                    self.assertIn(str(output), stdout.getvalue())

    def test_relative_data_inputs_use_storage_but_config_inputs_use_code(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"PREDICTION_BOT_COLLECTOR_ROOT": tmp}
        ):
            self.assertEqual(compose._data_path(compose.DEFAULT_LANE_DECISION_PATH), Path(tmp) / compose.DEFAULT_LANE_DECISION_PATH)
            self.assertEqual(compose._root_path("lane_compositions/example.yaml"), compose.ROOT / "lane_compositions/example.yaml")
            explicit = Path(tmp) / "archived-lanes.jsonl"
            self.assertEqual(compose._data_path(explicit), explicit)

    def test_unified_corpus_discovers_persistent_composition_outputs(self):
        from scripts import unified_replay_corpus as corpus
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"PREDICTION_BOT_COLLECTOR_ROOT": tmp}
        ), patch.object(corpus, "ROOT", Path(tmp) / "code"):
            receipt = Path(tmp) / "data" / "derived_reports" / "lane_compositions" / "fixture" / "resolved_rows.jsonl"
            receipt.parent.mkdir(parents=True)
            receipt.write_text("")
            paths = corpus._discover_paths(explicit=[], patterns=[], defaults=corpus.DEFAULT_RESOLVED_REPLAY_GLOBS, include_defaults=True)
            self.assertEqual(paths, [receipt])

    def test_raw_storage_output_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"PREDICTION_BOT_COLLECTOR_ROOT": tmp}
        ):
            raw = Path(tmp) / "data" / "beta_shadow" / "raw"
            with self.assertRaises(ValueError):
                compose._output_dir(str(raw), "fixture")
            with self.assertRaises(ValueError):
                sweep._sweep_output_dir(str(raw))
