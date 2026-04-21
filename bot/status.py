"""Shared bot status and notification helpers for paper and live modes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json
import logging
import os
import subprocess
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BotStatusSnapshot:
    mode: str
    trading_enabled: bool
    tradable_cap: str
    max_position_size: str
    balance: float
    available_cash: str
    reserved_capital: str
    exposure: str
    pnl: float
    pnl_pct: float
    win_rate_pct: float
    total_trades: int
    open_trades: int
    resolved_trades: int
    scan_num: int = 0
    session_id: str = ""
    extra: dict[str, Any] | None = None


def _collect_log_storage_entries(config: dict[str, Any], *, project_root: str | Path) -> tuple[dict[str, Any], Path, list[tuple[Path, int, float]]]:
    storage_cfg = ((config or {}).get("storage", {}) or {}).get("logs", {}) or {}
    root = Path(project_root).resolve()
    include_paths = [root / Path(p) for p in storage_cfg.get("include_paths", [])]
    exclude_paths = [(root / Path(p)).resolve() for p in storage_cfg.get("exclude_paths", [])]

    def _is_excluded(path: Path) -> bool:
        for excluded in exclude_paths:
            try:
                path.relative_to(excluded)
                return True
            except ValueError:
                continue
        return False

    tracked: list[tuple[Path, int, float]] = []
    seen: set[Path] = set()
    for target in include_paths:
        if not target.exists():
            continue
        paths = [target]
        if target.is_dir():
            paths = [p for p in target.rglob("*") if p.is_file()]
        for path in paths:
            path = path.resolve()
            if path in seen or _is_excluded(path):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            seen.add(path)
            tracked.append((path, stat.st_size, stat.st_mtime))
    return storage_cfg, root, tracked


def summarize_log_storage(config: dict[str, Any], *, project_root: str | Path) -> dict[str, Any] | None:
    storage_cfg = ((config or {}).get("storage", {}) or {}).get("logs", {}) or {}
    if not storage_cfg.get("enabled", True):
        return None

    storage_cfg, root, tracked = _collect_log_storage_entries(config, project_root=project_root)
    total_bytes = sum(size for _, size, _ in tracked)
    max_total_gb = float(storage_cfg.get("max_total_gb", 50) or 50)
    max_bytes = int(max_total_gb * 1024 * 1024 * 1024)
    usage_pct = (total_bytes / max_bytes * 100) if max_bytes > 0 else 0.0
    tracked.sort(key=lambda item: item[1], reverse=True)

    return {
        "total_bytes": total_bytes,
        "max_bytes": max_bytes,
        "usage_pct": round(usage_pct, 1),
        "warning_threshold_pct": float(storage_cfg.get("warning_threshold_pct", 90) or 90),
        "hard_stop_threshold_pct": float(storage_cfg.get("hard_stop_threshold_pct", 105) or 105),
        "over_warning": usage_pct >= float(storage_cfg.get("warning_threshold_pct", 90) or 90),
        "over_hard_stop": usage_pct >= float(storage_cfg.get("hard_stop_threshold_pct", 105) or 105),
        "tracked_files": len(tracked),
        "largest_files": [
            {
                "path": os.path.relpath(str(path), str(root)),
                "bytes": size,
            }
            for path, size, _ in tracked[:5]
        ],
    }


def prune_log_storage(config: dict[str, Any], *, project_root: str | Path) -> dict[str, Any] | None:
    storage_cfg = ((config or {}).get("storage", {}) or {}).get("logs", {}) or {}
    if not storage_cfg.get("enabled", True) or not storage_cfg.get("auto_prune", False):
        return None

    storage_cfg, root, tracked = _collect_log_storage_entries(config, project_root=project_root)
    max_total_gb = float(storage_cfg.get("max_total_gb", 50) or 50)
    max_bytes = int(max_total_gb * 1024 * 1024 * 1024)
    total_bytes = sum(size for _, size, _ in tracked)
    if max_bytes <= 0 or total_bytes <= max_bytes:
        return {
            "performed": False,
            "before_bytes": total_bytes,
            "after_bytes": total_bytes,
            "bytes_reclaimed": 0,
            "pruned_files": [],
        }

    archive_preferred: list[tuple[Path, int, float]] = []
    other_candidates: list[tuple[Path, int, float]] = []
    for path, size, mtime in tracked:
        rel = os.path.relpath(str(path), str(root))
        if rel == "data/paper_loop.log":
            continue
        if rel == "data/paper_loop_runtime.log":
            continue
        if rel.startswith("data/archive/ops/") and rel != "data/archive/ops/prune_history.jsonl":
            archive_preferred.append((path, size, mtime))
        elif rel.startswith("logs/") or rel in {"data/watchdog.log", "data/watchdog_cron.log"}:
            other_candidates.append((path, size, mtime))

    policy = str(storage_cfg.get("prune_policy", "oldest_first") or "oldest_first")
    reverse = policy == "newest_first"
    archive_preferred.sort(key=lambda item: item[2], reverse=reverse)
    other_candidates.sort(key=lambda item: item[2], reverse=reverse)
    candidates = archive_preferred + other_candidates

    pruned_files: list[str] = []
    bytes_reclaimed = 0
    for path, size, _ in candidates:
        if total_bytes - bytes_reclaimed <= max_bytes:
            break
        try:
            path.unlink()
        except OSError:
            continue
        pruned_files.append(os.path.relpath(str(path), str(root)))
        bytes_reclaimed += size

    after_bytes = max(total_bytes - bytes_reclaimed, 0)
    result = {
        "performed": bool(pruned_files),
        "before_bytes": total_bytes,
        "after_bytes": after_bytes,
        "bytes_reclaimed": bytes_reclaimed,
        "pruned_files": pruned_files,
    }

    if pruned_files:
        history_path = root / "data/archive/ops/prune_history.jsonl"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "reason": "over_budget_auto_prune",
            "before_bytes": total_bytes,
            "after_bytes": after_bytes,
            "bytes_reclaimed": bytes_reclaimed,
            "files": pruned_files,
        }
        with history_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    return result


def _format_gb(num_bytes: int) -> str:
    return f"{num_bytes / (1024 ** 3):.2f} GB"


def build_snapshot(*, mode: str, trading_enabled: bool, tradable_cap: str, max_position_size: str,
                   balance: float, available_cash: str, reserved_capital: str, exposure: str,
                   pnl: float, pnl_pct: float, win_rate_pct: float, total_trades: int,
                   open_trades: int, resolved_trades: int, scan_num: int = 0,
                   session_id: str = "", extra: dict[str, Any] | None = None) -> BotStatusSnapshot:
    return BotStatusSnapshot(
        mode=mode,
        trading_enabled=trading_enabled,
        tradable_cap=tradable_cap,
        max_position_size=max_position_size,
        balance=balance,
        available_cash=available_cash,
        reserved_capital=reserved_capital,
        exposure=exposure,
        pnl=pnl,
        pnl_pct=pnl_pct,
        win_rate_pct=win_rate_pct,
        total_trades=total_trades,
        open_trades=open_trades,
        resolved_trades=resolved_trades,
        scan_num=scan_num,
        session_id=session_id,
        extra=extra or {},
    )


def format_status_message(snapshot: BotStatusSnapshot, *, reason: str) -> str:
    lines = [
        f"🤖 Bot status update ({reason})",
        f"scan={snapshot.scan_num}",
        f"mode={snapshot.mode}",
        f"trading_enabled={snapshot.trading_enabled}",
        f"tradable_cap={snapshot.tradable_cap}",
        f"max_position={snapshot.max_position_size}",
        f"balance=${snapshot.balance:.2f}",
        f"available_cash={snapshot.available_cash}",
        f"reserved_capital={snapshot.reserved_capital}",
        f"exposure={snapshot.exposure}",
        f"pnl={snapshot.pnl:+.2f} ({snapshot.pnl_pct:+.1f}%)",
        f"win_rate={snapshot.win_rate_pct:.0f}%",
        f"trades={snapshot.total_trades} ({snapshot.open_trades} open / {snapshot.resolved_trades} resolved)",
    ]
    if snapshot.session_id:
        lines.append(f"session={snapshot.session_id}")
    for key, value in (snapshot.extra or {}).items():
        if key == "log_storage" and isinstance(value, dict):
            lines.append(
                f"log_storage={_format_gb(value.get('total_bytes', 0))} / {_format_gb(value.get('max_bytes', 0))} ({value.get('usage_pct', 0)}%)"
            )
            if value.get("largest_files"):
                largest = value["largest_files"][0]
                lines.append(
                    f"log_storage_largest={largest.get('path')} ({_format_gb(largest.get('bytes', 0))})"
                )
            if value.get("over_hard_stop"):
                lines.append("log_storage_status=hard_stop")
            elif value.get("over_warning"):
                lines.append("log_storage_status=warning")
            else:
                lines.append("log_storage_status=ok")
            continue
        lines.append(f"{key}={value}")
    return "\n".join(lines)


def send_status_update(message: str, *, project_root: str | Path) -> bool:
    try:
        result = subprocess.run(
            ["python3", "scripts/send_alert.py", "-m", message],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return True
        logger.warning("Status alert failed: %s", (result.stderr or result.stdout).strip())
        return False
    except Exception as e:
        logger.warning("Status alert error: %s", e)
        return False
