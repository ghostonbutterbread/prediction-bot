#!/usr/bin/env python3
"""Analyze paper/live simulation sessions and emit actionable summaries."""

import json
import glob
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.trade_audit import (
    coerce_float as audit_coerce_float,
    enrich_trade_audit_fields,
    group_trades_by_event,
    is_trade_effective_row,
    summarize_event_performance,
)
from bot.config import load_config
from bot.hidden_gem_evidence import (
    format_hidden_gem_evidence_summary,
    summarize_hidden_gem_evidence_cards,
)
from bot.prediction_lab_shadow_delta import (
    format_shadow_delta_summary,
    summarize_shadow_delta_rows,
)
from bot.status import prune_log_storage, summarize_log_storage
from bot.strategy_lane_reporting import (
    build_strategy_lane_rollout_readiness,
    format_strategy_lane_rollout_readiness,
    format_strategy_lane_summary,
    summarize_strategy_lanes,
)
from bot.strategy_policy import strategy_policy_status

DATA_DIR = Path(__file__).parent.parent / "data"
PACIFIC = timezone(timedelta(hours=-7))
DEFAULT_SESSION_GLOBS = ("paper/sim_*.json", "live/sim_*.json", "sim_*.json")
PREDICTION_LAB_SHADOW_DELTA_FILES = ("predictions.jsonl", "market_snapshots.jsonl")


def _coerce_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _session_sort_key(session: dict) -> tuple[str, float]:
    file_path = Path(session.get("_file", ""))
    try:
        mtime = file_path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (
        str(session.get("session_id") or ""),
        mtime,
    )


def _effective_trades(trades: list[dict]) -> tuple[list[dict], int]:
    effective = []
    ignored = 0
    for trade in trades or []:
        enrich_trade_audit_fields(trade)
        if not is_trade_effective_row(trade):
            ignored += 1
            continue
        effective.append(trade)
    return effective, ignored


def _build_position_performance(trades: list[dict]) -> dict:
    if not trades:
        return {}

    wins = [t for t in trades if audit_coerce_float(t.get("net_pnl", t.get("pnl")), 0.0) > 0]
    losses = [t for t in trades if audit_coerce_float(t.get("net_pnl", t.get("pnl")), 0.0) < 0]
    total_pnl = sum(audit_coerce_float(t.get("net_pnl", t.get("pnl")), 0.0) for t in trades)
    loss_total = sum(audit_coerce_float(t.get("net_pnl", t.get("pnl")), 0.0) for t in losses)

    return {
        "basis": "trusted_resolved_positions",
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "total_pnl": round(total_pnl, 2),
        "wins": len(wins),
        "losses": len(losses),
        "avg_win": round(
            sum(audit_coerce_float(t.get("net_pnl", t.get("pnl")), 0.0) for t in wins) / len(wins),
            2,
        ) if wins else 0,
        "avg_loss": round(
            sum(audit_coerce_float(t.get("net_pnl", t.get("pnl")), 0.0) for t in losses) / len(losses),
            2,
        ) if losses else 0,
        "profit_factor": round(
            sum(audit_coerce_float(t.get("net_pnl", t.get("pnl")), 0.0) for t in wins) / abs(loss_total)
            if losses and loss_total != 0 else 0,
            2,
        ),
    }


def _strategy_policy_status_from_latest(latest: dict, fallback_config: dict) -> dict:
    """Prefer session/report policy snapshots over today's config.yaml."""
    candidate_sources = [
        latest,
        latest.get("summary", {}) if isinstance(latest.get("summary"), dict) else {},
        latest.get("report", {}) if isinstance(latest.get("report"), dict) else {},
        latest.get("config_snapshot", {}) if isinstance(latest.get("config_snapshot"), dict) else {},
        latest.get("config", {}) if isinstance(latest.get("config"), dict) else {},
    ]
    for trade in reversed(latest.get("raw_trades", latest.get("trades", [])) or []):
        if not isinstance(trade, dict):
            continue
        artifact = trade.get("decision_artifact") if isinstance(trade.get("decision_artifact"), dict) else {}
        decision = artifact.get("shared_core_decision") if isinstance(artifact.get("shared_core_decision"), dict) else {}
        reasoning = decision.get("reasoning") if isinstance(decision.get("reasoning"), dict) else {}
        candidate_sources.extend([artifact, reasoning])

    for source in candidate_sources:
        status = source.get("strategy_policy_status") if isinstance(source, dict) else None
        if isinstance(status, dict) and status.get("version") and status.get("mode"):
            return dict(status)
        if isinstance(source, dict) and source.get("strategy_policy_normalized"):
            return strategy_policy_status(source.get("strategy_policy_normalized"))
        if isinstance(source, dict) and source.get("strategy_policy"):
            return strategy_policy_status(source.get("strategy_policy"))

    return strategy_policy_status(fallback_config.get("strategy_policy_normalized"))


def _analysis_config_path() -> Path:
    raw = os.getenv("ANALYZE_CONFIG")
    if raw:
        return Path(raw)
    return PROJECT_ROOT / "config.yaml"


def _isolated_env_data_dir_enabled() -> bool:
    return bool(os.getenv("ANALYZE_DATA_DIR")) and _env_bool("ANALYZE_DATA_DIR_ONLY")


def _disable_storage_audit(config: dict) -> dict:
    patched = dict(config or {})
    storage = dict(patched.get("storage", {}) or {})
    logs = dict(storage.get("logs", {}) or {})
    logs["enabled"] = False
    storage["logs"] = logs
    patched["storage"] = storage
    return patched


def analyze(*, prune_logs: bool = True) -> dict:
    """Run full analysis and return structured insights.

    Set prune_logs=False for read-only report contexts.
    """

    sessions = load_sessions()
    latest = sessions[-1] if sessions else {}
    trades = latest.get("trades", [])
    raw_trades = latest.get("raw_trades", trades)
    ignored_trades = latest.get("summary", {}).get("ignored_invalid_trades", 0)
    resolved = [t for t in trades if t.get("resolved")]
    trusted_resolved = [t for t in resolved if t.get("integrity_status") == "ok"]
    event_performance = summarize_event_performance(trusted_resolved)
    open_trades = [t for t in trades if not t.get("resolved")]
    open_event_groups = group_trades_by_event(open_trades)
    top_concentrated_events = sorted(
        (
            {
                "event_key": event_key,
                "open_positions": len(group),
                "reserved_capital": round(sum(audit_coerce_float(t.get("reserved_capital"), 0.0) for t in group), 2),
            }
            for event_key, group in open_event_groups.items()
        ),
        key=lambda row: (row["reserved_capital"], row["open_positions"]),
        reverse=True,
    )[:5]
    integrity_issue_counts = defaultdict(int)
    for trade in resolved:
        for issue in trade.get("integrity_errors", []) or []:
            integrity_issue_counts[issue] += 1

    result = {
        "timestamp": datetime.now(PACIFIC).isoformat(),
        "summary": {
            "total_sessions": len(sessions),
            "total_trades_ever": sum(len(s.get("trades", [])) for s in sessions),
            "current_session": latest.get("session_id", "?"),
            "current_session_file": latest.get("_file"),
            "current_trades": len(trades),
            "resolved": len(resolved),
            "trusted_resolved_positions": len(trusted_resolved),
            "invalid_resolved_positions": len(resolved) - len(trusted_resolved),
            "resolved_events": event_performance["resolved_events"],
            "retrade_count": event_performance.get("retrade_count", 0),
            "open_event_count": len(open_event_groups),
            "scans": latest.get("scan_count", 0),
            "total_equity": latest.get("balance"),
            "available_cash": latest.get("available_cash"),
            "reserved_capital": latest.get("reserved_capital"),
            "ignored_invalid_trades": ignored_trades,
        },
        "performance": {},
        "event_performance": {},
        "signal_quality": {},
        "strategy_breakdown": {},
        "integrity": {
            "invalid_resolved_positions": len(resolved) - len(trusted_resolved),
            "issue_counts": dict(sorted(integrity_issue_counts.items())),
        },
        "issues": [],
        "actions": [],
    }

    config = load_config(_analysis_config_path())
    if _isolated_env_data_dir_enabled() and not os.getenv("ANALYZE_CONFIG"):
        config = _disable_storage_audit(config)
    result["strategy_policy_status"] = _strategy_policy_status_from_latest(latest, config)
    result["strategy_lanes"] = summarize_strategy_lanes(raw_trades)
    result["hidden_gem_evidence_cards"] = summarize_hidden_gem_evidence_cards(raw_trades)
    result["shadow_delta"] = summarize_shadow_delta_rows(
        _iter_shadow_delta_analysis_rows(
            raw_trades,
            latest,
            include_prediction_lab_files=(result["strategy_policy_status"].get("shadow") is True),
        )
    )
    result["strategy_lane_rollout_readiness"] = build_strategy_lane_rollout_readiness(
        policy_status=result["strategy_policy_status"],
        strategy_lane_summary=result["strategy_lanes"],
        hidden_gem_evidence_summary=result["hidden_gem_evidence_cards"],
    )
    prune_result = prune_log_storage(config, project_root=PROJECT_ROOT) if prune_logs else None
    storage_summary = summarize_log_storage(config, project_root=PROJECT_ROOT)
    if storage_summary:
        result["storage"] = {
            "log_audit_usage_gb": round(storage_summary["total_bytes"] / (1024 ** 3), 2),
            "log_audit_cap_gb": round(storage_summary["max_bytes"] / (1024 ** 3), 2),
            "usage_pct": storage_summary["usage_pct"],
            "tracked_files": storage_summary["tracked_files"],
            "largest_files": storage_summary["largest_files"],
            "warning_threshold_pct": storage_summary["warning_threshold_pct"],
            "hard_stop_threshold_pct": storage_summary["hard_stop_threshold_pct"],
            "over_warning": storage_summary["over_warning"],
            "over_hard_stop": storage_summary["over_hard_stop"],
        }
        if prune_result and prune_result.get("performed"):
            result["storage"]["prune_result"] = prune_result
            result["issues"].append({
                "severity": "warning",
                "code": "LOG_STORAGE_PRUNED",
                "message": (
                    f"Auto-pruned {len(prune_result.get('pruned_files', []))} files and reclaimed "
                    f"{round(prune_result.get('bytes_reclaimed', 0) / (1024 ** 3), 2)} GB"
                ),
                "suggestion": "Review prune_history.jsonl and raise storage cap if pruning is too frequent",
            })
        if storage_summary["over_hard_stop"]:
            result["issues"].append({
                "severity": "critical",
                "code": "LOG_STORAGE_HARD_STOP",
                "message": (
                    f"Log/Audit storage {storage_summary['usage_pct']:.1f}% exceeds hard stop threshold "
                    f"({storage_summary['hard_stop_threshold_pct']:.0f}%)"
                ),
                "suggestion": "Prune archived logs or raise configured storage.logs.max_total_gb",
            })
        elif storage_summary["over_warning"]:
            result["issues"].append({
                "severity": "warning",
                "code": "LOG_STORAGE_WARNING",
                "message": (
                    f"Log/Audit storage {storage_summary['usage_pct']:.1f}% exceeds warning threshold "
                    f"({storage_summary['warning_threshold_pct']:.0f}%)"
                ),
                "suggestion": "Prepare to prune older audit logs or raise configured log budget",
            })

    if trusted_resolved:
        result["performance"] = _build_position_performance(trusted_resolved)
        result["event_performance"] = {
            "basis": "trusted_resolved_events",
            **event_performance,
            "open_event_concentration": {
                "open_events": len(open_event_groups),
                "top_concentrated_events": top_concentrated_events,
            },
        }

    if trades:
        edges = [t.get("edge", 0) for t in trades]
        confs = [t.get("confidence", 0) for t in trades]

        result["signal_quality"] = {
            "avg_edge": round(sum(edges) / len(edges) * 100, 2),
            "max_edge": round(max(edges) * 100, 2),
            "avg_confidence": round(sum(confs) / len(confs) * 100, 1),
            "edge_distribution": {
                "under_2pct": sum(1 for e in edges if e < 0.02),
                "2_to_4pct": sum(1 for e in edges if 0.02 <= e < 0.04),
                "4_to_6pct": sum(1 for e in edges if 0.04 <= e < 0.06),
            },
        }

    # Strategy breakdown
    by_direction = defaultdict(int)
    by_type = defaultdict(int)
    for t in trades:
        by_direction[t.get("direction", "?")] += 1
        sigs = t.get("signals", {})
        sig_type = sigs.get("type", "strategy") if isinstance(sigs, dict) else "strategy"
        by_type[sig_type] += 1

    result["strategy_breakdown"] = {
        "by_direction": dict(by_direction),
        "by_type": dict(by_type),
    }

    # === ISSUE DETECTION ===
    issues = detect_issues(trades, resolved, trusted_resolved, latest)
    result["issues"] = issues

    # === ACTIONABLE RECOMMENDATIONS ===
    actions = generate_actions(issues, result)
    result["actions"] = actions

    return result


def detect_issues(trades: list, resolved: list, trusted_resolved: list, session: dict) -> list:
    """Detect issues programmatically."""
    issues = []

    # 1. No resolution after many trades
    if len(trades) > 100 and len(resolved) == 0:
        issues.append({
            "severity": "warning",
            "code": "NO_RESOLUTIONS",
            "message": f"{len(trades)} trades, 0 resolved — markets too long-term",
            "suggestion": "Focus on shorter-term markets (sports, daily events)",
        })

    # 2. Low edge
    if trades:
        avg_edge = sum(t.get("edge", 0) for t in trades) / len(trades)
        if avg_edge < 0.02:
            issues.append({
                "severity": "warning",
                "code": "LOW_EDGE",
                "message": f"Average edge {avg_edge*100:.1f}% is below 2%",
                "suggestion": "Raise MIN_EDGE or improve signal generation",
            })

    # 3. Direction bias
    if trades:
        buy_yes = sum(1 for t in trades if t.get("direction") == "BUY_YES")
        ratio = buy_yes / len(trades)
        if ratio > 0.9 or ratio < 0.1:
            issues.append({
                "severity": "warning",
                "code": "DIRECTION_BIAS",
                "message": f"BUY_YES ratio: {ratio:.0%} — extreme bias detected",
                "suggestion": "Strategy may be missing NO opportunities or vice versa",
            })

    # 4. Duplicate trades
    market_ids = [t.get("market_id", "") for t in trades]
    dupes = len(market_ids) - len(set(market_ids))
    if dupes > 0:
        issues.append({
            "severity": "error",
            "code": "DUPLICATE_TRADES",
            "message": f"{dupes} duplicate trades on same market",
            "suggestion": "Enable traded_markets dedup check",
        })

    # 5. Position size concentration
    if trades:
        sizes = [t.get("position_size", 0) for t in trades]
        max_size = max(sizes)
        avg_size = sum(sizes) / len(sizes)
        if max_size > avg_size * 5:
            issues.append({
                "severity": "info",
                "code": "SIZE_OUTLIER",
                "message": f"Max position ${max_size:.2f} is {max_size/avg_size:.0f}x average",
                "suggestion": "Check if Kelly sizing is calculating correctly",
            })

    # 6. Win rate too low (if we have resolutions)
    if len(trusted_resolved) >= 10:
        wins = sum(
            1 for t in trusted_resolved
            if audit_coerce_float(t.get("net_pnl", t.get("pnl")), 0.0) > 0
        )
        wr = wins / len(trusted_resolved)
        if wr < 0.40:
            issues.append({
                "severity": "critical",
                "code": "LOW_WINRATE",
                "message": f"Win rate {wr:.0%} is below 40% — losing money",
                "suggestion": "Review strategy signals, consider disabling underperforming ones",
            })

    invalid_resolved = len(resolved) - len(trusted_resolved)
    if invalid_resolved > 0:
        issues.append({
            "severity": "error",
            "code": "UNTRUSTED_RESOLVED_ROWS",
            "message": f"{invalid_resolved} resolved rows failed accounting integrity checks",
            "suggestion": "Inspect integrity_errors on resolved rows before trusting paper P&L",
        })

    ignored_trades = session.get("summary", {}).get("ignored_invalid_trades")
    if ignored_trades:
        issues.append({
            "severity": "warning",
            "code": "INVALID_TRADES_IGNORED",
            "message": f"Ignored {ignored_trades} zero-sized or malformed trade rows in reporting",
            "suggestion": "Clean persisted sessions so accounting matches executed trades only",
        })

    return issues


def generate_actions(issues: list, result: dict) -> list:
    """Generate specific actionable recommendations."""
    actions = []

    for issue in issues:
        code = issue["code"]

        if code == "NO_RESOLUTIONS":
            actions.append({
                "priority": 1,
                "action": "Add sports market filter for events closing within 24h",
                "file": "bot/strategies/sports.py",
                "status": "already_built",
            })
            actions.append({
                "priority": 2,
                "action": "Increase sports market fetch limit from 30 to 100",
                "file": "bot/simulator.py",
                "line": "markets = exchange.get_markets(limit=100)",
            })

        elif code == "LOW_EDGE":
            actions.append({
                "priority": 1,
                "action": "Raise MIN_EDGE from 0.015 to 0.025",
                "file": ".env",
                "line": "MIN_EDGE=0.025",
            })

        elif code == "DIRECTION_BIAS":
            yes_count = result.get("strategy_breakdown", {}).get("by_direction", {}).get("BUY_YES", 0)
            no_count = result.get("strategy_breakdown", {}).get("by_direction", {}).get("BUY_NO", 0)
            actions.append({
                "priority": 2,
                "action": f"Add more NO-side analysis — currently {yes_count} YES vs {no_count} NO",
                "file": "bot/strategies/enhanced.py",
            })

        elif code == "DUPLICATE_TRADES":
            actions.append({
                "priority": 1,
                "action": "Ensure traded_markets dedup is active",
                "file": "bot/simulator.py",
                "status": "already_fixed",
            })

        elif code == "UNTRUSTED_RESOLVED_ROWS":
            actions.append({
                "priority": 1,
                "action": "Re-run resolution or clean malformed resolved rows before using paper P&L",
                "file": "bot/resolver.py",
            })

    # Always suggest focusing on short-term
    if result["summary"]["total_trades_ever"] > 500:
        actions.append({
            "priority": 3,
            "action": "Prioritize sports/injury markets over long-term political markets",
            "file": "bot/simulator.py",
        })

    return sorted(actions, key=lambda x: x["priority"])


def _append_lower_detail(lines: list[str], detail_lines: list[str]) -> None:
    compact = [
        part.strip()
        for line in detail_lines
        for part in str(line).split(" | ")
        if part.strip()
    ]
    if not compact:
        return
    lines.extend(["", "🔎 **Lower Detail**"])
    lines.extend(f"• {line}" for line in compact)


def _money(value, *, fallback: str = "unresolved") -> str:
    try:
        if value is None:
            return fallback
        return f"${float(value):+.2f}"
    except (TypeError, ValueError):
        return fallback


def _shadow_opportunities(shadow_delta: dict | None) -> int:
    if not isinstance(shadow_delta, dict):
        return 0
    return int(shadow_delta.get("total_shadow_delta_opportunities") or shadow_delta.get("shadow_delta_opportunities") or 0)


def _shadow_summary_cell(shadow_delta: dict | None) -> str:
    opportunities = _shadow_opportunities(shadow_delta)
    if not isinstance(shadow_delta, dict) or opportunities <= 0:
        return "0 opp"
    changed = int(shadow_delta.get("changed_rows") or 0)
    action = int(shadow_delta.get("action_changed") or 0)
    size = int(shadow_delta.get("size_changed") or 0)
    lane = int(shadow_delta.get("lane_changed") or 0)
    return f"{opportunities} opp, {changed} changed, action {action}, size {size}, lane {lane}"


def _dashboard_row(metric: str, paper: str, shadow: str | None = None) -> str:
    if shadow is None:
        return f"• {metric}: {paper}"
    return f"`{metric:<16} {paper:<24} {shadow}`"


def _format_policy_features(policy_status: dict) -> str:
    features = policy_status.get("enabled_features") if isinstance(policy_status.get("enabled_features"), dict) else {}
    return ", ".join(sorted(name for name, enabled in features.items() if enabled is True)) or "none"


def _format_status_line(issues: list[dict]) -> str:
    if not issues:
        return "ok"
    highest = issues[0]
    code = highest.get("code") or highest.get("severity") or "issue"
    return f"{len(issues)} issue(s), first={code}"


def _shadow_eval_rows(analysis: dict) -> int:
    for key in ("strategy_lanes", "hidden_gem_evidence_cards"):
        value = analysis.get(key)
        if isinstance(value, dict) and int(value.get("rows_scanned") or 0) > 0:
            return int(value.get("rows_scanned") or 0)
    return _shadow_opportunities(analysis.get("shadow_delta"))


def _short_shadow_cell(analysis: dict) -> str:
    rows = _shadow_eval_rows(analysis)

    if not rows:
        rows = _shadow_opportunities(analysis.get("shadow_delta"))
    parts = [f"{rows} eval" if rows else "on"]
    hidden = analysis.get("hidden_gem_evidence_cards") if isinstance(analysis.get("hidden_gem_evidence_cards"), dict) else {}
    lanes = analysis.get("strategy_lanes") if isinstance(analysis.get("strategy_lanes"), dict) else {}
    if hidden:
        parts.append(f"cards {int(hidden.get('card_rows') or 0)}/{int(hidden.get('rows_scanned') or 0)}")
        beta_rejected = int(hidden.get("beta_rejected_cards") or 0)
        if beta_rejected:
            parts.append(f"beta reject {beta_rejected}")
    if lanes:
        shadow_sizing = int(lanes.get("lane_sizing_shadow_rows") or 0)
        if shadow_sizing:
            parts.append(f"shadow size {shadow_sizing}")
    return "; ".join(parts)


def format_report(analysis: dict) -> str:
    """Format analysis into a compact Telegram dashboard."""
    s = analysis["summary"]
    p = analysis.get("performance", {})
    policy_status = analysis.get("strategy_policy_status") or {}
    shadow_running = policy_status.get("shadow") is True or _shadow_opportunities(analysis.get("shadow_delta")) > 0
    current_trades = int(s.get("current_trades") or 0)
    resolved_raw = int(s.get("resolved") or 0)
    trusted_resolved = int(s.get("trusted_resolved_positions") or 0)
    open_positions = max(0, current_trades - resolved_raw)
    pnl = _money(p.get("total_pnl") if p else None)
    policy_label = (
        f"{policy_status.get('version', 'stable')}/{policy_status.get('mode', 'off')}"
        if policy_status
        else "unknown"
    )
    status_line = _format_status_line(analysis.get("issues") or [])

    # The paper loop is the execution/logging path when paper trades exist.
    # If no paper rows exist but shadow evidence does, mark shadow as active.
    active_column = "shadow" if shadow_running and current_trades == 0 else "paper"
    paper_marker = "▶ " if active_column == "paper" else ""
    shadow_marker = "▶ " if active_column == "shadow" else ""

    lines = [
        f"📊 **Bot Report** — {analysis['timestamp'][:16]}",
        "",
        "📋 **Paper / Shadow**" if shadow_running else "📋 **Paper**",
    ]
    if shadow_running:
        lines.extend(
            [
                "`Metric        Paper                 Shadow`",
                _dashboard_row("Trading", f"{paper_marker}paper sim", f"{shadow_marker}{policy_label}"),
                _dashboard_row("Trades", f"{paper_marker}{current_trades}", f"{shadow_marker}{_short_shadow_cell(analysis)}"),
                _dashboard_row("Open/closed", f"{paper_marker}{open_positions} / {trusted_resolved} closed", f"{shadow_marker}eval / no close"),
                _dashboard_row("PnL", f"{paper_marker}{pnl}", f"{shadow_marker}not executed"),
                _dashboard_row("Status", status_line, "same"),
                "• ▶ = actual execution/logging path",
            ]
        )
    else:
        lines.extend(
            [
                _dashboard_row("Trading", f"paper sim; {policy_label}"),
                _dashboard_row("Trades", str(current_trades)),
                _dashboard_row("Open positions", str(open_positions)),
                _dashboard_row("Closed positions", f"{trusted_resolved} trusted / {resolved_raw} raw"),
                _dashboard_row("PnL", pnl),
                _dashboard_row("Status", status_line),
            ]
        )

    lines.extend([
        "",
        "📌 **Snapshot**",
        f"• Strategy policy: {policy_label}",
        f"• active={policy_status.get('active') is True} shadow={policy_status.get('shadow') is True} enforce={policy_status.get('enforce') is True}",
        f"• features={_format_policy_features(policy_status)}",
        f"• Session: {s['current_session']}",
        f"• Scans: {s['scans']}",
        f"• Resolved: {resolved_raw} raw / {trusted_resolved} trusted / {s.get('resolved_events', 0)} events",
    ])
    if s.get("current_session_file"):
        lines.append(f"• Source: {s['current_session_file']}")

    hidden_gem_line = format_hidden_gem_evidence_summary(analysis.get("hidden_gem_evidence_cards"))
    strategy_lane_line = format_strategy_lane_summary(analysis.get("strategy_lanes"))
    readiness_line = format_strategy_lane_rollout_readiness(analysis.get("strategy_lane_rollout_readiness"))
    detail_lines = []
    shadow_delta_line = format_shadow_delta_summary(analysis.get("shadow_delta"))
    if analysis.get("event_performance"):
        ep = analysis.get("event_performance") or {}
        detail_lines.append(f"Event Win Rate: {ep.get('win_rate', 0)}%")
    for line in (hidden_gem_line, strategy_lane_line, shadow_delta_line, readiness_line):
        if line:
            detail_lines.extend(part.strip() for part in str(line).split(" | ") if part.strip())
    if detail_lines:
        lines.extend(["", "🔎 **Detail**"])
        lines.extend(f"• {line}" for line in detail_lines)

    if analysis["issues"]:
        lines.extend(["", "⚠️ **Issues:**"])
        for i in analysis["issues"]:
            emoji = {"critical": "🔴", "error": "🟠", "warning": "🟡", "info": "🔵"}.get(i["severity"], "⚪")
            code = f" [{i['code']}]" if i.get("code") else ""
            lines.append(f"• {emoji}{code} {i['message']}")

    if analysis["actions"]:
        lines.extend(["", "🔧 **Actions:**"])
        for a in analysis["actions"][:3]:
            lines.append(f"• [{a['priority']}] {a['action']} → {a.get('file', '?')}")

    return "\n".join(lines)

def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_sessions() -> list:
    sessions = []
    roots = _analysis_data_roots()

    seen_files = set()
    for root in roots:
        for pattern in DEFAULT_SESSION_GLOBS:
            for f in sorted(glob.glob(str(root / pattern))):
                if f in seen_files:
                    continue
                try:
                    with open(f) as fp:
                        session = json.load(fp)
                        session["_file"] = f
                        raw_trade_rows = list(session.get("trades", []) or [])
                        trade_rows, ignored = _effective_trades(raw_trade_rows)
                        session["raw_trades"] = raw_trade_rows
                        session["trades"] = trade_rows
                        session.setdefault("summary", {})
                        session["summary"]["ignored_invalid_trades"] = ignored
                        sessions.append(session)
                        seen_files.add(f)
                except Exception:
                    pass
    sessions.sort(key=_session_sort_key)
    return sessions


def _analysis_data_roots() -> list[Path]:
    roots = []
    env_data_dir = os.getenv("ANALYZE_DATA_DIR")
    if env_data_dir:
        roots.append(Path(env_data_dir))
    if not env_data_dir or not _env_bool("ANALYZE_DATA_DIR_ONLY"):
        roots.append(DATA_DIR)
    return roots


def _iter_shadow_delta_analysis_rows(
    raw_trades: list[dict],
    latest_session: dict | None = None,
    *,
    include_prediction_lab_files: bool = False,
):
    for trade in raw_trades or []:
        if isinstance(trade, dict) and isinstance(trade.get("shadow_delta"), dict):
            yield trade

    if not include_prediction_lab_files:
        return

    seen_paths: set[str] = set()
    for lab_dir in _prediction_lab_dirs_for_session(latest_session or {}):
        for filename in PREDICTION_LAB_SHADOW_DELTA_FILES:
            path = lab_dir / filename
            path_key = str(path.expanduser().resolve(strict=False))
            if path_key in seen_paths:
                continue
            seen_paths.add(path_key)
            if not path.exists():
                continue
            yield from _iter_shadow_delta_jsonl_rows(path)


def _prediction_lab_dirs_for_session(session: dict) -> list[Path]:
    source_file = session.get("_file")
    if not source_file:
        return []
    source_path = Path(source_file)
    parent = source_path.parent
    dirs = [parent / "prediction_lab"]
    if parent.name in {"paper", "live"}:
        dirs.append(parent.parent / "prediction_lab")
    return dirs


def _iter_shadow_delta_jsonl_rows(path: Path):
    try:
        with path.open("rb") as fh:
            for raw_line in fh:
                try:
                    row = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(row, dict) and isinstance(row.get("shadow_delta"), dict):
                    row["_source_path"] = str(path)
                    yield row
    except OSError:
        return


if __name__ == "__main__":
    import sys
    analysis = analyze()

    if "--json" in sys.argv:
        print(json.dumps(analysis, indent=2))
    elif "--report" in sys.argv:
        print(format_report(analysis))
    else:
        # Default: JSON + report
        print(json.dumps(analysis, indent=2))
        print("\n" + "="*60 + "\n")
        print(format_report(analysis))

        # Save
        summary_dir = DATA_DIR / "summaries"
        summary_dir.mkdir(exist_ok=True)
        today = datetime.now(PACIFIC).strftime("%Y-%m-%d")
        with open(summary_dir / f"analysis_{today}.json", "w") as f:
            json.dump(analysis, f, indent=2)
        with open(summary_dir / f"report_{today}.md", "w") as f:
            f.write(format_report(analysis))
