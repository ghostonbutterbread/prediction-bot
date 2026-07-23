import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from bot.agent_decision_ledger import summarize_agent_decision_coverage, summarize_agent_decision_reporting
from bot.config import load_config
from bot.file_ops import append_jsonl, load_jsonl
from bot.paper_shadow_lanes import (
    PAPER_LANE_DECISION_ROLE,
    build_paper_shadow_lane_resolution_rows,
    paper_shadow_lanes_enabled,
    summarize_paper_shadow_lane_report,
    summarize_paper_shadow_lane_resolved_pnl,
    update_paper_shadow_lane_incremental_pnl,
    write_paper_shadow_lane_decisions,
)
from bot.paper_wallet_runner import run_shared_candidate_paper_evaluation
from bot.prediction_lab import PredictionLab
from scripts.paper_shadow_lane_report import main as paper_shadow_lane_report_main

REPO_ROOT = Path(__file__).resolve().parents[1]


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
        (lanes_dir / "shadow_source_scoreboard.yaml").write_text(
            """
id: shadow_source_scoreboard
type: source_reliability
source_wallet: stable_paper
source_role: baseline
input_source: shared_candidate_dataset
input_market_source: shared_market
enabled: false
description: Stable paper decision, but record source scoreboard recommendations and future-PnL provenance only.
parameters: {}
"""
        )
        (lanes_dir / "shadow_source_router.yaml").write_text(
            """
id: shadow_source_router
type: source_router
source_wallet: stable_paper
source_role: baseline
input_source: shared_candidate_dataset
input_market_source: shared_market
enabled: false
description: Source-router shadow lane that records independent source-implied decisions and future-PnL provenance only.
parameters:
  hypothetical_notional_usd: 10.0
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
        self.assertNotIn("source_scoreboard", lane_row["provenance"])
        self.assertNotIn("future_pnl_inputs", lane_row["provenance"])
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

    def test_shadow_source_scoreboard_lane_loads_from_yaml_and_writes_non_mutating_row(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            decision_path = Path(tmpdir) / "lanes.jsonl"
            scoreboard_path = Path(tmpdir) / "source_scoreboard_by_slice.jsonl"
            lanes_dir = self._write_lane_definition_dir(tmpdir)
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
            candidate_id = "candidate-scoreboard-yaml"
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
                "confidence": 0.80,
                "requested_position_size_usd": 10.0,
                "approved_position_size_usd": 10.0,
            }

            result = write_paper_shadow_lane_decisions(
                config={
                    "paper_shadow_lanes": {
                        "enabled": True,
                        "definitions_dir": str(lanes_dir),
                        "decision_ledger_path": str(decision_path),
                        "enabled_lanes": ["shadow_source_scoreboard"],
                        "source_scoreboard_path": str(scoreboard_path),
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

        self.assertEqual(result.lane_ids, ("shadow_source_scoreboard",))
        self.assertEqual(lane_row["policy"], "shadow_source_scoreboard")
        self.assertFalse(lane_row["mutation_contract"]["mutates_accounting"])
        self.assertFalse(lane_row["accounting_ref"]["mutates_accounting"])
        self.assertTrue(lane_row["provenance"]["source_scoreboard"]["available"])
        self.assertEqual(lane_row["provenance"]["source_scoreboard"]["recommended_action"], "BUY_YES")
        self.assertIn("source scoreboard", lane_row["lane_description"].lower())

    def test_shadow_source_scoreboard_keeps_top_level_action_stable_and_recommendation_in_provenance(self):
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
            candidate_id = "candidate-scoreboard-stable-aligned"
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
                "confidence": 0.80,
                "requested_position_size_usd": 10.0,
                "approved_position_size_usd": 10.0,
            }

            result = write_paper_shadow_lane_decisions(
                config={
                    "paper_shadow_lanes": {
                        "enabled": True,
                        "decision_ledger_path": str(decision_path),
                        "enabled_lanes": ["shadow_source_scoreboard"],
                        "shadow_source_scoreboard": {"scoreboard_path": str(scoreboard_path), "enabled": True},
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
        scoreboard = lane_row["provenance"]["source_scoreboard"]
        self.assertEqual(scoreboard["recommended_action"], "SKIP")
        self.assertEqual(scoreboard["reason_code"], "trusted_dissent")
        self.assertEqual(lane_row["provenance"]["source_reliability"]["recommended_action"], "SKIP")
        self.assertEqual(lane_row["provenance"]["future_pnl_inputs"]["stable_action"], "BUY_YES")

    def test_shadow_source_scoreboard_future_pnl_inputs_capture_order_book_and_label_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            decision_path = Path(tmpdir) / "lanes.jsonl"
            scoreboard_path = Path(tmpdir) / "source_scoreboard_by_slice.jsonl"
            append_jsonl(
                scoreboard_path,
                {
                    "source_id": "nws",
                    "source_name": "nws",
                    "city_id": "seattle_wa",
                    "market_kind": "high",
                    "contract_shape": "tail",
                    "sample_count": 100,
                    "threshold_direction_accuracy": 0.95,
                },
            )
            candidate_id = "candidate-future-pnl-inputs"
            stable_decision = {
                "shared_candidate_id": candidate_id,
                "wallet_id": "stable_paper",
                "run_id": "stable-run",
                "candidate_dataset_path": str(dataset_path),
                "decision_role": "paper_shadow",
                "decision_id": "stable-source-decision",
                "policy": "normal",
                "market_id": "KXHIGHSEA-260515-T70",
                "observed_at": "2026-05-14T12:00:00+00:00",
                "action": "BUY_YES",
                "reason_code": "approved",
                "reason": "stable approved",
                "confidence": 0.88,
                "requested_position_size_usd": 10.0,
                "approved_position_size_usd": 10.0,
            }

            result = write_paper_shadow_lane_decisions(
                config={
                    "paper_shadow_lanes": {
                        "enabled": True,
                        "decision_ledger_path": str(decision_path),
                        "enabled_lanes": ["shadow_source_scoreboard"],
                        "shadow_source_scoreboard": {"scoreboard_path": str(scoreboard_path), "enabled": True},
                    }
                },
                candidate_dataset_path=dataset_path,
                inputs_by_shared_candidate_id={
                    candidate_id: {
                        "stable": SimpleNamespace(
                            signal={
                                "shared_candidate_id": candidate_id,
                                "market_id": "KXHIGHSEA-260515-T70",
                                "question": "Will Seattle high temperature be above 70 degrees on May 15, 2026?",
                                "city_id": "seattle_wa",
                                "threshold": 70.0,
                                "question_side": "above",
                                "contract_shape": "tail",
                                "direction": "BUY_YES",
                                "confidence": 0.88,
                                "market_price": 0.44,
                                "candidate_observed_at": "2026-05-14T12:00:00+00:00",
                                "decision_artifact": {
                                    "execution_snapshot_source": "book",
                                    "order_book_snapshot": {
                                        "source": "book",
                                        "data": {
                                            "best_yes_ask": 0.44,
                                            "best_yes_bid": 0.42,
                                            "best_no_ask": 0.58,
                                            "best_no_bid": 0.56,
                                        },
                                    },
                                    "execution_snapshot": {
                                        "source": "book",
                                        "best_yes_ask": 0.44,
                                        "best_yes_bid": 0.42,
                                        "best_no_ask": 0.58,
                                        "best_no_bid": 0.56,
                                        "estimated_fill_price": 0.445,
                                        "as_of": "2026-05-14T12:00:00+00:00",
                                    },
                                },
                                "source_details": [{"source_name": "nws", "forecast_high": 72.0}],
                            },
                            shared_candidate={
                                "candidate_id": candidate_id,
                                "market_id": "KXHIGHSEA-260515-T70",
                                "snapshot_as_of": "2026-05-14T12:00:00+00:00",
                                "market": {
                                    "id": "KXHIGHSEA-260515-T70",
                                    "question": "Will Seattle high temperature be above 70 degrees on May 15, 2026?",
                                },
                                "evidence": {
                                    "weather_source_snapshot": {
                                        "as_of": "2026-05-14T12:00:00+00:00",
                                        "settlement_source": "kalshi_settlement",
                                        "forecast": {"actual_temp_used": 73.0},
                                    }
                                },
                                "resolution": {
                                    "actual_source": "nws_observed",
                                    "resolved_at": "2026-05-16T13:00:00+00:00",
                                    "known_after": "2026-05-16T13:00:00+00:00",
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

        future_pnl_inputs = lane_row["provenance"]["future_pnl_inputs"]
        self.assertEqual(future_pnl_inputs["shared_candidate_id"], candidate_id)
        self.assertEqual(future_pnl_inputs["market_id"], "KXHIGHSEA-260515-T70")
        self.assertEqual(future_pnl_inputs["observed_at"], "2026-05-14T12:00:00+00:00")
        self.assertEqual(future_pnl_inputs["stable_action"], "BUY_YES")
        self.assertEqual(future_pnl_inputs["stable_reason_code"], "approved")
        self.assertEqual(future_pnl_inputs["stable_requested_position_size_usd"], 10.0)
        self.assertEqual(future_pnl_inputs["stable_confidence"], 0.88)
        self.assertEqual(future_pnl_inputs["recommended_action"], "BUY_YES")
        self.assertEqual(future_pnl_inputs["side"], "YES")
        self.assertEqual(future_pnl_inputs["entry_price"], 0.44)
        self.assertEqual(future_pnl_inputs["estimated_fill_price"], 0.445)
        self.assertEqual(future_pnl_inputs["best_yes_ask"], 0.44)
        self.assertEqual(future_pnl_inputs["best_yes_bid"], 0.42)
        self.assertEqual(future_pnl_inputs["best_no_ask"], 0.58)
        self.assertEqual(future_pnl_inputs["best_no_bid"], 0.56)
        self.assertEqual(future_pnl_inputs["execution_snapshot_source"], "book")
        self.assertEqual(future_pnl_inputs["order_book_source"], "book")
        self.assertEqual(future_pnl_inputs["snapshot_as_of"], "2026-05-14T12:00:00+00:00")
        self.assertEqual(future_pnl_inputs["execution_snapshot_as_of"], "2026-05-14T12:00:00+00:00")
        for forbidden_key in (
            "actual_temp_used",
            "settlement_source",
            "actual_source",
            "resolved_at",
            "known_after",
            "label_target",
        ):
            self.assertNotIn(forbidden_key, future_pnl_inputs)
        self.assertEqual(future_pnl_inputs["threshold"], 70.0)
        self.assertEqual(future_pnl_inputs["question_side"], "above")
        self.assertEqual(future_pnl_inputs["market_kind"], "high")
        self.assertEqual(future_pnl_inputs["contract_shape"], "tail")
        self.assertEqual(
            future_pnl_inputs["question"],
            "Will Seattle high temperature be above 70 degrees on May 15, 2026?",
        )
        self.assertEqual(
            lane_row["provenance"]["source_scoreboard"]["future_pnl_inputs"]["estimated_fill_price"],
            0.445,
        )

    def test_shadow_source_scoreboard_uses_side_ask_when_signal_market_price_is_zero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            decision_path = Path(tmpdir) / "lanes.jsonl"
            scoreboard_path = Path(tmpdir) / "source_scoreboard_by_slice.jsonl"
            append_jsonl(
                scoreboard_path,
                {
                    "source_id": "nws",
                    "source_name": "nws",
                    "city_id": "seattle_wa",
                    "market_kind": "high",
                    "contract_shape": "tail",
                    "sample_count": 100,
                    "threshold_direction_accuracy": 0.95,
                },
            )
            candidate_id = "candidate-scoreboard-zero-market-price"
            stable_decision = {
                "shared_candidate_id": candidate_id,
                "wallet_id": "stable_paper",
                "run_id": "stable-run",
                "candidate_dataset_path": str(dataset_path),
                "decision_role": "paper_shadow",
                "decision_id": "stable-source-decision-zero-market-price",
                "policy": "normal",
                "market_id": "KXHIGHSEA-260515-T70",
                "observed_at": "2026-05-14T12:00:00+00:00",
                "action": "BUY_NO",
                "reason_code": "approved",
                "reason": "stable approved",
                "confidence": 0.88,
                "requested_position_size_usd": 10.0,
                "approved_position_size_usd": 10.0,
            }

            result = write_paper_shadow_lane_decisions(
                config={
                    "paper_shadow_lanes": {
                        "enabled": True,
                        "decision_ledger_path": str(decision_path),
                        "enabled_lanes": ["shadow_source_scoreboard"],
                        "shadow_source_scoreboard": {"scoreboard_path": str(scoreboard_path), "enabled": True},
                    }
                },
                candidate_dataset_path=dataset_path,
                inputs_by_shared_candidate_id={
                    candidate_id: {
                        "stable": SimpleNamespace(
                            signal={
                                "shared_candidate_id": candidate_id,
                                "market_id": "KXHIGHSEA-260515-T70",
                                "question": "Will Seattle high temperature be above 70 degrees on May 15, 2026?",
                                "city_id": "seattle_wa",
                                "threshold": 70.0,
                                "question_side": "above",
                                "direction": "BUY_NO",
                                "confidence": 0.88,
                                "market_price": 0.0,
                                "candidate_observed_at": "2026-05-14T12:00:00+00:00",
                                "best_yes_ask": 0.0,
                                "best_no_ask": 0.58,
                                "source_details": [{"source_name": "nws", "forecast_high": 68.0}],
                            },
                            shared_candidate={
                                "candidate_id": candidate_id,
                                "market_id": "KXHIGHSEA-260515-T70",
                            },
                        ),
                    }
                },
                wallet_decision_rows={"stable_paper": [stable_decision], "beta_paper": []},
                wallet_runs={"stable_paper": SimpleNamespace(session_id="stable-run")},
                ledger_root=tmpdir,
            )
            lane_row = load_jsonl(Path(result.decision_path))[0]

        self.assertEqual(lane_row["policy"], "shadow_source_scoreboard")
        self.assertEqual(lane_row["action"], "BUY_NO")
        self.assertEqual(lane_row["entry_price"], 0.58)
        self.assertEqual(lane_row["price"], 0.58)
        future_pnl_inputs = lane_row["provenance"]["future_pnl_inputs"]
        self.assertEqual(future_pnl_inputs["entry_price"], 0.58)
        self.assertEqual(future_pnl_inputs["estimated_fill_price"], 0.58)
        self.assertEqual(future_pnl_inputs["best_no_ask"], 0.58)
        self.assertNotIn("best_yes_ask", future_pnl_inputs)

    def test_shadow_source_scoreboard_future_pnl_inputs_capture_resolution_metadata_from_shared_weather_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            decision_path = Path(tmpdir) / "lanes.jsonl"
            scoreboard_path = Path(tmpdir) / "source_scoreboard_by_slice.jsonl"
            append_jsonl(
                scoreboard_path,
                {
                    "source_id": "nws",
                    "source_name": "nws",
                    "city_id": "seattle_wa",
                    "market_kind": "high",
                    "contract_shape": "tail",
                    "sample_count": 100,
                    "threshold_direction_accuracy": 0.95,
                },
            )
            candidate_id = "candidate-future-pnl-resolution-context"
            stable_decision = {
                "shared_candidate_id": candidate_id,
                "wallet_id": "stable_paper",
                "run_id": "stable-run",
                "candidate_dataset_path": str(dataset_path),
                "decision_role": "paper_shadow",
                "decision_id": "stable-source-decision",
                "policy": "normal",
                "market_id": "KXHIGHSEA-260515-T70",
                "observed_at": "2026-05-14T12:00:00+00:00",
                "action": "BUY_YES",
                "reason_code": "approved",
                "reason": "stable approved",
                "confidence": 0.88,
                "requested_position_size_usd": 10.0,
                "approved_position_size_usd": 10.0,
            }

            result = write_paper_shadow_lane_decisions(
                config={
                    "paper_shadow_lanes": {
                        "enabled": True,
                        "decision_ledger_path": str(decision_path),
                        "enabled_lanes": ["shadow_source_scoreboard"],
                        "shadow_source_scoreboard": {"scoreboard_path": str(scoreboard_path), "enabled": True},
                    }
                },
                candidate_dataset_path=dataset_path,
                inputs_by_shared_candidate_id={
                    candidate_id: {
                        "stable": SimpleNamespace(
                            signal={
                                "shared_candidate_id": candidate_id,
                                "market_id": "KXHIGHSEA-260515-T70",
                                "confidence": 0.88,
                                "market_price": 0.44,
                                "candidate_observed_at": "2026-05-14T12:00:00+00:00",
                                "source_details": [{"source_name": "nws", "forecast_high": 72.0}],
                            },
                            shared_candidate={
                                "candidate_id": candidate_id,
                                "market_id": "KXHIGHSEA-260515-T70",
                                "market": {
                                    "id": "KXHIGHSEA-260515-T70",
                                    "question": "Will Seattle high temperature be above 70 degrees on May 15, 2026?",
                                    "market_kind": "high",
                                    "contract_shape": "tail",
                                },
                                "evidence": {
                                    "weather_source_snapshot": {
                                        "as_of": "2026-05-14T12:00:00+00:00",
                                        "settlement_source": "kalshi_settlement",
                                        "forecast": {
                                            "threshold": 70.0,
                                            "question_side": "above",
                                            "actual_temp_used": 73.0,
                                            "actual_outcome": "YES",
                                        },
                                    }
                                },
                                "resolution": {
                                    "actual_source": "nws_observed",
                                    "actual_outcome": "YES",
                                    "resolved_outcome": "YES",
                                    "settled_side": "YES",
                                    "resolved_at": "2026-05-16T13:00:00+00:00",
                                    "known_after": "2026-05-16T13:00:00+00:00",
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

        future_pnl_inputs = lane_row["provenance"]["future_pnl_inputs"]
        self.assertEqual(future_pnl_inputs["threshold"], 70.0)
        self.assertEqual(future_pnl_inputs["question_side"], "above")
        self.assertEqual(future_pnl_inputs["market_kind"], "high")
        self.assertEqual(future_pnl_inputs["contract_shape"], "tail")
        self.assertEqual(
            future_pnl_inputs["question"],
            "Will Seattle high temperature be above 70 degrees on May 15, 2026?",
        )
        for forbidden_key in ("actual_outcome", "resolved_outcome", "settled_side"):
            self.assertNotIn(forbidden_key, future_pnl_inputs)

    def test_shadow_source_router_writes_independent_buy_no_with_side_specific_price(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            decision_path = Path(tmpdir) / "lanes.jsonl"
            scoreboard_path = Path(tmpdir) / "source_scoreboard_by_slice.jsonl"
            append_jsonl(
                scoreboard_path,
                {
                    "source_id": "nws",
                    "source_name": "nws",
                    "city_id": "seattle_wa",
                    "market_kind": "high",
                    "contract_shape": "tail",
                    "sample_count": 200,
                    "threshold_direction_accuracy": 0.95,
                },
            )
            candidate_id = "candidate-router-buy-no"
            stable_decision = {
                "shared_candidate_id": candidate_id,
                "wallet_id": "stable_paper",
                "run_id": "stable-run",
                "candidate_dataset_path": str(dataset_path),
                "decision_role": "paper_shadow",
                "decision_id": "stable-source-decision",
                "policy": "normal",
                "market_id": "KXHIGHSEA-260515-T70",
                "observed_at": "2026-05-14T12:00:00+00:00",
                "action": "BUY_YES",
                "reason_code": "approved",
                "reason": "stable approved",
                "confidence": 0.88,
                "requested_position_size_usd": 10.0,
                "approved_position_size_usd": 10.0,
            }

            result = write_paper_shadow_lane_decisions(
                config={
                    "paper_shadow_lanes": {
                        "enabled": True,
                        "decision_ledger_path": str(decision_path),
                        "enabled_lanes": ["shadow_source_router"],
                        "source_scoreboard_path": str(scoreboard_path),
                        "shadow_source_router": {
                            "enabled": True,
                            "parameters": {"hypothetical_notional_usd": 12.5},
                        },
                    }
                },
                candidate_dataset_path=dataset_path,
                inputs_by_shared_candidate_id={
                    candidate_id: {
                        "stable": SimpleNamespace(
                            signal={
                                "shared_candidate_id": candidate_id,
                                "market_id": "KXHIGHSEA-260515-T70",
                                "question": "Will Seattle high temperature be above 70 degrees on May 15, 2026?",
                                "city_id": "seattle_wa",
                                "threshold": 70.0,
                                "question_side": "above",
                                "contract_shape": "tail",
                                "direction": "BUY_YES",
                                "confidence": 0.88,
                                "market_price": 0.44,
                                "candidate_observed_at": "2026-05-14T12:00:00+00:00",
                                "best_yes_ask": 0.44,
                                "best_yes_bid": 0.42,
                                "best_no_ask": 0.58,
                                "best_no_bid": 0.56,
                                "source_details": [
                                    {
                                        "source_id": "nws",
                                        "source_name": "nws",
                                        "forecast_high": 68.0,
                                        "observed_at": "2026-05-14T11:55:00+00:00",
                                    },
                                    {
                                        "source_id": "local_station_98101",
                                        "source_name": "Seattle local station 98101",
                                        "forecast_high": 69.0,
                                        "observed_at": "2026-05-14T11:56:00+00:00",
                                    },
                                ],
                            },
                            shared_candidate={
                                "candidate_id": candidate_id,
                                "market_id": "KXHIGHSEA-260515-T70",
                                "market": {
                                    "id": "KXHIGHSEA-260515-T70",
                                    "question": "Will Seattle high temperature be above 70 degrees on May 15, 2026?",
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

        self.assertEqual(result.lane_ids, ("shadow_source_router",))
        self.assertEqual(lane_row["action"], "BUY_NO")
        self.assertEqual(lane_row["side"], "NO")
        self.assertEqual(lane_row["entry_price"], 0.58)
        self.assertEqual(lane_row["price"], 0.58)
        self.assertEqual(lane_row["requested_position_size_usd"], 12.5)
        self.assertEqual(lane_row["approved_position_size_usd"], 12.5)
        self.assertFalse(lane_row["mutation_contract"]["mutates_accounting"])
        router = lane_row["provenance"]["source_router"]
        self.assertEqual(router["recommended_action"], "BUY_NO")
        self.assertEqual(router["source_direction"], "NO")
        self.assertEqual(router["scoreboard_path"], str(scoreboard_path))
        self.assertEqual(router["future_pnl_inputs"]["recommended_action"], "BUY_NO")
        self.assertEqual(router["future_pnl_inputs"]["side"], "NO")
        self.assertEqual(router["future_pnl_inputs"]["estimated_fill_price"], 0.58)
        self.assertEqual(router["future_pnl_inputs"]["approved_position_size_usd"], 12.5)
        self.assertEqual(lane_row["provenance"]["baseline_action"], "BUY_YES")
        source_ids = {row["source_id"] for row in router["source_observations"]}
        self.assertIn("nws", source_ids)
        self.assertIn("local_station_98101", source_ids)

    def test_shadow_source_router_preserves_source_observations_when_history_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            decision_path = Path(tmpdir) / "lanes.jsonl"
            candidate_id = "candidate-router-no-history"

            result = write_paper_shadow_lane_decisions(
                config={
                    "paper_shadow_lanes": {
                        "enabled": True,
                        "decision_ledger_path": str(decision_path),
                        "enabled_lanes": ["shadow_source_router"],
                        "shadow_source_router": {"enabled": True},
                    }
                },
                candidate_dataset_path=dataset_path,
                inputs_by_shared_candidate_id={
                    candidate_id: {
                        "stable": SimpleNamespace(
                            signal={
                                "shared_candidate_id": candidate_id,
                                "market_id": "KXHIGHSEA-260515-T70",
                                "question": "Will Seattle high temperature be above 70 degrees on May 15, 2026?",
                                "city_id": "seattle_wa",
                                "threshold": 70.0,
                                "question_side": "above",
                                "confidence": 0.88,
                                "source_details": [
                                    {
                                        "source_id": "local_station_98101",
                                        "source_name": "Seattle local station 98101",
                                        "forecast_high": 69.0,
                                    }
                                ],
                            },
                            shared_candidate={"candidate_id": candidate_id, "market_id": "KXHIGHSEA-260515-T70"},
                        ),
                    }
                },
                wallet_decision_rows={"stable_paper": [], "beta_paper": []},
                wallet_runs={"stable_paper": SimpleNamespace(session_id="stable-run")},
                ledger_root=tmpdir,
            )
            lane_row = load_jsonl(Path(result.decision_path))[0]

        self.assertEqual(lane_row["action"], "SKIP")
        self.assertEqual(lane_row["approved_position_size_usd"], 0.0)
        router = lane_row["provenance"]["source_router"]
        self.assertEqual(router["recommended_action"], "SKIP")
        self.assertEqual(router["source_direction"], "UNKNOWN")
        self.assertEqual(router["source_observations"][0]["source_id"], "local_station_98101")
        self.assertEqual(router["future_pnl_inputs"]["recommended_action"], "SKIP")

    # ── source router edge gate tests ──

    def test_compute_source_router_edge_buy_yes(self):
        from bot.paper_shadow_lanes import _compute_source_router_edge

        # Positive edge: model says 65%, market says 55%
        edge = _compute_source_router_edge(
            {"model_probability": 0.65, "best_yes_ask": 0.55},
            "BUY_YES",
        )
        self.assertAlmostEqual(edge, 0.10)

        # Negative edge: model says 45%, market says 55%
        edge = _compute_source_router_edge(
            {"model_probability": 0.45, "best_yes_ask": 0.55},
            "BUY_YES",
        )
        self.assertAlmostEqual(edge, -0.10)

        # Fallback to market_price when best_yes_ask missing
        edge = _compute_source_router_edge(
            {"model_probability": 0.60, "market_price": 0.50},
            "BUY_YES",
        )
        self.assertAlmostEqual(edge, 0.10)

    def test_compute_source_router_edge_buy_no(self):
        from bot.paper_shadow_lanes import _compute_source_router_edge

        # Positive NO edge: model says 10% YES → 90% NO, market says 80% NO
        edge = _compute_source_router_edge(
            {"model_probability": 0.10, "best_no_ask": 0.80},
            "BUY_NO",
        )
        self.assertAlmostEqual(edge, 0.10)

        # Negative NO edge: model says 30% YES → 70% NO, market says 80% NO
        edge = _compute_source_router_edge(
            {"model_probability": 0.30, "best_no_ask": 0.80},
            "BUY_NO",
        )
        self.assertAlmostEqual(edge, -0.10)

        # Edge with fallback: best_no_ask missing → use 1.0 - market_price
        edge = _compute_source_router_edge(
            {"model_probability": 0.15, "market_price": 0.20},
            "BUY_NO",
        )
        self.assertAlmostEqual(edge, 0.05)  # (1-0.15) - (1-0.20) = 0.85 - 0.80

    def test_compute_source_router_edge_missing_data(self):
        from bot.paper_shadow_lanes import _compute_source_router_edge

        # No model_probability → None
        self.assertIsNone(_compute_source_router_edge({}, "BUY_YES"))
        self.assertIsNone(_compute_source_router_edge({}, "BUY_NO"))

        # model_probability but no price → None
        self.assertIsNone(
            _compute_source_router_edge({"model_probability": 0.50}, "BUY_YES")
        )
        # model_probability but no NO-only price and no market_price → None
        self.assertIsNone(
            _compute_source_router_edge({"model_probability": 0.50}, "BUY_NO")
        )

    def test_compute_source_router_edge_ignores_zero_price(self):
        from bot.paper_shadow_lanes import _compute_source_router_edge

        self.assertIsNone(
            _compute_source_router_edge(
                {"model_probability": 0.60, "best_yes_ask": 0.0, "market_price": 0.0},
                "BUY_YES",
            )
        )
        self.assertIsNone(
            _compute_source_router_edge(
                {"model_probability": 0.40, "best_no_ask": 0.0, "market_price": 0.0},
                "BUY_NO",
            )
        )

    def test_shadow_source_router_uses_side_ask_when_signal_market_price_is_zero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            decision_path = Path(tmpdir) / "lanes.jsonl"
            scoreboard_path = Path(tmpdir) / "source_scoreboard_by_slice.jsonl"
            append_jsonl(
                scoreboard_path,
                {
                    "source_id": "nws",
                    "source_name": "nws",
                    "city_id": "seattle_wa",
                    "market_kind": "high",
                    "contract_shape": "tail",
                    "sample_count": 200,
                    "threshold_direction_accuracy": 0.95,
                },
            )
            candidate_id = "candidate-router-zero-market-price"
            result = write_paper_shadow_lane_decisions(
                config={
                    "paper_shadow_lanes": {
                        "enabled": True,
                        "decision_ledger_path": str(decision_path),
                        "enabled_lanes": ["shadow_source_router"],
                        "source_scoreboard_path": str(scoreboard_path),
                        "shadow_source_router": {
                            "enabled": True,
                            "parameters": {"hypothetical_notional_usd": 12.5},
                        },
                    }
                },
                candidate_dataset_path=dataset_path,
                inputs_by_shared_candidate_id={
                    candidate_id: {
                        "stable": SimpleNamespace(
                            signal={
                                "shared_candidate_id": candidate_id,
                                "market_id": "KXHIGHSEA-260515-T70",
                                "question": "Will Seattle high temperature be above 70 degrees on May 15, 2026?",
                                "city_id": "seattle_wa",
                                "threshold": 70.0,
                                "question_side": "above",
                                "direction": "BUY_YES",
                                "confidence": 0.88,
                                "market_price": 0.0,
                                "candidate_observed_at": "2026-05-14T12:00:00+00:00",
                                "best_yes_ask": 0.0,
                                "best_no_ask": 0.58,
                                "source_details": [
                                    {
                                        "source_id": "nws",
                                        "source_name": "nws",
                                        "forecast_high": 68.0,
                                    },
                                ],
                            },
                            shared_candidate={
                                "candidate_id": candidate_id,
                                "market_id": "KXHIGHSEA-260515-T70",
                            },
                        ),
                    }
                },
                wallet_decision_rows={"stable_paper": [], "beta_paper": []},
                wallet_runs={"stable_paper": SimpleNamespace(session_id="stable-run")},
                ledger_root=tmpdir,
            )
            lane_row = load_jsonl(Path(result.decision_path))[0]

        self.assertEqual(lane_row["action"], "BUY_NO")
        self.assertEqual(lane_row["side"], "NO")
        self.assertEqual(lane_row["entry_price"], 0.58)
        self.assertEqual(lane_row["price"], 0.58)
        self.assertEqual(lane_row["provenance"]["source_router"]["future_pnl_inputs"]["estimated_fill_price"], 0.58)

    def test_source_router_edge_gate_skips_when_edge_below_minimum(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            decision_path = Path(tmpdir) / "lanes.jsonl"
            scoreboard_path = Path(tmpdir) / "source_scoreboard_by_slice.jsonl"
            append_jsonl(
                scoreboard_path,
                {
                    "source_id": "nws",
                    "source_name": "nws",
                    "city_id": "seattle_wa",
                    "market_kind": "high",
                    "contract_shape": "tail",
                    "sample_count": 200,
                    "threshold_direction_accuracy": 0.95,
                },
            )
            candidate_id = "candidate-edge-gate-skip"
            result = write_paper_shadow_lane_decisions(
                config={
                    "paper_shadow_lanes": {
                        "enabled": True,
                        "decision_ledger_path": str(decision_path),
                        "enabled_lanes": ["shadow_source_router"],
                        "source_scoreboard_path": str(scoreboard_path),
                        "shadow_source_router": {
                            "enabled": True,
                            "parameters": {
                                "hypothetical_notional_usd": 10.0,
                                "min_edge": 0.03,
                            },
                        },
                    }
                },
                candidate_dataset_path=dataset_path,
                inputs_by_shared_candidate_id={
                    candidate_id: {
                        "stable": SimpleNamespace(
                            signal={
                                "shared_candidate_id": candidate_id,
                                "market_id": "KXHIGHSEA-260515-T70",
                                "question": "Will Seattle high temperature be above 70 degrees on May 15, 2026?",
                                "city_id": "seattle_wa",
                                "threshold": 70.0,
                                "question_side": "above",
                                "contract_shape": "tail",
                                "direction": "BUY_YES",
                                "confidence": 0.88,
                                "market_price": 0.44,
                                "model_probability": 0.50,
                                "candidate_observed_at": "2026-05-14T12:00:00+00:00",
                                "best_yes_ask": 0.52,
                                "best_no_ask": 0.50,
                                "source_details": [
                                    {
                                        "source_id": "nws",
                                        "source_name": "nws",
                                        "forecast_high": 68.0,
                                        "observed_at": "2026-05-14T11:55:00+00:00",
                                    },
                                ],
                            },
                            shared_candidate={
                                "candidate_id": candidate_id,
                                "market_id": "KXHIGHSEA-260515-T70",
                                "market": {
                                    "id": "KXHIGHSEA-260515-T70",
                                    "question": "Will Seattle high temperature be above 70 degrees on May 15, 2026?",
                                },
                            },
                        ),
                    }
                },
                wallet_decision_rows={"stable_paper": [], "beta_paper": []},
                wallet_runs={"stable_paper": SimpleNamespace(session_id="stable-run")},
                ledger_root=tmpdir,
            )
            lane_row = load_jsonl(Path(result.decision_path))[0]

        self.assertEqual(lane_row["action"], "SKIP")
        self.assertEqual(lane_row["reason_code"], "source_router_insufficient_edge")
        self.assertIn("edge", lane_row["reason"])
        self.assertIn("minimum 0.0300", lane_row["reason"])
        self.assertEqual(lane_row["approved_position_size_usd"], 0.0)
        # Source direction is still recorded even though trade was skipped
        router = lane_row["provenance"]["source_router"]
        self.assertIn(router["source_direction"], ("YES", "NO"))
        self.assertEqual(router["recommended_action"], "SKIP")
        self.assertEqual(router["reason_code"], "source_router_insufficient_edge")

    def test_source_router_edge_gate_skips_when_edge_unavailable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            decision_path = Path(tmpdir) / "lanes.jsonl"
            scoreboard_path = Path(tmpdir) / "source_scoreboard_by_slice.jsonl"
            append_jsonl(
                scoreboard_path,
                {
                    "source_id": "nws",
                    "source_name": "nws",
                    "city_id": "seattle_wa",
                    "market_kind": "high",
                    "contract_shape": "tail",
                    "sample_count": 200,
                    "threshold_direction_accuracy": 0.95,
                },
            )
            candidate_id = "candidate-edge-gate-unavailable"
            result = write_paper_shadow_lane_decisions(
                config={
                    "paper_shadow_lanes": {
                        "enabled": True,
                        "decision_ledger_path": str(decision_path),
                        "enabled_lanes": ["shadow_source_router"],
                        "source_scoreboard_path": str(scoreboard_path),
                        "shadow_source_router": {
                            "enabled": True,
                            "parameters": {
                                "hypothetical_notional_usd": 10.0,
                                "min_edge": 0.03,
                            },
                        },
                    }
                },
                candidate_dataset_path=dataset_path,
                inputs_by_shared_candidate_id={
                    candidate_id: {
                        "stable": SimpleNamespace(
                            signal={
                                "shared_candidate_id": candidate_id,
                                "market_id": "KXHIGHSEA-260515-T70",
                                "question": "Will Seattle high temperature be above 70 degrees on May 15, 2026?",
                                "city_id": "seattle_wa",
                                "threshold": 70.0,
                                "question_side": "above",
                                "contract_shape": "tail",
                                "direction": "BUY_YES",
                                "confidence": 0.88,
                                "market_price": 0.44,
                                "candidate_observed_at": "2026-05-14T12:00:00+00:00",
                                "best_yes_ask": 0.44,
                                "best_no_ask": 0.58,
                                "source_details": [
                                    {
                                        "source_id": "nws",
                                        "source_name": "nws",
                                        "forecast_high": 68.0,
                                        "observed_at": "2026-05-14T11:55:00+00:00",
                                    },
                                ],
                            },
                            shared_candidate={
                                "candidate_id": candidate_id,
                                "market_id": "KXHIGHSEA-260515-T70",
                                "market": {
                                    "id": "KXHIGHSEA-260515-T70",
                                    "question": "Will Seattle high temperature be above 70 degrees on May 15, 2026?",
                                },
                            },
                        ),
                    }
                },
                wallet_decision_rows={"stable_paper": [], "beta_paper": []},
                wallet_runs={"stable_paper": SimpleNamespace(session_id="stable-run")},
                ledger_root=tmpdir,
            )
            lane_row = load_jsonl(Path(result.decision_path))[0]

        self.assertEqual(lane_row["action"], "SKIP")
        self.assertEqual(lane_row["reason_code"], "source_router_edge_unavailable")
        self.assertEqual(lane_row["approved_position_size_usd"], 0.0)
        router = lane_row["provenance"]["source_router"]
        self.assertEqual(router["recommended_action"], "SKIP")
        self.assertEqual(router["reason_code"], "source_router_edge_unavailable")

    def test_source_router_edge_gate_disabled_when_min_edge_not_set(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dataset_path = Path(tmpdir) / "shared" / "prediction_lab" / "market_snapshots.jsonl"
            decision_path = Path(tmpdir) / "lanes.jsonl"
            scoreboard_path = Path(tmpdir) / "source_scoreboard_by_slice.jsonl"
            append_jsonl(
                scoreboard_path,
                {
                    "source_id": "nws",
                    "source_name": "nws",
                    "city_id": "seattle_wa",
                    "market_kind": "high",
                    "contract_shape": "tail",
                    "sample_count": 200,
                    "threshold_direction_accuracy": 0.95,
                },
            )
            candidate_id = "candidate-edge-gate-disabled"
            result = write_paper_shadow_lane_decisions(
                config={
                    "paper_shadow_lanes": {
                        "enabled": True,
                        "decision_ledger_path": str(decision_path),
                        "enabled_lanes": ["shadow_source_router"],
                        "source_scoreboard_path": str(scoreboard_path),
                        "shadow_source_router": {
                            "enabled": True,
                            "parameters": {
                                "hypothetical_notional_usd": 10.0,
                            },
                        },
                    }
                },
                candidate_dataset_path=dataset_path,
                inputs_by_shared_candidate_id={
                    candidate_id: {
                        "stable": SimpleNamespace(
                            signal={
                                "shared_candidate_id": candidate_id,
                                "market_id": "KXHIGHSEA-260515-T70",
                                "question": "Will Seattle high temperature be above 70 degrees on May 15, 2026?",
                                "city_id": "seattle_wa",
                                "threshold": 70.0,
                                "question_side": "above",
                                "contract_shape": "tail",
                                "direction": "BUY_YES",
                                "confidence": 0.88,
                                "market_price": 0.44,
                                "candidate_observed_at": "2026-05-14T12:00:00+00:00",
                                "best_yes_ask": 0.44,
                                "best_no_ask": 0.58,
                                "source_details": [
                                    {
                                        "source_id": "nws",
                                        "source_name": "nws",
                                        "forecast_high": 68.0,
                                        "observed_at": "2026-05-14T11:55:00+00:00",
                                    },
                                ],
                            },
                            shared_candidate={
                                "candidate_id": candidate_id,
                                "market_id": "KXHIGHSEA-260515-T70",
                                "market": {
                                    "id": "KXHIGHSEA-260515-T70",
                                    "question": "Will Seattle high temperature be above 70 degrees on May 15, 2026?",
                                },
                            },
                        ),
                    }
                },
                wallet_decision_rows={"stable_paper": [], "beta_paper": []},
                wallet_runs={"stable_paper": SimpleNamespace(session_id="stable-run")},
                ledger_root=tmpdir,
            )
            lane_row = load_jsonl(Path(result.decision_path))[0]

        # Without min_edge param, the lane should work normally (no gating)
        self.assertNotEqual(lane_row["reason_code"], "source_router_insufficient_edge")

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

    def test_paper_shadow_lane_report_includes_source_scoreboard_coverage_counts(self):
        lane_rows = [
            {
                "policy": "shadow_source_scoreboard",
                "shared_candidate_id": "candidate-1",
                "action": "BUY_YES",
                "provenance": {
                    "source_scoreboard": {
                        "available": True,
                        "recommended_action": "BUY_YES",
                        "reason_code": "trusted_support",
                        "future_pnl_inputs": {
                            "estimated_fill_price": 0.44,
                            "best_yes_ask": 0.44,
                            "actual_source": "nws_observed",
                            "settlement_source": "kalshi_settlement",
                            "label_target": "nws_observed",
                        },
                    }
                },
            },
            {
                "policy": "shadow_source_scoreboard",
                "shared_candidate_id": "candidate-2",
                "action": "BUY_YES",
                "provenance": {
                    "source_scoreboard": {
                        "available": False,
                        "recommended_action": "SKIP",
                        "reason_code": "source_reliability_unavailable",
                        "future_pnl_inputs": {"label_target": "unknown"},
                    }
                },
            },
        ]

        report = summarize_paper_shadow_lane_report(lane_rows=lane_rows)

        scoreboard = report["source_scoreboard"]
        self.assertEqual(scoreboard["evaluated_rows"], 2)
        self.assertEqual(scoreboard["lane_row_counts"], {"shadow_source_scoreboard": 2})
        self.assertEqual(scoreboard["available_rows"], 1)
        self.assertEqual(scoreboard["unavailable_rows"], 1)
        self.assertEqual(scoreboard["recommended_action_counts"], {"BUY_YES": 1, "SKIP": 1})
        self.assertEqual(
            scoreboard["reason_code_counts"],
            {"source_reliability_unavailable": 1, "trusted_support": 1},
        )
        self.assertEqual(scoreboard["rows_with_estimated_fill_price"], 1)
        self.assertEqual(scoreboard["rows_with_order_book_execution_prices"], 1)
        self.assertEqual(scoreboard["label_source_counts"], {"nws_observed": 1, "unknown": 1})
        self.assertEqual(scoreboard["actual_source_counts"], {"nws_observed": 1})
        self.assertEqual(scoreboard["settlement_source_counts"], {"kalshi_settlement": 1})

    def test_paper_shadow_lane_report_keeps_source_reliability_and_scoreboard_counts_separate(self):
        lane_rows = [
            {
                "policy": "shadow_source_reliability",
                "shared_candidate_id": "candidate-1",
                "action": "BUY_YES",
                "provenance": {
                    "source_reliability": {
                        "available": True,
                        "recommended_action": "BUY_YES",
                        "reason_code": "trusted_support",
                    }
                },
            },
            {
                "policy": "shadow_source_scoreboard",
                "shared_candidate_id": "candidate-2",
                "action": "BUY_YES",
                "provenance": {
                    "source_scoreboard": {
                        "available": False,
                        "recommended_action": "SKIP",
                        "reason_code": "source_reliability_unavailable",
                        "future_pnl_inputs": {"label_target": "unknown"},
                    }
                },
            },
        ]

        report = summarize_paper_shadow_lane_report(lane_rows=lane_rows)

        self.assertEqual(report["source_reliability"]["evaluated_rows"], 1)
        self.assertEqual(report["source_reliability"]["lane_row_counts"], {"shadow_source_reliability": 1})
        self.assertEqual(report["source_reliability"]["recommended_action_counts"], {"BUY_YES": 1})
        self.assertEqual(report["source_reliability"]["reason_code_counts"], {"trusted_support": 1})
        self.assertEqual(report["source_scoreboard"]["evaluated_rows"], 1)
        self.assertEqual(report["source_scoreboard"]["lane_row_counts"], {"shadow_source_scoreboard": 1})
        self.assertEqual(report["source_scoreboard"]["recommended_action_counts"], {"SKIP": 1})
        self.assertEqual(
            report["source_scoreboard"]["reason_code_counts"],
            {"source_reliability_unavailable": 1},
        )

    def test_paper_shadow_lane_report_includes_source_scoreboard_readiness_counts(self):
        lane_rows = [
            {
                "policy": "shadow_source_scoreboard",
                "shared_candidate_id": "candidate-1",
                "action": "BUY_YES",
                "provenance": {
                    "source_reliability": {"tier_counts": {"trusted": 1}},
                    "source_scoreboard": {
                        "available": True,
                        "recommended_action": "BUY_YES",
                        "reason_code": "trusted_support",
                        "future_pnl_inputs": {
                            "shared_candidate_id": "candidate-1",
                            "market_id": "KXHIGHSEA-1",
                            "observed_at": "2026-05-14T12:00:00+00:00",
                            "known_after": "2026-05-16T13:00:00+00:00",
                            "actual_outcome": "YES",
                            "resolved_outcome": "YES",
                            "label_target": "nws_observed",
                            "actual_source": "nws_observed",
                            "settlement_source": "kalshi_settlement",
                            "estimated_fill_price": 0.44,
                            "best_yes_ask": 0.44,
                            "execution_snapshot_source": "book",
                        },
                    },
                },
            },
            {
                "policy": "shadow_source_scoreboard",
                "shared_candidate_id": "candidate-2",
                "action": "BUY_YES",
                "provenance": {
                    "source_reliability": {"tier_counts": {"neutral": 1}},
                    "source_scoreboard": {
                        "available": True,
                        "recommended_action": "SKIP",
                        "reason_code": "no_trusted_support",
                        "future_pnl_inputs": {
                            "shared_candidate_id": "candidate-2",
                            "market_id": "KXHIGHSEA-2",
                            "observed_at": "2026-05-17T12:00:00+00:00",
                            "known_after": "2026-05-16T13:00:00+00:00",
                            "actual_outcome": "NO",
                            "resolved_outcome": "NO",
                            "label_target": "kalshi_settlement",
                            "settlement_source": "kalshi_settlement",
                        },
                    },
                },
            },
            {
                "policy": "shadow_source_scoreboard",
                "shared_candidate_id": "candidate-3",
                "action": "BUY_YES",
                "provenance": {
                    "source_reliability": {"tier_counts": {"excluded": 1}},
                    "source_scoreboard": {
                        "available": False,
                        "recommended_action": "SKIP",
                        "reason_code": "source_reliability_unavailable",
                        "future_pnl_inputs": {
                            "shared_candidate_id": "candidate-3",
                            "market_id": "KXHIGHSEA-3",
                            "observed_at": "2026-05-14T12:00:00+00:00",
                            "label_target": "unknown",
                        },
                    },
                },
            },
        ]

        report = summarize_paper_shadow_lane_report(lane_rows=lane_rows)

        readiness = report["source_scoreboard_readiness"]
        self.assertEqual(readiness["evaluated_rows"], 3)
        self.assertTrue(readiness["recommendation_only"])
        self.assertEqual(
            readiness["label_source_counts"],
            {"kalshi_settlement": 1, "nws_observed": 1, "unknown": 1},
        )
        self.assertEqual(
            readiness["label_class_counts"],
            {
                "explicit_non_independent": 0,
                "independent": 0,
                "settlement_derived": 2,
                "unknown": 1,
            },
        )
        self.assertEqual(readiness["explicit_label_rows"], 2)
        self.assertEqual(readiness["independent_label_rows"], 0)
        self.assertEqual(readiness["order_book_quote_rows"], 1)
        self.assertEqual(readiness["execution_snapshot_rows"], 1)
        self.assertEqual(readiness["estimated_fill_price_rows"], 1)
        self.assertEqual(
            readiness["reliability_tier_counts"],
            {"excluded": 1, "neutral": 1, "trusted": 1},
        )
        self.assertEqual(readiness["rows_with_trusted_sources"], 1)
        self.assertEqual(readiness["rows_with_neutral_sources"], 1)
        self.assertEqual(readiness["rows_with_excluded_sources"], 1)
        self.assertEqual(
            readiness["reason_code_counts"],
            {
                "no_trusted_support": 1,
                "source_reliability_unavailable": 1,
                "trusted_support": 1,
            },
        )
        self.assertEqual(
            readiness["leak_risk_indicators"],
            {
                "known_after_not_after_observed_at_rows": 1,
                "label_matches_settlement_source_rows": 1,
                "settlement_derived_label_rows": 2,
                "unknown_label_rows": 1,
            },
        )
        self.assertEqual(
            readiness["missing_field_blockers"],
            {
                "missing_actual_outcome_rows": 1,
                "missing_estimated_fill_price_rows": 2,
                "missing_execution_snapshot_rows": 2,
                "missing_known_after_rows": 1,
                "missing_label_source_rows": 1,
                "missing_market_id_rows": 0,
                "missing_observed_at_rows": 0,
                "missing_order_book_quotes_rows": 2,
                "missing_resolution_outcome_rows": 1,
                "missing_shared_candidate_id_rows": 0,
            },
        )
        self.assertEqual(readiness["rows_with_any_blocker"], 2)
        self.assertEqual(readiness["rows_with_any_leak_risk"], 3)

    def test_paper_shadow_lane_report_cli_prints_source_scoreboard_readiness(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lane_path = Path(tmpdir) / "paper_shadow_lane_decisions.jsonl"
            append_jsonl(
                lane_path,
                {
                    "policy": "shadow_source_scoreboard",
                    "shared_candidate_id": "candidate-1",
                    "action": "BUY_YES",
                    "provenance": {
                        "source_reliability": {"tier_counts": {"trusted": 1}},
                        "source_scoreboard": {
                            "available": True,
                            "recommended_action": "BUY_YES",
                            "reason_code": "trusted_support",
                            "future_pnl_inputs": {
                                "shared_candidate_id": "candidate-1",
                                "market_id": "KXHIGHSEA-1",
                                "observed_at": "2026-05-14T12:00:00+00:00",
                                "known_after": "2026-05-16T13:00:00+00:00",
                                "actual_outcome": "YES",
                                "resolved_outcome": "YES",
                                "label_target": "nws_observed",
                                "actual_source": "nws_observed",
                                "settlement_source": "kalshi_settlement",
                                "estimated_fill_price": 0.44,
                                "best_yes_ask": 0.44,
                                "execution_snapshot_source": "book",
                            },
                        },
                    },
                },
            )

            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = paper_shadow_lane_report_main(
                    ["--lane-decision-path", str(lane_path), "--format", "json"]
                )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["evaluated_rows"], 1)
        self.assertEqual(payload["independent_label_rows"], 0)
        self.assertEqual(payload["order_book_quote_rows"], 1)

    def test_repo_source_scoreboard_runtime_config_uses_isolated_output_path(self):
        config_path = REPO_ROOT / "data/runtime_configs/paper_source_scoreboard_shadow_20260516.yaml"
        if not config_path.exists():
            self.skipTest("ignored runtime-local scoreboard config is not present in this worktree")

        with patch.dict(os.environ, {}, clear=True):
            config = load_config(config_path)

        paper_shadow_lanes = config["paper_shadow_lanes"]
        self.assertTrue(paper_shadow_lanes["enabled"])
        self.assertEqual(
            paper_shadow_lanes["enabled_lanes"],
            [
                "control_stable",
                "shadow_confidence_floor",
                "shadow_current_beta",
                "shadow_source_scoreboard",
                "shadow_source_router",
            ],
        )
        self.assertEqual(
            paper_shadow_lanes["decision_ledger_path"],
            "data/beta_shadow/paper/source_scoreboard/paper_shadow_lane_decisions.jsonl",
        )
        self.assertEqual(
            paper_shadow_lanes["source_scoreboard_path"],
            "/tmp/weather_source_scoreboard_beta_smoke/source_scoreboard_by_slice.jsonl",
        )
        self.assertTrue(paper_shadow_lanes["shadow_source_router"]["enabled"])
        self.assertEqual(
            paper_shadow_lanes["shadow_source_router"]["parameters"]["hypothetical_notional_usd"],
            10.0,
        )
        self.assertFalse(config["alerts"]["enabled"])
        self.assertFalse(config["alerts"]["telegram_enabled"])

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


    def test_source_scoreboard_resolution_rows_preserve_replayable_pnl_inputs(self):
        lane_rows = [
            {
                "decision_id": "lane-decision-1",
                "agent_run_id": "agent-run",
                "run_id": "lane-run",
                "policy": "shadow_source_scoreboard",
                "shared_candidate_id": "candidate-win",
                "market_id": "KXHIGHSEA-1",
                "observed_at": "2026-05-14T12:00:00+00:00",
                "action": "BUY_YES",
                "approved_position_size_usd": 10.0,
                "provenance": {
                    "source_scoreboard": {
                        "recommended_action": "BUY_YES",
                        "future_pnl_inputs": {
                            "shared_candidate_id": "candidate-win",
                            "market_id": "KXHIGHSEA-1",
                            "recommended_action": "BUY_YES",
                            "side": "YES",
                            "entry_price": 0.24,
                            "estimated_fill_price": 0.25,
                            "stable_approved_position_size_usd": 10.0,
                        },
                    }
                },
            }
        ]
        resolution_rows = [{"market_id": "KXHIGHSEA-1", "resolution": {"outcome": "YES", "resolved_at": "2026-05-16T13:00:00+00:00"}}]

        joined = build_paper_shadow_lane_resolution_rows(lane_rows=lane_rows, resolution_rows=resolution_rows)

        self.assertEqual(len(joined), 1)
        row = joined[0]
        self.assertEqual(row["schema_name"], "paper_shadow_lane_resolution")
        self.assertTrue(row["non_mutating"])
        self.assertEqual(row["lane_decision_id"], "lane-decision-1")
        self.assertEqual(row["lane_id"], "shadow_source_scoreboard")
        self.assertEqual(row["shared_candidate_id"], "candidate-win")
        self.assertEqual(row["market_id"], "KXHIGHSEA-1")
        self.assertEqual(row["action"], "BUY_YES")
        self.assertEqual(row["side"], "YES")
        self.assertEqual(row["entry_price"], 0.24)
        self.assertEqual(row["fill_price"], 0.25)
        self.assertEqual(row["notional_usd"], 10.0)
        self.assertEqual(row["resolution"]["matched"], True)
        self.assertEqual(row["resolution"]["match_source"], "market_id")
        self.assertEqual(row["resolution"]["outcome"], "YES")
        self.assertEqual(row["pnl"], {"calculable": True, "stake_usd": 10.0, "contracts": 40.0, "payout_usd": 40.0, "pnl_usd": 30.0, "won": True})
        self.assertEqual(row["blocker"], None)
        self.assertEqual(row["replay_sizing"]["recorded_notional_usd"], 10.0)
        self.assertEqual(row["replay_sizing"]["sizing_source"], "lane_approved_position_size_usd")
        self.assertTrue(row["replay_sizing"]["replayable_with_alternate_balance"])

    def test_source_scoreboard_resolved_pnl_joins_lane_rows_to_resolution_rows(self):
        lane_rows = [
            {
                "policy": "shadow_source_scoreboard",
                "shared_candidate_id": "candidate-win",
                "action": "BUY_YES",
                "approved_position_size_usd": 10.0,
                "provenance": {
                    "source_scoreboard": {
                        "recommended_action": "BUY_YES",
                        "future_pnl_inputs": {
                            "shared_candidate_id": "candidate-win",
                            "market_id": "KXHIGHSEA-1",
                            "recommended_action": "BUY_YES",
                            "side": "YES",
                            "estimated_fill_price": 0.25,
                            "stable_approved_position_size_usd": 10.0,
                        },
                    }
                },
            },
            {
                "policy": "shadow_source_scoreboard",
                "shared_candidate_id": "candidate-loss",
                "action": "BUY_NO",
                "approved_position_size_usd": 5.0,
                "provenance": {
                    "source_scoreboard": {
                        "recommended_action": "BUY_NO",
                        "future_pnl_inputs": {
                            "shared_candidate_id": "candidate-loss",
                            "market_id": "KXHIGHSEA-2",
                            "recommended_action": "BUY_NO",
                            "side": "NO",
                            "entry_price": 0.20,
                            "stable_approved_position_size_usd": 5.0,
                        },
                    }
                },
            },
            {
                "policy": "shadow_source_scoreboard",
                "shared_candidate_id": "candidate-skip",
                "action": "SKIP",
                "provenance": {
                    "source_scoreboard": {
                        "recommended_action": "SKIP",
                        "future_pnl_inputs": {
                            "shared_candidate_id": "candidate-skip",
                            "market_id": "KXHIGHSEA-3",
                            "recommended_action": "SKIP",
                        },
                    }
                },
            },
        ]
        resolution_rows = [
            {"shared_candidate_id": "candidate-win", "market_id": "KXHIGHSEA-1", "outcome": "YES"},
            {"market_id": "KXHIGHSEA-2", "outcome": "YES"},
            {"shared_candidate_id": "candidate-skip", "market_id": "KXHIGHSEA-3", "outcome": "NO"},
        ]

        report = summarize_paper_shadow_lane_resolved_pnl(lane_rows=lane_rows, resolution_rows=resolution_rows)

        self.assertEqual(report["evaluated_rows"], 3)
        self.assertEqual(report["resolved_rows"], 3)
        self.assertEqual(report["buy_rows"], 2)
        self.assertEqual(report["skip_rows"], 1)
        self.assertEqual(report["winning_buy_rows"], 1)
        self.assertEqual(report["losing_buy_rows"], 1)
        self.assertEqual(report["total_stake_usd"], 15.0)
        self.assertEqual(report["total_payout_usd"], 40.0)
        self.assertEqual(report["total_pnl_usd"], 25.0)
        self.assertEqual(report["by_lane"]["shadow_source_scoreboard"]["total_pnl_usd"], 25.0)
        self.assertEqual(report["blocker_counts"], {})

    def test_void_resolution_is_excluded_from_buy_pnl(self):
        lane_rows = [
            {
                "policy": "shadow_source_scoreboard",
                "shared_candidate_id": "candidate-void",
                "action": "BUY_YES",
                "approved_position_size_usd": 10.0,
                "provenance": {
                    "future_pnl_inputs": {
                        "shared_candidate_id": "candidate-void",
                        "market_id": "KXVOID-1",
                        "side": "YES",
                        "estimated_fill_price": 0.25,
                    }
                },
            }
        ]
        resolution_rows = [
            {"shared_candidate_id": "candidate-void", "market_id": "KXVOID-1", "outcome": "VOID"}
        ]

        rows = build_paper_shadow_lane_resolution_rows(lane_rows=lane_rows, resolution_rows=resolution_rows)
        report = summarize_paper_shadow_lane_resolved_pnl(lane_rows=lane_rows, resolution_rows=resolution_rows)

        self.assertEqual(rows[0]["resolution"]["outcome"], "VOID")
        self.assertEqual(rows[0]["blocker"], "void_resolution")
        self.assertIsNone(rows[0]["pnl"])
        self.assertEqual(report["resolved_rows"], 1)
        self.assertEqual(report["losing_buy_rows"], 0)
        self.assertEqual(report["total_pnl_usd"], 0.0)
        self.assertEqual(report["blocker_counts"], {"void_resolution": 1})

    def test_incremental_pnl_replay_advances_cursor_and_resolves_pending_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ledger_path = root / "lane_decisions.jsonl"
            resolution_path = root / "resolutions.jsonl"
            state_path = root / "state.json"
            events_path = root / "events.jsonl"

            append_jsonl(
                ledger_path,
                {
                    "decision_id": "decision-win",
                    "policy": "shadow_source_scoreboard",
                    "shared_candidate_id": "candidate-win",
                    "market_id": "KXWIN",
                    "action": "BUY_YES",
                    "approved_position_size_usd": 10.0,
                    "provenance": {
                        "future_pnl_inputs": {
                            "shared_candidate_id": "candidate-win",
                            "market_id": "KXWIN",
                            "recommended_action": "BUY_YES",
                            "side": "YES",
                            "estimated_fill_price": 0.5,
                            "starting_balance_usd": 100.0,
                            "approved_position_size_usd": 10.0,
                        }
                    },
                },
            )
            append_jsonl(
                ledger_path,
                {
                    "decision_id": "decision-later",
                    "policy": "shadow_source_scoreboard",
                    "shared_candidate_id": "candidate-later",
                    "market_id": "KXLATER",
                    "action": "BUY_YES",
                    "approved_position_size_usd": 10.0,
                    "provenance": {
                        "future_pnl_inputs": {
                            "shared_candidate_id": "candidate-later",
                            "market_id": "KXLATER",
                            "recommended_action": "BUY_YES",
                            "side": "YES",
                            "estimated_fill_price": 0.5,
                            "starting_balance_usd": 100.0,
                            "approved_position_size_usd": 10.0,
                        }
                    },
                },
            )
            append_jsonl(resolution_path, {"shared_candidate_id": "candidate-win", "market_id": "KXWIN", "outcome": "YES"})

            state = update_paper_shadow_lane_incremental_pnl(
                lane_decision_path=ledger_path,
                resolution_path=resolution_path,
                state_path=state_path,
                event_output_path=events_path,
                starting_balance_usd=100.0,
                sizing_mode="balance_scaled",
                max_new_rows=1,
            )

            lane = state["lanes"]["shadow_source_scoreboard"]
            self.assertEqual(state["last_run"]["new_rows_read"], 1)
            self.assertEqual(lane["balance_usd"], 110.0)
            self.assertEqual(lane["total_pnl_usd"], 10.0)
            self.assertEqual(state["pending_count"], 0)

            state = update_paper_shadow_lane_incremental_pnl(
                lane_decision_path=ledger_path,
                resolution_path=resolution_path,
                state_path=state_path,
                event_output_path=events_path,
                starting_balance_usd=100.0,
                sizing_mode="balance_scaled",
                max_new_rows=10,
            )

            self.assertEqual(state["pending_count"], 1)
            self.assertEqual(state["lanes"]["shadow_source_scoreboard"]["balance_usd"], 110.0)

            append_jsonl(resolution_path, {"shared_candidate_id": "candidate-later", "market_id": "KXLATER", "outcome": "NO"})
            state = update_paper_shadow_lane_incremental_pnl(
                lane_decision_path=ledger_path,
                resolution_path=resolution_path,
                state_path=state_path,
                event_output_path=events_path,
                starting_balance_usd=100.0,
                sizing_mode="balance_scaled",
                max_new_rows=0,
            )

            lane = state["lanes"]["shadow_source_scoreboard"]
            self.assertEqual(state["last_run"]["resolved_pending_rows"], 1)
            self.assertEqual(state["pending_count"], 0)
            self.assertEqual(lane["total_stake_usd"], 21.0)
            self.assertEqual(lane["total_pnl_usd"], -1.0)
            self.assertEqual(lane["balance_usd"], 99.0)
            self.assertEqual(len(load_jsonl(events_path)), 2)

    def test_source_router_resolved_pnl_reports_raw_win_rate_and_standardized_pnl(self):
        lane_rows = [
            {
                "policy": "shadow_source_router",
                "shared_candidate_id": "router-win",
                "action": "BUY_NO",
                "approved_position_size_usd": 10.0,
                "provenance": {
                    "source_router": {
                        "recommended_action": "BUY_NO",
                        "future_pnl_inputs": {
                            "shared_candidate_id": "router-win",
                            "market_id": "KXROUTER-1",
                            "recommended_action": "BUY_NO",
                            "side": "NO",
                            "estimated_fill_price": 0.25,
                            "approved_position_size_usd": 10.0,
                        },
                    },
                    "future_pnl_inputs": {
                        "shared_candidate_id": "router-win",
                        "market_id": "KXROUTER-1",
                        "recommended_action": "BUY_NO",
                        "side": "NO",
                        "estimated_fill_price": 0.25,
                        "approved_position_size_usd": 10.0,
                    },
                },
            },
            {
                "policy": "shadow_source_router",
                "shared_candidate_id": "router-loss",
                "action": "BUY_YES",
                "approved_position_size_usd": 10.0,
                "provenance": {
                    "future_pnl_inputs": {
                        "shared_candidate_id": "router-loss",
                        "market_id": "KXROUTER-2",
                        "recommended_action": "BUY_YES",
                        "side": "YES",
                        "estimated_fill_price": 0.50,
                        "approved_position_size_usd": 10.0,
                    }
                },
            },
            {
                "policy": "shadow_source_router",
                "shared_candidate_id": "router-skip",
                "action": "SKIP",
                "provenance": {
                    "future_pnl_inputs": {
                        "shared_candidate_id": "router-skip",
                        "market_id": "KXROUTER-3",
                        "recommended_action": "SKIP",
                    }
                },
            },
            {
                "policy": "shadow_source_scoreboard",
                "shared_candidate_id": "scoreboard-row",
                "action": "BUY_YES",
                "approved_position_size_usd": 10.0,
                "provenance": {
                    "future_pnl_inputs": {
                        "shared_candidate_id": "scoreboard-row",
                        "market_id": "KXSCORE-1",
                        "recommended_action": "BUY_YES",
                        "side": "YES",
                        "estimated_fill_price": 0.25,
                    }
                },
            },
        ]
        resolution_rows = [
            {"shared_candidate_id": "router-win", "market_id": "KXROUTER-1", "outcome": "NO"},
            {"shared_candidate_id": "router-loss", "market_id": "KXROUTER-2", "outcome": "NO"},
            {"shared_candidate_id": "router-skip", "market_id": "KXROUTER-3", "outcome": "YES"},
            {"shared_candidate_id": "scoreboard-row", "market_id": "KXSCORE-1", "outcome": "YES"},
        ]

        report = summarize_paper_shadow_lane_resolved_pnl(lane_rows=lane_rows, resolution_rows=resolution_rows)

        router = report["source_router"]
        self.assertEqual(router["evaluated_rows"], 3)
        self.assertEqual(router["buy_rows"], 2)
        self.assertEqual(router["skip_rows"], 1)
        self.assertEqual(router["raw_router_resolved_buy_rows"], 2)
        self.assertEqual(router["raw_router_correct_side_rows"], 1)
        self.assertEqual(router["raw_router_win_rate_pct"], 50.0)
        self.assertEqual(router["standardized_hypothetical_stake_usd"], 20.0)
        self.assertEqual(router["standardized_hypothetical_pnl_usd"], 20.0)
        self.assertEqual(router["standardized_hypothetical_roi_pct"], 100.0)
        self.assertEqual(router["action_counts"], {"BUY_NO": 1, "BUY_YES": 1, "SKIP": 1})
        self.assertEqual(report["by_lane"]["shadow_source_router"]["total_pnl_usd"], 20.0)

    def test_source_scoreboard_resolved_pnl_reports_blockers_without_guessing(self):
        lane_rows = [
            {
                "policy": "shadow_source_scoreboard",
                "shared_candidate_id": "candidate-missing-fill",
                "action": "BUY_YES",
                "provenance": {
                    "source_scoreboard": {
                        "future_pnl_inputs": {
                            "shared_candidate_id": "candidate-missing-fill",
                            "market_id": "KXHIGHSEA-1",
                            "side": "YES",
                            "stable_approved_position_size_usd": 10.0,
                        }
                    }
                },
            },
            {
                "policy": "shadow_source_scoreboard",
                "shared_candidate_id": "candidate-missing-resolution",
                "action": "BUY_YES",
                "provenance": {
                    "source_scoreboard": {
                        "future_pnl_inputs": {
                            "shared_candidate_id": "candidate-missing-resolution",
                            "market_id": "KXHIGHSEA-2",
                            "side": "YES",
                            "estimated_fill_price": 0.25,
                            "stable_approved_position_size_usd": 10.0,
                        }
                    }
                },
            },
        ]
        resolution_rows = [{"shared_candidate_id": "candidate-missing-fill", "outcome": "YES"}]

        report = summarize_paper_shadow_lane_resolved_pnl(lane_rows=lane_rows, resolution_rows=resolution_rows)

        self.assertEqual(report["evaluated_rows"], 2)
        self.assertEqual(report["resolved_rows"], 1)
        self.assertEqual(report["pnl_calculable_rows"], 0)
        self.assertEqual(
            report["blocker_counts"],
            {"missing_fill_price": 1, "missing_resolution": 1},
        )

    def test_lane_resolution_prefers_shared_candidate_over_market_fallback(self):
        lane_rows = [
            {
                "policy": "shadow_source_scoreboard",
                "shared_candidate_id": "candidate-specific",
                "market_id": "KXDUP-1",
                "action": "BUY_YES",
                "approved_position_size_usd": 10.0,
                "provenance": {
                    "future_pnl_inputs": {
                        "shared_candidate_id": "candidate-specific",
                        "market_id": "KXDUP-1",
                        "recommended_action": "BUY_YES",
                        "side": "YES",
                        "estimated_fill_price": 0.25,
                    }
                },
            }
        ]
        resolution_rows = [
            {"shared_candidate_id": "other-candidate", "market_id": "KXDUP-1", "outcome": "NO"},
            {"shared_candidate_id": "candidate-specific", "market_id": "KXDUP-1", "resolution": {"outcome": "YES"}, "prediction_id": "pred-specific"},
        ]

        rows = build_paper_shadow_lane_resolution_rows(lane_rows=lane_rows, resolution_rows=resolution_rows)

        self.assertEqual(rows[0]["resolution"]["outcome"], "YES")
        self.assertEqual(rows[0]["resolution"]["matched_by"], "shared_candidate_id")
        self.assertEqual(rows[0]["resolution"]["resolution_row_id"], "pred-specific")
        self.assertEqual(rows[0]["blocker"], None)

    def test_lane_resolution_reports_ambiguous_market_fallback(self):
        lane_rows = [
            {
                "policy": "shadow_source_scoreboard",
                "market_id": "KXAMBIG-1",
                "action": "BUY_YES",
                "approved_position_size_usd": 10.0,
                "provenance": {"future_pnl_inputs": {"market_id": "KXAMBIG-1", "side": "YES", "estimated_fill_price": 0.25}},
            }
        ]
        resolution_rows = [
            {"shared_candidate_id": "c1", "market_id": "KXAMBIG-1", "outcome": "YES"},
            {"shared_candidate_id": "c2", "market_id": "KXAMBIG-1", "outcome": "NO"},
        ]

        rows = build_paper_shadow_lane_resolution_rows(lane_rows=lane_rows, resolution_rows=resolution_rows)
        report = summarize_paper_shadow_lane_resolved_pnl(lane_rows=lane_rows, resolution_rows=resolution_rows)

        self.assertEqual(rows[0]["blocker"], "ambiguous_resolution")
        self.assertEqual(rows[0]["resolution"]["matched_by"], "market_id")
        self.assertEqual(report["blocker_counts"], {"ambiguous_resolution": 1})

    def test_paper_shadow_lane_report_materializes_resolution_jsonl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lane_path = Path(tmpdir) / "lane_decisions.jsonl"
            resolution_path = Path(tmpdir) / "resolutions.jsonl"
            output_path = REPO_ROOT / "data" / "summaries" / "test_lane_resolutions.jsonl"
            append_jsonl(
                lane_path,
                {
                    "policy": "shadow_source_scoreboard",
                    "shared_candidate_id": "candidate-jsonl",
                    "market_id": "KXJSONL-1",
                    "action": "BUY_NO",
                    "approved_position_size_usd": 8.0,
                    "provenance": {
                        "future_pnl_inputs": {
                            "shared_candidate_id": "candidate-jsonl",
                            "market_id": "KXJSONL-1",
                            "recommended_action": "BUY_NO",
                            "side": "NO",
                            "estimated_fill_price": 0.20,
                        }
                    },
                },
            )
            append_jsonl(resolution_path, {"shared_candidate_id": "candidate-jsonl", "market_id": "KXJSONL-1", "resolution": {"outcome": "NO"}})
            cwd_lane = os.path.relpath(lane_path, REPO_ROOT)
            cwd_resolution = os.path.relpath(resolution_path, REPO_ROOT)
            cwd_output = os.path.relpath(output_path, REPO_ROOT)
            stdout = StringIO()
            with redirect_stdout(stdout):
                rc = paper_shadow_lane_report_main(
                    [
                        "--lane-decision-path",
                        cwd_lane,
                        "--resolution-path",
                        cwd_resolution,
                        "--section",
                        "resolved_pnl",
                        "--resolved-output-jsonl",
                        cwd_output,
                    ]
                )

            self.assertEqual(rc, 0)
            materialized = load_jsonl(output_path)
            self.assertEqual(len(materialized), 1)
            self.assertTrue(materialized[0]["non_mutating"])
            self.assertEqual(materialized[0]["resolution"]["outcome"], "NO")
            self.assertEqual(materialized[0]["pnl"]["pnl_usd"], 32.0)
            output_path.unlink(missing_ok=True)

    def test_paper_shadow_lane_report_rejects_wallet_like_resolution_output_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lane_path = Path(tmpdir) / "lane_decisions.jsonl"
            append_jsonl(lane_path, {"policy": "shadow_source_scoreboard", "market_id": "KXSAFE-1", "action": "SKIP"})
            cwd_lane = os.path.relpath(lane_path, REPO_ROOT)
            with self.assertRaises(ValueError):
                paper_shadow_lane_report_main(
                    [
                        "--lane-decision-path",
                        cwd_lane,
                        "--section",
                        "resolved_pnl",
                        "--resolved-output-jsonl",
                        "data/paper/risk_state.json",
                    ]
                )

    def test_lane_resolution_uses_run_market_before_ambiguous_market_fallback(self):
        lane_rows = [
            {
                "policy": "shadow_source_scoreboard",
                "run_id": "run-target",
                "market_id": "KXRUN-1",
                "action": "BUY_YES",
                "approved_position_size_usd": 10.0,
                "provenance": {"future_pnl_inputs": {"market_id": "KXRUN-1", "side": "YES", "estimated_fill_price": 0.25}},
            }
        ]
        resolution_rows = [
            {"run_id": "run-other", "market_id": "KXRUN-1", "outcome": "NO"},
            {"run_id": "run-target", "market_id": "KXRUN-1", "outcome": "YES"},
        ]

        rows = build_paper_shadow_lane_resolution_rows(lane_rows=lane_rows, resolution_rows=resolution_rows)

        self.assertEqual(rows[0]["resolution"]["outcome"], "YES")
        self.assertEqual(rows[0]["resolution"]["matched_by"], "run_id_market_id")
        self.assertEqual(rows[0]["resolution"]["candidate_match_count"], 1)


    def test_lane_resolution_requires_independent_resolution_not_future_outcome(self):
        lane_rows = [
            {
                "policy": "shadow_source_scoreboard",
                "market_id": "KXLEAK-1",
                "action": "BUY_YES",
                "approved_position_size_usd": 10.0,
                "provenance": {
                    "future_pnl_inputs": {
                        "market_id": "KXLEAK-1",
                        "side": "YES",
                        "estimated_fill_price": 0.25,
                        "actual_outcome": "YES",
                    }
                },
            }
        ]

        rows = build_paper_shadow_lane_resolution_rows(lane_rows=lane_rows, resolution_rows=[])

        self.assertEqual(rows[0]["blocker"], "missing_resolution")
        self.assertFalse(rows[0]["resolution"]["matched"])
        self.assertIsNone(rows[0]["pnl"])
        self.assertNotIn(
            "actual_outcome",
            rows[0]["source_inputs"]["future_pnl_inputs"],
        )

if __name__ == "__main__":
    unittest.main()
