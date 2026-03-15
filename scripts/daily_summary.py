#!/usr/bin/env python3
"""
Daily Performance Summary — runs at 6 AM via cron/heartbeat.
Reads simulation data and writes a concise summary for Ghost to review.
"""

import json
import os
import glob
from datetime import datetime, timezone, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
SUMMARY_DIR = Path(__file__).parent.parent / "data" / "summaries"
PACIFIC = timezone(timedelta(hours=-7))


def get_latest_session() -> dict:
    """Find the most recent simulation session."""
    files = sorted(glob.glob(str(DATA_DIR / "sim_*.json")))
    if not files:
        return {}
    
    with open(files[-1]) as f:
        return json.load(f)


def get_all_sessions() -> list[dict]:
    """Load all simulation sessions."""
    sessions = []
    for f in sorted(glob.glob(str(DATA_DIR / "sim_*.json"))):
        try:
            with open(f) as fp:
                sessions.append(json.load(fp))
        except:
            pass
    return sessions


def generate_summary() -> str:
    """Generate a concise daily summary."""
    now = datetime.now(PACIFIC)
    today = now.strftime("%Y-%m-%d")
    
    latest = get_latest_session()
    all_sessions = get_all_sessions()
    
    if not latest:
        return "❌ No simulation data found"
    
    trades = latest.get("trades", [])
    resolved = [t for t in trades if t.get("resolved")]
    unresolved = [t for t in trades if not t.get("resolved")]
    
    # Calculate stats
    wins = [t for t in resolved if (t.get("actual_pnl") or 0) > 0]
    losses = [t for t in resolved if (t.get("actual_pnl") or 0) < 0]
    total_pnl = sum(t.get("actual_pnl", 0) for t in resolved)
    
    # Direction breakdown
    by_direction = {}
    for t in trades:
        d = t.get("direction", "?")
        by_direction[d] = by_direction.get(d, 0) + 1
    
    # Edge stats
    edges = [t.get("edge", 0) for t in trades]
    avg_edge = sum(edges) / len(edges) if edges else 0
    max_edge = max(edges) if edges else 0
    
    # Confidence stats
    confs = [t.get("confidence", 0) for t in trades]
    avg_conf = sum(confs) / len(confs) if confs else 0
    
    # Exposure
    total_exposure = sum(t.get("position_size", 0) for t in trades)
    
    # Risk state
    risk_file = DATA_DIR / "risk_state.json"
    risk = {}
    if risk_file.exists():
        with open(risk_file) as f:
            risk = json.load(f)
    
    # Build summary
    lines = [
        f"# 📊 Daily Report — {today}",
        f"Generated: {now.strftime('%I:%M %p PST')}",
        "",
        f"## Session: {latest.get('session_id', '?')}",
        f"**Scans:** {latest.get('scan_count', 0)}",
        f"**Total Trades:** {len(trades)}",
        f"**Resolved:** {len(resolved)} | **Unresolved:** {len(unresolved)}",
        "",
    ]
    
    if resolved:
        win_rate = len(wins) / len(resolved) * 100
        lines.extend([
            "## Performance",
            f"**Win Rate:** {win_rate:.1f}% ({len(wins)}W / {len(losses)}L)",
            f"**Total P&L:** ${total_pnl:+.2f}",
            f"**Avg Edge:** {avg_edge*100:.2f}%",
            f"**Max Edge:** {max_edge*100:.2f}%",
            f"**Avg Confidence:** {avg_conf*100:.1f}%",
            "",
        ])
    else:
        lines.extend([
            "## Performance",
            "*No trades resolved yet — markets pending*",
            "",
        ])
    
    lines.extend([
        "## Trade Stats",
        f"**Directions:** {by_direction}",
        f"**Total Exposure:** ${total_exposure:.2f}",
        f"**Avg Position:** ${total_exposure/len(trades):.2f}" if trades else "",
        "",
    ])
    
    if risk:
        bal = risk.get("current_balance", 100)
        peak = risk.get("peak_balance", 100)
        dd = (peak - bal) / peak * 100 if peak > 0 else 0
        lines.extend([
            "## Risk Status",
            f"**Balance:** ${bal:.2f}",
            f"**Peak:** ${peak:.2f}",
            f"**Drawdown:** {dd:.1f}%",
            f"**Open Positions:** {risk.get('open_positions', 0)}",
            f"**Consecutive Losses:** {risk.get('consecutive_losses', 0)}",
            "",
        ])
    
    lines.extend([
        "## All Sessions",
        f"Total sessions: {len(all_sessions)}",
        f"Total trades (all): {sum(len(s.get('trades',[])) for s in all_sessions)}",
        "",
    ])
    
    # Top 5 trades by edge
    if trades:
        top = sorted(trades, key=lambda x: x.get("edge", 0), reverse=True)[:5]
        lines.extend([
            "## Top 5 Trades by Edge",
        ])
        for i, t in enumerate(top, 1):
            q = t.get("question", "?")[:50]
            lines.append(f"{i}. {t.get('direction','')} | {t.get('edge',0)*100:.1f}% edge | {t.get('confidence',0)*100:.0f}% conf | {q}")
        lines.append("")
    
    # Improvement suggestions
    lines.extend([
        "## 🔧 Suggested Improvements",
    ])
    
    if avg_edge < 0.03:
        lines.append("- Edge is low (avg < 3%) — consider raising MIN_EDGE or improving signal quality")
    if avg_conf < 0.55:
        lines.append("- Confidence is low (avg < 55%) — strategy may need refinement")
    if len(resolved) == 0 and len(trades) > 50:
        lines.append("- Many unresolved trades — consider focusing on shorter-term markets")
    if total_exposure > 200:
        lines.append("- High total exposure relative to $100 balance — risk of overexposure")
    
    if not any("—" in line for line in lines[-3:]):
        lines.append("- No major issues detected ✅")
    
    return "\n".join(lines)


def save_summary(summary: str):
    """Save summary to file."""
    SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(PACIFIC).strftime("%Y-%m-%d")
    path = SUMMARY_DIR / f"daily_{today}.md"
    
    with open(path, "w") as f:
        f.write(summary)
    
    print(f"Summary saved to {path}")
    return str(path)


if __name__ == "__main__":
    summary = generate_summary()
    print(summary)
    save_summary(summary)
