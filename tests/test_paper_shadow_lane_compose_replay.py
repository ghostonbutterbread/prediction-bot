import unittest
from copy import deepcopy
from pathlib import Path

from scripts.paper_shadow_lane_compose_replay import compose_lane_replay


ROOT = Path(__file__).resolve().parent.parent


def _load_composition(name: str) -> dict:
    import yaml

    return yaml.safe_load((ROOT / "lane_compositions" / f"{name}.yaml").read_text(encoding="utf-8"))


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


def _source_router_row(
    *,
    candidate_id: str,
    market_id: str,
    action: str,
    observed_at: str,
    contract_shape: str = "range",
    source_ids: list[str] | None = None,
    sample_count: int = 5,
    yes_price: float | None = None,
    no_price: float | None = None,
) -> dict:
    sources_used = [
        {"source_id": source_id, "sample_count": sample_count, "tier": "strong_trusted"}
        for source_id in (source_ids or ["nws"])
    ]
    side = "YES" if action == "BUY_YES" else ("NO" if action == "BUY_NO" else None)
    return {
        "policy": "shadow_source_router",
        "shared_candidate_id": candidate_id,
        "market_id": market_id,
        "observed_at": observed_at,
        "action": action,
        "approved_position_size_usd": 10.0,
        "provenance": {
            "source_router": {
                "source_grade": "STRONG_YES" if side == "YES" else "STRONG_NO",
                "source_direction": side,
                "sources_used": sources_used,
                "data_quality": {"source_observation_count": len(sources_used), "usable_forecast_count": len(sources_used)},
            },
            "future_pnl_inputs": {
                "shared_candidate_id": candidate_id,
                "market_id": market_id,
                "observed_at": observed_at,
                "recommended_action": action,
                "side": side,
                "best_yes_ask": yes_price,
                "best_no_ask": no_price,
                "approved_position_size_usd": 10.0,
                "contract_shape": contract_shape,
            },
        },
    }


class PaperShadowLaneComposeReplayTests(unittest.TestCase):
    def test_composes_router_side_with_stable_sizing_and_prices(self):
        lane_rows = [
            {
                "policy": "control_stable",
                "shared_candidate_id": "candidate-1",
                "market_id": "KXCOMPOSE-1",
                "action": "BUY_YES",
                "approved_position_size_usd": 5.0,
                "provenance": {
                    "future_pnl_inputs": {
                        "shared_candidate_id": "candidate-1",
                        "market_id": "KXCOMPOSE-1",
                        "recommended_action": "BUY_YES",
                        "side": "YES",
                        "best_yes_ask": 0.40,
                        "best_no_ask": 0.65,
                        "approved_position_size_usd": 5.0,
                    }
                },
            },
            {
                "policy": "shadow_source_router",
                "shared_candidate_id": "candidate-1",
                "market_id": "KXCOMPOSE-1",
                "action": "BUY_NO",
                "approved_position_size_usd": 10.0,
                "provenance": {
                    "future_pnl_inputs": {
                        "shared_candidate_id": "candidate-1",
                        "market_id": "KXCOMPOSE-1",
                        "recommended_action": "BUY_NO",
                        "side": "NO",
                        "best_yes_ask": 0.40,
                        "best_no_ask": 0.65,
                        "approved_position_size_usd": 10.0,
                    }
                },
            },
        ]
        config = {
            "composition": {
                "name": "router_side_stable_size",
                "base_lane": "control_stable",
                "action_lane": "shadow_source_router",
                "sizing_lane": "control_stable",
                "price_lane": "shadow_source_router",
            }
        }

        result = compose_lane_replay(
            lane_rows=lane_rows,
            resolution_rows=[{"shared_candidate_id": "candidate-1", "market_id": "KXCOMPOSE-1", "outcome": "NO"}],
            config=config,
        )

        row = result["composition_rows"][0]
        self.assertEqual(row["action"], "BUY_NO")
        self.assertEqual(row["side"], "NO")
        self.assertEqual(row["approved_position_size_usd"], 5.0)
        self.assertEqual(row["entry_price"], 0.65)
        self.assertEqual(result["summary"]["pnl"]["winning_buy_rows"], 1)
        self.assertAlmostEqual(result["summary"]["pnl"]["total_pnl_usd"], 2.6923)

    def test_vetoes_side_conflict_without_mutating_source_rows(self):
        lane_rows = [
            {
                "policy": "control_stable",
                "shared_candidate_id": "candidate-2",
                "market_id": "KXCOMPOSE-2",
                "action": "BUY_YES",
                "approved_position_size_usd": 5.0,
                "provenance": {
                    "future_pnl_inputs": {
                        "shared_candidate_id": "candidate-2",
                        "market_id": "KXCOMPOSE-2",
                        "recommended_action": "BUY_YES",
                        "side": "YES",
                        "best_yes_ask": 0.40,
                        "best_no_ask": 0.65,
                        "approved_position_size_usd": 5.0,
                    }
                },
            },
            {
                "policy": "shadow_source_router",
                "shared_candidate_id": "candidate-2",
                "market_id": "KXCOMPOSE-2",
                "action": "BUY_NO",
                "provenance": {
                    "future_pnl_inputs": {
                        "shared_candidate_id": "candidate-2",
                        "market_id": "KXCOMPOSE-2",
                        "recommended_action": "BUY_NO",
                        "side": "NO",
                        "best_no_ask": 0.65,
                    }
                },
            },
        ]
        config = {
            "composition": {
                "name": "stable_requires_source_agreement",
                "base_lane": "control_stable",
                "action_lane": "control_stable",
                "sizing_lane": "control_stable",
                "vetoes": [{"lane": "shadow_source_router", "mode": "require_agreement"}],
            }
        }

        result = compose_lane_replay(
            lane_rows=lane_rows,
            resolution_rows=[{"shared_candidate_id": "candidate-2", "market_id": "KXCOMPOSE-2", "outcome": "YES"}],
            config=config,
        )

        row = result["composition_rows"][0]
        self.assertEqual(row["action"], "SKIP")
        self.assertEqual(row["reason_code"], "side_conflict:shadow_source_router")
        self.assertFalse(row["mutation_contract"]["mutates_accounting"])
        self.assertEqual(result["summary"]["diagnostics"], {"side_conflict:shadow_source_router": 1})
        self.assertEqual(result["resolved_rows"][0]["blocker"], None)
        self.assertEqual(result["summary"]["pnl"]["skip_rows"], 1)

    def test_summary_uses_emitted_resolved_rows_for_blockers(self):
        lane_rows = [
            {
                "policy": "control_stable",
                "shared_candidate_id": "candidate-3",
                "market_id": "KXCOMPOSE-3",
                "action": "BUY_YES",
                "approved_position_size_usd": 5.0,
                "provenance": {
                    "future_pnl_inputs": {
                        "shared_candidate_id": "candidate-3",
                        "market_id": "KXCOMPOSE-3",
                        "recommended_action": "BUY_YES",
                        "side": "YES",
                        "approved_position_size_usd": 5.0,
                    }
                },
            }
        ]
        config = {
            "composition": {
                "name": "stable_missing_price",
                "base_lane": "control_stable",
                "action_lane": "control_stable",
                "sizing_lane": "control_stable",
            }
        }

        result = compose_lane_replay(
            lane_rows=lane_rows,
            resolution_rows=[{"shared_candidate_id": "candidate-3", "market_id": "KXCOMPOSE-3", "outcome": "YES"}],
            config=config,
        )

        self.assertEqual(result["resolved_rows"][0]["blocker"], "missing_fill_price")
        self.assertEqual(result["summary"]["pnl"]["blocker_counts"], {"missing_fill_price": 1})
        self.assertEqual(result["summary"]["pnl"]["pnl_calculable_rows"], 0)

    def test_confidence_floor_with_source_router_veto_uses_confidence_variables_and_scores_pnl(self):
        lane_rows = [
            _lane_row(
                policy="shadow_confidence_floor",
                candidate_id="candidate-4",
                market_id="KXCOMPOSE-4",
                action="BUY_YES",
                size=6.0,
                yes_price=0.50,
                no_price=0.55,
            ),
            _lane_row(
                policy="shadow_source_router",
                candidate_id="candidate-4",
                market_id="KXCOMPOSE-4",
                action="BUY_YES",
                size=12.0,
                yes_price=0.42,
                no_price=0.70,
            ),
        ]
        original_lane_rows = deepcopy(lane_rows)

        result = compose_lane_replay(
            lane_rows=lane_rows,
            resolution_rows=[{"shared_candidate_id": "candidate-4", "market_id": "KXCOMPOSE-4", "outcome": "YES"}],
            config=_load_composition("confidence_floor_with_source_router_veto"),
        )

        row = result["composition_rows"][0]
        self.assertEqual(row["policy"], "composition:confidence_floor_with_source_router_veto")
        self.assertEqual(row["action"], "BUY_YES")
        self.assertEqual(row["side"], "YES")
        self.assertEqual(row["approved_position_size_usd"], 6.0)
        self.assertEqual(row["entry_price"], 0.50)
        self.assertEqual(row["provenance"]["composition"]["action_lane"], "shadow_confidence_floor")
        self.assertEqual(row["provenance"]["composition"]["sizing_lane"], "shadow_confidence_floor")
        self.assertEqual(row["provenance"]["composition"]["price_lane"], "shadow_confidence_floor")
        self.assertFalse(row["mutation_contract"]["mutates_accounting"])
        self.assertTrue(row["non_mutating"])
        self.assertEqual(lane_rows, original_lane_rows)
        self.assertEqual(result["resolved_rows"][0]["blocker"], None)
        self.assertEqual(result["summary"]["pnl"]["winning_buy_rows"], 1)
        self.assertAlmostEqual(result["summary"]["pnl"]["total_pnl_usd"], 6.0)

    def test_confidence_floor_with_source_router_veto_skips_on_source_disagreement(self):
        lane_rows = [
            _lane_row(
                policy="shadow_confidence_floor",
                candidate_id="candidate-5",
                market_id="KXCOMPOSE-5",
                action="BUY_YES",
                size=6.0,
                yes_price=0.50,
                no_price=0.55,
            ),
            _lane_row(
                policy="shadow_source_router",
                candidate_id="candidate-5",
                market_id="KXCOMPOSE-5",
                action="BUY_NO",
                size=12.0,
                yes_price=0.42,
                no_price=0.70,
            ),
        ]
        original_lane_rows = deepcopy(lane_rows)

        result = compose_lane_replay(
            lane_rows=lane_rows,
            resolution_rows=[{"shared_candidate_id": "candidate-5", "market_id": "KXCOMPOSE-5", "outcome": "YES"}],
            config=_load_composition("confidence_floor_with_source_router_veto"),
        )

        row = result["composition_rows"][0]
        self.assertEqual(row["action"], "SKIP")
        self.assertEqual(row["reason_code"], "side_conflict:shadow_source_router")
        self.assertEqual(row["approved_position_size_usd"], 0.0)
        self.assertFalse(row["mutation_contract"]["mutates_balance"])
        self.assertFalse(row["mutation_contract"]["places_live_orders"])
        self.assertEqual(lane_rows, original_lane_rows)
        self.assertEqual(result["summary"]["diagnostics"], {"side_conflict:shadow_source_router": 1})
        self.assertEqual(result["summary"]["pnl"]["skip_rows"], 1)

    def test_source_router_side_confidence_floor_size_uses_router_action_price_and_confidence_size(self):
        lane_rows = [
            _lane_row(
                policy="shadow_confidence_floor",
                candidate_id="candidate-6",
                market_id="KXCOMPOSE-6",
                action="BUY_YES",
                size=7.0,
                yes_price=0.50,
                no_price=0.56,
            ),
            _lane_row(
                policy="shadow_source_router",
                candidate_id="candidate-6",
                market_id="KXCOMPOSE-6",
                action="BUY_NO",
                size=14.0,
                yes_price=0.43,
                no_price=0.70,
            ),
        ]
        original_lane_rows = deepcopy(lane_rows)

        result = compose_lane_replay(
            lane_rows=lane_rows,
            resolution_rows=[{"shared_candidate_id": "candidate-6", "market_id": "KXCOMPOSE-6", "outcome": "NO"}],
            config=_load_composition("source_router_side_confidence_floor_size"),
        )

        row = result["composition_rows"][0]
        self.assertEqual(row["policy"], "composition:source_router_side_confidence_floor_size")
        self.assertEqual(row["action"], "BUY_NO")
        self.assertEqual(row["side"], "NO")
        self.assertEqual(row["approved_position_size_usd"], 7.0)
        self.assertEqual(row["entry_price"], 0.70)
        self.assertEqual(row["provenance"]["composition"]["action_lane"], "shadow_source_router")
        self.assertEqual(row["provenance"]["composition"]["sizing_lane"], "shadow_confidence_floor")
        self.assertEqual(row["provenance"]["composition"]["price_lane"], "shadow_source_router")
        self.assertFalse(row["mutation_contract"]["mutates_accounting"])
        self.assertTrue(row["non_mutating"])
        self.assertEqual(lane_rows, original_lane_rows)
        self.assertEqual(result["resolved_rows"][0]["blocker"], None)
        self.assertEqual(result["summary"]["pnl"]["winning_buy_rows"], 1)
        self.assertAlmostEqual(result["summary"]["pnl"]["total_pnl_usd"], 3.0)

    def test_source_router_side_confidence_floor_size_requires_router_rows(self):
        lane_rows = [
            _lane_row(
                policy="shadow_confidence_floor",
                candidate_id="candidate-7",
                market_id="KXCOMPOSE-7",
                action="BUY_YES",
                size=7.0,
                yes_price=0.50,
                no_price=0.56,
            ),
        ]

        result = compose_lane_replay(
            lane_rows=lane_rows,
            resolution_rows=[{"shared_candidate_id": "candidate-7", "market_id": "KXCOMPOSE-7", "outcome": "YES"}],
            config=_load_composition("source_router_side_confidence_floor_size"),
        )

        self.assertEqual(result["composition_rows"], [])
        self.assertEqual(result["resolved_rows"], [])
        self.assertEqual(result["summary"]["diagnostics"], {"missing_action_lane": 1})

    def test_gated_composition_filters_source_shape_and_sample_count(self):
        lane_rows = [
            _lane_row(
                policy="control_stable",
                candidate_id="candidate-8",
                market_id="KXCOMPOSE-8",
                action="BUY_YES",
                size=5.0,
                yes_price=0.50,
            ),
            _source_router_row(
                candidate_id="candidate-8",
                market_id="KXCOMPOSE-8",
                action="BUY_YES",
                observed_at="2026-06-01T00:00:00+00:00",
                contract_shape="range",
                source_ids=["nws"],
                sample_count=5,
                yes_price=0.40,
            ),
            _lane_row(
                policy="control_stable",
                candidate_id="candidate-9",
                market_id="KXCOMPOSE-9",
                action="BUY_YES",
                size=5.0,
                yes_price=0.50,
            ),
            _source_router_row(
                candidate_id="candidate-9",
                market_id="KXCOMPOSE-9",
                action="BUY_YES",
                observed_at="2026-06-01T00:00:00+00:00",
                contract_shape="tail",
                source_ids=["nws"],
                sample_count=5,
                yes_price=0.40,
            ),
        ]

        result = compose_lane_replay(
            lane_rows=lane_rows,
            resolution_rows=[
                {"shared_candidate_id": "candidate-8", "market_id": "KXCOMPOSE-8", "outcome": "YES"},
                {"shared_candidate_id": "candidate-9", "market_id": "KXCOMPOSE-9", "outcome": "YES"},
            ],
            config=_load_composition("range_sample_ge5_cap1"),
        )

        self.assertEqual([row["shared_candidate_id"] for row in result["composition_rows"]], ["candidate-8"])
        self.assertEqual(result["summary"]["diagnostics"]["composed_buy"], 1)
        self.assertEqual(result["summary"]["diagnostics"]["gate_failed:range_contract"], 1)
        self.assertEqual(result["summary"]["pnl"]["winning_buy_rows"], 1)

    def test_exposure_cap_keeps_latest_row_per_market(self):
        lane_rows = [
            _lane_row(
                policy="control_stable",
                candidate_id="candidate-10a",
                market_id="KXCOMPOSE-10",
                action="BUY_YES",
                size=5.0,
                yes_price=0.50,
            ),
            _source_router_row(
                candidate_id="candidate-10a",
                market_id="KXCOMPOSE-10",
                action="BUY_YES",
                observed_at="2026-06-01T00:00:00+00:00",
                source_ids=["nws"],
                sample_count=5,
                yes_price=0.40,
            ),
            _lane_row(
                policy="control_stable",
                candidate_id="candidate-10b",
                market_id="KXCOMPOSE-10",
                action="BUY_YES",
                size=5.0,
                yes_price=0.50,
            ),
            _source_router_row(
                candidate_id="candidate-10b",
                market_id="KXCOMPOSE-10",
                action="BUY_NO",
                observed_at="2026-06-01T01:00:00+00:00",
                source_ids=["nws"],
                sample_count=5,
                no_price=0.70,
            ),
        ]

        result = compose_lane_replay(
            lane_rows=lane_rows,
            resolution_rows=[{"market_id": "KXCOMPOSE-10", "outcome": "NO"}],
            config=_load_composition("nws_only_cap1"),
        )

        self.assertEqual([row["shared_candidate_id"] for row in result["composition_rows"]], ["candidate-10b"])
        self.assertEqual(result["summary"]["diagnostics"]["exposure_dropped_rows"], 1)
        self.assertEqual(result["summary"]["pnl"]["winning_buy_rows"], 1)


if __name__ == "__main__":
    unittest.main()
