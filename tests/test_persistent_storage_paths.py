"""Persistent beta storage is independent of the source checkout and CWD."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from bot.config import load_config
from bot.shared_market_runtime import shared_market_runtime_root


class PersistentStoragePathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = self.root / "collector"
        self.env = patch.dict(os.environ, {"PREDICTION_BOT_COLLECTOR_ROOT": str(self.storage)}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(os.chdir, Path.cwd())
        self.cwd_a = self.root / "checkout-a"
        self.cwd_b = self.root / "checkout-b"
        self.cwd_a.mkdir()
        self.cwd_b.mkdir()

    def write_config(self, value, name="config.yaml"):
        path = self.root / name
        path.write_text(yaml.safe_dump(value))
        return path

    def test_beta_raw_and_derived_paths_share_storage_across_cwds(self):
        cohort = "data/beta_shadow/cohort"
        for overlay in ("paper_beta_shadow_runtime", "prediction_lab_beta_shadow_runtime"):
            with self.subTest(overlay=overlay):
                base = self.write_config({"config_composition": {"overlays": [overlay]}}, "base.yaml")
                cfg_path = self.write_config({
                    "config_composition": {"base": base.name},
                    "runtime": {"base_dir": cohort},
                    "shared_market": {"runtime_root": f"{cohort}/shared_market_runtime"},
                    "prediction_lab": {"replay_index": {"root_dir": str(self.storage / cohort / "replay_index")}},
                    "resolution_feed": {
                        "market_ref_paths": [str(self.storage / cohort / "paper/prediction_lab/market_snapshots.jsonl")],
                        "output_dir": f"{cohort}/resolution_feed",
                        "central_output_dir": f"{cohort}/resolutions",
                    },
                })
                snapshots = []
                for cwd in (self.cwd_a, self.cwd_b):
                    os.chdir(cwd)
                    cfg = load_config(cfg_path)
                    snapshots.append({
                        "base": Path(cfg["runtime"]["base_dir"]).resolve(),
                        "raw": (Path(cfg["data_dir"]) / "prediction_lab/market_snapshots.jsonl").resolve(),
                        "shared": shared_market_runtime_root(cfg).resolve(),
                        "index": Path(cfg["prediction_lab"]["replay_index"]["root_dir"]).resolve(),
                        "resolver_input": Path(cfg["resolution_feed"]["market_ref_paths"][0]).resolve(),
                        "resolver_output": Path(cfg["resolution_feed"]["output_dir"]).resolve(),
                        "resolutions": Path(cfg["resolution_feed"]["central_output_dir"]).resolve(),
                    })
                self.assertEqual(snapshots[0], snapshots[1])
                paths = snapshots[0]
                self.assertEqual(paths["base"], self.storage / cohort)
                self.assertEqual(paths["raw"], paths["resolver_input"])
                self.assertEqual(paths["index"].parent, paths["base"])
                self.assertTrue(all(path.is_relative_to(self.storage) for path in paths.values()))
                self.assertEqual(cfg["runtime"]["storage_root"], str(self.storage))
                self.assertFalse(self.storage.exists(), "loading config must not create runtime storage")

    def test_wallet_overrides_are_anchored_before_contracts_are_built(self):
        from bot.paper_wallets import resolve_paper_wallet_contract

        for stable_key, beta_key in (("stable_paper", "beta_paper"), ("stable", "beta")):
            with self.subTest(aliases=(stable_key, beta_key)):
                cfg = load_config(self.write_config({
                    "paper_wallets": {
                        stable_key: {"root_dir": "wallets/control"},
                        beta_key: {"base_dir": "wallets/challenger"},
                    },
                }))
                for wallet_id, relative in (("stable_paper", "wallets/control"), ("beta_paper", "wallets/challenger")):
                    contract = resolve_paper_wallet_contract(cfg, wallet_id=wallet_id, session_id="fixture")
                    self.assertEqual(contract.root_dir, self.storage / relative / "paper")
                    self.assertEqual(contract.risk_state_path, contract.root_dir / "risk_state.json")
                    self.assertEqual(contract.session_path, contract.root_dir / "sim_fixture.json")
                self.assertFalse(self.storage.exists())

    def test_resolution_inputs_outputs_and_globs_use_storage_root(self):
        from bot.resolution_feed import DEFAULT_OUTPUT_DIR, normalize_resolution_feed_config

        path_keys = (
            "output_dir", "central_output_dir", "canonical_output_dir", "decision_ledger_path", "ledger_path",
            "decision_ledger_paths", "ledger_paths", "market_ref_paths", "collector_market_paths",
            "decision_ledger_globs", "decision_ledger_path_globs", "ledger_path_globs",
        )
        for nested in (False, True):
            for key in path_keys:
                for value in ("data/input*.jsonl", ["data/input*.jsonl", str(self.root / "absolute.jsonl")]):
                    if isinstance(value, list) and key in path_keys[:5]:
                        continue
                    with self.subTest(nested=nested, key=key, value=value):
                        feed = {key: value}
                        raw = {"prediction_lab": {"resolution_feed": feed}} if nested else {"resolution_feed": feed}
                        cfg = load_config(self.write_config(raw))
                        section = cfg["prediction_lab"]["resolution_feed"] if nested else cfg["resolution_feed"]
                        expected = [str(self.storage / item) for item in value] if isinstance(value, list) else str(self.storage / value)
                        self.assertEqual(section[key], expected)
            with self.subTest(default_output=nested):
                raw = {"prediction_lab": {"resolution_feed": {"enabled": True}}} if nested else {"resolution_feed": {"enabled": True}}
                normalized = normalize_resolution_feed_config(load_config(self.write_config(raw)))
                self.assertEqual(normalized["output_dir"], str(self.storage / DEFAULT_OUTPUT_DIR))
                self.assertEqual(normalized["central_output_dir"], normalized["output_dir"])
        cfg = load_config(self.write_config({
            "prediction_lab": {"resolution_feed": {"output_dir": "nested/output"}},
            "resolution_feed": {"central_output_dir": "top/central"},
        }))
        normalized = normalize_resolution_feed_config(cfg)
        self.assertEqual(normalized["output_dir"], str(self.storage / "nested/output"))
        self.assertEqual(normalized["central_output_dir"], str(self.storage / "top/central"))

    def test_lane_scoreboard_and_ledger_consumers_use_storage_root(self):
        from bot.paper_shadow_lanes import _configured_lane_definition_map, _lane_decision_path

        for section_name in ("paper_shadow_lanes", "paper_decision_lanes"):
            for field in ("scoreboard_path", "source_reliability_scoreboard", "source_reliability_scoreboard_path", "source_scoreboard_path"):
                lane = {"parameters": {field: "data/derived_reports/current/scoreboard.jsonl"}}
                for route in ("inline", "lanes", "enabled_lanes", "top_level"):
                    if field == "scoreboard_path" and route == "top_level":
                        continue  # No global scoreboard_path alias in the consumer.
                    with self.subTest(section=section_name, field=field, route=route):
                        shadow: dict = {"decision_ledger_path": "data/lane_decisions.jsonl", "definitions_dir": "lanes"}
                        if route == "inline":
                            shadow["shadow_source_router"] = {field: lane["parameters"][field]}
                        elif route == "top_level":
                            shadow[field] = lane["parameters"][field]
                        else:
                            shadow[route] = {"shadow_source_router": lane}
                        cfg = load_config(self.write_config({section_name: shadow}))
                        normalized = cfg[section_name]
                        resolved = _configured_lane_definition_map(normalized)["shadow_source_router"]
                        self.assertEqual(resolved.parameters[field if route != "top_level" else "scoreboard_path"], str(self.storage / lane["parameters"][field]))
                        self.assertEqual(_lane_decision_path(normalized, ledger_root=self.cwd_a), self.storage / "data/lane_decisions.jsonl")
                        self.assertEqual(normalized["definitions_dir"], "lanes")

    def test_log_storage_and_observations_do_not_follow_checkout(self):
        from bot.status import _collect_log_storage_entries

        included = self.storage / "ops/report.log"
        excluded = self.storage / "ops/excluded.log"
        included.parent.mkdir(parents=True)
        included.write_text("fixture log\n")
        excluded.write_text("excluded fixture\n")
        for observation, expected in (
            (None, "data/paper/weather_observations.jsonl"),
            ("data/weather_observations.jsonl", "data/paper/weather_observations.jsonl"),
            ("observations/custom.jsonl", "observations/custom.jsonl"),
            (str(self.root / "absolute.jsonl"), str(self.root / "absolute.jsonl")),
        ):
            with self.subTest(observation=observation):
                strategy = {} if observation is None else {"weather_observation_log_path": observation}
                cfg = load_config(self.write_config({
                    "strategy": strategy,
                    "storage": {"logs": {"include_paths": ["ops/"], "exclude_paths": ["ops/excluded.log"]}},
                }))
                self.assertEqual(cfg["strategy"].get("weather_observation_log_path"), str(self.storage / expected))
                for cwd in (self.cwd_a, self.cwd_b):
                    _, _, entries = _collect_log_storage_entries(cfg, project_root=cwd)
                    self.assertEqual([entry[0] for entry in entries], [included])
                self.assertEqual(Path(cfg["logging"]["log_dir"]), self.storage / "data/paper")

    def test_candidate_dataset_overrides_reach_the_snapshot_consumer(self):
        from bot.simulator import Simulator

        for key in ("market_snapshots_path", "candidate_dataset_path"):
            with self.subTest(key=key):
                cfg = load_config(self.write_config({
                    "prediction_lab": {key: "data/collector/market_snapshots.jsonl"},
                    "paper_candidate_dataset_path": "data/candidates.jsonl",
                }))
                # Exercise the path-only consumer without initializing accounting.
                consumer = Simulator.__new__(Simulator)
                consumer.config = cfg
                consumer.data_dir = Path(cfg["data_dir"])
                selected = consumer._shared_market_snapshot_candidate_source_path({})
                self.assertEqual(selected, self.storage / "data/collector/market_snapshots.jsonl")
                self.assertEqual(cfg["paper_candidate_dataset_path"], str(self.storage / "data/candidates.jsonl"))

    def test_relative_storage_root_is_rejected_instead_of_using_cwd(self):
        for env_value, configured in (("relative-volume", None), (None, "relative-volume")):
            with self.subTest(env=env_value, configured=configured):
                env = {} if env_value is None else {"PREDICTION_BOT_COLLECTOR_ROOT": env_value}
                with patch.dict(os.environ, env, clear=True):
                    path = self.write_config({"runtime": {"storage_root": configured}})
                    with self.assertRaisesRegex(ValueError, "storage root must be absolute"):
                        load_config(path)

    def test_root_precedence_is_evaluated_on_each_load(self):
        from bot.collector_paths import DEFAULT_COLLECTOR_ROOT, derived_reports_root

        cfg_path = self.write_config({
            "config_composition": {"overlays": ["paper_beta_shadow_runtime"]},
            "runtime": {"storage_root": str(self.root / "configured")},
        })
        for selected in (self.storage, self.root / "second-volume"):
            os.environ["PREDICTION_BOT_COLLECTOR_ROOT"] = str(selected)
            cfg = load_config(cfg_path)
            self.assertEqual(cfg["runtime"]["storage_root"], str(selected))
            self.assertEqual(Path(cfg["data_dir"]), selected / "data/beta_shadow/paper")
            self.assertEqual(derived_reports_root(), selected / "data/derived_reports")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_config(cfg_path)["runtime"]["storage_root"], str(self.root / "configured"))
            for overlay in ("paper_beta_shadow_runtime", "prediction_lab_beta_shadow_runtime"):
                cfg = load_config(self.write_config({"config_composition": {"overlays": [overlay]}}))
                self.assertEqual(Path(cfg["runtime"]["storage_root"]), DEFAULT_COLLECTOR_ROOT)
                self.assertEqual(Path(cfg["data_dir"]), DEFAULT_COLLECTOR_ROOT / "data/beta_shadow/paper")

    def test_loaded_config_storage_authority_reaches_actual_promotion(self):
        from bot.auto_source_router_promotion import auto_populate_source_router_history
        from tests.test_auto_source_router_promotion import _collector_row, _strict_resolution, _write_jsonl

        configured = self.root / "configured"
        cfg_path = self.write_config({
            "runtime": {"storage_root": str(configured)},
            "derived_maintenance": {"source_router_promotion": {
                "collector_snapshots_path": "data/snapshots.jsonl",
                "strict_resolutions_path": "data/resolutions.jsonl",
            }},
        })
        for env, selected in (({}, configured), ({"PREDICTION_BOT_COLLECTOR_ROOT": str(self.storage)}, self.storage)):
            with self.subTest(selected=selected), patch.dict(os.environ, env, clear=True):
                cfg = load_config(cfg_path)
                promotion = cfg["derived_maintenance"]["source_router_promotion"]
                row = _collector_row()
                snapshots = Path(promotion["collector_snapshots_path"])
                resolutions = Path(promotion["strict_resolutions_path"])
                _write_jsonl(snapshots, [row])
                _write_jsonl(resolutions, [_strict_resolution(row["market_id"])])
                original = (snapshots.read_bytes(), resolutions.read_bytes())
                kwargs = dict(promotion, storage_root=cfg["runtime"]["storage_root"])
                result = auto_populate_source_router_history(**kwargs)
                expected = selected / "data/derived_reports/auto_source_router_history"
                self.assertEqual(result.status, "history_ready")
                self.assertTrue(result.generation_dir.is_relative_to(expected))
                self.assertEqual((expected / "current").resolve(), result.generation_dir)
                self.assertTrue(auto_populate_source_router_history(**kwargs).reused)
                self.assertEqual((snapshots.read_bytes(), resolutions.read_bytes()), original)
                # The default destination and explicit destination share authority.
                kwargs.pop("output_root")
                self.assertEqual(auto_populate_source_router_history(**kwargs).generation_dir, result.generation_dir)
                # Output paths cannot grant themselves authority, even on the same volume.
                for forbidden in (selected / "data/derived_reports", selected / "raw/history", self.root / "outside"):
                    with self.assertRaisesRegex(ValueError, "output root must be below"):
                        auto_populate_source_router_history(**kwargs, output_root=forbidden)
                if not env:
                    with self.assertRaisesRegex(ValueError, "output root must be below"):
                        auto_populate_source_router_history(**promotion)

    def test_strict_chronology_contract_does_not_reuse_v1_generation(self):
        from bot import auto_source_router_promotion as pipeline
        from tests.test_auto_source_router_promotion import _collector_row, _strict_resolution, _write_jsonl

        snapshots, resolutions = self.root / "snapshots.jsonl", self.root / "resolutions.jsonl"
        row = _collector_row()
        _write_jsonl(snapshots, [row])
        _write_jsonl(resolutions, [_strict_resolution(row["market_id"])])
        kwargs = dict(collector_snapshots_path=snapshots, strict_resolutions_path=resolutions)
        # A complete prior-schema generation with byte-identical inputs must not
        # bypass the changed materializer simply because its hashes still match.
        with patch.object(pipeline, "PIPELINE_SCHEMA_VERSION", 1):
            previous = pipeline.auto_populate_source_router_history(**kwargs)
        previous_bytes = {path: path.read_bytes() for path in previous.generation_dir.rglob("*") if path.is_file()}
        current = pipeline.auto_populate_source_router_history(**kwargs)
        self.assertFalse(current.reused)
        self.assertNotEqual(current.generation_dir, previous.generation_dir)
        self.assertEqual({path: path.read_bytes() for path in previous_bytes}, previous_bytes)
        self.assertEqual((current.generation_dir.parent.parent / "current").resolve(), current.generation_dir)
        self.assertTrue(pipeline.auto_populate_source_router_history(**kwargs).reused)

    def test_legacy_non_beta_config_stays_relative_without_explicit_root(self):
        cfg_path = self.write_config({
            "runtime": {"base_dir": "legacy-data"},
            "shared_market": {"runtime_root": "legacy-shared"},
            "strategy": {"weather_observation_log_path": "data/weather_observations.jsonl"},
            "resolution_feed": {"output_dir": "legacy-feed"},
        })
        with patch.dict(os.environ, {}, clear=True):
            for cwd in (self.cwd_a, self.cwd_b):
                os.chdir(cwd)
                cfg = load_config(cfg_path)
                self.assertEqual(cfg["runtime"]["base_dir"], "legacy-data")
                self.assertEqual(cfg["data_dir"], "legacy-data/paper")
                self.assertEqual(cfg["shared_market"]["runtime_root"], "legacy-shared")
                self.assertEqual(cfg["strategy"]["weather_observation_log_path"], "legacy-data/paper/weather_observations.jsonl")
                self.assertEqual(cfg["resolution_feed"]["output_dir"], "legacy-feed")
                self.assertNotIn("storage_root", cfg["runtime"])
                self.assertEqual(cfg["storage"]["logs"]["include_paths"][0], "data/paper_loop.log")
            cfg = load_config(self.write_config({"runtime": {"storage_root": str(self.storage)}}))
            self.assertEqual(Path(cfg["data_dir"]), self.storage / "data/paper")

    def test_absolute_paths_code_paths_and_provenance_are_preserved(self):
        base = self.root / "absolute-cohort"
        provenance = {"source_path": "data/historical.jsonl", "scoreboard_path": "data/historical_scoreboard.jsonl"}
        cfg = load_config(self.write_config({
            "runtime": {"base_dir": str(base)},
            "shared_market": {"runtime_root": str(base / "shared")},
            "prediction_lab": {"replay_index": {"root_dir": str(base / "index")}},
            "paper_shadow_lanes": {"definitions_dir": "lanes", "provenance": provenance},
            "source_history": {"manifest_path": "data/unchanged_manifest.json"},
            "provenance": provenance,
        }))
        self.assertEqual(cfg["runtime"]["base_dir"], str(base))
        self.assertEqual(cfg["data_dir"], str(base / "paper"))
        self.assertEqual(cfg["shared_market"]["runtime_root"], str(base / "shared"))
        self.assertEqual(cfg["prediction_lab"]["replay_index"]["root_dir"], str(base / "index"))
        self.assertEqual(cfg["paper_shadow_lanes"], {"definitions_dir": "lanes", "provenance": provenance})
        self.assertEqual(cfg["provenance"], provenance)
        self.assertEqual(cfg["source_history"], {"manifest_path": "data/unchanged_manifest.json"})

    def test_current_generation_symlinks_are_not_pinned_during_config_load(self):
        generation = self.storage / "data/derived_reports/generation-a"
        generation.mkdir(parents=True)
        current = generation.parent / "current"
        current.symlink_to(generation, target_is_directory=True)
        for path in ("data/derived_reports/current/scoreboard.jsonl", str(current / "scoreboard.jsonl")):
            cfg = load_config(self.write_config({"paper_shadow_lanes": {"source_scoreboard_path": path}}))
            self.assertEqual(cfg["paper_shadow_lanes"]["source_scoreboard_path"], str(current / "scoreboard.jsonl"))

    def test_empty_optional_sections_keep_consumer_defaults(self):
        cfg = load_config(self.write_config({
            "shared_market": None,
            "prediction_lab": {"replay_index": None, "resolution_feed": None},
            "resolution_feed": None,
            "paper_shadow_lanes": None,
            "paper_wallets": None,
        }))
        self.assertEqual(shared_market_runtime_root(cfg), self.storage / "data/shared_market_runtime")

    def test_derived_maintenance_paths_and_default_promotion_root_are_persistent(self):
        maintenance = {
            "enabled": False,
            "state_path": "data/maintenance/state.json",
            "source_router_promotion": {
                "enabled": False,
                "state_path": "data/promotion/state.json",
                "collector_snapshots_path": "data/cohort/market_snapshots.jsonl",
                "strict_resolutions_path": str(self.root / "absolute-resolutions.jsonl"),
                "output_root": "data/derived_reports/custom-history",
            },
        }
        cfg_path = self.write_config({"derived_maintenance": maintenance})
        for cwd in (self.cwd_a, self.cwd_b):
            with self.subTest(cwd=cwd):
                os.chdir(cwd)
                cfg = load_config(cfg_path)["derived_maintenance"]
                self.assertEqual(cfg["state_path"], str(self.storage / maintenance["state_path"]))
                for key, value in maintenance["source_router_promotion"].items():
                    if key != "enabled":
                        self.assertEqual(cfg["source_router_promotion"][key], str(self.storage / value))
        for promotion in ({}, {"output_root": None}, {"output_root": ""}):
            with self.subTest(promotion=promotion), patch.dict(os.environ, {}, clear=True):
                cfg = load_config(self.write_config({
                    "runtime": {"storage_root": str(self.storage)},
                    "derived_maintenance": {"source_router_promotion": promotion},
                }))
                self.assertEqual(
                    cfg["derived_maintenance"]["source_router_promotion"].get("output_root"),
                    str(self.storage / "data/derived_reports/auto_source_router_history"),
                )
                self.assertNotIn("state_path", cfg["derived_maintenance"], "required maintenance state must not be invented")
        self.assertFalse(self.storage.exists(), "config loading must not activate maintenance")


if __name__ == "__main__":
    unittest.main()
