from __future__ import annotations

import logging
import time
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from bot.config import ensure_mode_storage_dir, get_runtime_mode, load_config
from bot.prediction_lab import PredictionLab
from bot.prediction_lab_support import build_prediction_lab_exchange
from bot.resolution_feed import run_resolution_feed_once
from bot.shared_market_runtime import SharedMarketRuntimeManager, build_shared_market_snapshot_metadata

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PredictionLabCollectorStatus:
    collect_runs: int = 0
    resolve_runs: int = 0
    skipped_collects: int = 0
    pause_reason: str | None = None
    warning_emitted: bool = False
    exit_reason: str | None = None
    owner_lock_acquired: bool = False


class PredictionLabCollectorDaemon:
    def __init__(
        self,
        config_path: str | Path,
        *,
        demo: bool = False,
        verbose: bool = False,
        config_loader: Callable[[str | Path], dict[str, Any]] = load_config,
        exchange_builder: Callable[..., tuple[Any, Any]] = build_prediction_lab_exchange,
        resolution_feed_runner: Callable[..., Any] = run_resolution_feed_once,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
        config_patch: dict[str, Any] | None = None,
    ):
        self.config_path = Path(config_path)
        self.demo = demo
        self.verbose = verbose
        self.config_loader = config_loader
        self.exchange_builder = exchange_builder
        self.resolution_feed_runner = resolution_feed_runner
        self.sleep_fn = sleep_fn
        self.monotonic_fn = monotonic_fn
        self.config_patch = deepcopy(config_patch) if config_patch else None
        self.status = PredictionLabCollectorStatus()

    def _load_config(self) -> dict[str, Any]:
        config = self.config_loader(self.config_path)
        if self.config_patch:
            config = self._deep_merge(config, self.config_patch)
            trading_cfg = config.get("trading", {}) or {}
            if "enabled" in trading_cfg:
                config["trading_enabled"] = bool(trading_cfg.get("enabled"))
            elif "trading_enabled" in trading_cfg:
                config["trading_enabled"] = bool(trading_cfg.get("trading_enabled"))
            self._refresh_runtime_paths(config)
        config["_config_path"] = str(self.config_path)
        return config

    @staticmethod
    def _refresh_runtime_paths(config: dict[str, Any]) -> None:
        mode = get_runtime_mode(config)
        runtime = config.setdefault("runtime", {})
        base_dir = Path(runtime.get("base_dir", config.get("data_dir", "data")))
        mode_dir = ensure_mode_storage_dir(base_dir, mode)
        runtime["mode"] = mode
        runtime["base_dir"] = str(base_dir)
        runtime["mode_dir"] = str(mode_dir)
        config["data_dir"] = str(mode_dir)
        config["log_dir"] = str(mode_dir)
        config.setdefault("logging", {})["log_dir"] = str(mode_dir)

    @staticmethod
    def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        merged = dict(base)
        for key, value in override.items():
            if isinstance(merged.get(key), dict) and isinstance(value, dict):
                merged[key] = PredictionLabCollectorDaemon._deep_merge(merged[key], value)
            else:
                merged[key] = deepcopy(value)
        return merged

    @staticmethod
    def _iso_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _shared_market_runtime_enabled(config: dict[str, Any]) -> bool:
        lab_cfg = config.get("prediction_lab", {}) or {}
        shared_market_cfg = config.get("shared_market", {}) or {}
        return bool(lab_cfg.get("shared_market_runtime_enabled", False)) and bool(
            shared_market_cfg.get("enabled", True)
        )

    def _shared_market_instance_id(self, config: dict[str, Any]) -> str:
        lab_cfg = config.get("prediction_lab", {}) or {}
        instance_id = lab_cfg.get("shared_market_runtime_instance_id")
        if instance_id not in (None, ""):
            return str(instance_id)
        return f"collector:{self.config_path.resolve()}"

    @staticmethod
    def _shared_market_publisher_is_self(
        state: dict[str, Any] | None,
        *,
        runtime_kind: str,
        instance_id: str,
    ) -> bool:
        publisher = (state or {}).get("publisher")
        if not isinstance(publisher, dict):
            return False
        return (
            str(publisher.get("runtime_kind") or "") == runtime_kind
            and str(publisher.get("instance_id") or "") == instance_id
        )

    @staticmethod
    def _shared_market_source_exchange(exchange: Any) -> str:
        for attr in ("name", "exchange_name", "exchange"):
            value = getattr(exchange, attr, None)
            if value not in (None, ""):
                return str(value)
        return "kalshi"

    def _record_shared_market_snapshot_metadata(
        self,
        *,
        manager: SharedMarketRuntimeManager,
        instance_id: str,
        lab: PredictionLab,
        run_result,
        exchange: Any,
        config: dict[str, Any],
    ) -> None:
        lab_cfg = config.get("prediction_lab", {}) or {}
        shared_market_cfg = config.get("shared_market", {}) or {}
        observed_at = lab.state.get("last_collect_at") or self._iso_now()
        published_at = self._iso_now()
        ttl_seconds = shared_market_cfg.get("snapshot_ttl_seconds") or lab_cfg.get("collector_interval_seconds")
        metadata = build_shared_market_snapshot_metadata(
            snapshot_id=str(getattr(run_result, "run_id", "") or ""),
            observed_at=observed_at,
            published_at=published_at,
            publisher_runtime="collector",
            publisher_instance_id=instance_id,
            candidate_count=int(getattr(run_result, "scanned_markets", 0) or 0),
            market_count=int(getattr(run_result, "scanned_markets", 0) or 0),
            ttl_seconds=int(ttl_seconds) if ttl_seconds not in (None, "") else None,
            source_exchange=self._shared_market_source_exchange(exchange),
        )
        manager.attach(
            runtime_kind="collector",
            instance_id=instance_id,
            can_publish=True,
            can_consume=True,
            desired_interval_seconds=int(lab_cfg.get("collector_interval_seconds", 900) or 900),
        )
        state = manager.acquire_publisher_lease(
            runtime_kind="collector",
            instance_id=instance_id,
        )
        if not self._shared_market_publisher_is_self(
            state,
            runtime_kind="collector",
            instance_id=instance_id,
        ):
            logger.warning(
                "collector: shared market snapshot metadata skipped; publisher lease is no longer owned by this collector"
            )
            return
        manager.record_snapshot_metadata(metadata)

    @staticmethod
    def _parse_iso(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None

    @contextmanager
    def _owner_lock(self, lab: PredictionLab):
        lock_path = lab.root_dir / "prediction_lab.owner.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = lock_path.open("a+", encoding="utf-8")
        try:
            try:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.status.owner_lock_acquired = True
                yield True
            except BlockingIOError:
                self.status.exit_reason = "owner_locked"
                self.status.owner_lock_acquired = False
                yield False
        finally:
            if self.status.owner_lock_acquired:
                try:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
            fh.close()

    def run(self, *, max_cycles: int | None = None, idle_sleep_seconds: float = 5.0) -> PredictionLabCollectorStatus:
        if self.verbose:
            logger.info('collector: starting config_path=%s demo=%s max_cycles=%s idle_sleep_seconds=%s', self.config_path, self.demo, max_cycles, idle_sleep_seconds)
        config = self._load_config()
        lab = PredictionLab(config)
        bot = None
        exchange = None
        cycle = 0
        wall_now = datetime.now(timezone.utc)
        shared_market_manager: SharedMarketRuntimeManager | None = None
        shared_market_instance_id: str | None = None
        shared_market_attached = False

        with self._owner_lock(lab) as owner:
            if owner is False:
                if self.verbose:
                    logger.warning('collector: owner lock unavailable, exiting')
                return self.status
            if self.verbose:
                logger.info('collector: owner lock acquired root_dir=%s', lab.root_dir)
            try:
                while max_cycles is None or cycle < max_cycles:
                    cycle += 1
                    config = self._load_config()
                    lab = PredictionLab(config)
                    lab_cfg = config.get("prediction_lab", {}) or {}
                    timestamp = self._iso_now()
                    wall_now = datetime.now(timezone.utc)
                    if self.verbose:
                        logger.info('collector: cycle=%s mode=%s enabled=%s paused=%s continue_collecting=%s groups=%s', cycle, lab.mode, bool(lab_cfg.get('enabled', True)), bool(lab_cfg.get('paused', False)), bool(lab_cfg.get('continue_collecting', False)), lab.groups)

                    if not bool(lab_cfg.get("enabled", True)):
                        if self.verbose:
                            logger.info('collector: disabled in config, exiting')
                        self.status.exit_reason = "disabled"
                        lab.update_runtime_state(run_state="paused", paused=bool(lab_cfg.get("paused", False)), pause_reason="disabled", last_storage_check_at=timestamp)
                        break

                    if exchange is None:
                        if self.verbose:
                            logger.info('collector: building exchange')
                        try:
                            bot, exchange = self.exchange_builder(config, demo=self.demo, verbose=self.verbose)
                        except TypeError:
                            bot, exchange = self.exchange_builder(config, demo=self.demo)
                        if self.verbose:
                            logger.info('collector: exchange ready=%s', getattr(exchange, 'name', type(exchange).__name__))

                    storage = lab.storage_usage()
                    prior_pause_reason = str(lab.state.get("pause_reason") or "none")
                    warning_emitted = bool(lab.state.get("warning_emitted", False) or storage["warning_threshold_reached"])
                    paused = bool(lab_cfg.get("paused", False))
                    pause_reason = "manual_pause" if paused else "none"
                    if storage["over_cap"] and bool(lab_cfg.get("auto_pause_collection_on_storage_cap", True)):
                        paused = True
                        pause_reason = "storage_cap"

                    collect_interval = max(1, int(lab_cfg.get("collector_interval_seconds", 900) or 900))
                    resolve_interval = max(1, int(lab_cfg.get("resolve_interval_seconds", 1800) or 1800))
                    shared_market_enabled = self._shared_market_runtime_enabled(config)
                    shared_market_state = None
                    shared_market_is_publisher = False
                    collection_allowed = not paused and bool(lab_cfg.get("continue_collecting", False))
                    if not shared_market_enabled and shared_market_attached and shared_market_manager is not None and shared_market_instance_id is not None:
                        shared_market_manager.detach(
                            runtime_kind="collector",
                            instance_id=shared_market_instance_id,
                            now=wall_now,
                        )
                        shared_market_attached = False
                    if shared_market_enabled:
                        if shared_market_manager is None:
                            shared_market_manager = SharedMarketRuntimeManager(config=config)
                        next_instance_id = self._shared_market_instance_id(config)
                        if shared_market_attached and shared_market_instance_id not in (None, next_instance_id):
                            shared_market_manager.detach(
                                runtime_kind="collector",
                                instance_id=shared_market_instance_id,
                                now=wall_now,
                            )
                            shared_market_attached = False
                        shared_market_instance_id = next_instance_id
                        shared_market_manager.attach(
                            runtime_kind="collector",
                            instance_id=shared_market_instance_id,
                            can_publish=collection_allowed,
                            can_consume=True,
                            desired_interval_seconds=collect_interval,
                            now=wall_now,
                        )
                        if collection_allowed:
                            shared_market_state = shared_market_manager.acquire_publisher_lease(
                                runtime_kind="collector",
                                instance_id=shared_market_instance_id,
                                now=wall_now,
                            )
                        shared_market_attached = True
                        shared_market_is_publisher = self._shared_market_publisher_is_self(
                            shared_market_state,
                            runtime_kind="collector",
                            instance_id=shared_market_instance_id,
                        )
                    last_collect_at = self._parse_iso(lab.state.get("last_collect_at"))
                    last_resolve_at = self._parse_iso(lab.state.get("last_resolve_at"))
                    legacy_collect_due = last_collect_at is None or (wall_now - last_collect_at).total_seconds() >= collect_interval
                    collect_due = legacy_collect_due
                    if shared_market_enabled and shared_market_manager is not None and shared_market_instance_id is not None:
                        collect_due = shared_market_manager.publisher_snapshot_due_for(
                            runtime_kind="collector",
                            instance_id=shared_market_instance_id,
                            now=wall_now,
                        )
                    resolve_due = last_resolve_at is None or (wall_now - last_resolve_at).total_seconds() >= resolve_interval
                    open_prediction_count = int(lab.state.get("open_prediction_count") or 0)

                    run_state = "paused" if paused else "idle_watch"
                    if not paused and collect_due and bool(lab_cfg.get("continue_collecting", False)):
                        run_state = "active_collect"
                    elif resolve_due and open_prediction_count > 0:
                        run_state = "active_resolve"
                    elif not bool(lab_cfg.get("continue_collecting", False)) and open_prediction_count == 0:
                        run_state = "completed"
                    if self.verbose:
                        logger.info(
                            'collector: state decision cycle=%s run_state=%s pause_reason=%s collect_due=%s resolve_due=%s open_prediction_count=%s storage_gb=%.6f over_cap=%s warning=%s',
                            cycle,
                            run_state,
                            pause_reason,
                            collect_due,
                            resolve_due,
                            open_prediction_count,
                            float(storage['gb']),
                            bool(storage['over_cap']),
                            bool(storage['warning_threshold_reached']),
                        )

                    self.status.pause_reason = None if pause_reason == "none" else pause_reason
                    self.status.warning_emitted = warning_emitted
                    lab.update_runtime_state(
                        mode=lab.mode,
                        run_state=run_state,
                        paused=paused,
                        pause_reason=pause_reason,
                        last_storage_check_at=timestamp,
                        storage_usage_bytes=storage["bytes"],
                        storage_usage_gb=storage["gb"],
                        warning_emitted=warning_emitted,
                        active_group=lab.groups[0] if lab.groups else None,
                        experiment_id=lab.experiment_id,
                        strategy_version=lab.strategy_version,
                        last_error=None,
                    )

                    if resolve_due and open_prediction_count > 0 and exchange is not None:
                        if self.verbose:
                            logger.info('collector: resolve pass starting cycle=%s open_prediction_count=%s', cycle, open_prediction_count)
                        lab.update_runtime_state(run_state="active_resolve")
                        resolve_result = lab.resolve_open_predictions(exchange)
                        self.status.resolve_runs += 1
                        if self.verbose:
                            logger.info('collector: resolve pass finished cycle=%s result=%s', cycle, resolve_result)
                        lab = PredictionLab(self._load_config())
                        open_prediction_count = int(lab.state.get("open_prediction_count") or 0)

                    try:
                        resolution_feed_result = self.resolution_feed_runner(config, now=wall_now)
                        if self.verbose and getattr(resolution_feed_result, "refreshed", False):
                            logger.info(
                                "collector: resolution feed refreshed resolved=%s unresolved=%s errors=%s",
                                getattr(resolution_feed_result, "resolved_market_count", None),
                                getattr(resolution_feed_result, "unresolved_market_count", None),
                                getattr(resolution_feed_result, "fetch_error_count", None),
                            )
                    except Exception as exc:
                        logger.exception("collector: resolution feed refresh failed: %s", exc)

                    if paused:
                        if self.verbose:
                            logger.info('collector: collect skipped due to pause cycle=%s pause_reason=%s', cycle, pause_reason)
                        self.status.skipped_collects += 1
                    elif (
                        shared_market_enabled
                        and not shared_market_is_publisher
                        and legacy_collect_due
                        and bool(lab_cfg.get("continue_collecting", False))
                    ):
                        if self.verbose:
                            logger.info('collector: collect skipped; shared market publisher owned by another runtime cycle=%s', cycle)
                        self.status.skipped_collects += 1
                    elif collect_due and bool(lab_cfg.get("continue_collecting", False)):
                        if self.verbose:
                            logger.info('collector: collect pass starting cycle=%s max_markets_per_run=%s', cycle, getattr(lab, 'max_markets_per_run', None))
                        lab.update_runtime_state(run_state="active_collect", paused=False, pause_reason="none")
                        run_result = lab.run(exchange)
                        self.status.collect_runs += 1
                        if (
                            shared_market_enabled
                            and shared_market_manager is not None
                            and shared_market_instance_id is not None
                            and shared_market_is_publisher
                        ):
                            self._record_shared_market_snapshot_metadata(
                                manager=shared_market_manager,
                                instance_id=shared_market_instance_id,
                                lab=lab,
                                run_result=run_result,
                                exchange=exchange,
                                config=config,
                            )
                        if self.verbose:
                            logger.info('collector: collect pass finished cycle=%s scanned=%s recorded=%s ledger=%s', cycle, run_result.scanned_markets, run_result.recorded_predictions, run_result.ledger_path)
                        storage = lab.storage_usage()
                        post_pause_reason = "storage_cap" if storage["over_cap"] and bool(lab_cfg.get("auto_pause_collection_on_storage_cap", True)) else "none"
                        lab.update_runtime_state(
                            run_state="paused" if post_pause_reason != "none" else "active_collect",
                            paused=post_pause_reason != "none",
                            pause_reason=post_pause_reason,
                            last_storage_check_at=self._iso_now(),
                            storage_usage_bytes=storage["bytes"],
                            storage_usage_gb=storage["gb"],
                            warning_emitted=bool(warning_emitted or storage["warning_threshold_reached"]),
                            seed_complete=lab.mode == "seed_and_watch",
                            last_error=None,
                        )
                        if (
                            post_pause_reason != "none"
                            and shared_market_manager is not None
                            and shared_market_attached
                            and shared_market_instance_id is not None
                        ):
                            shared_market_manager.detach(
                                runtime_kind="collector",
                                instance_id=shared_market_instance_id,
                                now=wall_now,
                            )
                            shared_market_attached = False
                        self.status.pause_reason = None if post_pause_reason == "none" else post_pause_reason
                    else:
                        if not bool(lab_cfg.get("continue_collecting", False)):
                            run_state = "idle_watch" if open_prediction_count > 0 else "completed"
                            lab.update_runtime_state(run_state=run_state)

                    if prior_pause_reason != pause_reason and pause_reason == "none":
                        lab.update_runtime_state(run_state="active_collect" if bool(lab_cfg.get("continue_collecting", False)) else "idle_watch")

                    if self.verbose:
                        logger.info('collector: sleeping cycle=%s seconds=%s', cycle, idle_sleep_seconds)
                    self.sleep_fn(idle_sleep_seconds)
            except Exception as exc:
                logger.exception('collector: fatal error: %s', exc)
                lab.update_runtime_state(run_state="errored", last_error=str(exc))
                raise
            finally:
                if shared_market_manager is not None and shared_market_attached and shared_market_instance_id is not None:
                    shared_market_manager.detach(
                        runtime_kind="collector",
                        instance_id=shared_market_instance_id,
                    )
                if bot is not None:
                    bot.close()

        if self.status.exit_reason is None:
            self.status.exit_reason = "max_cycles" if max_cycles is not None else "stopped"
        return self.status
