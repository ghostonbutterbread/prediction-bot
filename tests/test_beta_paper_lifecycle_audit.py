"""Actual paper lifecycle regressions: only external transport is faked."""

import json
import os
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot.simulator import Simulator
from bot.strategies.enhanced import KellySizer


class SettlementExchange:
    def __init__(self, market_id, *, result="YES", status="settled", yes_price=1.0, **metadata):
        self.market = SimpleNamespace(
            id=market_id,
            metadata={"status": status, "result": result, **metadata},
            yes_price=yes_price,
            no_price=1 - yes_price,
            close_price=None,
            closes_at=None,
        )
        self.calls = 0

    def get_market(self, market_id):
        assert market_id == self.market.id
        self.calls += 1
        return self.market


class BetaPaperLifecycleAuditTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="paper-lifecycle-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)
        self.enterContext(patch.dict(os.environ, {"PAPER_MODE": "true", "KALSHI_USE_DEMO": "true"}))
        for key in (
            "KALSHI_FEE_RATE", "KELLY_FRACTION", "MAX_BET_PCT", "MAX_DRAWDOWN_PCT",
            "FORCE_RESUME", "MAX_POSITION_SIZE_USD", "MAX_TRADABLE_BALANCE_USD",
        ):
            os.environ.pop(key, None)
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("network forbidden")))

    def config(self, name="wallet", **overrides):
        return {
            "data_dir": str(self.root / name),
            "starting_balance": 100.0,
            "trading": {"mode": "paper"},
            "enable_social": False,
            "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
            **overrides,
        }

    def signal(self, direction="BUY_YES", probability=0.7, price=0.4, market_id="KXHIGHNY-26SEP07-T80"):
        return {
            "market_id": market_id,
            "question": "Will NYC high temperature be above 80 degrees?",
            "series_ticker": "KXHIGHNY",
            "event_ticker": market_id.rsplit("-", 1)[0],
            "exchange": "kalshi",
            "direction": direction,
            "model_probability": probability,
            "market_price": price,
            "yes_price": price if direction == "BUY_YES" else 1 - price,
            "no_price": price if direction == "BUY_NO" else 1 - price,
            "best_yes_ask": price if direction == "BUY_YES" else 1 - price,
            "best_no_ask": price if direction == "BUY_NO" else 1 - price,
            "edge": (probability if direction == "BUY_YES" else 1 - probability) - price,
            "confidence": 0.9,
            "signals": {},
        }

    def submit(self, sim, **signal_overrides):
        blockers = Counter()
        trade = sim.submit_paper_signal(self.signal(**signal_overrides), blockers)
        assert trade is not None, dict(blockers)
        return trade

    def persisted(self, sim):
        return json.loads((sim.data_dir / f"sim_{sim.session_id}.json").read_text())

    def test_resolved_kelly_controls_actual_reservation(self):
        for fraction in (0.01, 0.5):
            with self.subTest(fraction=fraction):
                sim = Simulator(self.config(str(fraction), risk={"kelly_fraction": fraction, "max_bet_pct": 0.01}))
                blockers = Counter()
                trade = sim.submit_paper_signal(self.signal(probability=0.6, price=0.5), blockers)
                expected = KellySizer(kelly_fraction=fraction, max_bet_pct=0.01).calculate(0.6, 0.5, 100)
                self.assertEqual(trade.position_size if trade else 0, expected)
                self.assertEqual(sim.reserved_capital, expected)
                self.assertEqual(sim.available_cash, 100 - expected)
                self.assertEqual(sim.risk.state.total_exposure, expected)
                sim.session_store.save_session()
                reloaded = Simulator(sim.config, load_from=sim.session_id)
                self.assertEqual(reloaded.reserved_capital, expected)
                self.assertEqual(reloaded.available_cash, 100 - expected)

    def test_void_releases_reservation_without_economic_result(self):
        for direction, probability in (("BUY_YES", 0.7), ("BUY_NO", 0.3)):
            for status, result in (("settled", "VOID"), ("cancelled", ""), ("closed", "cancelled")):
                with self.subTest(direction=direction, status=status):
                    sim = Simulator(self.config(f"{direction}-{status}"))
                    trade = self.submit(sim, direction=direction, probability=probability)
                    reserved = trade.position_size
                    exchange = SettlementExchange(trade.market_id, status=status, result=result)
                    events = sim.resolve_open_positions(exchange)
                    self.assertEqual(len(events), 1)
                    self.assertEqual(events[0].outcome, "VOID")
                    self.assertEqual(events[0].status, "void_resolution")
                    self.assertIsNone(events[0].pnl)
                    self.assertEqual(events[0].settlement_value, reserved)  # refund, not winnings
                    sim.session_store.save_session()
                    reloaded = Simulator(sim.config, load_from=sim.session_id)
                    self.assertEqual(reloaded.resolve_open_positions(exchange), [])
                    self.assertEqual(exchange.calls, 1)
                    for wallet in (sim, reloaded):
                        self.assertEqual(wallet.balance, 100)
                        self.assertEqual(wallet.available_cash, 100)
                        self.assertEqual(wallet.reserved_capital, 0)
                        self.assertEqual(wallet.list_open_positions(), [])
                        self.assertEqual(wallet.risk.state.total_exposure, 0)
                        self.assertEqual(wallet.risk.state.open_positions, 0)
                        self.assertEqual(wallet.risk.state.daily_pnl, 0)
                        self.assertEqual(wallet.risk.state.consecutive_wins, 0)
                        self.assertEqual(wallet.risk.state.consecutive_losses, 0)
                        self.assertEqual(wallet.report()["resolved_events"], 0)
                        self.assertEqual(wallet.report()["invalid_resolved_trades"], 0)
                    row = self.persisted(reloaded)["trades"][0]
                    self.assertEqual(row["integrity_status"], "ok")
                    self.assertEqual(row["resolution_result"], "void")
                    for key in ("pnl", "net_pnl", "gross_pnl", "expected_pnl", "exit_price", "unrealized_pnl"):
                        self.assertIsNone(row[key], key)
                    self.assertEqual(row["fee_paid"], 0)

    def test_void_does_not_change_prior_win_streak_or_report_denominator(self):
        sim = Simulator(self.config())
        winner = self.submit(sim)
        sim.resolve_open_positions(SettlementExchange(winner.market_id))
        before = (sim.balance, sim.risk.state.daily_pnl, sim.risk.state.consecutive_wins)
        trade = self.submit(sim, market_id="KXHIGHNY-26SEP08-T80")
        sim.resolve_open_positions(SettlementExchange(trade.market_id, result="VOID"))
        self.assertTrue(sim.trades[-1].resolved)
        self.assertEqual((sim.balance, sim.risk.state.daily_pnl, sim.risk.state.consecutive_wins), before)
        self.assertEqual(sim.risk.state.win_rate, 1.0)
        report = self.persisted(sim)["report"]
        self.assertEqual(report["wins"], 1)
        self.assertEqual(report["losses"], 0)
        self.assertEqual(report["win_rate"], 1)
        self.assertEqual(report["resolved_events"], 1)
        self.assertEqual(report["invalid_resolved_trades"], 0)


    def test_mark_to_market_uses_selected_side_price_for_both_sides(self):
        for direction, probability in (("BUY_YES", 0.7), ("BUY_NO", 0.3)):
            for current_price in (0.6, 0.2):
                for status in ("open", "closed"):
                    with self.subTest(direction=direction, current_price=current_price, status=status):
                        sim = Simulator(self.config(f"{direction}-{current_price}-{status}"))
                        trade = self.submit(sim, direction=direction, probability=probability)
                        exchange = SettlementExchange(
                            trade.market_id, result="", status=status,
                            yes_price=1 - current_price if direction == "BUY_NO" else current_price,
                        )
                        if status == "closed":
                            exchange.market.closes_at = datetime.now(timezone.utc) - timedelta(days=1)
                        self.assertEqual(sim.resolve_open_positions(exchange), [])
                        expected = round(trade.position_size / trade.entry_price * (current_price - trade.entry_price), 4)
                        row = self.persisted(sim)["trades"][0]
                        self.assertAlmostEqual(row["unrealized_pnl"], expected)
                        self.assertAlmostEqual(row["price_delta"], current_price - trade.entry_price)
                        reloaded = Simulator(sim.config, load_from=sim.session_id)
                        self.assertAlmostEqual(reloaded.list_open_positions()[0].unrealized_pnl, expected)
                        self.assertEqual(reloaded.balance, 100)
                        self.assertEqual(reloaded.reserved_capital, trade.position_size)

    def test_entry_fee_is_preserved_through_settlement_audit_and_reload(self):
        for fee_rate in (0.0, 0.12):
            for direction, probability, outcome in (("BUY_YES", 0.7, "YES"), ("BUY_NO", 0.3, "NO")):
                with self.subTest(fee_rate=fee_rate, direction=direction):
                    sim = Simulator(self.config(f"{fee_rate}-{direction}", kalshi_fee_rate=fee_rate))
                    trade = self.submit(sim, direction=direction, probability=probability)
                    assert trade.entry_price is not None
                    gross = trade.position_size / trade.entry_price * (1 - trade.entry_price)
                    expected = round(gross * (1 - fee_rate), 4)
                    exchange = SettlementExchange(trade.market_id, result=outcome)
                    events = sim.resolve_open_positions(exchange)
                    self.assertAlmostEqual(events[0].pnl, expected)
                    self.assertEqual(sim.kelly.fee_rate, fee_rate)
                    changed_config = {**sim.config, "kalshi_fee_rate": 0.31}
                    reloaded = Simulator(changed_config, load_from=sim.session_id)
                    reloaded.session_store.save_session()
                    self.assertEqual(reloaded.resolve_open_positions(exchange), [])
                    row = self.persisted(reloaded)["trades"][0]
                    self.assertEqual(row["fee_rate"], fee_rate)
                    self.assertAlmostEqual(row["fee_paid"], round(gross * fee_rate, 4))
                    self.assertAlmostEqual(row["expected_pnl"], expected)
                    self.assertAlmostEqual(row["net_pnl"], expected)
                    self.assertEqual(row["integrity_status"], "ok")
                    self.assertEqual(reloaded.balance, round(100 + expected, 2))
                    self.assertEqual(reloaded.available_cash, reloaded.balance)
                    self.assertEqual(reloaded.reserved_capital, 0)
                    self.assertAlmostEqual(reloaded.risk.state.daily_pnl, expected)
                    self.assertEqual(reloaded.report()["invalid_resolved_trades"], 0)

    def test_env_fee_and_entry_fee_survive_reload_before_standalone_resolution(self):
        from bot.resolver import TradeResolver

        with patch.dict(os.environ, {"KALSHI_FEE_RATE": "0.2"}):
            sim = Simulator(self.config())
        trade = self.submit(sim, probability=0.6, price=0.5)
        expected_size = KellySizer(
            kelly_fraction=sim.risk.kelly_fraction, max_bet_pct=sim.risk.max_bet_pct, fee_rate=0.2,
        ).calculate(0.6, 0.5, 100)
        self.assertEqual(trade.position_size, expected_size)
        sim.session_store.save_session()
        reloaded = Simulator({**sim.config, "kalshi_fee_rate": 0.0}, load_from=sim.session_id)
        exchange = SettlementExchange(trade.market_id)
        resolver = TradeResolver(str(reloaded.data_dir))
        summary = resolver.resolve_session(reloaded.session_id, exchange, reloaded.risk)
        expected_pnl = round(expected_size * 0.8, 4)
        self.assertAlmostEqual(summary["session_pnl"], expected_pnl)
        again = resolver.resolve_session(reloaded.session_id, exchange, reloaded.risk)
        self.assertEqual(again["resolved_this_pass"], 0)
        self.assertEqual(again["session_pnl"], summary["session_pnl"])
        self.assertEqual(exchange.calls, 1)
        reloaded = Simulator(reloaded.config, load_from=reloaded.session_id)
        self.assertAlmostEqual(reloaded.trades[0].net_pnl, expected_pnl)
        self.assertEqual(reloaded.report()["invalid_resolved_trades"], 0)

    def test_explicit_settlement_not_after_entry_keeps_cash_reserved(self):
        for field in ("settlement_ts", "outcome_known_at"):
            for outcome in ("YES", "VOID"):
                for delta in (timedelta(days=-1), timedelta(0)):
                    with self.subTest(field=field, outcome=outcome, delta=delta):
                        sim = Simulator(self.config(f"{field}-{outcome}-{delta.days}"))
                        trade = self.submit(sim)
                        timestamp = (datetime.fromisoformat(trade.timestamp) + delta).isoformat()
                        exchange = SettlementExchange(trade.market_id, result=outcome, **{field: timestamp})
                        self.assertEqual(sim.resolve_open_positions(exchange), [])
                        self.assertEqual(sim.available_cash, 100 - trade.position_size)
                        self.assertEqual(sim.reserved_capital, trade.position_size)
                        self.assertEqual(sim.risk.state.open_positions, 1)
                        self.assertEqual(sim.risk.state.daily_pnl, 0)
                        row = self.persisted(sim)["trades"][0]
                        self.assertFalse(row["resolved"])
                        self.assertIsNone(row["resolved_at"])
                        self.assertEqual(row[field], timestamp)
                        self.assertEqual(row["resolution_blocker"], "settlement_not_after_entry")
                        # A later corrected authoritative receipt can settle exactly once.
                        later = (datetime.fromisoformat(trade.timestamp) + timedelta(seconds=1)).isoformat()
                        reloaded = Simulator(sim.config, load_from=sim.session_id)
                        corrected = SettlementExchange(trade.market_id, result=outcome, **{field: later})
                        events = reloaded.resolve_open_positions(corrected)
                        self.assertEqual(len(events), 1)
                        self.assertEqual(events[0].metadata[field], later)
                        self.assertIsNone(self.persisted(reloaded)["trades"][0]["resolution_blocker"])
                        self.assertEqual(reloaded.resolve_open_positions(corrected), [])

    def test_settlement_time_is_preserved_without_inventing_missing_fields(self):
        for field in (None, "settlement_ts", "outcome_known_at"):
            with self.subTest(field=field):
                sim = Simulator(self.config(str(field)))
                trade = self.submit(sim)
                later = (datetime.fromisoformat(trade.timestamp) + timedelta(seconds=1)).isoformat()
                metadata = {field: later} if field else {}
                exchange = SettlementExchange(trade.market_id, **metadata)
                events = sim.resolve_open_positions(exchange)
                self.assertEqual(len(events), 1)
                sim.session_store.save_session()
                reloaded = Simulator(sim.config, load_from=sim.session_id)
                row = self.persisted(reloaded)["trades"][0]
                for name in ("settlement_ts", "outcome_known_at"):
                    self.assertEqual(row.get(name), later if name == field else None)
                    self.assertEqual(events[0].metadata.get(name), later if name == field else None)
                self.assertIsNotNone(row["resolved_at"])  # retrieval receipt, not authoritative time
                self.assertEqual(reloaded.report()["invalid_resolved_trades"], 0)

    def test_prediction_lab_configured_kelly_keeps_stateless_opportunity_semantics(self):
        from bot.decision_pipeline import DecisionPipelineInput, FixedOpportunityRiskPolicy
        from bot.prediction_lab import PredictionLab
        from bot.shared_core import build_execution_snapshot, build_trade_decision

        for fraction, max_bet_pct in ((0.01, 0.01), (0.5, 0.01), (0.8, 0.2)):
            with self.subTest(fraction=fraction, max_bet_pct=max_bet_pct):
                config = self.config(
                    f"lab-{fraction}-{max_bet_pct}",
                    risk={"kelly_fraction": fraction, "max_bet_pct": max_bet_pct, "max_event_exposure_pct": 0.5},
                    prediction_lab={"use_shared_pipeline": True, "hypothetical_notional_mode": "fresh_kelly"},
                )
                lab = PredictionLab(config)
                signal = self.signal(probability=0.6, price=0.5)
                market = SimpleNamespace(id=signal["market_id"], question=signal["question"], exchange="kalshi", yes_price=0.5, no_price=0.5, metadata={})
                account = lab.opportunity_account_provider.get_account_state()
                evaluator = lab.decision_evaluator
                assert evaluator is not None
                context = evaluator._build_trade_context(
                    DecisionPipelineInput(market, account, config_snapshot=config), signal,
                    source_context={}, execution_snapshot=build_execution_snapshot(signal, direction="BUY_YES"),
                )
                decision = build_trade_decision(
                    context, kelly_sizer=evaluator.kelly_sizer, risk_policy=evaluator.risk_policy,
                    min_edge=0.01, min_confidence=0.5, max_entry_price=0.7,
                )
                expected = KellySizer(kelly_fraction=fraction, max_bet_pct=max_bet_pct).calculate(0.6, 0.5, 100)
                self.assertEqual(decision.reasoning["kelly"]["requested_size"], expected)
                # Independent weather/event caps may still reduce the approved size.
                self.assertLessEqual(decision.position_size or 0.0, expected)
                self.assertEqual(lab._build_hypothetical_metadata(market, signal)["position_size_usd"], expected)
                self.assertIsInstance(evaluator.risk_policy, FixedOpportunityRiskPolicy)
                self.assertEqual(lab.opportunity_account_provider.get_account_state(), account)
                self.assertEqual(account.available_cash, 100)
                # Explicit flat diagnostic must remain fixed-notional, not Kelly.
                flat = PredictionLab({**config, "prediction_lab": {"hypothetical_notional_mode": "flat", "flat_notional_usd": 7}})
                self.assertEqual(flat._build_hypothetical_metadata(market, signal)["position_size_usd"], 7)
                self.assertEqual(list(self.root.rglob("risk_state.json")), [])

    def test_kelly_resolution_preserves_precedence_and_presets(self):
        from bot.risk import LIVE_LIMITS, PAPER_LIMITS, resolve_kelly_limits

        for preset in (PAPER_LIMITS, LIVE_LIMITS):
            with self.subTest(preset=preset):
                self.assertEqual(resolve_kelly_limits({}, preset=preset), (preset["kelly_fraction"], preset["max_bet_pct"]))
                config: dict = {"kelly_fraction": 0.4, "max_bet_pct": 0.3}
                self.assertEqual(resolve_kelly_limits(config, preset=preset), (0.4, 0.3))
                config["risk"] = {"kelly_fraction": 0.0, "max_bet_pct": 0.0}
                self.assertEqual(resolve_kelly_limits(config, preset=preset), (0.0, 0.0))
                with patch.dict(os.environ, {"KELLY_FRACTION": "0.01", "MAX_BET_PCT": "0.02"}):
                    self.assertEqual(resolve_kelly_limits(config, preset=preset), (0.01, 0.02))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_prediction_lab_kelly_environment_matches_simulator(self):
        from bot.prediction_lab import PredictionLab
        from bot.risk import RiskManager

        cases = (
            ({"KELLY_FRACTION": "0.01", "MAX_BET_PCT": "0.01"}, 0.01, 0.01),
            ({"KELLY_FRACTION": "0.5", "MAX_BET_PCT": "0.01"}, 0.5, 0.01),
            ({"KELLY_FRACTION": "0.01"}, 0.01, 0.2),
            ({"MAX_BET_PCT": "0.01"}, 0.8, 0.01),
            ({}, 0.8, 0.2),
        )
        for index, (environment, fraction, max_bet_pct) in enumerate(cases):
            with self.subTest(environment=environment), patch.dict(os.environ, environment):
                config = self.config(
                    f"env-{index}", kelly_fraction=0.4, max_bet_pct=0.3,
                    risk={"kelly_fraction": 0.8, "max_bet_pct": 0.2},
                    prediction_lab={"use_shared_pipeline": True, "hypothetical_notional_mode": "fresh_kelly"},
                )
                sim = Simulator(config)
                with patch.object(RiskManager, "__init__", side_effect=AssertionError("lab must remain stateless")):
                    lab = PredictionLab(config)
                expected = KellySizer(kelly_fraction=fraction, max_bet_pct=max_bet_pct).calculate(0.6, 0.5, 100)
                self.assertEqual(sim.kelly.calculate(0.6, 0.5, 100), expected)
                signal = self.signal(probability=0.6, price=0.5)
                market = SimpleNamespace(id=signal["market_id"], question=signal["question"], exchange="kalshi", yes_price=0.5, no_price=0.5, metadata={})
                self.assertEqual(lab._build_hypothetical_metadata(market, signal)["position_size_usd"], expected)
                self.assertEqual(lab.kelly.fraction, fraction)
                self.assertEqual(lab.kelly.max_bet_pct, max_bet_pct)
                assert lab.decision_evaluator is not None
                self.assertEqual(lab.decision_evaluator.kelly_sizer.calculate(0.6, 0.5, 100), expected)
                self.assertEqual(lab.decision_evaluator.risk_policy.max_bet_pct, max_bet_pct)
                self.assertEqual(lab.opportunity_account_provider.get_account_state().available_cash, 100)

    def test_binary_outcomes_release_only_their_own_overlapping_reservation(self):
        for direction, probability in (("BUY_YES", 0.7), ("BUY_NO", 0.3)):
            for outcome in ("YES", "NO"):
                with self.subTest(direction=direction, outcome=outcome):
                    sim = Simulator(self.config(f"binary-{direction}-{outcome}"))
                    first = self.submit(sim, direction=direction, probability=probability)
                    second = self.submit(sim, market_id="KXHIGHNY-26SEP08-T80")
                    reserved = first.position_size + second.position_size
                    self.assertEqual(sim.reserved_capital, reserved)
                    self.assertEqual(sim.available_cash, 100 - reserved)
                    self.assertEqual(sim.risk.state.open_positions, 2)
                    # Real duplicate/event risk controls still reject another entry.
                    self.assertIsNone(sim.submit_paper_signal(self.signal(direction, probability)))
                    self.assertEqual(sim.reserved_capital, reserved)
                    receipt = SettlementExchange(first.market_id, result=outcome).market
                    source = SimpleNamespace(get_market=lambda market_id: receipt if market_id == first.market_id else None)
                    events = sim.resolve_open_positions(source)
                    won = (direction == "BUY_YES" and outcome == "YES") or (direction == "BUY_NO" and outcome == "NO")
                    assert first.entry_price is not None
                    pnl = round(first.position_size / first.entry_price * (1 - first.entry_price) * 0.93, 4) if won else -first.position_size
                    self.assertEqual(len(events), 1)
                    self.assertEqual(events[0].pnl, pnl)
                    self.assertEqual(sim.reserved_capital, second.position_size)
                    self.assertEqual(sim.balance, round(100 + pnl, 2))
                    self.assertEqual(sim.available_cash, round(sim.balance - second.position_size, 2))
                    self.assertEqual(sim.risk.state.open_positions, 1)
                    self.assertEqual(sim.risk.state.daily_pnl, pnl)
                    self.assertEqual(sim.resolve_open_positions(source), [])
                    reloaded = Simulator(sim.config, load_from=sim.session_id)
                    self.assertEqual(reloaded.reserved_capital, second.position_size)
                    reloaded.resolve_open_positions(SettlementExchange(second.market_id, result="VOID"))
                    self.assertEqual(reloaded.available_cash, round(100 + pnl, 2))
                    self.assertEqual(reloaded.reserved_capital, 0)
                    self.assertEqual(reloaded.risk.state.total_exposure, 0)
                    self.assertEqual(reloaded.risk.state.daily_pnl, pnl)
                    self.assertEqual(reloaded.risk.state.consecutive_wins, int(won))
                    self.assertEqual(reloaded.risk.state.consecutive_losses, int(not won))
                    self.assertEqual(reloaded.report()["invalid_resolved_trades"], 0)


if __name__ == "__main__":
    unittest.main()
