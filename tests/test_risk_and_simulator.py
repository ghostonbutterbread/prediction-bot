import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from bot.risk import RiskManager
from bot.simulator import Simulator


class FakeStandbyExchange:
    def __init__(self):
        self.calls = 0

    def get_market(self, market_id: str):
        return None

    def get_markets(self, limit=100):
        self.calls += 1
        return []


class RiskManagerTests(unittest.TestCase):
    def test_max_exposure_caps_size_instead_of_full_reject(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            risk = RiskManager(
                {
                    "data_dir": tmpdir,
                    "starting_balance": 100.0,
                    "max_exposure_pct": 0.40,
                }
            )
            risk.state.current_balance = 100.0
            risk.state.total_exposure = 39.0

            decision = risk.check_trade({"question": "Will BTC rise?"}, 5.0)

            self.assertTrue(decision.approved)
            self.assertEqual(decision.adjusted_size, 1.0)
            self.assertTrue(any("Exposure headroom capped size" in warning for warning in decision.warnings))

    def test_available_cash_caps_size(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            risk = RiskManager(
                {
                    "data_dir": tmpdir,
                    "starting_balance": 100.0,
                }
            )
            risk.state.current_balance = 100.0
            risk.state.available_cash = 3.5

            decision = risk.check_trade({"question": "Will BTC rise?"}, 5.0, available_cash=3.5)

            self.assertTrue(decision.approved)
            self.assertEqual(decision.adjusted_size, 3.5)
            self.assertEqual(decision.metadata["effective_tradable_cash"], 3.5)

    def test_trading_pause_rejects_new_trades(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            risk = RiskManager(
                {
                    "data_dir": tmpdir,
                    "starting_balance": 100.0,
                    "trading_enabled": False,
                }
            )

            decision = risk.check_trade({"question": "Will BTC rise?"}, 5.0)

            self.assertFalse(decision.approved)
            self.assertEqual(decision.reason, "Trading paused by operator")
            self.assertEqual(decision.metadata["reason_code"], "trading_disabled")

    def test_max_tradable_balance_caps_size_before_available_cash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            risk = RiskManager(
                {
                    "data_dir": tmpdir,
                    "starting_balance": 100.0,
                    "max_tradable_balance_usd": 10.0,
                }
            )
            risk.state.current_balance = 100.0
            risk.state.available_cash = 50.0

            decision = risk.check_trade({"question": "Will BTC rise?"}, 18.0, available_cash=50.0)

            self.assertTrue(decision.approved)
            self.assertEqual(decision.adjusted_size, 10.0)
            self.assertTrue(any("Tradable balance capped size" in warning for warning in decision.warnings))
            self.assertEqual(decision.metadata["effective_tradable_cash"], 10.0)

    def test_hard_position_cap_clips_size(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            risk = RiskManager(
                {
                    "data_dir": tmpdir,
                    "starting_balance": 100.0,
                    "max_position_size_usd": 4.0,
                }
            )
            risk.state.current_balance = 100.0
            risk.state.available_cash = 20.0

            decision = risk.check_trade({"question": "Will BTC rise?"}, 8.0, available_cash=20.0)

            self.assertTrue(decision.approved)
            self.assertEqual(decision.adjusted_size, 4.0)
            self.assertTrue(any("Hard position cap clipped size" in warning for warning in decision.warnings))

    def test_capital_blockers_enter_standby_after_threshold(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            risk = RiskManager({"data_dir": tmpdir, "starting_balance": 100.0})
            risk.state.open_positions = risk.max_open_positions
            risk.state.total_exposure = 40.0
            risk.state.available_cash = 2.0

            for _ in range(2):
                risk.record_blocked_scan({"risk_max_positions_15_15": 3}, trades_taken=0)
                self.assertFalse(risk.state.standby_active)

            risk.record_blocked_scan({"risk_max_positions_15_15": 2}, trades_taken=0)

            self.assertTrue(risk.state.standby_active)
            self.assertEqual(risk.state.standby_reason_codes, ["max_positions"])
            self.assertEqual(risk.state.standby_blocked_scan_count, 3)
            self.assertEqual(risk.state.standby_unresolved_positions_at_entry, risk.max_open_positions)
            self.assertEqual(risk.state.standby_exposure_at_entry, 40.0)

    def test_standby_resumes_when_positions_resolve_and_useful_capacity_returns(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            risk = RiskManager({"data_dir": tmpdir, "starting_balance": 100.0})
            risk.state.standby_active = True
            risk.state.standby_reason_codes = ["max_positions"]
            risk.state.standby_unresolved_positions_at_entry = 5
            risk.state.standby_exposure_at_entry = 30.0
            risk.state.open_positions = 3
            risk.state.total_exposure = 18.0
            risk.state.available_cash = 20.0
            risk.state.current_balance = 100.0

            result = risk.evaluate_standby_resume()

            self.assertTrue(result["resumed"])
            self.assertFalse(risk.state.standby_active)
            self.assertIn("positions_resolved=2", risk.state.standby_last_resume_reason)

    def test_reconcile_startup_standby_asserts_when_portfolio_already_extended(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            risk = RiskManager({"data_dir": tmpdir, "starting_balance": 100.0})
            risk.max_open_positions = 15
            risk.state.open_positions = 15
            risk.state.total_exposure = 127.21
            risk.state.available_cash = 316.25
            risk.state.current_balance = 443.46
            risk.state.standby_active = False

            result = risk.reconcile_startup_standby()

            self.assertTrue(result["asserted"])
            self.assertTrue(risk.state.standby_active)
            self.assertIn("max_positions", risk.state.standby_reason_codes)
            self.assertEqual(risk.state.standby_unresolved_positions_at_entry, 15)
            self.assertEqual(risk.state.standby_blocked_scan_count, risk.standby_blocked_scan_threshold)

    def test_reconcile_startup_standby_preserves_existing_active_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            risk = RiskManager({"data_dir": tmpdir, "starting_balance": 100.0})
            risk.state.standby_active = True
            risk.state.standby_reason_codes = ["max_positions"]
            risk.state.open_positions = 15
            risk.state.total_exposure = 127.21
            risk.state.available_cash = 316.25
            risk.state.current_balance = 443.46

            result = risk.reconcile_startup_standby()

            self.assertFalse(result["asserted"])
            self.assertTrue(result["standby_active"])
            self.assertEqual(risk.state.standby_reason_codes, ["max_positions"])


class SimulatorSessionTests(unittest.TestCase):
    def test_create_trade_sizes_from_available_cash_and_reserves_it(self):
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
                "signals": {},
            }

            with patch.object(sim.kelly, "calculate", return_value=10.0) as mock_calculate:
                trade = sim._create_trade(signal)

            self.assertIsNotNone(trade)
            mock_calculate.assert_called_once_with(0.7, 0.4, 25.0)
            self.assertEqual(trade.position_size, 10.0)
            self.assertEqual(trade.reserved_capital, 10.0)
            self.assertEqual(trade.available_cash_before, 25.0)
            self.assertEqual(trade.available_cash_after_entry, 15.0)
            self.assertEqual(sim.available_cash, 15.0)
            self.assertEqual(sim.reserved_capital, 85.0)
            self.assertEqual(sim.risk.state.available_cash, 15.0)
            self.assertEqual(sim.risk.state.reserved_capital, 85.0)
            self.assertEqual(trade.decision_trace["normalized"]["direction"], "BUY_YES")
            self.assertEqual(trade.decision_trace["account_state"]["available_cash"], 25.0)
            self.assertEqual(trade.decision_trace["kelly"]["bankroll"], 25.0)

    def test_create_trade_routes_buy_no_through_shared_decision_path(self):
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
                "market_id": "test-market-no",
                "question": "Will test settle NO?",
                "exchange": "kalshi",
                "direction": "BUY_NO",
                "model_probability": 0.3,
                "market_price": 0.4,
                "no_market_price": 0.62,
                "edge": 0.3,
                "confidence": 0.9,
                "signals": {},
            }

            with patch.object(sim.kelly, "calculate", return_value=10.0) as mock_calculate:
                trade = sim._create_trade(signal)

            self.assertIsNotNone(trade)
            mock_calculate.assert_called_once_with(0.7, 0.62, 25.0)
            self.assertEqual(trade.direction, "BUY_NO")
            self.assertEqual(trade.market_price, 0.62)
            self.assertEqual(trade.model_probability, 0.7)
            self.assertEqual(trade.decision_trace["normalized"]["direction"], "BUY_NO")
            self.assertEqual(trade.decision_trace["normalized"]["entry_price"], 0.62)

    def test_create_trade_uses_tradable_balance_for_kelly_bankroll(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sim = Simulator(
                {
                    "data_dir": tmpdir,
                    "enable_social": False,
                    "max_tradable_balance_usd": 10.0,
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
                "market_id": "test-market-cap",
                "question": "Will test settle YES?",
                "exchange": "kalshi",
                "direction": "BUY_YES",
                "model_probability": 0.7,
                "market_price": 0.4,
                "edge": 0.3,
                "confidence": 0.9,
                "signals": {},
            }

            with patch.object(sim.kelly, "calculate", return_value=10.0) as mock_calculate:
                trade = sim._create_trade(signal)

            self.assertIsNotNone(trade)
            mock_calculate.assert_called_once_with(0.7, 0.4, 10.0)
            self.assertEqual(trade.decision_trace["kelly"]["bankroll"], 10.0)

    def test_create_trade_uses_side_specific_buy_no_market_price_when_no_no_quote_is_provided(self):
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
                "market_id": "test-market-no-side-price",
                "question": "Will test settle NO?",
                "exchange": "kalshi",
                "direction": "BUY_NO",
                "model_probability": 0.3,
                "market_price": 0.24,
                "edge": 0.3,
                "confidence": 0.9,
                "signals": {},
            }

            with patch.object(sim.kelly, "calculate", return_value=10.0) as mock_calculate:
                trade = sim._create_trade(signal)

            self.assertIsNotNone(trade)
            mock_calculate.assert_called_once_with(0.7, 0.24, 25.0)
            self.assertEqual(trade.direction, "BUY_NO")
            self.assertEqual(trade.market_price, 0.24)
            self.assertEqual(trade.model_probability, 0.7)
            self.assertEqual(trade.decision_trace["normalized"]["direction"], "BUY_NO")
            self.assertEqual(trade.decision_trace["normalized"]["entry_price"], 0.24)

    def test_create_trade_allows_same_event_retrade_but_blocks_exact_duplicate_market(self):
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
            first_signal = {
                "market_id": "KXHIGHNY-26APR16-T70",
                "question": "Will NYC high be below 70?",
                "exchange": "kalshi",
                "direction": "BUY_YES",
                "model_probability": 0.7,
                "market_price": 0.4,
                "edge": 0.3,
                "confidence": 0.9,
                "signals": {},
            }
            second_signal = dict(first_signal)
            second_signal["market_id"] = "KXHIGHNY-26APR16-T72"
            second_signal["question"] = "Will NYC high be below 72?"
            with patch.object(sim.kelly, "calculate", return_value=2.0):
                first_trade = sim._create_trade(first_signal)
                sim.trades.append(first_trade)
                sim.traded_markets.add(first_trade.market_id)
                retrade = sim._create_trade(second_signal)
                duplicate = sim._create_trade(first_signal)

            self.assertIsNotNone(retrade)
            self.assertEqual(retrade.event_key, "KXHIGHNY-26APR16")
            self.assertIsNone(duplicate)

    def test_resolved_market_is_removed_from_runtime_duplicate_blocklist(self):
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
            first_signal = {
                "market_id": "KXHIGHNY-26APR16-T70",
                "question": "Will NYC high be below 70?",
                "exchange": "kalshi",
                "direction": "BUY_YES",
                "model_probability": 0.7,
                "market_price": 0.4,
                "edge": 0.3,
                "confidence": 0.9,
                "signals": {},
            }
            second_signal = dict(first_signal)
            second_signal["market_id"] = "KXHIGHNY-26APR16-T72"
            second_signal["question"] = "Will NYC high be below 72?"

            with patch.object(sim.kelly, "calculate", return_value=2.0):
                first_trade = sim._create_trade(first_signal)
                sim.trades.append(first_trade)
                sim.traded_markets.add(first_trade.market_id)
                first_trade.resolved = True
                sim.state_adapter.refresh_traded_markets()
                retrade = sim._create_trade(second_signal)

            self.assertIsNotNone(retrade)
            self.assertNotIn(first_trade.market_id, sim.traded_markets)

    def test_load_session_discards_zero_sized_trade_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            session_file = data_dir / "sim_20260321_193703.json"
            session = {
                "session_id": "20260321_193703",
                "starting_balance": 100.0,
                "balance": 105.0,
                "scan_count": 12,
                "trades": [
                    {
                        "id": "bad-1",
                        "timestamp": "2026-03-21T00:00:00+00:00",
                        "exchange": "kalshi",
                        "market_id": "bad-market",
                        "question": "Bad market",
                        "direction": "BUY_YES",
                        "model_probability": 0.6,
                        "market_price": 0.2,
                        "edge": 0.4,
                        "confidence": 0.8,
                        "position_size": 0.0,
                        "signals": {},
                        "resolved": True,
                        "pnl": 0.0,
                    },
                    {
                        "id": "good-1",
                        "timestamp": "2026-03-21T01:00:00+00:00",
                        "exchange": "kalshi",
                        "market_id": "good-market",
                        "question": "Good market",
                        "direction": "BUY_YES",
                        "model_probability": 0.6,
                        "market_price": 0.2,
                        "edge": 0.4,
                        "confidence": 0.8,
                        "position_size": 5.0,
                        "signals": {},
                        "resolved": False,
                        "pnl": None,
                    },
                ],
            }
            session_file.write_text(json.dumps(session))

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

            self.assertEqual(len(sim.trades), 1)
            self.assertEqual(sim.trades[0].id, "good-1")
            self.assertEqual(sim.risk.state.open_positions, 1)
            self.assertEqual(sim.risk.state.total_exposure, 5.0)
            self.assertEqual(sim.balance, 105.0)
            self.assertEqual(sim.available_cash, 100.0)
            self.assertEqual(sim.reserved_capital, 5.0)
            self.assertEqual(sim.risk.state.available_cash, 100.0)
            self.assertEqual(sim.risk.state.reserved_capital, 5.0)

    def test_simulator_skips_market_fetch_while_standby_active(self):
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
            sim.risk.state.standby_active = True
            sim.risk.state.standby_reason_codes = ["max_positions"]
            sim.risk.state.standby_unresolved_positions_at_entry = 5
            sim.risk.state.open_positions = 5
            sim.risk.state.total_exposure = 30.0
            sim.risk.state.available_cash = 1.0
            exchange = FakeStandbyExchange()

            result = sim.scan(exchange)

            self.assertEqual(exchange.calls, 0)
            self.assertTrue(result["standby"]["active"])
            self.assertEqual(result["standby"]["reason_codes"], ["max_positions"])


if __name__ == "__main__":
    unittest.main()
