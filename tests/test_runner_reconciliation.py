import json
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from bot.exchanges.base import Market, Position, RestingOrder
from bot.runner import PredictionBot


class FakeReconExchange:
    def __init__(self, positions=None, orders=None, balance=25.0, market_map=None, connect_result=True):
        self._positions = positions or []
        self._orders = orders or []
        self._balance = balance
        self._market_map = market_map or {}
        self._connect_result = connect_result
        self.connected = False
        self.placed_orders = []

    def connect(self):
        self.connected = True
        return self._connect_result

    def get_positions(self):
        return list(self._positions)

    def get_resting_orders(self):
        return list(self._orders)

    def get_balance(self):
        return self._balance

    def get_market(self, market_id):
        return self._market_map.get(market_id)

    def get_market_bid_ask(self, market_id):
        return {"best_yes_ask": 0.40, "best_no_ask": 0.60}

    def place_order(self, market_id, side, price, size):
        order = SimpleNamespace(id=f"placed-{len(self.placed_orders) + 1}", status="open", filled_size=0.0, remaining_size=size)
        self.placed_orders.append({"market_id": market_id, "side": side, "price": price, "size": size})
        return order

    def close(self):
        return None


class FailingReconExchange(FakeReconExchange):
    def get_positions(self):
        raise RuntimeError("recovery unavailable")


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
            self.assertEqual(reconcile_events[0]["details"]["reconciliation_verdict"], "safe")
            self.assertEqual(bot.live_runtime_state["state"], "safe")

    def test_connect_all_blocks_when_startup_reconciliation_fails_even_without_live_safety(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = PredictionBot(
                {
                    "log_dir": tmpdir,
                    "data_dir": tmpdir,
                    "trading": {"mode": "live", "enabled": True, "live_safety": {"enabled": False}},
                    "strategy": {
                        "enable_news": False,
                        "enable_social": False,
                        "enable_ai": False,
                    },
                }
            )
            bot.exchanges["kalshi"] = FailingReconExchange(balance=25.0)

            result = bot.connect_all()

            self.assertTrue(result["kalshi"])
            self.assertEqual(bot.reconciliation_gate["kalshi"]["verdict"], "blocked")
            self.assertIn("reconciliation_refresh_failed", bot.reconciliation_gate["kalshi"]["issues"])
            self.assertEqual(bot.live_runtime_state["state"], "blocked")

    def test_connect_all_blocks_when_connect_succeeds_false_without_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.exchanges["kalshi"] = FakeReconExchange(connect_result=False)

            result = bot.connect_all()

            self.assertFalse(result["kalshi"])
            self.assertEqual(bot.reconciliation_gate["kalshi"]["verdict"], "blocked")
            self.assertIn("startup_reconciliation_not_run", bot.reconciliation_gate["kalshi"]["issues"])
            self.assertEqual(bot.live_runtime_state["state"], "blocked")

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
            self.assertEqual(bot.live_runtime_state["state"], "blocked")

    def test_connect_all_surfaces_degraded_reconciliation_runtime_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.exchanges["kalshi"] = FakeReconExchange(
                orders=[
                    RestingOrder(
                        order_id="ord-resting",
                        market_id="KXREST-1",
                        exchange="kalshi",
                        side="YES",
                        requested_size=3.0,
                        filled_size=1.0,
                        remaining_size=2.0,
                        price=0.41,
                        status="partial",
                        created_at=datetime(2026, 4, 20, 17, 0, tzinfo=timezone.utc),
                    )
                ],
                balance=25.0,
            )

            result = bot.connect_all()

            self.assertTrue(result["kalshi"])
            self.assertNotIn("kalshi", bot.reconciliation_gate)
            self.assertEqual(bot.live_runtime_state["state"], "degraded")
            self.assertEqual(bot.live_runtime_state["exchange_states"]["kalshi"]["state"], "degraded")
            self.assertIn("partial_fill_exposure_present", bot.live_runtime_state["issues"])
            self.assertIn("resting_orders_present", bot.live_runtime_state["issues"])

            with open(f"{tmpdir}/live/lifecycle.jsonl") as f:
                events = [json.loads(line) for line in f if line.strip()]

            reconcile_events = [e for e in events if e["event"] == "reconciliation_completed"]
            self.assertEqual(reconcile_events[0]["details"]["status"], "degraded")
            self.assertEqual(reconcile_events[0]["details"]["runtime_state"], "degraded")

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

    def test_blocked_ambiguous_startup_state_prevents_duplicate_live_entry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            exchange = FakeReconExchange(
                orders=[
                    RestingOrder(
                        order_id="exchange-ord-2",
                        market_id="KXDUP-ENTRY",
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
            bot.open_orders = [
                {
                    "order_id": "local-ord-1",
                    "market_id": "KXDUP-ENTRY",
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
            bot.exchanges["kalshi"] = exchange

            bot.connect_all()
            result = bot._process_signal(
                {
                    "exchange": "kalshi",
                    "market_id": "KXDUP-ENTRY",
                    "question": "Should ambiguous startup block duplicate entry?",
                    "direction": "BUY_YES",
                    "market_price": 0.40,
                    "yes_price": 0.40,
                    "no_price": 0.60,
                    "model_probability": 0.75,
                    "edge": 0.25,
                    "confidence": 0.90,
                }
            )

            self.assertEqual(result["blocked_reason"], "reconciliation_state_blocked")
            self.assertIn("ambiguous_local_exchange_duplicate_exposure", result["reconciliation_issues"])
            self.assertEqual(exchange.placed_orders, [])

    def test_resolution_distinguishes_market_outcome_from_trade_result_for_buy_no_loss(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.exchanges["kalshi"] = FakeReconExchange(
                positions=[
                    Position(
                        market_id="KXLOSS-1",
                        exchange="kalshi",
                        question="Will it not rain?",
                        side="NO",
                        entry_price=0.62,
                        size=2.0,
                        current_price=0.38,
                        pnl=0.0,
                        opened_at=datetime(2026, 4, 20, 17, 0, tzinfo=timezone.utc),
                    )
                ],
                market_map={
                    "KXLOSS-1": Market(
                        id="KXLOSS-1",
                        exchange="kalshi",
                        question="Will it not rain?",
                        yes_price=1.0,
                        no_price=0.0,
                        volume=0,
                        liquidity=0,
                        closes_at=datetime.now(timezone.utc),
                        category="weather",
                        metadata={"result": "YES"},
                        close_price=1.0,
                    )
                },
                balance=25.0,
            )

            bot.connect_all()
            bot._sync_resolved_positions()

            resolved_row = next(row for row in bot.trade_history if row["trade_id"].startswith("reconciled:kalshi:KXLOSS-1"))
            self.assertEqual(resolved_row["outcome"], "YES")
            self.assertEqual(resolved_row["resolution_outcome"], "YES")
            self.assertEqual(resolved_row["resolution_result"], "lost")
            self.assertEqual(resolved_row["resolution_type"], "settled")
            self.assertEqual(resolved_row["exit_price"], 1.0)
            self.assertEqual(resolved_row["settlement_value"], 0.0)
            self.assertEqual(resolved_row["pnl"], -2.0)
            self.assertEqual(resolved_row["gross_pnl"], -2.0)
            self.assertEqual(resolved_row["fee_paid"], 0.0)
            self.assertEqual(resolved_row["expected_pnl"], -2.0)
            self.assertEqual(resolved_row["net_pnl"], -2.0)
            self.assertEqual(resolved_row["contracts"], 3.2258)
            self.assertEqual(resolved_row["integrity_status"], "ok")
            self.assertEqual(resolved_row["integrity_errors"], [])

    def test_resolution_enrichment_respects_explicit_zero_fee_rate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.kelly.fee_rate = 0.0
            bot.exchanges["kalshi"] = FakeReconExchange(
                positions=[
                    Position(
                        market_id="KXZEROFEE-1",
                        exchange="kalshi",
                        question="Zero fee settlement",
                        side="YES",
                        entry_price=0.40,
                        size=2.0,
                        current_price=0.40,
                        pnl=0.0,
                        opened_at=datetime(2026, 4, 20, 17, 0, tzinfo=timezone.utc),
                    )
                ],
                market_map={
                    "KXZEROFEE-1": Market(
                        id="KXZEROFEE-1",
                        exchange="kalshi",
                        question="Zero fee settlement",
                        yes_price=1.0,
                        no_price=0.0,
                        volume=0,
                        liquidity=0,
                        closes_at=datetime.now(timezone.utc),
                        category="weather",
                        metadata={"result": "YES"},
                        close_price=1.0,
                    )
                },
                balance=25.0,
            )

            bot.connect_all()
            bot._sync_resolved_positions()

            resolved_row = next(row for row in bot.trade_history if row["trade_id"].startswith("reconciled:kalshi:KXZEROFEE-1"))
            self.assertEqual(resolved_row["fee_paid"], 0.0)
            self.assertEqual(resolved_row["expected_pnl"], 3.0)
            self.assertEqual(resolved_row["net_pnl"], 3.0)
            self.assertEqual(resolved_row["settlement_value"], 5.0)
            self.assertEqual(resolved_row["integrity_status"], "ok")

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

    def test_reconciliation_corrects_local_trade_row_when_exchange_reports_partial(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.open_orders = [
                {
                    "order_id": "ord-local-partial",
                    "exchange": "kalshi",
                    "market_id": "KXCORRECT-1",
                    "question": "Will local row be corrected?",
                    "direction": "BUY_YES",
                    "status": "placed",
                    "requested_size": 4.0,
                    "placed_size": 4.0,
                    "filled_size": 0.0,
                    "remaining_size": 4.0,
                    "reserved_capital": 4.0,
                    "price": 0.41,
                    "created_at": "2026-04-20T00:00:00+00:00",
                }
            ]
            bot.trade_history = [
                {
                    "timestamp": "2026-04-20T00:00:00+00:00",
                    "trade_id": "ord-local-partial",
                    "order_id": "ord-local-partial",
                    "exchange": "kalshi",
                    "market_id": "KXCORRECT-1",
                    "question": "Will local row be corrected?",
                    "direction": "BUY_YES",
                    "status": "placed",
                    "lifecycle_state": "placed_open",
                    "requested_size": 4.0,
                    "approved_size": 4.0,
                    "placed_size": 4.0,
                    "filled_size": 0.0,
                    "remaining_size": 4.0,
                    "reserved_capital": 4.0,
                    "price": 0.41,
                    "market_price": 0.41,
                    "entry_price": 0.41,
                    "fill_price": None,
                    "resolved": False,
                    "decision_reason_code": "approved",
                    "reconciled": False,
                }
            ]
            bot.exchanges["kalshi"] = FakeReconExchange(
                orders=[
                    RestingOrder(
                        order_id="ord-local-partial",
                        market_id="KXCORRECT-1",
                        exchange="kalshi",
                        question="Will local row be corrected?",
                        side="YES",
                        requested_size=4.0,
                        filled_size=1.5,
                        remaining_size=2.5,
                        price=0.41,
                        status="partial",
                        created_at=datetime(2026, 4, 20, 0, 0, tzinfo=timezone.utc),
                    )
                ],
                balance=25.0,
            )

            bot.connect_all()

            self.assertEqual(len(bot.trade_history), 1)
            corrected = bot.trade_history[0]
            self.assertTrue(corrected["reconciliation_corrected"])
            self.assertEqual(corrected["status"], "partial")
            self.assertEqual(corrected["lifecycle_state"], "partial_open")
            self.assertEqual(corrected["filled_size"], 1.5)
            self.assertEqual(corrected["remaining_size"], 2.5)
            self.assertEqual(corrected["reserved_capital"], 4.0)
            self.assertEqual(corrected["reconciliation_contract"]["severity"], "medium")

            with open(f"{tmpdir}/live/reconciliation.jsonl") as f:
                snapshots = [json.loads(line) for line in f if line.strip()]
            self.assertEqual(snapshots[-1]["severity"], "medium")
            self.assertEqual(snapshots[-1]["action"], "correct_and_continue")
            self.assertEqual(snapshots[-1]["filled_exposure"], 0.0)
            self.assertEqual(snapshots[-1]["pending_exposure"], 2.5)
            self.assertTrue(any(event["issue"] == "local_order_status_corrected_from_exchange" for event in snapshots[-1]["corrections"]))

    def test_reconciliation_does_not_duplicate_matching_local_trade_row(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.trade_history = [
                {
                    "timestamp": "2026-04-20T00:00:00+00:00",
                    "trade_id": "ord-match-1",
                    "order_id": "ord-match-1",
                    "exchange": "kalshi",
                    "market_id": "KXMATCH-1",
                    "question": "Will matching rows stay deduped?",
                    "direction": "BUY_YES",
                    "status": "partial",
                    "lifecycle_state": "partial_open",
                    "requested_size": 4.0,
                    "approved_size": 4.0,
                    "placed_size": 4.0,
                    "filled_size": 1.5,
                    "remaining_size": 2.5,
                    "reserved_capital": 4.0,
                    "price": 0.41,
                    "market_price": 0.41,
                    "entry_price": 0.41,
                    "fill_price": None,
                    "resolved": False,
                    "decision_reason_code": "approved",
                    "reconciled": False,
                }
            ]
            bot.exchanges["kalshi"] = FakeReconExchange(
                orders=[
                    RestingOrder(
                        order_id="ord-match-1",
                        market_id="KXMATCH-1",
                        exchange="kalshi",
                        question="Will matching rows stay deduped?",
                        side="YES",
                        requested_size=4.0,
                        filled_size=1.5,
                        remaining_size=2.5,
                        price=0.41,
                        status="partial",
                        created_at=datetime(2026, 4, 20, 0, 0, tzinfo=timezone.utc),
                    )
                ],
                balance=25.0,
            )

            bot.connect_all()

            self.assertEqual(len(bot.trade_history), 1)
            self.assertEqual(bot.trade_history[0]["trade_id"], "ord-match-1")
            self.assertFalse(bot.trade_history[0].get("reconciled", False))


if __name__ == "__main__":
    unittest.main()
