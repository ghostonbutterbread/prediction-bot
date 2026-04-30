import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from bot.resolver import TradeResolver
import scripts.analyze as analyze_script


class FakeMarket:
    def __init__(
        self,
        *,
        status: str = "settled",
        result: str = "YES",
        yes_price: float = 1.0,
        no_price: float = 0.0,
        close_price: float = 1.0,
    ):
        self.metadata = {"status": status, "result": result}
        self.yes_price = yes_price
        self.no_price = no_price
        self.close_price = close_price
        self.closes_at = datetime.now(timezone.utc)


class FakeExchange:
    def __init__(self, markets: dict[str, FakeMarket]):
        self.markets = markets

    def get_market(self, market_id: str):
        return self.markets.get(market_id)


class PaperAccountingTests(unittest.TestCase):
    def test_resolver_backfills_accounting_fields_and_event_summary_for_laddered_weather_positions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session_id = "20260416_120000"
            session_path = Path(tmpdir) / f"sim_{session_id}.json"
            session_path.write_text(
                json.dumps(
                    {
                        "session_id": session_id,
                        "starting_balance": 100.0,
                        "balance": 100.0,
                        "trades": [
                            {
                                "id": "t1",
                                "timestamp": "2026-04-16T12:00:00+00:00",
                                "exchange": "kalshi",
                                "market_id": "KXHIGHNY-26APR16-T70",
                                "question": "Will the high temp in NYC be <70° on Apr 16, 2026?",
                                "direction": "BUY_YES",
                                "model_probability": 0.7,
                                "market_price": 0.5,
                                "edge": 0.2,
                                "confidence": 0.9,
                                "position_size": 5.0,
                                "signals": {},
                                "resolved": False,
                            },
                            {
                                "id": "t2",
                                "timestamp": "2026-04-16T12:05:00+00:00",
                                "exchange": "kalshi",
                                "market_id": "KXHIGHNY-26APR16-T72",
                                "question": "Will the high temp in NYC be <72° on Apr 16, 2026?",
                                "direction": "BUY_YES",
                                "model_probability": 0.75,
                                "market_price": 0.25,
                                "edge": 0.3,
                                "confidence": 0.9,
                                "position_size": 5.0,
                                "signals": {},
                                "resolved": False,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            resolver = TradeResolver(tmpdir)
            summary = resolver.resolve_session(
                session_id,
                FakeExchange(
                    {
                        "KXHIGHNY-26APR16-T70": FakeMarket(result="YES"),
                        "KXHIGHNY-26APR16-T72": FakeMarket(result="YES"),
                    }
                ),
            )

            session = json.loads(session_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["resolved_this_pass"], 2)
            self.assertEqual(summary["trusted_resolved"], 2)
            self.assertAlmostEqual(summary["session_pnl"], 18.6)

            for trade in session["trades"]:
                self.assertEqual(trade["integrity_status"], "ok")
                self.assertEqual(trade["event_key"], "KXHIGHNY-26APR16")
                self.assertIn("contracts", trade)
                self.assertIn("gross_pnl", trade)
                self.assertIn("fee_paid", trade)
                self.assertIn("net_pnl", trade)

            report = session["report"]
            self.assertEqual(report["resolved_trades"], 2)
            self.assertEqual(report["trusted_resolved_trades"], 2)
            self.assertEqual(report["invalid_resolved_trades"], 0)
            self.assertEqual(report["resolved_events"], 1)
            self.assertEqual(report["event_wins"], 1)
            self.assertAlmostEqual(report["total_realized_pnl"], 18.6)
            self.assertAlmostEqual(session["balance"], 118.6)
            self.assertAlmostEqual(session["available_cash"], 118.6)
            self.assertAlmostEqual(session["reserved_capital"], 0.0)
            self.assertAlmostEqual(report["available_cash"], 118.6)
            self.assertAlmostEqual(report["reserved_capital"], 0.0)

    def test_resolver_releases_reserved_capital_when_trade_settles(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session_id = "20260416_121500"
            session_path = Path(tmpdir) / f"sim_{session_id}.json"
            session_path.write_text(
                json.dumps(
                    {
                        "session_id": session_id,
                        "starting_balance": 100.0,
                        "balance": 100.0,
                        "available_cash": 90.0,
                        "reserved_capital": 10.0,
                        "trades": [
                            {
                                "id": "t1",
                                "timestamp": "2026-04-16T12:00:00+00:00",
                                "exchange": "kalshi",
                                "market_id": "KXHIGHNY-26APR16-T70",
                                "question": "Will the high temp in NYC be <70° on Apr 16, 2026?",
                                "direction": "BUY_YES",
                                "model_probability": 0.7,
                                "market_price": 0.5,
                                "edge": 0.2,
                                "confidence": 0.9,
                                "position_size": 10.0,
                                "reserved_capital": 10.0,
                                "available_cash_before": 100.0,
                                "available_cash_after_entry": 90.0,
                                "signals": {},
                                "resolved": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            resolver = TradeResolver(tmpdir)
            summary = resolver.resolve_session(
                session_id,
                FakeExchange({"KXHIGHNY-26APR16-T70": FakeMarket(result="YES")}),
            )

            session = json.loads(session_path.read_text(encoding="utf-8"))
            [trade] = session["trades"]

            self.assertTrue(trade["resolved"])
            self.assertAlmostEqual(trade["pnl"], 9.3)
            self.assertAlmostEqual(trade["settlement_value"], 19.3)
            self.assertAlmostEqual(summary["balance"], 109.3)
            self.assertAlmostEqual(summary["available_cash"], 109.3)
            self.assertAlmostEqual(summary["reserved_capital"], 0.0)
            self.assertAlmostEqual(session["balance"], 109.3)
            self.assertAlmostEqual(session["available_cash"], 109.3)
            self.assertAlmostEqual(session["reserved_capital"], 0.0)

    def test_resolver_settles_kalshi_finalized_market_with_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session_id = "20260423_120000"
            session_path = Path(tmpdir) / f"sim_{session_id}.json"
            session_path.write_text(
                json.dumps(
                    {
                        "session_id": session_id,
                        "starting_balance": 100.0,
                        "balance": 100.0,
                        "available_cash": 90.0,
                        "reserved_capital": 10.0,
                        "trades": [
                            {
                                "id": "t1",
                                "timestamp": "2026-04-23T12:00:00+00:00",
                                "exchange": "kalshi",
                                "market_id": "KXHIGHLAX-26APR23-T75",
                                "question": "Will the high temp in LA be >75° on Apr 23, 2026?",
                                "direction": "BUY_YES",
                                "model_probability": 0.7,
                                "market_price": 0.5,
                                "edge": 0.2,
                                "confidence": 0.9,
                                "position_size": 10.0,
                                "reserved_capital": 10.0,
                                "signals": {},
                                "resolved": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            resolver = TradeResolver(tmpdir)
            summary = resolver.resolve_session(
                session_id,
                FakeExchange({"KXHIGHLAX-26APR23-T75": FakeMarket(status="finalized", result="no")}),
            )

            session = json.loads(session_path.read_text(encoding="utf-8"))
            [trade] = session["trades"]

            self.assertEqual(summary["resolved_this_pass"], 1)
            self.assertTrue(trade["resolved"])
            self.assertEqual(trade["outcome"], "NO")
            self.assertAlmostEqual(trade["pnl"], -10.0)
            self.assertAlmostEqual(summary["reserved_capital"], 0.0)

    def test_resolver_normalizes_explicit_results_and_settled_close_prices(self):
        resolver = TradeResolver()

        explicit_yes = FakeMarket(status="closed", result="YES", close_price=None)
        explicit_no = FakeMarket(status="closed", result="no", close_price=None)
        settled_yes = FakeMarket(status="settled", result="", close_price=1.0)
        settled_no = FakeMarket(status="finalized", result="", close_price=0.0)

        self.assertTrue(resolver._has_result(explicit_yes))
        self.assertTrue(resolver._has_result(explicit_no))
        self.assertTrue(resolver._has_result(settled_yes))
        self.assertTrue(resolver._has_result(settled_no))
        self.assertEqual(resolver._determine_outcome(explicit_yes), "YES")
        self.assertEqual(resolver._determine_outcome(explicit_no), "NO")
        self.assertEqual(resolver._determine_outcome(settled_yes), "YES")
        self.assertEqual(resolver._determine_outcome(settled_no), "NO")

    def test_resolver_does_not_treat_bot_relative_result_as_market_outcome(self):
        resolver = TradeResolver()

        settled_won = FakeMarket(status="settled", result="won", close_price=None)
        settled_lost = FakeMarket(status="settled", result="lost", close_price=None)

        self.assertFalse(resolver._has_result(settled_won))
        self.assertFalse(resolver._has_result(settled_lost))
        self.assertEqual(resolver._determine_outcome(settled_won), "UNKNOWN")
        self.assertEqual(resolver._determine_outcome(settled_lost), "UNKNOWN")

    def test_resolver_does_not_settle_closed_market_without_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session_id = "20260423_123000"
            session_path = Path(tmpdir) / f"sim_{session_id}.json"
            session_path.write_text(
                json.dumps(
                    {
                        "session_id": session_id,
                        "starting_balance": 100.0,
                        "balance": 100.0,
                        "available_cash": 90.0,
                        "reserved_capital": 10.0,
                        "trades": [
                            {
                                "id": "t1",
                                "timestamp": "2026-04-23T12:00:00+00:00",
                                "exchange": "kalshi",
                                "market_id": "KXHIGHLAX-26APR23-T75",
                                "question": "Will the high temp in LA be >75° on Apr 23, 2026?",
                                "direction": "BUY_YES",
                                "model_probability": 0.7,
                                "market_price": 0.5,
                                "edge": 0.2,
                                "confidence": 0.9,
                                "position_size": 10.0,
                                "reserved_capital": 10.0,
                                "signals": {},
                                "resolved": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            resolver = TradeResolver(tmpdir)
            summary = resolver.resolve_session(
                session_id,
                FakeExchange(
                    {
                        "KXHIGHLAX-26APR23-T75": FakeMarket(
                            status="closed",
                            result=None,
                            yes_price=0.5,
                            no_price=0.5,
                            close_price=None,
                        )
                    }
                ),
            )

            session = json.loads(session_path.read_text(encoding="utf-8"))
            [trade] = session["trades"]

            self.assertEqual(summary["resolved_this_pass"], 0)
            self.assertFalse(trade["resolved"])
            self.assertEqual(trade["outcome"], "pending_settlement")
            self.assertEqual(trade["resolution_type"], "closed_unsettled")
            self.assertAlmostEqual(summary["reserved_capital"], 10.0)

    def test_closed_unsettled_close_price_does_not_count_as_result(self):
        resolver = TradeResolver()
        market = FakeMarket(status="closed", result="", close_price=1.0)

        self.assertFalse(resolver._has_result(market))
        self.assertEqual(resolver._determine_outcome(market), "UNKNOWN")

    def test_closed_unsettled_terminal_quotes_do_not_resolve_without_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session_id = "20260426_230000"
            session_path = Path(tmpdir) / f"sim_{session_id}.json"
            session_path.write_text(
                json.dumps(
                    {
                        "session_id": session_id,
                        "starting_balance": 100.0,
                        "balance": 100.0,
                        "reserved_capital": 10.0,
                        "trades": [
                            {
                                "id": "t1",
                                "timestamp": "2026-04-26T23:00:00+00:00",
                                "exchange": "kalshi",
                                "market_id": "KXHIGHMIA-26APR26-T80",
                                "question": "Will the high temp in Miami be <80° on Apr 26, 2026?",
                                "direction": "BUY_YES",
                                "model_probability": 0.16,
                                "market_price": 0.01,
                                "edge": 0.15,
                                "confidence": 0.9,
                                "position_size": 10.0,
                                "reserved_capital": 10.0,
                                "signals": {},
                                "resolved": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            resolver = TradeResolver(tmpdir)
            summary = resolver.resolve_session(
                session_id,
                FakeExchange(
                    {
                        "KXHIGHMIA-26APR26-T80": FakeMarket(
                            status="closed",
                            result="",
                            yes_price=0.01,
                            no_price=1.0,
                            close_price=None,
                        )
                    }
                ),
            )

            session = json.loads(session_path.read_text(encoding="utf-8"))
            [trade] = session["trades"]

            self.assertEqual(summary["resolved_this_pass"], 0)
            self.assertFalse(trade["resolved"])
            self.assertEqual(trade["outcome"], "pending_settlement")
            self.assertEqual(trade["resolution_type"], "closed_unsettled")
            self.assertIsNone(trade["pnl"])
            self.assertAlmostEqual(session["balance"], 100.0)
            self.assertAlmostEqual(session["reserved_capital"], 10.0)

    def test_resolver_does_not_force_yes_no_for_void_result(self):
        resolver = TradeResolver()
        market = FakeMarket(status="closed", result="cancelled", close_price=1.0)

        self.assertTrue(resolver._has_result(market))
        self.assertEqual(resolver._determine_outcome(market), "VOID")

    def test_resolver_marks_malformed_resolved_rows_untrusted_and_excludes_them_from_balance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session_id = "20260416_130000"
            session_path = Path(tmpdir) / f"sim_{session_id}.json"
            session_path.write_text(
                json.dumps(
                    {
                        "session_id": session_id,
                        "starting_balance": 100.0,
                        "balance": 112.0,
                        "trades": [
                            {
                                "id": "bad-resolved",
                                "timestamp": "2026-04-16T12:00:00+00:00",
                                "exchange": "kalshi",
                                "market_id": "KXHIGHNY-26APR16-T70",
                                "question": "Will the high temp in NYC be <70° on Apr 16, 2026?",
                                "direction": "BUY_YES",
                                "model_probability": 0.7,
                                "market_price": None,
                                "edge": 0.2,
                                "confidence": 0.9,
                                "position_size": 5.0,
                                "signals": {},
                                "resolved": True,
                                "outcome": "YES",
                                "pnl": 12.0,
                                "resolved_at": "2026-04-17T00:00:00+00:00",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            resolver = TradeResolver(tmpdir)
            summary = resolver.resolve_session(session_id, FakeExchange({}))
            session = json.loads(session_path.read_text(encoding="utf-8"))
            [trade] = session["trades"]

            self.assertEqual(trade["integrity_status"], "invalid")
            self.assertIn("invalid_market_price", trade["integrity_errors"])
            self.assertEqual(summary["trusted_resolved"], 0)
            self.assertAlmostEqual(summary["session_pnl"], 0.0)
            self.assertAlmostEqual(session["balance"], 100.0)
            self.assertAlmostEqual(session["available_cash"], 100.0)
            self.assertAlmostEqual(session["reserved_capital"], 0.0)

            report = session["report"]
            self.assertEqual(report["resolved_trades"], 1)
            self.assertEqual(report["trusted_resolved_trades"], 0)
            self.assertEqual(report["invalid_resolved_trades"], 1)
            self.assertAlmostEqual(report["total_realized_pnl"], 0.0)

    def test_analyze_reports_trusted_positions_and_grouped_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session_path = Path(tmpdir) / "sim_20269999_999999.json"
            session_path.write_text(
                json.dumps(
                    {
                        "session_id": "20269999_999999",
                        "scan_count": 7,
                        "trades": [
                            {
                                "market_id": "KXHIGHNY-26APR16-T70",
                                "question": "Will the high temp in NYC be <70° on Apr 16, 2026?",
                                "direction": "BUY_YES",
                                "market_price": 0.5,
                                "position_size": 5.0,
                                "resolved": True,
                                "outcome": "YES",
                                "resolved_at": "2026-04-17T00:00:00+00:00",
                                "pnl": 4.65,
                            },
                            {
                                "market_id": "KXHIGHNY-26APR16-T72",
                                "question": "Will the high temp in NYC be <72° on Apr 16, 2026?",
                                "direction": "BUY_YES",
                                "market_price": 0.25,
                                "position_size": 5.0,
                                "resolved": True,
                                "outcome": "YES",
                                "resolved_at": "2026-04-17T00:00:00+00:00",
                                "pnl": 13.95,
                            },
                            {
                                "market_id": "KXHIGHNY-26APR17-T70",
                                "question": "Will the high temp in NYC be <70° on Apr 17, 2026?",
                                "direction": "BUY_YES",
                                "market_price": None,
                                "position_size": 5.0,
                                "resolved": True,
                                "outcome": "YES",
                                "resolved_at": "2026-04-18T00:00:00+00:00",
                                "pnl": 12.0,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(analyze_script, "DATA_DIR", Path(tmpdir)):
                with patch.dict(os.environ, {"ANALYZE_DATA_DIR": tmpdir}, clear=False):
                    analysis = analyze_script.analyze()
                    report = analyze_script.format_report(analysis)

            self.assertEqual(analysis["summary"]["resolved"], 3)
            self.assertEqual(analysis["summary"]["trusted_resolved_positions"], 2)
            self.assertEqual(analysis["summary"]["invalid_resolved_positions"], 1)
            self.assertEqual(analysis["summary"]["resolved_events"], 1)
            self.assertEqual(analysis["performance"]["basis"], "trusted_resolved_positions")
            self.assertAlmostEqual(analysis["performance"]["total_pnl"], 18.6)
            self.assertEqual(analysis["event_performance"]["basis"], "trusted_resolved_events")
            self.assertEqual(analysis["event_performance"]["resolved_events"], 1)
            self.assertAlmostEqual(analysis["event_performance"]["total_pnl"], 18.6)
            self.assertIn("UNTRUSTED_RESOLVED_ROWS", {issue["code"] for issue in analysis["issues"]})
            self.assertIn("Resolved: 3 raw / 2 trusted / 1 events", report)
            self.assertIn("Event Win Rate: 100.0%", report)


if __name__ == "__main__":
    unittest.main()
