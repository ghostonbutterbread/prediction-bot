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
from bot.status import prune_log_storage, summarize_log_storage
from bot.strategy_policy import strategy_policy_status

DATA_DIR = Path(__file__).parent.parent / "data"
PACIFIC = timezone(timedelta(hours=-7))
DEFAULT_SESSION_GLOBS = ("paper/sim_*.json", "live/sim_*.json", "sim_*.json")


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
    for trade in reversed(latest.get("trades", []) or []):
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


def analyze(*, prune_logs: bool = True) -> dict:
    """Run full analysis and return structured insights.

    Set prune_logs=False for read-only report contexts.
    """
    
    sessions = load_sessions()
    latest = sessions[-1] if sessions else {}
    trades = latest.get("trades", [])
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

    config = load_config(PROJECT_ROOT / "config.yaml")
    result["strategy_policy_status"] = _strategy_policy_status_from_latest(latest, config)
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


def format_report(analysis: dict) -> str:
    """Format analysis into a concise report."""
    s = analysis["summary"]
    p = analysis.get("performance", {})
    ep = analysis.get("event_performance", {})
    sq = analysis.get("signal_quality", {})
    
    lines = [
        f"📊 **Bot Report** — {analysis['timestamp'][:16]}",
        "",
        (
            f"Session: {s['current_session']} | Scans: {s['scans']} | Trades: {s['current_trades']} | "
            f"Resolved: {s.get('resolved', 0)} raw / {s.get('trusted_resolved_positions', 0)} trusted / "
            f"{s.get('resolved_events', 0)} events"
        ),
    ]
    if s.get("current_session_file"):
        lines.append(f"Source: {s['current_session_file']}")
    policy_status = analysis.get("strategy_policy_status") or {}
    if policy_status:
        features = policy_status.get("enabled_features") or {}
        enabled = ", ".join(sorted(name for name, enabled in features.items() if enabled)) or "none"
        lines.append(
            f"Strategy policy: {policy_status.get('version', 'stable')}/{policy_status.get('mode', 'off')} | "
            f"active={bool(policy_status.get('active'))} shadow={bool(policy_status.get('shadow'))} "
            f"enforce={bool(policy_status.get('enforce'))} | features={enabled}"
        )
    if s.get("ignored_invalid_trades"):
        lines.append(f"Ignored invalid trade rows: {s['ignored_invalid_trades']}")
    if s.get("invalid_resolved_positions"):
        lines.append(f"Untrusted resolved rows: {s['invalid_resolved_positions']}")
    if s.get("total_equity") is not None:
        lines.append(
            f"Capital: equity ${s.get('total_equity', 0):.2f} | "
            f"available ${s.get('available_cash', s.get('total_equity', 0)):.2f} | "
            f"reserved ${s.get('reserved_capital', 0):.2f}"
        )
    
    if p:
        emoji = "🟢" if p.get("total_pnl", 0) > 0 else "🔴" if p.get("total_pnl", 0) < 0 else "⚪"
        lines.append(
            f"{emoji} Position Win Rate: {p.get('win_rate', 0)}% | "
            f"P&L: ${p.get('total_pnl', 0):+.2f} | PF: {p.get('profit_factor', 0)}"
        )
    if ep:
        lines.append(
            f"📍 Event Win Rate: {ep.get('win_rate', 0)}% | "
            f"Events: {ep.get('resolved_events', 0)} | Avg/Event: ${ep.get('avg_pnl_per_event', 0):+.2f} | "
            f"Avg Pos/Event: {ep.get('avg_positions_per_resolved_event', 0)}"
        )
        if ep.get("retrade_count") is not None:
            lines.append(f"🔁 Retrades: {ep.get('retrade_count', 0)} | Open Events: {s.get('open_event_count', 0)}")
        top_events = (ep.get("open_event_concentration") or {}).get("top_concentrated_events") or []
        if top_events:
            top = top_events[0]
            lines.append(
                f"Top Open Event: {top.get('event_key')} | {top.get('open_positions')} positions | ${top.get('reserved_capital', 0):.2f} reserved"
            )
    
    if sq:
        lines.append(f"Edge: {sq.get('avg_edge', 0)}% avg, {sq.get('max_edge', 0)}% max | Conf: {sq.get('avg_confidence', 0)}%")
    
    storage = analysis.get("storage", {})
    if storage:
        lines.append(
            f"Storage: {storage.get('log_audit_usage_gb', 0)} GB / {storage.get('log_audit_cap_gb', 0)} GB ({storage.get('usage_pct', 0)}%)"
        )
        if storage.get("largest_files"):
            largest = storage["largest_files"][0]
            lines.append(
                f"Largest tracked log: {largest.get('path')} ({round(largest.get('bytes', 0) / (1024 ** 3), 2)} GB)"
            )
        prune_result = storage.get("prune_result")
        if prune_result and prune_result.get("performed"):
            lines.append(
                f"Auto-pruned: reclaimed {round(prune_result.get('bytes_reclaimed', 0) / (1024 ** 3), 2)} GB from {len(prune_result.get('pruned_files', []))} files"
            )

    if analysis["issues"]:
        lines.append("")
        lines.append("⚠️ **Issues:**")
        for i in analysis["issues"]:
            emoji = {"critical": "🔴", "error": "🟠", "warning": "🟡", "info": "🔵"}.get(i["severity"], "⚪")
            lines.append(f"  {emoji} {i['message']}")
    
    if analysis["actions"]:
        lines.append("")
        lines.append("🔧 **Actions:**")
        for a in analysis["actions"][:3]:
            lines.append(f"  [{a['priority']}] {a['action']} → {a.get('file', '?')}")
    
    return "\n".join(lines)


def load_sessions() -> list:
    sessions = []
    roots = []
    env_data_dir = os.getenv("ANALYZE_DATA_DIR")
    if env_data_dir:
        roots.append(Path(env_data_dir))
    roots.append(DATA_DIR)

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
                        trade_rows, ignored = _effective_trades(session.get("trades", []))
                        session["trades"] = trade_rows
                        session.setdefault("summary", {})
                        session["summary"]["ignored_invalid_trades"] = ignored
                        sessions.append(session)
                        seen_files.add(f)
                except Exception:
                    pass
    sessions.sort(key=_session_sort_key)
    return sessions


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
