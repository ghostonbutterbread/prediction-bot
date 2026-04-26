import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot.exchanges.base import Position, RestingOrder
from bot.live_execution import RunnerLiveExecutionAdapter
from bot.shared_core import build_execution_snapshot
from bot.runner import LivePosition, PredictionBot


class FakeExchange:
    def __init__(self, *, balance=25.0, positions=None, resting_orders=None, order_status="open"):
        self.orders = []
        self.balance = balance
        self.positions = positions or []
        self.resting_orders = resting_orders or []
        self.order_status = order_status

    def get_balance(self):
        return self.balance

    def get_positions(self):
        return list(self.positions)

    def get_resting_orders(self):
        return list(self.resting_orders)

    def get_market_bid_ask(self, market_id):
        return {"best_yes_ask": 0.40, "best_no_ask": 0.60, "best_yes_bid": 0.39, "best_no_bid": 0.59}

    def place_order(self, market_id, side, price, size):
        order = SimpleNamespace(id=f"ord-{len(self.orders)+1}", status=self.order_status)
        self.orders.append({"market_id": market_id, "side": side, "price": price, "size": size})
        return order



class LiveExecutionTests(unittest.TestCase):
    def test_build_execution_snapshot_uses_book_prices_and_side_specific_market_price(self):
        yes_snapshot = build_execution_snapshot(
            {"market_price": 0.40},
            direction="BUY_YES",
            bid_ask={"best_yes_ask": 0.41, "best_no_ask": 0.59, "best_yes_bid": 0.40, "best_no_bid": 0.58},
        )
        self.assertEqual(yes_snapshot["market_price"], 0.41)
        self.assertEqual(yes_snapshot["source"], "book")

        no_snapshot = build_execution_snapshot(
            {"market_price": 0.40},
            direction="BUY_NO",
            bid_ask={"best_yes_ask": 0.41, "best_no_ask": 0.59, "best_yes_bid": 0.40, "best_no_bid": 0.58},
        )
        self.assertEqual(no_snapshot["market_price"], 0.59)

    def _make_bot(self, tmpdir):
        bot = PredictionBot(
            {
                "log_dir": tmpdir,
                "data_dir": tmpdir,
                "trading": {"mode": "live", "enabled": True},
                "strategy": {
                    "min_edge": 0.05,
                    "min_confidence": 0.5,
                    "enable_news": False,
                    "enable_social": False,
                    "enable_ai": False,
                },
                "max_tradable_balance_usd": 10.0,
                "max_position_size_usd": 4.0,
            }
        )
        bot.risk.state.current_balance = 25.0
        bot.risk.state.available_cash = 25.0
        bot.risk.state.peak_balance = 25.0
        bot.risk.state.session_starting_balance = 25.0
        bot.risk.state.session_peak_balance = 25.0
        bot.risk.state.max_drawdown_halt = False
        return bot

    def test_build_trade_context_uses_live_account_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            adapter = RunnerLiveExecutionAdapter(bot)
            exchange = FakeExchange()
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

            context = adapter.build_trade_context(signal, exchange, bot.config)
            self.assertEqual(context.account_state.current_balance, 25.0)
            self.assertEqual(context.account_state.metadata["effective_tradable_cash"], 10.0)

    def test_build_trade_context_counts_pending_same_event_exposure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.open_positions = [
                LivePosition(
                    market_id="KXHIGHNY-26APR16-T70",
                    question="Will NYC high be below 70?",
                    direction="BUY_YES",
                    price=0.40,
                    size=2.0,
                    order_id="pos-1",
                    created_at="2026-04-20T00:00:00+00:00",
                    event_key="KXHIGHNY-26APR16",
                )
            ]
            bot.open_orders = [
                {
                    "order_id": "ord-open",
                    "market_id": "KXHIGHNY-26APR16-T71",
                    "question": "Will NYC high be below 71?",
                    "direction": "BUY_YES",
                    "remaining_size": 3.0,
                    "price": 0.42,
                    "event_key": "KXHIGHNY-26APR16",
                }
            ]
            adapter = RunnerLiveExecutionAdapter(bot)
            exchange = FakeExchange()
            signal = {
                "exchange": "kalshi",
                "market_id": "KXHIGHNY-26APR16-T72",
                "question": "Will NYC high be below 72?",
                "direction": "BUY_YES",
                "market_price": 0.43,
                "yes_price": 0.43,
                "no_price": 0.57,
                "model_probability": 0.70,
                "edge": 0.30,
                "confidence": 0.90,
            }

            context = adapter.build_trade_context(signal, exchange, bot.config)
            snapshot = context.metadata["event_snapshot"]
            self.assertEqual(snapshot["event_position_count_before"], 2)
            self.assertEqual(snapshot["event_exposure_before"], 5.0)
            self.assertEqual(snapshot["filled_event_exposure_before"], 2.0)
            self.assertEqual(snapshot["pending_event_exposure_before"], 3.0)
            self.assertIn("KXHIGHNY-26APR16-T71", snapshot["held_market_ids"])

    def test_execute_submitted_order_maps_to_canonical_placed_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            adapter = RunnerLiveExecutionAdapter(bot)
            exchange = FakeExchange(order_status="submitted")
            signal = {
                "exchange": "kalshi",
                "market_id": "m-submitted",
                "question": "Will a submitted order stay canonical?",
                "direction": "BUY_YES",
                "market_price": 0.40,
                "yes_price": 0.40,
                "no_price": 0.60,
                "model_probability": 0.70,
                "edge": 0.30,
                "confidence": 0.90,
            }
            decision = SimpleNamespace(
                action="BUY_YES",
                position_size=2.5,
                entry_price=0.40,
                reason="ok",
                reason_code="ok",
                requested_position_size=2.5,
                reasoning={},
            )

            result = adapter.execute(signal, decision, exchange)

            self.assertIsNotNone(result)
            trade_row = bot.trade_history[0]
            self.assertEqual(trade_row["status"], "placed")
            self.assertEqual(trade_row["lifecycle_state"], "placed_open")
            self.assertEqual(trade_row["filled_size"], 0.0)
            self.assertEqual(trade_row["remaining_size"], 1.0)

    def test_execute_places_order_as_resting_until_fill_is_known(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            adapter = RunnerLiveExecutionAdapter(bot)
            exchange = FakeExchange()
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
            decision = SimpleNamespace(
                action="BUY_YES",
                position_size=2.5,
                entry_price=0.40,
                reason="ok",
                reason_code="ok",
                requested_position_size=2.5,
                reasoning={},
            )

            result = adapter.execute(signal, decision, exchange)
            self.assertIsNotNone(result)
            self.assertEqual(len(bot.open_positions), 0)
            self.assertEqual(len(bot.open_orders), 1)
            self.assertEqual(len(bot.trade_history), 1)
            self.assertEqual(exchange.orders[0]["size"], 1.0)
            self.assertIn("refresh", result)
            self.assertEqual(result["refresh"]["balance"], 25.0)
            self.assertTrue(result["refresh"]["pre_trade_refresh"]["pre_trade_refresh"])
            self.assertEqual(result["refresh"]["pre_trade_refresh"]["open_orders"], 0)
            self.assertEqual(result["refresh"]["pre_trade_refresh"]["reconciliation_verdict"], "safe")
            self.assertEqual(result["refresh"]["pre_trade_refresh"]["reconciliation_issues"], [])
            trade_row = bot.trade_history[0]
            self.assertEqual(trade_row["decision_reason_code"], "approved")
            self.assertEqual(trade_row["status"], "placed")
            self.assertEqual(trade_row["lifecycle_state"], "placed_open")
            self.assertEqual(trade_row["requested_size"], 1.0)
            self.assertEqual(trade_row["approved_size"], 1.0)
            self.assertEqual(trade_row["placed_size"], 1.0)
            self.assertEqual(trade_row["filled_size"], 0.0)
            self.assertEqual(trade_row["remaining_size"], 1.0)
            self.assertIsNone(trade_row["fill_price"])
            self.assertEqual(bot.open_orders[0]["remaining_size"], 1.0)
            self.assertEqual(trade_row["reserved_capital"], 1.0)
            self.assertFalse(trade_row["parity_mode_enabled"])
            self.assertTrue(trade_row["execution_revalidated"])
            self.assertEqual(trade_row["execution_revalidation_outcome"], "approved")
            logged_row = json.loads((Path(tmpdir) / "live" / "trades.jsonl").read_text().strip())
            self.assertEqual(logged_row["trade_id"], trade_row["trade_id"])
            self.assertEqual(logged_row["status"], "placed")
            self.assertEqual(logged_row["remaining_size"], 1.0)

    def test_live_context_and_execution_snapshot_match_under_identical_prices(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            adapter = RunnerLiveExecutionAdapter(bot)
            exchange = FakeExchange()
            signal = {
                "exchange": "kalshi",
                "market_id": "m-parity",
                "question": "Will parity hold?",
                "direction": "BUY_YES",
                "market_price": 0.40,
                "yes_price": 0.40,
                "no_price": 0.60,
                "model_probability": 0.70,
                "edge": 0.30,
                "confidence": 0.90,
            }

            snapshot = build_execution_snapshot(
                signal,
                direction="BUY_YES",
                bid_ask=exchange.get_market_bid_ask("m-parity"),
            )
            context = adapter.build_trade_context({**signal, **snapshot}, exchange, bot.config)

            self.assertEqual(context.market_price, snapshot["market_price"])
            self.assertEqual(context.yes_price, snapshot["yes_price"])
            self.assertEqual(context.metadata["event_snapshot"]["execution_snapshot_source"], "fallback")

    def test_execute_revalidates_against_live_ask_before_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.config["max_entry_price"] = 0.70
            adapter = RunnerLiveExecutionAdapter(bot)
            exchange = FakeExchange()
            exchange.get_market_bid_ask = lambda market_id: {"best_yes_ask": 0.75, "best_no_ask": 0.25, "best_yes_bid": 0.74, "best_no_bid": 0.24}
            signal = {
                "exchange": "kalshi",
                "market_id": "m1",
                "question": "Will rain happen?",
                "direction": "BUY_YES",
                "market_price": 0.40,
                "yes_price": 0.40,
                "no_price": 0.60,
                "model_probability": 0.90,
                "edge": 0.30,
                "confidence": 0.90,
            }
            decision = SimpleNamespace(
                action="BUY_YES",
                approved=True,
                position_size=4.0,
                entry_price=0.40,
                reason="ok",
                reason_code="approved",
                requested_position_size=4.0,
                reasoning={},
            )

            result = adapter.execute(signal, decision, exchange)

            self.assertIsNone(result)
            self.assertEqual(exchange.orders, [])
            self.assertEqual(len(bot.trade_history), 1)
            trade_row = bot.trade_history[0]
            self.assertEqual(trade_row["status"], "rejected")
            self.assertEqual(trade_row["failure_stage"], "revalidation")
            self.assertEqual(trade_row["decision_reason_code"], "entry_price_above_cap")
            self.assertTrue(trade_row["execution_revalidated"])
            self.assertEqual(trade_row["execution_revalidation_outcome"], "rejected")
            self.assertEqual(trade_row["original_decision_reason_code"], "approved")
            self.assertEqual(trade_row["execution_decision_reason_code"], "entry_price_above_cap")
            self.assertTrue(str(trade_row["trade_id"]).startswith("live-revalidation:"))
            self.assertEqual(trade_row["filled_size"], 0.0)
            self.assertEqual(trade_row["remaining_size"], 0.0)

    def test_execute_persists_live_trade_history_with_parity_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            adapter = RunnerLiveExecutionAdapter(bot)
            exchange = FakeExchange()
            signal = {
                "exchange": "kalshi",
                "market_id": "m-parity-history",
                "question": "Will parity metadata persist?",
                "direction": "BUY_YES",
                "market_price": 0.40,
                "yes_price": 0.40,
                "no_price": 0.60,
                "model_probability": 0.70,
                "edge": 0.30,
                "confidence": 0.90,
                "signals": {},
            }
            decision = SimpleNamespace(
                action="BUY_YES",
                approved=True,
                position_size=2.5,
                entry_price=0.40,
                win_probability=0.70,
                reason="ok",
                reason_code="approved",
                requested_position_size=2.5,
                reasoning={},
            )

            result = adapter.execute(signal, decision, exchange)

            self.assertIsNotNone(result)
            trade_row = bot.trade_history[0]
            self.assertFalse(trade_row["parity_mode_enabled"])
            self.assertTrue(trade_row["execution_revalidated"])
            self.assertEqual(trade_row["execution_revalidation_outcome"], "approved")
            self.assertEqual(trade_row["execution_snapshot_source"], "book")
            self.assertIsNotNone(trade_row["original_signal_snapshot"])
            self.assertEqual(trade_row["original_decision_reason_code"], "approved")
            self.assertEqual(trade_row["execution_decision_reason_code"], "approved")
            self.assertEqual(trade_row["execution_snapshot"]["source"], "book")
            self.assertEqual(trade_row["decision_reason_code"], "approved")

    def test_execute_partial_fill_keeps_combined_reserved_capital(self):
        class PartialFillExchange(FakeExchange):
            def place_order(self, market_id, side, price, size):
                order = SimpleNamespace(id="ord-partial", status="partial", filled_size=0.4, remaining_size=0.6)
                self.orders.append({"market_id": market_id, "side": side, "price": price, "size": size})
                return order

        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            adapter = RunnerLiveExecutionAdapter(bot)
            exchange = PartialFillExchange()
            signal = {
                "exchange": "kalshi",
                "market_id": "m-partial",
                "question": "Will partial fill happen?",
                "direction": "BUY_YES",
                "market_price": 0.40,
                "yes_price": 0.40,
                "no_price": 0.60,
                "model_probability": 0.70,
                "edge": 0.30,
                "confidence": 0.90,
            }
            decision = SimpleNamespace(
                action="BUY_YES",
                approved=True,
                position_size=2.5,
                entry_price=0.40,
                win_probability=0.70,
                reason="ok",
                reason_code="approved",
                requested_position_size=2.5,
                reasoning={},
            )

            result = adapter.execute(signal, decision, exchange)

            self.assertIsNotNone(result)
            trade_row = bot.trade_history[0]
            self.assertEqual(trade_row["status"], "partial")
            self.assertEqual(trade_row["lifecycle_state"], "partial_open")
            self.assertEqual(trade_row["filled_size"], 0.4)
            self.assertEqual(trade_row["remaining_size"], 0.6)
            self.assertEqual(trade_row["reserved_capital"], 1.0)
            self.assertEqual(len(bot.open_positions), 1)
            self.assertEqual(len(bot.open_orders), 1)

    def test_build_trade_context_threads_price_improvement_and_book_inputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.risk.require_price_improvement_for_same_market_family = True
            bot.risk.price_improvement_ticks = 0.03
            bot.open_positions = [
                LivePosition(
                    market_id="KXHIGHNY-26APR16-T70",
                    question="Will NYC high be below 70?",
                    direction="BUY_YES",
                    price=0.42,
                    size=2.0,
                    order_id="pos-1",
                    created_at="2026-04-20T00:00:00+00:00",
                    event_key="KXHIGHNY-26APR16",
                )
            ]
            adapter = RunnerLiveExecutionAdapter(bot)
            exchange = FakeExchange()
            signal = {
                "exchange": "kalshi",
                "market_id": "KXHIGHNY-26APR16-T72",
                "question": "Will NYC high be below 72?",
                "direction": "BUY_YES",
                "market_price": 0.39,
                "yes_price": 0.39,
                "no_price": 0.61,
                "best_yes_ask": 0.39,
                "best_no_ask": 0.61,
                "best_yes_bid": 0.38,
                "best_no_bid": 0.60,
                "liquidity": 50.0,
                "model_probability": 0.70,
                "edge": 0.30,
                "confidence": 0.90,
            }

            context = adapter.build_trade_context(signal, exchange, bot.config)
            snapshot = context.metadata["event_snapshot"]
            self.assertEqual(snapshot["best_same_family_entry_price"], 0.42)
            self.assertEqual(snapshot["best_yes_ask"], 0.39)
            self.assertEqual(snapshot["liquidity"], 50.0)
            self.assertTrue(context.metadata["retrade_policy"]["require_price_improvement_for_same_market_family"])

    def test_execute_refreshes_exchange_state_before_revalidating(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.risk.max_event_exposure_pct = 0.10
            adapter = RunnerLiveExecutionAdapter(bot)
            exchange = FakeExchange(
                resting_orders=[
                    RestingOrder(
                        order_id="ord-existing",
                        market_id="KXHIGHNY-26APR16-T71",
                        exchange="kalshi",
                        question="Will NYC high be below 71?",
                        side="YES",
                        requested_size=2.5,
                        filled_size=0.0,
                        remaining_size=2.5,
                        price=0.42,
                        status="open",
                        created_at=None,
                    )
                ]
            )
            signal = {
                "exchange": "kalshi",
                "market_id": "KXHIGHNY-26APR16-T72",
                "question": "Will NYC high be below 72?",
                "direction": "BUY_YES",
                "market_price": 0.40,
                "yes_price": 0.40,
                "no_price": 0.60,
                "model_probability": 0.90,
                "edge": 0.30,
                "confidence": 0.90,
            }
            decision = SimpleNamespace(
                action="BUY_YES",
                approved=True,
                position_size=4.0,
                entry_price=0.40,
                reason="ok",
                reason_code="approved",
                requested_position_size=4.0,
                reasoning={},
            )

            result = adapter.execute(signal, decision, exchange)

            self.assertIsNone(result)
            self.assertEqual(exchange.orders, [])
            self.assertEqual(len(bot.open_orders), 1)
            self.assertEqual(bot.open_orders[0]["order_id"], "ord-existing")
            self.assertEqual(len(bot.trade_history), 1)
            self.assertEqual(bot.trade_history[0]["status"], "rejected")
            self.assertIn(bot.trade_history[0]["decision_reason_code"], {"event_exposure_limit", "event_exposure_limit_reached", "entry_price_above_cap"})

    def test_execute_rechecks_same_event_exposure_before_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.risk.max_event_exposure_pct = 0.10
            adapter = RunnerLiveExecutionAdapter(bot)
            exchange = FakeExchange(
                resting_orders=[
                    RestingOrder(
                        order_id="ord-open",
                        market_id="KXHIGHNY-26APR16-T71",
                        exchange="kalshi",
                        question="Will NYC high be below 71?",
                        side="YES",
                        requested_size=2.5,
                        filled_size=0.0,
                        remaining_size=2.5,
                        price=0.42,
                        status="open",
                        created_at=None,
                    )
                ]
            )
            signal = {
                "exchange": "kalshi",
                "market_id": "KXHIGHNY-26APR16-T72",
                "question": "Will NYC high be below 72?",
                "direction": "BUY_YES",
                "market_price": 0.40,
                "yes_price": 0.40,
                "no_price": 0.60,
                "model_probability": 0.90,
                "edge": 0.30,
                "confidence": 0.90,
            }
            decision = SimpleNamespace(
                action="BUY_YES",
                approved=True,
                position_size=4.0,
                entry_price=0.40,
                reason="ok",
                reason_code="approved",
                requested_position_size=4.0,
                reasoning={},
            )

            result = adapter.execute(signal, decision, exchange)

            self.assertIsNone(result)
            self.assertEqual(exchange.orders, [])
            self.assertEqual(len(bot.trade_history), 1)
            trade_row = bot.trade_history[0]
            self.assertEqual(trade_row["status"], "rejected")
            self.assertEqual(trade_row["failure_stage"], "revalidation")
            self.assertIn(trade_row["decision_reason_code"], {"event_exposure_limit", "event_exposure_limit_reached", "entry_price_above_cap"})
            self.assertEqual(trade_row["reserved_capital"], 0.0)

    def test_execute_tracks_partial_fill_when_exchange_reports_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            adapter = RunnerLiveExecutionAdapter(bot)

            class PartialFillExchange(FakeExchange):
                def place_order(self, market_id, side, price, size):
                    self.orders.append({"market_id": market_id, "side": side, "price": price, "size": size})
                    return SimpleNamespace(id="ord-partial", status="partial_fill", filled_size=0.4, remaining_size=0.6)

            exchange = PartialFillExchange()
            signal = {
                "exchange": "kalshi",
                "market_id": "m-partial",
                "question": "Will partial fill be tracked?",
                "direction": "BUY_YES",
                "market_price": 0.40,
                "yes_price": 0.40,
                "no_price": 0.60,
                "model_probability": 0.70,
                "edge": 0.30,
                "confidence": 0.90,
                "signals": {},
            }
            decision = SimpleNamespace(
                action="BUY_YES",
                approved=True,
                position_size=2.5,
                entry_price=0.40,
                win_probability=0.70,
                reason="ok",
                reason_code="approved",
                requested_position_size=2.5,
                reasoning={},
            )

            result = adapter.execute(signal, decision, exchange)

            self.assertIsNotNone(result)
            self.assertEqual(len(bot.open_positions), 1)
            self.assertEqual(len(bot.open_orders), 1)
            self.assertEqual(bot.open_positions[0].size, 0.4)
            self.assertEqual(bot.open_orders[0]["remaining_size"], 0.6)
            trade_row = bot.trade_history[0]
            self.assertEqual(trade_row["status"], "partial")
            self.assertEqual(trade_row["lifecycle_state"], "partial_open")
            self.assertEqual(trade_row["filled_size"], 0.4)
            self.assertEqual(trade_row["remaining_size"], 0.6)
            self.assertEqual(trade_row["fill_price"], 0.4)
            self.assertEqual(trade_row["reserved_capital"], 1.0)

    def test_execute_logs_failed_order_placement_with_structured_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            adapter = RunnerLiveExecutionAdapter(bot)

            class NoFillExchange(FakeExchange):
                def place_order(self, market_id, side, price, size):
                    self.orders.append({"market_id": market_id, "side": side, "price": price, "size": size})
                    return None

            exchange = NoFillExchange()
            signal = {
                "exchange": "kalshi",
                "market_id": "m-failed-placement",
                "question": "Will placement fail?",
                "direction": "BUY_YES",
                "market_price": 0.40,
                "yes_price": 0.40,
                "no_price": 0.60,
                "model_probability": 0.70,
                "edge": 0.30,
                "confidence": 0.90,
                "signals": {},
            }
            decision = SimpleNamespace(
                action="BUY_YES",
                approved=True,
                position_size=2.5,
                entry_price=0.40,
                win_probability=0.70,
                reason="ok",
                reason_code="approved",
                requested_position_size=2.5,
                reasoning={},
            )

            result = adapter.execute(signal, decision, exchange)

            self.assertIsNone(result)
            self.assertEqual(len(bot.trade_history), 1)
            trade_row = bot.trade_history[0]
            self.assertEqual(trade_row["status"], "failed")
            self.assertEqual(trade_row["failure_stage"], "placement")
            self.assertEqual(trade_row["decision_reason_code"], "approved")
            self.assertEqual(trade_row["approved_size"], 1.0)
            self.assertEqual(trade_row["placed_size"], 0.0)
            self.assertEqual(trade_row["filled_size"], 0.0)
            self.assertEqual(trade_row["remaining_size"], 0.0)
            self.assertEqual(trade_row["message"], "Exchange did not return an order")


if __name__ == "__main__":
    unittest.main()
