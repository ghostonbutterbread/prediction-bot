#!/usr/bin/env python3
"""
Strategy Analyzer — Programmatic analysis of simulation data.
Outputs actionable insights as JSON so Ghost can make decisions without reading raw data.
"""

import json
import glob
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

DATA_DIR = Path(__file__).parent.parent / "data"
PACIFIC = timezone(timedelta(hours=-7))


def analyze() -> dict:
    """Run full analysis and return structured insights."""
    
    sessions = load_sessions()
    latest = sessions[-1] if sessions else {}
    trades = latest.get("trades", [])
    resolved = [t for t in trades if t.get("resolved")]
    
    result = {
        "timestamp": datetime.now(PACIFIC).isoformat(),
        "summary": {
            "total_sessions": len(sessions),
            "total_trades_ever": sum(len(s.get("trades", [])) for s in sessions),
            "current_session": latest.get("session_id", "?"),
            "current_trades": len(trades),
            "resolved": len(resolved),
            "scans": latest.get("scan_count", 0),
        },
        "performance": {},
        "signal_quality": {},
        "strategy_breakdown": {},
        "issues": [],
        "actions": [],
    }
    
    if resolved:
        wins = [t for t in resolved if (t.get("actual_pnl") or 0) > 0]
        losses = [t for t in resolved if (t.get("actual_pnl") or 0) < 0]
        total_pnl = sum(t.get("actual_pnl", 0) for t in resolved)
        
        result["performance"] = {
            "win_rate": round(len(wins) / len(resolved) * 100, 1),
            "total_pnl": round(total_pnl, 2),
            "wins": len(wins),
            "losses": len(losses),
            "avg_win": round(sum(t.get("actual_pnl", 0) for t in wins) / len(wins), 2) if wins else 0,
            "avg_loss": round(sum(t.get("actual_pnl", 0) for t in losses) / len(losses), 2) if losses else 0,
            "profit_factor": round(
                sum(t.get("actual_pnl", 0) for t in wins) / abs(sum(t.get("actual_pnl", 0) for t in losses))
                if losses and sum(t.get("actual_pnl", 0) for t in losses) != 0 else 0, 2
            ),
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
    issues = detect_issues(trades, resolved, latest)
    result["issues"] = issues
    
    # === ACTIONABLE RECOMMENDATIONS ===
    actions = generate_actions(issues, result)
    result["actions"] = actions
    
    return result


def detect_issues(trades: list, resolved: list, session: dict) -> list:
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
    resolved_trades = [t for t in trades if t.get("resolved")]
    if len(resolved_trades) >= 10:
        wins = sum(1 for t in resolved_trades if (t.get("actual_pnl") or 0) > 0)
        wr = wins / len(resolved_trades)
        if wr < 0.40:
            issues.append({
                "severity": "critical",
                "code": "LOW_WINRATE",
                "message": f"Win rate {wr:.0%} is below 40% — losing money",
                "suggestion": "Review strategy signals, consider disabling underperforming ones",
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
    sq = analysis.get("signal_quality", {})
    
    lines = [
        f"📊 **Bot Report** — {analysis['timestamp'][:16]}",
        "",
        f"Session: {s['current_session']} | Scans: {s['scans']} | Trades: {s['current_trades']}",
    ]
    
    if p:
        emoji = "🟢" if p.get("total_pnl", 0) > 0 else "🔴" if p.get("total_pnl", 0) < 0 else "⚪"
        lines.append(f"{emoji} Win Rate: {p.get('win_rate', 0)}% | P&L: ${p.get('total_pnl', 0):+.2f} | PF: {p.get('profit_factor', 0)}")
    
    if sq:
        lines.append(f"Edge: {sq.get('avg_edge', 0)}% avg, {sq.get('max_edge', 0)}% max | Conf: {sq.get('avg_confidence', 0)}%")
    
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
    for f in sorted(glob.glob(str(DATA_DIR / "sim_*.json"))):
        try:
            with open(f) as fp:
                sessions.append(json.load(fp))
        except:
            pass
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
