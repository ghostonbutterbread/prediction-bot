import tempfile
import unittest
from datetime import datetime, timezone

from bot.exchanges.base import Market, Position, RestingOrder
from bot.live_adapters import RunnerLiveReconciliationAdapter, RunnerLiveStateAdapter
from bot.runner import PredictionBot, LivePosition


class FakeExchange:
    def __init__(self, positions=None, orders=None, balance=25.0, market_map=None):
        self._positions = positions or []
        self._orders = orders or []
        self._balance = balance
        self._market_map = market_map or {}

    def get_positions(self):
        return list(self._positions)

    def get_resting_orders(self):
        return list(self._orders)

    def get_balance(self):
        return self._balance

    def get_market(self, market_id):
        return self._market_map.get(market_id)


class LiveAdaptersTests(unittest.TestCase):
    def _make_bot(self, tmpdir):
        return PredictionBot(
            {
                "log_dir": tmpdir,
                "data_dir": tmpdir,
                "trading": {"mode": "live", "enabled": True},
                "strategy": {
                    "enable_news": False,
                    "enable_social": False,
                    "enable_ai": False,
                },
            }
        )

    def test_reconciliation_adapter_builds_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            adapter = RunnerLiveReconciliationAdapter(bot)
            exchange = FakeExchange(
                positions=[
                    Position(
                        market_id="M1",
                        exchange="kalshi",
                        question="Will it rain?",
                        side="YES",
                        entry_price=0.40,
                        size=2.0,
                        current_price=0.44,
                        pnl=0.0,
                        opened_at=datetime(2026, 4, 20, 18, 0, tzinfo=timezone.utc),
                    )
                ],
                orders=[
                    RestingOrder(
                        order_id="ord-1",
                        market_id="M2",
                        exchange="kalshi",
                        side="NO",
                        requested_size=4.0,
                        filled_size=1.0,
                        remaining_size=3.0,
                        price=0.61,
                        status="open",
                        created_at=datetime(2026, 4, 20, 18, 1, tzinfo=timezone.utc),
                    )
                ],
                balance=15.0,
            )

            snapshot = adapter.reconcile("kalshi", exchange)
            self.assertEqual(len(snapshot.open_positions), 1)
            self.assertEqual(len(snapshot.open_orders), 1)
            self.assertEqual(snapshot.reserved_capital, 5.0)
            self.assertEqual(snapshot.available_cash, 10.0)
            self.assertEqual(snapshot.partial_fills, 1)
            self.assertEqual(snapshot.verdict, "degraded")
            self.assertIn("partial_fill_exposure_present", snapshot.issues)
            self.assertIn("resting_orders_present", snapshot.issues)

    def test_reconciliation_normalizes_submitted_and_resting_orders_to_placed_open(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            adapter = RunnerLiveReconciliationAdapter(bot)
            exchange = FakeExchange(
                orders=[
                    RestingOrder(
                        order_id="ord-submitted",
                        market_id="M-submitted",
                        exchange="kalshi",
                        side="YES",
                        requested_size=4.0,
                        filled_size=0.0,
                        remaining_size=4.0,
                        price=0.45,
                        status="submitted",
                        created_at=datetime(2026, 4, 20, 18, 2, tzinfo=timezone.utc),
                    ),
                    RestingOrder(
                        order_id="ord-resting",
                        market_id="M-resting",
                        exchange="kalshi",
                        side="NO",
                        requested_size=3.0,
                        filled_size=0.0,
                        remaining_size=3.0,
                        price=0.58,
                        status="resting",
                        created_at=datetime(2026, 4, 20, 18, 2, tzinfo=timezone.utc),
                    ),
                ],
                balance=25.0,
            )

            snapshot = adapter.reconcile("kalshi", exchange)
            self.assertEqual(len(snapshot.open_orders), 2)
            submitted = next(row for row in snapshot.trade_history_rows if row["trade_id"] == "ord-submitted")
            resting = next(row for row in snapshot.trade_history_rows if row["trade_id"] == "ord-resting")
            self.assertEqual(submitted["status"], "placed")
            self.assertEqual(submitted["lifecycle_state"], "placed_open")
            self.assertEqual(resting["status"], "placed")
            self.assertEqual(resting["lifecycle_state"], "placed_open")

    def test_reconciliation_keeps_closed_order_outcomes_in_history_but_not_open_orders(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            adapter = RunnerLiveReconciliationAdapter(bot)
            exchange = FakeExchange(
                orders=[
                    RestingOrder(
                        order_id="ord-cancel-partial",
                        market_id="M3",
                        exchange="kalshi",
                        side="YES",
                        requested_size=4.0,
                        filled_size=1.5,
                        remaining_size=0.0,
                        price=0.45,
                        status="cancelled",
                        created_at=datetime(2026, 4, 20, 18, 3, tzinfo=timezone.utc),
                    ),
                    RestingOrder(
                        order_id="ord-expired",
                        market_id="M4",
                        exchange="kalshi",
                        side="NO",
                        requested_size=2.0,
                        filled_size=0.0,
                        remaining_size=0.0,
                        price=0.58,
                        status="expired",
                        created_at=datetime(2026, 4, 20, 18, 4, tzinfo=timezone.utc),
                    )
                ],
                balance=25.0,
            )

            snapshot = adapter.reconcile("kalshi", exchange)
            self.assertEqual(snapshot.open_orders, [])
            self.assertEqual(snapshot.reserved_capital, 0.0)
            self.assertEqual(snapshot.available_cash, 25.0)

            partial_cancel = next(row for row in snapshot.trade_history_rows if row["trade_id"] == "ord-cancel-partial")
            expired = next(row for row in snapshot.trade_history_rows if row["trade_id"] == "ord-expired")
            self.assertEqual(partial_cancel["status"], "canceled")
            self.assertEqual(partial_cancel["lifecycle_state"], "canceled_partial")
            self.assertEqual(expired["status"], "stale")
            self.assertEqual(expired["lifecycle_state"], "stale_open_order")

    def test_reconciliation_marks_negative_effective_cash_as_blocked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            adapter = RunnerLiveReconciliationAdapter(bot)
            exchange = FakeExchange(
                positions=[
                    Position(
                        market_id="M1",
                        exchange="kalshi",
                        question="Will it rain?",
                        side="YES",
                        entry_price=0.40,
                        size=4.0,
                        current_price=0.44,
                        pnl=0.0,
                        opened_at=datetime(2026, 4, 20, 18, 0, tzinfo=timezone.utc),
                    )
                ],
                orders=[
                    RestingOrder(
                        order_id="ord-1",
                        market_id="M2",
                        exchange="kalshi",
                        side="NO",
                        requested_size=4.0,
                        filled_size=0.0,
                        remaining_size=4.0,
                        price=0.61,
                        status="open",
                        created_at=datetime(2026, 4, 20, 18, 1, tzinfo=timezone.utc),
                    )
                ],
                balance=5.0,
            )

            snapshot = adapter.reconcile("kalshi", exchange)
            self.assertEqual(snapshot.verdict, "blocked")
            self.assertIn("negative_available_cash_after_reconcile", snapshot.issues)

    def test_reconciliation_blocks_ambiguous_local_exchange_duplicate_exposure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.open_orders = [
                {
                    "order_id": "local-ord-1",
                    "market_id": "M-DUP",
                    "question": "Will duplicate state exist?",
                    "direction": "BUY_YES",
                    "status": "open",
                    "requested_size": 4.0,
                    "filled_size": 0.0,
                    "remaining_size": 4.0,
                    "price": 0.45,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ]
            adapter = RunnerLiveReconciliationAdapter(bot)
            exchange = FakeExchange(
                orders=[
                    RestingOrder(
                        order_id="exchange-ord-9",
                        market_id="M-DUP",
                        exchange="kalshi",
                        side="YES",
                        requested_size=4.0,
                        filled_size=0.0,
                        remaining_size=4.0,
                        price=0.45,
                        status="open",
                        created_at=datetime(2026, 4, 20, 18, 2, tzinfo=timezone.utc),
                    )
                ],
                balance=25.0,
            )

            snapshot = adapter.reconcile("kalshi", exchange)
            self.assertEqual(snapshot.verdict, "blocked")
            self.assertIn("ambiguous_local_exchange_duplicate_exposure", snapshot.issues)

    def test_reconciliation_blocks_ambiguous_duplicate_when_local_order_id_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.open_orders = [
                {
                    "order_id": "",
                    "market_id": "M-DUP-MISSING-ID",
                    "question": "Will duplicate state exist without a local id?",
                    "direction": "BUY_YES",
                    "status": "open",
                    "requested_size": 4.0,
                    "filled_size": 0.0,
                    "remaining_size": 4.0,
                    "price": 0.45,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ]
            adapter = RunnerLiveReconciliationAdapter(bot)
            exchange = FakeExchange(
                orders=[
                    RestingOrder(
                        order_id="exchange-ord-9",
                        market_id="M-DUP-MISSING-ID",
                        exchange="kalshi",
                        side="YES",
                        requested_size=4.0,
                        filled_size=0.0,
                        remaining_size=4.0,
                        price=0.45,
                        status="open",
                        created_at=datetime(2026, 4, 20, 18, 2, tzinfo=timezone.utc),
                    )
                ],
                balance=25.0,
            )

            snapshot = adapter.reconcile("kalshi", exchange)
            self.assertEqual(snapshot.verdict, "blocked")
            self.assertIn("ambiguous_local_exchange_duplicate_exposure", snapshot.issues)

    def test_state_adapter_exposes_positions_and_orders(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.open_positions = [
                LivePosition(
                    market_id="M1",
                    question="Will it rain?",
                    direction="BUY_YES",
                    price=0.4,
                    size=2.0,
                    order_id="ord-pos",
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
            ]
            bot.open_orders = [
                {
                    "order_id": "ord-open",
                    "market_id": "M2",
                    "question": "Will it snow?",
                    "direction": "BUY_NO",
                    "status": "open",
                    "requested_size": 4.0,
                    "filled_size": 1.0,
                    "remaining_size": 3.0,
                    "price": 0.61,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ]
            bot.risk.state.current_balance = 20.0

            adapter = RunnerLiveStateAdapter(bot)
            account = adapter.get_account_state()
            self.assertEqual(account.reserved_capital, 5.0)
            self.assertEqual(account.metadata["filled_event_exposure"], 2.0)
            self.assertEqual(account.metadata["pending_event_exposure"], 3.0)
            self.assertEqual(len(adapter.list_open_positions()), 1)
            self.assertEqual(len(adapter.list_resting_orders()), 1)

    def test_settle_emits_resolution_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            adapter = RunnerLiveReconciliationAdapter(bot)
            exchange = FakeExchange(
                market_map={
                    "M1": Market(
                        id="M1",
                        exchange="kalshi",
                        question="Will it rain?",
                        yes_price=0.4,
                        no_price=0.6,
                        volume=0,
                        liquidity=0,
                        closes_at=datetime.now(timezone.utc),
                        category="weather",
                        metadata={"result": "YES"},
                        close_price=1.0,
                    )
                }
            )
            open_positions = [
                LivePosition(
                    market_id="M1",
                    question="Will it rain?",
                    direction="BUY_YES",
                    price=0.4,
                    size=2.0,
                    order_id="ord-pos",
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
            ]

            events = adapter.settle("kalshi", exchange, open_positions)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].outcome, "YES")
            self.assertEqual(events[0].metadata["resolution_result"], "won")
            self.assertEqual(events[0].pnl, 1.2)


if __name__ == "__main__":
    unittest.main()
