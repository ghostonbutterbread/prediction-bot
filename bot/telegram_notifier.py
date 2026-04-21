"""Telegram delivery bridge for operator notifications."""

from __future__ import annotations

import subprocess
from typing import Any


class TelegramNotifier:
    def __init__(self, config: dict[str, Any] | None = None):
        cfg = config or {}
        self.enabled = bool(cfg.get("telegram_enabled", False))
        self.channel = cfg.get("telegram_channel", "telegram")
        self.target = str(cfg.get("telegram_target", "7104548956"))
        self.thread_id = str(cfg.get("telegram_thread_id", "")).strip()
        self.silent = bool(cfg.get("telegram_silent", False))

    def send(self, message: str) -> bool:
        if not self.enabled or not message:
            return False

        cmd = [
            "openclaw",
            "message",
            "send",
            "--channel",
            self.channel,
            "--target",
            self.target,
            "--message",
            message,
        ]
        if self.thread_id:
            cmd.extend(["--thread-id", self.thread_id])
        if self.silent:
            cmd.append("--silent")

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except Exception:
            return False
        return result.returncode == 0
