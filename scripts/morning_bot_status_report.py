#!/usr/bin/env python3
"""Deterministic morning Bot Status report.

This helper is intentionally read-only for runtime state: it detects active
paper/collector processes and formats a concise status message. It does not
start loops, run live trading, send notifications, or mutate cron state.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import analyze as paper_analyze  # noqa: E402
from scripts import prediction_lab_monitor as lab_monitor  # noqa: E402

PAPER_LOOP_SCRIPT = "paper_loop.py"
PREDICTION_LAB_COLLECT_SCRIPT = "prediction_lab_collect.py"
DEFAULT_PREDICTION_LAB_CONFIG = "config.prediction_lab_weather_overnight.yaml"
LIVE_MODE_NAME = "Live Trading"
PAPER_MODE_NAME = "Paper Trading"
COLLECTOR_MODE_NAME = "Prediction Lab Collector"
ALL_MODE_NAMES = (PAPER_MODE_NAME, COLLECTOR_MODE_NAME, LIVE_MODE_NAME)


def _script_matches(cmdline: list[str], script_name: str) -> bool:
    """Match a Python/direct script process without matching shell grep text."""
    normalized = lab_monitor.normalize_cmdline(cmdline)
    if not normalized:
        return False

    argv0 = Path(normalized[0]).name
    if argv0 in {"python", "python3"}:
        return any(Path(part).name == script_name for part in normalized[1:])
    return argv0 == script_name


def find_script_processes(
    script_name: str,
    cmdlines: list[tuple[int, list[str]]] | None = None,
) -> list[dict[str, Any]]:
    if cmdlines is None:
        cmdlines = lab_monitor.read_proc_cmdlines()

    return [
        {"pid": pid, "cmdline": cmdline}
        for pid, cmdline in cmdlines
        if _script_matches(cmdline, script_name)
    ]


def _main_command_matches(cmdline: list[str], command: str) -> bool:
    """Match `python main.py <command>` without matching shell wrapper text."""
    normalized = lab_monitor.normalize_cmdline(cmdline)
    if not normalized:
        return False

    argv0 = Path(normalized[0]).name
    if argv0 not in {"python", "python3"}:
        return False

    for index, part in enumerate(normalized[1:], start=1):
        if Path(part).name == "main.py" and index + 1 < len(normalized):
            return normalized[index + 1].lower() == command.lower()
    return False


def find_main_command_processes(
    command: str,
    cmdlines: list[tuple[int, list[str]]] | None = None,
) -> list[dict[str, Any]]:
    if cmdlines is None:
        cmdlines = lab_monitor.read_proc_cmdlines()
    return [
        {"pid": pid, "cmdline": cmdline}
        for pid, cmdline in cmdlines
        if _main_command_matches(cmdline, command)
    ]


def _format_pids(processes: list[dict[str, Any]]) -> str:
    return ", ".join(str(process["pid"]) for process in processes)


def latest_paper_report() -> str | None:
    analysis = paper_analyze.analyze(prune_logs=False)
    if not analysis.get("summary", {}).get("total_sessions"):
        return None
    return paper_analyze.format_report(analysis)


def format_paper_section(processes: list[dict[str, Any]], report: str | None) -> str:
    lines = [
        "**Paper Trading**",
        f"Process: active (PID(s): {_format_pids(processes)})",
    ]
    if report:
        lines.extend(["", report])
    else:
        lines.append("Latest paper analysis: unavailable (no paper sessions found).")
    return "\n".join(lines)


def format_live_section(processes: list[dict[str, Any]]) -> str:
    return "\n".join(
        [
            "**Live Trading**",
            f"Process: active (PID(s): {_format_pids(processes)})",
            "Mode: real-money live runner detected; report is read-only and did not start/stop it.",
        ]
    )


def format_inactive_section(inactive_modes: list[str]) -> str:
    if not inactive_modes:
        return "Inactive modes: none"
    return "Inactive modes: " + ", ".join(inactive_modes)


def format_prediction_lab_section(result: lab_monitor.MonitorResult) -> str:
    details = result.details
    processes = details.get("collector_processes") or []
    lines = [
        "**Prediction Lab Collector**",
        f"Status: {'healthy' if result.healthy else 'unhealthy'}",
    ]
    if processes:
        lines.append(f"Process: active (PID(s): {_format_pids(processes)})")
    else:
        lines.append("Process: active")

    if details.get("last_collect_age_seconds") is not None:
        lines.append(f"Last collect age: {int(details['last_collect_age_seconds'])}s")
    if details.get("latest_log"):
        lines.append(f"Latest log: {details['latest_log']}")
    if result.issues:
        issues = "; ".join(f"{issue.code}: {issue.message}" for issue in result.issues[:3])
        lines.append(f"Issues: {issues}")
    return "\n".join(lines)


def _collector_config_from_processes(processes: list[dict[str, Any]]) -> Path:
    for process in processes:
        cmdline = lab_monitor.normalize_cmdline(process.get("cmdline") or [])
        for index, part in enumerate(cmdline):
            if part == "--config" and index + 1 < len(cmdline):
                return Path(cmdline[index + 1])
    return Path("config.yaml") if processes else Path(DEFAULT_PREDICTION_LAB_CONFIG)


def build_report(
    *,
    cmdlines: list[tuple[int, list[str]]] | None = None,
    prediction_lab_config: Path | None = None,
    now: datetime | None = None,
) -> str:
    if cmdlines is None:
        cmdlines = lab_monitor.read_proc_cmdlines()
    now = now or datetime.now(timezone.utc)

    paper_processes = find_script_processes(PAPER_LOOP_SCRIPT, cmdlines)
    collector_processes = find_script_processes(PREDICTION_LAB_COLLECT_SCRIPT, cmdlines)
    live_processes = find_main_command_processes("live", cmdlines)
    active_modes: list[str] = []
    sections: list[str] = []

    paper_report: str | None = None
    if paper_processes:
        active_modes.append(PAPER_MODE_NAME)
        paper_report = latest_paper_report()
        sections.append(format_paper_section(paper_processes, paper_report))

    if collector_processes:
        active_modes.append(COLLECTOR_MODE_NAME)
        lab_config = prediction_lab_config or _collector_config_from_processes(collector_processes)
        lab_result = lab_monitor.evaluate_health(lab_config, now=now, cmdlines=cmdlines)
        sections.append(format_prediction_lab_section(lab_result))

    if live_processes:
        active_modes.append(LIVE_MODE_NAME)
        sections.append(format_live_section(live_processes))

    inactive_modes = [mode for mode in ALL_MODE_NAMES if mode not in active_modes]

    header = [
        f"🤖 **Morning Bot Status** — {now.isoformat(timespec='seconds')}",
        f"Active modes: {', '.join(active_modes) if active_modes else 'none'}",
        format_inactive_section(inactive_modes),
    ]

    if not active_modes:
        paper_report = latest_paper_report()
        if paper_report:
            sections.append("**Latest Paper Analysis**\n\n" + paper_report)
        else:
            sections.append("Latest paper analysis: unavailable (no paper sessions found).")

    return "\n\n".join(header + sections)


def build_json(
    *,
    cmdlines: list[tuple[int, list[str]]] | None = None,
    prediction_lab_config: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if cmdlines is None:
        cmdlines = lab_monitor.read_proc_cmdlines()
    now = now or datetime.now(timezone.utc)
    paper_processes = find_script_processes(PAPER_LOOP_SCRIPT, cmdlines)
    collector_processes = find_script_processes(PREDICTION_LAB_COLLECT_SCRIPT, cmdlines)
    live_processes = find_main_command_processes("live", cmdlines)
    lab_result = None
    if collector_processes:
        lab_config = prediction_lab_config or _collector_config_from_processes(collector_processes)
        lab_result = lab_monitor.evaluate_health(lab_config, now=now, cmdlines=cmdlines)
    return {
        "timestamp": now.isoformat(),
        "paper_trading_active": bool(paper_processes),
        "paper_processes": paper_processes,
        "prediction_lab_collector_active": bool(collector_processes),
        "prediction_lab_processes": collector_processes,
        "live_trading_active": bool(live_processes),
        "live_processes": live_processes,
        "prediction_lab_health": None
        if lab_result is None
        else {
            "healthy": lab_result.healthy,
            "summary": lab_result.summary(),
            "issues": [asdict(issue) for issue in lab_result.issues],
            "details": lab_result.details,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print deterministic morning Bot Status report")
    parser.add_argument(
        "--prediction-lab-config",
        help="Prediction Lab config to evaluate; defaults to the collector argv config or config.yaml",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable active-mode status")
    args = parser.parse_args(argv)

    config_path = Path(args.prediction_lab_config) if args.prediction_lab_config else None
    if args.json:
        print(json.dumps(build_json(prediction_lab_config=config_path), indent=2, sort_keys=True, default=str))
    else:
        print(build_report(prediction_lab_config=config_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
