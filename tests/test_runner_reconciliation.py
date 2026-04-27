import json
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from bot.exchanges.base import Position, RestingOrder
from bot.runner import PredictionBot


class FakeReconExchange:
    def __init__(self, positions=None, orders=None, balance=25.0):
        self._positions = positions or []
        self._orders = orders or []
        self._balance = balance
        self.connected = False

    def connect(self):
        self.connected = True
        return True

    def get_positions(self):
        return list(self._positions)

    def get_resting_orders(self):
        return list(self._orders)

    def get_balance(self):
        return self._balance

    def close(self):
        return None


class RunnerReconciliationTests(unittest.TestCase):
    def _make_bot(self, tmpdir):
        config = {
            "log_dir": tmpdir,
            "data_dir": tmpdir,
            "trading": {"mode": "live", "enabled": True},
            "strategy": {
                "enable_news": False,
                "enable_social": False,
                "enable_ai": False,
            },
        }
        return PredictionBot(config)

    def test_connect_all_reconciles_open_positions_into_runner_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.exchanges["kalshi"] = FakeReconExchange(
                positions=[
                    Position(
                        market_id="KXRAIN-1",
                        exchange="kalshi",
                        question="Will it rain?",
                        side="YES",
                        entry_price=0.41,
                        size=3.0,
                        current_price=0.45,
                        pnl=0.0,
                        opened_at=datetime(2026, 4, 20, 17, 0, tzinfo=timezone.utc),
                    ),
                    Position(
                        market_id="KXSNOW-1",
                        exchange="kalshi",
                        question="Will it snow?",
                        side="NO",
                        entry_price=0.62,
                        size=2.0,
                        current_price=0.58,
                        pnl=0.0,
                        opened_at=datetime(2026, 4, 20, 17, 5, tzinfo=timezone.utc),
                    ),
                ],
                balance=25.0,
            )

            result = bot.connect_all()

            self.assertTrue(result["kalshi"])
            self.assertEqual(len(bot.open_positions), 2)
            self.assertEqual(bot.open_positions[0].market_id, "KXRAIN-1")
            self.assertEqual(bot.open_positions[1].direction, "BUY_NO")
            self.assertEqual(bot.risk.state.open_positions, 2)
            self.assertEqual(bot.risk.state.reserved_capital, 5.0)
            self.assertEqual(bot.risk.state.available_cash, 20.0)
            self.assertEqual(len(bot.trade_history), 2)
            self.assertTrue(all(t.get("reconciled") for t in bot.trade_history))

            with open(f"{tmpdir}/live/lifecycle.jsonl") as f:
                events = [json.loads(line) for line in f if line.strip()]

            reconcile_events = [e for e in events if e["event"] == "reconciliation_completed"]
            self.assertEqual(len(reconcile_events), 1)
            self.assertEqual(reconcile_events[0]["details"]["open_positions"], 2)

    def test_connect_all_sets_startup_gate_when_reconciliation_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.exchanges["kalshi"] = FakeReconExchange(
                positions=[
                    Position(
                        market_id="KXBLOCK-1",
                        exchange="kalshi",
                        question="Blocked startup",
                        side="YES",
                        entry_price=0.41,
                        size=6.0,
                        current_price=0.45,
                        pnl=0.0,
                        opened_at=datetime(2026, 4, 20, 17, 0, tzinfo=timezone.utc),
                    )
                ],
                balance=2.0,
            )

            result = bot.connect_all()

            self.assertTrue(result["kalshi"])
            self.assertEqual(bot.reconciliation_gate["kalshi"]["verdict"], "blocked")
            self.assertIn("negative_available_cash_after_reconcile", bot.reconciliation_gate["kalshi"]["issues"])

    def test_connect_all_blocks_ambiguous_duplicate_restart_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.open_orders = [
                {
                    "order_id": "local-ord-1",
                    "market_id": "KXDUP-1",
                    "question": "Duplicate restart state",
                    "direction": "BUY_YES",
                    "status": "open",
                    "requested_size": 3.0,
                    "filled_size": 0.0,
                    "remaining_size": 3.0,
                    "price": 0.41,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ]
            bot.exchanges["kalshi"] = FakeReconExchange(
                orders=[
                    RestingOrder(
                        order_id="exchange-ord-2",
                        market_id="KXDUP-1",
                        exchange="kalshi",
                        side="YES",
                        requested_size=3.0,
                        filled_size=0.0,
                        remaining_size=3.0,
                        price=0.41,
                        status="open",
                        created_at=datetime(2026, 4, 20, 17, 0, tzinfo=timezone.utc),
                    )
                ],
                balance=25.0,
            )

            result = bot.connect_all()

            self.assertTrue(result["kalshi"])
            self.assertEqual(bot.reconciliation_gate["kalshi"]["verdict"], "blocked")
            self.assertIn("ambiguous_local_exchange_duplicate_exposure", bot.reconciliation_gate["kalshi"]["issues"])

    def test_reconciliation_replaces_previous_reconciled_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.trade_history = [
                {"market_id": "manual-1", "reconciled": False},
                {"market_id": "old-reconciled", "reconciled": True},
            ]
            bot.exchanges["kalshi"] = FakeReconExchange(
                positions=[
                    Position(
                        market_id="KXNEW-1",
                        exchange="kalshi",
                        question="New position",
                        side="YES",
                        entry_price=0.33,
                        size=1.5,
                        current_price=0.35,
                        pnl=0.0,
                        opened_at=datetime.now(timezone.utc),
                    )
                ],
                balance=10.0,
            )

            bot.connect_all()

            self.assertEqual(len(bot.trade_history), 2)
            self.assertEqual(bot.trade_history[0]["market_id"], "manual-1")
            self.assertEqual(bot.trade_history[1]["market_id"], "KXNEW-1")
            self.assertTrue(bot.trade_history[1]["reconciled"])


if __name__ == "__main__":
    unittest.main()
