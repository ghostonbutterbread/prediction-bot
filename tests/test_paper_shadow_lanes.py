import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from bot.agent_decision_ledger import summarize_agent_decision_coverage, summarize_agent_decision_reporting
from bot.config import load_config
from bot.file_ops import append_jsonl, load_jsonl
from bot.paper_shadow_lanes import (
    PAPER_LANE_DECISION_ROLE,
    paper_shadow_lanes_enabled,
    summarize_paper_shadow_lane_report,
    write_paper_shadow_lane_decisions,
)
from bot.paper_wallet_runner import run_shared_candidate_paper_evaluation
from bot.prediction_lab import PredictionLab


class PaperShadowLaneTests(unittest.TestCase):
    def _config(self, tmpdir: str) -> dict:
        config_path = Path(tmpdir) / "config.yaml"
        config_path.write_text(
            f"""
runtime:
  base_dir: {Path(tmpdir) / "wallet_data"}
trading:
  mode: paper
strategy_policy:
  version: beta
  beta:
    mode: shadow
    features: {{}}
strategy:
  enable_news: false
  enable_social: false
  enable_ai: false
paper_shadow_lanes:
  enabled: true
  confidence_floor: 0.58
"""
        )
        return load_config(config_path)

    def _market(
        self,
        market_id: str,
        *,
        question: str = "Will the high temperature in New York exceed 71 degrees?",
        series_ticker: str = "KXHIGHNY",
    ):
        return SimpleNamespace(
            id=market_id,
            exchange="kalshi",
            question=question,
            category="weather",
            yes_price=0.41,
            no_price=0.59,
            volume=1200,
            metadata={
                "market_group": "weather",
                "market_family": "daily_temperature",
                "series": "daily_temperature",
                "series_ticker": series_ticker,
                "event_ticker": market_id,
                "market_route": {"group": "weather", "family": "daily_temperature", "allowed": True},
            },
        )

    def _signal(self, *, confidence: float, station_id: str = "KNYC"):
        return {
            "direction": "BUY_YES",
            "model_probability": 0.67,
            "market_price": 0.41,
            "yes_market_price": 0.41,
            "no_market_price": 0.59,
            "edge": 0.26,
            "confidence": confidence,
            "station_id": station_id,
            "source_as_of": "2026-05-13T12:00:00+00:00",
            "signals": {"unit": 0.67},
        }

    def _snapshot_row(
        self,
        *,
        market_id: str,
        confidence: float,
        observed_at: str,
        question: str = "Will the high temperature in New York exceed 71 degrees?",
        series_ticker: str = "KXHIGHNY",
        station_id: str = "KNYC",
    ):
        lab = PredictionLab(
            {
                "data_dir": "/tmp/prediction-lab-fixture",
                "prediction_lab": {"enabled": True, "mode": "collector", "groups": ["weather"]},
                "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
            }
        )
        signal = self._signal(confidence=confidence, station_id=station_id)
        return lab._build_market_snapshot_row(
            f"run-{market_id}",
            self._market(market_id, question=question, series_ticker=series_ticker),
            signal,
            decision_type="buy_yes",
            prediction_recorded=True,
            decision_artifact={
                "final_action": "BUY_YES",
                "final_reason_code": "approved",
                "strategy_signal": signal,
                "shared_core_decision": {
                    "requested_position_size": 10.0,
                    "reason_code": "approved",
                    "confidence": confidence,
                },
            },
            observed_at=observed_at,
        )

    def _write_lane_definition_dir(self, tmpdir: str, *, confidence_floor: float = 0.58) -> Path:
        lanes_dir = Path(tmpdir) / "lanes"
        lanes_dir.mkdir(parents=True, exist_ok=True)
        (lanes_dir / "control_stable.yaml").write_text(
            """
id: control_stable
type: passthrough
source_wallet: stable_paper
enabled: true
description: Control lane that mirrors the stable paper wallet decision.
"""
        )
        (lanes_dir / "shadow_current_beta.yaml").write_text(
            """
id: shadow_current_beta
type: passthrough
source_wallet: beta_paper
enabled: true
description: Shadow lane that mirrors the current beta paper wallet decision.
"""
        )
        (lanes_dir / "shadow_confidence_floor.yaml").write_text(
            f"""
id: shadow_confidence_floor
type: confidence_floor
source_wallet: stable_paper
enabled: true
description: Stable paper decision, but require confidence >= configured floor before it would buy.
parameters:
  confidence_floor: {confidence_floor}
"""
        )
        (lanes_dir / "shadow_premium_city.yaml").write_text(
            """
id: shadow_premium_city
type: premium_city
source_wallet: stable_paper
enabled: false
description: Stable paper decision, but only allow buys for configured premium cities.
parameters:
  allowlist: []
"""
        )
        return lanes_dir

    def _production_lanes_dir(self) -> Path:
        return Path(__file__).resolve().parents[1] / "lanes"

    def _production_lane_ids(self) -> tuple[str, ...]:
        lane_ids: list[str] = []
        for path in sorted(self._production_lanes_dir().glob("*.yaml")):
            with path.open() as handle:
                loaded = yaml.safe_load(handle) or {}
            lane_ids.append(str(loaded.get("id") or path.stem))
        return tuple(lane_ids)

    def test_shadow_lanes_write_multiple_non_mutating_decisions_per_shared_candidate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            low_row = self._snapshot_row(
                market_id="KXHIGHNY-260513-T71",
                confidence=0.55,
                observed_at="2026-05-13T12:00:01+00:00",
            )
            high_row = self._snapshot_row(
                market_id="KXHIGHNY-260514-T71",
                confidence=0.91,
                observed_at="2026-05-14T12:00:01+00:00",
            )
            append_jsonl(dataset_path, low_row)
            append_jsonl(dataset_path, high_row)

            with patch("bot.simulator.KellySizer.calculate", return_value=10.0):
                result = run_shared_candidate_paper_evaluation(dataset_path, config=config)

            lane_rows = load_jsonl(Path(result.paper_lane_decision_path))

        self.assertEqual(result.paper_lane_ids, ("control_stable", "shadow_current_beta", "shadow_confidence_floor"))
        self.assertEqual(result.paper_lane_decision_count, 6)
        self.assertEqual(len(lane_rows), 6)
        self.assertEqual({row["decision_role"] for row in lane_rows}, {PAPER_LANE_DECISION_ROLE})
        self.assertEqual({row["runtime"] for row in lane_rows}, {"paper"})
        self.assertEqual({row["agent_id"] for row in lane_rows}, {"paper"})
        self.assertEqual({row["shared_candidate_id"] for row in lane_rows}, set(result.shared_candidate_ids))
        self.assertEqual({row["input_source"] for row in lane_rows}, {"shared_candidate_dataset"})
        self.assertEqual({row["input_market_source"] for row in lane_rows}, {"shared_market"})
        self.assertTrue(all(row["shared_candidate"]["input_source"] == "shared_candidate_dataset" for row in lane_rows))
        self.assertTrue(all(row["provenance"]["input_source"] == "shared_candidate_dataset" for row in lane_rows))
        self.assertTrue(all(row["provenance"]["input_market_source"] == "shared_market" for row in lane_rows))
        self.assertTrue(all(row["mutation_contract"]["mutates_accounting"] is False for row in lane_rows))
        self.assertTrue(all(row["accounting_ref"]["mutates_accounting"] is False for row in lane_rows))

        by_candidate = {}
        for row in lane_rows:
            by_candidate.setdefault(row["shared_candidate_id"], []).append(row)
        self.assertEqual({candidate_id: len(rows) for candidate_id, rows in by_candidate.items()}, {low_row["shared_candidate_id"]: 3, high_row["shared_candidate_id"]: 3})

        confidence_floor_rows = {
            row["shared_candidate_id"]: row
            for row in lane_rows
            if row["policy"] == "shadow_confidence_floor"
        }
        self.assertEqual(confidence_floor_rows[low_row["shared_candidate_id"]]["action"], "SKIP")
        self.assertEqual(confidence_floor_rows[low_row["shared_candidate_id"]]["reason_code"], "confidence_below_floor")
        self.assertEqual(confidence_floor_rows[high_row["shared_candidate_id"]]["action"], "BUY_YES")
        self.assertEqual(confidence_floor_rows[high_row["shared_candidate_id"]]["reason_code"], "approved_confidence_floor")
        self.assertEqual(confidence_floor_rows[high_row["shared_candidate_id"]]["side"], "YES")

    def test_enabled_lanes_loads_only_selected_lanes_from_definition_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            lanes_dir = self._write_lane_definition_dir(tmpdir)
            config["paper_shadow_lanes"].update(
                {
                    "definitions_dir": str(lanes_dir),
                    "enabled_lanes": ["control_stable", "shadow_confidence_floor"],
                }
            )
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            row = self._snapshot_row(
                market_id="KXHIGHNY-260513-T71",
                confidence=0.91,
                observed_at="2026-05-13T12:00:01+00:00",
            )
            append_jsonl(dataset_path, row)

            with patch("bot.simulator.KellySizer.calculate", return_value=10.0):
                result = run_shared_candidate_paper_evaluation(dataset_path, config=config)

            lane_rows = load_jsonl(Path(result.paper_lane_decision_path))

        self.assertEqual(result.paper_lane_ids, ("control_stable", "shadow_confidence_floor"))
        self.assertEqual(result.paper_lane_decision_count, 2)
        self.assertEqual({row["policy"] for row in lane_rows}, {"control_stable", "shadow_confidence_floor"})

    def test_all_folder_backed_yaml_lanes_execute_with_premium_city_allowlist(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            lane_ids = self._production_lane_ids()
            config["paper_shadow_lanes"].update(
                {
                    "definitions_dir": str(self._production_lanes_dir()),
                    "enabled_lanes": list(lane_ids),
                    "shadow_premium_city": {
                        "enabled": True,
                        "parameters": {"allowlist": ["new_york_ny"]},
                    },
                }
            )
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            new_york_row = self._snapshot_row(
                market_id="KXHIGHNY-260513-T71",
                confidence=0.91,
                observed_at="2026-05-13T12:00:01+00:00",
                question="Will the high temperature in New York exceed 71 degrees?",
                series_ticker="KXHIGHNY",
                station_id="KNYC",
            )
            miami_row = self._snapshot_row(
                market_id="KXHIGHMIA-260513-T83",
                confidence=0.91,
                observed_at="2026-05-13T12:00:02+00:00",
                question="Will the high temperature in Miami exceed 83 degrees?",
                series_ticker="KXHIGHMIA",
                station_id="KMIA",
            )
            append_jsonl(dataset_path, new_york_row)
            append_jsonl(dataset_path, miami_row)

            with patch("bot.simulator.KellySizer.calculate", return_value=10.0):
                result = run_shared_candidate_paper_evaluation(dataset_path, config=config)

            lane_rows = load_jsonl(Path(result.paper_lane_decision_path))
            report = summarize_paper_shadow_lane_report(
                lane_rows=lane_rows,
                config=config,
                shared_candidate_ids=result.shared_candidate_ids,
                candidate_dataset_path=result.candidate_dataset_path,
            )

        self.assertTrue(
            {"control_stable", "shadow_confidence_floor", "shadow_current_beta", "shadow_premium_city"}.issubset(
                set(lane_ids)
            )
        )
        self.assertEqual(result.paper_lane_ids, lane_ids)
        self.assertEqual(result.paper_lane_decision_count, len(lane_ids) * 2)
        self.assertEqual(len(lane_rows), len(lane_ids) * 2)
        self.assertEqual({row["policy"] for row in lane_rows}, set(lane_ids))
        self.assertTrue(
            all(Path(row["lane_definition_path"]).parent == self._production_lanes_dir() for row in lane_rows)
        )
        self.assertTrue(
            all(row["provenance"]["lane_definition_path"] == row["lane_definition_path"] for row in lane_rows)
        )

        premium_rows = {
            row["shared_candidate_id"]: row
            for row in lane_rows
            if row["policy"] == "shadow_premium_city"
        }
        self.assertEqual(premium_rows[new_york_row["shared_candidate_id"]]["action"], "BUY_YES")
        self.assertEqual(premium_rows[new_york_row["shared_candidate_id"]]["reason_code"], "approved_premium_city")
        self.assertEqual(premium_rows[miami_row["shared_candidate_id"]]["action"], "SKIP")
        self.assertEqual(premium_rows[miami_row["shared_candidate_id"]]["reason_code"], "premium_city_not_allowlisted")
        self.assertEqual(premium_rows[miami_row["shared_candidate_id"]]["approved_position_size_usd"], 0.0)

        self.assertEqual(report["enabled_lane_ids"], lane_ids)
        self.assertEqual(report["rows_written"], len(lane_ids) * 2)
        self.assertEqual(report["candidate_count"], 2)
        self.assertEqual(report["lane_row_counts"], {lane_id: 2 for lane_id in sorted(lane_ids)})
        self.assertEqual(report["buy_counts"]["shadow_premium_city"], 1)
        self.assertEqual(report["skip_counts"]["shadow_premium_city"], 1)

    def test_confidence_floor_lane_records_explicit_description_and_file_provenance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            lanes_dir = self._write_lane_definition_dir(tmpdir)
            config["paper_shadow_lanes"].update(
                {
                    "definitions_dir": str(lanes_dir),
                    "enabled_lanes": ["shadow_confidence_floor"],
                }
            )
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            row = self._snapshot_row(
                market_id="KXHIGHNY-260513-T71",
                confidence=0.91,
                observed_at="2026-05-13T12:00:01+00:00",
            )
            append_jsonl(dataset_path, row)

            with patch("bot.simulator.KellySizer.calculate", return_value=10.0):
                result = run_shared_candidate_paper_evaluation(dataset_path, config=config)

            lane_row = load_jsonl(Path(result.paper_lane_decision_path))[0]

        self.assertEqual(lane_row["policy"], "shadow_confidence_floor")
        self.assertIn("stable paper decision", lane_row["lane_description"].lower())
        self.assertIn("confidence >= configured floor", lane_row["lane_description"])
        self.assertTrue(lane_row["lane_definition_path"].endswith("shadow_confidence_floor.yaml"))
        self.assertEqual(lane_row["provenance"]["lane_description"], lane_row["lane_description"])
        self.assertEqual(lane_row["provenance"]["lane_definition_path"], lane_row["lane_definition_path"])

    def test_inline_config_override_can_change_confidence_floor_definition(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            lanes_dir = self._write_lane_definition_dir(tmpdir, confidence_floor=0.50)
            config["paper_shadow_lanes"].pop("confidence_floor", None)
            config["paper_shadow_lanes"].update(
                {
                    "definitions_dir": str(lanes_dir),
                    "enabled_lanes": ["shadow_confidence_floor"],
                    "shadow_confidence_floor": {"confidence_floor": 0.90},
                }
            )
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            row = self._snapshot_row(
                market_id="KXHIGHNY-260513-T71",
                confidence=0.80,
                observed_at="2026-05-13T12:00:01+00:00",
            )
            append_jsonl(dataset_path, row)

            with patch("bot.simulator.KellySizer.calculate", return_value=10.0):
                result = run_shared_candidate_paper_evaluation(dataset_path, config=config)

            lane_row = load_jsonl(Path(result.paper_lane_decision_path))[0]

        self.assertEqual(lane_row["action"], "SKIP")
        self.assertEqual(lane_row["reason_code"], "confidence_below_floor")
        self.assertIn("0.90", lane_row["reason"])

    def test_existing_lanes_mapping_compatibility_still_selects_enabled_lanes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            config["paper_shadow_lanes"]["lanes"] = {
                "control_stable": {"enabled": True},
                "shadow_current_beta": {"enabled": False},
                "shadow_confidence_floor": {"enabled": False},
            }
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            row = self._snapshot_row(
                market_id="KXHIGHNY-260513-T71",
                confidence=0.91,
                observed_at="2026-05-13T12:00:01+00:00",
            )
            append_jsonl(dataset_path, row)

            with patch("bot.simulator.KellySizer.calculate", return_value=10.0):
                result = run_shared_candidate_paper_evaluation(dataset_path, config=config)

            lane_rows = load_jsonl(Path(result.paper_lane_decision_path))

        self.assertEqual(result.paper_lane_ids, ("control_stable",))
        self.assertEqual(len(lane_rows), 1)
        self.assertEqual(lane_rows[0]["policy"], "control_stable")

    def test_confidence_floor_reuses_stable_decision_then_overrides_lane_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            decision_path = Path(tmpdir) / "lanes.jsonl"
            candidate_id = "candidate-1"
            stable_decision = {
                "shared_candidate_id": candidate_id,
                "wallet_id": "stable_paper",
                "run_id": "stable-run",
                "candidate_dataset_path": str(dataset_path),
                "decision_role": "paper_shadow",
                "decision_id": "stable-source-decision",
                "policy": "normal",
                "market_id": "KXHIGHNY-260513-T71",
                "observed_at": "2026-05-13T12:00:01+00:00",
                "action": "BUY_YES",
                "reason_code": "approved",
                "reason": "stable approved",
                "confidence": 0.95,
                "requested_position_size_usd": 10.0,
                "approved_position_size_usd": 10.0,
            }
            beta_decision = dict(stable_decision)
            beta_decision.update(
                {
                    "wallet_id": "beta_paper",
                    "run_id": "beta-run",
                    "decision_id": "beta-source-decision",
                    "policy": "beta_enforce",
                    "action": "SKIP",
                    "reason_code": "beta_skip",
                    "confidence": 0.70,
                    "approved_position_size_usd": 0.0,
                }
            )

            result = write_paper_shadow_lane_decisions(
                config={
                    "paper_shadow_lanes": {
                        "enabled": True,
                        "decision_ledger_path": str(decision_path),
                        "enabled_lanes": ["shadow_confidence_floor"],
                        "shadow_confidence_floor": {"confidence_floor": 0.90},
                    }
                },
                candidate_dataset_path=dataset_path,
                inputs_by_shared_candidate_id={
                    candidate_id: {
                        "stable": SimpleNamespace(
                            signal={
                                "shared_candidate_id": candidate_id,
                                "market_id": "KXHIGHNY-260513-T71",
                                "confidence": 0.80,
                                "candidate_source_runtime": "prediction_lab",
                                "candidate_provenance": "live_known_at_time",
                                "candidate_observed_at": "2026-05-13T12:00:01+00:00",
                                "snapshot_as_of": "2026-05-13T12:00:01+00:00",
                            },
                            shared_candidate={
                                "candidate_id": candidate_id,
                                "market_id": "KXHIGHNY-260513-T71",
                                "source_runtime": "prediction_lab",
                                "provenance": "live_known_at_time",
                                "observed_at": "2026-05-13T12:00:01+00:00",
                                "snapshot_as_of": "2026-05-13T12:00:01+00:00",
                            },
                        ),
                    }
                },
                wallet_decision_rows={"stable_paper": [stable_decision], "beta_paper": [beta_decision]},
                wallet_runs={"stable_paper": SimpleNamespace(session_id="stable-run"), "beta_paper": SimpleNamespace(session_id="beta-run")},
                ledger_root=tmpdir,
            )
            lane_row = load_jsonl(Path(result.decision_path))[0]

        self.assertEqual(lane_row["policy"], "shadow_confidence_floor")
        self.assertEqual(lane_row["input_source"], "shared_candidate_dataset")
        self.assertEqual(lane_row["input_market_source"], "shared_market")
        self.assertEqual(lane_row["shared_candidate"]["candidate_id"], candidate_id)
        self.assertEqual(lane_row["shared_candidate"]["source_runtime"], "prediction_lab")
        self.assertEqual(lane_row["shared_candidate_provenance"], "live_known_at_time")
        self.assertEqual(lane_row["provenance"]["source_wallet_id"], "stable_paper")
        self.assertEqual(lane_row["provenance"]["source_decision_id"], "stable-source-decision")
        self.assertEqual(lane_row["provenance"]["source_policy"], "normal")
        self.assertEqual(lane_row["provenance"]["baseline_wallet_id"], "stable_paper")
        self.assertEqual(lane_row["provenance"]["baseline_decision_id"], "stable-source-decision")
        self.assertEqual(lane_row["provenance"]["comparison_wallet_id"], "beta_paper")
        self.assertEqual(lane_row["provenance"]["comparison_decision_id"], "beta-source-decision")
        self.assertEqual(lane_row["provenance"]["input_confidence"], 0.80)
        self.assertEqual(lane_row["provenance"]["source_confidence"], 0.95)
        self.assertEqual(lane_row["provenance"]["baseline_confidence"], 0.95)
        self.assertEqual(lane_row["provenance"]["comparison_confidence"], 0.70)
        self.assertEqual(lane_row["confidence"], 0.80)
        self.assertEqual(lane_row["requested_position_size_usd"], 10.0)
        self.assertEqual(lane_row["action"], "SKIP")
        self.assertEqual(lane_row["reason_code"], "confidence_below_floor")
        self.assertEqual(lane_row["approved_position_size_usd"], 0.0)

    def test_source_reliability_lane_trusted_support_dominates_excluded_dissent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            decision_path = Path(tmpdir) / "lanes.jsonl"
            scoreboard_path = Path(tmpdir) / "source_scoreboard_by_slice.jsonl"
            append_jsonl(
                scoreboard_path,
                {
                    "source_id": "nws",
                    "source_name": "nws",
                    "city_id": "new_york_ny",
                    "market_kind": "high",
                    "contract_shape": "tail",
                    "sample_count": 100,
                    "threshold_direction_accuracy": 0.95,
                },
            )
            append_jsonl(
                scoreboard_path,
                {
                    "source_id": "bad_model",
                    "source_name": "bad-model",
                    "city_id": "new_york_ny",
                    "market_kind": "high",
                    "contract_shape": "tail",
                    "sample_count": 100,
                    "threshold_direction_accuracy": 0.30,
                },
            )
            candidate_id = "candidate-1"
            stable_decision = {
                "shared_candidate_id": candidate_id,
                "wallet_id": "stable_paper",
                "run_id": "stable-run",
                "candidate_dataset_path": str(dataset_path),
                "decision_role": "paper_shadow",
                "decision_id": "stable-source-decision",
                "policy": "normal",
                "market_id": "KXHIGHNY-260513-T71",
                "observed_at": "2026-05-13T12:00:01+00:00",
                "action": "BUY_YES",
                "reason_code": "approved",
                "confidence": 0.80,
                "requested_position_size_usd": 10.0,
                "approved_position_size_usd": 10.0,
            }

            result = write_paper_shadow_lane_decisions(
                config={
                    "paper_shadow_lanes": {
                        "enabled": True,
                        "decision_ledger_path": str(decision_path),
                        "enabled_lanes": ["shadow_source_reliability"],
                        "shadow_source_reliability": {"scoreboard_path": str(scoreboard_path), "enabled": True},
                    }
                },
                candidate_dataset_path=dataset_path,
                inputs_by_shared_candidate_id={
                    candidate_id: {
                        "stable": SimpleNamespace(
                            signal={
                                "shared_candidate_id": candidate_id,
                                "market_id": "KXHIGHNY-260513-T71",
                                "question": "Will the high temperature in New York exceed 71 degrees?",
                                "city_id": "new_york_ny",
                                "threshold": 71.0,
                                "question_side": "above",
                                "confidence": 0.80,
                                "source_details": [
                                    {"source_name": "nws", "forecast_high": 73.0},
                                    {"source_name": "bad-model", "forecast_high": 69.0},
                                ],
                            },
                            shared_candidate={
                                "candidate_id": candidate_id,
                                "market_id": "KXHIGHNY-260513-T71",
                                "market": {
                                    "id": "KXHIGHNY-260513-T71",
                                    "question": "Will the high temperature in New York exceed 71 degrees?",
                                },
                            },
                        ),
                    }
                },
                wallet_decision_rows={"stable_paper": [stable_decision], "beta_paper": []},
                wallet_runs={"stable_paper": SimpleNamespace(session_id="stable-run")},
                ledger_root=tmpdir,
            )
            lane_row = load_jsonl(Path(result.decision_path))[0]

        self.assertEqual(result.lane_ids, ("shadow_source_reliability",))
        self.assertEqual(lane_row["action"], "BUY_YES")
        self.assertEqual(lane_row["reason_code"], "approved")
        self.assertEqual(lane_row["approved_position_size_usd"], 10.0)
        reliability = lane_row["provenance"]["source_reliability"]
        self.assertEqual(reliability["recommended_action"], "BUY_YES")
        self.assertEqual(reliability["reason_code"], "trusted_support")
        self.assertEqual(reliability["decision_contract"], "recommendation_only_top_level_lane_action_unchanged")
        self.assertEqual(reliability["trusted_support_count"], 1)
        self.assertEqual(reliability["excluded_dissent_count"], 1)
        self.assertEqual(reliability["weighted_dissent"], 0.0)
        self.assertFalse(lane_row["mutation_contract"]["mutates_accounting"])

    def test_source_reliability_lane_records_skip_recommendation_without_mutating_top_level_action(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            decision_path = Path(tmpdir) / "lanes.jsonl"
            scoreboard_path = Path(tmpdir) / "source_scoreboard_by_slice.jsonl"
            append_jsonl(
                scoreboard_path,
                {
                    "source_id": "nws",
                    "source_name": "nws",
                    "city_id": "new_york_ny",
                    "market_kind": "high",
                    "contract_shape": "tail",
                    "sample_count": 100,
                    "threshold_direction_accuracy": 0.95,
                },
            )
            candidate_id = "candidate-skip-recommendation"
            stable_decision = {
                "shared_candidate_id": candidate_id,
                "wallet_id": "stable_paper",
                "run_id": "stable-run",
                "candidate_dataset_path": str(dataset_path),
                "decision_role": "paper_shadow",
                "decision_id": "stable-source-decision",
                "policy": "normal",
                "market_id": "KXHIGHNY-260513-T71",
                "observed_at": "2026-05-13T12:00:01+00:00",
                "action": "BUY_YES",
                "reason_code": "approved",
                "confidence": 0.80,
                "requested_position_size_usd": 10.0,
                "approved_position_size_usd": 10.0,
            }

            result = write_paper_shadow_lane_decisions(
                config={
                    "paper_shadow_lanes": {
                        "enabled": True,
                        "decision_ledger_path": str(decision_path),
                        "enabled_lanes": ["shadow_source_reliability"],
                        "shadow_source_reliability": {"scoreboard_path": str(scoreboard_path), "enabled": True},
                    }
                },
                candidate_dataset_path=dataset_path,
                inputs_by_shared_candidate_id={
                    candidate_id: {
                        "stable": SimpleNamespace(
                            signal={
                                "shared_candidate_id": candidate_id,
                                "market_id": "KXHIGHNY-260513-T71",
                                "question": "Will the high temperature in New York exceed 71 degrees?",
                                "city_id": "new_york_ny",
                                "threshold": 71.0,
                                "question_side": "above",
                                "confidence": 0.80,
                                "source_details": [{"source_name": "nws", "forecast_high": 69.0}],
                            },
                            shared_candidate={
                                "candidate_id": candidate_id,
                                "market_id": "KXHIGHNY-260513-T71",
                                "market": {
                                    "id": "KXHIGHNY-260513-T71",
                                    "question": "Will the high temperature in New York exceed 71 degrees?",
                                },
                            },
                        ),
                    }
                },
                wallet_decision_rows={"stable_paper": [stable_decision], "beta_paper": []},
                wallet_runs={"stable_paper": SimpleNamespace(session_id="stable-run")},
                ledger_root=tmpdir,
            )
            lane_row = load_jsonl(Path(result.decision_path))[0]

        self.assertEqual(lane_row["action"], "BUY_YES")
        self.assertEqual(lane_row["reason_code"], "approved")
        self.assertEqual(lane_row["approved_position_size_usd"], 10.0)
        self.assertEqual(lane_row["confidence"], 0.80)
        reliability = lane_row["provenance"]["source_reliability"]
        self.assertEqual(reliability["recommended_action"], "SKIP")
        self.assertEqual(reliability["reason_code"], "trusted_dissent")
        self.assertAlmostEqual(reliability["confidence_after"], 0.60)
        self.assertFalse(lane_row["mutation_contract"]["mutates_accounting"])

    def test_source_reliability_lane_missing_scoreboard_records_unavailable_without_mutating_top_level_action(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            decision_path = Path(tmpdir) / "lanes.jsonl"
            candidate_id = "candidate-missing-scoreboard"
            stable_decision = {
                "shared_candidate_id": candidate_id,
                "wallet_id": "stable_paper",
                "run_id": "stable-run",
                "candidate_dataset_path": str(dataset_path),
                "decision_role": "paper_shadow",
                "decision_id": "stable-source-decision",
                "policy": "normal",
                "market_id": "KXHIGHNY-260513-T71",
                "observed_at": "2026-05-13T12:00:01+00:00",
                "action": "BUY_YES",
                "reason_code": "approved",
                "confidence": 0.80,
                "requested_position_size_usd": 10.0,
                "approved_position_size_usd": 10.0,
            }

            result = write_paper_shadow_lane_decisions(
                config={
                    "paper_shadow_lanes": {
                        "enabled": True,
                        "decision_ledger_path": str(decision_path),
                        "enabled_lanes": ["shadow_source_reliability"],
                        "shadow_source_reliability": {"enabled": True},
                    }
                },
                candidate_dataset_path=dataset_path,
                inputs_by_shared_candidate_id={
                    candidate_id: {
                        "stable": SimpleNamespace(
                            signal={
                                "shared_candidate_id": candidate_id,
                                "market_id": "KXHIGHNY-260513-T71",
                                "question": "Will the high temperature in New York exceed 71 degrees?",
                                "city_id": "new_york_ny",
                                "threshold": 71.0,
                                "question_side": "above",
                                "confidence": 0.80,
                                "source_details": [{"source_name": "nws", "forecast_high": 73.0}],
                            },
                            shared_candidate={
                                "candidate_id": candidate_id,
                                "market_id": "KXHIGHNY-260513-T71",
                                "market": {
                                    "id": "KXHIGHNY-260513-T71",
                                    "question": "Will the high temperature in New York exceed 71 degrees?",
                                },
                            },
                        ),
                    }
                },
                wallet_decision_rows={"stable_paper": [stable_decision], "beta_paper": []},
                wallet_runs={"stable_paper": SimpleNamespace(session_id="stable-run")},
                ledger_root=tmpdir,
            )
            lane_row = load_jsonl(Path(result.decision_path))[0]

        self.assertEqual(lane_row["action"], "BUY_YES")
        self.assertEqual(lane_row["reason_code"], "approved")
        self.assertEqual(lane_row["approved_position_size_usd"], 10.0)
        reliability = lane_row["provenance"]["source_reliability"]
        self.assertFalse(reliability["available"])
        self.assertEqual(reliability["recommended_action"], "SKIP")
        self.assertEqual(reliability["reason_code"], "source_reliability_unavailable")
        self.assertFalse(lane_row["mutation_contract"]["mutates_accounting"])

    def test_paper_shadow_lane_report_counts_rows_actions_and_reference_drift(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            decision_path = Path(tmpdir) / "lanes.jsonl"
            candidate_id = "candidate-1"
            stable_decision = {
                "shared_candidate_id": candidate_id,
                "wallet_id": "stable_paper",
                "run_id": "stable-run",
                "candidate_dataset_path": str(dataset_path),
                "decision_role": "paper_shadow",
                "decision_id": "stable-source-decision",
                "policy": "normal",
                "market_id": "KXHIGHNY-260513-T71",
                "observed_at": "2026-05-13T12:00:01+00:00",
                "action": "BUY_YES",
                "reason_code": "approved",
                "confidence": 0.95,
                "requested_position_size_usd": 10.0,
                "approved_position_size_usd": 10.0,
            }
            beta_decision = dict(stable_decision)
            beta_decision.update(
                {
                    "wallet_id": "beta_paper",
                    "run_id": "beta-run",
                    "decision_id": "beta-source-decision",
                    "policy": "beta_enforce",
                    "action": "SKIP",
                    "reason_code": "beta_skip",
                    "confidence": 0.70,
                    "approved_position_size_usd": 0.0,
                }
            )
            config = {
                "paper_shadow_lanes": {
                    "enabled": True,
                    "decision_ledger_path": str(decision_path),
                    "confidence_floor": 0.90,
                }
            }

            result = write_paper_shadow_lane_decisions(
                config=config,
                candidate_dataset_path=dataset_path,
                inputs_by_shared_candidate_id={
                    candidate_id: {
                        "stable": SimpleNamespace(
                            signal={
                                "shared_candidate_id": candidate_id,
                                "market_id": "KXHIGHNY-260513-T71",
                                "confidence": 0.80,
                            },
                            shared_candidate={"candidate_id": candidate_id, "market_id": "KXHIGHNY-260513-T71"},
                        ),
                    }
                },
                wallet_decision_rows={"stable_paper": [stable_decision], "beta_paper": [beta_decision]},
                wallet_runs={"stable_paper": SimpleNamespace(session_id="stable-run"), "beta_paper": SimpleNamespace(session_id="beta-run")},
                ledger_root=tmpdir,
            )

            report = summarize_paper_shadow_lane_report(
                lane_decision_path=result.decision_path,
                config=config,
                shared_candidate_ids=[candidate_id],
                baseline_rows=[stable_decision],
                comparison_rows=[beta_decision],
            )

        self.assertEqual(report["enabled_lane_ids"], ("control_stable", "shadow_current_beta", "shadow_confidence_floor"))
        self.assertEqual(report["candidate_count"], 1)
        self.assertEqual(report["rows_written"], 3)
        self.assertEqual(
            report["lane_row_counts"],
            {"control_stable": 1, "shadow_confidence_floor": 1, "shadow_current_beta": 1},
        )
        self.assertEqual(report["buy_counts"], {"control_stable": 1})
        self.assertEqual(report["skip_counts"], {"shadow_confidence_floor": 1, "shadow_current_beta": 1})
        self.assertEqual(report["drift"]["vs_baseline"]["candidate_count_with_action_drift"], 1)
        self.assertEqual(
            report["drift"]["vs_baseline"]["by_lane"],
            {"shadow_confidence_floor": 1, "shadow_current_beta": 1},
        )
        self.assertEqual(report["drift"]["vs_comparison"]["candidate_count_with_action_drift"], 1)
        self.assertEqual(report["drift"]["vs_comparison"]["by_lane"], {"control_stable": 1})

    def test_paper_shadow_lane_report_filters_rows_to_requested_candidates(self):
        lane_rows = [
            {
                "policy": "control_stable",
                "shared_candidate_id": "candidate-1",
                "action": "BUY_YES",
                "provenance": {"baseline_action": "BUY_YES", "comparison_action": "SKIP"},
            },
            {
                "policy": "control_stable",
                "shared_candidate_id": "candidate-2",
                "action": "SKIP",
                "provenance": {"baseline_action": "SKIP", "comparison_action": "SKIP"},
            },
        ]

        report = summarize_paper_shadow_lane_report(lane_rows=lane_rows, shared_candidate_ids=["candidate-1"])

        self.assertEqual(report["candidate_count"], 1)
        self.assertEqual(report["rows_written"], 1)
        self.assertEqual(report["lane_row_counts"], {"control_stable": 1})
        self.assertEqual(report["buy_counts"], {"control_stable": 1})
        self.assertEqual(report["skip_counts"], {})
        self.assertEqual(report["drift"]["vs_comparison"]["reference_candidate_count"], 1)
        self.assertEqual(report["drift"]["vs_comparison"]["by_shared_candidate_id"], {"candidate-1": 1})

    def test_paper_shadow_lane_report_scopes_to_current_run_dataset_and_enabled_lanes(self):
        lane_rows = [
            {
                "policy": "control_stable",
                "shared_candidate_id": "candidate-1",
                "candidate_dataset_path": "/tmp/current.jsonl",
                "run_id": "stable-run:paper_lanes",
                "action": "BUY_YES",
                "provenance": {"baseline_action": "BUY_YES", "comparison_action": "SKIP"},
            },
            {
                "policy": "shadow_premium_city",
                "shared_candidate_id": "candidate-1",
                "candidate_dataset_path": "/tmp/current.jsonl",
                "run_id": "stable-run:paper_lanes",
                "action": "SKIP",
                "provenance": {"baseline_action": "BUY_YES", "comparison_action": "SKIP"},
            },
            {
                "policy": "control_stable",
                "shared_candidate_id": "candidate-1",
                "candidate_dataset_path": "/tmp/current.jsonl",
                "run_id": "old-run:paper_lanes",
                "action": "SKIP",
                "provenance": {"baseline_action": "SKIP", "comparison_action": "SKIP"},
            },
            {
                "policy": "control_stable",
                "shared_candidate_id": "candidate-1",
                "candidate_dataset_path": "/tmp/old.jsonl",
                "run_id": "stable-run:paper_lanes",
                "action": "SKIP",
                "provenance": {"baseline_action": "SKIP", "comparison_action": "SKIP"},
            },
        ]
        baseline_rows = [
            {
                "shared_candidate_id": "candidate-1",
                "candidate_dataset_path": "/tmp/current.jsonl",
                "run_id": "stable-run",
                "action": "BUY_YES",
            }
        ]
        config = {"paper_shadow_lanes": {"enabled": True, "enabled_lanes": ["control_stable"]}}

        report = summarize_paper_shadow_lane_report(
            lane_rows=lane_rows,
            config=config,
            shared_candidate_ids=["candidate-1", "candidate-missing"],
            baseline_rows=baseline_rows,
        )

        self.assertEqual(report["requested_candidate_count"], 2)
        self.assertEqual(report["observed_candidate_count"], 1)
        self.assertEqual(report["candidate_count"], 1)
        self.assertEqual(report["rows_loaded"], 4)
        self.assertEqual(report["rows_written"], 1)
        self.assertEqual(report["lane_row_counts"], {"control_stable": 1})
        self.assertEqual(report["run_id"], "stable-run:paper_lanes")
        self.assertEqual(report["candidate_dataset_path"], "/tmp/current.jsonl")

    def test_paper_shadow_lanes_reject_non_shared_input_sources(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "paper_shadow_lanes": {
                    "enabled": True,
                    "enabled_lanes": ["control_stable"],
                    "control_stable": {"input_source": "stable_paper"},
                }
            }

            with self.assertRaisesRegex(ValueError, "input_source=shared_candidate_dataset"):
                write_paper_shadow_lane_decisions(
                    config=config,
                    candidate_dataset_path=Path(tmpdir) / "candidates.jsonl",
                    inputs_by_shared_candidate_id={},
                    wallet_decision_rows={"stable_paper": [], "beta_paper": []},
                    wallet_runs={},
                    ledger_root=tmpdir,
                )

    def test_paper_shadow_lanes_reject_invalid_confidence_floor(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "paper_shadow_lanes": {
                    "enabled": True,
                    "enabled_lanes": ["shadow_confidence_floor"],
                    "confidence_floor": "strict",
                }
            }

            with self.assertRaisesRegex(ValueError, "invalid confidence_floor"):
                write_paper_shadow_lane_decisions(
                    config=config,
                    candidate_dataset_path=Path(tmpdir) / "candidates.jsonl",
                    inputs_by_shared_candidate_id={
                        "candidate-1": {
                            "stable_paper": SimpleNamespace(
                                signal={"shared_candidate_id": "candidate-1", "confidence": 0.9},
                                shared_candidate={"candidate_id": "candidate-1"},
                            )
                        }
                    },
                    wallet_decision_rows={"stable_paper": [], "beta_paper": []},
                    wallet_runs={},
                    ledger_root=tmpdir,
                )

    def test_paper_shadow_lanes_reject_unknown_lane_types(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "paper_shadow_lanes": {
                    "enabled": True,
                    "enabled_lanes": ["control_stable"],
                    "control_stable": {"type": "mystery"},
                }
            }

            with self.assertRaisesRegex(ValueError, "unknown paper shadow lane type"):
                write_paper_shadow_lane_decisions(
                    config=config,
                    candidate_dataset_path=Path(tmpdir) / "candidates.jsonl",
                    inputs_by_shared_candidate_id={
                        "candidate-1": {
                            "stable_paper": SimpleNamespace(
                                signal={"shared_candidate_id": "candidate-1", "confidence": 0.9},
                                shared_candidate={"candidate_id": "candidate-1"},
                            )
                        }
                    },
                    wallet_decision_rows={"stable_paper": [], "beta_paper": []},
                    wallet_runs={},
                    ledger_root=tmpdir,
                )

    def test_shadow_lane_rows_are_accepted_by_agent_decision_reporting(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            row = self._snapshot_row(
                market_id="KXHIGHNY-260513-T71",
                confidence=0.55,
                observed_at="2026-05-13T12:00:01+00:00",
            )
            append_jsonl(dataset_path, row)

            with patch("bot.simulator.KellySizer.calculate", return_value=10.0):
                result = run_shared_candidate_paper_evaluation(dataset_path, config=config)

            lane_rows = load_jsonl(Path(result.paper_lane_decision_path))
            coverage = summarize_agent_decision_coverage(lane_rows, shared_candidate_ids=result.shared_candidate_ids)
            report = summarize_agent_decision_reporting(lane_rows, shared_candidate_ids=result.shared_candidate_ids)

        self.assertEqual(coverage["total_rows"], 3)
        self.assertEqual(coverage["matched_shared_candidate_ids"], 1)
        self.assertEqual(coverage["by_decision_role"], {"paper_lane": 3})
        self.assertEqual(
            coverage["by_policy"],
            {
                "control_stable": 1,
                "shadow_confidence_floor": 1,
                "shadow_current_beta": 1,
            },
        )
        self.assertEqual(report["coverage"]["total_rows"], 3)
        self.assertEqual(report["overlap"]["candidate_count_with_multiple_decisions"], 1)
        self.assertEqual(report["policy_drift"]["candidate_count_with_action_drift"], 1)

    def test_string_false_does_not_enable_shadow_lane_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config(tmpdir)
            config["paper_shadow_lanes"]["enabled"] = "false"
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            row = self._snapshot_row(
                market_id="KXHIGHNY-260513-T71",
                confidence=0.91,
                observed_at="2026-05-13T12:00:01+00:00",
            )
            append_jsonl(dataset_path, row)

            with patch("bot.simulator.KellySizer.calculate", return_value=10.0):
                result = run_shared_candidate_paper_evaluation(dataset_path, config=config)

        self.assertFalse(paper_shadow_lanes_enabled(config))
        self.assertIsNone(result.paper_lane_decision_path)
        self.assertEqual(result.paper_lane_decision_count, 0)
        self.assertEqual(result.paper_lane_ids, ())

    def test_shadow_lanes_ignore_stale_wallet_decisions_for_same_candidate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            decision_path = Path(tmpdir) / "lanes.jsonl"
            candidate_id = "candidate-1"
            current_stable = {
                "shared_candidate_id": candidate_id,
                "wallet_id": "stable_paper",
                "run_id": "stable-run",
                "candidate_dataset_path": str(dataset_path),
                "decision_role": "paper_shadow",
                "decision_id": "current-stable",
                "policy": "normal",
                "market_id": "KXHIGHNY-260513-T71",
                "observed_at": "2026-05-13T12:00:01+00:00",
                "action": "BUY_YES",
                "reason_code": "approved",
                "confidence": 0.91,
                "approved_position_size_usd": 10.0,
            }
            stale_stable = dict(current_stable)
            stale_stable.update(
                {
                    "run_id": "old-run",
                    "decision_id": "stale-stable",
                    "action": "SKIP",
                    "reason_code": "stale_skip",
                    "confidence": 0.10,
                    "approved_position_size_usd": 0.0,
                }
            )
            current_beta = dict(current_stable)
            current_beta.update({"wallet_id": "beta_paper", "run_id": "beta-run", "decision_id": "current-beta", "policy": "beta_enforce"})

            result = write_paper_shadow_lane_decisions(
                config={"paper_shadow_lanes": {"enabled": True, "decision_ledger_path": str(decision_path)}},
                candidate_dataset_path=dataset_path,
                inputs_by_shared_candidate_id={
                    candidate_id: {
                        "stable": SimpleNamespace(signal={"market_id": "KXHIGHNY-260513-T71", "confidence": 0.91}),
                    }
                },
                wallet_decision_rows={"stable_paper": [current_stable, stale_stable], "beta_paper": [current_beta]},
                wallet_runs={"stable_paper": SimpleNamespace(session_id="stable-run"), "beta_paper": SimpleNamespace(session_id="beta-run")},
                ledger_root=tmpdir,
            )
            rows = load_jsonl(Path(result.decision_path))

        control_row = next(row for row in rows if row["policy"] == "control_stable")
        confidence_row = next(row for row in rows if row["policy"] == "shadow_confidence_floor")
        self.assertEqual(control_row["action"], "BUY_YES")
        self.assertEqual(control_row["provenance"]["source_decision_id"], "current-stable")
        self.assertEqual(confidence_row["action"], "BUY_YES")
        self.assertEqual(confidence_row["reason_code"], "approved_confidence_floor")


if __name__ == "__main__":
    unittest.main()
