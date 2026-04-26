import tempfile
import unittest
from types import SimpleNamespace

from bot.live_execution import RunnerLiveExecutionAdapter
from bot.parity_audit import normalize_parity_trade_row
from bot.paper_adapters import SimulatorPaperExecutionAdapter
from bot.runner import LivePosition, PredictionBot
from bot.shared_core import build_execution_snapshot
from bot.simulator import SimTrade, Simulator


class StaticBookExchange:
    def get_balance(self):
        return 25.0

    def get_market_bid_ask(self, market_id):
        return {
            "best_yes_ask": 0.41,
            "best_no_ask": 0.59,
            "best_yes_bid": 0.40,
            "best_no_bid": 0.58,
        }

    def place_order(self, market_id, side, price, size):
        return SimpleNamespace(id="ord-1")


class ParityProofTests(unittest.TestCase):
    def _signal(self):
        return {
            "exchange": "kalshi",
            "market_id": "KXHIGHNY-26APR16-T72",
            "question": "Will NYC high be below 72?",
            "direction": "BUY_YES",
            "market_price": 0.40,
            "yes_price": 0.40,
            "no_price": 0.60,
            "model_probability": 0.70,
            "edge": 0.30,
            "confidence": 0.90,
            "signals": {},
            "liquidity": 50.0,
        }

    def test_paper_and_live_build_equivalent_account_state_semantics(self):
        signal = self._signal()
        exchange = StaticBookExchange()
        execution_snapshot = build_execution_snapshot(
            signal,
            direction="BUY_YES",
            bid_ask=exchange.get_market_bid_ask(signal["market_id"]),
        )

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
                    "max_position_size_usd": 4.0,
                    "max_tradable_balance_usd": 10.0,
                }
            )
            sim.balance = 25.0
            sim.available_cash = 20.0
            sim.reserved_capital = 5.0
            sim.risk.state.current_balance = 25.0
            sim.risk.state.available_cash = 20.0
            sim.risk.state.reserved_capital = 5.0
            sim.risk.state.total_exposure = 5.0
            sim.risk.state.open_positions = 1
            sim.trades = [
                SimTrade(
                    id="paper-open-1",
                    timestamp="2026-04-23T00:00:00+00:00",
                    exchange="kalshi",
                    market_id="KXHIGHNY-26APR16-T70",
                    question="Will NYC high be below 70?",
                    direction="BUY_YES",
                    model_probability=0.70,
                    market_price=0.42,
                    edge=0.20,
                    confidence=0.90,
                    position_size=5.0,
                    signals={},
                    reserved_capital=5.0,
                    resolved=False,
                    event_key="KXHIGHNY-26APR16",
                )
            ]
            paper_context = sim.state_adapter.build_trade_context_from_snapshot(signal, execution_snapshot=execution_snapshot)

        with tempfile.TemporaryDirectory() as tmpdir:
            bot = PredictionBot(
                {
                    "log_dir": tmpdir,
                    "data_dir": tmpdir,
                    "trading": {"mode": "live", "enabled": True},
                    "strategy": {
                        "min_edge": 0.01,
                        "min_confidence": 0.5,
                        "enable_news": False,
                        "enable_social": False,
                        "enable_ai": False,
                    },
                    "max_position_size_usd": 4.0,
                    "max_tradable_balance_usd": 10.0,
                }
            )
            bot.risk.state.current_balance = 25.0
            bot.risk.state.available_cash = 20.0
            bot.risk.state.reserved_capital = 5.0
            bot.risk.state.total_exposure = 5.0
            bot.risk.state.open_positions = 1
            bot.risk.state.peak_balance = 25.0
            bot.risk.state.session_starting_balance = 25.0
            bot.risk.state.session_peak_balance = 25.0
            bot.risk.state.max_drawdown_halt = False
            bot.open_positions = [
                LivePosition(
                    market_id="KXHIGHNY-26APR16-T70",
                    question="Will NYC high be below 70?",
                    direction="BUY_YES",
                    price=0.42,
                    size=5.0,
                    order_id="pos-1",
                    created_at="2026-04-23T00:00:00+00:00",
                    event_key="KXHIGHNY-26APR16",
                )
            ]
            adapter = RunnerLiveExecutionAdapter(bot)
            live_signal = dict(signal)
            live_signal.update(execution_snapshot)
            live_context = adapter.build_trade_context(live_signal, exchange, bot.config)

        self.assertEqual(paper_context.account_state.current_balance, live_context.account_state.current_balance)
        self.assertEqual(paper_context.account_state.available_cash, live_context.account_state.available_cash)
        self.assertEqual(paper_context.account_state.reserved_capital, live_context.account_state.reserved_capital)
        self.assertEqual(paper_context.account_state.total_exposure, live_context.account_state.total_exposure)
        self.assertEqual(paper_context.account_state.open_positions, live_context.account_state.open_positions)
        self.assertEqual(
            paper_context.metadata["event_snapshot"]["event_exposure_before"],
            live_context.metadata["event_snapshot"]["event_exposure_before"],
        )
        self.assertEqual(
            paper_context.metadata["event_snapshot"]["best_same_family_entry_price"],
            live_context.metadata["event_snapshot"]["best_same_family_entry_price"],
        )

    def test_normalized_rows_match_core_shape_for_equivalent_paper_and_live_fills(self):
        signal = self._signal()
        exchange = StaticBookExchange()
        execution_snapshot = build_execution_snapshot(
            signal,
            direction="BUY_YES",
            bid_ask=exchange.get_market_bid_ask(signal["market_id"]),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            sim = Simulator(
                {
                    "data_dir": tmpdir,
                    "enable_social": False,
                    "parity_mode": {
                        "enabled": True,
                        "record_revalidation_snapshot": True,
                        "require_book_prices": False,
                        "fallback_to_signal_prices": True,
                    },
                    "strategy": {
                        "enable_news": False,
                        "enable_social": False,
                        "enable_ai": False,
                    },
                    "max_position_size_usd": 4.0,
                    "max_tradable_balance_usd": 10.0,
                }
            )
            paper_context = sim.state_adapter.build_trade_context_from_snapshot(signal, execution_snapshot=execution_snapshot)
            paper_decision = SimpleNamespace(
                approved=True,
                action="BUY_YES",
                reason="approved",
                reason_code="approved",
                requested_position_size=1.0,
                position_size=1.0,
                entry_price=0.41,
                win_probability=0.70,
                reasoning={
                    "parity_mode": {
                        "enabled": True,
                        "execution_revalidated": True,
                        "execution_revalidation_outcome": "approved",
                        "execution_snapshot_source": "book",
                        "original_signal_snapshot": {"market_price": 0.40},
                        "execution_snapshot": execution_snapshot,
                        "original_decision_reason_code": "approved",
                        "execution_decision_reason_code": "approved",
                    }
                },
            )
            paper_result = SimulatorPaperExecutionAdapter(sim).execute(paper_decision, paper_context)
            paper_row = {
                "id": paper_result.trade_id,
                "timestamp": paper_result.metadata["timestamp"],
                "exchange": paper_result.metadata["exchange"],
                "market_id": paper_result.metadata["market_id"],
                "question": paper_result.metadata["question"],
                "direction": paper_decision.action,
                "market_price": paper_result.fill_price,
                "model_probability": paper_result.metadata["model_probability"],
                "edge": paper_result.metadata["edge"],
                "confidence": paper_result.metadata["confidence"],
                "position_size": paper_result.filled_size,
                "reserved_capital": paper_result.metadata["reserved_capital"],
                "available_cash_before": paper_result.metadata["available_cash_before"],
                "available_cash_after_entry": paper_result.metadata["available_cash_after_entry"],
                "event_key": paper_result.metadata["event_key"],
                "decision_trace": paper_result.metadata["decision_trace"],
                "decision_reason_code": "approved",
                "requested_size": paper_result.requested_size,
                "approved_size": paper_result.filled_size,
                "placed_size": paper_result.filled_size,
                "filled_size": paper_result.filled_size,
                "remaining_size": paper_result.remaining_size,
                "fill_price": paper_result.fill_price,
                "status": paper_result.status,
                "parity_mode_enabled": paper_result.metadata["parity_mode_enabled"],
                "execution_revalidated": paper_result.metadata["execution_revalidated"],
                "execution_revalidation_outcome": paper_result.metadata["execution_revalidation_outcome"],
                "original_signal_snapshot": paper_result.metadata["original_signal_snapshot"],
                "execution_snapshot": paper_result.metadata["execution_snapshot"],
                "original_decision_reason_code": paper_result.metadata["original_decision_reason_code"],
                "execution_decision_reason_code": paper_result.metadata["execution_decision_reason_code"],
                "execution_snapshot_source": paper_result.metadata["execution_snapshot_source"],
            }
            normalized_paper = normalize_parity_trade_row(paper_row, source="paper")

        with tempfile.TemporaryDirectory() as tmpdir:
            bot = PredictionBot(
                {
                    "log_dir": tmpdir,
                    "data_dir": tmpdir,
                    "trading": {"mode": "live", "enabled": True},
                    "strategy": {
                        "min_edge": 0.01,
                        "min_confidence": 0.5,
                        "enable_news": False,
                        "enable_social": False,
                        "enable_ai": False,
                    },
                    "max_position_size_usd": 4.0,
                    "max_tradable_balance_usd": 10.0,
                }
            )
            bot.risk.state.current_balance = 25.0
            bot.risk.state.available_cash = 25.0
            bot.risk.state.peak_balance = 25.0
            bot.risk.state.session_starting_balance = 25.0
            bot.risk.state.session_peak_balance = 25.0
            bot.risk.state.max_drawdown_halt = False
            adapter = RunnerLiveExecutionAdapter(bot)
            exchange.place_order = lambda market_id, side, price, size: SimpleNamespace(id="ord-filled", status="filled", filled_size=size, remaining_size=0.0)
            live_decision = SimpleNamespace(
                action="BUY_YES",
                approved=True,
                position_size=1.0,
                entry_price=0.41,
                win_probability=0.70,
                reason="approved",
                reason_code="approved",
                requested_position_size=1.0,
                reasoning={
                    "parity_mode": {
                        "enabled": True,
                        "execution_revalidated": True,
                        "execution_revalidation_outcome": "approved",
                        "execution_snapshot_source": "book",
                        "original_signal_snapshot": {"market_price": 0.40},
                        "execution_snapshot": execution_snapshot,
                        "original_decision_reason_code": "approved",
                        "execution_decision_reason_code": "approved",
                    }
                },
            )
            adapter.execute(signal, live_decision, exchange)
            live_row = bot.trade_history[0]
            live_row["execution_snapshot_source"] = "book"
            live_row["parity_mode_enabled"] = True
            live_row["execution_revalidated"] = True
            live_row["execution_revalidation_outcome"] = "approved"
            live_row["original_signal_snapshot"] = {"market_price": 0.40}
            live_row["execution_snapshot"] = execution_snapshot
            live_row["original_decision_reason_code"] = "approved"
            live_row["execution_decision_reason_code"] = "approved"
            normalized_live = normalize_parity_trade_row(live_row, source="live")

        for field in [
            "market_id",
            "direction",
            "status",
            "decision_reason_code",
            "requested_size",
            "approved_size",
            "placed_size",
            "filled_size",
            "remaining_size",
            "entry_price",
            "execution_snapshot_source",
            "parity_mode_enabled",
            "execution_revalidated",
            "execution_revalidation_outcome",
            "original_decision_reason_code",
            "execution_decision_reason_code",
        ]:
            self.assertEqual(normalized_paper[field], normalized_live[field], field)


if __name__ == "__main__":
    unittest.main()
