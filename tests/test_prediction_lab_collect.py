import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot.file_ops import append_jsonl, load_jsonl

from bot.config import load_config
from bot.decision_pipeline import DecisionPipelineEvaluator
from bot.prediction_lab import PredictionLab, PredictionLabRunResult
from bot.prediction_lab_collect import PredictionLabCollectorDaemon
from bot.risk import RiskDecision
from bot.strategies.enhanced import StrategyTrace
from scripts import prediction_lab_collect as prediction_lab_collect_script


class _DirectExchange:
    def __init__(self):
        self.calls = []

    def get_markets(self, limit=0):
        self.calls.append(("get_markets", limit))
        return []

    def get_markets_direct(self, limit=0, page_size=0, max_pages=0):
        self.calls.append(("get_markets_direct", limit, page_size, max_pages))
        return []


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds: float):
        self.now += seconds


class _FakeBot:
    def close(self):
        return None


class _FixedKelly:
    fee_rate = 0.07

    def __init__(self, size: float = 10.0):
        self.size = size

    def calculate(self, win_probability: float, entry_price: float, bankroll: float) -> float:
        return self.size


class _AllowRisk:
    def check_trade(self, signal: dict, position_size: float, *, available_cash: float | None = None):
        return RiskDecision(
            approved=True,
            reason="Approved",
            adjusted_size=position_size,
            original_size=position_size,
        )


class _DenyRisk:
    def check_trade(self, signal: dict, position_size: float, *, available_cash: float | None = None):
        return RiskDecision(
            approved=False,
            reason="Shared core unit denial",
            adjusted_size=0.0,
            original_size=position_size,
            metadata={"reason_code": "risk_unit_denied"},
        )


class _TracedSignalStrategy:
    def __init__(self, signal: dict | None, *, skip_reason_code: str | None = None):
        self.signal = signal
        self.skip_reason_code = skip_reason_code

    def analyze_market_with_trace(self, market, order_book=None):
        trace = StrategyTrace(
            raw_signals={"unit": {"provided": self.signal is not None}},
            accepted_signals={"unit": dict(self.signal)} if self.signal else {},
            rejected_signals={"unit": {"reason": self.skip_reason_code}} if self.signal is None else {},
            ensemble_signal=dict(self.signal) if self.signal else None,
            skip_reason_code=self.skip_reason_code,
        )
        return (dict(self.signal) if self.signal else None), trace


class PredictionLabCollectorTests(unittest.TestCase):
    @staticmethod
    def _runtime_prediction_lab_dir(data_dir: Path) -> Path:
        return data_dir / "paper" / "prediction_lab"

    def _write_config(self, path: Path, *, data_dir: Path, paused: bool = False, enabled: bool = True, cap_gb: float = 5.0, groups: str = "[weather]", score_only: bool = True):
        path.write_text(
            "\n".join(
                [
                    f"data_dir: {data_dir}",
                    "prediction_lab:",
                    f"  enabled: {'true' if enabled else 'false'}",
                    f"  paused: {'true' if paused else 'false'}",
                    "  mode: collector",
                    f"  groups: {groups}",
                    f"  score_only: {'true' if score_only else 'false'}",
                    "  continue_collecting: true",
                    "  collector_interval_seconds: 60",
                    "  resolve_interval_seconds: 60",
                    "  collector_record_market_snapshots: true",
                    "  collector_record_predictions: true",
                    f"  collection_storage_cap_gb: {cap_gb}",
                    "  collection_warning_threshold_pct: 90",
                    "  auto_pause_collection_on_storage_cap: true",
                    "strategy:",
                    "  enable_news: false",
                    "  enable_social: false",
                    "  enable_ai: false",
                ]
            )
        )

    def test_load_config_normalizes_prediction_lab_paused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            self._write_config(config_path, data_dir=Path(tmpdir), paused=True)

            config = load_config(config_path)

            self.assertTrue(config["prediction_lab"]["paused"])
            self.assertFalse(config["prediction_lab"]["observer_mode"])

    def test_prediction_lab_run_respects_manual_pause(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "prediction_lab": {
                    "enabled": True,
                    "paused": True,
                    "mode": "collector",
                    "groups": ["weather"],
                },
                "strategy": {
                    "enable_news": False,
                    "enable_social": False,
                    "enable_ai": False,
                },
            }

            lab = PredictionLab(config)
            result = lab.run(SimpleNamespace(get_markets=lambda limit: []))

            self.assertEqual(result.scanned_markets, 0)
            self.assertEqual(result.recorded_predictions, 0)
            self.assertTrue(lab.state["paused"])
            self.assertEqual(lab.state["paused_reason"], "manual_pause")

    def test_collector_hot_reloads_manual_pause(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = tmp_path / "config.yaml"
            self._write_config(config_path, data_dir=tmp_path, paused=False)
            clock = _FakeClock()
            run_calls = []

            def fake_run(lab, exchange):
                run_calls.append(lab.lab_cfg.get("paused"))
                if len(run_calls) == 1:
                    self._write_config(config_path, data_dir=tmp_path, paused=True)
                lab.update_runtime_state(last_collect_at="collect")
                return PredictionLabRunResult("run-1", 0, 0, {}, {}, str(lab.predictions_path))

            with patch.object(PredictionLab, "run", new=fake_run):
                with patch.object(PredictionLab, "resolve_open_predictions", return_value={"resolved": 0}):
                    daemon = PredictionLabCollectorDaemon(
                        config_path,
                        config_loader=load_config,
                        exchange_builder=lambda config, demo=False: (_FakeBot(), object()),
                        sleep_fn=clock.sleep,
                        monotonic_fn=clock.monotonic,
                    )
                    status = daemon.run(max_cycles=3, idle_sleep_seconds=60)

            state = json.loads((self._runtime_prediction_lab_dir(tmp_path) / "state.json").read_text())
            self.assertEqual(run_calls, [False])
            self.assertEqual(status.collect_runs, 1)
            self.assertEqual(state["paused_reason"], "manual_pause")
            self.assertTrue(state["paused"])

    def test_collector_auto_pauses_when_storage_cap_reached(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = tmp_path / "config.yaml"
            self._write_config(config_path, data_dir=tmp_path, paused=False, cap_gb=0.000001)
            lab_dir = self._runtime_prediction_lab_dir(tmp_path)
            lab_dir.mkdir(parents=True, exist_ok=True)
            append_jsonl(lab_dir / "market_snapshots.jsonl", {"x": "y" * 5000})
            clock = _FakeClock()

            with patch.object(PredictionLab, "run", side_effect=AssertionError("collect should be paused")):
                with patch.object(PredictionLab, "resolve_open_predictions", return_value={"resolved": 0}):
                    daemon = PredictionLabCollectorDaemon(
                        config_path,
                        config_loader=load_config,
                        exchange_builder=lambda config, demo=False: (_FakeBot(), object()),
                        sleep_fn=clock.sleep,
                        monotonic_fn=clock.monotonic,
                    )
                    status = daemon.run(max_cycles=1, idle_sleep_seconds=1)

            state = json.loads((self._runtime_prediction_lab_dir(tmp_path) / "state.json").read_text())
            self.assertEqual(status.pause_reason, "storage_cap")
            self.assertEqual(status.collect_runs, 0)
            self.assertGreater(state["storage_usage_bytes"], 0)
            self.assertEqual(state["paused_reason"], "storage_cap")
            self.assertTrue(state["warning_emitted"])

    def test_storage_usage_ignores_resolutions_and_state_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": str(Path(tmpdir) / "paper"),
                "prediction_lab": {"enabled": True, "mode": "collector", "groups": ["weather"]},
                "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
            }
            lab = PredictionLab(config)
            append_jsonl(lab.resolutions_path, {"ignored": True, "blob": "r" * 5000})
            lab.state_path.write_text(json.dumps({"ignored": True, "blob": "s" * 5000}))
            append_jsonl(lab.predictions_path, {"counted": True, "blob": "p" * 100})

            storage = lab.storage_usage()

            self.assertGreater(storage["bytes"], 0)
            self.assertLess(storage["bytes"], 1000)

    def test_prediction_dedupe_uses_identity_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": str(Path(tmpdir) / "paper"),
                "prediction_lab": {"enabled": True, "mode": "collector", "groups": ["weather"], "score_only": False},
                "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
            }
            lab = PredictionLab(config)
            row = {"market_id": "m1", "experiment_id": "default", "strategy_version": "v1", "status": "open"}

            self.assertTrue(lab._append_prediction_if_absent(dict(row)))
            self.assertFalse(lab._append_prediction_if_absent(dict(row)))
            self.assertTrue(lab._append_prediction_if_absent({**row, "experiment_id": "exp-2"}))
            self.assertEqual(len(load_jsonl(lab.predictions_path)), 2)

    def test_prediction_lab_collector_prefers_direct_paginated_pull(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "prediction_lab": {
                    "enabled": True,
                    "mode": "collector",
                    "groups": ["weather"],
                    "score_only": True,
                    "collector_fetch_mode": "direct_markets",
                    "collector_direct_page_size": 150,
                    "collector_max_pages": 4,
                },
                "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
            }
            lab = PredictionLab(config)
            exchange = _DirectExchange()

            lab.run(exchange)

            self.assertEqual(exchange.calls[0], ("get_markets_direct", lab.max_markets_per_run, 150, 4))

    def test_prediction_lab_default_off_uses_legacy_strategy_analyze_market_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "prediction_lab": {
                    "enabled": True,
                    "mode": "collector",
                    "groups": ["weather"],
                    "score_only": True,
                    "use_shared_pipeline": False,
                },
                "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
            }
            lab = PredictionLab(config)
            market = SimpleNamespace(
                id="KXHIGHTSEA-26APR26-T64",
                exchange="kalshi",
                question="Will the maximum temperature be <64° on Apr 26?",
                category="weather",
                yes_price=0.03,
                no_price=0.97,
                volume=0,
                closes_at=datetime.now(timezone.utc) + timedelta(hours=6),
                metadata={"market_group": "weather", "series": "daily_temperature"},
            )
            exchange = SimpleNamespace(
                get_markets_direct=lambda **kwargs: [market],
                get_order_book=lambda market_id: (_ for _ in ()).throw(AssertionError("legacy path should not fetch order book")),
            )

            with patch.object(lab.strategy, "analyze_market", return_value=None) as analyze_market:
                lab.run(exchange)

            analyze_market.assert_called_once_with(market, None)

    def test_prediction_lab_rejects_multiple_groups_in_v1(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "prediction_lab": {"enabled": True, "mode": "collector", "groups": ["weather", "sports"]},
                "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
            }
            lab = PredictionLab(config)
            with self.assertRaises(ValueError):
                lab.run(SimpleNamespace(get_markets=lambda limit: []))

    def test_prediction_lab_can_record_skip_rows_when_record_all_scored_is_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "prediction_lab": {
                    "enabled": True,
                    "mode": "collector",
                    "observer_mode": True,
                    "groups": ["weather"],
                    "score_only": False,
                    "record_all_scored": True,
                    "collector_record_predictions": True,
                },
                "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
            }
            lab = PredictionLab(config)
            market = SimpleNamespace(
                id="KXHIGHTSEA-26APR26-T64",
                exchange="kalshi",
                question="Will the maximum temperature be <64° on Apr 26?",
                category="weather",
                yes_price=0.03,
                no_price=0.97,
                volume=0,
                closes_at=datetime.now(timezone.utc) + timedelta(hours=6),
                metadata={"market_group": "weather", "series": "daily_temperature"},
            )
            exchange = SimpleNamespace(get_markets_direct=lambda **kwargs: [market])

            result = lab.run(exchange)
            rows = load_jsonl(lab.predictions_path)

            self.assertEqual(result.recorded_predictions, 1)
            self.assertEqual(rows[0]["direction"], "SKIP")
            self.assertTrue(rows[0]["observer_mode"])
            self.assertFalse(rows[0]["trading_enabled"])
            self.assertFalse(rows[0]["order_execution_enabled"])
            self.assertIn("weather_risk", rows[0])

    def test_prediction_lab_shared_pipeline_records_skip_artifact_with_snapshots(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "prediction_lab": {
                    "enabled": True,
                    "mode": "collector",
                    "observer_mode": True,
                    "groups": ["weather"],
                    "score_only": False,
                    "record_all_scored": True,
                    "collector_record_predictions": True,
                    "collector_record_market_snapshots": True,
                    "use_shared_pipeline": True,
                },
                "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
            }
            lab = PredictionLab(config)
            lab.decision_evaluator = DecisionPipelineEvaluator(
                lab.config,
                strategy=_TracedSignalStrategy(None, skip_reason_code="unit_no_signal"),
                kelly_sizer=_FixedKelly(),
                risk_policy=_AllowRisk(),
            )
            market = SimpleNamespace(
                id="KXHIGHTSEA-26APR26-T64",
                exchange="kalshi",
                question="Will the maximum temperature be <64° on Apr 26?",
                category="weather",
                yes_price=0.03,
                no_price=0.97,
                volume=0,
                closes_at=datetime.now(timezone.utc) + timedelta(hours=6),
                metadata={"market_group": "weather", "series": "daily_temperature"},
            )
            exchange = SimpleNamespace(get_markets_direct=lambda **kwargs: [market])

            result = lab.run(exchange)
            prediction_rows = load_jsonl(lab.predictions_path)
            snapshot_rows = load_jsonl(lab.market_snapshots_path)

            self.assertEqual(result.recorded_predictions, 1)
            self.assertEqual(prediction_rows[0]["direction"], "SKIP")
            self.assertEqual(prediction_rows[0]["decision_type"], "skip")
            self.assertEqual(prediction_rows[0]["shared_pipeline"]["final_action"], "SKIP")
            self.assertEqual(prediction_rows[0]["decision_artifact"]["final_reason_code"], "unit_no_signal")
            self.assertEqual(prediction_rows[0]["decision_artifact"]["source_context"]["source"], "provided")
            self.assertIn("market_metadata", prediction_rows[0]["decision_artifact"]["source_context"]["data"])
            self.assertEqual(prediction_rows[0]["decision_artifact"]["order_book_snapshot"]["source"], "missing")
            self.assertEqual(snapshot_rows[0]["decision_artifact"]["final_reason_code"], "unit_no_signal")

    def test_prediction_lab_shared_pipeline_records_buy_artifact_and_preserves_prediction_schema(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "prediction_lab": {
                    "enabled": True,
                    "mode": "collector",
                    "groups": ["sports"],
                    "score_only": False,
                    "record_all_scored": True,
                    "collector_record_predictions": True,
                    "use_shared_pipeline": True,
                },
                "strategy": {"min_edge": 0.01, "min_confidence": 0.5, "enable_news": False, "enable_social": False, "enable_ai": False},
                "max_entry_price": 0.7,
            }
            lab = PredictionLab(config)
            signal = {
                "market_id": "KXNBATEST-26APR29-HOME",
                "exchange": "kalshi",
                "question": "Will the NBA test team win?",
                "direction": "BUY_YES",
                "model_probability": 0.7,
                "market_price": 0.4,
                "yes_market_price": 0.4,
                "no_market_price": 0.6,
                "edge": 0.3,
                "confidence": 0.9,
                "signals": {"unit": 0.7},
            }
            lab.decision_evaluator = DecisionPipelineEvaluator(
                lab.config,
                strategy=_TracedSignalStrategy(signal),
                kelly_sizer=_FixedKelly(10.0),
                risk_policy=_AllowRisk(),
            )
            market = SimpleNamespace(
                id="KXNBATEST-26APR29-HOME",
                exchange="kalshi",
                question="Will the NBA test team win?",
                category="sports",
                yes_price=0.4,
                no_price=0.6,
                volume=1000,
                closes_at=datetime.now(timezone.utc) + timedelta(hours=6),
                metadata={"market_group": "sports", "series": "nba"},
            )
            exchange = SimpleNamespace(
                get_markets_direct=lambda **kwargs: [market],
                get_order_book=lambda market_id: {
                    "best_yes_ask": 0.41,
                    "best_yes_bid": 0.4,
                    "best_no_ask": 0.61,
                    "best_no_bid": 0.6,
                },
            )

            lab.run(exchange)
            row = load_jsonl(lab.predictions_path)[0]

            legacy_prediction_keys = {
                "prediction_id",
                "run_id",
                "timestamp",
                "status",
                "group",
                "series",
                "event_ticker",
                "market_id",
                "question",
                "direction",
                "decision_type",
                "confidence",
                "edge",
                "model_probability",
                "market_price",
                "yes_market_price",
                "no_market_price",
                "signals",
                "signal_details",
                "weather_context",
                "experiment_id",
                "strategy_version",
                "hypothetical",
                "observer_mode",
                "trading_enabled",
                "order_execution_enabled",
            }
            self.assertTrue(legacy_prediction_keys.issubset(row.keys()))
            self.assertEqual(row["decision_type"], "buy_yes")
            self.assertEqual(row["shared_pipeline"]["final_action"], "BUY_YES")
            self.assertEqual(row["decision_artifact"]["order_book_snapshot"]["source"], "book")
            self.assertEqual(row["decision_artifact"]["execution_snapshot_source"], "book")
            self.assertEqual(row["decision_artifact"]["source_context"]["source"], "provided")

    def test_prediction_lab_shared_pipeline_vetoed_signal_is_stored_skip_safe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "prediction_lab": {
                    "enabled": True,
                    "mode": "collector",
                    "observer_mode": True,
                    "groups": ["sports"],
                    "score_only": False,
                    "record_all_scored": True,
                    "collector_record_predictions": True,
                    "collector_record_market_snapshots": True,
                    "use_shared_pipeline": True,
                    "hypothetical_notional_mode": "flat",
                    "flat_notional_usd": 10,
                },
                "strategy": {"min_edge": 0.01, "min_confidence": 0.5, "enable_news": False, "enable_social": False, "enable_ai": False},
                "max_entry_price": 0.7,
            }
            lab = PredictionLab(config)
            signal = {
                "market_id": "KXNBATEST-26APR29-HOME",
                "exchange": "kalshi",
                "question": "Will the NBA test team win?",
                "direction": "BUY_YES",
                "model_probability": 0.7,
                "market_price": 0.4,
                "yes_market_price": 0.4,
                "no_market_price": 0.6,
                "edge": 0.3,
                "confidence": 0.9,
                "signals": {"unit": 0.7},
            }
            lab.decision_evaluator = DecisionPipelineEvaluator(
                lab.config,
                strategy=_TracedSignalStrategy(signal),
                kelly_sizer=_FixedKelly(0.0),
                risk_policy=_AllowRisk(),
            )
            market = SimpleNamespace(
                id="KXNBATEST-26APR29-HOME",
                exchange="kalshi",
                question="Will the NBA test team win?",
                category="sports",
                yes_price=0.4,
                no_price=0.6,
                volume=1000,
                closes_at=datetime.now(timezone.utc) + timedelta(hours=6),
                metadata={"market_group": "sports", "series": "nba"},
            )
            exchange = SimpleNamespace(
                get_markets_direct=lambda **kwargs: [market],
                get_order_book=lambda market_id: {
                    "best_yes_ask": 0.41,
                    "best_yes_bid": 0.4,
                    "best_no_ask": 0.61,
                    "best_no_bid": 0.6,
                },
                _fetch_market_raw=lambda market_id: {
                    "status": "settled",
                    "result": "YES",
                    "close_price": 1.0,
                },
            )

            lab.run(exchange)
            prediction_row = load_jsonl(lab.predictions_path)[0]
            snapshot_row = load_jsonl(lab.market_snapshots_path)[0]
            resolve_result = lab.resolve_open_predictions(exchange)
            resolved_row = load_jsonl(lab.predictions_path)[0]

            self.assertEqual(prediction_row["direction"], "SKIP")
            self.assertEqual(snapshot_row["direction"], "SKIP")
            self.assertEqual(prediction_row["decision_type"], "skip")
            self.assertEqual(prediction_row["hypothetical"]["position_size_usd"], 0.0)
            self.assertEqual(prediction_row["hypothetical"]["approved_position_size_usd"], 0.0)
            self.assertEqual(prediction_row["hypothetical"]["requested_position_size_usd"], 0.0)
            self.assertEqual(prediction_row["decision_artifact"]["strategy_signal"]["direction"], "BUY_YES")
            self.assertEqual(prediction_row["decision_artifact"]["final_action"], "SKIP")
            self.assertEqual(prediction_row["decision_artifact"]["final_reason_code"], "kelly_zero_size")
            self.assertEqual(prediction_row["shared_pipeline"]["final_reason_code"], "kelly_zero_size")
            self.assertEqual(resolve_result["skipped"], 1)
            self.assertEqual(resolved_row["resolution"]["is_correct"], None)
            self.assertEqual(resolved_row["resolution"]["position_size"], 0.0)

    def test_prediction_lab_shared_pipeline_risk_denial_with_positive_kelly_is_stored_skip_safe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "prediction_lab": {
                    "enabled": True,
                    "mode": "collector",
                    "observer_mode": True,
                    "groups": ["sports"],
                    "score_only": False,
                    "record_all_scored": True,
                    "collector_record_predictions": True,
                    "collector_record_market_snapshots": True,
                    "use_shared_pipeline": True,
                    "hypothetical_notional_mode": "flat",
                    "flat_notional_usd": 10,
                },
                "strategy": {"min_edge": 0.01, "min_confidence": 0.5, "enable_news": False, "enable_social": False, "enable_ai": False},
                "max_entry_price": 0.7,
            }
            lab = PredictionLab(config)
            signal = {
                "market_id": "KXNBATEST-26APR29-HOME",
                "exchange": "kalshi",
                "question": "Will the NBA test team win?",
                "direction": "BUY_YES",
                "model_probability": 0.7,
                "market_price": 0.4,
                "yes_market_price": 0.4,
                "no_market_price": 0.6,
                "edge": 0.3,
                "confidence": 0.9,
                "signals": {"unit": 0.7},
            }
            lab.decision_evaluator = DecisionPipelineEvaluator(
                lab.config,
                strategy=_TracedSignalStrategy(signal),
                kelly_sizer=_FixedKelly(10.0),
                risk_policy=_DenyRisk(),
            )
            market = SimpleNamespace(
                id="KXNBATEST-26APR29-HOME",
                exchange="kalshi",
                question="Will the NBA test team win?",
                category="sports",
                yes_price=0.4,
                no_price=0.6,
                volume=1000,
                closes_at=datetime.now(timezone.utc) + timedelta(hours=6),
                metadata={"market_group": "sports", "series": "nba"},
            )
            exchange = SimpleNamespace(
                get_markets_direct=lambda **kwargs: [market],
                get_order_book=lambda market_id: {
                    "best_yes_ask": 0.41,
                    "best_yes_bid": 0.4,
                    "best_no_ask": 0.61,
                    "best_no_bid": 0.6,
                },
            )

            lab.run(exchange)
            prediction_row = load_jsonl(lab.predictions_path)[0]
            snapshot_row = load_jsonl(lab.market_snapshots_path)[0]

            self.assertEqual(prediction_row["direction"], "SKIP")
            self.assertEqual(snapshot_row["direction"], "SKIP")
            self.assertEqual(prediction_row["decision_type"], "skip")
            self.assertEqual(snapshot_row["decision_type"], "skip")
            self.assertEqual(prediction_row["hypothetical"]["position_size_usd"], 0.0)
            self.assertEqual(prediction_row["hypothetical"]["approved_position_size_usd"], 0.0)
            self.assertEqual(prediction_row["hypothetical"]["requested_position_size_usd"], 0.0)
            self.assertEqual(prediction_row["decision_artifact"]["strategy_signal"]["direction"], "BUY_YES")
            self.assertEqual(prediction_row["decision_artifact"]["shared_core_decision"]["requested_position_size"], 10.0)
            self.assertEqual(prediction_row["decision_artifact"]["final_action"], "SKIP")
            self.assertEqual(prediction_row["decision_artifact"]["final_reason_code"], "risk_unit_denied")
            self.assertEqual(prediction_row["shared_pipeline"]["final_reason_code"], "risk_unit_denied")
            self.assertEqual(snapshot_row["decision_artifact"]["strategy_signal"]["direction"], "BUY_YES")
            self.assertEqual(snapshot_row["decision_artifact"]["final_reason_code"], "risk_unit_denied")

    def test_prediction_lab_shared_pipeline_falls_back_to_bid_ask_when_order_book_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "prediction_lab": {
                    "enabled": True,
                    "mode": "collector",
                    "groups": ["sports"],
                    "score_only": False,
                    "record_all_scored": True,
                    "collector_record_predictions": True,
                    "use_shared_pipeline": True,
                },
                "strategy": {"min_edge": 0.01, "min_confidence": 0.5, "enable_news": False, "enable_social": False, "enable_ai": False},
                "max_entry_price": 0.7,
            }
            lab = PredictionLab(config)
            signal = {
                "market_id": "KXNBATEST-26APR29-HOME",
                "exchange": "kalshi",
                "question": "Will the NBA test team win?",
                "direction": "BUY_YES",
                "model_probability": 0.7,
                "market_price": 0.4,
                "yes_market_price": 0.4,
                "no_market_price": 0.6,
                "edge": 0.3,
                "confidence": 0.9,
                "signals": {"unit": 0.7},
            }
            lab.decision_evaluator = DecisionPipelineEvaluator(
                lab.config,
                strategy=_TracedSignalStrategy(signal),
                kelly_sizer=_FixedKelly(10.0),
                risk_policy=_AllowRisk(),
            )
            market = SimpleNamespace(
                id="KXNBATEST-26APR29-HOME",
                exchange="kalshi",
                question="Will the NBA test team win?",
                category="sports",
                yes_price=0.4,
                no_price=0.6,
                volume=1000,
                closes_at=datetime.now(timezone.utc) + timedelta(hours=6),
                metadata={"market_group": "sports", "series": "nba"},
            )
            exchange = SimpleNamespace(
                get_markets_direct=lambda **kwargs: [market],
                get_order_book=lambda market_id: None,
                get_market_bid_ask=lambda market_id: {
                    "best_yes_ask": 0.42,
                    "best_yes_bid": 0.41,
                    "best_no_ask": 0.59,
                    "best_no_bid": 0.58,
                },
            )

            lab.run(exchange)
            row = load_jsonl(lab.predictions_path)[0]

            self.assertEqual(row["decision_artifact"]["order_book_snapshot"]["source"], "book")
            self.assertEqual(row["decision_artifact"]["order_book_snapshot"]["data"]["best_yes_ask"], 0.42)
            self.assertEqual(row["decision_artifact"]["execution_snapshot_source"], "book")
            self.assertEqual(row["shared_pipeline"]["order_book_source"], "book")

    def test_prediction_lab_shared_pipeline_falls_back_to_bid_ask_when_order_book_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "prediction_lab": {
                    "enabled": True,
                    "mode": "collector",
                    "groups": ["sports"],
                    "score_only": False,
                    "record_all_scored": True,
                    "collector_record_predictions": True,
                    "use_shared_pipeline": True,
                },
                "strategy": {"min_edge": 0.01, "min_confidence": 0.5, "enable_news": False, "enable_social": False, "enable_ai": False},
                "max_entry_price": 0.7,
            }
            lab = PredictionLab(config)
            signal = {
                "market_id": "KXNBATEST-26APR29-HOME",
                "exchange": "kalshi",
                "question": "Will the NBA test team win?",
                "direction": "BUY_YES",
                "model_probability": 0.7,
                "market_price": 0.4,
                "yes_market_price": 0.4,
                "no_market_price": 0.6,
                "edge": 0.3,
                "confidence": 0.9,
                "signals": {"unit": 0.7},
            }
            lab.decision_evaluator = DecisionPipelineEvaluator(
                lab.config,
                strategy=_TracedSignalStrategy(signal),
                kelly_sizer=_FixedKelly(10.0),
                risk_policy=_AllowRisk(),
            )
            market = SimpleNamespace(
                id="KXNBATEST-26APR29-HOME",
                exchange="kalshi",
                question="Will the NBA test team win?",
                category="sports",
                yes_price=0.4,
                no_price=0.6,
                volume=1000,
                closes_at=datetime.now(timezone.utc) + timedelta(hours=6),
                metadata={"market_group": "sports", "series": "nba"},
            )
            exchange = SimpleNamespace(
                get_markets_direct=lambda **kwargs: [market],
                get_order_book=lambda market_id: (_ for _ in ()).throw(RuntimeError("book unavailable")),
                get_market_bid_ask=lambda market_id: {
                    "best_yes_ask": 0.42,
                    "best_yes_bid": 0.41,
                    "best_no_ask": 0.59,
                    "best_no_bid": 0.58,
                },
            )

            lab.run(exchange)
            row = load_jsonl(lab.predictions_path)[0]

            self.assertEqual(row["decision_artifact"]["order_book_snapshot"]["source"], "book")
            self.assertEqual(row["decision_artifact"]["order_book_snapshot"]["data"]["best_yes_ask"], 0.42)
            self.assertEqual(row["decision_artifact"]["execution_snapshot_source"], "book")
            self.assertEqual(row["shared_pipeline"]["order_book_source"], "book")

    def test_prediction_lab_snapshot_rows_include_observer_metadata_and_are_not_deduped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "prediction_lab": {
                    "enabled": True,
                    "mode": "collector",
                    "observer_mode": True,
                    "groups": ["weather"],
                    "score_only": True,
                    "record_all_scored": True,
                    "collector_interval_seconds": 123,
                    "collector_record_market_snapshots": True,
                },
                "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
            }
            lab = PredictionLab(config)
            market = SimpleNamespace(
                id="KXHIGHTSEA-26APR26-T64",
                exchange="kalshi",
                question="Will the maximum temperature be <64° on Apr 26?",
                category="weather",
                yes_price=0.03,
                no_price=0.97,
                volume=0,
                closes_at=datetime.now(timezone.utc) + timedelta(hours=6),
                metadata={"market_group": "weather", "series": "daily_temperature"},
            )
            exchange = SimpleNamespace(get_markets_direct=lambda **kwargs: [market])

            lab.run(exchange)
            lab.run(exchange)

            rows = load_jsonl(lab.market_snapshots_path)

            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["snapshot_key"], market.id)
            self.assertEqual(rows[1]["snapshot_key"], market.id)
            self.assertEqual(rows[0]["observed_at"], rows[0]["timestamp"])
            self.assertEqual(rows[0]["collector_interval_seconds"], 123)
            self.assertTrue(rows[0]["observer_mode"])
            self.assertFalse(rows[0]["trading_enabled"])
            self.assertFalse(rows[0]["order_execution_enabled"])
            self.assertTrue(rows[0]["run_id"])

    def test_prediction_lab_observer_mode_outside_collector_uses_archive_semantics(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "prediction_lab": {
                    "enabled": True,
                    "mode": "seed_and_watch",
                    "observer_mode": True,
                    "groups": ["weather"],
                    "score_only": False,
                    "record_all_scored": True,
                    "collector_record_predictions": True,
                    "collector_record_market_snapshots": True,
                },
                "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
            }
            lab = PredictionLab(config)
            market = SimpleNamespace(
                id="KXHIGHTSEA-26APR26-T64",
                exchange="kalshi",
                question="Will the maximum temperature be <64° on Apr 26?",
                category="weather",
                yes_price=0.03,
                no_price=0.97,
                volume=0,
                closes_at=datetime.now(timezone.utc) + timedelta(hours=6),
                metadata={"market_group": "weather", "series": "daily_temperature"},
            )
            exchange = _DirectExchange()

            def _get_markets(limit=0):
                exchange.calls.append(("get_markets", limit))
                return [market]

            def _get_markets_direct(limit=0, page_size=0, max_pages=0):
                exchange.calls.append(("get_markets_direct", limit, page_size, max_pages))
                return [market]

            exchange.get_markets = _get_markets
            exchange.get_markets_direct = _get_markets_direct

            first = lab.run(exchange)
            second = lab.run(exchange)
            prediction_rows = load_jsonl(lab.predictions_path)
            snapshot_rows = load_jsonl(lab.market_snapshots_path)
            state = json.loads(lab.state_path.read_text())

            self.assertEqual(exchange.calls[0], ("get_markets_direct", lab.max_markets_per_run, 200, 10))
            self.assertEqual(first.recorded_predictions, 1)
            self.assertEqual(second.recorded_predictions, 0)
            self.assertEqual(len(prediction_rows), 1)
            self.assertEqual(len(snapshot_rows), 2)
            self.assertTrue(prediction_rows[0]["observer_mode"])
            self.assertTrue(snapshot_rows[0]["observer_mode"])
            self.assertTrue(state["observer_mode"])
            self.assertFalse(state["trading_enabled"])
            self.assertFalse(state["order_execution_enabled"])

    def test_collector_config_patch_can_force_observer_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = tmp_path / "config.yaml"
            self._write_config(config_path, data_dir=tmp_path, paused=False)
            seen_configs = []
            clock = _FakeClock()

            def fake_builder(config, demo=False):
                seen_configs.append(config)
                return _FakeBot(), SimpleNamespace(get_markets=lambda limit=0: [], get_markets_direct=lambda **kwargs: [])

            daemon = PredictionLabCollectorDaemon(
                config_path,
                config_loader=load_config,
                exchange_builder=fake_builder,
                sleep_fn=clock.sleep,
                monotonic_fn=clock.monotonic,
                config_patch={
                    "prediction_lab": {
                        "observer_mode": True,
                        "mode": "collector",
                        "continue_collecting": True,
                        "collector_record_market_snapshots": True,
                        "collector_record_predictions": True,
                        "record_all_scored": True,
                        "score_only": False,
                        "min_confidence_to_record": 0.0,
                        "min_edge_to_record": -1.0,
                    },
                    "trading": {
                        "enabled": False,
                        "trading_enabled": False,
                    },
                    "trading_enabled": False,
                },
            )
            status = daemon.run(max_cycles=1, idle_sleep_seconds=0)

            state = json.loads((self._runtime_prediction_lab_dir(tmp_path) / "state.json").read_text())
            self.assertEqual(status.collect_runs, 1)
            self.assertEqual(len(seen_configs), 1)
            self.assertTrue(seen_configs[0]["prediction_lab"]["observer_mode"])
            self.assertEqual(seen_configs[0]["prediction_lab"]["mode"], "collector")
            self.assertTrue(seen_configs[0]["prediction_lab"]["continue_collecting"])
            self.assertTrue(seen_configs[0]["prediction_lab"]["collector_record_market_snapshots"])
            self.assertTrue(seen_configs[0]["prediction_lab"]["collector_record_predictions"])
            self.assertTrue(seen_configs[0]["prediction_lab"]["record_all_scored"])
            self.assertFalse(seen_configs[0]["prediction_lab"]["score_only"])
            self.assertEqual(seen_configs[0]["prediction_lab"]["min_confidence_to_record"], 0.0)
            self.assertEqual(seen_configs[0]["prediction_lab"]["min_edge_to_record"], -1.0)
            self.assertFalse(seen_configs[0]["trading_enabled"])
            self.assertFalse(seen_configs[0]["trading"]["enabled"])
            self.assertTrue(state["observer_mode"])
            self.assertFalse(state["trading_enabled"])
            self.assertFalse(state["order_execution_enabled"])

    def test_prediction_lab_collect_cli_observer_flag_sets_config_patch(self):
        class _DaemonStub:
            kwargs = None

            def __init__(self, *args, **kwargs):
                type(self).kwargs = kwargs

            def run(self, max_cycles=None, idle_sleep_seconds=5.0):
                return SimpleNamespace(
                    collect_runs=0,
                    resolve_runs=0,
                    skipped_collects=0,
                    pause_reason=None,
                    warning_emitted=False,
                    owner_lock_acquired=True,
                    exit_reason="max_cycles",
                )

        with patch.object(sys, "argv", ["prediction_lab_collect", "--config", "config.yaml", "--observer"]):
            with patch.object(prediction_lab_collect_script, "PredictionLabCollectorDaemon", _DaemonStub):
                exit_code = prediction_lab_collect_script.main()

        self.assertEqual(exit_code, 0)
        self.assertIsNotNone(_DaemonStub.kwargs)
        self.assertTrue(_DaemonStub.kwargs["config_patch"]["prediction_lab"]["observer_mode"])
        self.assertEqual(_DaemonStub.kwargs["config_patch"]["prediction_lab"]["mode"], "collector")
        self.assertTrue(_DaemonStub.kwargs["config_patch"]["prediction_lab"]["continue_collecting"])
        self.assertTrue(_DaemonStub.kwargs["config_patch"]["prediction_lab"]["collector_record_market_snapshots"])
        self.assertTrue(_DaemonStub.kwargs["config_patch"]["prediction_lab"]["collector_record_predictions"])
        self.assertTrue(_DaemonStub.kwargs["config_patch"]["prediction_lab"]["record_all_scored"])
        self.assertFalse(_DaemonStub.kwargs["config_patch"]["prediction_lab"]["score_only"])
        self.assertEqual(_DaemonStub.kwargs["config_patch"]["prediction_lab"]["min_confidence_to_record"], 0.0)
        self.assertEqual(_DaemonStub.kwargs["config_patch"]["prediction_lab"]["min_edge_to_record"], -1.0)
        self.assertFalse(_DaemonStub.kwargs["config_patch"]["trading"]["enabled"])
        self.assertFalse(_DaemonStub.kwargs["config_patch"]["trading_enabled"])

    def test_prediction_lab_weather_overnight_config_loads_expected_observer_settings(self):
        with patch.dict(os.environ, {}, clear=True):
            config = load_config(Path(__file__).resolve().parent.parent / "config.prediction_lab_weather_overnight.yaml")
            prediction_lab = config["prediction_lab"]

            self.assertTrue(prediction_lab["observer_mode"])
            self.assertEqual(prediction_lab["mode"], "collector")
            self.assertEqual(prediction_lab["max_markets_per_run"], 1000)
            self.assertEqual(prediction_lab["collector_interval_seconds"], 900)
            self.assertEqual(prediction_lab["collection_storage_cap_gb"], 25)
            self.assertEqual(prediction_lab["collector_fetch_mode"], "direct_markets")
            self.assertEqual(prediction_lab["hypothetical_notional_mode"], "fresh_kelly")
            self.assertEqual(prediction_lab["fresh_wallet_bankroll_usd"], 100.0)
            self.assertFalse(config.get("trading_enabled", False))

    def test_prediction_lab_config_defaults_and_aliases_fresh_wallet_kelly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            default_path = Path(tmpdir) / "default.yaml"
            default_path.write_text("prediction_lab:\n  enabled: true\n")
            default_config = load_config(default_path)

            alias_path = Path(tmpdir) / "alias.yaml"
            alias_path.write_text("prediction_lab:\n  hypothetical_notional_mode: kelly\n  fresh_wallet_bankroll_usd: 250\n")
            alias_config = load_config(alias_path)

            self.assertEqual(default_config["prediction_lab"]["hypothetical_notional_mode"], "flat")
            self.assertEqual(default_config["prediction_lab"]["fresh_wallet_bankroll_usd"], 100.0)
            self.assertEqual(alias_config["prediction_lab"]["hypothetical_notional_mode"], "fresh_kelly")
            self.assertEqual(alias_config["prediction_lab"]["fresh_wallet_bankroll_usd"], 250.0)

    def test_prediction_lab_rows_include_fresh_wallet_kelly_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "prediction_lab": {
                    "enabled": True,
                    "mode": "collector",
                    "groups": ["weather"],
                    "score_only": False,
                    "hypothetical_notional_mode": "kelly",
                    "fresh_wallet_bankroll_usd": 100,
                },
                "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
                "kalshi_fee_rate": 0.07,
            }
            with patch.dict(os.environ, {"KALSHI_USE_DEMO": "true"}, clear=False):
                lab = PredictionLab(config)
            market = SimpleNamespace(
                id="KXHIGHTSEA-26APR26-T64",
                question="Will the maximum temperature be >64° on Apr 26?",
                category="weather",
                yes_price=0.5,
                no_price=0.5,
                volume=100,
                metadata={"market_group": "weather", "series": "daily_temperature"},
            )
            signal = {
                "direction": "BUY_YES",
                "confidence": 0.8,
                "edge": 0.25,
                "model_probability": 0.75,
                "market_price": 0.5,
                "yes_market_price": 0.5,
                "no_market_price": 0.5,
                "signals": {},
            }

            row = lab._build_prediction_row("run-1", market, signal, decision_type="buy_yes")
            hypothetical = row["hypothetical"]

            self.assertEqual(hypothetical["mode"], "fresh_kelly")
            self.assertEqual(hypothetical["sizing_method"], "fresh_wallet_kelly")
            self.assertEqual(hypothetical["bankroll_usd"], 100.0)
            self.assertEqual(hypothetical["entry_price"], 0.5)
            self.assertEqual(hypothetical["win_probability"], 0.75)
            self.assertEqual(hypothetical["requested_position_size_usd"], 10.0)
            self.assertEqual(hypothetical["approved_position_size_usd"], 10.0)
            self.assertEqual(hypothetical["position_size_usd"], 10.0)
            self.assertIsNone(hypothetical["zero_reason"])
            self.assertEqual(hypothetical["kelly"]["requested_position_size_usd"], 10.0)

    def test_prediction_lab_fresh_wallet_kelly_records_zero_reason(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "prediction_lab": {
                    "enabled": True,
                    "mode": "collector",
                    "groups": ["weather"],
                    "score_only": False,
                    "hypothetical_notional_mode": "fresh_kelly",
                    "fresh_wallet_bankroll_usd": 100,
                },
                "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
            }
            lab = PredictionLab(config)
            market = SimpleNamespace(
                id="KXHIGHTSEA-26APR26-T64",
                question="Will the maximum temperature be >64° on Apr 26?",
                category="weather",
                yes_price=0.5,
                no_price=0.5,
                volume=100,
                metadata={"market_group": "weather", "series": "daily_temperature"},
            )
            signal = {
                "direction": "BUY_YES",
                "confidence": 0.8,
                "edge": 0.0,
                "model_probability": 0.5,
                "market_price": 0.5,
                "yes_market_price": 0.5,
                "no_market_price": 0.5,
                "signals": {},
            }

            row = lab._build_prediction_row("run-1", market, signal, decision_type="buy_yes")

            self.assertEqual(row["hypothetical"]["approved_position_size_usd"], 0.0)
            self.assertEqual(row["hypothetical"]["requested_position_size_usd"], 0.0)
            self.assertEqual(row["hypothetical"]["reason_if_zero"], "kelly_zero_size")

    def test_prediction_lab_resolution_uses_stored_position_size_for_pnl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "prediction_lab": {
                    "enabled": True,
                    "mode": "collector",
                    "groups": ["weather"],
                    "score_only": False,
                    "flat_notional_usd": 10,
                },
                "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
                "kalshi_fee_rate": 0.07,
            }
            lab = PredictionLab(config)
            append_jsonl(
                lab.predictions_path,
                {
                    "prediction_id": "pred-1",
                    "status": "open",
                    "market_id": "KXHIGHTSEA-26APR26-T64",
                    "direction": "BUY_YES",
                    "experiment_id": "default",
                    "strategy_version": "v1",
                    "yes_market_price": 0.5,
                    "no_market_price": 0.5,
                    "hypothetical": {
                        "mode": "fresh_kelly",
                        "position_size_usd": 20.0,
                        "approved_position_size_usd": 20.0,
                        "notional_usd": 20.0,
                    },
                },
            )
            exchange = SimpleNamespace(
                _fetch_market_raw=lambda market_id: {
                    "status": "settled",
                    "result": "YES",
                    "close_price": 1.0,
                }
            )

            result = lab.resolve_open_predictions(exchange)
            rows = load_jsonl(lab.predictions_path)
            resolutions = load_jsonl(lab.resolutions_path)

            self.assertEqual(result["resolved"], 1)
            self.assertAlmostEqual(result["net_pnl"], 18.6)
            self.assertEqual(rows[0]["status"], "resolved")
            self.assertAlmostEqual(rows[0]["resolution"]["position_size"], 20.0)
            self.assertAlmostEqual(resolutions[0]["resolution"]["net_pnl"], 18.6)

    def test_prediction_lab_rows_include_weather_risk_metadata_when_derivable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "prediction_lab": {"enabled": True, "mode": "collector", "groups": ["weather"], "score_only": False},
                "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
            }
            lab = PredictionLab(config)
            market = SimpleNamespace(
                id="KXLOWTOKC-26APR27-T67",
                question="Will the minimum temperature be >67° on Apr 27?",
                category="weather",
                yes_price=0.02,
                no_price=0.98,
                volume=4500,
                metadata={"market_group": "weather", "series": "daily_temperature", "event_ticker": "EVT-1"},
            )
            signal = {
                "direction": "BUY_YES",
                "confidence": 0.95,
                "edge": 0.36,
                "model_probability": 0.38,
                "market_price": 0.02,
                "yes_market_price": 0.02,
                "no_market_price": 0.98,
                "distribution_probability": 0.28,
                "station_id": "KOKC",
                "signals": {"live": 0.38, "price": 0.37},
            }

            row = lab._build_prediction_row("run-1", market, signal, decision_type="buy_yes")
            snapshot = lab._build_market_snapshot_row(
                "run-1",
                market,
                signal,
                decision_type="buy_yes",
                prediction_recorded=True,
            )

            self.assertIn("weather_risk", row)
            self.assertIn("weather_risk", snapshot)
            self.assertEqual(row["weather_risk"]["hidden_gem_tier"], "exceptional")
            self.assertTrue(row["weather_risk"]["evidence_perfect"])
            self.assertEqual(row["weather_risk"]["evidence"]["weather_station_mapping"], "exact")
            self.assertEqual(snapshot["weather_risk"]["evidence"]["market_volume"], 4500.0)

    def test_prediction_lab_rows_can_derive_exact_station_mapping_from_ticker_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "prediction_lab": {"enabled": True, "mode": "collector", "groups": ["weather"], "score_only": False},
                "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
            }
            lab = PredictionLab(config)
            market = SimpleNamespace(
                id="KXHIGHMIA-26APR26-B82.5",
                question="Will the high temp in Miami be 82-83° on Apr 26?",
                category="weather",
                yes_price=0.03,
                no_price=0.97,
                volume=4500,
                metadata={"market_group": "weather", "series": "daily_temperature", "event_ticker": "EVT-2"},
            )
            signal = {
                "direction": "BUY_YES",
                "confidence": 0.95,
                "edge": 0.33,
                "model_probability": 0.38,
                "market_price": 0.03,
                "yes_market_price": 0.03,
                "no_market_price": 0.97,
                "distribution_probability": 0.28,
                "signals": {"live": 0.38, "price": 0.37},
            }

            row = lab._build_prediction_row("run-1", market, signal, decision_type="buy_yes")

            self.assertEqual(row["weather_risk"]["evidence"]["weather_station_mapping"], "exact")
            self.assertEqual(row["weather_risk"]["evidence"]["weather_station_resolution"]["city_code"], "MIA")
            self.assertEqual(row["weather_risk"]["evidence"]["weather_station_resolution"]["station_id"], "KMIA")

    def test_prediction_lab_resolution_does_not_train_on_closed_unsettled_close_price(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "prediction_lab": {"enabled": True, "mode": "collector", "groups": ["weather"], "score_only": False},
                "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
            }
            lab = PredictionLab(config)
            exchange = SimpleNamespace(
                _fetch_market_raw=lambda market_id: {
                    "status": "closed",
                    "result": "",
                    "close_price": 1.0,
                }
            )

            self.assertIsNone(lab._fetch_market_outcome(exchange, "KXHIGHMIA-26APR26-T80"))

    def test_prediction_lab_resolution_does_not_train_on_terminal_quotes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "prediction_lab": {"enabled": True, "mode": "collector", "groups": ["weather"], "score_only": False},
                "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
            }
            lab = PredictionLab(config)
            exchange = SimpleNamespace(
                _fetch_market_raw=lambda market_id: {
                    "status": "closed",
                    "result": "",
                    "yes_price": 0.0,
                    "no_price": 1.0,
                }
            )

            self.assertIsNone(lab._fetch_market_outcome(exchange, "KXHIGHMIA-26APR26-T80"))

    def test_prediction_lab_resolution_accepts_settled_close_price_and_explicit_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "prediction_lab": {"enabled": True, "mode": "collector", "groups": ["weather"], "score_only": False},
                "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
            }
            lab = PredictionLab(config)
            settled_yes_exchange = SimpleNamespace(
                _fetch_market_raw=lambda market_id: {
                    "status": "resolved",
                    "result": "",
                    "close_price": 1.0,
                }
            )
            settled_exchange = SimpleNamespace(
                _fetch_market_raw=lambda market_id: {
                    "status": "settled",
                    "result": "",
                    "close_price": 0.0,
                }
            )
            result_no_exchange = SimpleNamespace(
                _fetch_market_raw=lambda market_id: {
                    "status": "closed",
                    "result": "NO",
                    "close_price": 1.0,
                }
            )
            result_exchange = SimpleNamespace(
                _fetch_market_raw=lambda market_id: {
                    "status": "closed",
                    "result": "YES",
                    "close_price": 0.0,
                }
            )
            void_exchange = SimpleNamespace(
                _fetch_market_raw=lambda market_id: {
                    "status": "cancelled",
                    "result": "",
                    "close_price": 1.0,
                }
            )

            self.assertEqual(lab._fetch_market_outcome(settled_yes_exchange, "KXHIGHMIA-26APR26-T80"), "YES")
            self.assertEqual(lab._fetch_market_outcome(settled_exchange, "KXHIGHMIA-26APR26-T80"), "NO")
            self.assertEqual(lab._fetch_market_outcome(result_no_exchange, "KXHIGHMIA-26APR26-T80"), "NO")
            self.assertEqual(lab._fetch_market_outcome(result_exchange, "KXHIGHMIA-26APR26-T80"), "YES")
            self.assertEqual(lab._fetch_market_outcome(void_exchange, "KXHIGHMIA-26APR26-T80"), "VOID")

    def test_collector_owner_lock_allows_only_one_runner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = tmp_path / "config.yaml"
            self._write_config(config_path, data_dir=tmp_path, paused=False)
            blocker = threading.Event()
            release = threading.Event()

            def holding_run(lab, exchange):
                blocker.set()
                release.wait(timeout=5)
                return PredictionLabRunResult("run-1", 0, 0, {}, {}, str(lab.predictions_path))

            with patch.object(PredictionLab, "run", new=holding_run), patch.object(PredictionLab, "resolve_open_predictions", return_value={"resolved": 0}):
                daemon_one = PredictionLabCollectorDaemon(config_path, config_loader=load_config, exchange_builder=lambda config, demo=False: (_FakeBot(), object()))
                results = {}
                thread = threading.Thread(target=lambda: results.setdefault("first", daemon_one.run(max_cycles=1, idle_sleep_seconds=0.1)))
                thread.start()
                blocker.wait(timeout=5)

                daemon_two = PredictionLabCollectorDaemon(config_path, config_loader=load_config, exchange_builder=lambda config, demo=False: (_FakeBot(), object()))
                status_two = daemon_two.run(max_cycles=1, idle_sleep_seconds=0.1)
                release.set()
                thread.join(timeout=5)

            self.assertEqual(status_two.exit_reason, "owner_locked")
            self.assertFalse(status_two.owner_lock_acquired)


if __name__ == "__main__":
    unittest.main()
