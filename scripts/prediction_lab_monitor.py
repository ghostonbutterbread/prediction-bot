#!/usr/bin/env python3
"""Deterministic Prediction Lab health monitor.

This is intentionally non-LLM code: it checks whether the long-running
Prediction Lab collector is healthy, alerts only on state changes, and can
optionally trigger a separate OpenClaw agent/cron job for remediation.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Allow running as: python3 scripts/prediction_lab_monitor.py
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.config import load_config  # noqa: E402

EXPECTED_COLLECTOR_ARGV = [
    "python3",
    "scripts/prediction_lab_collect.py",
    "--config",
    "config.prediction_lab_weather_overnight.yaml",
]
DEFAULT_OPENCLAW_CANDIDATES = (
    "/home/linuxbrew/.linuxbrew/bin/openclaw",
    "/usr/local/bin/openclaw",
    "/usr/bin/openclaw",
)


@dataclass(slots=True)
class MonitorIssue:
    code: str
    message: str
    severity: str = "critical"


@dataclass(slots=True)
class MonitorResult:
    healthy: bool
    issues: list[MonitorIssue] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        if self.healthy:
            return "Prediction Lab healthy"
        return "; ".join(f"{issue.code}: {issue.message}" for issue in self.issues)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        return {"_json_error": str(exc)}


def prediction_lab_dir(config: dict[str, Any]) -> Path:
    return Path(config.get("data_dir", "data")) / "prediction_lab"


def normalize_cmdline(parts: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    for part in parts:
        if not part:
            continue
        if part.endswith("/python3") or part.endswith("/python"):
            normalized.append(Path(part).name)
        else:
            normalized.append(part)
    return normalized


def read_proc_cmdlines(proc_root: Path = Path("/proc")) -> list[tuple[int, list[str]]]:
    cmdlines: list[tuple[int, list[str]]] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        parts = [p.decode(errors="replace") for p in raw.split(b"\0") if p]
        if parts:
            cmdlines.append((int(entry.name), parts))
    return cmdlines


def collector_matches(cmdline: list[str], expected: list[str] = EXPECTED_COLLECTOR_ARGV) -> bool:
    cmdline = normalize_cmdline(cmdline)
    if not cmdline or Path(cmdline[0]).name not in {"python", "python3"}:
        return False

    script_index = None
    for index, part in enumerate(cmdline[1:], start=1):
        if Path(part).name == "prediction_lab_collect.py":
            script_index = index
            break
    if script_index is None:
        return False

    expected_config = expected[3] if len(expected) > 3 else "config.prediction_lab_weather_overnight.yaml"
    for index, part in enumerate(cmdline):
        if part == "--config" and index + 1 < len(cmdline):
            return Path(cmdline[index + 1]).name == Path(expected_config).name
    return Path(expected_config).name == "config.yaml"


def find_collector_processes(
    cmdlines: list[tuple[int, list[str]]] | None = None,
    *,
    expected: list[str] = EXPECTED_COLLECTOR_ARGV,
) -> list[dict[str, Any]]:
    if cmdlines is None:
        cmdlines = read_proc_cmdlines()
    matches = []
    for pid, cmdline in cmdlines:
        if collector_matches(cmdline, expected=expected):
            matches.append({"pid": pid, "cmdline": cmdline})
    return matches


def file_age_seconds(path: Path, *, now: datetime) -> float | None:
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except FileNotFoundError:
        return None
    return max(0.0, (now - mtime).total_seconds())


def file_health_details(path: Path, *, now: datetime) -> dict[str, Any]:
    details: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "size_bytes": 0,
        "age_seconds": None,
    }
    try:
        stat = path.stat()
    except FileNotFoundError:
        return details
    details["size_bytes"] = stat.st_size
    details["age_seconds"] = max(0.0, (now - datetime.fromtimestamp(stat.st_mtime, timezone.utc)).total_seconds())
    return details


def collector_log_candidates(lab_dir: Path) -> list[Path]:
    candidates: list[Path] = []
    log_dir = lab_dir / "logs"
    if log_dir.exists():
        candidates.extend(log_dir.glob("collector_*.log"))
    for path in (lab_dir / "collector.supervisor.log", lab_dir / "collector.log"):
        if path.exists():
            candidates.append(path)
    return candidates


def evaluate_health(
    config_path: Path,
    *,
    now: datetime | None = None,
    cmdlines: list[tuple[int, list[str]]] | None = None,
    stale_collect_multiplier: float = 3.0,
    stale_log_seconds: int = 1800,
) -> MonitorResult:
    now = now or utc_now()
    config = load_config(config_path)
    lab_cfg = config.get("prediction_lab", {}) or {}
    lab_dir = prediction_lab_dir(config)
    state_path = lab_dir / "state.json"
    state = read_json(state_path)
    issues: list[MonitorIssue] = []
    details: dict[str, Any] = {
        "config_path": str(config_path),
        "lab_dir": str(lab_dir),
        "state_path": str(state_path),
        "state": state,
    }

    required = {
        "observer_mode": True,
        "trading_enabled": False,
        "order_execution_enabled": False,
    }
    for key, expected in required.items():
        actual = state.get(key)
        if actual is not expected:
            issues.append(MonitorIssue("unsafe_state", f"state {key}={actual!r}, expected {expected!r}"))

    if not bool(lab_cfg.get("enabled", True)):
        issues.append(MonitorIssue("config_disabled", "prediction_lab.enabled is false"))
    if bool(lab_cfg.get("paused", False)):
        issues.append(MonitorIssue("config_paused", "prediction_lab.paused is true", severity="warning"))

    expected_cfg = {
        "observer_mode": True,
        "max_markets_per_run": 1000,
        "collection_storage_cap_gb": 100,
    }
    for key, expected in expected_cfg.items():
        actual = lab_cfg.get(key)
        if actual != expected:
            issues.append(MonitorIssue("config_drift", f"prediction_lab.{key}={actual!r}, expected {expected!r}", severity="warning"))

    expected_collector_argv = [
        "python3",
        "scripts/prediction_lab_collect.py",
        "--config",
        str(config_path),
    ]
    processes = find_collector_processes(cmdlines, expected=expected_collector_argv)
    details["collector_processes"] = processes
    if not processes:
        issues.append(MonitorIssue("collector_not_running", "collector process is not running"))
    elif len(processes) > 1:
        issues.append(MonitorIssue("multiple_collectors", f"{len(processes)} collector processes are running"))

    last_collect_at: datetime | None = None
    last_collect_age: float | None = None
    interval = max(1, int(lab_cfg.get("collector_interval_seconds", 900) or 900))
    stale_collect_seconds = int(interval * stale_collect_multiplier)

    if not state:
        issues.append(MonitorIssue("missing_state", f"state file missing or empty: {state_path}"))
    elif state.get("_json_error"):
        issues.append(MonitorIssue("bad_state_json", f"state JSON parse failed: {state['_json_error']}"))
    else:
        if state.get("last_error"):
            issues.append(MonitorIssue("collector_last_error", f"collector last_error={state.get('last_error')!r}"))
        if bool(state.get("paused", False)):
            issues.append(MonitorIssue("collector_paused", f"collector paused: {state.get('pause_reason') or state.get('paused_reason')}", severity="warning"))
        if state.get("run_state") == "errored":
            issues.append(MonitorIssue("collector_errored", "collector run_state is errored"))

        last_collect_at = parse_iso(state.get("last_collect_at"))
        if last_collect_at is None:
            issues.append(MonitorIssue("missing_last_collect", "state.last_collect_at is missing"))
        else:
            last_collect_age = (now - last_collect_at).total_seconds()
            details["last_collect_age_seconds"] = last_collect_age

        storage_gb = float(state.get("storage_usage_gb") or 0.0)
        cap_gb = float(lab_cfg.get("collection_storage_cap_gb", 0) or 0)
        details["storage_usage_gb"] = storage_gb
        details["storage_cap_gb"] = cap_gb
        if cap_gb > 0 and storage_gb >= cap_gb:
            issues.append(MonitorIssue("storage_cap_reached", f"storage {storage_gb:.3f} GB >= cap {cap_gb:.3f} GB"))

        heartbeat_at = parse_iso(state.get("last_storage_check_at"))
        if heartbeat_at is not None:
            details["state_heartbeat_age_seconds"] = (now - heartbeat_at).total_seconds()

    log_dir = lab_dir / "logs"
    log_candidates = collector_log_candidates(lab_dir)
    latest_log = max(log_candidates, key=lambda p: p.stat().st_mtime, default=None)
    details["collector_log_paths"] = [str(path) for path in log_candidates]
    details["latest_log"] = str(latest_log) if latest_log else None
    heartbeat_age = details.get("state_heartbeat_age_seconds")
    heartbeat_fresh = isinstance(heartbeat_age, (int, float)) and heartbeat_age <= stale_log_seconds
    latest_log_age = file_age_seconds(latest_log, now=now) if latest_log is not None else None
    log_fresh = isinstance(latest_log_age, (int, float)) and latest_log_age <= stale_log_seconds
    if latest_log is None:
        if not heartbeat_fresh:
            issues.append(MonitorIssue("missing_log", f"no collector logs under {log_dir} or supervisor logs under {lab_dir}"))
        else:
            details["latest_log_status"] = "missing_but_state_heartbeat_fresh"
    else:
        details["latest_log_age_seconds"] = latest_log_age
        if latest_log_age is not None and latest_log_age > stale_log_seconds and not heartbeat_fresh:
            issues.append(MonitorIssue("stale_log", f"latest collector log is {int(latest_log_age)}s old; threshold {stale_log_seconds}s"))

    market_snapshots = lab_dir / "market_snapshots.jsonl"
    replay_inputs = {"market_snapshots": file_health_details(market_snapshots, now=now)}
    details["replay_inputs"] = replay_inputs
    market_snapshot_details = replay_inputs["market_snapshots"]
    market_snapshot_age = market_snapshot_details["age_seconds"]
    market_snapshot_present = bool(market_snapshot_details["exists"]) and int(market_snapshot_details["size_bytes"] or 0) > 0
    market_snapshot_fresh = market_snapshot_present and isinstance(market_snapshot_age, (int, float)) and market_snapshot_age <= stale_collect_seconds
    market_snapshot_details["fresh"] = market_snapshot_fresh
    market_snapshot_details["fresh_threshold_seconds"] = stale_collect_seconds
    if not market_snapshot_details["exists"]:
        issues.append(MonitorIssue("missing_replay_input", f"market_snapshots.jsonl missing: {market_snapshots}", severity="warning"))
    elif int(market_snapshot_details["size_bytes"] or 0) <= 0:
        issues.append(MonitorIssue("empty_replay_input", f"market_snapshots.jsonl is empty: {market_snapshots}", severity="warning"))
    elif not market_snapshot_fresh:
        issues.append(
            MonitorIssue(
                "stale_replay_input",
                f"market_snapshots.jsonl is {int(market_snapshot_age)}s old; threshold {stale_collect_seconds}s",
                severity="warning",
            )
        )

    liveness_fresh = heartbeat_fresh or log_fresh or market_snapshot_fresh
    details["liveness_fresh"] = liveness_fresh
    if last_collect_age is not None and last_collect_age > stale_collect_seconds:
        severity = "warning" if processes and liveness_fresh else "critical"
        issues.append(
            MonitorIssue(
                "stale_collect",
                f"last collect is {int(last_collect_age)}s old; threshold {stale_collect_seconds}s",
                severity=severity,
            )
        )

    return MonitorResult(healthy=not any(issue.severity == "critical" for issue in issues), issues=issues, details=details)


def state_key(result: MonitorResult) -> str:
    if result.healthy:
        return "healthy"
    return "broken:" + ",".join(sorted(issue.code for issue in result.issues if issue.severity == "critical"))


def load_monitor_state(path: Path) -> dict[str, Any]:
    return read_json(path)


def save_monitor_state(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def should_notify(result: MonitorResult, monitor_state: dict[str, Any]) -> bool:
    current = state_key(result)
    return current != monitor_state.get("last_state_key")


def format_alert(result: MonitorResult) -> str:
    if result.healthy:
        return "✅ Prediction Lab recovered and is healthy again."
    lines = ["🚨 Prediction Lab monitor detected a problem:"]
    for issue in result.issues:
        marker = "CRIT" if issue.severity == "critical" else "WARN"
        lines.append(f"- [{marker}] {issue.code}: {issue.message}")
    details = result.details
    if details.get("collector_processes"):
        pids = ", ".join(str(p["pid"]) for p in details["collector_processes"])
        lines.append(f"- collector PID(s): {pids}")
    if details.get("latest_log"):
        lines.append(f"- latest log: {details['latest_log']}")
    lines.append("A repair agent can be invoked because the monitor itself is deterministic/non-LLM.")
    return "\n".join(lines)


def resolve_openclaw_bin() -> str | None:
    configured = os.environ.get("OPENCLAW_BIN")
    if configured:
        configured_path = Path(configured).expanduser()
        if configured_path.exists() and os.access(configured_path, os.X_OK):
            return str(configured_path)
    found = shutil.which("openclaw")
    if found:
        return found
    for candidate in DEFAULT_OPENCLAW_CANDIDATES:
        candidate_path = Path(candidate)
        if candidate_path.exists() and os.access(candidate_path, os.X_OK):
            return str(candidate_path)
    return None


def run_command(cmd: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout)


def send_telegram_alert(message: str, *, target: str, thread_id: str | None = None, silent: bool = False) -> bool:
    openclaw = resolve_openclaw_bin()
    if not openclaw:
        print("openclaw executable not found; set OPENCLAW_BIN or install it on PATH", file=sys.stderr)
        return False
    cmd = [openclaw, "message", "send", "--channel", "telegram", "--target", target, "--message", message]
    if thread_id:
        cmd.extend(["--thread-id", str(thread_id)])
    if silent:
        cmd.append("--silent")
    result = run_command(cmd)
    if result.returncode != 0:
        print(result.stderr or result.stdout, file=sys.stderr)
        return False
    return True


def trigger_repair_cron(job_id: str) -> bool:
    openclaw = resolve_openclaw_bin()
    if not openclaw:
        print("openclaw executable not found; set OPENCLAW_BIN or install it on PATH", file=sys.stderr)
        return False
    result = run_command([openclaw, "cron", "run", job_id])
    if result.returncode != 0:
        print(result.stderr or result.stdout, file=sys.stderr)
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic Prediction Lab monitor")
    parser.add_argument("--config", default="config.prediction_lab_weather_overnight.yaml")
    parser.add_argument("--state-file", default="data/paper/prediction_lab/monitor_state.json")
    parser.add_argument("--json", action="store_true", help="Print JSON result")
    parser.add_argument("--notify", action="store_true", help="Send Telegram alert on state changes")
    parser.add_argument("--target", default="-1003763915138", help="Telegram chat target")
    parser.add_argument("--thread-id", default="8", help="Telegram topic/thread id")
    parser.add_argument("--silent", action="store_true", help="Send Telegram alert silently")
    parser.add_argument("--repair-cron-job-id", help="OpenClaw cron job to run when unhealthy")
    parser.add_argument("--stale-collect-multiplier", type=float, default=3.0)
    parser.add_argument("--stale-log-seconds", type=int, default=1800)
    args = parser.parse_args(argv)

    result = evaluate_health(
        Path(args.config),
        stale_collect_multiplier=args.stale_collect_multiplier,
        stale_log_seconds=args.stale_log_seconds,
    )
    monitor_state_path = Path(args.state_file)
    monitor_state = load_monitor_state(monitor_state_path)
    notify = should_notify(result, monitor_state)

    output = {
        "healthy": result.healthy,
        "summary": result.summary(),
        "issues": [asdict(issue) for issue in result.issues],
        "details": result.details,
        "state_changed": notify,
    }
    if args.json:
        print(json.dumps(output, indent=2, sort_keys=True, default=str))
    else:
        print(output["summary"])

    if notify:
        alert = format_alert(result)
        if args.notify:
            send_telegram_alert(alert, target=args.target, thread_id=args.thread_id, silent=args.silent)
        if not result.healthy and args.repair_cron_job_id:
            trigger_repair_cron(args.repair_cron_job_id)

    monitor_state.update(
        {
            "last_state_key": state_key(result),
            "last_checked_at": utc_now().isoformat(),
            "last_healthy": result.healthy,
            "last_summary": result.summary(),
        }
    )
    save_monitor_state(monitor_state_path, monitor_state)
    return 0 if result.healthy else 2


if __name__ == "__main__":
    raise SystemExit(main())
