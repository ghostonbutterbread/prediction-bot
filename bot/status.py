"""Shared bot status and notification helpers for paper and live modes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import logging
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
