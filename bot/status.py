"""Shared bot status and notification helpers for paper and live modes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import logging
import os
import subprocess

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


def summarize_log_storage(config: dict[str, Any], *, project_root: str | Path) -> dict[str, Any] | None:
    storage_cfg = ((config or {}).get("storage", {}) or {}).get("logs", {}) or {}
    if not storage_cfg.get("enabled", True):
        return None

    root = Path(project_root)
    include_paths = [root / Path(p) for p in storage_cfg.get("include_paths", [])]
    exclude_paths = [root / Path(p) for p in storage_cfg.get("exclude_paths", [])]

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
