import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot.file_ops import append_jsonl, load_jsonl
from bot.collector_lane_replay_kelly import replay_recorded_collector_lane_with_kelly
from bot.exchanges.kalshi import _market_from_raw
from bot.paper_shadow_lanes import build_paper_shadow_lane_resolution_rows, summarize_paper_shadow_lane_report
from bot.simulator import Simulator
from bot.strategies.enhanced import StrategyTrace


class FakeExchange:
    name = "kalshi"

    def __init__(self, markets):
        self.markets = list(markets)

    def get_markets(self, limit=100):
        return list(self.markets)


def fake_weather_market():
    return SimpleNamespace(
        id="KXHIGHSEA-260515-T70",
        exchange="kalshi",
        question="Will Seattle high temperature be above 70 degrees on May 15, 2026?",
        category="weather",
        yes_price=0.44,
        no_price=0.58,
        yes_bid=0.43,
        no_bid=0.57,
        volume=1200,
        closes_at=None,
        metadata={
            "market_group": "weather",
            "market_family": "daily_temperature",
            "series": "daily_temperature",
            "series_ticker": "KXHIGHSEA",
            "event_ticker": "KXHIGHSEA-260515",
            "market_route": {"group": "weather", "family": "daily_temperature", "allowed": True},
            "recorded_book_asks": {"best_yes_ask": 0.44, "best_no_ask": 0.58},
        },
    )


def fake_zero_yes_weather_market():
    market = fake_weather_market()
    market.yes_price = 0.0
    market.yes_bid = None
    market.no_price = 0.58
    market.metadata["recorded_book_asks"]["best_yes_ask"] = None
    return market


def shadow_config(tmpdir, scoreboard_path, decision_path):
    return {
        "data_dir": str(Path(tmpdir) / "paper"),
        "enable_social": False,
        "min_confidence": 0.5,
        "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
        "paper_shadow_lanes": {
            "enabled": True,
            "enabled_lanes": ["shadow_source_scoreboard"],
            "source_scoreboard_path": str(scoreboard_path),
            "decision_ledger_path": str(decision_path),
        },
    }


def router_shadow_config(tmpdir, scoreboard_path, decision_path):
    config = shadow_config(tmpdir, scoreboard_path, decision_path)
    config["paper_shadow_lanes"]["enabled_lanes"] = ["shadow_source_router"]
    config["paper_shadow_lanes"]["shadow_source_router"] = {
        "enabled": True,
        "parameters": {"hypothetical_notional_usd": 10.0},
    }
    return config


def weather_signal():
    return {
        "direction": "BUY_YES",
        "model_probability": 0.67,
        "market_price": 0.44,
        "yes_market_price": 0.44,
        "no_market_price": 0.58,
        "edge": 0.23,
        "confidence": 0.40,
        "city_id": "seattle_wa",
        "threshold": 70.0,
        "question_side": "above",
        "source_details": [{"source_name": "nws", "forecast_high": 72.0}],
        "signals": {"unit": 0.67},
    }


class StableSkipWeatherTraceStrategy:
    def analyze_market(self, market, order_book=None):
        return None

    def analyze_market_with_trace(self, market, order_book=None):
        live_signal = {
            "signal_type": "weather",
            "predicted_prob": 0.20,
            "confidence": 0.82,
            "data": {
                "city": "seattle_wa",
                "threshold": 70.0,
                "question_side": "above",
                "source_details": [
                    {
                        "source_id": "nws",
                        "source_name": "nws",
                        "forecast_high": 68.0,
                    }
                ],
            },
        }
        return None, StrategyTrace(
            raw_signals={"live": dict(live_signal)},
            accepted_signals={"live": dict(live_signal)},
            skip_reason_code="confidence_below_threshold",
        )


class StableSkipYesSourceWeatherTraceStrategy:
    def analyze_market(self, market, order_book=None):
        return None

    def analyze_market_with_trace(self, market, order_book=None):
        live_signal = {
            "signal_type": "weather",
            "predicted_prob": 0.20,
            "confidence": 0.82,
            "data": {
                "city": "seattle_wa",
                "threshold": 70.0,
                "question_side": "above",
                "source_details": [
                    {
                        "source_id": "nws",
                        "source_name": "nws",
                        "forecast_high": 72.0,
                    }
                ],
            },
        }
        return None, StrategyTrace(
            raw_signals={"live": dict(live_signal)},
            accepted_signals={"live": dict(live_signal)},
            skip_reason_code="confidence_below_threshold",
        )


class SimulatorSourceScoreboardShadowTests(unittest.TestCase):
    def test_scan_appends_source_scoreboard_rows_without_accounting_or_trade_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scoreboard_path = Path(tmpdir) / "source_scoreboard_by_slice.jsonl"
            decision_path = Path(tmpdir) / "paper_shadow_lane_decisions.jsonl"
            append_jsonl(
                scoreboard_path,
                {
                    "source_id": "nws",
                    "source_name": "nws",
                    "city_id": "seattle_wa",
                    "market_kind": "high",
                    "contract_shape": "tail",
                    "sample_count": 100,
                    "threshold_direction_accuracy": 0.95,
                },
            )
            sim = Simulator(shadow_config(tmpdir, scoreboard_path, decision_path))
            starting_balance = sim.balance
            starting_available = sim.available_cash
            starting_reserved = sim.reserved_capital

            with patch.object(sim.strategy, "analyze_market", return_value=weather_signal()):
                result = sim.scan(FakeExchange([fake_weather_market()]))

            lane_rows = load_jsonl(decision_path)
            agent_rows = load_jsonl(Path(sim.data_dir) / "agent_decisions.jsonl")
            source_rows = [row for row in lane_rows if row.get("policy") == "shadow_source_scoreboard"]

        self.assertEqual(result["signals"], 1)
        self.assertEqual(result["trades"], 0)
        self.assertEqual(sim.trades, [])
        self.assertEqual(sim.balance, starting_balance)
        self.assertEqual(sim.available_cash, starting_available)
        self.assertEqual(sim.reserved_capital, starting_reserved)
        self.assertEqual(len(source_rows), 1)
        self.assertFalse(source_rows[0]["mutation_contract"]["mutates_accounting"])
        self.assertFalse(source_rows[0]["accounting_ref"]["mutates_accounting"])
        self.assertTrue(agent_rows)
        self.assertTrue(all(not row["mutation_contract"]["mutates_accounting"] for row in agent_rows))

    def test_future_pnl_inputs_include_hypothetical_fill_and_book_provenance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scoreboard_path = Path(tmpdir) / "source_scoreboard_by_slice.jsonl"
            decision_path = Path(tmpdir) / "paper_shadow_lane_decisions.jsonl"
            append_jsonl(
                scoreboard_path,
                {
                    "source_id": "nws",
                    "source_name": "nws",
                    "city_id": "seattle_wa",
                    "market_kind": "high",
                    "contract_shape": "tail",
                    "sample_count": 100,
                    "threshold_direction_accuracy": 0.95,
                },
            )
            sim = Simulator(shadow_config(tmpdir, scoreboard_path, decision_path))

            with patch.object(sim.strategy, "analyze_market", return_value=weather_signal()):
                sim.scan(FakeExchange([fake_weather_market()]))

            row = load_jsonl(decision_path)[0]

        future_pnl_inputs = row["provenance"]["future_pnl_inputs"]
        self.assertEqual(future_pnl_inputs["entry_price"], 0.44)
        self.assertEqual(future_pnl_inputs["estimated_fill_price"], 0.44)
        self.assertEqual(future_pnl_inputs["best_yes_ask"], 0.44)
        self.assertEqual(future_pnl_inputs["best_yes_bid"], 0.43)
        self.assertEqual(future_pnl_inputs["best_no_ask"], 0.58)
        self.assertEqual(future_pnl_inputs["best_no_bid"], 0.57)
        self.assertEqual(future_pnl_inputs["execution_snapshot_source"], "paper_shadow_hypothetical_book")
        self.assertEqual(
            future_pnl_inputs["execution_snapshot_marker"],
            "paper_shadow_source_scoreboard_hypothetical_execution",
        )
        self.assertTrue(future_pnl_inputs["hypothetical_execution_snapshot"])
        self.assertEqual(future_pnl_inputs["order_book_source"], "paper_scan_recorded_book_ask")
        self.assertIn("execution_snapshot_as_of", future_pnl_inputs)

    def test_simulator_preserves_raw_ask_absence_before_hypothetical_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scoreboard_path = Path(tmpdir) / "source_scoreboard_by_slice.jsonl"
            decision_path = Path(tmpdir) / "paper_shadow_lane_decisions.jsonl"
            sim = Simulator(shadow_config(tmpdir, scoreboard_path, decision_path))
            inputs = {}
            signal = {**weather_signal(), "market_id": "KXHIGHSEA-260515-T70"}
            market = fake_weather_market()
            market.metadata["recorded_book_asks"]["best_no_ask"] = None
            sim._prepare_shadow_source_scoreboard_signal(
                signal, market=market,
                order_book={"best_yes_ask": 0.44, "best_no_ask": None},
                inputs_by_shared_candidate_id=inputs,
            )
            self.assertTrue(inputs)
            writer_signal = next(iter(inputs.values()))["stable_paper"].signal
            self.assertEqual(writer_signal["paper_shadow_recorded_best_yes_ask"], 0.44)
            self.assertIsNone(writer_signal["paper_shadow_recorded_best_no_ask"])

    def test_readiness_summary_counts_simulator_shadow_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scoreboard_path = Path(tmpdir) / "source_scoreboard_by_slice.jsonl"
            decision_path = Path(tmpdir) / "paper_shadow_lane_decisions.jsonl"
            append_jsonl(
                scoreboard_path,
                {
                    "source_id": "nws",
                    "source_name": "nws",
                    "city_id": "seattle_wa",
                    "market_kind": "high",
                    "contract_shape": "tail",
                    "sample_count": 100,
                    "threshold_direction_accuracy": 0.95,
                },
            )
            sim = Simulator(shadow_config(tmpdir, scoreboard_path, decision_path))

            with patch.object(sim.strategy, "analyze_market", return_value=weather_signal()):
                sim.scan(FakeExchange([fake_weather_market()]))

            report = summarize_paper_shadow_lane_report(lane_decision_path=decision_path)

        readiness = report["source_scoreboard_readiness"]
        self.assertEqual(readiness["evaluated_rows"], 1)
        self.assertEqual(readiness["order_book_quote_rows"], 1)
        self.assertEqual(readiness["execution_snapshot_rows"], 1)
        self.assertEqual(readiness["estimated_fill_price_rows"], 1)

    def test_source_router_gets_market_candidate_when_stable_strategy_returns_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scoreboard_path = Path(tmpdir) / "source_scoreboard_by_slice.jsonl"
            decision_path = Path(tmpdir) / "paper_shadow_lane_decisions.jsonl"
            append_jsonl(
                scoreboard_path,
                {
                    "source_id": "nws",
                    "source_name": "nws",
                    "city_id": "seattle_wa",
                    "market_kind": "high",
                    "contract_shape": "tail",
                    "sample_count": 100,
                    "threshold_direction_accuracy": 0.95,
                },
            )
            sim = Simulator(router_shadow_config(tmpdir, scoreboard_path, decision_path))
            sim.strategy = StableSkipWeatherTraceStrategy()

            result = sim.scan(FakeExchange([fake_weather_market()]))

            lane_rows = load_jsonl(decision_path)
            router_rows = [row for row in lane_rows if row.get("policy") == "shadow_source_router"]

        self.assertEqual(result["signals"], 0)
        self.assertEqual(result["trades"], 0)
        self.assertEqual(len(router_rows), 1)
        self.assertEqual(router_rows[0]["action"], "BUY_NO")
        self.assertEqual(router_rows[0]["side"], "NO")
        self.assertEqual(router_rows[0]["entry_price"], 0.58)
        self.assertEqual(router_rows[0]["provenance"]["baseline_action"], "SKIP")
        self.assertEqual(router_rows[0]["provenance"]["source_router"]["source_direction"], "NO")
        self.assertFalse(router_rows[0]["mutation_contract"]["mutates_accounting"])

    def test_source_router_shadow_does_not_fill_yes_from_no_only_quote(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scoreboard_path = Path(tmpdir) / "source_scoreboard_by_slice.jsonl"
            decision_path = Path(tmpdir) / "paper_shadow_lane_decisions.jsonl"
            append_jsonl(
                scoreboard_path,
                {
                    "source_id": "nws",
                    "source_name": "nws",
                    "city_id": "seattle_wa",
                    "market_kind": "high",
                    "contract_shape": "tail",
                    "sample_count": 100,
                    "threshold_direction_accuracy": 0.95,
                },
            )
            sim = Simulator(router_shadow_config(tmpdir, scoreboard_path, decision_path))
            sim.strategy = StableSkipYesSourceWeatherTraceStrategy()

            result = sim.scan(FakeExchange([fake_zero_yes_weather_market()]))

            router_rows = [
                row
                for row in load_jsonl(decision_path)
                if row.get("policy") == "shadow_source_router"
            ]

        self.assertEqual(result["signals"], 0)
        self.assertEqual(result["trades"], 0)
        self.assertEqual(len(router_rows), 1)
        self.assertEqual(router_rows[0]["action"], "BUY_YES")
        self.assertEqual(router_rows[0]["side"], "YES")
        self.assertIsNone(router_rows[0].get("entry_price"))
        self.assertIsNone(router_rows[0].get("price"))
        future_inputs = router_rows[0]["provenance"]["source_router"]["future_pnl_inputs"]
        self.assertNotIn("estimated_fill_price", future_inputs)
        self.assertNotIn("best_yes_ask", future_inputs)
        self.assertEqual(future_inputs["best_no_ask"], 0.58)

    def test_market_reference_without_recorded_ask_cannot_settle_shadow_wallet(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scoreboard_path = Path(tmpdir) / "source_scoreboard_by_slice.jsonl"
            decision_path = Path(tmpdir) / "paper_shadow_lane_decisions.jsonl"
            append_jsonl(scoreboard_path, {
                "source_id": "nws", "source_name": "nws", "city_id": "seattle_wa",
                "market_kind": "high", "contract_shape": "tail", "sample_count": 100,
                "threshold_direction_accuracy": 0.95,
            })
            market = fake_weather_market()  # Reference yes/no prices; no recorded ask metadata.
            market.metadata.pop("recorded_book_asks")
            market.yes_bid = market.no_bid = None
            sim = Simulator(router_shadow_config(tmpdir, scoreboard_path, decision_path))
            sim.strategy = StableSkipWeatherTraceStrategy()
            sim.scan(FakeExchange([market]))
            [row] = [r for r in load_jsonl(decision_path) if r.get("policy") == "shadow_source_router"]
            self.assertIsNone(row["provenance"]["source_router"]["future_pnl_inputs"].get("best_yes_bid"))
            [receipt] = build_paper_shadow_lane_resolution_rows(
                lane_rows=[row], resolution_rows=[{
                    "shared_candidate_id": row["shared_candidate_id"],
                    "run_id": row["run_id"], "market_id": market.id,
                    "resolution_id": "independent-NO", "outcome": "NO",
                    "settlement_ts": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                }],
            )
            wallet = replay_recorded_collector_lane_with_kelly(
                decisions=[row], resolutions=[receipt], starting_balance_usd=100.0,
            )
        self.assertEqual(row["action"], "BUY_NO")
        self.assertIsNone(row.get("entry_price"))
        self.assertEqual(receipt["blocker"], "missing_fill_price")
        self.assertIsNone(receipt["pnl"])
        self.assertEqual(wallet["summary"]["settled_positions"], 0)
        self.assertEqual(wallet["summary"]["final_balance_usd"], 100.0)

    def test_raw_kalshi_ask_remains_available_to_shadow_resolution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scoreboard_path = Path(tmpdir) / "source_scoreboard_by_slice.jsonl"
            decision_path = Path(tmpdir) / "paper_shadow_lane_decisions.jsonl"
            append_jsonl(scoreboard_path, {
                "source_id": "nws", "source_name": "nws", "city_id": "seattle_wa",
                "market_kind": "high", "contract_shape": "tail", "sample_count": 100,
                "threshold_direction_accuracy": 0.95,
            })
            market = _market_from_raw({
                "ticker": "KXHIGHSEA-260515-T70",
                "title": "Will Seattle high temperature be above 70 degrees on May 15, 2026?",
                "yes_ask_dollars": ".44", "no_ask_dollars": ".58",
                "volume_fp": 1200,
            }, category="weather", metadata={
                k: v for k, v in fake_weather_market().metadata.items() if k != "recorded_book_asks"
            })
            self.assertIsNotNone(market)
            sim = Simulator(router_shadow_config(tmpdir, scoreboard_path, decision_path))
            sim.strategy = StableSkipWeatherTraceStrategy()
            sim.scan(FakeExchange([market]))
            [row] = [r for r in load_jsonl(decision_path) if r.get("policy") == "shadow_source_router"]
            [receipt] = build_paper_shadow_lane_resolution_rows(
                lane_rows=[row], resolution_rows=[{
                    "shared_candidate_id": row["shared_candidate_id"],
                    "run_id": row["run_id"], "market_id": market.id,
                    "resolution_id": "independent-NO", "outcome": "NO",
                    "settlement_ts": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                }],
            )
        self.assertEqual(row["action"], "BUY_NO")
        self.assertEqual(row["entry_price"], 0.58)
        self.assertIsNone(receipt["blocker"])
        self.assertEqual(receipt["pnl"]["pnl_usd"], 7.2414)


if __name__ == "__main__":
    unittest.main()
