import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from bot.agent_decision_ledger import build_agent_decision_rows_from_source_row, build_agent_run_id
from bot.file_ops import append_jsonl, load_jsonl
from bot.prediction_lab import PredictionLab
from bot.shared_market_feed import (
    build_dual_policy_decision_metadata,
    build_shared_market_candidate_row,
    shared_candidate_id_from_row,
    shared_candidate_from_market_snapshot_row,
    summarize_dual_policy_pnl_snapshot_rows,
    summarize_dual_policy_snapshot_rows,
)


class SharedMarketFeedTests(unittest.TestCase):
    def _market(self):
        return SimpleNamespace(
            id="KXHIGHNY-260506-T71",
            exchange="kalshi",
            question="Will the high temperature in New York exceed 71 degrees?",
            category="weather",
            yes_price=0.41,
            no_price=0.59,
            volume=1200,
            metadata={
                "market_group": "weather",
                "market_family": "daily_temperature",
                "series": "daily_temperature",
                "series_ticker": "KXHIGHNY",
                "event_ticker": "EVT-1",
                "market_route": {"group": "weather", "family": "daily_temperature", "allowed": True},
            },
        )

    def _signal(self):
        return {
            "direction": "BUY_YES",
            "model_probability": 0.67,
            "market_price": 0.41,
            "yes_market_price": 0.41,
            "no_market_price": 0.59,
            "edge": 0.26,
            "confidence": 0.91,
            "station_id": "KNYC",
            "source_as_of": "2026-05-06T12:00:00+00:00",
            "signals": {"unit": 0.67},
        }

    def test_shared_candidate_builder_is_deterministic_for_same_inputs(self):
        artifact = {
            "final_action": "BUY_YES",
            "final_reason_code": "approved",
            "decision_latency_ms": 12.3,
            "pre_logic_order_book_snapshot": {
                "source": "book",
                "data": {
                    "best_yes_bid": 0.4,
                    "best_yes_ask": 0.41,
                    "best_no_bid": 0.58,
                    "best_no_ask": 0.59,
                },
            },
        }
        kwargs = {
            "run_id": "run-1",
            "market": self._market(),
            "signal": self._signal(),
            "decision_artifact": artifact,
            "source_runtime": "prediction_lab",
            "provenance": "live_known_at_time",
            "observed_at": "2026-05-06T12:00:01+00:00",
            "snapshot_as_of": "2026-05-06T12:00:00+00:00",
            "snapshot_ttl_seconds": 900,
            "weather_risk": {"hidden_gem_tier": "strong"},
        }

        first = build_shared_market_candidate_row(**kwargs)
        second = build_shared_market_candidate_row(**kwargs)

        self.assertEqual(first, second)
        self.assertEqual(first["schema_name"], "shared_market_candidate")
        self.assertEqual(first["schema_version"], 1)
        self.assertEqual(first["candidate_id"], second["candidate_id"])
        self.assertEqual(first["market"]["route"]["family"], "daily_temperature")
        self.assertEqual(first["prices"]["best_yes_ask"], 0.41)
        self.assertEqual(first["evidence"]["weather_risk"]["hidden_gem_tier"], "strong")
        self.assertEqual(first["decision"]["final_action"], "BUY_YES")
        json.dumps(first, sort_keys=True)

    def test_prediction_lab_snapshot_row_preserves_legacy_fields_and_embeds_shared_candidate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lab = PredictionLab(
                {
                    "data_dir": tmpdir,
                    "prediction_lab": {"enabled": True, "mode": "collector", "groups": ["weather"]},
                    "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
                }
            )
            row = lab._build_market_snapshot_row(
                "run-1",
                self._market(),
                self._signal(),
                decision_type="buy_yes",
                prediction_recorded=True,
                decision_artifact={"final_action": "BUY_YES", "final_reason_code": "approved"},
            )

        for legacy_key in (
            "timestamp",
            "observed_at",
            "run_id",
            "snapshot_key",
            "market_id",
            "group",
            "series",
            "question",
            "yes_price",
            "no_price",
            "confidence",
            "edge",
            "direction",
            "decision_type",
            "recorded_prediction",
        ):
            self.assertIn(legacy_key, row)
        shared = row["shared_candidate"]
        self.assertEqual(row["shared_candidate_id"], shared["candidate_id"])
        self.assertEqual(shared["schema_name"], "shared_market_candidate")
        self.assertEqual(shared["run_id"], row["run_id"])
        self.assertEqual(shared["market_id"], row["market_id"])
        self.assertEqual(shared["observed_at"], row["observed_at"])
        self.assertEqual(shared["source_runtime"], "prediction_lab")
        self.assertEqual(shared["provenance"], "live_known_at_time")
        self.assertEqual(shared["main_runtime"], "prediction_lab")
        self.assertEqual(shared["main_decision"]["runtime"], "prediction_lab")
        self.assertTrue(shared["main_decision"]["authoritative"])
        self.assertEqual(shared_candidate_id_from_row(row), shared["candidate_id"])

    def test_dual_policy_metadata_derives_normal_shadow_and_delta(self):
        artifact = {
            "final_action": "BUY_YES",
            "final_reason_code": "approved",
            "strategy_signal": self._signal(),
            "shared_core_decision": {
                "requested_position_size": 10.0,
                "reason_code": "approved",
                "reasoning": {"strategy_lane": {"lane_id": "edge"}},
            },
        }
        shadow_delta = {
            "status": "complete",
            "comparison_complete": True,
            "action_comparison_available": True,
            "policy": {"version": "beta", "mode": "shadow"},
            "stable": {
                "action": "BUY_YES",
                "direction": "BUY_YES",
                "reason_code": "approved",
                "requested_position_size": 10.0,
                "selected_lane": "edge",
            },
            "shadow": {
                "action": "SKIP",
                "direction": "SKIP",
                "reason_code": "weather_hidden_gem_without_strong_evidence",
                "requested_position_size": None,
                "selected_lane": "edge",
            },
            "changed": True,
            "action_changed": True,
            "side_changed": True,
            "buy_decision_changed": True,
            "reason_changed": True,
            "size_changed": True,
            "lane_changed": False,
            "evidence_sources": ["weather_risk.beta_gate"],
        }

        metadata = build_dual_policy_decision_metadata(
            artifact,
            shadow_delta=shadow_delta,
            fallback_signal=self._signal(),
            source_runtime="paper",
        )

        self.assertEqual(metadata["main_runtime"], "paper")
        self.assertEqual(metadata["main_decision"]["runtime"], "paper")
        self.assertEqual(metadata["main_decision"]["action"], "BUY_YES")
        self.assertTrue(metadata["main_decision"]["authoritative"])
        self.assertEqual(metadata["normal_decision"]["policy"], "stable")
        self.assertEqual(metadata["normal_decision"]["action"], "BUY_YES")
        self.assertEqual(metadata["normal_decision"]["side"], "YES")
        self.assertEqual(metadata["normal_decision"]["size"], 10.0)
        self.assertEqual(metadata["shadow_decision"]["policy"], "beta_shadow")
        self.assertEqual(metadata["shadow_decision"]["action"], "SKIP")
        self.assertTrue(metadata["decision_delta"]["buy_decision_changed"])
        self.assertFalse(metadata["decision_delta"]["lane_changed"])

    def test_prediction_lab_snapshot_row_includes_dual_policy_columns_when_shadow_delta_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lab = PredictionLab(
                {
                    "data_dir": tmpdir,
                    "prediction_lab": {"enabled": True, "mode": "collector", "groups": ["weather"]},
                    "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
                    "strategy_policy": {"version": "beta", "beta": {"mode": "shadow", "features": {"lane_sizing_caps": True}}},
                }
            )
            artifact = {
                "final_action": "BUY_YES",
                "final_reason_code": "approved",
                "strategy_signal": self._signal(),
                "shared_core_decision": {
                    "requested_position_size": 10.0,
                    "reason_code": "approved",
                    "reasoning": {
                        "strategy_lane": {"lane_id": "edge"},
                        "lane_sizing": {
                            "active": True,
                            "shadow": True,
                            "enforced": False,
                            "beta_adjusted_size": 4.0,
                            "policy": {"version": "beta", "beta": {"mode": "shadow", "features": {"lane_sizing_caps": True}}},
                        },
                    },
                },
            }
            row = lab._build_market_snapshot_row(
                "run-1",
                self._market(),
                self._signal(),
                decision_type="buy_yes",
                prediction_recorded=True,
                decision_artifact=artifact,
            )

        self.assertIn("shadow_delta", row)
        self.assertEqual(row["normal_decision"]["action"], "BUY_YES")
        self.assertEqual(row["normal_decision"]["size"], 10.0)
        self.assertEqual(row["shadow_decision"]["action"], "BUY_YES")
        self.assertEqual(row["shadow_decision"]["size"], 4.0)
        self.assertFalse(row["decision_delta"]["action_changed"])
        self.assertTrue(row["decision_delta"]["size_changed"])
        self.assertEqual(row["main_runtime"], "prediction_lab")
        self.assertEqual(row["main_decision"]["runtime"], "prediction_lab")
        self.assertEqual(row["shared_candidate"]["main_decision"], row["main_decision"])
        self.assertEqual(row["shared_candidate"]["normal_decision"], row["normal_decision"])
        self.assertEqual(row["shared_candidate"]["shadow_decision"], row["shadow_decision"])

    def test_shared_candidate_without_artifact_still_derives_main_from_signal(self):
        row = build_shared_market_candidate_row(
            run_id="run-1",
            market=self._market(),
            signal=self._signal(),
            source_runtime="prediction_lab",
            observed_at="2026-05-06T12:00:01+00:00",
        )

        self.assertEqual(row["main_runtime"], "prediction_lab")
        self.assertEqual(row["main_decision"]["runtime"], "prediction_lab")
        self.assertEqual(row["main_decision"]["action"], "BUY_YES")
        self.assertEqual(row["normal_decision"]["action"], "BUY_YES")

    def test_prediction_lab_snapshot_row_without_shadow_delta_keeps_normal_decision_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lab = PredictionLab(
                {
                    "data_dir": tmpdir,
                    "prediction_lab": {"enabled": True, "mode": "collector", "groups": ["weather"]},
                    "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
                    "strategy_policy": {"version": "stable"},
                }
            )
            artifact = {
                "final_action": "BUY_YES",
                "final_reason_code": "approved",
                "strategy_signal": self._signal(),
                "shared_core_decision": {"requested_position_size": 10.0, "reason_code": "approved"},
            }
            row = lab._build_market_snapshot_row(
                "run-1",
                self._market(),
                self._signal(),
                decision_type="buy_yes",
                prediction_recorded=True,
                decision_artifact=artifact,
            )

        self.assertNotIn("shadow_delta", row)
        self.assertEqual(row["normal_decision"]["action"], "BUY_YES")
        self.assertEqual(row["normal_decision"]["size"], 10.0)
        self.assertNotIn("shadow_decision", row)
        self.assertNotIn("decision_delta", row)
        self.assertEqual(row["main_runtime"], "prediction_lab")
        self.assertEqual(row["main_decision"]["action"], "BUY_YES")

    def test_dual_policy_snapshot_summary_counts_policy_columns(self):
        summary = summarize_dual_policy_snapshot_rows(
            [
                {
                    "market_id": "normal-buy-shadow-skip",
                    "normal_decision": {"action": "BUY_YES", "size": 10.0},
                    "shadow_decision": {"action": "SKIP", "size": 0.0},
                    "decision_delta": {"action_changed": True, "size_changed": True},
                },
                {
                    "market_id": "normal-skip-shadow-buy",
                    "shared_candidate": {
                        "normal_decision": {"action": "SKIP", "size": 0.0},
                        "shadow_decision": {"action": "BUY_NO", "size": 3.0},
                    },
                },
                {
                    "market_id": "normal-buy-no-shadow",
                    "normal_decision": {"action": "BUY_YES", "size": 5.0},
                },
            ]
        )

        self.assertEqual(summary["total_rows"], 3)
        self.assertEqual(summary["normal_buys"], 2)
        self.assertEqual(summary["normal_skips"], 1)
        self.assertEqual(summary["shadow_buys"], 1)
        self.assertEqual(summary["shadow_skips"], 1)
        self.assertEqual(summary["action_changes"], 2)
        self.assertEqual(summary["size_changes"], 2)
        self.assertEqual(summary["skipped_by_shadow"], 1)
        self.assertEqual(summary["shadow_only_buys"], 1)
        self.assertEqual(summary["missing_shadow_decision"], 1)

    def test_dual_policy_pnl_summary_counts_avoided_losses_and_missed_wins(self):
        summary = summarize_dual_policy_pnl_snapshot_rows(
            [
                {
                    "market_id": "avoided-loss",
                    "yes_market_price": 0.25,
                    "resolution": {"outcome": "NO"},
                    "normal_decision": {"action": "BUY_YES", "size": 10.0},
                    "shadow_decision": {"action": "SKIP", "size": 0.0},
                },
                {
                    "market_id": "missed-win",
                    "yes_market_price": 0.5,
                    "resolution": {"outcome": "YES"},
                    "normal_decision": {"action": "BUY_YES", "size": 10.0},
                    "shadow_decision": {"action": "SKIP", "size": 0.0},
                },
            ]
        )

        bucket = summary["normal_buy_shadow_skip"]
        self.assertEqual(bucket["count"], 2)
        self.assertEqual(bucket["resolved_count"], 2)
        self.assertEqual(bucket["avoided_exposure_usd"], 20.0)
        self.assertEqual(bucket["avoided_loss_count"], 1)
        self.assertEqual(bucket["avoided_loss_usd"], 10.0)
        self.assertEqual(bucket["missed_win_count"], 1)
        self.assertEqual(bucket["missed_win_usd"], 10.0)
        self.assertEqual(summary["normal_hypothetical_pnl"], 0.0)
        self.assertEqual(summary["shadow_hypothetical_pnl"], 0.0)

    def test_dual_policy_pnl_summary_handles_size_adjusted_and_shadow_only_buys(self):
        summary = summarize_dual_policy_pnl_snapshot_rows(
            [
                {
                    "market_id": "smaller-buy-loss",
                    "yes_market_price": 0.5,
                    "outcome": "NO",
                    "normal_decision": {"action": "BUY_YES", "size": 10.0},
                    "shadow_decision": {"action": "BUY_YES", "size": 4.0},
                },
                {
                    "market_id": "shadow-only-win",
                    "shared_candidate": {
                        "prices": {"no_market_price": 0.25},
                        "normal_decision": {"action": "SKIP", "size": 0.0},
                        "shadow_decision": {"action": "BUY_NO", "size": 5.0},
                    },
                    "resolution": {"outcome": "NO"},
                },
                {
                    "market_id": "insufficient-price",
                    "outcome": "YES",
                    "normal_decision": {"action": "BUY_YES", "size": 1.0},
                    "shadow_decision": {"action": "SKIP", "size": 0.0},
                },
            ]
        )

        smaller = summary["normal_buy_shadow_smaller_buy"]
        self.assertEqual(smaller["count"], 1)
        self.assertEqual(smaller["resolved_count"], 1)
        self.assertEqual(smaller["size_reduction_usd"], 6.0)
        self.assertEqual(smaller["pnl_delta_shadow_minus_normal"], 6.0)
        shadow_only = summary["normal_skip_shadow_buy"]
        self.assertEqual(shadow_only["count"], 1)
        self.assertEqual(shadow_only["resolved_count"], 1)
        self.assertEqual(shadow_only["shadow_pnl_usd"], 15.0)
        self.assertEqual(shadow_only["shadow_win_count"], 1)
        self.assertEqual(summary["insufficient_rows"], 1)
        self.assertEqual(summary["normal_hypothetical_pnl"], -10.0)
        self.assertEqual(summary["shadow_hypothetical_pnl"], 11.0)
        self.assertEqual(summary["pnl_delta_shadow_minus_normal"], 21.0)

    def test_prediction_lab_summary_includes_dual_policy_pnl_comparison(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lab = PredictionLab(
                {
                    "data_dir": tmpdir,
                    "prediction_lab": {"enabled": True, "mode": "collector", "groups": ["weather"]},
                    "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
                }
            )
            append_jsonl(
                lab.market_snapshots_path,
                {
                    "market_id": "shadow-only",
                    "yes_market_price": 0.4,
                    "outcome": "YES",
                    "normal_decision": {"action": "SKIP", "size": 0.0},
                    "shadow_decision": {"action": "BUY_YES", "size": 10.0},
                },
            )
            result = lab.summarize()

        self.assertIn("dual_policy_pnl_comparison", result)
        pnl_summary = result["dual_policy_pnl_comparison"]
        self.assertEqual(pnl_summary["normal_skip_shadow_buy"]["shadow_pnl_usd"], 15.0)
        self.assertEqual(pnl_summary["shadow_hypothetical_pnl"], 15.0)

    def test_prediction_lab_summary_consumes_top_level_and_nested_shared_candidate_dual_policy_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lab = PredictionLab(
                {
                    "data_dir": tmpdir,
                    "prediction_lab": {"enabled": True, "mode": "collector", "groups": ["weather"]},
                    "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
                }
            )
            append_jsonl(
                lab.market_snapshots_path,
                {
                    "market_id": "normal-buy-shadow-skip-loss",
                    "yes_market_price": 0.25,
                    "resolution": {"outcome": "NO"},
                    "normal_decision": {"action": "BUY_YES", "size": 10.0},
                    "shadow_decision": {"action": "SKIP", "size": 0.0},
                },
            )
            append_jsonl(
                lab.market_snapshots_path,
                {
                    "market_id": "shadow-only-win",
                    "resolution": {"outcome": "NO"},
                    "shared_candidate": {
                        "prices": {"no_market_price": 0.25},
                        "normal_decision": {"action": "SKIP", "size": 0.0},
                        "shadow_decision": {"action": "BUY_NO", "size": 5.0},
                    },
                },
            )
            append_jsonl(
                lab.market_snapshots_path,
                {
                    "market_id": "smaller-shadow-buy-loss",
                    "outcome": "NO",
                    "shared_candidate": {
                        "prices": {"yes_market_price": 0.5},
                        "normal_decision": {"action": "BUY_YES", "size": 10.0},
                        "shadow_decision": {"action": "BUY_YES", "size": 4.0},
                    },
                },
            )
            append_jsonl(
                lab.market_snapshots_path,
                {
                    "market_id": "unresolved-shadow-skip",
                    "normal_decision": {"action": "BUY_YES", "size": 10.0},
                    "shadow_decision": {"action": "SKIP", "size": 0.0},
                },
            )
            append_jsonl(
                lab.market_snapshots_path,
                {
                    "market_id": "insufficient-price-shadow-skip",
                    "outcome": "YES",
                    "shared_candidate": {
                        "normal_decision": {"action": "BUY_YES", "size": 1.0},
                        "shadow_decision": {"action": "SKIP", "size": 0.0},
                    },
                },
            )

            result = lab.summarize()

        columns = result["dual_policy_columns"]
        self.assertEqual(columns["total_rows"], 5)
        self.assertEqual(columns["normal_buys"], 4)
        self.assertEqual(columns["normal_skips"], 1)
        self.assertEqual(columns["shadow_buys"], 2)
        self.assertEqual(columns["shadow_skips"], 3)
        self.assertEqual(columns["skipped_by_shadow"], 3)
        self.assertEqual(columns["shadow_only_buys"], 1)
        self.assertEqual(columns["size_changes"], 5)

        pnl_summary = result["dual_policy_pnl_comparison"]
        self.assertEqual(pnl_summary["normal_buy_shadow_skip"]["count"], 3)
        self.assertEqual(pnl_summary["normal_buy_shadow_skip"]["resolved_count"], 1)
        self.assertEqual(pnl_summary["normal_buy_shadow_skip"]["avoided_loss_count"], 1)
        self.assertEqual(pnl_summary["normal_buy_shadow_smaller_buy"]["count"], 1)
        self.assertEqual(pnl_summary["normal_buy_shadow_smaller_buy"]["pnl_delta_shadow_minus_normal"], 6.0)
        self.assertEqual(pnl_summary["normal_skip_shadow_buy"]["count"], 1)
        self.assertEqual(pnl_summary["normal_skip_shadow_buy"]["shadow_pnl_usd"], 15.0)
        self.assertEqual(pnl_summary["unresolved_rows"], 1)
        self.assertEqual(pnl_summary["insufficient_rows"], 1)
        self.assertEqual(pnl_summary["normal_hypothetical_pnl"], -20.0)
        self.assertEqual(pnl_summary["shadow_hypothetical_pnl"], 11.0)
        self.assertEqual(pnl_summary["pnl_delta_shadow_minus_normal"], 31.0)

    def test_shared_candidate_keeps_decision_truth_separate_from_accounting_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lab = PredictionLab(
                {
                    "data_dir": tmpdir,
                    "prediction_lab": {
                        "enabled": True,
                        "mode": "collector",
                        "groups": ["weather"],
                        "paper_lab_mode": "opportunity",
                    },
                    "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
                    "strategy_policy": {"version": "beta", "beta": {"mode": "shadow", "features": {"lane_sizing_caps": True}}},
                }
            )
            artifact = {
                "final_action": "BUY_YES",
                "final_reason_code": "approved",
                "strategy_signal": self._signal(),
                "account_state_snapshot": {
                    "available_cash": 250.0,
                    "reserved_capital": 0.0,
                    "total_exposure": 0.0,
                    "open_positions": 0,
                    "metadata": {
                        "account_state_provider": "fixed_opportunity",
                        "effective_tradable_cash": 250.0,
                    },
                },
                "shared_core_decision": {
                    "requested_position_size": 10.0,
                    "position_size": 10.0,
                    "reason_code": "approved",
                    "reasoning": {
                        "lane_sizing": {
                            "active": True,
                            "shadow": True,
                            "enforced": False,
                            "beta_adjusted_size": 4.0,
                            "policy": {"version": "beta", "beta": {"mode": "shadow", "features": {"lane_sizing_caps": True}}},
                        }
                    },
                },
                "pre_logic_order_book_snapshot": {
                    "source": "book",
                    "data": {
                        "best_yes_bid": 0.4,
                        "best_yes_ask": 0.41,
                        "best_no_bid": 0.58,
                        "best_no_ask": 0.59,
                    },
                },
                "paper_lab": {
                    "mode": "paper_lab",
                    "paper_lab_mode": "opportunity",
                    "account_state_provider": "fixed_opportunity",
                    "isolated_bankroll": True,
                    "mutates_portfolio_account": False,
                },
                "opportunity_mode": {
                    "mode": "opportunity",
                    "account_state_provider": "fixed_opportunity",
                    "bankroll_usd": 250.0,
                    "isolated_bankroll": True,
                    "mutates_portfolio_account": False,
                },
            }
            row = lab._build_market_snapshot_row(
                "run-1",
                self._market(),
                self._signal(),
                decision_type="buy_yes",
                prediction_recorded=True,
                decision_artifact=artifact,
            )

        shared = row["shared_candidate"]
        self.assertEqual(shared["main_runtime"], "prediction_lab")
        self.assertEqual(shared["main_decision"]["runtime"], "prediction_lab")
        self.assertEqual(shared["normal_decision"]["action"], "BUY_YES")
        self.assertEqual(shared["shadow_decision"]["size"], 4.0)
        self.assertEqual(shared["prices"]["best_yes_ask"], 0.41)
        self.assertEqual(shared["observed_at"], row["observed_at"])
        self.assertEqual(shared["snapshot_ttl_seconds"], row["collector_interval_seconds"])
        self.assertEqual(row["decision_artifact"]["account_state_snapshot"]["available_cash"], 250.0)
        self.assertFalse(row["paper_lab"]["mutates_portfolio_account"])
        self.assertFalse(row["order_execution_enabled"])
        for forbidden_key in (
            "available_cash",
            "reserved_capital",
            "total_exposure",
            "open_positions",
            "filled_size",
            "entry_price",
            "resolution",
            "net_pnl",
        ):
            self.assertNotIn(forbidden_key, shared)
            self.assertNotIn(forbidden_key, shared["decision"])

    def test_legacy_snapshot_rows_still_load_and_can_be_converted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "market_snapshots.jsonl"
            legacy_row = {
                "timestamp": "2026-05-06T12:00:01+00:00",
                "observed_at": "2026-05-06T12:00:01+00:00",
                "run_id": "run-old",
                "snapshot_key": "KXHIGHNY-260506-T71",
                "market_id": "KXHIGHNY-260506-T71",
                "group": "weather",
                "series": "daily_temperature",
                "question": "Will the high temperature in New York exceed 71 degrees?",
                "yes_price": 0.41,
                "no_price": 0.59,
                "confidence": 0.91,
                "edge": 0.26,
                "direction": "BUY_YES",
                "decision_type": "buy_yes",
                "recorded_prediction": True,
                "collector_interval_seconds": 900,
            }
            append_jsonl(path, legacy_row)

            loaded = load_jsonl(path)[0]
            self.assertNotIn("shared_candidate", loaded)
            self.assertIsNone(shared_candidate_id_from_row(loaded))
            self.assertEqual(loaded["market_id"], legacy_row["market_id"])
            shared = shared_candidate_from_market_snapshot_row(loaded)

        self.assertEqual(shared["schema_name"], "shared_market_candidate")
        self.assertEqual(shared["run_id"], "run-old")
        self.assertEqual(shared["market_id"], "KXHIGHNY-260506-T71")
        self.assertEqual(shared["snapshot_ttl_seconds"], 900)
        self.assertEqual(shared["market"]["series"], "daily_temperature")

    def test_agent_decision_builder_is_stable_and_allows_normal_only_rows_without_shadow(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lab = PredictionLab(
                {
                    "data_dir": tmpdir,
                    "prediction_lab": {"enabled": True, "mode": "collector", "groups": ["weather"]},
                    "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
                }
            )
            source_row = lab._build_market_snapshot_row(
                "run-1",
                self._market(),
                self._signal(),
                decision_type="buy_yes",
                prediction_recorded=True,
                decision_artifact={
                    "final_action": "BUY_YES",
                    "final_reason_code": "approved",
                    "strategy_signal": self._signal(),
                    "shared_core_decision": {
                        "requested_position_size": 10.0,
                        "reason_code": "approved",
                    },
                    "paper_lab": {
                        "mode": "paper_lab",
                        "paper_lab_mode": "opportunity",
                        "account_state_provider": "fixed_opportunity",
                        "isolated_bankroll": True,
                        "mutates_portfolio_account": False,
                    },
                    "opportunity_mode": {
                        "mode": "opportunity",
                        "account_state_provider": "fixed_opportunity",
                        "bankroll_usd": 250.0,
                        "isolated_bankroll": True,
                        "mutates_portfolio_account": False,
                    },
                },
            )

        self.assertNotIn("shadow_decision", source_row)

        first = build_agent_decision_rows_from_source_row(
            source_row,
            agent_run_id=build_agent_run_id(agent_id="prediction_lab", run_id="run-1"),
            agent_id="prediction_lab",
            runtime="prediction_lab",
            candidate_dataset_path=Path("/tmp/prediction_lab/market_snapshots.jsonl"),
        )
        second = build_agent_decision_rows_from_source_row(
            source_row,
            agent_run_id=build_agent_run_id(agent_id="prediction_lab", run_id="run-1"),
            agent_id="prediction_lab",
            runtime="prediction_lab",
            candidate_dataset_path=Path("/tmp/prediction_lab/market_snapshots.jsonl"),
        )

        self.assertEqual(first, second)
        self.assertEqual([row["decision_role"] for row in first], ["main", "normal", "prediction_lab_paper"])
        self.assertEqual({row["shared_candidate_id"] for row in first}, {source_row["shared_candidate_id"]})
        self.assertTrue(all(row["decision_id"] for row in first))
        self.assertTrue(all(row["candidate_dataset_path"].endswith("market_snapshots.jsonl") for row in first))
        paper_row = next(row for row in first if row["decision_role"] == "prediction_lab_paper")
        self.assertEqual(paper_row["accounting_ref"]["namespace"], "/tmp/prediction_lab/paper_accounting")
        self.assertNotIn("ledger_path", paper_row["accounting_ref"])
        self.assertFalse(paper_row["accounting_ref"]["mutates_balance"])
        self.assertFalse(paper_row["accounting_ref"]["mutates_accounting"])
        self.assertFalse(paper_row["accounting_ref"]["places_orders"])
        self.assertEqual(paper_row["accounting_ref"]["balance_model"], "fixed_opportunity")
        self.assertFalse(paper_row["mutation_contract"]["mutates_accounting"])
        self.assertFalse(paper_row["mutation_contract"]["places_orders"])
        with self.assertRaises(ValueError):
            build_agent_decision_rows_from_source_row(
                source_row,
                agent_run_id=build_agent_run_id(agent_id="prediction_lab", run_id="run-1"),
                agent_id="prediction_lab",
                runtime="prediction_lab",
                candidate_dataset_path=None,
            )

    def test_mutating_prediction_lab_metadata_suppresses_paper_agent_decision(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lab = PredictionLab(
                {
                    "data_dir": tmpdir,
                    "prediction_lab": {"enabled": True, "mode": "collector", "groups": ["weather"]},
                    "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
                }
            )
            source_row = lab._build_market_snapshot_row(
                "run-1",
                self._market(),
                self._signal(),
                decision_type="buy_yes",
                prediction_recorded=True,
                decision_artifact={
                    "final_action": "BUY_YES",
                    "final_reason_code": "approved",
                    "strategy_signal": self._signal(),
                    "shared_core_decision": {
                        "requested_position_size": 10.0,
                        "reason_code": "approved",
                    },
                    "paper_lab": {
                        "mode": "paper_lab",
                        "paper_lab_mode": "opportunity",
                        "account_state_provider": "fixed_opportunity",
                        "mutates_portfolio_account": True,
                    },
                    "opportunity_mode": {
                        "mode": "opportunity",
                        "account_state_provider": "fixed_opportunity",
                        "mutates_portfolio_account": True,
                    },
                },
            )

        rows = build_agent_decision_rows_from_source_row(
            source_row,
            agent_run_id=build_agent_run_id(agent_id="prediction_lab", run_id="run-1"),
            agent_id="prediction_lab",
            runtime="prediction_lab",
            candidate_dataset_path=Path("/tmp/prediction_lab/market_snapshots.jsonl"),
        )

        self.assertNotIn("prediction_lab_paper", [row["decision_role"] for row in rows])


if __name__ == "__main__":
    unittest.main()
