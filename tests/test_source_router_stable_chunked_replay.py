from scripts.source_router_stable_chunked_replay import _aggregate


def test_chunked_aggregate_preserves_composition_name():
    aggregate = _aggregate(
        [
            {
                "candidate_groups": 2,
                "composed_rows": 2,
                "buy_rows": 1,
                "skip_rows": 1,
                "resolved_rows": 1,
                "pnl_calculable_rows": 1,
                "winning_buy_rows": 1,
                "losing_buy_rows": 0,
                "total_stake_usd": 10.0,
                "total_pnl_usd": 5.0,
            }
        ],
        composition_name="stable_with_source_router_veto",
    )

    assert aggregate["composition"] == "stable_with_source_router_veto"
    assert aggregate["total_pnl_usd"] == 5.0
    assert aggregate["roi_pct"] == 50.0
