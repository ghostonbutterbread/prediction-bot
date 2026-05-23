import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.weather.source_router import (
    build_joined_source_router_ledger_rows,
    build_source_router_replay_rows,
    select_source_for_candidate,
    summarize_source_router_replay_rows,
)
from scripts.weather_source_router_replay import main as source_router_main
from scripts.weather_source_router_replay import _load_router_ledger_rows
from scripts.backfill import main as backfill_main

REPO_ROOT = Path(__file__).resolve().parents[1]


def ledger_row(
    *,
    source_id: str,
    source_side: str,
    official: str,
    observed_at: str,
    outcome_known_at: str,
    shared_candidate_id: str,
    market_id: str,
    stable_action: str = "BUY_YES",
    city_id: str = "miami_fl",
    price_yes: float = 0.25,
    price_no: float = 0.75,
) -> dict:
    return {
        "market_id": market_id,
        "shared_candidate_id": shared_candidate_id,
        "source_id": source_id,
        "source_name": source_id,
        "city_id": city_id,
        "market_kind": "high",
        "contract_shape": "range",
        "question_side": "above",
        "observed_at": observed_at,
        "outcome_known_at": outcome_known_at,
        "predicted_outcome": source_side,
        "official_outcome": official,
        "best_yes_ask": price_yes,
        "best_no_ask": price_no,
        "stable_action": stable_action,
        "stable_approved_position_size_usd": 100.0,
        "stable_reason_code": "approved",
    }


def source_row(
    *,
    market_id: str = "KXHIGHMIA-26MAY03-T80",
    shared_candidate_id: str = "candidate-1",
    observed_at: str = "2026-05-03T12:00:00+00:00",
    source_name: str = "nws",
    forecast_high: float = 82.0,
    actual_temp: float = 83.0,
) -> dict:
    return {
        "market_id": market_id,
        "shared_candidate_id": shared_candidate_id,
        "question": "Will the maximum temperature in Miami be >80 on May 3, 2026?",
        "observed_at": observed_at,
        "resolved_at": "2026-05-04T12:00:00+00:00",
        "actual_temp_used": actual_temp,
        "weather_risk": {
            "evidence": {
                "weather_station_resolution": {
                    "city_id": "miami_fl",
                    "city": "Miami",
                    "station_id": "KMIA",
                }
            }
        },
        "decision_artifact": {
            "strategy_trace": {
                "raw_signals": {
                    "live": {
                        "data": {
                            "threshold": 80.0,
                            "question_side": "above",
                            "market_date": "2026-05-03",
                            "source_details": [{"source_name": source_name, "forecast_high": forecast_high}],
                        }
                    }
                }
            }
        },
    }


def stable_decision(
    *,
    market_id: str = "KXHIGHMIA-26MAY03-T80",
    shared_candidate_id: str = "candidate-1",
    observed_at: str = "2026-05-03T12:00:03+00:00",
    action: str = "BUY_YES",
) -> dict:
    return {
        "decision_id": "stable-1",
        "policy": "control_stable",
        "wallet_id": "stable_paper",
        "market_id": market_id,
        "shared_candidate_id": shared_candidate_id,
        "observed_at": observed_at,
        "action": action,
        "side": "YES" if action == "BUY_YES" else None,
        "approved_position_size_usd": 10.0,
        "requested_position_size_usd": 10.0,
        "reason_code": "approved",
        "yes_price": 0.2,
        "no_price": 0.8,
    }


class WeatherSourceRouterTests(unittest.TestCase):
    def test_select_source_uses_only_prior_history_for_matching_slice(self):
        candidate = {
            "city_id": "miami_fl",
            "market_kind": "high",
            "contract_shape": "range",
            "question_side": "above",
            "observed_at": "2026-05-03T12:00:00+00:00",
        }
        history = [
            {
                "source_id": "nws",
                "source_name": "NWS",
                "city_id": "miami_fl",
                "market_kind": "high",
                "contract_shape": "range",
                "question_side": "above",
                "eligible_for_edge_validation": True,
                "outcome_known_at": "2026-05-02T00:00:00+00:00",
                "win": True,
                "binary_edge_realized": 0.6,
            },
            {
                "source_id": "open_meteo",
                "city_id": "miami_fl",
                "market_kind": "high",
                "contract_shape": "range",
                "question_side": "above",
                "eligible_for_edge_validation": True,
                "outcome_known_at": "2026-05-04T00:00:00+00:00",
                "win": True,
                "binary_edge_realized": 0.8,
            },
        ]

        selected = select_source_for_candidate(candidate, history, min_sample_count=1)

        self.assertTrue(selected["routeable"])
        self.assertEqual(selected["chosen_source_id"], "nws")
        self.assertEqual(selected["prior_sample_count"], 1)

    def test_replay_filters_stable_buy_when_best_source_disagrees(self):
        rows = [
            ledger_row(
                source_id="open_meteo",
                source_side="NO",
                official="NO",
                observed_at="2026-05-01T12:00:00+00:00",
                outcome_known_at="2026-05-02T00:00:00+00:00",
                shared_candidate_id="old-1",
                market_id="KXOLD1",
            ),
            ledger_row(
                source_id="nws",
                source_side="YES",
                official="NO",
                observed_at="2026-05-01T12:00:00+00:00",
                outcome_known_at="2026-05-02T00:00:00+00:00",
                shared_candidate_id="old-1",
                market_id="KXOLD1",
            ),
            ledger_row(
                source_id="open_meteo",
                source_side="NO",
                official="NO",
                observed_at="2026-05-03T12:00:00+00:00",
                outcome_known_at="2026-05-04T00:00:00+00:00",
                shared_candidate_id="new-1",
                market_id="KXNEW1",
            ),
            ledger_row(
                source_id="nws",
                source_side="YES",
                official="NO",
                observed_at="2026-05-03T12:00:00+00:00",
                outcome_known_at="2026-05-04T00:00:00+00:00",
                shared_candidate_id="new-1",
                market_id="KXNEW1",
            ),
        ]

        replay = build_source_router_replay_rows(rows, min_sample_count=1)
        by_candidate = {row["shared_candidate_id"]: row for row in replay}
        new_row = by_candidate["new-1"]

        self.assertEqual(new_row["router"]["chosen_source_id"], "open_meteo")
        self.assertEqual(new_row["router"]["source_implied_side"], "NO")
        self.assertEqual(new_row["comparison"]["source_router_action"], "BUY_NO")
        self.assertGreater(new_row["comparison"]["source_router_pnl_usd"], 0)
        self.assertEqual(new_row["comparison"]["source_filter_action"], "SKIP")
        self.assertTrue(new_row["comparison"]["would_filter_stable_buy"])
        self.assertGreater(new_row["comparison"]["source_filter_minus_stable_pnl_usd"], 0)

    def test_source_router_can_buy_when_stable_skips(self):
        rows = [
            ledger_row(
                source_id="open_meteo",
                source_side="YES",
                official="YES",
                observed_at="2026-05-01T12:00:00+00:00",
                outcome_known_at="2026-05-02T00:00:00+00:00",
                shared_candidate_id="old-1",
                market_id="KXOLD1",
                stable_action="SKIP",
            ),
            ledger_row(
                source_id="open_meteo",
                source_side="YES",
                official="YES",
                observed_at="2026-05-03T12:00:00+00:00",
                outcome_known_at="2026-05-04T00:00:00+00:00",
                shared_candidate_id="new-1",
                market_id="KXNEW1",
                stable_action="SKIP",
                price_yes=0.2,
            ),
        ]

        replay = build_source_router_replay_rows(rows, min_sample_count=1)
        by_candidate = {row["shared_candidate_id"]: row for row in replay}
        new_row = by_candidate["new-1"]

        self.assertEqual(new_row["stable_baseline"]["action"], "SKIP")
        self.assertEqual(new_row["comparison"]["source_router_action"], "BUY_YES")
        self.assertEqual(new_row["comparison"]["source_router_side"], "YES")
        self.assertGreater(new_row["comparison"]["source_router_pnl_usd"], 0)

    def test_summary_reports_stable_vs_source_filter_pnl(self):
        rows = [
            ledger_row(
                source_id="open_meteo",
                source_side="YES",
                official="YES",
                observed_at="2026-05-01T12:00:00+00:00",
                outcome_known_at="2026-05-02T00:00:00+00:00",
                shared_candidate_id="old-1",
                market_id="KXOLD1",
            ),
            ledger_row(
                source_id="open_meteo",
                source_side="YES",
                official="YES",
                observed_at="2026-05-03T12:00:00+00:00",
                outcome_known_at="2026-05-04T00:00:00+00:00",
                shared_candidate_id="new-1",
                market_id="KXNEW1",
            ),
        ]

        summary = summarize_source_router_replay_rows(build_source_router_replay_rows(rows, min_sample_count=1))

        self.assertEqual(summary["summary"]["routeable_rows"], 1)
        self.assertEqual(summary["summary"]["source_router_buy_rows"], 1)
        self.assertEqual(summary["summary"]["confirmed_rows"], 1)
        self.assertGreater(summary["summary"]["source_router_pnl_usd"], 0)
        self.assertGreater(summary["summary"]["source_filter_pnl_usd"], 0)

    def test_builds_joined_ledger_rows_from_source_snapshots_and_stable_decisions(self):
        joined, stats = build_joined_source_router_ledger_rows(
            [source_row()],
            [stable_decision()],
        )

        self.assertEqual(stats["source_rows_with_decision"], 1)
        self.assertEqual(len(joined), 1)
        self.assertEqual(joined[0]["stable_action"], "BUY_YES")
        self.assertEqual(joined[0]["stable_approved_position_size_usd"], 10.0)
        self.assertEqual(joined[0]["yes_price"], 0.2)

    def test_cli_writes_replay_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            ledger_path = tmp / "ledger.jsonl"
            ledger_path.write_text(
                "\n".join(
                    [
                        __import__("json").dumps(
                            ledger_row(
                                source_id="open_meteo",
                                source_side="YES",
                                official="YES",
                                observed_at="2026-05-01T12:00:00+00:00",
                                outcome_known_at="2026-05-02T00:00:00+00:00",
                                shared_candidate_id="old-1",
                                market_id="KXOLD1",
                            )
                        ),
                        __import__("json").dumps(
                            ledger_row(
                                source_id="open_meteo",
                                source_side="YES",
                                official="YES",
                                observed_at="2026-05-03T12:00:00+00:00",
                                outcome_known_at="2026-05-04T00:00:00+00:00",
                                shared_candidate_id="new-1",
                                market_id="KXNEW1",
                            )
                        ),
                    ]
                )
                + "\n"
            )
            outcomes_path = tmp / "outcomes.jsonl"
            outcomes_path.write_text('{"market_id":"KXOLD1","official_outcome":"YES"}\n{"market_id":"KXNEW1","official_outcome":"YES"}\n')
            output_dir = tmp / "out"

            with patch(
                "sys.argv",
                [
                    "weather_source_router_replay.py",
                    "--ledger-input",
                    str(ledger_path),
                    "--outcome-input",
                    str(outcomes_path),
                    "--output-dir",
                    str(output_dir),
                    "--min-sample-count",
                    "1",
                ],
            ):
                self.assertEqual(source_router_main(), 0)

            self.assertTrue((output_dir / "source_router_decisions.jsonl").exists())
            self.assertTrue((output_dir / "source_router_summary.json").exists())
            self.assertTrue((output_dir / "source_router_vs_stable.md").exists())

    def test_cli_joins_source_and_decision_inputs_without_persistent_merge_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            source_path = tmp / "source.jsonl"
            source_path.write_text(__import__("json").dumps(source_row()) + "\n")
            decision_path = tmp / "decisions.jsonl"
            decision_path.write_text(__import__("json").dumps(stable_decision()) + "\n")
            outcomes_path = tmp / "outcomes.jsonl"
            outcomes_path.write_text('{"market_id":"KXHIGHMIA-26MAY03-T80","official_outcome":"YES"}\n')
            output_dir = tmp / "out"

            with patch(
                "sys.argv",
                [
                    "weather_source_router_replay.py",
                    "--source-input",
                    str(source_path),
                    "--decision-input",
                    str(decision_path),
                    "--outcome-input",
                    str(outcomes_path),
                    "--output-dir",
                    str(output_dir),
                    "--min-sample-count",
                    "1",
                ],
            ):
                self.assertEqual(source_router_main(), 0)

            metadata = __import__("json").loads((output_dir / "run_metadata.json").read_text())
            self.assertEqual(metadata["input_load_stats"]["mode"], "joined_source_and_decision_inputs")
            self.assertTrue((output_dir / "source_router_decisions.jsonl").exists())

    def test_unified_backfill_runs_source_router_replay(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            ledger_path = tmp / "ledger.jsonl"
            ledger_path.write_text(
                __import__("json").dumps(
                    ledger_row(
                        source_id="open_meteo",
                        source_side="YES",
                        official="YES",
                        observed_at="2026-05-01T12:00:00+00:00",
                        outcome_known_at="2026-05-02T00:00:00+00:00",
                        shared_candidate_id="old-1",
                        market_id="KXOLD1",
                    )
                )
                + "\n"
            )
            outcomes_path = tmp / "outcomes.jsonl"
            outcomes_path.write_text('{"market_id":"KXOLD1","official_outcome":"YES"}\n')
            summaries_root = REPO_ROOT / "data" / "summaries"
            data_root = summaries_root.parent
            data_root_existed = data_root.exists()
            summaries_root_existed = summaries_root.exists()
            summaries_root.mkdir(parents=True, exist_ok=True)
            try:
                with tempfile.TemporaryDirectory(dir=summaries_root) as output_tmp:
                    output_dir = Path(output_tmp)
                    with patch(
                        "sys.argv",
                        [
                            "backfill.py",
                            "--kind",
                            "source-router-replay",
                            str(ledger_path),
                            "--resolutions",
                            str(outcomes_path),
                            "--output-dir",
                            str(output_dir),
                            "--min-sample-count",
                            "1",
                        ],
                    ):
                        self.assertEqual(backfill_main(), 0)

                    self.assertTrue((output_dir / "source_router_decisions.jsonl").exists())
                    self.assertTrue((output_dir / "run_metadata.json").exists())
            finally:
                if not summaries_root_existed:
                    summaries_root.rmdir()
                if not data_root_existed:
                    data_root.rmdir()

    def test_history_ledger_rows_do_not_consume_replay_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            history_path = tmp / "history.jsonl"
            ledger_path = tmp / "current.jsonl"
            history_path.write_text(
                "".join(
                    __import__("json").dumps(
                        ledger_row(
                            source_id="open_meteo",
                            source_side="YES",
                            official="YES",
                            observed_at=f"2026-05-0{index}T12:00:00+00:00",
                            outcome_known_at=f"2026-05-0{index}T23:00:00+00:00",
                            shared_candidate_id=f"history-{index}",
                            market_id=f"KXHISTORY{index}",
                        )
                    )
                    + "\n"
                    for index in range(1, 4)
                )
            )
            ledger_path.write_text(
                "".join(
                    __import__("json").dumps(
                        ledger_row(
                            source_id="nws",
                            source_side="NO",
                            official="NO",
                            observed_at=f"2026-05-1{index}T12:00:00+00:00",
                            outcome_known_at=f"2026-05-1{index}T23:00:00+00:00",
                            shared_candidate_id=f"current-{index}",
                            market_id=f"KXCURRENT{index}",
                        )
                    )
                    + "\n"
                    for index in range(1, 3)
                )
            )

            rows, stats = _load_router_ledger_rows(
                [ledger_path],
                history_ledger_paths=[history_path],
                source_paths=[],
                decision_paths=[],
                limit=1,
            )

            current_rows = [row for row in rows if not row.get("source_router_history_only")]
            self.assertEqual(len(rows), 4)
            self.assertEqual(len(current_rows), 1)
            self.assertEqual(current_rows[0]["shared_candidate_id"], "current-1")
            self.assertEqual(stats["history_ledger_rows"], 3)
            self.assertEqual(stats["replay_ledger_rows"], 1)
            self.assertTrue(stats["limit_reached"])


if __name__ == "__main__":
    unittest.main()
