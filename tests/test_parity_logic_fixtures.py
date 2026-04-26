import tempfile
import unittest
from unittest.mock import Mock

from bot.live_execution import RunnerLiveExecutionAdapter
from bot.runner import LivePosition, PredictionBot
from bot.shared_core import build_execution_snapshot, build_trade_decision
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


class ParityLogicFixtureTests(unittest.TestCase):
    def _make_shared_inputs(self):
        signal = {
            "exchange": "kalshi",
            "market_id": "KXHIGHNY-26APR16-T72",
            "question": "Will NYC high be below 72?",
            "direction": "BUY_YES",
            "market_price": 0.40,
            "yes_price": 0.40,
            "no_price": 0.60,
            "model_probability": 0.75,
            "edge": 0.15,
            "confidence": 0.90,
            "signals": {},
            "liquidity": 100.0,
        }
        exchange = StaticBookExchange()
        execution_snapshot = build_execution_snapshot(
            signal,
            direction="BUY_YES",
            bid_ask=exchange.get_market_bid_ask(signal["market_id"]),
        )
        return signal, exchange, execution_snapshot

    def test_hidden_gem_decision_matches_between_paper_and_live_under_same_inputs(self):
        signal, exchange, execution_snapshot = self._make_shared_inputs()
        signal.update({
            "market_price": 0.03,
            "yes_price": 0.03,
            "no_price": 0.97,
            "model_probability": 0.12,
            "edge": 0.09,
        })
        execution_snapshot.update({
            "market_price": 0.03,
            "yes_price": 0.03,
            "no_price": 0.97,
            "best_yes_ask": 0.03,
            "best_no_ask": 0.97,
            "estimated_fill_price": 0.03,
        })

        kelly = Mock()
        kelly.calculate.return_value = 2.0
        risk = Mock()
        risk.max_tradable_balance = 10.0
        risk.max_event_exposure_pct = 0.10
        risk.max_event_positions = 3
        risk.retrade_edge_premium = 0.0
        risk.retrade_confidence_premium = 0.0
        risk.retrade_size_decay = 1.0
        risk.strict_event_overlap = False
        risk.min_retrade_net_edge = 0.01
        risk.min_retrade_expected_profit_usd = 0.0
        risk.require_price_improvement_for_same_market_family = False
        risk.price_improvement_ticks = 0.0
        risk.check_trade.return_value = type("RiskDecision", (), {
            "approved": True,
            "reason": "Approved",
            "adjusted_size": 2.0,
            "risk_score": 0.1,
            "warnings": [],
        })()

        with tempfile.TemporaryDirectory() as tmpdir:
            sim = Simulator({
                "data_dir": tmpdir,
                "enable_social": False,
                "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
                "max_position_size_usd": 4.0,
                "max_tradable_balance_usd": 10.0,
            })
            sim.kelly = kelly
            sim.risk = risk
            sim.available_cash = 25.0
            sim.reserved_capital = 0.0
            sim.risk.state.available_cash = 25.0
            sim.risk.state.reserved_capital = 0.0
            sim.risk.state.daily_pnl = 0.0
            sim.risk.state.drawdown_pct = 0.0
            sim.risk.state.consecutive_losses = 0
            sim.risk.state.consecutive_wins = 0
            sim.risk.state.trading_enabled = True
            sim.risk.state.standby_active = False
            sim.risk.state.standby_reason_codes = []
            sim.risk.state.standby_blocked_scan_count = 0
            sim.risk.max_position_size_usd = 4.0
            paper_context = sim.state_adapter.build_trade_context_from_snapshot(signal, execution_snapshot=execution_snapshot)
            paper_decision = build_trade_decision(
                paper_context,
                kelly_sizer=sim.kelly,
                risk_policy=sim.risk,
                min_edge=0.01,
                min_confidence=0.5,
                max_entry_price=0.70,
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            bot = PredictionBot({
                "log_dir": tmpdir,
                "data_dir": tmpdir,
                "trading": {"mode": "live", "enabled": True},
                "strategy": {"min_edge": 0.01, "min_confidence": 0.5, "enable_news": False, "enable_social": False, "enable_ai": False},
                "max_position_size_usd": 4.0,
                "max_tradable_balance_usd": 10.0,
            })
            bot.kelly = kelly
            bot.risk = risk
            bot.risk.state.current_balance = 25.0
            bot.risk.state.available_cash = 25.0
            bot.risk.state.reserved_capital = 0.0
            bot.risk.state.total_exposure = 0.0
            bot.risk.state.daily_pnl = 0.0
            bot.risk.state.drawdown_pct = 0.0
            bot.risk.state.consecutive_losses = 0
            bot.risk.state.consecutive_wins = 0
            bot.risk.state.peak_balance = 25.0
            bot.risk.state.session_starting_balance = 25.0
            bot.risk.state.session_peak_balance = 25.0
            bot.risk.state.max_drawdown_halt = False
            adapter = RunnerLiveExecutionAdapter(bot)
            live_signal = dict(signal)
            live_signal.update(execution_snapshot)
            live_context = adapter.build_trade_context(live_signal, exchange, bot.config)
            live_decision = build_trade_decision(
                live_context,
                kelly_sizer=bot.kelly,
                risk_policy=bot.risk,
                min_edge=0.01,
                min_confidence=0.5,
                max_entry_price=0.70,
            )

        self.assertEqual(paper_decision.approved, live_decision.approved)
        self.assertEqual(paper_decision.reason_code, live_decision.reason_code)
        self.assertEqual(paper_decision.entry_price, live_decision.entry_price)

    def test_retrade_decision_matches_between_paper_and_live_under_same_inputs(self):
        signal, exchange, execution_snapshot = self._make_shared_inputs()

        kelly = Mock()
        kelly.calculate.return_value = 10.0
        risk = Mock()
        risk.max_tradable_balance = 10.0
        risk.max_event_exposure_pct = 0.10
        risk.max_event_positions = 3
        risk.retrade_edge_premium = 0.0
        risk.retrade_confidence_premium = 0.0
        risk.retrade_size_decay = 1.0
        risk.strict_event_overlap = False
        risk.min_retrade_net_edge = 0.01
        risk.min_retrade_expected_profit_usd = 0.0
        risk.require_price_improvement_for_same_market_family = False
        risk.price_improvement_ticks = 0.0
        risk.check_trade.return_value = type("RiskDecision", (), {
            "approved": True,
            "reason": "Approved",
            "adjusted_size": 2.0,
            "risk_score": 0.1,
            "warnings": [],
        })()

        with tempfile.TemporaryDirectory() as tmpdir:
            sim = Simulator({
                "data_dir": tmpdir,
                "enable_social": False,
                "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
                "max_position_size_usd": 4.0,
                "max_tradable_balance_usd": 10.0,
            })
            sim.kelly = kelly
            sim.risk = risk
            sim.balance = 25.0
            sim.available_cash = 20.0
            sim.reserved_capital = 5.0
            sim.risk.state.current_balance = 25.0
            sim.risk.state.daily_pnl = 0.0
            sim.risk.state.drawdown_pct = 0.0
            sim.risk.state.consecutive_losses = 0
            sim.risk.state.consecutive_wins = 0
            sim.risk.state.trading_enabled = True
            sim.risk.state.standby_active = False
            sim.risk.state.standby_reason_codes = []
            sim.risk.state.standby_blocked_scan_count = 0
            sim.risk.max_position_size_usd = 4.0
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
            paper_decision = build_trade_decision(
                paper_context,
                kelly_sizer=sim.kelly,
                risk_policy=sim.risk,
                min_edge=0.01,
                min_confidence=0.5,
                max_entry_price=0.70,
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            bot = PredictionBot({
                "log_dir": tmpdir,
                "data_dir": tmpdir,
                "trading": {"mode": "live", "enabled": True},
                "strategy": {"min_edge": 0.01, "min_confidence": 0.5, "enable_news": False, "enable_social": False, "enable_ai": False},
                "max_position_size_usd": 4.0,
                "max_tradable_balance_usd": 10.0,
            })
            bot.kelly = kelly
            bot.risk = risk
            bot.risk.state.current_balance = 25.0
            bot.risk.state.available_cash = 20.0
            bot.risk.state.reserved_capital = 5.0
            bot.risk.state.daily_pnl = 0.0
            bot.risk.state.drawdown_pct = 0.0
            bot.risk.state.consecutive_losses = 0
            bot.risk.state.consecutive_wins = 0
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
            live_decision = build_trade_decision(
                live_context,
                kelly_sizer=bot.kelly,
                risk_policy=bot.risk,
                min_edge=0.01,
                min_confidence=0.5,
                max_entry_price=0.70,
            )

        self.assertEqual(paper_decision.approved, live_decision.approved)
        self.assertEqual(paper_decision.reason_code, live_decision.reason_code)
        self.assertEqual(paper_decision.position_size, live_decision.position_size)
        self.assertEqual(
            paper_decision.reasoning.get("retrade", {}).get("size_decay_applied"),
            live_decision.reasoning.get("retrade", {}).get("size_decay_applied"),
        )


if __name__ == "__main__":
    unittest.main()
