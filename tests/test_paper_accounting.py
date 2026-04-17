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
