#!/usr/bin/env python3
"""Deterministic morning Bot Status report.

This helper is intentionally read-only for runtime state: it detects active
paper/collector processes and formats a concise status message. It does not
start loops, run live trading, send notifications, or mutate cron state.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.paper_shadow_lanes import paper_shadow_lanes_enabled, summarize_paper_shadow_lane_report  # noqa: E402
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


def _config_from_processes(processes: list[dict[str, Any]]) -> Path | None:
    configs = _configs_from_processes(processes)
    return configs[0] if configs else None


def _configs_from_processes(processes: list[dict[str, Any]]) -> list[Path]:
    configs: list[Path] = []
    for process in processes:
        cmdline = lab_monitor.normalize_cmdline(process.get("cmdline") or [])
        for index, part in enumerate(cmdline):
            if part == "--config" and index + 1 < len(cmdline):
                configs.append(Path(cmdline[index + 1]))
                break
    return configs


def _paper_data_dir_from_config(config_path: Path | None) -> Path | None:
    if config_path is None:
        return None
    config = paper_analyze.load_config(config_path)
    data_dir = config.get("data_dir")
    return Path(data_dir) if data_dir else None


def latest_paper_report(config_path: Path | None = None) -> str | None:
    previous_env = {
        name: os.environ.get(name)
        for name in ("ANALYZE_CONFIG", "ANALYZE_DATA_DIR", "ANALYZE_DATA_DIR_ONLY")
    }
    data_dir = _paper_data_dir_from_config(config_path)
    try:
        if config_path is not None:
            os.environ["ANALYZE_CONFIG"] = str(config_path)
        if data_dir is not None:
            os.environ["ANALYZE_DATA_DIR"] = str(data_dir)
            os.environ["ANALYZE_DATA_DIR_ONLY"] = "1"

        analysis = paper_analyze.analyze(prune_logs=False)
    finally:
        for name, value in previous_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    if not analysis.get("summary", {}).get("total_sessions"):
        return None
    report = paper_analyze.format_report(analysis)
    return _strip_lower_detail(report)


def _strip_lower_detail(report: str) -> str:
    lines = report.splitlines()
    output: list[str] = []
    skipping = False
    for line in lines:
        if line in {"🔎 **Detail**", "🔎 **Lower Detail**"}:
            skipping = True
            continue
        if skipping and line.startswith(("⚠️ ", "🔧 ")):
            skipping = False
        if not skipping:
            output.append(line)
    while output and output[-1] == "":
        output.pop()
    return "\n".join(output)


def _paper_shadow_lane_status(config_path: Path | None) -> dict[str, Any] | None:
    if config_path is None:
        return None
    try:
        config = paper_analyze.load_config(config_path)
    except Exception:
        return None
    lane_config = config.get("paper_shadow_lanes") or config.get("paper_decision_lanes") or {}
    if not paper_shadow_lanes_enabled(config):
        return {"enabled": False, "lane_ids": [], "decision_path": None, "lane_row_counts": {}}

    lane_ids = lane_config.get("enabled_lanes") or []
    if isinstance(lane_ids, str):
        lane_ids = [part.strip() for part in lane_ids.split(",") if part.strip()]
    elif isinstance(lane_ids, dict):
        lane_ids = [str(lane_id) for lane_id, enabled in lane_ids.items() if enabled]
    else:
        lane_ids = [str(lane_id) for lane_id in lane_ids if str(lane_id).strip()]

    decision_path = lane_config.get("decision_ledger_path") or lane_config.get("ledger_path")
    if not decision_path:
        data_dir = config.get("data_dir") or _paper_data_dir_from_config(config_path)
        decision_path = str(Path(data_dir) / "paper_shadow_lane_decisions.jsonl") if data_dir else None

    row_counts: dict[str, int] = {}
    action_counts: dict[str, int] = {}
    scoreboard_readiness: dict[str, Any] = {}
    if decision_path and Path(decision_path).exists():
        try:
            summary = summarize_paper_shadow_lane_report(lane_decision_path=decision_path, config=config)
            row_counts = dict(summary.get("lane_row_counts") or {})
            action_counts = dict(summary.get("action_counts") or {})
            scoreboard_readiness = dict(summary.get("source_scoreboard_readiness") or {})
        except Exception:
            row_counts = {}
            action_counts = {}
            scoreboard_readiness = {}

    return {
        "enabled": True,
        "lane_ids": lane_ids,
        "decision_path": decision_path,
        "lane_row_counts": row_counts,
        "action_counts": action_counts,
        "source_scoreboard_readiness": scoreboard_readiness,
    }


def _format_paper_shadow_lane_status(status: dict[str, Any] | None) -> list[str]:
    if not status:
        return []
    if not status.get("enabled"):
        return ["• Paper shadow lanes: disabled"]
    lane_ids = list(status.get("lane_ids") or [])
    lines = ["• Paper shadow lanes: " + (", ".join(lane_ids) if lane_ids else "enabled")]
    row_counts = status.get("lane_row_counts") if isinstance(status.get("lane_row_counts"), dict) else {}
    if row_counts:
        compact_counts = ", ".join(f"{lane}={row_counts[lane]}" for lane in sorted(row_counts))
        lines.append(f"• Lane rows: {compact_counts}")
    decision_path = status.get("decision_path")
    if decision_path:
        lines.append(f"• Lane ledger: {decision_path}")
    readiness = status.get("source_scoreboard_readiness") if isinstance(status.get("source_scoreboard_readiness"), dict) else {}
    if readiness and int(readiness.get("evaluated_rows") or 0) > 0:
        lines.append(
            "• Source-scoreboard readiness: "
            f"rows={readiness.get('evaluated_rows', 0)}, "
            f"independent_labels={readiness.get('independent_label_rows', 0)}, "
            f"order_book={readiness.get('order_book_quote_rows', 0)}, "
            f"execution={readiness.get('execution_snapshot_rows', 0)}, "
            f"estimated_fill={readiness.get('estimated_fill_price_rows', 0)}"
        )
    return lines


def _paper_shadow_lane_status_from_processes(processes: list[dict[str, Any]]) -> dict[str, Any] | None:
    disabled_status: dict[str, Any] | None = None
    for config_path in _configs_from_processes(processes):
        status = _paper_shadow_lane_status(config_path)
        if status and status.get("enabled"):
            return status
        if status and disabled_status is None:
            disabled_status = status
    return disabled_status


def format_paper_section(processes: list[dict[str, Any]], report: str | None, lane_status: dict[str, Any] | None = None) -> str:
    lines = [
        "📄 **Paper Trading**",
        f"• 🟢 Process: active (PID(s): {_format_pids(processes)})",
    ]
    lines.extend(_format_paper_shadow_lane_status(lane_status))
    if report:
        lines.extend(["", report])
    else:
        lines.append("• ⚪ Latest paper analysis: unavailable (no paper sessions found).")
    return "\n".join(lines)


def format_live_section(processes: list[dict[str, Any]]) -> str:
    return "\n".join(
        [
            "💵 **Live Trading**",
            f"• 🔴 Process: active (PID(s): {_format_pids(processes)})",
            "• Mode: real-money live runner detected; report is read-only and did not start/stop it.",
        ]
    )


def format_inactive_section(inactive_modes: list[str]) -> str:
    if not inactive_modes:
        return "⚪ Inactive modes: none"
    return "⚪ Inactive modes: " + ", ".join(inactive_modes)


def format_prediction_lab_section(result: lab_monitor.MonitorResult) -> str:
    details = result.details
    processes = details.get("collector_processes") or []
    marker = "🟢" if result.healthy else "🟠"
    lines = [
        "🧪 **Prediction Lab Collector**",
        f"• {marker} Status: {'healthy' if result.healthy else 'unhealthy'}",
    ]
    if processes:
        lines.append(f"• Process: active (PID(s): {_format_pids(processes)})")
    else:
        lines.append("• Process: active")

    if details.get("last_collect_age_seconds") is not None:
        lines.append(f"• Last collect age: {int(details['last_collect_age_seconds'])}s")
    if details.get("latest_log"):
        lines.append(f"• Latest log: {details['latest_log']}")
    if result.issues:
        issues = "; ".join(f"{issue.code}: {issue.message}" for issue in result.issues[:3])
        lines.append(f"• ⚠️ Issues: {issues}")
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
        paper_config = _config_from_processes(paper_processes)
        paper_report = latest_paper_report(paper_config)
        sections.append(format_paper_section(paper_processes, paper_report, _paper_shadow_lane_status_from_processes(paper_processes)))

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
        f"✅ Active modes: {', '.join(active_modes) if active_modes else 'none'}",
        format_inactive_section(inactive_modes),
    ]

    if not active_modes:
        paper_report = latest_paper_report()
        if paper_report:
            sections.append("📊 **Latest Paper Analysis**\n\n" + paper_report)
        else:
            sections.append("⚪ Latest paper analysis: unavailable (no paper sessions found).")

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
        "paper_shadow_lanes": _paper_shadow_lane_status_from_processes(paper_processes) if paper_processes else None,
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
