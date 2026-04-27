import tempfile
import unittest
from unittest.mock import patch

from bot.shared_core import (
    AccountState,
    CancelOrderResult,
    ExecutionResult,
    LiveExecutionAdapter,
    LiveStateAdapter,
    OrderState,
    PaperExecutionAdapter,
    ResolutionAdapter,
    ResolutionEvent,
    PaperStateAdapter,
    build_trade_decision,
)
from bot.simulator import Simulator


class FakeResolvedMarket:
    def __init__(self, *, result: str = "YES", yes_price: float = 1.0, no_price: float = 0.0):
        self.metadata = {"status": "settled", "result": result}
        self.yes_price = yes_price
        self.no_price = no_price
        self.close_price = 1.0 if result == "YES" else 0.0
        self.closes_at = None


class FakeSettlementExchange:
    def __init__(self, markets):
        self.markets = markets

    def get_market(self, market_id: str):
        return self.markets.get(market_id)


class SharedCorePaperAdapterTests(unittest.TestCase):
    def test_simulator_exposes_paper_state_and_execution_adapter_shapes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sim = Simulator(
                {
                    "data_dir": tmpdir,
                    "enable_social": False,
                    "strategy": {
                        "enable_news": False,
                        "enable_social": False,
                        "enable_ai": False,
                    },
                }
            )

            self.assertIsInstance(sim, PaperStateAdapter)
            self.assertIsInstance(sim, PaperExecutionAdapter)
            self.assertIsInstance(sim, ResolutionAdapter)
            self.assertIsInstance(sim.state_adapter, PaperStateAdapter)
            self.assertIsInstance(sim.execution_adapter, PaperExecutionAdapter)
            self.assertIsInstance(sim.resolution_adapter, ResolutionAdapter)

            session = sim.get_paper_session_state()
            self.assertEqual(session.session_id, sim.session_id)
            self.assertEqual(session.scan_count, 0)
            self.assertEqual(session.traded_market_count, 0)
            self.assertEqual(session.data_path, str(sim.data_dir))

            signal = {
                "market_id": "test-market",
                "question": "Will test settle YES?",
                "exchange": "kalshi",
                "direction": "BUY_YES",
                "model_probability": 0.7,
                "market_price": 0.4,
                "edge": 0.3,
                "confidence": 0.9,
                "signals": {},
            }

            with patch.object(sim.kelly, "calculate", return_value=10.0):
                trade = sim._create_trade(signal)

            self.assertIsNotNone(trade)
            sim.trades.append(trade)
            positions = sim.list_open_positions()
            self.assertEqual(len(positions), 1)
            self.assertEqual(positions[0].position_id, trade.id)
            self.assertEqual(positions[0].reserved_capital, 10.0)
            self.assertEqual(positions[0].metadata["exchange"], "kalshi")

    def test_simulator_execution_adapter_returns_fill_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sim = Simulator(
                {
                    "data_dir": tmpdir,
                    "enable_social": False,
                    "strategy": {
                        "enable_news": False,
                        "enable_social": False,
                        "enable_ai": False,
                    },
                }
            )
            sim.available_cash = 25.0
            sim.reserved_capital = 75.0
            sim.risk.state.available_cash = 25.0
            sim.risk.state.reserved_capital = 75.0

            signal = {
                "market_id": "test-market",
                "question": "Will test settle YES?",
                "exchange": "kalshi",
                "direction": "BUY_YES",
                "model_probability": 0.7,
                "market_price": 0.4,
                "edge": 0.3,
                "confidence": 0.9,
                "signals": {"source": "unit-test"},
            }

            with patch.object(sim.kelly, "calculate", return_value=10.0):
                context = sim._build_trade_context(signal)
                decision = build_trade_decision(
                    context,
                    kelly_sizer=sim.kelly,
                    risk_policy=sim.risk,
                    min_edge=sim.min_edge,
                    min_confidence=sim.min_confidence,
                    max_entry_price=sim.max_entry_price,
                )
                result = sim.execute(decision, context)

            self.assertTrue(result.accepted)
            self.assertEqual(result.status, "filled")
            self.assertTrue(result.trade_id.startswith(f"sim_{sim.session_id}_"))
            self.assertEqual(result.filled_size, 10.0)
            self.assertEqual(result.reserved_capital_delta, 10.0)
            self.assertEqual(result.available_cash_after, 15.0)
            self.assertEqual(result.metadata["available_cash_before"], 25.0)
            self.assertEqual(result.metadata["available_cash_after_entry"], 15.0)
            self.assertEqual(result.metadata["signals"]["source"], "unit-test")

    def test_simulator_resolution_adapter_returns_resolution_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sim = Simulator(
                {
                    "data_dir": tmpdir,
                    "enable_social": False,
                    "strategy": {
                        "enable_news": False,
                        "enable_social": False,
                        "enable_ai": False,
                    },
                }
            )

            signal = {
                "market_id": "test-market",
                "question": "Will test settle YES?",
                "exchange": "kalshi",
                "direction": "BUY_YES",
                "model_probability": 0.7,
                "market_price": 0.4,
                "edge": 0.3,
                "confidence": 0.9,
                "signals": {"source": "unit-test"},
            }

            with patch.object(sim.kelly, "calculate", return_value=10.0):
                trade = sim._create_trade(signal)

            sim.trades.append(trade)
            self.assertFalse(hasattr(sim.state_adapter, "save_session"))
            self.assertFalse(hasattr(sim.state_adapter, "load_session"))

            with patch.object(sim.session_store, "save_session", wraps=sim.session_store.save_session) as save_mock:
                with patch.object(sim.session_store, "load_session", wraps=sim.session_store.load_session) as load_mock:
                    events = sim.resolve_open_positions(
                        FakeSettlementExchange({"test-market": FakeResolvedMarket(result="YES")})
                    )

            self.assertEqual(len(events), 1)
            self.assertIsInstance(events[0], ResolutionEvent)
            self.assertEqual(events[0].position_id, trade.id)
            self.assertEqual(events[0].market_id, "test-market")
            self.assertEqual(events[0].status, "settled")
            self.assertEqual(events[0].outcome, "YES")
            self.assertAlmostEqual(events[0].pnl, 13.95)
            self.assertAlmostEqual(events[0].settlement_value, 23.95)
            self.assertTrue(sim.trades[0].resolved)
            self.assertAlmostEqual(sim.balance, 113.95)
            self.assertAlmostEqual(sim.available_cash, 113.95)
            self.assertAlmostEqual(sim.reserved_capital, 0.0)
            self.assertEqual(sim.resolution_adapter.last_summary["resolved_this_pass"], 1)
            save_mock.assert_called_once()
            load_mock.assert_called_once()


class SharedCoreLiveAdapterShapeTests(unittest.TestCase):
    def test_stub_paper_execution_adapter_matches_protocol_shape(self):
        class StubPaperExecutionAdapter:
            def execute(self, decision, context) -> ExecutionResult:
                return ExecutionResult(
                    accepted=True,
                    action="BUY_YES",
                    status="filled",
                    trade_id="paper-1",
                    requested_size=12.0,
                    filled_size=12.0,
                    remaining_size=0.0,
                    fill_price=0.44,
                    reserved_capital_delta=12.0,
                    available_cash_after=88.0,
                )

        adapter = StubPaperExecutionAdapter()
        result = adapter.execute(None, None)

        self.assertIsInstance(adapter, PaperExecutionAdapter)
        self.assertEqual(result.trade_id, "paper-1")
        self.assertEqual(result.status, "filled")

    def test_stub_live_adapters_match_protocol_shapes(self):
        class StubLiveAdapter:
            def get_account_state(self) -> AccountState:
                return AccountState(
                    starting_balance=100.0,
                    current_balance=112.5,
                    available_cash=40.0,
                    reserved_capital=12.5,
                    total_exposure=60.0,
                    open_positions=2,
                    metadata={"mode": "live"},
                )

            def list_open_positions(self):
                return []

            def list_resting_orders(self):
                return [
                    OrderState(
                        order_id="ord-1",
                        market_id="market-1",
                        direction="BUY_YES",
                        status="open",
                        requested_size=15.0,
                        filled_size=5.0,
                        remaining_size=10.0,
                        limit_price=0.42,
                    )
                ]

            def execute(self, decision, context) -> ExecutionResult:
                return ExecutionResult(
                    accepted=True,
                    action="BUY_YES",
                    status="accepted",
                    order_id="ord-1",
                    requested_size=15.0,
                    filled_size=5.0,
                    remaining_size=10.0,
                    fill_price=0.42,
                )

            def get_order_status(self, order_id: str) -> OrderState | None:
                return self.list_resting_orders()[0] if order_id == "ord-1" else None

            def cancel_order(self, order_id: str) -> CancelOrderResult:
                return CancelOrderResult(
                    accepted=True,
                    order_id=order_id,
                    status="cancelled",
                    message="Cancelled remaining size",
                )

        adapter = StubLiveAdapter()

        self.assertIsInstance(adapter, LiveStateAdapter)
        self.assertIsInstance(adapter, LiveExecutionAdapter)
        self.assertEqual(adapter.list_resting_orders()[0].remaining_size, 10.0)
        self.assertEqual(adapter.get_order_status("ord-1").status, "open")
        self.assertEqual(adapter.cancel_order("ord-1").status, "cancelled")


if __name__ == "__main__":
    unittest.main()
