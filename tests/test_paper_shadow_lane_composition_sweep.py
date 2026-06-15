import json
import tempfile
import unittest
from pathlib import Path

from scripts.paper_shadow_lane_composition_sweep import (
    ROOT,
    _composition_config_paths,
    main,
    run_composition_sweep,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _lane_row(
    *,
    policy: str,
    candidate_id: str,
    market_id: str,
    action: str,
    size: float | None = None,
    yes_price: float | None = None,
    no_price: float | None = None,
) -> dict:
    future_inputs = {
        "shared_candidate_id": candidate_id,
        "market_id": market_id,
        "recommended_action": action,
        "side": "YES" if action == "BUY_YES" else ("NO" if action == "BUY_NO" else None),
        "best_yes_ask": yes_price,
        "best_no_ask": no_price,
        "approved_position_size_usd": size,
    }
    return {
        "policy": policy,
        "shared_candidate_id": candidate_id,
        "market_id": market_id,
        "action": action,
        "approved_position_size_usd": size,
        "provenance": {"future_pnl_inputs": {key: value for key, value in future_inputs.items() if value is not None}},
    }


class PaperShadowLaneCompositionSweepTests(unittest.TestCase):
    def test_runs_multiple_configs_and_writes_aggregate_artifacts(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "data" / "summaries") as tmp:
            tmp_path = Path(tmp)
            config_one = tmp_path / "stable_with_router_veto.yaml"
            config_two = tmp_path / "router_side_stable_size.yaml"
            output_dir = tmp_path / "sweep_output"
            config_one.write_text(
                """
composition:
  name: stable_with_router_veto
  base_lane: control_stable
  action_lane: control_stable
  sizing_lane: control_stable
  price_lane: control_stable
  fallback_to_base: true
  vetoes:
    - lane: shadow_source_router
      mode: require_agreement
""".lstrip(),
                encoding="utf-8",
            )
            config_two.write_text(
                """
composition:
  name: router_side_stable_size
  base_lane: control_stable
  action_lane: shadow_source_router
  sizing_lane: control_stable
  price_lane: shadow_source_router
  fallback_to_base: false
""".lstrip(),
                encoding="utf-8",
            )
            lane_rows = [
                _lane_row(
                    policy="control_stable",
                    candidate_id="candidate-1",
                    market_id="KXSWEEP-1",
                    action="BUY_YES",
                    size=5.0,
                    yes_price=0.50,
                    no_price=0.55,
                ),
                _lane_row(
                    policy="shadow_source_router",
                    candidate_id="candidate-1",
                    market_id="KXSWEEP-1",
                    action="BUY_YES",
                    size=10.0,
                    yes_price=0.40,
                    no_price=0.65,
                ),
                _lane_row(
                    policy="control_stable",
                    candidate_id="candidate-2",
                    market_id="KXSWEEP-2",
                    action="BUY_YES",
                    size=5.0,
                    yes_price=0.50,
                    no_price=0.55,
                ),
                _lane_row(
                    policy="shadow_source_router",
                    candidate_id="candidate-2",
                    market_id="KXSWEEP-2",
                    action="BUY_NO",
                    size=10.0,
                    yes_price=0.35,
                    no_price=0.60,
                ),
                _lane_row(
                    policy="control_stable",
                    candidate_id="candidate-3",
                    market_id="KXSWEEP-3",
                    action="BUY_YES",
                    size=5.0,
                ),
            ]
            resolution_rows = [
                {"shared_candidate_id": "candidate-1", "market_id": "KXSWEEP-1", "outcome": "YES"},
                {"shared_candidate_id": "candidate-2", "market_id": "KXSWEEP-2", "outcome": "YES"},
                {"shared_candidate_id": "candidate-3", "market_id": "KXSWEEP-3", "outcome": "YES"},
            ]

            result = run_composition_sweep(
                lane_rows=lane_rows,
                resolution_rows=resolution_rows,
                config_paths=[config_one, config_two],
                output_dir=output_dir,
            )

            summary = result["summary"]
            self.assertTrue(summary["non_mutating"])
            self.assertEqual(summary["composition_count"], 2)
            self.assertEqual(summary["aggregate"]["diagnostics"]["composed_buy"], 4)
            self.assertEqual(summary["aggregate"]["diagnostics"]["side_conflict:shadow_source_router"], 1)
            self.assertEqual(summary["aggregate"]["diagnostics"]["missing_action_lane"], 1)
            self.assertEqual(summary["aggregate"]["blocker_counts"], {"missing_fill_price": 1})
            self.assertEqual(
                summary["aggregate"]["best_total_pnl_usd"],
                {
                    "name": "stable_with_router_veto",
                    "config_path": str(config_one.relative_to(ROOT)),
                    "output_dir": str((output_dir / "stable_with_router_veto").relative_to(ROOT)),
                    "total_pnl_usd": 5.0,
                },
            )
            self.assertTrue((output_dir / "stable_with_router_veto" / "composition_rows.jsonl").exists())
            self.assertTrue((output_dir / "router_side_stable_size" / "resolved_rows.jsonl").exists())
            self.assertTrue((output_dir / "summary.json").exists())
            self.assertTrue((output_dir / "report.md").exists())

            persisted = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["schema_name"], "paper_shadow_lane_composition_sweep_summary")

    def test_cli_loads_repeated_config_and_composition_dir(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "data" / "summaries") as tmp:
            tmp_path = Path(tmp)
            config_dir = tmp_path / "configs"
            config_dir.mkdir()
            explicit_config = tmp_path / "stable.json"
            dir_config = config_dir / "router.yaml"
            lane_path = tmp_path / "lane_rows.jsonl"
            resolution_path = tmp_path / "resolution_rows.jsonl"
            output_dir = tmp_path / "cli_output"
            explicit_config.write_text(
                json.dumps(
                    {
                        "composition": {
                            "name": "stable_only",
                            "base_lane": "control_stable",
                            "action_lane": "control_stable",
                            "sizing_lane": "control_stable",
                            "price_lane": "control_stable",
                        }
                    }
                ),
                encoding="utf-8",
            )
            dir_config.write_text(
                """
composition:
  name: router_only
  base_lane: control_stable
  action_lane: shadow_source_router
  sizing_lane: control_stable
  price_lane: shadow_source_router
  fallback_to_base: false
""".lstrip(),
                encoding="utf-8",
            )
            _write_jsonl(
                lane_path,
                [
                    _lane_row(
                        policy="control_stable",
                        candidate_id="candidate-cli",
                        market_id="KXSWEEP-CLI",
                        action="BUY_YES",
                        size=4.0,
                        yes_price=0.50,
                    ),
                    _lane_row(
                        policy="shadow_source_router",
                        candidate_id="candidate-cli",
                        market_id="KXSWEEP-CLI",
                        action="BUY_NO",
                        size=8.0,
                        no_price=0.80,
                    ),
                ],
            )
            _write_jsonl(
                resolution_path,
                [{"shared_candidate_id": "candidate-cli", "market_id": "KXSWEEP-CLI", "outcome": "YES"}],
            )

            code = main(
                [
                    "--lane-decision-path",
                    str(lane_path),
                    "--resolution-path",
                    str(resolution_path),
                    "--composition-config",
                    str(explicit_config),
                    "--composition-dir",
                    str(config_dir),
                    "--output-dir",
                    str(output_dir),
                    "--format",
                    "json",
                ]
            )

            self.assertEqual(code, 0)
            config_paths = _composition_config_paths([str(explicit_config)], [str(config_dir)])
            self.assertEqual([path.name for path in config_paths], ["stable.json", "router.yaml"])
            summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual([row["name"] for row in summary["compositions"]], ["stable_only", "router_only"])
            self.assertTrue((output_dir / "stable_only" / "summary.json").exists())
            self.assertTrue((output_dir / "router_only" / "summary.json").exists())

    def test_rejects_output_outside_safe_derived_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "stable.json"
            config_path.write_text(
                json.dumps({"composition": {"name": "stable_only", "base_lane": "control_stable"}}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                run_composition_sweep(
                    lane_rows=[],
                    resolution_rows=[],
                    config_paths=[config_path],
                    output_dir=Path(tmp) / "unsafe_output",
                )

    def test_best_metric_keeps_duplicate_name_paths_traceable(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "data" / "summaries") as tmp:
            tmp_path = Path(tmp)
            config_one = tmp_path / "dupe_one.yaml"
            config_two = tmp_path / "dupe_two.yaml"
            output_dir = tmp_path / "sweep_output"
            config_one.write_text(
                """
composition:
  name: duplicate_name
  base_lane: control_stable
  action_lane: control_stable
  sizing_lane: control_stable
  price_lane: control_stable
""".lstrip(),
                encoding="utf-8",
            )
            config_two.write_text(
                """
composition:
  name: duplicate_name
  base_lane: control_stable
  action_lane: shadow_source_router
  sizing_lane: control_stable
  price_lane: shadow_source_router
  fallback_to_base: false
""".lstrip(),
                encoding="utf-8",
            )
            lane_rows = [
                _lane_row(
                    policy="control_stable",
                    candidate_id="candidate-dupe",
                    market_id="KXSWEEP-DUPE",
                    action="BUY_YES",
                    size=5.0,
                    yes_price=0.50,
                ),
                _lane_row(
                    policy="shadow_source_router",
                    candidate_id="candidate-dupe",
                    market_id="KXSWEEP-DUPE",
                    action="BUY_NO",
                    size=10.0,
                    no_price=0.80,
                ),
            ]

            result = run_composition_sweep(
                lane_rows=lane_rows,
                resolution_rows=[{"shared_candidate_id": "candidate-dupe", "market_id": "KXSWEEP-DUPE", "outcome": "NO"}],
                config_paths=[config_one, config_two],
                output_dir=output_dir,
            )

            best = result["summary"]["aggregate"]["best_total_pnl_usd"]
            self.assertEqual(best["name"], "duplicate_name")
            self.assertEqual(best["config_path"], str(config_two.relative_to(ROOT)))
            self.assertEqual(best["output_dir"], str((output_dir / "duplicate_name_2").relative_to(ROOT)))
            self.assertEqual(best["total_pnl_usd"], 1.25)


if __name__ == "__main__":
    unittest.main()
