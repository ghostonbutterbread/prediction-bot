import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from bot.risk import RiskManager
from bot.runner import PredictionBot
from bot.live_execution import RunnerLiveExecutionAdapter
from bot.shared_core import build_execution_snapshot, build_trade_decision
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


class StaticBookExchange:
    def __init__(self, yes_ask=0.41, no_ask=0.59, yes_bid=0.40, no_bid=0.58):
        self.snapshot = {
            "best_yes_ask": yes_ask,
            "best_no_ask": no_ask,
            "best_yes_bid": yes_bid,
            "best_no_bid": no_bid,
        }

    def get_balance(self):
        return 25.0

    def get_market_bid_ask(self, market_id):
        return dict(self.snapshot)

    def place_order(self, market_id, side, price, size):
        return SimpleNamespace(id="dry-run")


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

    def test_create_trade_records_parity_metadata_when_enabled(self):
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
                }
            )
            sim.available_cash = 25.0
            sim.reserved_capital = 75.0
            sim.risk.state.available_cash = 25.0
            sim.risk.state.reserved_capital = 75.0

            signal = {
                "market_id": "test-parity-market",
                "question": "Will test settle YES?",
                "exchange": "kalshi",
                "direction": "BUY_YES",
                "market_price": 0.04,
                "yes_price": 0.04,
                "no_price": 0.96,
                "best_yes_ask": 0.04,
                "best_no_ask": 0.96,
                "model_probability": 0.20,
                "edge": 0.16,
                "confidence": 0.9,
                "signals": {},
            }

            with patch.object(sim.kelly, "calculate", return_value=5.0):
                trade = sim._create_trade(signal)

            self.assertIsNotNone(trade)
            parity = trade.decision_trace.get("parity_mode", {})
            self.assertTrue(parity.get("enabled"))
            self.assertTrue(parity.get("execution_revalidated"))
            self.assertEqual(parity.get("execution_snapshot_source"), "book")
            self.assertEqual(parity.get("execution_revalidation_outcome"), "approved")

    def test_create_trade_parity_mode_rejects_drift_and_persists_audit_metadata(self):
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
                    "max_entry_price": 0.70,
                }
            )
            sim.available_cash = 25.0
            sim.reserved_capital = 75.0
            sim.risk.state.available_cash = 25.0
            sim.risk.state.reserved_capital = 75.0

            signal = {
                "market_id": "test-parity-drift",
                "question": "Will test drift reject?",
                "exchange": "kalshi",
                "direction": "BUY_YES",
                "market_price": 0.40,
                "yes_price": 0.40,
                "no_price": 0.60,
                "best_yes_ask": 0.75,
                "best_no_ask": 0.25,
                "model_probability": 0.90,
                "edge": 0.30,
                "confidence": 0.9,
                "signals": {},
            }

            with patch.object(sim.kelly, "calculate", return_value=5.0):
                trade = sim._create_trade(signal)

            self.assertIsNotNone(trade)
            self.assertEqual(trade.integrity_status, "execution_rejected")
            self.assertEqual(trade.status, "rejected")
            self.assertEqual(trade.lifecycle_state, "revalidation_rejected")
            self.assertEqual(trade.failure_stage, "revalidation")
            self.assertEqual(trade.requested_size, 0.0)
            self.assertEqual(trade.approved_size, 0.0)
            self.assertEqual(trade.placed_size, 0.0)
            self.assertEqual(trade.filled_size, 0.0)
            self.assertEqual(trade.entry_price, 0.75)
            parity = trade.decision_trace.get("parity_mode", {})
            self.assertTrue(parity.get("enabled"))
            self.assertEqual(parity.get("execution_revalidation_outcome"), "rejected")
            self.assertEqual(parity.get("execution_snapshot_source"), "book")
            self.assertEqual(parity.get("original_decision_reason_code"), "approved")
            self.assertEqual(parity.get("execution_decision_reason_code"), "entry_price_above_cap")
            self.assertTrue(trade.parity_mode_enabled)
            self.assertTrue(trade.execution_revalidated)
            self.assertEqual(trade.execution_revalidation_outcome, "rejected")
            self.assertEqual(trade.execution_snapshot_source, "book")
            self.assertEqual(trade.original_decision_reason_code, "approved")
            self.assertEqual(trade.execution_decision_reason_code, "entry_price_above_cap")

    def test_paper_session_save_promotes_parity_fields_into_canonical_row(self):
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
                    "max_entry_price": 0.70,
                }
            )
            sim.available_cash = 25.0
            sim.reserved_capital = 75.0
            sim.risk.state.available_cash = 25.0
            sim.risk.state.reserved_capital = 75.0

            signal = {
                "market_id": "test-parity-save",
                "question": "Will saved paper rows keep parity fields?",
                "exchange": "kalshi",
                "direction": "BUY_YES",
                "market_price": 0.40,
                "yes_price": 0.40,
                "no_price": 0.60,
                "best_yes_ask": 0.41,
                "best_no_ask": 0.59,
                "model_probability": 0.90,
                "edge": 0.30,
                "confidence": 0.9,
                "signals": {},
            }

            with patch.object(sim.kelly, "calculate", return_value=5.0):
                trade = sim._create_trade(signal)

            self.assertIsNotNone(trade)
            sim.trades.append(trade)
            session_path = sim.session_store.save_session()
            payload = json.loads(Path(session_path).read_text())
            row = payload["trades"][0]
            self.assertEqual(row["schema_name"], "execution_audit_row")
            self.assertEqual(row["schema_version"], 1)
            self.assertTrue(row["parity_mode_enabled"])
            self.assertTrue(row["execution_revalidated"])
            self.assertEqual(row["execution_snapshot_source"], "book")
            self.assertIsNotNone(row["execution_snapshot"])
            self.assertEqual(row["trade_id"], trade.id)
            self.assertEqual(row["lifecycle_state"], "filled_open")

    def test_create_trade_parity_off_preserves_logic_only_flow(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sim = Simulator(
                {
                    "data_dir": tmpdir,
                    "enable_social": False,
                    "parity_mode": {
                        "enabled": False,
                        "record_revalidation_snapshot": True,
                        "require_book_prices": False,
                        "fallback_to_signal_prices": True,
                    },
                    "strategy": {
                        "enable_news": False,
                        "enable_social": False,
                        "enable_ai": False,
                    },
                    "max_entry_price": 0.70,
                }
            )
            sim.available_cash = 25.0
            sim.reserved_capital = 75.0
            sim.risk.state.available_cash = 25.0
            sim.risk.state.reserved_capital = 75.0

            signal = {
                "market_id": "test-parity-off",
                "question": "Will parity off preserve old flow?",
                "exchange": "kalshi",
                "direction": "BUY_YES",
                "market_price": 0.40,
                "yes_price": 0.40,
                "no_price": 0.60,
                "best_yes_ask": 0.75,
                "best_no_ask": 0.25,
                "model_probability": 0.90,
                "edge": 0.30,
                "confidence": 0.9,
                "signals": {},
            }

            with patch.object(sim.kelly, "calculate", return_value=5.0):
                trade = sim._create_trade(signal)

            self.assertIsNotNone(trade)
            self.assertEqual(trade.integrity_status, "ok")
            parity = trade.decision_trace.get("parity_mode", {})
            self.assertFalse(parity.get("enabled"))
            self.assertIsNone(parity.get("execution_revalidation_outcome"))

    def test_golden_parity_same_execution_snapshot_matches_live_reason_code(self):
        signal = {
            "exchange": "kalshi",
            "market_id": "m-golden",
            "question": "Will parity match?",
            "direction": "BUY_YES",
            "market_price": 0.40,
            "yes_price": 0.40,
            "no_price": 0.60,
            "model_probability": 0.70,
            "edge": 0.30,
            "confidence": 0.90,
            "signals": {},
        }
        exchange = StaticBookExchange()

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
            sim.available_cash = 25.0
            sim.reserved_capital = 0.0
            sim.risk.state.available_cash = 25.0
            sim.risk.state.reserved_capital = 0.0

            execution_snapshot = build_execution_snapshot(
                signal,
                direction="BUY_YES",
                bid_ask=exchange.get_market_bid_ask("m-golden"),
            )
            paper_context = sim.state_adapter.build_trade_context_from_snapshot(signal, execution_snapshot=execution_snapshot)
            paper_decision = build_trade_decision(
                paper_context,
                kelly_sizer=sim.kelly,
                risk_policy=sim.risk,
                min_edge=sim.min_edge,
                min_confidence=sim.min_confidence,
                max_entry_price=sim.max_entry_price,
            )

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
            live_signal = dict(signal)
            live_signal.update(execution_snapshot)
            live_context = adapter.build_trade_context(live_signal, exchange, bot.config)
            live_decision = build_trade_decision(
                live_context,
                kelly_sizer=bot.kelly,
                risk_policy=bot.risk,
                min_edge=0.01,
                min_confidence=0.5,
                max_entry_price=bot.config.get("max_entry_price", 0.70),
            )

        self.assertEqual(paper_decision.approved, live_decision.approved)
        self.assertEqual(paper_decision.reason_code, live_decision.reason_code)
        self.assertEqual(paper_decision.entry_price, live_decision.entry_price)

    def test_report_includes_parity_status_counts(self):
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
                    "max_entry_price": 0.70,
                }
            )
            sim.available_cash = 25.0
            sim.reserved_capital = 75.0
            sim.risk.state.available_cash = 25.0
            sim.risk.state.reserved_capital = 75.0

            approved_signal = {
                "market_id": "test-parity-report-approved",
                "question": "Will approved parity trade pass?",
                "exchange": "kalshi",
                "direction": "BUY_YES",
                "market_price": 0.04,
                "yes_price": 0.04,
                "no_price": 0.96,
                "best_yes_ask": 0.04,
                "best_no_ask": 0.96,
                "model_probability": 0.20,
                "edge": 0.16,
                "confidence": 0.9,
                "signals": {},
            }
            rejected_signal = {
                "market_id": "test-parity-report-rejected",
                "question": "Will rejected parity trade fail?",
                "exchange": "kalshi",
                "direction": "BUY_YES",
                "market_price": 0.40,
                "yes_price": 0.40,
                "no_price": 0.60,
                "best_yes_ask": 0.75,
                "best_no_ask": 0.25,
                "model_probability": 0.90,
                "edge": 0.30,
                "confidence": 0.9,
                "signals": {},
            }

            with patch.object(sim.kelly, "calculate", return_value=5.0):
                approved_trade = sim._create_trade(approved_signal)
                rejected_trade = sim._create_trade(rejected_signal)
                sim.trades.extend([approved_trade, rejected_trade])

            report = sim.report()
            self.assertTrue(report["parity_mode_enabled"])
            self.assertEqual(report["parity_revalidated_trades"], 2)
            self.assertEqual(report["parity_rejected_trades"], 1)
            self.assertEqual(report["parity_fallback_trades"], 0)
            self.assertIn("parity_summary", report)
            self.assertTrue(report["parity_summary"]["parity_mode_enabled"])
            self.assertEqual(report["parity_summary"]["parity_revalidated_trades"], 2)
            self.assertEqual(report["parity_summary"]["parity_rejected_trades"], 1)
            self.assertEqual(report["parity_summary"]["snapshot_source_counts"]["book"], 2)
            self.assertEqual(report["parity_summary"]["lifecycle_state_counts"]["filled_open"], 1)
            self.assertEqual(report["parity_summary"]["lifecycle_state_counts"]["revalidation_rejected"], 1)
            self.assertEqual(report["parity_summary"]["invalid_contract_rows"], 0)
            self.assertEqual(report["parity_summary"]["top_contract_issues"], [])
            self.assertEqual(report["parity_summary"]["top_execution_reason_codes"][0][0], "approved")

    def test_create_trade_parity_mode_can_reject_missing_book_when_required(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sim = Simulator(
                {
                    "data_dir": tmpdir,
                    "enable_social": False,
                    "parity_mode": {
                        "enabled": True,
                        "record_revalidation_snapshot": True,
                        "require_book_prices": True,
                        "fallback_to_signal_prices": False,
                    },
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
                "market_id": "test-parity-no-book",
                "question": "Will test settle YES?",
                "exchange": "kalshi",
                "direction": "BUY_YES",
                "model_probability": 0.20,
                "edge": 0.16,
                "confidence": 0.9,
                "signals": {},
            }

            with patch.object(sim.kelly, "calculate", return_value=5.0):
                trade = sim._create_trade(signal)

            self.assertIsNone(trade)
            self.assertNotIn("parity_book_prices_required", [getattr(t, "integrity_status", None) for t in sim.trades])

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
