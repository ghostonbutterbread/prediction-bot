"""Direct Source Router history across verified monthly collector archives."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from bot.collector_replay_index import build_collector_replay_index
from bot.paper_shadow_lanes import _LaneDefinition, _direct_source_history_parameters, _source_router_decision
from bot.weather.source_router_direct_history import load_direct_strict_source_history
from scripts.collector_lane_replay import build_collector_lane_replay
from test_auto_source_router_promotion import _collector_row, _strict_resolution, _write_jsonl


def _day_row(day: date) -> dict:
    label = day.strftime("%y%b%d").upper()
    row = _collector_row(market_id=f"KXHIGHSEA-{label}-T70")
    observed = datetime.combine(day, time(12), tzinfo=timezone.utc)
    source_as_of = observed - timedelta(minutes=6)
    row["observed_at"] = observed.isoformat()
    row["shared_snapshot_id"] = f"snapshot-{day.isoformat()}"
    data = row["decision_artifact"]["source_context"]["data"]
    data["weather_source_snapshot"]["market_date"] = day.isoformat()
    data["weather_source_snapshot"]["sources"][0]["source_as_of"] = source_as_of.isoformat()
    data["weather_source_snapshot"]["sources"][0]["target_mapping"].update(
        market_target_date=day.isoformat(), source_target_date=day.isoformat(),
    )
    row["decision_artifact"]["source_context"]["as_of"] = (observed - timedelta(minutes=5)).isoformat()
    return row


def _receipt(row: dict, *, days_later: int = 1) -> dict:
    receipt = _strict_resolution(row["market_id"])
    settled = datetime.fromisoformat(row["observed_at"]) + timedelta(days=days_later)
    receipt["settlement_ts"] = settled.isoformat()
    receipt["resolution_id"] = f"resolution-{row['market_id']}"
    return receipt


def _archive(root: Path, name: str, rows: list[dict], resolutions: list[dict]) -> dict:
    folder = root / name
    folder.mkdir()
    source, index, manifest = (folder / filename for filename in
                               ("market_snapshots.jsonl", "index.jsonl", "index.manifest.json"))
    resolved = folder / "strict_resolutions.jsonl"
    _write_jsonl(source, rows)
    _write_jsonl(resolved, resolutions)
    build_collector_replay_index(source, index, manifest)
    return {"index_path": str(index), "manifest_path": str(manifest),
            "strict_resolutions_path": str(resolved)}


class CrossMonthSourceHistoryTests(unittest.TestCase):
    def test_indexed_offline_replay_calls_paper_router_with_older_month_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aug = _day_row(date(2026, 8, 29))
            sep = _day_row(date(2026, 9, 2))
            candidate = _day_row(date(2026, 9, 5))
            candidate.update(run_id="collector-sep", shared_candidate_id="candidate-sep")
            newer = _archive(root, "2026-09", [sep, candidate], [_receipt(sep)])
            older = _archive(root, "2026-08", [aug], [_receipt(aug)])
            definitions = root / "lanes"
            definitions.mkdir()
            (definitions / "shadow_source_router.yaml").write_text(json.dumps({
                "id": "shadow_source_router", "type": "source_router", "source_wallet": "stable_paper",
                "input_source": "shared_candidate_dataset", "input_market_source": "shared_market",
                "enabled": True, "parameters": {
                    "collector_replay_archives": [newer, older], "history_events_per_bucket": 2,
                },
            }), encoding="utf-8")
            result = build_collector_lane_replay(
                index_path=Path(newer["index_path"]), manifest_path=Path(newer["manifest_path"]),
                output_dir=root / "data" / "derived_reports" / "offline_router",
                enabled_lanes=["shadow_source_router"], market_ids=[candidate["market_id"]],
                max_rows=1, definitions_dir=definitions,
            )
            [row] = [json.loads(line) for line in result.lane_decision_path.read_text().splitlines()]
            self.assertEqual(row["market_id"], candidate["market_id"])
            self.assertEqual(row["policy"], "shadow_source_router")
            self.assertTrue(row["provenance"]["source_router"]["available"])
            self.assertTrue(row["provenance"]["source_router"]["source_observations"])
            direct_receipt = row["provenance"]["source_router"]["direct_history"]
            self.assertEqual(len(direct_receipt["archive_receipts"]), 2)
            self.assertEqual(direct_receipt["bucket_coverage"][0]["selected_units"], 2)
            self.assertEqual(direct_receipt["history_events_per_bucket"], 2)
            self.assertTrue(all(receipt["index_manifest_sha256"] and receipt["resolution_sha256"]
                                for receipt in direct_receipt["archive_receipts"]))
            self.assertFalse(row["mutation_contract"]["places_orders"])

    def test_receipt_in_earlier_archive_cannot_contradict_later_selected_market(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aug = _day_row(date(2026, 8, 29))
            sep = _day_row(date(2026, 9, 2))
            first = _archive(root, "first", [aug], [_receipt(aug), {**_receipt(sep), "kalshi_result": "no"}])
            second = _archive(root, "second", [sep], [_receipt(sep)])
            with self.assertRaisesRegex(ValueError, "conflicting strict resolutions across archives"):
                load_direct_strict_source_history(
                    archive_sources=[first, second], accepted_limit=2,
                    as_of_decision_time="2026-09-05T00:00:00Z",
                )

    def test_ambiguous_receipt_in_other_archive_cannot_train_selected_market(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sep = _day_row(date(2026, 9, 2))
            newer = _archive(root, "first", [sep], [_receipt(sep)])
            duplicate = _receipt(sep)
            older = _archive(root, "second", [], [duplicate, copy.deepcopy(duplicate)])
            with self.assertRaisesRegex(ValueError, "ambiguous strict resolutions across archives"):
                load_direct_strict_source_history(
                    archive_sources=[newer, older], accepted_limit=2,
                    as_of_decision_time="2026-09-05T00:00:00Z",
                )

    def test_conflicting_overlap_resolution_cannot_train_from_either_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sep = _day_row(date(2026, 9, 2))
            yes = _receipt(sep)
            no = {**yes, "kalshi_result": "no"}
            newer = _archive(root, "2026-09", [sep], [yes])
            older = _archive(root, "2026-08", [copy.deepcopy(sep)], [no])
            with self.assertRaisesRegex(ValueError, "conflicting strict resolutions across archives"):
                load_direct_strict_source_history(
                    archive_sources=[newer, older], accepted_limit=2,
                    as_of_decision_time="2026-09-05T00:00:00Z",
                )

    def test_bucket_keeps_newest_settled_event_across_archive_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aug = _day_row(date(2026, 8, 29))
            sep = _day_row(date(2026, 9, 2))
            older = _archive(root, "2026-08", [aug], [_receipt(aug)])
            newer = _archive(root, "2026-09", [sep], [_receipt(sep)])
            result = load_direct_strict_source_history(
                archive_sources=[older, newer], accepted_limit=1,
                as_of_decision_time="2026-09-05T00:00:00Z",
            )
            [coverage] = result.bucket_coverage
            self.assertEqual(coverage["selected_units"], 1)
            self.assertEqual(coverage["earliest_source_as_of"][:10], "2026-09-02")

    def test_older_archive_fills_sparse_newest_bucket_without_duplicate_month_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aug = _day_row(date(2026, 8, 29))
            sep = _day_row(date(2026, 9, 2))
            unresolved = _day_row(date(2026, 9, 3))
            newer = _archive(root, "2026-09", [sep, unresolved], [_receipt(sep)])
            older = _archive(root, "2026-08", [aug, copy.deepcopy(sep)],
                             [_receipt(aug), _receipt(sep)])
            result = load_direct_strict_source_history(
                archive_sources=[newer, older], accepted_limit=2,
                as_of_decision_time="2026-09-05T00:00:00Z",
            )
            self.assertEqual(result.scorecard_rows[0]["sample_count"], 2)
            [coverage] = result.bucket_coverage
            self.assertEqual(coverage["selected_units"], 2)
            self.assertEqual(coverage["earliest_source_as_of"][:10], "2026-08-29")
            self.assertEqual(coverage["latest_source_as_of"][:10], "2026-09-02")
            self.assertEqual(result.counts["indexed_rows_inspected"], 4)

    def test_paper_router_uses_same_cross_month_history_at_decision_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            aug = _day_row(date(2026, 8, 29))
            sep = _day_row(date(2026, 9, 2))
            newer = _archive(root, "2026-09", [sep], [_receipt(sep)])
            older = _archive(root, "2026-08", [aug], [_receipt(aug)])
            lane = _LaneDefinition("shadow_source_router", lane_type="source_router", parameters={
                "collector_replay_archives": [newer, older], "history_events_per_bucket": 2,
            })
            self.assertEqual(_direct_source_history_parameters(lane)["archive_sources"], [newer, older])
            signal = {
                "market_id": "KXHIGHSEA-26SEP05-T70", "observed_at": "2026-09-05T12:00:00+00:00",
                "question": "Will Seattle high temperature be above 70°?", "city_id": "seattle_wa",
                "market_kind": "high", "contract_shape": "tail", "question_side": "above", "threshold": 70.0,
                "source_details": [{"source_id": "nws", "source_name": "NWS", "forecast_high": 75.0}],
            }
            candidate = {"observed_at": signal["observed_at"],
                         "market": {"id": signal["market_id"], "question": signal["question"]}}
            decision = _source_router_decision(lane, signal, None, candidate)
            self.assertTrue(decision["source_router"]["available"], decision)
            self.assertEqual(decision["source_router"]["direct_history"]["bucket_coverage"][0]["selected_units"], 2)

            # A candidate before the September settlement cannot see that event.
            earlier_signal = {**signal, "observed_at": "2026-09-03T00:00:00+00:00"}
            earlier = _source_router_decision(
                lane, earlier_signal, None, {**candidate, "observed_at": earlier_signal["observed_at"]},
            )
            self.assertEqual(earlier["source_router"]["direct_history"]["bucket_coverage"][0]["selected_units"], 1)


if __name__ == "__main__":
    unittest.main()
