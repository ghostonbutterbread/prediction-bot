import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.auto_source_router_promotion import auto_populate_source_router_history
from bot.collector_paths import COLLECTOR_ROOT_ENV
from bot.collector_replay_index import build_collector_replay_index
from bot.paper_shadow_lanes import _LaneDefinition, _source_router_decision
from bot.replay_outcome_binding import _index_strict_resolutions as _real_index_strict_resolutions
from bot.weather.source_router_direct_history import (
    _index_strict_resolutions, _load_stable_resolution_index, load_direct_strict_source_history,
)
from test_auto_source_router_promotion import _collector_row, _strict_resolution, _write_jsonl


class DirectSourceRouterHistoryTests(unittest.TestCase):
    def test_resolution_receipt_rejects_same_length_in_place_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "resolutions.jsonl"
            original = {**_strict_resolution("KXHIGHSEA-26AUG01-T70"), "resolution_id": "aaaa", "kalshi_result": "YES"}
            replacement = {**original, "resolution_id": "bbbb"}
            original_bytes = (json.dumps(original, sort_keys=True) + "\n").encode("utf-8")
            replacement_bytes = (json.dumps(replacement, sort_keys=True) + "\n").encode("utf-8")
            self.assertEqual(len(original_bytes), len(replacement_bytes))
            path.write_bytes(original_bytes)

            def consume_then_rewrite(rows):
                result = _real_index_strict_resolutions(rows)
                path.write_bytes(replacement_bytes)
                return result

            with patch("bot.weather.source_router_direct_history._index_strict_resolutions", side_effect=consume_then_rewrite):
                with self.assertRaisesRegex(ValueError, "changed while being read"):
                    _load_stable_resolution_index(path)

    def test_resolution_index_marks_many_duplicate_rows_ambiguous_without_retaining_them(self):
        row = _strict_resolution("KXHIGHSEA-26AUG01-T70")
        indexed, stats = _index_strict_resolutions(
            ((number, row, json.dumps(row).encode("utf-8")) for number in range(1, 10_001))
        )
        self.assertNotIn(row["market_id"], indexed.accepted)
        self.assertIn(row["market_id"], indexed.ambiguous_market_ids)
        self.assertEqual(stats["ambiguous_resolution_records"], 10_000)

    def test_missing_index_is_a_fail_closed_domain_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resolutions = root / "resolutions.jsonl"
            _write_jsonl(resolutions, [])
            with self.assertRaisesRegex(ValueError, "replay index"):
                load_direct_strict_source_history(
                    index_path=root / "missing-index.jsonl", manifest_path=root / "missing-manifest.json",
                    strict_resolutions_path=resolutions, as_of_decision_time="2026-08-06T00:00:00+00:00",
                )

    def test_lane_skips_when_direct_evidence_disappears(self):
        signal = {"market_id": "KXHIGHSEA-26AUG06-T70", "observed_at": "2026-08-06T12:00:00+00:00", "question": "Will Seattle high temperature be above 70°?"}
        result = _source_router_decision(
            _LaneDefinition("shadow_source_router", lane_type="source_router", parameters={
                "collector_replay_index_path": "/missing/index.jsonl",
                "collector_replay_manifest_path": "/missing/manifest.json",
                "strict_resolutions_path": "/missing/resolutions.jsonl",
            }), signal, None, {"observed_at": signal["observed_at"], "market": {"id": signal["market_id"], "question": signal["question"]}},
        )
        self.assertEqual(result["action"], "SKIP")
        self.assertEqual(result["reason_code"], "strict_scorecard_verification_failed")

    def test_window_excludes_newest_sanitized_input_without_finalized_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive, resolutions = root / "snapshots.jsonl", root / "resolutions.jsonl"
            rows = []
            for number in range(1, 5):
                row = _collector_row(market_id=f"KXHIGHSEA-26AUG{number:02}-T70")
                row["shared_snapshot_id"] = f"snapshot-{number}"
                row["observed_at"] = f"2026-08-0{number}T12:00:00+00:00"
                snapshot = row["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]
                date = f"2026-08-{number:02}"
                snapshot["market_date"] = date
                snapshot["sources"][0]["source_as_of"] = f"{date}T11:54:00+00:00"
                snapshot["sources"][0]["target_mapping"].update(
                    market_target_date=date, source_target_date=date,
                )
                rows.append(row)
            _write_jsonl(archive, rows)
            # The newest raw record can be replay-sanitized, but without an
            # authoritative finalized outcome it is not one of the 3 good rows.
            _write_jsonl(resolutions, [_strict_resolution(row["market_id"]) for row in rows[:-1]])
            index, manifest = root / "index.jsonl", root / "index.manifest.json"
            build_collector_replay_index(archive, index, manifest)

            direct = load_direct_strict_source_history(
                index_path=index, manifest_path=manifest, strict_resolutions_path=resolutions, accepted_limit=3,
                as_of_decision_time="2026-08-06T00:00:00+00:00",
            )

            self.assertEqual(direct.counts["accepted_replay_inputs"], 3)
            self.assertEqual(direct.counts["indexed_rows_inspected"], 4)
            self.assertEqual(direct.counts["settled_strict_observations"], 3)
            self.assertEqual(direct.scorecard_rows[0]["sample_count"], 3)

    def test_indexed_newest_accepted_window_matches_writer_scorecard_without_direct_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive, resolutions = root / "snapshots.jsonl", root / "resolutions.jsonl"
            rows = []
            for number in range(1, 4):
                row = _collector_row(market_id=f"KXHIGHSEA-26AUG{number:02}-T70")
                row["shared_snapshot_id"] = f"snapshot-{number}"
                row["observed_at"] = f"2026-08-0{number}T12:00:00+00:00"
                snapshot = row["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]
                date = f"2026-08-{number:02}"
                snapshot["market_date"] = date
                snapshot["sources"][0]["source_as_of"] = f"{date}T11:54:00+00:00"
                snapshot["sources"][0]["target_mapping"].update(
                    market_target_date=date, source_target_date=date,
                )
                rows.append(row)
            _write_jsonl(archive, rows)
            # It has enough compact identity fields to be indexed, but not the
            # decision evidence needed for a sanitized replay input. Direct
            # newest-window selection must scan past it rather than return two
            # good rows and stop.
            _write_jsonl(archive, [*rows, {
                "market_id": "KXHIGHSEA-26AUG04-T70", "shared_snapshot_id": "snapshot-invalid",
                "observed_at": "2026-08-04T12:00:00+00:00",
            }])
            _write_jsonl(resolutions, [_strict_resolution(row["market_id"]) for row in rows])
            index, manifest = root / "index.jsonl", root / "index.manifest.json"
            build_collector_replay_index(archive, index, manifest)

            direct = load_direct_strict_source_history(
                index_path=index, manifest_path=manifest, strict_resolutions_path=resolutions, accepted_limit=3,
                as_of_decision_time="2026-08-06T00:00:00+00:00",
            )
            with patch.dict(os.environ, {COLLECTOR_ROOT_ENV: str(root)}):
                expected = auto_populate_source_router_history(
                    collector_snapshots_path=archive, strict_resolutions_path=resolutions,
                    output_root=root / "data" / "derived_reports" / "legacy",
                    storage_root=root,
                )
            writer_rows = [json.loads(line) for line in expected.scoreboard_path.read_text().splitlines()]

            self.assertEqual(direct.counts["accepted_replay_inputs"], 3)
            self.assertEqual(direct.counts["indexed_rows_inspected"], 4)
            def semantic(rows):
                result = []
                for row in rows:
                    copy = json.loads(json.dumps(row))
                    copy["provenance"].pop("source_history_sha256", None)
                    result.append(copy)
                return result
            self.assertEqual(semantic(direct.scorecard_rows), semantic(writer_rows))
            self.assertFalse((root / "direct").exists())

            signal = {
                "market_id": "KXHIGHSEA-26AUG06-T70", "observed_at": "2026-08-06T12:00:00+00:00",
                "question": "Will Seattle high temperature be above 70°?", "city_id": "seattle_wa",
                "market_kind": "high", "contract_shape": "tail", "question_side": "above", "threshold": 70.0,
                "source_details": [{"source_id": "nws", "source_name": "NWS", "forecast_high": 75.0}],
            }
            candidate = {"observed_at": signal["observed_at"], "market": {"id": signal["market_id"], "question": signal["question"]}}
            legacy_decision = _source_router_decision(
                _LaneDefinition("shadow_source_router", lane_type="source_router", parameters={"scoreboard_path": str(expected.scoreboard_path)}),
                signal, None, candidate,
            )
            direct_decision = _source_router_decision(
                _LaneDefinition("shadow_source_router", lane_type="source_router", parameters={
                    "collector_replay_index_path": str(index), "collector_replay_manifest_path": str(manifest),
                    "strict_resolutions_path": str(resolutions), "history_row_limit": 3,
                }), signal, None, candidate,
            )
            self.assertEqual(direct_decision["action"], legacy_decision["action"])
            self.assertEqual(direct_decision["source_router"]["source_direction"], legacy_decision["source_router"]["source_direction"])
            self.assertEqual(direct_decision["source_router"]["direct_history"]["mode"], "committed_index_direct_read")


if __name__ == "__main__":
    unittest.main()
