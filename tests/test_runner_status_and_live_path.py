import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot.runner import PredictionBot
from bot.shared_core import build_trade_decision


class FakeExchange:
    def __init__(self):
        self.orders = []

    def get_markets(self, limit=30):
        return []

    def get_order_book(self, market_id):
        return {"best_yes_ask": 0.40, "best_no_ask": 0.60}

    def get_market_bid_ask(self, market_id):
        return {"best_yes_ask": 0.40, "best_no_ask": 0.60}

    def get_balance(self):
        return 25.0

    def place_order(self, market_id, side, price, size):
        order = SimpleNamespace(id=f"ord-{len(self.orders)+1}")
        self.orders.append({"market_id": market_id, "side": side, "price": price, "size": size})
        return order


class FailingExchange(FakeExchange):
    def place_order(self, market_id, side, price, size):
        return None

    def close(self):
        return None


class BadReconcileExchange(FakeExchange):
    def get_positions(self):
        raise RuntimeError("reconcile unavailable")


class DegradedExchange(FakeExchange):
    def get_positions(self):
        return []

    def get_resting_orders(self):
        return [
            {
                "order_id": "ord-resting",
                "market_id": "m-resting",
                "side": "YES",
                "requested_size": 1.0,
                "filled_size": 0.0,
                "remaining_size": 1.0,
                "price": 0.40,
                "status": "open",
                "question": "Existing resting order?",
            }
        ]


class RunnerLivePathTests(unittest.TestCase):
    def _make_bot(self, tmpdir, **overrides):
        config = {
            "log_dir": tmpdir,
            "data_dir": tmpdir,
            "trading_enabled": True,
            "max_tradable_balance_usd": 10.0,
            "max_position_size_usd": 4.0,
            "trading": {"mode": "live", "trading_enabled": True},
            "strategy": {
                "min_edge": 0.05,
                "min_confidence": 0.5,
                "enable_news": False,
                "enable_social": False,
                "enable_ai": False,
            },
        }
        config.update(overrides)
        bot = PredictionBot(config)
        bot.exchanges["kalshi"] = FakeExchange()
        bot.risk.state.current_balance = 25.0
        bot.risk.state.peak_balance = 25.0
        bot.risk.state.session_starting_balance = 25.0
        bot.risk.state.session_peak_balance = 25.0
        bot.risk.state.available_cash = 25.0
        bot.risk.state.max_drawdown_halt = False
        return bot

    def test_live_path_uses_shared_risk_caps_before_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            signal = {
                "exchange": "kalshi",
                "market_id": "m1",
                "question": "Will rain happen?",
                "direction": "BUY_YES",
                "market_price": 0.40,
                "yes_price": 0.40,
                "no_price": 0.60,
                "model_probability": 0.70,
                "edge": 0.30,
                "confidence": 0.90,
            }

            with patch.object(bot.kelly, "calculate", return_value=10.0):
                result = bot._process_signal(signal)

            self.assertIn("order", result)
            self.assertEqual(len(bot.exchanges["kalshi"].orders), 1)
            self.assertEqual(bot.exchanges["kalshi"].orders[0]["size"], 2.5)

    def test_live_path_blocks_when_startup_reconciliation_gate_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.reconciliation_gate["kalshi"] = {
                "verdict": "blocked",
                "issues": ["negative_available_cash_after_reconcile"],
            }
            signal = {
                "exchange": "kalshi",
                "market_id": "m-gated",
                "question": "Should startup gate block?",
                "direction": "BUY_YES",
                "market_price": 0.35,
                "yes_price": 0.35,
                "no_price": 0.65,
                "model_probability": 0.70,
                "edge": 0.20,
                "confidence": 0.90,
            }

            with patch.object(bot.kelly, "calculate", return_value=5.0):
                result = bot._process_signal(signal)

            self.assertEqual(result["blocked_reason"], "reconciliation_state_blocked")
            self.assertIn("negative_available_cash_after_reconcile", result["reconciliation_issues"])
            self.assertEqual(len(bot.exchanges["kalshi"].orders), 0)

    def test_live_path_blocks_negative_available_cash_runtime_invariant(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.startup_reconciliation_status["kalshi"] = {
                "completed": True,
                "source": "test",
                "status": "safe",
                "runtime_state": "safe",
                "reconciliation_verdict": "safe",
                "reconciliation_issues": [],
                "updated_at": "",
            }
            bot.risk.state.available_cash = -0.25
            signal = {
                "exchange": "kalshi",
                "market_id": "m-negative-cash",
                "question": "Should negative cash pause live entry?",
                "direction": "BUY_YES",
                "market_price": 0.40,
                "yes_price": 0.40,
                "no_price": 0.60,
                "model_probability": 0.70,
                "edge": 0.20,
                "confidence": 0.90,
            }

            with patch.object(bot.kelly, "calculate", return_value=5.0):
                result = bot._process_signal(signal)

            self.assertEqual(result["blocked_reason"], "runtime_invariant_violation")
            self.assertIn("negative_available_cash_runtime", result["reconciliation_issues"])
            self.assertEqual(len(bot.exchanges["kalshi"].orders), 0)
            self.assertEqual(bot.reconciliation_gate["kalshi"]["recovery_state"], "manual_review_required")

            snapshot = bot.build_status_snapshot(reason="manual")
            self.assertTrue(snapshot.extra["safety_pause"]["active"])
            self.assertEqual(snapshot.extra["safety_pause"]["recovery_state"], "manual_review_required")

    def test_live_path_blocks_duplicate_open_order_exposure_runtime_invariant(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.startup_reconciliation_status["kalshi"] = {
                "completed": True,
                "source": "test",
                "status": "safe",
                "runtime_state": "safe",
                "reconciliation_verdict": "safe",
                "reconciliation_issues": [],
                "updated_at": "",
            }
            bot.open_orders = [
                {
                    "order_id": "ord-a",
                    "exchange": "kalshi",
                    "market_id": "m-dup",
                    "direction": "BUY_YES",
                    "status": "open",
                    "remaining_size": 1.0,
                    "filled_size": 0.0,
                    "placed_size": 1.0,
                },
                {
                    "order_id": "ord-b",
                    "exchange": "kalshi",
                    "market_id": "m-dup",
                    "direction": "BUY_YES",
                    "status": "open",
                    "remaining_size": 1.0,
                    "filled_size": 0.0,
                    "placed_size": 1.0,
                },
            ]
            bot.risk.sync_account_state(
                current_balance=25.0,
                available_cash=23.0,
                reserved_capital=2.0,
                total_exposure=2.0,
                open_positions=0,
            )
            signal = {
                "exchange": "kalshi",
                "market_id": "m-dup",
                "question": "Should duplicate live exposure pause?",
                "direction": "BUY_YES",
                "market_price": 0.40,
                "yes_price": 0.40,
                "no_price": 0.60,
                "model_probability": 0.70,
                "edge": 0.20,
                "confidence": 0.90,
            }

            with patch.object(bot.kelly, "calculate", return_value=5.0):
                result = bot._process_signal(signal)

            self.assertEqual(result["blocked_reason"], "runtime_invariant_violation")
            self.assertIn("duplicate_live_exposure", result["reconciliation_issues"])
            self.assertEqual(len(bot.exchanges["kalshi"].orders), 0)

    def test_live_path_blocks_when_pre_trade_reconciliation_cannot_refresh(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            exchange = BadReconcileExchange()
            bot.exchanges["kalshi"] = exchange
            signal = {
                "exchange": "kalshi",
                "market_id": "m-refresh-fail",
                "question": "Should failed refresh halt live entry?",
                "direction": "BUY_YES",
                "market_price": 0.40,
                "yes_price": 0.40,
                "no_price": 0.60,
                "model_probability": 0.70,
                "edge": 0.20,
                "confidence": 0.90,
            }

            with patch.object(bot.kelly, "calculate", return_value=5.0):
                result = bot._process_signal(signal)

            self.assertEqual(result["blocked_reason"], "reconciliation_state_blocked")
            self.assertIn("reconciliation_refresh_failed", result["reconciliation_issues"])
            self.assertEqual(exchange.orders, [])
            self.assertEqual(bot.live_runtime_state["state"], "blocked")

    def test_live_path_can_block_degraded_runtime_by_policy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(
                tmpdir,
                trading={
                    "mode": "live",
                    "trading_enabled": True,
                    "live_reconciliation": {"block_on_degraded": True},
                },
            )
            exchange = DegradedExchange()
            bot.exchanges["kalshi"] = exchange
            bot._reconcile_exchange_state("kalshi", exchange)
            signal = {
                "exchange": "kalshi",
                "market_id": "m-new",
                "question": "Should degraded runtime block by policy?",
                "direction": "BUY_YES",
                "market_price": 0.40,
                "yes_price": 0.40,
                "no_price": 0.60,
                "model_probability": 0.70,
                "edge": 0.20,
                "confidence": 0.90,
            }

            with patch.object(bot.kelly, "calculate", return_value=5.0):
                result = bot._process_signal(signal)

            self.assertEqual(result["blocked_reason"], "reconciliation_state_degraded")
            self.assertIn("resting_orders_present", result["reconciliation_issues"])
            self.assertEqual(exchange.orders, [])

    def test_live_path_repeated_critical_failures_trigger_exchange_pause(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(
                tmpdir,
                trading={
                    "mode": "live",
                    "trading_enabled": True,
                    "live_safety": {"enabled": True, "max_consecutive_critical_failures": 2},
                },
            )
            bot.exchanges["kalshi"] = FailingExchange()
            signal = {
                "exchange": "kalshi",
                "market_id": "m-fail",
                "question": "Will repeated live failures pause new entries?",
                "direction": "BUY_YES",
                "market_price": 0.40,
                "yes_price": 0.40,
                "no_price": 0.60,
                "model_probability": 0.70,
                "edge": 0.20,
                "confidence": 0.90,
            }

            with patch.object(bot.kelly, "calculate", return_value=5.0):
                first = bot._process_signal(signal)
                second = bot._process_signal(signal)
                third = bot._process_signal(signal)

            self.assertEqual(first["blocked_reason"], "placement_failed")
            self.assertEqual(second["blocked_reason"], "placement_failed")
            self.assertIn("decision_artifact", first)
            self.assertIn("decision_artifact", second)
            self.assertEqual(bot.live_failure_streaks["kalshi"]["count"], 2)
            self.assertEqual(bot.reconciliation_gate["kalshi"]["verdict"], "blocked")
            self.assertIn("repeated_live_failures_threshold_reached", bot.reconciliation_gate["kalshi"]["issues"])
            self.assertEqual(bot.reconciliation_gate["kalshi"]["recovery_state"], "requires_safe_reconciliation")
            self.assertEqual(third["blocked_reason"], "reconciliation_state_blocked")

    def test_repeated_reconciliation_mismatches_trigger_safety_pause(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(
                tmpdir,
                trading={
                    "mode": "live",
                    "trading_enabled": True,
                    "live_safety": {"enabled": True, "max_consecutive_reconciliation_mismatches": 2},
                },
            )

            bot._apply_reconciliation_runtime_state(
                "kalshi",
                "degraded",
                ["local_order_status_corrected_from_exchange"],
                source="pre_trade_reconciliation",
            )
            bot._apply_reconciliation_runtime_state(
                "kalshi",
                "degraded",
                ["local_order_status_corrected_from_exchange"],
                source="pre_trade_reconciliation",
            )

            self.assertEqual(bot.reconciliation_gate["kalshi"]["verdict"], "blocked")
            self.assertIn("repeated_reconciliation_mismatches_threshold_reached", bot.reconciliation_gate["kalshi"]["issues"])
            self.assertEqual(bot.live_runtime_state["state"], "blocked")
            self.assertEqual(bot.live_runtime_state["reason"], "repeated_reconciliation_mismatches_threshold_reached")
            snapshot = bot.build_status_snapshot(reason="manual")
            self.assertTrue(snapshot.extra["safety_pause"]["active"])
            self.assertEqual(snapshot.extra["safety_pause"]["recovery_hint"], "safe_reconciliation_clears_pause")

    def test_live_path_respects_trading_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir, trading_enabled=False, trading={"mode": "live", "trading_enabled": False})
            bot.risk.state.trading_enabled = False
            signal = {
                "exchange": "kalshi",
                "market_id": "m2",
                "question": "Will snow happen?",
                "direction": "BUY_YES",
                "market_price": 0.35,
                "yes_price": 0.35,
                "no_price": 0.65,
                "model_probability": 0.70,
                "edge": 0.20,
                "confidence": 0.90,
            }

            with patch.object(bot.kelly, "calculate", return_value=5.0):
                result = bot._process_signal(signal)

            self.assertEqual(result["blocked_reason"], "trading_disabled")
            self.assertEqual(len(bot.exchanges["kalshi"].orders), 0)

    def test_status_snapshot_marks_startup_reconciliation_pending_before_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)

            snapshot = bot.build_status_snapshot(reason="pre-startup", scan_num=0)

            self.assertIn("startup_reconciliation", snapshot.extra)
            self.assertEqual(snapshot.extra["startup_reconciliation"]["kalshi"]["status"], "pending")
            self.assertFalse(snapshot.extra["startup_reconciliation"]["kalshi"]["completed"])
            self.assertIn("startup_reconciliation_not_run", snapshot.extra["startup_reconciliation"]["kalshi"]["reconciliation_issues"])

    def test_safe_reconciliation_does_not_clear_manual_review_pause(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.risk.state.available_cash = -0.25
            bot._enforce_live_runtime_invariants("kalshi", source="pre_signal")

            bot.risk.state.available_cash = 25.0
            bot._apply_reconciliation_runtime_state("kalshi", "safe", [], source="pre_trade_reconciliation")

            self.assertEqual(bot.reconciliation_gate["kalshi"]["verdict"], "blocked")
            self.assertEqual(bot.reconciliation_gate["kalshi"]["recovery_state"], "manual_review_required")
            self.assertEqual(bot.live_runtime_state["state"], "blocked")

    def test_live_path_surfaces_runtime_invariant_from_pre_trade_refresh(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.startup_reconciliation_status["kalshi"] = {
                "completed": True,
                "source": "test",
                "status": "safe",
                "runtime_state": "safe",
                "reconciliation_verdict": "safe",
                "reconciliation_issues": [],
                "updated_at": "",
            }
            bot.trade_history = [
                {
                    "trade_id": "bad-cancel-1",
                    "order_id": "bad-cancel-1",
                    "market_id": "m-bad-cancel",
                    "direction": "BUY_YES",
                    "status": "canceled",
                    "lifecycle_state": "canceled_partial",
                    "requested_size": 2.0,
                    "approved_size": 2.0,
                    "placed_size": 2.0,
                    "filled_size": 1.0,
                    "remaining_size": 1.0,
                    "reserved_capital": 1.0,
                }
            ]
            signal = {
                "exchange": "kalshi",
                "market_id": "m-runtime-refresh",
                "question": "Should pre-trade invariants surface clearly?",
                "direction": "BUY_YES",
                "market_price": 0.40,
                "yes_price": 0.40,
                "no_price": 0.60,
                "model_probability": 0.70,
                "edge": 0.20,
                "confidence": 0.90,
            }

            with patch.object(bot.kelly, "calculate", return_value=5.0):
                result = bot._process_signal(signal)

            self.assertEqual(result["blocked_reason"], "runtime_invariant_violation")
            self.assertIn("runtime_invariant_violation", result["reconciliation_issues"])
            self.assertEqual(result.get("recovery_state"), "manual_review_required")
            self.assertEqual(bot.reconciliation_gate["kalshi"]["reason"], "runtime_invariant_violation")
            self.assertEqual(bot.live_failure_streaks["kalshi"]["count"], 1)

    def test_runtime_invariant_threshold_one_keeps_root_cause_reason(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(
                tmpdir,
                trading={
                    "mode": "live",
                    "trading_enabled": True,
                    "live_safety": {"enabled": True, "max_consecutive_critical_failures": 1},
                },
            )
            bot.startup_reconciliation_status["kalshi"] = {
                "completed": True,
                "source": "test",
                "status": "safe",
                "runtime_state": "safe",
                "reconciliation_verdict": "safe",
                "reconciliation_issues": [],
                "updated_at": "",
            }
            bot.risk.state.available_cash = -0.25
            signal = {
                "exchange": "kalshi",
                "market_id": "m-threshold-one",
                "question": "Should runtime invariant preserve root cause at threshold one?",
                "direction": "BUY_YES",
                "market_price": 0.40,
                "yes_price": 0.40,
                "no_price": 0.60,
                "model_probability": 0.70,
                "edge": 0.20,
                "confidence": 0.90,
            }

            with patch.object(bot.kelly, "calculate", return_value=5.0):
                result = bot._process_signal(signal)

            self.assertEqual(result["blocked_reason"], "runtime_invariant_violation")
            self.assertEqual(bot.reconciliation_gate["kalshi"]["reason"], "runtime_invariant_violation")
            self.assertIn("repeated_live_failures_threshold_reached", bot.reconciliation_gate["kalshi"]["issues"])
            self.assertEqual(bot.live_runtime_state["recovery_state"], "manual_review_required")

    def test_build_status_snapshot_uses_shared_shape(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir, parity_mode={"enabled": True, "comparison_mode": "identical_risk"})
            bot.trade_history = [
                {
                    "resolved": False,
                    "status": "filled",
                    "lifecycle_state": "filled_open",
                    "trade_id": "live-1",
                    "market_id": "m-1",
                    "direction": "BUY_YES",
                    "requested_size": 1.0,
                    "approved_size": 1.0,
                    "placed_size": 1.0,
                    "filled_size": 1.0,
                    "remaining_size": 0.0,
                    "reserved_capital": 1.0,
                    "parity_mode_enabled": True,
                    "execution_revalidated": True,
                    "execution_revalidation_outcome": "approved",
                    "execution_snapshot_source": "book",
                    "execution_decision_reason_code": "approved",
                    "execution_snapshot": {"market_price": 0.41, "source": "book"},
                },
                {
                    "resolved": "False",
                    "status": "rejected",
                    "lifecycle_state": "revalidation_rejected",
                    "trade_id": "live-2",
                    "market_id": "m-2",
                    "direction": "BUY_YES",
                    "requested_size": 1.0,
                    "approved_size": 1.0,
                    "placed_size": 0.0,
                    "filled_size": 0.0,
                    "remaining_size": 0.0,
                    "reserved_capital": 0.0,
                    "parity_mode_enabled": True,
                    "execution_revalidated": True,
                    "execution_revalidation_outcome": "rejected",
                    "execution_snapshot_source": "fallback",
                    "execution_decision_reason_code": "price_above_threshold",
                    "execution_snapshot": {"market_price": 0.47, "source": "fallback"},
                },
                {
                    "resolved": True,
                    "status": "resolved",
                    "lifecycle_state": "resolved_position",
                    "trade_id": "live-3",
                    "market_id": "m-3",
                    "direction": "BUY_YES",
                    "requested_size": 1.0,
                    "approved_size": 1.0,
                    "placed_size": 1.0,
                    "filled_size": 1.0,
                    "remaining_size": 0.0,
                    "reserved_capital": 0.0,
                    "resolved_at": "2026-04-30T00:00:00+00:00",
                    "outcome": "YES",
                    "pnl": 0.5,
                    "settlement_value": 1.5,
                    "resolution_type": "settled",
                    "decision_reason_code": "approved",
                },
            ]
            bot.open_positions = [
                SimpleNamespace(size=4.0),
            ]
            snapshot = bot.build_status_snapshot(reason="manual status", scan_num=7)

            self.assertEqual(snapshot.scan_num, 7)
            self.assertEqual(snapshot.open_trades, 1)
            self.assertEqual(snapshot.resolved_trades, 1)
            self.assertEqual(snapshot.total_trades, 3)
            self.assertIn("source", snapshot.extra)
            self.assertEqual(snapshot.extra["mode_label"], "identical-risk comparison")
            self.assertEqual(snapshot.extra["risk_preset_mode"], "paper")
            self.assertEqual(snapshot.extra["parity_comparison_mode"], "identical_risk")
            self.assertIn("live_failure_streaks", snapshot.extra)
            self.assertIn("reconciliation_gate", snapshot.extra)
            self.assertIn("live_runtime_state", snapshot.extra)
            self.assertEqual(snapshot.extra["live_runtime_state"]["state"], "safe")
            self.assertEqual(snapshot.extra["filled_event_exposure"], 4.0)
            self.assertEqual(snapshot.extra["pending_event_exposure"], 0.0)
            self.assertIn("normalized_trade_summary", snapshot.extra)
            self.assertEqual(snapshot.extra["normalized_trade_summary"]["total_rows"], 3)
            self.assertIn("parity_summary", snapshot.extra)
            self.assertTrue(snapshot.extra["parity_summary"]["parity_mode_enabled"])
            self.assertEqual(snapshot.extra["parity_summary"]["execution_revalidated_rows"], 2)
            self.assertEqual(snapshot.extra["parity_summary"]["execution_rejected_rows"], 1)
            self.assertEqual(snapshot.extra["parity_summary"]["fallback_rows"], 1)
            self.assertEqual(snapshot.extra["parity_summary"]["snapshot_source_counts"]["book"], 1)
            self.assertEqual(snapshot.extra["parity_summary"]["snapshot_source_counts"]["fallback"], 1)
            self.assertEqual(snapshot.extra["parity_summary"]["lifecycle_state_counts"]["filled_open"], 1)
            self.assertEqual(snapshot.extra["parity_summary"]["lifecycle_state_counts"]["revalidation_rejected"], 1)
            self.assertEqual(snapshot.extra["parity_summary"]["lifecycle_state_counts"]["resolved_position"], 1)
            self.assertEqual(snapshot.extra["normalized_trade_summary"]["execution_revalidation_outcome_counts"]["approved"], 1)
            self.assertEqual(snapshot.extra["normalized_trade_summary"]["execution_revalidation_outcome_counts"]["rejected"], 1)
            self.assertEqual(snapshot.extra["parity_summary"]["invalid_contract_rows"], 0)
            self.assertEqual(snapshot.extra["parity_summary"]["top_contract_issues"], [])


if __name__ == "__main__":
    unittest.main()
