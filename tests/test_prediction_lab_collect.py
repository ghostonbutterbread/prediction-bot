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
from bot.prediction_lab_replay import classify_replay_row_quality
from bot.risk import RiskDecision
from bot.shared_market_runtime import SharedMarketRuntimeManager
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


class _SequentialBookExchange:
    def __init__(self, market, books: list[dict | None]):
        self.market = market
        self.books = list(books)
        self.book_calls = 0

    def get_markets_direct(self, **kwargs):
        return [self.market]

    def get_order_book(self, market_id):
        self.book_calls += 1
        if self.books:
            value = self.books.pop(0)
            return dict(value) if isinstance(value, dict) else value
        return None


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


class _RecordingKelly:
    fee_rate = 0.07
    fraction = 0.5
    max_bet_pct = 0.2

    def __init__(self):
        self.bankrolls = []

    def calculate(self, win_probability: float, entry_price: float, bankroll: float) -> float:
        self.bankrolls.append(bankroll)
        return round(bankroll * 0.2, 4)


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

    def _collect_sports_buy_artifact(self, tmpdir: str, books: list[dict | None], *, lab_patch: dict | None = None) -> dict:
        prediction_lab_cfg = {
            "enabled": True,
            "mode": "collector",
            "groups": ["weather"],
            "score_only": False,
            "record_all_scored": True,
            "collector_record_predictions": True,
            "use_shared_pipeline": True,
            "execution_feasibility_max_slippage": 0.01,
            "execution_feasibility_max_elapsed_ms": 10_000,
        }
        if lab_patch:
            prediction_lab_cfg.update(lab_patch)
        config = {
            "data_dir": tmpdir,
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
            "prediction_lab": prediction_lab_cfg,
            "strategy": {"min_edge": 0.01, "min_confidence": 0.5, "enable_news": False, "enable_social": False, "enable_ai": False},
            "max_entry_price": 0.7,
        }
        lab = PredictionLab(config)
        signal = {
            "market_id": "KXHIGHNY-260506-T71",
            "exchange": "kalshi",
            "question": "Will the high temperature in New York exceed 71 degrees?",
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
            id="KXHIGHNY-260506-T71",
            exchange="kalshi",
            question="Will the high temperature in New York exceed 71 degrees?",
            category="KXHIGHNY",
            yes_price=0.4,
            no_price=0.6,
            volume=1000,
            closes_at=datetime.now(timezone.utc) + timedelta(hours=6),
            metadata={"market_group": "weather", "market_family": "daily_temperature", "series_ticker": "KXHIGHNY", "series": "daily_temperature"},
        )
        exchange = _SequentialBookExchange(market, books)

        lab.run(exchange)

        row = load_jsonl(lab.predictions_path)[0]
        row["exchange_book_calls"] = exchange.book_calls
        return row["decision_artifact"]

    def _collect_shadow_delta_rows(self, tmpdir: str, *, strategy_policy: dict) -> tuple[list[dict], list[dict]]:
        config = {
            "data_dir": tmpdir,
            "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
            "strategy_policy": strategy_policy,
            "strategy_lanes": {
                "enabled": True,
                "sizing": {
                    "edge": {"max_position_usd": 4.0},
                },
            },
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
            "strategy": {"min_edge": 0.01, "min_confidence": 0.5, "enable_news": False, "enable_social": False, "enable_ai": False},
            "max_entry_price": 0.7,
        }
        lab = PredictionLab(config)
        signal = {
            "market_id": "KXHIGHNY-260506-T71",
            "exchange": "kalshi",
            "question": "Will the high temperature in New York exceed 71 degrees?",
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
            id="KXHIGHNY-260506-T71",
            exchange="kalshi",
            question="Will the high temperature in New York exceed 71 degrees?",
            category="KXHIGHNY",
            yes_price=0.4,
            no_price=0.6,
            volume=1000,
            closes_at=datetime.now(timezone.utc) + timedelta(hours=6),
            metadata={"market_group": "weather", "market_family": "daily_temperature", "series_ticker": "KXHIGHNY", "series": "daily_temperature"},
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
        return load_jsonl(lab.predictions_path), load_jsonl(lab.market_snapshots_path)

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

    def test_collector_runs_resolution_feed_runner_each_cycle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = tmp_path / "config.yaml"
            self._write_config(config_path, data_dir=tmp_path, paused=False)
            config_path.write_text(
                config_path.read_text(encoding="utf-8")
                .replace("  continue_collecting: true", "  continue_collecting: false")
                + "\nresolution_feed:\n  enabled: true\n  decision_ledger_path: /tmp/decisions.jsonl\n"
            )
            calls = []

            def resolution_runner(config, *, now=None):
                calls.append((config, now))
                return SimpleNamespace(
                    refreshed=True,
                    resolved_market_count=1,
                    unresolved_market_count=2,
                    fetch_error_count=0,
                )

            daemon = PredictionLabCollectorDaemon(
                config_path,
                config_loader=load_config,
                exchange_builder=lambda config, demo=False: (_FakeBot(), SimpleNamespace(name="kalshi")),
                resolution_feed_runner=resolution_runner,
                sleep_fn=lambda seconds: None,
                monotonic_fn=lambda: 0.0,
            )

            status = daemon.run(max_cycles=1, idle_sleep_seconds=0)

        self.assertEqual(status.exit_reason, "max_cycles")
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][0]["resolution_feed"]["enabled"])

    def test_observer_patch_refreshes_runtime_paths_after_env_trading_mode_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "shadow.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        f"runtime:",
                        f"  base_dir: {Path(tmpdir) / 'data' / 'beta_shadow'}",
                        "trading:",
                        "  mode: paper",
                        "  enabled: false",
                        "prediction_lab:",
                        "  enabled: true",
                        "  mode: collector",
                        "  observer_mode: true",
                    ]
                )
            )
            daemon = PredictionLabCollectorDaemon(
                config_path,
                config_patch={
                    "prediction_lab": {"observer_mode": True, "mode": "collector"},
                    "trading": {"mode": "paper", "enabled": False, "trading_enabled": False},
                    "trading_enabled": False,
                },
            )

            with patch.dict(os.environ, {"TRADING_MODE": "live"}, clear=False):
                config = daemon._load_config()

        self.assertEqual(config["trading"]["mode"], "paper")
        self.assertEqual(Path(config["data_dir"]), Path(tmpdir) / "data" / "beta_shadow" / "paper")
        self.assertEqual(Path(config["runtime"]["mode_dir"]), Path(tmpdir) / "data" / "beta_shadow" / "paper")

    def test_prediction_lab_stable_and_beta_off_rows_omit_shadow_delta(self):
        policies = [
            {"version": "stable"},
            {
                "version": "beta",
                "beta": {
                    "mode": "off",
                    "features": {"lane_sizing_caps": True},
                },
            },
        ]
        for policy in policies:
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as tmpdir:
                prediction_rows, snapshot_rows = self._collect_shadow_delta_rows(tmpdir, strategy_policy=policy)

                self.assertEqual(len(prediction_rows), 1)
                self.assertEqual(len(snapshot_rows), 1)
                self.assertNotIn("shadow_delta", prediction_rows[0])
                self.assertNotIn("shadow_delta", snapshot_rows[0])

    def test_prediction_lab_beta_shadow_rows_include_compact_shadow_delta(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prediction_rows, snapshot_rows = self._collect_shadow_delta_rows(
                tmpdir,
                strategy_policy={
                    "version": "beta",
                    "beta": {
                        "mode": "shadow",
                        "features": {"lane_sizing_caps": True},
                    },
                },
            )

            prediction_delta = prediction_rows[0]["shadow_delta"]
            snapshot_delta = snapshot_rows[0]["shadow_delta"]

        self.assertEqual(prediction_delta["schema_version"], 1)
        self.assertEqual(prediction_delta["mode"], "beta_shadow_delta")
        self.assertEqual(prediction_delta["status"], "complete")
        self.assertTrue(prediction_delta["comparison_complete"])
        self.assertTrue(prediction_delta["action_comparison_available"])
        self.assertEqual(prediction_delta["policy"]["version"], "beta")
        self.assertEqual(prediction_delta["policy"]["mode"], "shadow")
        self.assertEqual(prediction_delta["policy"]["enabled_features"], ["lane_sizing_caps"])
        self.assertEqual(prediction_delta["stable"]["action"], "BUY_YES")
        self.assertEqual(prediction_delta["stable"]["requested_position_size"], 10.0)
        self.assertEqual(prediction_delta["shadow"]["action"], "BUY_YES")
        self.assertEqual(prediction_delta["shadow"]["requested_position_size"], 4.0)
        self.assertTrue(prediction_delta["changed"])
        self.assertTrue(prediction_delta["size_changed"])
        self.assertFalse(prediction_delta["action_changed"])
        self.assertIn("lane_sizing", prediction_delta["evidence_sources"])
        self.assertEqual(snapshot_delta["dedupe_key"], prediction_delta["dedupe_key"])
        self.assertNotIn("shadow_delta", prediction_rows[0]["decision_artifact"])

    def test_prediction_lab_beta_shadow_does_not_create_duplicate_prediction_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prediction_rows, snapshot_rows = self._collect_shadow_delta_rows(
                tmpdir,
                strategy_policy={
                    "version": "beta",
                    "beta": {
                        "mode": "shadow",
                        "features": {"lane_sizing_caps": True},
                    },
                },
            )

        self.assertEqual(len(prediction_rows), 1)
        self.assertEqual(len(snapshot_rows), 1)
        self.assertIn("shadow_delta", prediction_rows[0])
        self.assertTrue(snapshot_rows[0]["recorded_prediction"])

    def test_prediction_lab_run_respects_manual_pause(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
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

    def test_collector_shared_runtime_publishes_when_elected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = tmp_path / "config.yaml"
            runtime_root = tmp_path / "shared-runtime"
            self._write_config(config_path, data_dir=tmp_path, paused=False)
            config_path.write_text(
                config_path.read_text().replace(
                    "strategy:",
                    "\n".join(
                        [
                            "  shared_market_runtime_enabled: true",
                            "  shared_market_runtime_instance_id: collector-test",
                            "shared_market:",
                            "  enabled: true",
                            f"  runtime_root: {runtime_root}",
                            "  snapshot_ttl_seconds: 777",
                            "strategy:",
                        ]
                    ),
                )
            )
            clock = _FakeClock()
            run_calls = []

            def fake_run(lab, exchange):
                run_calls.append(True)
                lab.update_runtime_state(last_collect_at="2026-05-14T12:00:00+00:00")
                return PredictionLabRunResult("run-shared-1", 7, 3, {}, {}, str(lab.predictions_path))

            exchange = SimpleNamespace(name="kalshi", get_markets=lambda limit=0: [])
            with patch.object(PredictionLab, "run", new=fake_run), patch.object(
                PredictionLab,
                "resolve_open_predictions",
                return_value={"resolved": 0},
            ):
                daemon = PredictionLabCollectorDaemon(
                    config_path,
                    config_loader=load_config,
                    exchange_builder=lambda config, demo=False: (_FakeBot(), exchange),
                    sleep_fn=clock.sleep,
                    monotonic_fn=clock.monotonic,
                )
                status = daemon.run(max_cycles=1, idle_sleep_seconds=0)

            state = json.loads((runtime_root / "runtime_state.json").read_text())
            latest = json.loads((runtime_root / "latest_snapshot.json").read_text())
            self.assertEqual(status.collect_runs, 1)
            self.assertEqual(run_calls, [True])
            self.assertEqual(latest["snapshot_id"], "run-shared-1")
            self.assertEqual(latest["publisher_runtime"], "collector")
            self.assertEqual(latest["publisher_instance_id"], "collector-test")
            self.assertEqual(latest["candidate_count"], 7)
            self.assertEqual(latest["market_count"], 7)
            self.assertEqual(latest["ttl_seconds"], 777)
            self.assertEqual(latest["source_exchange"], "kalshi")
            self.assertEqual(state["latest_snapshot"], latest)
            self.assertEqual(state["consumers"], {})
            self.assertIsNone(state["publisher"])

    def test_collector_shared_runtime_skips_collect_when_other_publisher_owns_feed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = tmp_path / "config.yaml"
            runtime_root = tmp_path / "shared-runtime"
            self._write_config(config_path, data_dir=tmp_path, paused=False)
            config_path.write_text(
                config_path.read_text().replace(
                    "strategy:",
                    "\n".join(
                        [
                            "  shared_market_runtime_enabled: true",
                            "  shared_market_runtime_instance_id: collector-test",
                            "shared_market:",
                            "  enabled: true",
                            f"  runtime_root: {runtime_root}",
                            "strategy:",
                        ]
                    ),
                )
            )
            manager = SharedMarketRuntimeManager(
                runtime_root=runtime_root,
                config={"shared_market": {"enabled": True, "runtime_root": str(runtime_root)}},
            )
            manager.attach(
                runtime_kind="paper",
                instance_id="paper-owner",
                can_publish=True,
                can_consume=True,
                desired_interval_seconds=60,
            )
            clock = _FakeClock()

            def fail_run(lab, exchange):
                raise AssertionError("collector should not collect while another shared publisher owns the feed")

            with patch.object(PredictionLab, "run", new=fail_run), patch.object(
                PredictionLab,
                "resolve_open_predictions",
                return_value={"resolved": 0},
            ):
                daemon = PredictionLabCollectorDaemon(
                    config_path,
                    config_loader=load_config,
                    exchange_builder=lambda config, demo=False: (_FakeBot(), SimpleNamespace(name="kalshi")),
                    sleep_fn=clock.sleep,
                    monotonic_fn=clock.monotonic,
                )
                status = daemon.run(max_cycles=1, idle_sleep_seconds=0)

            state = json.loads((runtime_root / "runtime_state.json").read_text())
            self.assertEqual(status.collect_runs, 0)
            self.assertEqual(status.skipped_collects, 1)
            self.assertEqual(state["publisher"]["runtime_kind"], "paper")
            self.assertEqual(state["publisher"]["instance_id"], "paper-owner")
            self.assertIn("paper:paper-owner", state["consumers"])
            self.assertNotIn("collector:collector-test", state["consumers"])
            self.assertFalse((runtime_root / "latest_snapshot.json").exists())

    def test_collector_shared_runtime_paused_collector_does_not_publish(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = tmp_path / "config.yaml"
            runtime_root = tmp_path / "shared-runtime"
            self._write_config(config_path, data_dir=tmp_path, paused=True)
            config_path.write_text(
                config_path.read_text().replace(
                    "strategy:",
                    "\n".join(
                        [
                            "  shared_market_runtime_enabled: true",
                            "  shared_market_runtime_instance_id: collector-test",
                            "shared_market:",
                            "  enabled: true",
                            f"  runtime_root: {runtime_root}",
                            "strategy:",
                        ]
                    ),
                )
            )
            clock = _FakeClock()

            def fail_run(lab, exchange):
                raise AssertionError("paused collector should not collect")

            with patch.object(PredictionLab, "run", new=fail_run), patch.object(
                PredictionLab,
                "resolve_open_predictions",
                return_value={"resolved": 0},
            ):
                daemon = PredictionLabCollectorDaemon(
                    config_path,
                    config_loader=load_config,
                    exchange_builder=lambda config, demo=False: (_FakeBot(), SimpleNamespace(name="kalshi")),
                    sleep_fn=clock.sleep,
                    monotonic_fn=clock.monotonic,
                )
                status = daemon.run(max_cycles=1, idle_sleep_seconds=0)

            state = json.loads((runtime_root / "runtime_state.json").read_text())
            self.assertEqual(status.collect_runs, 0)
            self.assertEqual(status.skipped_collects, 1)
            self.assertIsNone(state["publisher"])
            self.assertNotIn("collector:collector-test", state["consumers"])
            self.assertFalse((runtime_root / "latest_snapshot.json").exists())

    def test_collector_shared_runtime_continue_collecting_false_does_not_publish(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = tmp_path / "config.yaml"
            runtime_root = tmp_path / "shared-runtime"
            self._write_config(config_path, data_dir=tmp_path, paused=False)
            config_path.write_text(
                config_path.read_text()
                .replace("  continue_collecting: true", "  continue_collecting: false")
                .replace(
                    "strategy:",
                    "\n".join(
                        [
                            "  shared_market_runtime_enabled: true",
                            "  shared_market_runtime_instance_id: collector-test",
                            "shared_market:",
                            "  enabled: true",
                            f"  runtime_root: {runtime_root}",
                            "strategy:",
                        ]
                    ),
                )
            )
            clock = _FakeClock()

            def fail_run(lab, exchange):
                raise AssertionError("collector should not collect when continue_collecting is false")

            with patch.object(PredictionLab, "run", new=fail_run), patch.object(
                PredictionLab,
                "resolve_open_predictions",
                return_value={"resolved": 0},
            ):
                daemon = PredictionLabCollectorDaemon(
                    config_path,
                    config_loader=load_config,
                    exchange_builder=lambda config, demo=False: (_FakeBot(), SimpleNamespace(name="kalshi")),
                    sleep_fn=clock.sleep,
                    monotonic_fn=clock.monotonic,
                )
                status = daemon.run(max_cycles=1, idle_sleep_seconds=0)

            state = json.loads((runtime_root / "runtime_state.json").read_text())
            self.assertEqual(status.collect_runs, 0)
            self.assertEqual(status.skipped_collects, 0)
            self.assertIsNone(state["publisher"])
            self.assertNotIn("collector:collector-test", state["consumers"])
            self.assertFalse((runtime_root / "latest_snapshot.json").exists())

    def test_collector_shared_runtime_detaches_after_post_collect_storage_pause(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = tmp_path / "config.yaml"
            runtime_root = tmp_path / "shared-runtime"
            self._write_config(config_path, data_dir=tmp_path, paused=False, cap_gb=0.000001)
            config_path.write_text(
                config_path.read_text().replace(
                    "strategy:",
                    "\n".join(
                        [
                            "  shared_market_runtime_enabled: true",
                            "  shared_market_runtime_instance_id: collector-test",
                            "shared_market:",
                            "  enabled: true",
                            f"  runtime_root: {runtime_root}",
                            "strategy:",
                        ]
                    ),
                )
            )
            clock = _FakeClock()

            def fake_run(lab, exchange):
                lab.update_runtime_state(last_collect_at="2026-05-14T12:00:00+00:00")
                append_jsonl(lab.market_snapshots_path, {"x": "y" * 5000})
                return PredictionLabRunResult("run-storage-pause", 1, 0, {}, {}, str(lab.predictions_path))

            with patch.object(PredictionLab, "run", new=fake_run), patch.object(
                PredictionLab,
                "resolve_open_predictions",
                return_value={"resolved": 0},
            ):
                daemon = PredictionLabCollectorDaemon(
                    config_path,
                    config_loader=load_config,
                    exchange_builder=lambda config, demo=False: (_FakeBot(), SimpleNamespace(name="kalshi")),
                    sleep_fn=clock.sleep,
                    monotonic_fn=clock.monotonic,
                )
                status = daemon.run(max_cycles=1, idle_sleep_seconds=0)

            runtime_state = json.loads((runtime_root / "runtime_state.json").read_text())
            lab_state = json.loads((self._runtime_prediction_lab_dir(tmp_path) / "state.json").read_text())
            self.assertEqual(status.collect_runs, 1)
            self.assertEqual(status.pause_reason, "storage_cap")
            self.assertEqual(lab_state["paused_reason"], "storage_cap")
            self.assertIsNone(runtime_state["publisher"])
            self.assertNotIn("collector:collector-test", runtime_state["consumers"])
            self.assertTrue((runtime_root / "latest_snapshot.json").exists())

    def test_collector_shared_runtime_disabled_keeps_legacy_collect_behavior(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = tmp_path / "config.yaml"
            runtime_root = tmp_path / "shared-runtime"
            self._write_config(config_path, data_dir=tmp_path, paused=False)
            clock = _FakeClock()
            run_calls = []

            def fake_run(lab, exchange):
                run_calls.append(True)
                lab.update_runtime_state(last_collect_at="2026-05-14T12:00:00+00:00")
                return PredictionLabRunResult("run-legacy-1", 2, 1, {}, {}, str(lab.predictions_path))

            with patch.object(PredictionLab, "run", new=fake_run), patch.object(
                PredictionLab,
                "resolve_open_predictions",
                return_value={"resolved": 0},
            ):
                daemon = PredictionLabCollectorDaemon(
                    config_path,
                    config_loader=load_config,
                    exchange_builder=lambda config, demo=False: (_FakeBot(), SimpleNamespace(name="kalshi")),
                    sleep_fn=clock.sleep,
                    monotonic_fn=clock.monotonic,
                )
                status = daemon.run(max_cycles=1, idle_sleep_seconds=0)

            self.assertEqual(status.collect_runs, 1)
            self.assertEqual(run_calls, [True])
            self.assertFalse(runtime_root.exists())

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
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
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
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
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
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
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
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
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
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
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

    def test_prediction_lab_shared_pipeline_records_weather_source_snapshot_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
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
                "strategy": {"min_edge": 0.01, "min_confidence": 0.5, "enable_news": False, "enable_social": False, "enable_ai": False},
                "max_entry_price": 0.7,
            }
            lab = PredictionLab(config)
            weather_signal = {
                "signal_type": "weather",
                "predicted_prob": 0.86,
                "confidence": 0.91,
                "source_timestamp": "2026-04-27T12:05:00+00:00",
                "ttl_seconds": 600,
                "question_side": "above",
                "edge": 0.46,
                "data": {
                    "forecast_high": 84.0,
                    "forecast_low": 63.0,
                    "current_temp": 72.0,
                    "actual_temp_used": 84.0,
                    "predicted_temp": 84.0,
                    "threshold": 80.0,
                    "city": "oklahoma city",
                    "sources": ["nws", "open-meteo"],
                    "source_details": [
                        {
                            "source_name": "nws",
                            "role": "settlement_primary",
                            "weight": 1.0,
                            "contribution": 1.0,
                            "forecast_high": 84.0,
                            "forecast_low": 63.0,
                            "current_forecast": 72.0,
                            "weather_date": "2026-04-27",
                            "fetched_at": "2026-04-27T12:05:00+00:00",
                            "as_of": "2026-04-27T12:05:01+00:00",
                            "forecast_period_name": "Today",
                            "forecast_period_start": "2026-04-27T06:00:00-05:00",
                            "forecast_period_end": "2026-04-27T18:00:00-05:00",
                        },
                        {
                            "source_name": "open-meteo",
                            "role": "cross_validation",
                            "weight": None,
                            "contribution": None,
                            "forecast_high": 82.5,
                            "forecast_low": 62.0,
                            "current_forecast": 71.5,
                            "weather_date": "2026-04-27",
                            "fetched_at": "2026-04-27T12:04:00+00:00",
                            "forecast_start": "2026-04-27T07:00",
                            "forecast_end": "2026-04-28T06:00",
                            "forecast_times": ["2026-04-27T07:00", "2026-04-27T08:00"],
                        },
                    ],
                    "agreement": 0.93,
                    "settlement_source": "nws",
                    "nws_open_meteo_gap": 1.5,
                    "weather_date": "2026-04-27",
                    "date_validation": {
                        "ok": True,
                        "reason": "dates_match",
                        "market_date": "2026-04-27",
                        "weather_date": "2026-04-27",
                        "source": "unit:explicit",
                    },
                    "fetched_at": "2026-04-27T12:06:00+00:00",
                    "as_of": "2026-04-27T12:06:01+00:00",
                    "station_id": "KOKC",
                    "station_cli": "OKC",
                },
            }
            signal = {
                "market_id": "KXHIGHTOKC-26APR27-T80",
                "exchange": "kalshi",
                "question": "Will the high temperature in Oklahoma City be above 80 degrees on Apr 27?",
                "direction": "BUY_YES",
                "model_probability": 0.86,
                "market_price": 0.4,
                "yes_market_price": 0.4,
                "no_market_price": 0.6,
                "edge": 0.46,
                "confidence": 0.91,
                "signals": {"live": 0.86},
                "signal_details": {"live": weather_signal},
            }
            lab.decision_evaluator = DecisionPipelineEvaluator(
                lab.config,
                strategy=_TracedSignalStrategy(signal),
                kelly_sizer=_FixedKelly(10.0),
                risk_policy=_AllowRisk(),
            )
            market = SimpleNamespace(
                id="KXHIGHTOKC-26APR27-T80",
                exchange="kalshi",
                question="Will the high temperature in Oklahoma City be above 80 degrees on Apr 27?",
                category="weather",
                yes_price=0.4,
                no_price=0.6,
                volume=4500,
                closes_at=datetime.now(timezone.utc) + timedelta(hours=6),
                metadata={"market_group": "weather", "series": "daily_temperature"},
            )
            exchange = SimpleNamespace(get_markets_direct=lambda **kwargs: [market])

            lab.run(exchange)
            prediction_row = load_jsonl(lab.predictions_path)[0]
            snapshot_row = load_jsonl(lab.market_snapshots_path)[0]
            prediction_snapshot = prediction_row["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]
            market_snapshot = snapshot_row["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]

            self.assertEqual(prediction_row["decision_artifact"]["source_context"]["source_mode"], "recorded_as_of")
            self.assertEqual(prediction_snapshot["market_date"], "2026-04-27")
            self.assertEqual(prediction_snapshot["target_forecast_date"], "2026-04-27")
            self.assertEqual(prediction_snapshot["weather_date"], "2026-04-27")
            self.assertEqual(prediction_snapshot["date_validation"]["source"], "unit:explicit")
            self.assertEqual(prediction_snapshot["source_fetched_at"], "2026-04-27T12:06:00+00:00")
            self.assertEqual(prediction_snapshot["source_as_of"], "2026-04-27T12:06:01+00:00")
            self.assertEqual(prediction_snapshot["station_id"], "KOKC")
            self.assertEqual(prediction_snapshot["settlement_source"], "nws")
            self.assertEqual(prediction_snapshot["source_agreement_score"], 0.93)
            self.assertEqual(prediction_snapshot["gaps"]["nws_open_meteo_gap"], 1.5)
            self.assertEqual(prediction_snapshot["sources"][0]["source_name"], "nws")
            self.assertEqual(prediction_snapshot["sources"][0]["weight"], 1.0)
            self.assertEqual(prediction_snapshot["sources"][0]["weather_date"], "2026-04-27")
            self.assertEqual(prediction_snapshot["sources"][0]["forecast_period_name"], "Today")
            self.assertEqual(prediction_snapshot["sources"][0]["forecast_period_start"], "2026-04-27T06:00:00-05:00")
            self.assertEqual(prediction_snapshot["sources"][0]["source_as_of"], "2026-04-27T12:05:01+00:00")
            self.assertEqual(prediction_snapshot["sources"][1]["source_name"], "open-meteo")
            self.assertEqual(prediction_snapshot["sources"][1]["weight"], 0.0)
            self.assertEqual(prediction_snapshot["sources"][1]["weight_note"], "validator_only_settlement_source_drives_forecast")
            self.assertEqual(prediction_snapshot["sources"][1]["forecast_start"], "2026-04-27T07:00")
            self.assertEqual(prediction_snapshot["sources"][1]["forecast_times"], ["2026-04-27T07:00", "2026-04-27T08:00"])
            self.assertEqual(market_snapshot["sources"][0]["weather_date"], "2026-04-27")
            self.assertNotIn("data", prediction_row["decision_artifact"]["source_snapshots"][0])
            self.assertNotIn("signal", prediction_row["decision_artifact"]["source_snapshots"][0])
            self.assertEqual(
                prediction_row["decision_artifact"]["source_snapshots"][0]["snapshot_ref"],
                "source_context.data.weather_source_snapshot",
            )

    def test_prediction_lab_weather_snapshot_records_rejected_signal_role_before_raw(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
                "prediction_lab": {
                    "enabled": True,
                    "mode": "collector",
                    "observer_mode": True,
                    "groups": ["weather"],
                    "score_only": False,
                    "record_all_scored": True,
                    "collector_record_predictions": True,
                    "use_shared_pipeline": True,
                },
                "strategy": {"min_edge": 0.01, "min_confidence": 0.5, "enable_news": False, "enable_social": False, "enable_ai": False},
                "max_entry_price": 0.7,
            }
            lab = PredictionLab(config)
            weather_signal = {
                "signal_type": "weather",
                "predicted_prob": 0.86,
                "confidence": 0.2,
                "source_timestamp": "2026-04-27T12:05:00+00:00",
                "ttl_seconds": 600,
                "question_side": "above",
                "data": {
                    "forecast_high": 150.0,
                    "forecast_low": 63.0,
                    "actual_temp_used": 150.0,
                    "predicted_temp": 150.0,
                    "threshold": 80.0,
                    "sources": ["nws"],
                    "weather_date": "2026-04-27",
                },
            }

            class RejectedWeatherStrategy:
                def analyze_market_with_trace(self, market, order_book=None):
                    trace = StrategyTrace(
                        raw_signals={"live": dict(weather_signal)},
                        rejected_signals={"live": {**weather_signal, "rejection_reason": "outside plausible bounds"}},
                        skip_reason_code="no_validated_signals",
                    )
                    return None, trace

            lab.decision_evaluator = DecisionPipelineEvaluator(
                lab.config,
                strategy=RejectedWeatherStrategy(),
                kelly_sizer=_FixedKelly(10.0),
                risk_policy=_AllowRisk(),
            )
            market = SimpleNamespace(
                id="KXHIGHTOKC-26APR27-T80",
                exchange="kalshi",
                question="Will the high temperature in Oklahoma City be above 80 degrees on Apr 27?",
                category="weather",
                yes_price=0.4,
                no_price=0.6,
                volume=4500,
                closes_at=datetime.now(timezone.utc) + timedelta(hours=6),
                metadata={"market_group": "weather", "series": "daily_temperature"},
            )
            exchange = SimpleNamespace(get_markets_direct=lambda **kwargs: [market])

            lab.run(exchange)
            snapshot = load_jsonl(lab.predictions_path)[0]["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]
            source_ref = load_jsonl(lab.predictions_path)[0]["decision_artifact"]["source_snapshots"][0]

            self.assertEqual(snapshot["signal_role"], "rejected")
            self.assertEqual(snapshot["validation"]["source_signal_status"], "rejected")
            self.assertEqual(source_ref["signal_role"], "rejected")
            self.assertEqual(snapshot["source_signal"]["data"]["actual_temp_used"], 150.0)

    def test_prediction_lab_historical_weather_snapshot_is_post_facto_not_recorded_as_of(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lab = PredictionLab(
                {
                    "data_dir": tmpdir,
                    "prediction_lab": {"enabled": True, "mode": "collector", "groups": ["weather"]},
                    "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
                }
            )
            artifact = {
                "market_id": "KXHIGHTOKC-26APR27-T80",
                "as_of": "2026-04-28T00:00:00+00:00",
                "strategy_signal": {
                    "signal_details": {
                        "live": {
                            "signal_type": "weather",
                            "predicted_prob": 0.95,
                            "confidence": 0.98,
                            "source_timestamp": "2026-04-27T23:59:59+00:00",
                            "question_side": "above",
                            "data": {
                                "forecast_high": 86.0,
                                "forecast_low": 63.0,
                                "actual_temp_used": 86.0,
                                "predicted_temp": 86.0,
                                "threshold": 80.0,
                                "sources": ["noaa_daily_summaries_station"],
                                "historical_replay": True,
                                "weather_date": "2026-04-27",
                                "date_validation": {
                                    "ok": True,
                                    "reason": "matched",
                                    "market_date": "2026-04-27",
                                    "weather_date": "2026-04-27",
                                },
                            },
                        },
                    }
                },
            }
            market = SimpleNamespace(
                id="KXHIGHTOKC-26APR27-T80",
                question="Will the high temperature in Oklahoma City be above 80 degrees on Apr 27?",
                metadata={"market_group": "weather", "series": "daily_temperature"},
            )

            lab._attach_weather_source_snapshot(artifact, market)
            snapshot = artifact["source_context"]["data"]["weather_source_snapshot"]

            self.assertEqual(artifact["source_context"]["source_mode"], "historical_post_facto")
            self.assertEqual(snapshot["mode"], "historical_post_facto")
            self.assertEqual(snapshot["source_provenance"], "historical_post_facto_backfill")
            self.assertEqual(snapshot["provenance"]["anti_hindsight"], "post_facto_weather_not_recorded_as_of")
            self.assertEqual(artifact["source_snapshots"][0]["mode"], "historical_post_facto")

    def test_prediction_lab_weather_snapshot_missing_weather_date_does_not_use_market_date_as_forecast_date(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
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
                "strategy": {"min_edge": 0.01, "min_confidence": 0.5, "enable_news": False, "enable_social": False, "enable_ai": False},
                "max_entry_price": 0.7,
            }
            lab = PredictionLab(config)
            weather_signal = {
                "signal_type": "weather",
                "predicted_prob": 0.86,
                "confidence": 0.91,
                "source_timestamp": "2026-04-27T12:05:00+00:00",
                "ttl_seconds": 600,
                "question_side": "above",
                "data": {
                    "forecast_high": 84.0,
                    "forecast_low": 63.0,
                    "actual_temp_used": 84.0,
                    "predicted_temp": 84.0,
                    "threshold": 80.0,
                    "city": "oklahoma city",
                    "sources": ["nws", "open-meteo"],
                    "agreement": 0.93,
                    "settlement_source": "nws",
                },
            }
            signal = {
                "market_id": "KXHIGHTOKC-26APR27-T80",
                "exchange": "kalshi",
                "question": "Will the high temperature in Oklahoma City be above 80 degrees on Apr 27?",
                "direction": "BUY_YES",
                "model_probability": 0.86,
                "market_price": 0.4,
                "yes_market_price": 0.4,
                "no_market_price": 0.6,
                "edge": 0.46,
                "confidence": 0.91,
                "signals": {"live": 0.86},
                "signal_details": {"live": weather_signal},
            }
            lab.decision_evaluator = DecisionPipelineEvaluator(
                lab.config,
                strategy=_TracedSignalStrategy(signal),
                kelly_sizer=_FixedKelly(10.0),
                risk_policy=_AllowRisk(),
            )
            market = SimpleNamespace(
                id="KXHIGHTTOKC-26APR27-T80",
                exchange="kalshi",
                question="Will the high temperature in Oklahoma City be above 80 degrees on Apr 27?",
                category="weather",
                yes_price=0.4,
                no_price=0.6,
                volume=4500,
                closes_at=datetime.now(timezone.utc) + timedelta(hours=6),
                metadata={"market_group": "weather", "series": "daily_temperature"},
            )
            exchange = SimpleNamespace(get_markets_direct=lambda **kwargs: [market])

            lab.run(exchange)
            prediction_snapshot = load_jsonl(lab.predictions_path)[0]["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]

            self.assertEqual(prediction_snapshot["market_date"], "2026-04-27")
            self.assertNotIn("target_forecast_date", prediction_snapshot)
            self.assertNotIn("forecast_date", prediction_snapshot)
            self.assertNotIn("weather_date", prediction_snapshot)
            self.assertEqual(prediction_snapshot["date_validation"]["reason"], "missing_weather_date")
            self.assertIsNone(prediction_snapshot["date_validation"]["weather_date"])
            self.assertNotIn("target_forecast_date", prediction_snapshot["sources"][0])
            self.assertNotIn("forecast_date", prediction_snapshot["sources"][0])
            self.assertNotIn("weather_date", prediction_snapshot["sources"][0])

    def test_prediction_lab_weather_snapshot_derives_date_from_forecast_period_start(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lab = PredictionLab(
                {
                    "data_dir": tmpdir,
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
                    "prediction_lab": {"enabled": True, "mode": "collector", "groups": ["weather"], "use_shared_pipeline": True},
                    "strategy": {"min_edge": 0.01, "min_confidence": 0.5, "enable_news": False, "enable_social": False, "enable_ai": False},
                }
            )
            weather_signal = {
                "signal_type": "weather",
                "predicted_prob": 0.86,
                "confidence": 0.91,
                "source_timestamp": "2026-04-27T12:05:00+00:00",
                "question_side": "above",
                "data": {
                    "forecast_high": 84.0,
                    "forecast_low": 63.0,
                    "actual_temp_used": 84.0,
                    "predicted_temp": 84.0,
                    "threshold": 80.0,
                    "sources": ["nws"],
                    "source_details": [
                        {
                            "source_name": "nws",
                            "forecast_high": 84.0,
                            "forecast_low": 63.0,
                            "forecast_period_name": "Today",
                            "forecast_period_start": "2026-04-27T06:00:00-05:00",
                            "forecast_period_end": "2026-04-27T18:00:00-05:00",
                        }
                    ],
                    "settlement_source": "nws",
                },
            }
            artifact = {
                "strategy_signal": {
                    "market_id": "KXHIGHTOKC-26APR27-T80",
                    "question": "Will the high temperature in Oklahoma City be above 80 degrees on Apr 27?",
                    "signal_details": {"live": weather_signal},
                },
                "strategy_trace": {},
                "as_of": "2026-04-27T12:06:00+00:00",
            }
            market = SimpleNamespace(
                id="KXHIGHTOKC-26APR27-T80",
                question="Will the high temperature in Oklahoma City be above 80 degrees on Apr 27?",
                category="weather",
                metadata={"market_group": "weather", "series": "daily_temperature"},
            )

            snapshot = lab._build_weather_source_snapshot(artifact, market)

            self.assertEqual(snapshot["forecast_date"], "2026-04-27")
            self.assertEqual(snapshot["weather_date"], "2026-04-27")
            self.assertEqual(snapshot["date_validation"]["reason"], "dates_match")
            self.assertEqual(snapshot["date_validation"]["weather_date"], "2026-04-27")
            self.assertEqual(snapshot["sources"][0]["forecast_date"], "2026-04-27")
            self.assertEqual(snapshot["sources"][0]["target_forecast_date"], "2026-04-27")

    def test_prediction_lab_weather_snapshot_uses_source_detail_weather_date_for_strict_replay_grade(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
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
                "strategy": {"min_edge": 0.01, "min_confidence": 0.5, "enable_news": False, "enable_social": False, "enable_ai": False},
                "max_entry_price": 0.7,
            }
            lab = PredictionLab(config)
            weather_signal = {
                "signal_type": "weather",
                "predicted_prob": 0.86,
                "confidence": 0.91,
                "source_timestamp": "2026-04-27T12:05:00+00:00",
                "ttl_seconds": 600,
                "question_side": "above",
                "data": {
                    "forecast_high": 84.0,
                    "forecast_low": 63.0,
                    "actual_temp_used": 84.0,
                    "predicted_temp": 84.0,
                    "threshold": 80.0,
                    "city": "oklahoma city",
                    "sources": ["nws"],
                    "source_details": [
                        {
                            "source_name": "nws",
                            "forecast_high": 84.0,
                            "forecast_low": 63.0,
                            "weather_date": "2026-04-27",
                            "forecast_date": "2026-04-27",
                            "date_validation": {"ok": True},
                            "fetched_at": "2026-04-27T12:05:00+00:00",
                        }
                    ],
                    "agreement": 0.93,
                    "settlement_source": "nws",
                    "date_validation": {"ok": True},
                },
            }
            signal = {
                "market_id": "KXHIGHTOKC-26APR27-T80",
                "exchange": "kalshi",
                "question": "Will the high temperature in Oklahoma City be above 80 degrees on Apr 27?",
                "direction": "BUY_YES",
                "model_probability": 0.86,
                "market_price": 0.4,
                "yes_market_price": 0.4,
                "no_market_price": 0.6,
                "edge": 0.46,
                "confidence": 0.91,
                "signals": {"live": 0.86},
                "signal_details": {"live": weather_signal},
            }
            lab.decision_evaluator = DecisionPipelineEvaluator(
                lab.config,
                strategy=_TracedSignalStrategy(signal),
                kelly_sizer=_FixedKelly(10.0),
                risk_policy=_AllowRisk(),
            )
            market = SimpleNamespace(
                id="KXHIGHTOKC-26APR27-T80",
                exchange="kalshi",
                question="Will the high temperature in Oklahoma City be above 80 degrees on Apr 27?",
                category="weather",
                yes_price=0.4,
                no_price=0.6,
                volume=4500,
                closes_at=datetime.now(timezone.utc) + timedelta(hours=6),
                metadata={"market_group": "weather", "series": "daily_temperature"},
            )
            exchange = SimpleNamespace(
                get_markets_direct=lambda **kwargs: [market],
                get_order_book=lambda market_id: {
                    "best_yes_ask": 0.41,
                    "best_yes_bid": 0.40,
                    "best_no_ask": 0.60,
                    "best_no_bid": 0.59,
                },
            )

            lab.run(exchange)
            prediction_row = load_jsonl(lab.predictions_path)[0]
            prediction_snapshot = prediction_row["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]
            quality = classify_replay_row_quality(prediction_row["decision_artifact"], prediction_row)

            self.assertEqual(prediction_snapshot["weather_date"], "2026-04-27")
            self.assertEqual(prediction_snapshot["forecast_date"], "2026-04-27")
            self.assertEqual(prediction_snapshot["date_validation"]["reason"], "dates_match")
            self.assertEqual(prediction_snapshot["date_validation"]["market_date"], "2026-04-27")
            self.assertEqual(prediction_snapshot["date_validation"]["weather_date"], "2026-04-27")
            self.assertEqual(prediction_snapshot["sources"][0]["weather_date"], "2026-04-27")
            self.assertEqual(prediction_snapshot["sources"][0]["date_validation"], {"ok": True})
            self.assertEqual(quality.category, "replay_grade_original")
            self.assertTrue(quality.include_in_strict)

    def test_prediction_lab_shared_pipeline_records_buy_artifact_and_preserves_prediction_schema(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
                "prediction_lab": {
                    "enabled": True,
                    "mode": "collector",
                    "groups": ["weather"],
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
                "market_id": "KXHIGHNY-260506-T71",
                "exchange": "kalshi",
                "question": "Will the high temperature in New York exceed 71 degrees?",
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
                id="KXHIGHNY-260506-T71",
                exchange="kalshi",
                question="Will the high temperature in New York exceed 71 degrees?",
                category="KXHIGHNY",
                yes_price=0.4,
                no_price=0.6,
                volume=1000,
                closes_at=datetime.now(timezone.utc) + timedelta(hours=6),
                metadata={"market_group": "weather", "market_family": "daily_temperature", "series_ticker": "KXHIGHNY", "series": "daily_temperature"},
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

    def test_prediction_lab_execution_feasibility_exact_match(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact = self._collect_sports_buy_artifact(
                tmpdir,
                [
                    {"best_yes_ask": 0.41, "best_yes_bid": 0.40, "best_no_ask": 0.60, "best_no_bid": 0.59, "best_yes_ask_quantity": 100},
                    {"best_yes_ask": 0.41, "best_yes_bid": 0.40, "best_no_ask": 0.60, "best_no_bid": 0.59, "best_yes_ask_quantity": 100},
                ],
            )

        feasibility = artifact["execution_feasibility"]
        self.assertEqual(artifact["pre_logic_order_book_snapshot"]["stage"], "pre_logic")
        self.assertEqual(artifact["post_logic_order_book_snapshot"]["stage"], "post_logic")
        self.assertGreaterEqual(artifact["decision_latency_ms"], 0)
        self.assertTrue(feasibility["feasible"])
        self.assertTrue(feasibility["ask_unchanged"])
        self.assertTrue(feasibility["sufficient_quantity"])
        self.assertFalse(feasibility["mutates_paper_state"])

    def test_prediction_lab_execution_feasibility_allows_configured_slippage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact = self._collect_sports_buy_artifact(
                tmpdir,
                [
                    {"best_yes_ask": 0.41, "best_yes_bid": 0.40, "best_no_ask": 0.60, "best_no_bid": 0.59},
                    {"best_yes_ask": 0.415, "best_yes_bid": 0.405, "best_no_ask": 0.585, "best_no_bid": 0.575},
                ],
                lab_patch={"execution_feasibility_max_slippage": 0.01},
            )

        feasibility = artifact["execution_feasibility"]
        self.assertTrue(feasibility["feasible"])
        self.assertFalse(feasibility["ask_unchanged"])
        self.assertTrue(feasibility["ask_within_slippage"])
        self.assertAlmostEqual(feasibility["ask_delta"], 0.005)

    def test_prediction_lab_execution_feasibility_rejects_moved_or_unavailable_ask(self):
        cases = {
            "moved": (
                {"best_yes_ask": 0.41, "best_yes_bid": 0.40, "best_no_ask": 0.60, "best_no_bid": 0.59},
                {"best_yes_ask": 0.43, "best_yes_bid": 0.42, "best_no_ask": 0.58, "best_no_bid": 0.57},
                "ask_within_slippage",
            ),
            "unavailable": (
                {"best_yes_ask": 0.41, "best_yes_bid": 0.40, "best_no_ask": 0.60, "best_no_bid": 0.59},
                {"best_yes_ask": None, "best_yes_bid": 0.40, "best_no_ask": 0.60, "best_no_bid": 0.59},
                "same_side_ask_present",
            ),
        }
        for name, (pre_book, post_book, failed_check) in cases.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as tmpdir:
                    artifact = self._collect_sports_buy_artifact(
                        tmpdir,
                        [pre_book, post_book],
                        lab_patch={"execution_feasibility_max_slippage": 0.01},
                    )

                feasibility = artifact["execution_feasibility"]
                self.assertFalse(feasibility["feasible"])
                self.assertIn(failed_check, feasibility["failed_checks"])

    def test_prediction_lab_shared_pipeline_uses_isolated_opportunity_bankroll(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
                "prediction_lab": {
                    "enabled": True,
                    "mode": "collector",
                    "groups": ["weather"],
                    "score_only": False,
                    "record_all_scored": True,
                    "collector_record_predictions": True,
                    "collector_record_market_snapshots": True,
                    "use_shared_pipeline": True,
                    "paper_lab_mode": "opportunity",
                    "opportunity_bankroll_usd": 250,
                    "hypothetical_notional_mode": "opportunity",
                },
                "strategy": {"min_edge": 0.01, "min_confidence": 0.5, "enable_news": False, "enable_social": False, "enable_ai": False},
                "max_entry_price": 0.7,
            }
            lab = PredictionLab(config)
            signal = {
                "market_id": "KXHIGHNY-260506-T71",
                "exchange": "kalshi",
                "question": "Will the high temperature in New York exceed 71 degrees?",
                "direction": "BUY_YES",
                "model_probability": 0.7,
                "market_price": 0.4,
                "yes_market_price": 0.4,
                "no_market_price": 0.6,
                "edge": 0.3,
                "confidence": 0.9,
                "signals": {"unit": 0.7},
            }
            kelly = _RecordingKelly()
            lab.decision_evaluator = DecisionPipelineEvaluator(
                lab.config,
                strategy=_TracedSignalStrategy(signal),
                kelly_sizer=kelly,
                risk_policy=_AllowRisk(),
            )
            market = SimpleNamespace(
                id="KXHIGHNY-260506-T71",
                exchange="kalshi",
                question="Will the high temperature in New York exceed 71 degrees?",
                category="KXHIGHNY",
                yes_price=0.4,
                no_price=0.6,
                volume=1000,
                closes_at=datetime.now(timezone.utc) + timedelta(hours=6),
                metadata={"market_group": "weather", "market_family": "daily_temperature", "series_ticker": "KXHIGHNY", "series": "daily_temperature"},
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
            artifact = prediction_row["decision_artifact"]
            account_snapshot = artifact["account_state_snapshot"]
            decision = artifact["shared_core_decision"]

            self.assertEqual(kelly.bankrolls, [250.0])
            self.assertEqual(artifact["mode"], "paper_lab")
            self.assertEqual(artifact["opportunity_mode"]["mode"], "opportunity")
            self.assertEqual(artifact["opportunity_mode"]["account_state_provider"], "fixed_opportunity")
            self.assertEqual(artifact["opportunity_mode"]["bankroll_usd"], 250.0)
            self.assertFalse(artifact["opportunity_mode"]["mutates_portfolio_account"])
            self.assertEqual(account_snapshot["available_cash"], 250.0)
            self.assertEqual(account_snapshot["reserved_capital"], 0.0)
            self.assertEqual(account_snapshot["total_exposure"], 0.0)
            self.assertEqual(account_snapshot["open_positions"], 0)
            self.assertEqual(decision["reasoning"]["kelly"]["requested_size"], 50.0)
            self.assertEqual(decision["requested_position_size"], 25.0)
            self.assertEqual(decision["position_size"], 25.0)
            self.assertEqual(decision["reasoning"]["kelly"]["bankroll"], 250.0)
            self.assertEqual(prediction_row["shared_pipeline"]["bankroll_usd"], 250.0)
            self.assertEqual(prediction_row["shared_pipeline"]["kelly_position_size_usd"], 50.0)
            self.assertEqual(prediction_row["shared_pipeline"]["requested_position_size_usd"], 25.0)
            self.assertEqual(prediction_row["paper_lab"]["paper_lab_mode"], "opportunity")
            self.assertEqual(prediction_row["opportunity_mode"]["account_state_provider"], "fixed_opportunity")
            self.assertFalse(prediction_row["opportunity_mode"]["mutates_portfolio_account"])
            self.assertEqual(snapshot_row["opportunity_mode"]["bankroll_usd"], 250.0)

    def test_prediction_lab_shared_pipeline_uses_fixed_opportunity_risk_not_paper_risk_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "risk_state.json").write_text(json.dumps({"max_drawdown_halt": True}), encoding="utf-8")
            config = {
                "data_dir": tmpdir,
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
                "prediction_lab": {
                    "enabled": True,
                    "mode": "collector",
                    "groups": ["weather"],
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
                "market_id": "unit-risk-isolation",
                "exchange": "kalshi",
                "question": "Will the high temperature in New York exceed 71 degrees?",
                "direction": "BUY_YES",
                "model_probability": 0.7,
                "market_price": 0.4,
                "yes_market_price": 0.4,
                "no_market_price": 0.6,
                "edge": 0.3,
                "confidence": 0.9,
                "signals": {"unit": 0.7},
            }
            lab.decision_evaluator.strategy = _TracedSignalStrategy(signal)
            lab.decision_evaluator.kelly_sizer = _FixedKelly(10.0)
            market = SimpleNamespace(
                id="unit-risk-isolation",
                exchange="kalshi",
                question="Will the high temperature in New York exceed 71 degrees?",
                category="KXHIGHNY",
                yes_price=0.4,
                no_price=0.6,
                volume=4500,
                closes_at=datetime.now(timezone.utc) + timedelta(hours=6),
                metadata={"market_group": "sports", "series": "unit"},
            )
            exchange = SimpleNamespace(
                get_markets_direct=lambda **kwargs: [market],
                get_order_book=lambda market_id: {
                    "best_yes_ask": 0.41,
                    "best_yes_bid": 0.40,
                    "best_no_ask": 0.60,
                    "best_no_bid": 0.59,
                },
            )

            lab.run(exchange)
            row = load_jsonl(lab.predictions_path)[0]

            self.assertEqual(row["decision_artifact"]["final_action"], "BUY_YES")
            self.assertEqual(row["decision_artifact"]["shared_core_decision"]["reason_code"], "approved")
            self.assertEqual(
                row["decision_artifact"]["shared_core_decision"]["reasoning"]["risk"]["metadata"]["risk_policy"],
                "fixed_opportunity",
            )

    def test_prediction_lab_shared_pipeline_vetoed_signal_is_stored_skip_safe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
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
                    "hypothetical_notional_mode": "flat",
                    "flat_notional_usd": 10,
                },
                "strategy": {"min_edge": 0.01, "min_confidence": 0.5, "enable_news": False, "enable_social": False, "enable_ai": False},
                "max_entry_price": 0.7,
            }
            lab = PredictionLab(config)
            signal = {
                "market_id": "KXHIGHNY-260506-T71",
                "exchange": "kalshi",
                "question": "Will the high temperature in New York exceed 71 degrees?",
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
                id="KXHIGHNY-260506-T71",
                exchange="kalshi",
                question="Will the high temperature in New York exceed 71 degrees?",
                category="KXHIGHNY",
                yes_price=0.4,
                no_price=0.6,
                volume=1000,
                closes_at=datetime.now(timezone.utc) + timedelta(hours=6),
                metadata={"market_group": "weather", "market_family": "daily_temperature", "series_ticker": "KXHIGHNY", "series": "daily_temperature"},
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
            resolution_row = load_jsonl(lab.resolutions_path)[0]

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
            self.assertNotIn("resolution", resolved_row)
            self.assertEqual(resolution_row["resolution"]["is_correct"], None)
            self.assertEqual(resolution_row["resolution"]["position_size"], 0.0)

    def test_prediction_lab_shared_pipeline_risk_denial_with_positive_kelly_is_stored_skip_safe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
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
                    "hypothetical_notional_mode": "flat",
                    "flat_notional_usd": 10,
                },
                "strategy": {"min_edge": 0.01, "min_confidence": 0.5, "enable_news": False, "enable_social": False, "enable_ai": False},
                "max_entry_price": 0.7,
            }
            lab = PredictionLab(config)
            signal = {
                "market_id": "KXHIGHNY-260506-T71",
                "exchange": "kalshi",
                "question": "Will the high temperature in New York exceed 71 degrees?",
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
                id="KXHIGHNY-260506-T71",
                exchange="kalshi",
                question="Will the high temperature in New York exceed 71 degrees?",
                category="KXHIGHNY",
                yes_price=0.4,
                no_price=0.6,
                volume=1000,
                closes_at=datetime.now(timezone.utc) + timedelta(hours=6),
                metadata={"market_group": "weather", "market_family": "daily_temperature", "series_ticker": "KXHIGHNY", "series": "daily_temperature"},
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
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
                "prediction_lab": {
                    "enabled": True,
                    "mode": "collector",
                    "groups": ["weather"],
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
                "market_id": "KXHIGHNY-260506-T71",
                "exchange": "kalshi",
                "question": "Will the high temperature in New York exceed 71 degrees?",
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
                id="KXHIGHNY-260506-T71",
                exchange="kalshi",
                question="Will the high temperature in New York exceed 71 degrees?",
                category="KXHIGHNY",
                yes_price=0.4,
                no_price=0.6,
                volume=1000,
                closes_at=datetime.now(timezone.utc) + timedelta(hours=6),
                metadata={"market_group": "weather", "market_family": "daily_temperature", "series_ticker": "KXHIGHNY", "series": "daily_temperature"},
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
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
                "prediction_lab": {
                    "enabled": True,
                    "mode": "collector",
                    "groups": ["weather"],
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
                "market_id": "KXHIGHNY-260506-T71",
                "exchange": "kalshi",
                "question": "Will the high temperature in New York exceed 71 degrees?",
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
                id="KXHIGHNY-260506-T71",
                exchange="kalshi",
                question="Will the high temperature in New York exceed 71 degrees?",
                category="KXHIGHNY",
                yes_price=0.4,
                no_price=0.6,
                volume=1000,
                closes_at=datetime.now(timezone.utc) + timedelta(hours=6),
                metadata={"market_group": "weather", "market_family": "daily_temperature", "series_ticker": "KXHIGHNY", "series": "daily_temperature"},
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
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
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
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
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
        self.assertNotIn("score_only", _DaemonStub.kwargs["config_patch"]["prediction_lab"])
        self.assertEqual(_DaemonStub.kwargs["config_patch"]["prediction_lab"]["min_confidence_to_record"], 0.0)
        self.assertNotIn("min_edge_to_record", _DaemonStub.kwargs["config_patch"]["prediction_lab"])
        self.assertEqual(_DaemonStub.kwargs["config_patch"]["trading"]["mode"], "paper")
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
            self.assertEqual(default_config["prediction_lab"]["paper_lab_mode"], "opportunity")
            self.assertEqual(default_config["prediction_lab"]["opportunity_bankroll_usd"], 100.0)
            self.assertEqual(default_config["prediction_lab"]["fresh_wallet_bankroll_usd"], 100.0)
            self.assertEqual(alias_config["prediction_lab"]["hypothetical_notional_mode"], "fresh_kelly")
            self.assertEqual(alias_config["prediction_lab"]["paper_lab_mode"], "opportunity")
            self.assertEqual(alias_config["prediction_lab"]["opportunity_bankroll_usd"], 250.0)
            self.assertEqual(alias_config["prediction_lab"]["fresh_wallet_bankroll_usd"], 250.0)

            opportunity_path = Path(tmpdir) / "opportunity.yaml"
            opportunity_path.write_text(
                "prediction_lab:\n  hypothetical_notional_mode: opportunity\n  opportunity_bankroll_usd: 125\n"
            )
            opportunity_config = load_config(opportunity_path)

            self.assertEqual(opportunity_config["prediction_lab"]["hypothetical_notional_mode"], "fresh_kelly")
            self.assertEqual(opportunity_config["prediction_lab"]["opportunity_bankroll_usd"], 125.0)
            self.assertEqual(opportunity_config["prediction_lab"]["fresh_wallet_bankroll_usd"], 125.0)

    def test_prediction_lab_rows_include_fresh_wallet_kelly_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
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
            self.assertEqual(hypothetical["paper_lab_mode"], "paper_lab")
            self.assertEqual(hypothetical["opportunity_mode"], "opportunity")
            self.assertEqual(hypothetical["sizing_method"], "fresh_wallet_kelly")
            self.assertEqual(hypothetical["account_state_provider"], "fixed_opportunity")
            self.assertEqual(hypothetical["bankroll_usd"], 100.0)
            self.assertEqual(hypothetical["opportunity_bankroll_usd"], 100.0)
            self.assertFalse(hypothetical["mutates_portfolio_account"])
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
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
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
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
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
            self.assertNotIn("resolution", rows[0])
            self.assertAlmostEqual(resolutions[0]["resolution"]["net_pnl"], 18.6)
            self.assertAlmostEqual(resolutions[0]["resolution"]["position_size"], 20.0)

    def test_prediction_lab_resolution_links_back_to_shared_candidate_without_mutating_snapshot_truth(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
                "prediction_lab": {
                    "enabled": True,
                    "mode": "collector",
                    "groups": ["weather"],
                    "score_only": False,
                    "record_all_scored": True,
                    "collector_record_predictions": True,
                    "collector_record_market_snapshots": True,
                    "use_shared_pipeline": True,
                    "paper_lab_mode": "opportunity",
                    "flat_notional_usd": 10.0,
                },
                "strategy": {"min_edge": 0.01, "min_confidence": 0.5, "enable_news": False, "enable_social": False, "enable_ai": False},
                "max_entry_price": 0.7,
                "kalshi_fee_rate": 0.07,
            }
            lab = PredictionLab(config)
            signal = {
                "market_id": "KXHIGHNY-260506-T71",
                "exchange": "kalshi",
                "question": "Will the high temperature in New York exceed 71 degrees?",
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
                id="KXHIGHNY-260506-T71",
                exchange="kalshi",
                question="Will the high temperature in New York exceed 71 degrees?",
                category="KXHIGHNY",
                yes_price=0.4,
                no_price=0.6,
                volume=1000,
                closes_at=datetime.now(timezone.utc) + timedelta(hours=6),
                metadata={"market_group": "weather", "market_family": "daily_temperature", "series_ticker": "KXHIGHNY", "series": "daily_temperature"},
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

            result = lab.resolve_open_predictions(exchange)
            resolved_prediction = load_jsonl(lab.predictions_path)[0]
            resolution_row = load_jsonl(lab.resolutions_path)[0]

        shared = snapshot_row["shared_candidate"]
        self.assertEqual(result["resolved"], 1)
        self.assertEqual(shared["run_id"], prediction_row["run_id"])
        self.assertEqual(shared["run_id"], resolution_row["run_id"])
        self.assertEqual(shared["market_id"], prediction_row["market_id"])
        self.assertEqual(shared["market_id"], resolution_row["market_id"])
        self.assertEqual(shared["candidate_id"], snapshot_row["shared_candidate_id"])
        self.assertEqual(shared["candidate_id"], prediction_row["shared_candidate_id"])
        self.assertEqual(shared["candidate_id"], resolution_row["shared_candidate_id"])
        self.assertEqual(shared["main_runtime"], "prediction_lab")
        self.assertEqual(shared["main_decision"]["runtime"], "prediction_lab")
        self.assertTrue(snapshot_row["recorded_prediction"])
        self.assertTrue(snapshot_row["observer_mode"])
        self.assertFalse(snapshot_row["trading_enabled"])
        self.assertFalse(snapshot_row["order_execution_enabled"])
        self.assertFalse(snapshot_row["paper_lab"]["mutates_portfolio_account"])
        self.assertNotIn("resolution", shared)
        self.assertNotIn("resolution", resolved_prediction)
        self.assertAlmostEqual(resolution_row["resolution"]["position_size"], 10.0)
        self.assertAlmostEqual(resolution_row["resolution"]["entry_price"], 0.4)
        self.assertGreater(resolution_row["resolution"]["net_pnl"], 0.0)

    def test_prediction_lab_resolution_backfills_shared_candidate_id_from_snapshot_when_prediction_is_legacy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "prediction_lab": {
                    "enabled": True,
                    "mode": "collector",
                    "groups": ["weather"],
                    "score_only": False,
                    "record_all_scored": True,
                    "collector_record_predictions": True,
                    "collector_record_market_snapshots": True,
                    "flat_notional_usd": 10.0,
                },
                "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
                "kalshi_fee_rate": 0.07,
            }
            lab = PredictionLab(config)
            market = SimpleNamespace(
                id="KXHIGHNY-260506-T71",
                exchange="kalshi",
                question="Will the high temperature in New York exceed 71 degrees?",
                category="weather",
                yes_price=0.4,
                no_price=0.6,
                volume=1000,
                metadata={"market_group": "weather", "series": "daily_temperature"},
            )
            signal = {
                "market_id": market.id,
                "exchange": "kalshi",
                "question": market.question,
                "direction": "BUY_YES",
                "model_probability": 0.7,
                "market_price": 0.4,
                "yes_market_price": 0.4,
                "no_market_price": 0.6,
                "edge": 0.3,
                "confidence": 0.9,
                "signals": {"unit": 0.7},
            }
            observed_at = "2026-05-06T12:00:00+00:00"
            prediction_row = lab._build_prediction_row(
                "run-legacy",
                market,
                signal,
                decision_type="buy_yes",
                observed_at=observed_at,
            )
            prediction_row.pop("shared_candidate_id", None)
            snapshot_row = lab._build_market_snapshot_row(
                "run-legacy",
                market,
                signal,
                decision_type="buy_yes",
                prediction_recorded=True,
                observed_at=observed_at,
            )
            append_jsonl(lab.predictions_path, prediction_row)
            append_jsonl(lab.market_snapshots_path, snapshot_row)

            exchange = SimpleNamespace(
                _fetch_market_raw=lambda market_id: {
                    "status": "settled",
                    "result": "YES",
                    "close_price": 1.0,
                }
            )

            result = lab.resolve_open_predictions(exchange)
            resolution_row = load_jsonl(lab.resolutions_path)[0]

        self.assertEqual(result["resolved"], 1)
        self.assertNotIn("shared_candidate_id", prediction_row)
        self.assertEqual(resolution_row["run_id"], "run-legacy")
        self.assertEqual(resolution_row["market_id"], market.id)
        self.assertEqual(resolution_row["shared_candidate_id"], snapshot_row["shared_candidate_id"])

    def test_prediction_lab_emits_agent_runs_and_decisions_linked_to_shared_candidate_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
                "prediction_lab": {
                    "enabled": True,
                    "mode": "collector",
                    "groups": ["weather"],
                    "score_only": False,
                    "record_all_scored": True,
                    "collector_record_predictions": True,
                    "collector_record_market_snapshots": True,
                    "use_shared_pipeline": True,
                },
                "strategy": {"min_edge": 0.01, "min_confidence": 0.5, "enable_news": False, "enable_social": False, "enable_ai": False},
                "max_entry_price": 0.7,
            }
            lab = PredictionLab(config)
            signal = {
                "market_id": "KXHIGHNY-260506-T71",
                "exchange": "kalshi",
                "question": "Will the high temperature in New York exceed 71 degrees?",
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
                id="KXHIGHNY-260506-T71",
                exchange="kalshi",
                question="Will the high temperature in New York exceed 71 degrees?",
                category="KXHIGHNY",
                yes_price=0.4,
                no_price=0.6,
                volume=1000,
                closes_at=datetime.now(timezone.utc) + timedelta(hours=6),
                metadata={"market_group": "weather", "market_family": "daily_temperature", "series_ticker": "KXHIGHNY", "series": "daily_temperature"},
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

            result = lab.run(exchange)
            snapshot_row = load_jsonl(lab.market_snapshots_path)[0]
            agent_run_rows = load_jsonl(lab.agent_runs_path)
            decision_rows = load_jsonl(lab.agent_decisions_path)

        self.assertEqual(result.recorded_predictions, 1)
        self.assertEqual(len(agent_run_rows), 1)
        self.assertEqual(agent_run_rows[0]["agent_run_id"], f"prediction_lab:{result.run_id}")
        self.assertEqual(agent_run_rows[0]["run_id"], result.run_id)
        self.assertEqual(agent_run_rows[0]["candidate_dataset_path"], str(lab.market_snapshots_path))
        self.assertEqual(agent_run_rows[0]["decision_ledger_path"], str(lab.agent_decisions_path))
        self.assertFalse(agent_run_rows[0]["mutates_accounting"])
        self.assertEqual([row["decision_role"] for row in decision_rows], ["main", "normal", "prediction_lab_paper"])
        self.assertTrue(all(row["agent_run_id"] == f"prediction_lab:{result.run_id}" for row in decision_rows))
        self.assertTrue(all(row["run_id"] == result.run_id for row in decision_rows))
        self.assertTrue(all(row["shared_candidate_id"] == snapshot_row["shared_candidate_id"] for row in decision_rows))
        self.assertTrue(all(row["candidate_dataset_path"] == str(lab.market_snapshots_path) for row in decision_rows))
        paper_row = next(row for row in decision_rows if row["decision_role"] == "prediction_lab_paper")
        self.assertEqual(paper_row["accounting_ref"]["namespace"], str(Path(tmpdir) / "prediction_lab" / "paper_accounting"))
        self.assertNotIn("ledger_path", paper_row["accounting_ref"])
        self.assertFalse(paper_row["accounting_ref"]["mutates_balance"])
        self.assertFalse(paper_row["accounting_ref"]["mutates_accounting"])
        self.assertFalse(paper_row["accounting_ref"]["places_orders"])
        self.assertEqual(paper_row["accounting_ref"]["balance_model"], "fixed_opportunity")
        self.assertFalse(paper_row["mutation_contract"]["mutates_shared_candidate"])
        self.assertFalse(paper_row["mutation_contract"]["mutates_accounting"])
        self.assertFalse(paper_row["mutation_contract"]["places_orders"])
        self.assertEqual(paper_row["shared_candidate_id"], snapshot_row["shared_candidate_id"])
        self.assertNotIn("shadow_decision", snapshot_row)

    def test_prediction_lab_prediction_only_agent_decisions_keep_candidate_dataset_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
                "prediction_lab": {
                    "enabled": True,
                    "mode": "collector",
                    "groups": ["weather"],
                    "score_only": False,
                    "record_all_scored": True,
                    "collector_record_predictions": True,
                    "collector_record_market_snapshots": False,
                    "use_shared_pipeline": True,
                },
                "strategy": {"min_edge": 0.01, "min_confidence": 0.5, "enable_news": False, "enable_social": False, "enable_ai": False},
                "max_entry_price": 0.7,
            }
            lab = PredictionLab(config)
            signal = {
                "market_id": "KXHIGHNY-260506-T71",
                "exchange": "kalshi",
                "question": "Will the high temperature in New York exceed 71 degrees?",
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
                id="KXHIGHNY-260506-T71",
                exchange="kalshi",
                question="Will the high temperature in New York exceed 71 degrees?",
                category="KXHIGHNY",
                yes_price=0.4,
                no_price=0.6,
                volume=1000,
                closes_at=datetime.now(timezone.utc) + timedelta(hours=6),
                metadata={"market_group": "weather", "market_family": "daily_temperature", "series_ticker": "KXHIGHNY", "series": "daily_temperature"},
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

            result = lab.run(exchange)
            decision_rows = load_jsonl(lab.agent_decisions_path)

        self.assertEqual(result.recorded_predictions, 1)
        self.assertFalse(lab.market_snapshots_path.exists())
        self.assertTrue(decision_rows)
        self.assertTrue(all(row["run_id"] == result.run_id for row in decision_rows))
        self.assertTrue(all(row["candidate_dataset_path"] == str(lab.market_snapshots_path) for row in decision_rows))

    def test_prediction_lab_rows_include_weather_risk_metadata_when_derivable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
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
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
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
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
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
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
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
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
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

    def test_prediction_lab_records_disallowed_route_rejection_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "data_dir": tmpdir,
                "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
                "prediction_lab": {
                    "enabled": True,
                    "mode": "collector",
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
                id="KXPRIMEENGCONSUMPTION-30-WIND",
                exchange="kalshi",
                question="Will wind power account for at least 30% of prime energy consumption?",
                category="KXPRIMEENGCONSUMPTION",
                yes_price=0.4,
                no_price=0.6,
                volume=1000,
                closes_at=datetime.now(timezone.utc) + timedelta(hours=6),
                metadata={},
            )
            exchange = SimpleNamespace(get_markets_direct=lambda **kwargs: [market])

            result = lab.run(exchange)
            rows = load_jsonl(lab.market_snapshots_path)
            decision_rows = load_jsonl(lab.agent_decisions_path)

            self.assertEqual(result.recorded_predictions, 0)
            self.assertEqual(rows[0]["decision_artifact"]["final_reason_code"], "unknown_market_route")
            self.assertEqual(rows[0]["market_route"]["group"], "unknown")
            self.assertNotIn("prediction_lab_paper", [row["decision_role"] for row in decision_rows])


if __name__ == "__main__":
    unittest.main()
