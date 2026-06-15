import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot.file_ops import load_jsonl
from bot.resolution_feed import run_resolution_feed_once, write_unique_market_refs


class ResolutionFeedTests(unittest.TestCase):
    def _write_jsonl(self, path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")

    def test_write_unique_market_refs_dedupes_nested_decision_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "decisions.jsonl"
            self._write_jsonl(
                input_path,
                [
                    {"market_id": "KXRESOLVE-26JUN11-T70"},
                    {
                        "shared_candidate": {"market_id": "KXRESOLVE-26JUN11-T71"},
                        "provenance": {"future_pnl_inputs": {"market_id": "KXRESOLVE-26JUN11-T72"}},
                    },
                    {"provenance": {"shared_candidate": {"market_id": "KXRESOLVE-26JUN11-T71"}}},
                    {"not_a_market": True},
                ],
            )

            refs_path = write_unique_market_refs(input_path, output_dir=tmp_path / "feed")
            rows = load_jsonl(refs_path)

        self.assertEqual(
            rows,
            [
                {"market_id": "KXRESOLVE-26JUN11-T70"},
                {"market_id": "KXRESOLVE-26JUN11-T71"},
            ],
        )

    def test_run_resolution_feed_once_writes_latest_artifacts_and_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "decisions.jsonl"
            output_dir = tmp_path / "resolution_feed"
            self._write_jsonl(
                input_path,
                [
                    {"market_id": "KXRESOLVE-26JUN11-T70"},
                    {"market_id": "KXRESOLVE-26JUN11-T71"},
                ],
            )

            def fetch(market_id: str):
                if market_id.endswith("T70"):
                    return {
                        "ticker": market_id,
                        "status": "finalized",
                        "result": "yes",
                        "settlement_value_dollars": "1.0000",
                    }
                return {"ticker": market_id, "status": "closed"}

            result = run_resolution_feed_once(
                {
                    "resolution_feed": {
                        "enabled": True,
                        "decision_ledger_path": str(input_path),
                        "output_dir": str(output_dir),
                        "interval_seconds": 1800,
                    }
                },
                now=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc),
                fetch_market=fetch,
            )

            state = json.loads((output_dir / "state.json").read_text(encoding="utf-8"))
            latest_rows = load_jsonl(output_dir / "latest_resolutions.jsonl")
            report = json.loads((output_dir / "latest_resolutions.report.json").read_text(encoding="utf-8"))

        self.assertEqual(result.status, "refreshed")
        self.assertTrue(result.refreshed)
        self.assertEqual(result.resolved_market_count, 1)
        self.assertEqual(result.unresolved_market_count, 1)
        self.assertEqual(latest_rows[0]["market_id"], "KXRESOLVE-26JUN11-T70")
        self.assertEqual(latest_rows[0]["resolution"]["outcome"], "YES")
        self.assertEqual(report["markets_requested"], 2)
        self.assertEqual(state["status"], "refreshed")
        self.assertEqual(Path(state["latest_resolution_path"]), output_dir / "latest_resolutions.jsonl")

    def test_run_resolution_feed_once_skips_when_not_due(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "decisions.jsonl"
            output_dir = tmp_path / "resolution_feed"
            self._write_jsonl(input_path, [{"market_id": "KXRESOLVE-26JUN11-T70"}])

            config = {
                "resolution_feed": {
                    "enabled": True,
                    "decision_ledger_path": str(input_path),
                    "output_dir": str(output_dir),
                    "interval_seconds": 1800,
                }
            }
            first = run_resolution_feed_once(
                config,
                now=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc),
                fetch_market=lambda market_id: {
                    "ticker": market_id,
                    "status": "finalized",
                    "result": "yes",
                    "settlement_value_dollars": "1.0000",
                },
            )
            second = run_resolution_feed_once(
                config,
                now=datetime(2026, 6, 11, 12, 10, tzinfo=timezone.utc),
                fetch_market=lambda market_id: (_ for _ in ()).throw(AssertionError("fetch should not run")),
            )

        self.assertTrue(first.refreshed)
        self.assertEqual(second.status, "skipped")
        self.assertEqual(second.reason, "not_due")
        self.assertEqual(second.output_path, output_dir / "latest_resolutions.jsonl")

    def test_incremental_unresolved_fetches_only_missing_market_refs_and_merges_latest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_path = tmp_path / "decisions.jsonl"
            output_dir = tmp_path / "resolution_feed"
            self._write_jsonl(
                input_path,
                [
                    {"market_id": "KXRESOLVE-26JUN11-T70"},
                    {"market_id": "KXRESOLVE-26JUN11-T71"},
                    {"market_id": "KXRESOLVE-26JUN11-T72"},
                ],
            )
            self._write_jsonl(
                output_dir / "resolution_market_refs.jsonl",
                [
                    {"market_id": "KXRESOLVE-26JUN11-T70"},
                    {"market_id": "KXRESOLVE-26JUN11-T71"},
                    {"market_id": "KXRESOLVE-26JUN11-T72"},
                ],
            )
            self._write_jsonl(
                output_dir / "latest_resolutions.jsonl",
                [
                    {
                        "market_id": "KXRESOLVE-26JUN11-T70",
                        "resolution": {"outcome": "YES", "source": "seed"},
                    }
                ],
            )
            fetched: list[str] = []

            def fetch(market_id: str):
                fetched.append(market_id)
                return {
                    "ticker": market_id,
                    "status": "finalized",
                    "result": "no",
                    "settlement_value_dollars": "0.0000",
                }

            result = run_resolution_feed_once(
                {
                    "resolution_feed": {
                        "enabled": True,
                        "decision_ledger_path": str(input_path),
                        "output_dir": str(output_dir),
                        "mode": "incremental_unresolved",
                        "max_incremental_markets": 1,
                        "interval_seconds": 1800,
                    }
                },
                now=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc),
                fetch_market=fetch,
            )

            latest_rows = load_jsonl(output_dir / "latest_resolutions.jsonl")
            state = json.loads((output_dir / "state.json").read_text(encoding="utf-8"))
            pending_rows = load_jsonl(output_dir / "resolution_market_refs.pending.jsonl")

        self.assertEqual(fetched, ["KXRESOLVE-26JUN11-T71"])
        self.assertEqual(result.status, "refreshed")
        self.assertEqual(result.resolved_market_count, 2)
        self.assertEqual([row["market_id"] for row in latest_rows], ["KXRESOLVE-26JUN11-T70", "KXRESOLVE-26JUN11-T71"])
        self.assertEqual(pending_rows, [{"market_id": "KXRESOLVE-26JUN11-T71"}])
        self.assertEqual(state["mode"], "incremental_unresolved")
        self.assertEqual(state["resolved_market_count"], 2)

    def test_central_resolution_cache_is_shared_across_lane_outputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            central_dir = tmp_path / "central_resolutions"
            lane_a_dir = tmp_path / "lane_a_resolution_feed"
            lane_b_dir = tmp_path / "lane_b_resolution_feed"
            lane_a_input = tmp_path / "lane_a_decisions.jsonl"
            lane_b_input = tmp_path / "lane_b_decisions.jsonl"
            self._write_jsonl(lane_a_input, [{"market_id": "KXRESOLVE-26JUN11-T70"}])
            self._write_jsonl(lane_b_input, [{"market_id": "KXRESOLVE-26JUN11-T70"}, {"market_id": "KXRESOLVE-26JUN11-T71"}])

            fetched: list[str] = []

            def fetch(market_id: str):
                fetched.append(market_id)
                return {
                    "ticker": market_id,
                    "status": "finalized",
                    "result": "yes",
                    "settlement_value_dollars": "1.0000",
                }

            first = run_resolution_feed_once(
                {
                    "resolution_feed": {
                        "enabled": True,
                        "decision_ledger_path": str(lane_a_input),
                        "output_dir": str(lane_a_dir),
                        "central_output_dir": str(central_dir),
                        "mode": "incremental_unresolved",
                        "interval_seconds": 1800,
                    }
                },
                now=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc),
                fetch_market=fetch,
            )
            second = run_resolution_feed_once(
                {
                    "resolution_feed": {
                        "enabled": True,
                        "decision_ledger_path": str(lane_b_input),
                        "output_dir": str(lane_b_dir),
                        "central_output_dir": str(central_dir),
                        "mode": "incremental_unresolved",
                        "interval_seconds": 1800,
                    }
                },
                now=datetime(2026, 6, 11, 13, 0, tzinfo=timezone.utc),
                fetch_market=fetch,
            )

            central_rows = load_jsonl(central_dir / "latest_resolutions.jsonl")
            lane_a_rows = load_jsonl(lane_a_dir / "latest_resolutions.jsonl")
            lane_b_rows = load_jsonl(lane_b_dir / "latest_resolutions.jsonl")
            lane_b_pending_rows = load_jsonl(lane_b_dir / "resolution_market_refs.pending.jsonl")
            lane_b_state = json.loads((lane_b_dir / "state.json").read_text(encoding="utf-8"))

        self.assertEqual(fetched, ["KXRESOLVE-26JUN11-T70", "KXRESOLVE-26JUN11-T71"])
        self.assertEqual(first.output_path, central_dir / "latest_resolutions.jsonl")
        self.assertEqual(second.output_path, central_dir / "latest_resolutions.jsonl")
        self.assertEqual([row["market_id"] for row in central_rows], ["KXRESOLVE-26JUN11-T70", "KXRESOLVE-26JUN11-T71"])
        self.assertEqual(lane_a_rows, central_rows[:1])
        self.assertEqual(lane_b_rows, central_rows)
        self.assertEqual(lane_b_pending_rows, [{"market_id": "KXRESOLVE-26JUN11-T71"}])
        self.assertEqual(Path(lane_b_state["central_resolution_path"]), central_dir / "latest_resolutions.jsonl")
        self.assertEqual(Path(lane_b_state["compatibility_latest_resolution_path"]), lane_b_dir / "latest_resolutions.jsonl")

    def test_central_resolution_cache_seeds_from_existing_lane_latest_on_migration(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            central_dir = tmp_path / "central_resolutions"
            output_dir = tmp_path / "resolution_feed"
            input_path = tmp_path / "decisions.jsonl"
            self._write_jsonl(input_path, [{"market_id": "KXRESOLVE-26JUN11-T70"}, {"market_id": "KXRESOLVE-26JUN11-T71"}])
            self._write_jsonl(
                output_dir / "latest_resolutions.jsonl",
                [
                    {
                        "market_id": "KXRESOLVE-26JUN11-T70",
                        "resolution": {"outcome": "YES", "source": "pre_central_lane_feed"},
                    }
                ],
            )
            fetched: list[str] = []

            def fetch(market_id: str):
                fetched.append(market_id)
                return {
                    "ticker": market_id,
                    "status": "closed",
                }

            result = run_resolution_feed_once(
                {
                    "resolution_feed": {
                        "enabled": True,
                        "decision_ledger_path": str(input_path),
                        "output_dir": str(output_dir),
                        "central_output_dir": str(central_dir),
                        "mode": "incremental_unresolved",
                        "max_incremental_markets": 1,
                        "interval_seconds": 1800,
                    }
                },
                now=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc),
                fetch_market=fetch,
            )

            central_rows = load_jsonl(central_dir / "latest_resolutions.jsonl")
            lane_rows = load_jsonl(output_dir / "latest_resolutions.jsonl")

        self.assertEqual(fetched, ["KXRESOLVE-26JUN11-T71"])
        self.assertEqual(result.resolved_market_count, 1)
        self.assertEqual(central_rows, lane_rows)
        self.assertEqual([row["market_id"] for row in central_rows], ["KXRESOLVE-26JUN11-T70"])


if __name__ == "__main__":
    unittest.main()
