import json
import tempfile
import unittest
from pathlib import Path

from scripts.unified_replay_corpus import build_unified_replay_corpus


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class UnifiedReplayCorpusTests(unittest.TestCase):
    def test_builds_resolved_decision_corpus_without_mutating_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decisions = root / "decisions.jsonl"
            resolutions = root / "resolutions.jsonl"
            output_dir = root / "data" / "derived_reports" / "corpus"
            _write_jsonl(
                decisions,
                [
                    {
                        "policy": "shadow_test",
                        "shared_candidate_id": "candidate-1",
                        "market_id": "KXTEST-1",
                        "observed_at": "2026-06-01T00:00:00+00:00",
                        "action": "BUY_YES",
                        "approved_position_size_usd": 10.0,
                        "provenance": {
                            "future_pnl_inputs": {
                                "market_id": "KXTEST-1",
                                "shared_candidate_id": "candidate-1",
                                "side": "YES",
                                "estimated_fill_price": 0.40,
                                "approved_position_size_usd": 10.0,
                            }
                        },
                    }
                ],
            )
            original_decisions = decisions.read_text(encoding="utf-8")
            _write_jsonl(resolutions, [{"market_id": "KXTEST-1", "outcome": "YES"}])

            result = build_unified_replay_corpus(
                output_dir=output_dir,
                decision_paths=[decisions],
                resolution_paths=[resolutions],
                resolved_replay_paths=[],
            )

            rows = [json.loads(line) for line in result["corpus_path"].read_text(encoding="utf-8").splitlines()]
            self.assertEqual(decisions.read_text(encoding="utf-8"), original_decisions)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["lane_id"], "shadow_test")
            self.assertEqual(rows[0]["resolution_outcome"], "YES")
            self.assertTrue(rows[0]["won"])
            self.assertAlmostEqual(rows[0]["pnl_usd"], 15.0)
            self.assertEqual(result["summary"]["unique_markets"], 1)
            self.assertEqual(result["summary"]["pnl_calculable_rows"], 1)

    def test_ingests_flattened_resolved_replay_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            replay = root / "joined_source_router_rows.jsonl"
            output_dir = root / "data" / "derived_reports" / "corpus"
            _write_jsonl(
                replay,
                [
                    {
                        "policy": "shadow_source_router",
                        "market_id": "KXTEST-2",
                        "shared_candidate_id": "candidate-2",
                        "observed_at": "2026-06-02T00:00:00+00:00",
                        "action": "BUY_NO",
                        "side": "NO",
                        "price": 0.80,
                        "stake_usd": 10.0,
                        "pnl_usd": 2.5,
                        "won": True,
                        "outcome": "NO",
                    }
                ],
            )

            result = build_unified_replay_corpus(
                output_dir=output_dir,
                decision_paths=[],
                resolution_paths=[],
                resolved_replay_paths=[replay],
            )

            rows = [json.loads(line) for line in result["corpus_path"].read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["source_kind"], "resolved_replay")
            self.assertEqual(rows[0]["lane_id"], "shadow_source_router")
            self.assertEqual(rows[0]["stake_usd"], 10.0)
            self.assertEqual(result["summary"]["total_pnl_usd"], 2.5)

    def test_exact_dedupe_keeps_duplicate_rows_out_of_corpus(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            replay = root / "dupes.jsonl"
            output_dir = root / "data" / "derived_reports" / "corpus"
            row = {
                "lane_id": "lane",
                "market_id": "KXTEST-3",
                "shared_candidate_id": "candidate-3",
                "observed_at": "2026-06-03T00:00:00+00:00",
                "action": "BUY_YES",
                "side": "YES",
                "stake_usd": 10.0,
                "pnl_usd": -10.0,
                "won": False,
                "outcome": "NO",
            }
            _write_jsonl(replay, [row, dict(row)])

            result = build_unified_replay_corpus(
                output_dir=output_dir,
                decision_paths=[],
                resolution_paths=[],
                resolved_replay_paths=[replay],
            )

            rows = result["corpus_path"].read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(rows), 1)
            self.assertEqual(result["summary"]["skipped_counts"], {"duplicate_exact": 1})

    def test_duplicate_resolution_mirrors_do_not_create_ambiguous_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decisions = root / "decisions.jsonl"
            resolutions_a = root / "central.jsonl"
            resolutions_b = root / "mirror.jsonl"
            output_dir = root / "data" / "derived_reports" / "corpus"
            _write_jsonl(
                decisions,
                [
                    {
                        "policy": "shadow_test",
                        "market_id": "KXTEST-4",
                        "action": "BUY_NO",
                        "approved_position_size_usd": 10.0,
                        "price": 0.50,
                    }
                ],
            )
            resolution_row = {"market_id": "KXTEST-4", "outcome": "NO"}
            _write_jsonl(resolutions_a, [resolution_row])
            _write_jsonl(resolutions_b, [dict(resolution_row)])

            result = build_unified_replay_corpus(
                output_dir=output_dir,
                decision_paths=[decisions],
                resolution_paths=[resolutions_a, resolutions_b],
                resolved_replay_paths=[],
            )

            rows = [json.loads(line) for line in result["corpus_path"].read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertIsNone(rows[0]["blocker"])
            self.assertTrue(rows[0]["won"])
            self.assertEqual(result["summary"]["blocker_counts"], {})


if __name__ == "__main__":
    unittest.main()
