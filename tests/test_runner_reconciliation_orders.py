import json
import tempfile
import unittest
from datetime import datetime, timezone

from bot.exchanges.base import Position, RestingOrder
from bot.runner import PredictionBot


class FakeExchangeWithOrders:
    def __init__(self, positions=None, orders=None, balance=25.0):
        self._positions = positions or []
        self._orders = orders or []
        self._balance = balance

    def connect(self):
        return True

    def get_positions(self):
        return list(self._positions)

    def get_resting_orders(self):
        return list(self._orders)

    def get_balance(self):
        return self._balance

    def close(self):
        return None


class RunnerReconciliationOrdersTests(unittest.TestCase):
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

    def test_reconciliation_includes_open_orders_and_partial_fills(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.exchanges["kalshi"] = FakeExchangeWithOrders(
                positions=[
                    Position(
                        market_id="POS-1",
                        exchange="kalshi",
                        question="Position one",
                        side="YES",
                        entry_price=0.40,
                        size=3.0,
                        current_price=0.44,
                        pnl=0.0,
                        opened_at=datetime(2026, 4, 20, 18, 0, tzinfo=timezone.utc),
                    )
                ],
                orders=[
                    RestingOrder(
                        order_id="ord-1",
                        market_id="ORD-1",
                        exchange="kalshi",
                        side="YES",
                        requested_size=5.0,
                        filled_size=2.0,
                        remaining_size=3.0,
                        price=0.41,
                        status="open",
                        created_at=datetime(2026, 4, 20, 18, 1, tzinfo=timezone.utc),
                    ),
                    RestingOrder(
                        order_id="ord-2",
                        market_id="ORD-2",
                        exchange="kalshi",
                        side="NO",
                        requested_size=4.0,
                        filled_size=0.0,
                        remaining_size=4.0,
                        price=0.62,
                        status="open",
                        created_at=datetime(2026, 4, 20, 18, 2, tzinfo=timezone.utc),
                    ),
                ],
                balance=20.0,
            )

            bot.connect_all()

            self.assertEqual(len(bot.open_positions), 1)
            self.assertEqual(len(bot.open_orders), 2)
            self.assertEqual(bot.open_orders[0]["order_id"], "ord-1")
            self.assertEqual(bot.open_orders[0]["filled_size"], 2.0)
            self.assertEqual(bot.open_orders[0]["remaining_size"], 3.0)
            self.assertEqual(bot.open_orders[1]["direction"], "BUY_NO")
            self.assertEqual(bot.risk.state.reserved_capital, 10.0)
            self.assertEqual(bot.risk.state.available_cash, 10.0)
            self.assertEqual(len(bot.trade_history), 3)
            reconciled_position = next(row for row in bot.trade_history if row["trade_id"].startswith("reconciled:kalshi:POS-1"))
            reconciled_partial = next(row for row in bot.trade_history if row["trade_id"] == "ord-1")
            reconciled_resting = next(row for row in bot.trade_history if row["trade_id"] == "ord-2")
            self.assertEqual(reconciled_position["status"], "filled")
            self.assertEqual(reconciled_partial["status"], "partial")
            self.assertEqual(reconciled_partial["lifecycle_state"], "partial_open")
            self.assertEqual(reconciled_partial["filled_size"], 2.0)
            self.assertEqual(reconciled_partial["remaining_size"], 3.0)
            self.assertEqual(reconciled_resting["status"], "placed")
            self.assertEqual(reconciled_resting["lifecycle_state"], "placed_open")

            with open(f"{tmpdir}/live/lifecycle.jsonl") as f:
                events = [json.loads(line) for line in f if line.strip()]
            reconcile_events = [e for e in events if e["event"] == "reconciliation_completed"]
            self.assertEqual(reconcile_events[0]["details"]["open_orders"], 2)
            self.assertEqual(reconcile_events[0]["details"]["partial_fills"], 1)


if __name__ == "__main__":
    unittest.main()
