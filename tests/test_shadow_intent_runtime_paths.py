import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot.file_ops import load_jsonl
from bot.runner import PredictionBot
from bot.simulator import Simulator


def _shadow_delta() -> dict:
    return {
        "schema_version": 1,
        "mode": "beta_shadow_delta",
        "status": "complete",
        "comparison_complete": True,
        "action_comparison_available": True,
        "policy": {
            "version": "beta",
            "mode": "shadow",
            "enabled_features": ["hidden_gem_lane_gates"],
        },
        "stable": {
            "action": "SKIP",
            "reason_code": "edge_below_threshold",
            "direction": "SKIP",
            "decision_type": "skip",
            "requested_position_size": None,
            "selected_lane": "edge",
        },
        "shadow": {
            "action": "BUY_YES",
            "reason_code": "approved",
            "direction": "BUY_YES",
            "decision_type": "buy_yes",
            "requested_position_size": 2.0,
            "selected_lane": "hidden_gem",
        },
        "changed": True,
        "action_changed": True,
        "side_changed": True,
        "buy_decision_changed": True,
        "reason_changed": True,
        "size_changed": True,
        "lane_changed": True,
        "dedupe_key": "KXTEST|run-1|beta-shadow",
        "evidence_sources": ["beta_lane_gate"],
    }


class _LiveExchange:
    def __init__(self):
        self.orders = []

    def get_balance(self):
        return 25.0

    def get_positions(self):
        return []

    def get_resting_orders(self):
        return []

    def place_order(self, market_id, side, price, size):
        self.orders.append({"market_id": market_id, "side": side, "price": price, "size": size})
        return SimpleNamespace(id="ord-1", status="open")


class ShadowIntentRuntimePathTests(unittest.TestCase):
    def test_paper_shadow_intent_append_does_not_mutate_trades_exposure_or_pnl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sim = Simulator(
                {
                    "data_dir": tmpdir,
                    "enable_social": False,
                    "strategy": {
                        "min_edge": 0.05,
                        "min_confidence": 0.5,
                        "enable_news": False,
                        "enable_social": False,
                        "enable_ai": False,
                    },
                }
            )
            before = {
                "trades": list(sim.trades),
                "balance": sim.balance,
                "available_cash": sim.available_cash,
                "reserved_capital": sim.reserved_capital,
                "risk_exposure": sim.risk.state.total_exposure,
                "risk_pnl": sim.risk.state.daily_pnl,
            }

            with patch("bot.simulator.build_shadow_delta", return_value=_shadow_delta()):
                row = sim._append_shadow_intent_if_any(
                    {"market_id": "KXTEST", "observed_at": "2026-05-08T00:00:00+00:00"},
                    {"market_id": "KXTEST"},
                )

            self.assertIsNotNone(row)
            self.assertEqual(sim.trades, before["trades"])
            self.assertEqual(sim.balance, before["balance"])
            self.assertEqual(sim.available_cash, before["available_cash"])
            self.assertEqual(sim.reserved_capital, before["reserved_capital"])
            self.assertEqual(sim.risk.state.total_exposure, before["risk_exposure"])
            self.assertEqual(sim.risk.state.daily_pnl, before["risk_pnl"])

            ledger_path = Path(tmpdir) / "paper" / "shadow_intents.jsonl"
            self.assertEqual(len(load_jsonl(ledger_path)), 1)
            self.assertFalse((Path(tmpdir) / "paper" / "trades.jsonl").exists())


    def test_paper_stable_skip_shadow_intent_append_does_not_mutate_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sim = Simulator(
                {
                    "data_dir": tmpdir,
                    "enable_social": False,
                    "strategy": {
                        "min_edge": 0.05,
                        "min_confidence": 0.5,
                        "enable_news": False,
                        "enable_social": False,
                        "enable_ai": False,
                    },
                    "strategy_policy": {
                        "version": "beta",
                        "beta": {"mode": "shadow", "features": {"hidden_gem_lane_gates": True}},
                    },
                    "strategy_lanes": {
                        "enabled": True,
                        "confidence_slow_profit": {"enabled": True, "min_edge": 0.02, "min_confidence": 0.75},
                    },
                }
            )
            before = {
                "trades": list(sim.trades),
                "balance": sim.balance,
                "available_cash": sim.available_cash,
                "reserved_capital": sim.reserved_capital,
                "risk_exposure": sim.risk.state.total_exposure,
                "risk_pnl": sim.risk.state.daily_pnl,
            }
            signal = {
                "exchange": "kalshi",
                "market_id": "KXHIGHNY-26MAY08-T71",
                "question": "Will the high temperature in New York exceed 71 degrees?",
                "direction": "BUY_YES",
                "market_price": 0.5,
                "yes_price": 0.5,
                "no_price": 0.5,
                "model_probability": 0.54,
                "edge": 0.04,
                "confidence": 0.8,
                "category": "weather",
            }

            row = sim._append_shadow_intent_for_stable_skip(signal, "edge_below_threshold")

            self.assertIsNotNone(row)
            self.assertEqual(sim.trades, before["trades"])
            self.assertEqual(sim.balance, before["balance"])
            self.assertEqual(sim.available_cash, before["available_cash"])
            self.assertEqual(sim.reserved_capital, before["reserved_capital"])
            self.assertEqual(sim.risk.state.total_exposure, before["risk_exposure"])
            self.assertEqual(sim.risk.state.daily_pnl, before["risk_pnl"])

            ledger_path = Path(tmpdir) / "paper" / "shadow_intents.jsonl"
            rows = load_jsonl(ledger_path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["shadow_intent"]["intent_kind"], "unknown")
            self.assertFalse((Path(tmpdir) / "paper" / "trades.jsonl").exists())

    def test_live_shadow_intent_on_stable_skip_does_not_place_order_or_mutate_live_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
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
                    "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
                    "max_tradable_balance_usd": 10.0,
                    "max_position_size_usd": 4.0,
                }
            )
            exchange = _LiveExchange()
            bot.exchanges["kalshi"] = exchange
            bot.risk.state.current_balance = 25.0
            bot.risk.state.available_cash = 25.0
            decision = SimpleNamespace(
                action="BUY_YES",
                approved=False,
                position_size=0.0,
                requested_position_size=2.0,
                entry_price=0.4,
                win_probability=0.7,
                reason="stable skip",
                reason_code="edge_below_threshold",
                reasoning={},
                warnings=[],
            )
            signal = {
                "exchange": "kalshi",
                "market_id": "KXHIGHNY-260508-T71",
                "question": "Will the high temperature in New York exceed 71 degrees?",
                "direction": "BUY_YES",
                "market_price": 0.4,
                "yes_price": 0.4,
                "no_price": 0.6,
                "model_probability": 0.7,
                "edge": 0.3,
                "confidence": 0.9,
                "category": "KXHIGHNY",
                "market_family": "daily_temperature",
            }

            with patch("bot.runner.build_trade_decision", return_value=decision), patch(
                "bot.runner.build_shadow_delta", return_value=_shadow_delta()
            ):
                result = bot._process_signal(signal)

            self.assertEqual(result["blocked_reason"], "edge_below_threshold")
            self.assertEqual(exchange.orders, [])
            self.assertEqual(bot.trade_history, [])
            self.assertEqual(bot.open_positions, [])
            self.assertEqual(bot.open_orders, [])
            self.assertEqual(bot.risk.state.total_exposure, 0.0)
            ledger_path = Path(tmpdir) / "live" / "shadow_intents.jsonl"
            self.assertEqual(len(load_jsonl(ledger_path)), 1)
            self.assertFalse((Path(tmpdir) / "live" / "trades.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
