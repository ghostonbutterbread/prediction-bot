from bot.trade_audit import apply_execution_audit_contract, validate_execution_audit_row


def test_placed_row_with_size_defaults_to_unfilled_open_order():
    row = apply_execution_audit_contract(
        {
            "trade_id": "placed-legacy-size",
            "timestamp": "2026-04-28T00:00:00+00:00",
            "market_id": "m-placed",
            "direction": "BUY_YES",
            "status": "placed",
            "size": 3.0,
            "requested_size": 3.0,
            "approved_size": 3.0,
            "market_price": 0.42,
            "decision_reason_code": "approved",
        }
    )

    assert row["status"] == "placed"
    assert row["lifecycle_state"] == "placed_open"
    assert row["placed_size"] == 3.0
    assert row["filled_size"] == 0.0
    assert row["remaining_size"] == 3.0
    assert row["reserved_capital"] == 3.0
    assert validate_execution_audit_row(row) == []


def test_failed_placement_row_uses_stage_failed_lifecycle_and_no_active_size():
    row = apply_execution_audit_contract(
        {
            "trade_id": "failed-placement",
            "timestamp": "2026-04-28T00:00:00+00:00",
            "market_id": "m-failed",
            "direction": "BUY_YES",
            "status": "failed",
            "failure_stage": "placement",
            "requested_size": 3.0,
            "approved_size": 3.0,
            "market_price": 0.42,
            "decision_reason_code": "approved",
        }
    )

    assert row["status"] == "failed"
    assert row["lifecycle_state"] == "placement_failed"
    assert row["placed_size"] == 0.0
    assert row["filled_size"] == 0.0
    assert row["remaining_size"] == 0.0
    assert row["reserved_capital"] == 0.0
    assert validate_execution_audit_row(row) == []


def test_canceled_partial_keeps_filled_exposure_reserved():
    row = apply_execution_audit_contract(
        {
            "trade_id": "cancel-partial",
            "timestamp": "2026-04-28T00:00:00+00:00",
            "market_id": "m-cancel-partial",
            "direction": "BUY_YES",
            "status": "canceled",
            "requested_size": 5.0,
            "approved_size": 5.0,
            "placed_size": 5.0,
            "filled_size": 2.0,
            "remaining_size": 0.0,
            "market_price": 0.42,
            "decision_reason_code": "reconciled_resting_order",
        }
    )

    assert row["status"] == "canceled"
    assert row["lifecycle_state"] == "canceled_partial"
    assert row["filled_size"] == 2.0
    assert row["remaining_size"] == 0.0
    assert row["reserved_capital"] == 2.0
    assert validate_execution_audit_row(row) == []
