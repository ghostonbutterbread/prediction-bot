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
from bot.prediction_lab import PredictionLab, PredictionLabRunResult
from bot.prediction_lab_collect import PredictionLabCollectorDaemon
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
            self.assertFalse(config.get("trading_enabled", False))

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
