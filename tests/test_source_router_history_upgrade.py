"""Runtime admission of pre-chronology history, using the genuine V1 producer.

The frozen materializer is checked in from the reviewed ancestor, not
reconstructed by changing flags or hashes on V2 output. No Git metadata needed.
"""
import hashlib
import json
import os
import socket

import sys
import tempfile
import types
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from bot import auto_source_router_promotion as promotion
from bot.collector_paths import COLLECTOR_ROOT_ENV
from bot.paper_shadow_lanes import _LaneDefinition, _source_router_decision
from bot.weather.source_observation_ledger import is_eligible_for_future_history
from test_auto_source_router_promotion import _collector_row, _strict_resolution, _write_jsonl

ROOT = Path(__file__).resolve().parents[1]
V1_REVISION = "41f75408eef2b5f2c8a57d67e9841c6835b94261"
V1_SOURCE_SHA256 = "be6a7972d5d092f2c00bcbad6c498ec53eed049c73620fdedd6f51ab82fc4859"
DECISION_TIME = "2026-08-06T12:00:00+00:00"


class SourceRouterHistoryUpgradeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {COLLECTOR_ROOT_ENV: str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        network = patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden"))
        network.start()
        self.addCleanup(network.stop)

    def _publish(self, *, legacy=False):
        rows, outcomes = [], []
        for index in range(100):
            day = date(2026, 1, 1) + timedelta(days=index)
            market_id = "KXHIGHSEA-" + day.strftime("%y%b%d").upper() + "-T70"
            row = _collector_row(market_id=market_id)
            observed = "2026-08-05" if legacy else day.isoformat()
            row["observed_at"] = observed + "T12:00:00+00:00"
            data = row["decision_artifact"]["source_context"]["data"]
            snapshot = data["weather_source_snapshot"]
            snapshot["market_date"] = day.isoformat()
            source = snapshot["sources"][0]
            source["source_as_of"] = observed + "T11:54:00+00:00"
            source["target_mapping"].update(market_target_date=day.isoformat(), source_target_date=day.isoformat())
            outcome = _strict_resolution(market_id)
            outcome["settlement_ts"] = (day + timedelta(days=1)).isoformat() + "T00:00:00+00:00"
            rows.append(row)
            outcomes.append(outcome)
        archive, resolutions = self.root / "snapshots.jsonl", self.root / "resolutions.jsonl"
        _write_jsonl(archive, rows)
        _write_jsonl(resolutions, outcomes)
        if legacy:
            source_bytes = (ROOT / "tests/fixtures/source_observation_ledger_v1.py.txt").read_bytes()
            self.assertEqual(hashlib.sha256(source_bytes).hexdigest(), V1_SOURCE_SHA256)
            source = source_bytes.decode("utf-8")
            module = types.ModuleType("_frozen_v1_source_observation_ledger")
            with patch.dict(sys.modules, {module.__name__: module}):
                exec(compile(source, f"{V1_REVISION}:source_observation_ledger.py", "exec"), module.__dict__)
            materializer = module.materialize_source_observation_ledger
        else:
            materializer = promotion.materialize_source_observation_ledger
        # The unchanged publisher/collapse contract hashes genuine old ledger
        # bytes; V1 differs in chronology/target validation and generation version.
        with patch.object(promotion, "materialize_source_observation_ledger", materializer), patch.object(
            promotion, "PIPELINE_SCHEMA_VERSION", 1 if legacy else 2,
        ):
            result = promotion.auto_populate_source_router_history(
                collector_snapshots_path=archive, strict_resolutions_path=resolutions,
                output_root=self.root / "data" / "derived_reports" / "history",
            )
        self.assertEqual(result.counts["eligible"], 100)
        return result

    def _decision(self, path):
        signal = {
            "market_id": "KXHIGHSEA-26AUG06-T70", "observed_at": DECISION_TIME,
            "question": "Will Seattle high temperature be above 70°?", "city_id": "seattle_wa",
            "market_kind": "high", "contract_shape": "tail", "question_side": "above", "threshold": 70.0,
            "source_details": [{"source_id": "nws", "source_name": "NWS", "forecast_high": 75.0}],
        }
        return _source_router_decision(
            _LaneDefinition("shadow_source_router", lane_type="source_router", parameters={"scoreboard_path": str(path)}),
            signal, None, {"observed_at": DECISION_TIME, "market": {"id": signal["market_id"], "question": signal["question"]}},
        )

    def _paths(self, result):
        return (result.scoreboard_path, result.generation_dir.parent.parent / "current" / promotion.STRICT_SCORECARD_RELATIVE_PATH)

    def test_genuine_v1_postsettlement_history_fails_closed_despite_valid_hashes(self):
        result = self._publish(legacy=True)
        manifest = promotion._validate_generation(result.manifest_path)
        self.assertEqual(manifest["schema_version"], 1)
        history = [json.loads(line) for line in result.history_path.read_text().splitlines()]
        self.assertEqual(len(history), 100)
        self.assertFalse(any(is_eligible_for_future_history(row, DECISION_TIME) for row in history))
        scorecards = promotion.load_verified_strict_scorecard_rows(result.scoreboard_path)
        assert scorecards is not None
        [scorecard] = scorecards
        self.assertEqual(scorecard["sample_count"], 100)
        self.assertEqual(scorecard["threshold_direction_accuracy"], 1.0)
        for path in self._paths(result):
            with self.subTest(path=path):
                decision = self._decision(path)
                self.assertEqual(decision["action"], "SKIP")
                self.assertEqual(decision["reason_code"], "strict_scorecard_verification_failed")
                self.assertIn("schema version 1", decision["reason"])
                self.assertIn("re-materialize", decision["reason"])
                self.assertFalse(decision["source_router"]["available"])
                self.assertEqual(decision["approved_position_size_usd"], 0)

    def test_current_v2_history_retains_buy_for_both_handoffs(self):
        result = self._publish()
        self.assertEqual(promotion._validate_generation(result.manifest_path)["schema_version"], 2)
        for path in self._paths(result):
            with self.subTest(path=path):
                decision = self._decision(path)
                self.assertEqual(decision["action"], "BUY_YES")
                self.assertTrue(decision["source_router"]["available"])

    def test_unknown_or_missing_pipeline_version_fails_closed(self):
        result = self._publish()
        manifest = json.loads(result.manifest_path.read_text())
        for version in (None, 999):
            with self.subTest(version=version):
                if version is None:
                    manifest.pop("schema_version", None)
                else:
                    manifest["schema_version"] = version
                result.manifest_path.write_text(json.dumps(manifest))
                decision = self._decision(result.scoreboard_path)
                self.assertEqual(decision["action"], "SKIP")
                self.assertIn("schema version", decision["reason"])


if __name__ == "__main__":
    unittest.main()
