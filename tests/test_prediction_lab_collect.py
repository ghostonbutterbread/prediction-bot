import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot.file_ops import append_jsonl, load_jsonl

from bot.config import load_config
from bot.prediction_lab import PredictionLab, PredictionLabRunResult
from bot.prediction_lab_collect import PredictionLabCollectorDaemon


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
