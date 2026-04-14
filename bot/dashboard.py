"""
Live Trading Dashboard - Rich terminal UI for paper_loop.

Shows:
- Session header (ID, scan count)
- Balance + P&L + Win Rate
- Open trades with unrealized P&L
- Resolved trades with outcomes

Usage:
    from bot.dashboard import LiveDashboard
    dashboard = LiveDashboard()
    dashboard.render(simulator, scan_num=5)
"""

from collections import deque

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.live import Live
from rich.layout import Layout
from rich.text import Text
from datetime import datetime, timezone
from typing import Optional

console = Console()
DISPLAY_TRADE_CAP = 200
DISPLAY_ROW_LIMIT = 10


def _bounded_trade_windows(trades, limit: int = DISPLAY_TRADE_CAP):
    """Count all trades while retaining only bounded display windows."""
    open_trades = deque(maxlen=limit)
    resolved_trades = deque(maxlen=limit)
    open_count = 0
    resolved_count = 0
    wins = 0

    for trade in trades:
        if getattr(trade, "resolved", False):
            resolved_count += 1
            resolved_trades.append(trade)
            if (getattr(trade, "pnl", 0) or 0) > 0:
                wins += 1
        else:
            open_count += 1
            open_trades.append(trade)

    return open_count, resolved_count, wins, open_trades, resolved_trades


def _cap_recent_trades(trades, limit: int = DISPLAY_TRADE_CAP) -> list:
    """Clamp any externally supplied trade list to the most recent entries."""
    return list(deque(trades, maxlen=limit))


def _display_rows(trades, row_limit: int = DISPLAY_ROW_LIMIT) -> list:
    """Render only the tail rows from an already bounded trade window."""
    return list(trades)[-row_limit:]


class LiveDashboard:
    """
    Renders a live-updating terminal dashboard for the prediction bot.
    Designed to be called after each scan in paper_loop.
    """

    def __init__(self):
        self.console = Console()

    def render(
        self,
        simulator,
        scan_num: int = 0,
        resolved_recent: list = None,
    ) -> str:
        """
        Render the full dashboard and return the string.

        Args:
            simulator: Simulator instance with .trades, .balance, .report()
            scan_num: Current scan number
            resolved_recent: Optional list of recently resolved trades (display cap 200)

        Returns:
            Rendered string (for logging)
        """
        lines = []
        lines.append("")
        lines.append("╔" + "═" * 78 + "╗")

        # Header
        session_id = getattr(simulator, "session_id", "unknown")
        balance = getattr(simulator, "balance", 0.0)
        starting_bal = getattr(simulator, "starting_balance", 100.0)
        pnl = balance - starting_bal
        pnl_pct = (pnl / starting_bal * 100) if starting_bal else 0.0
        (
            open_count,
            resolved_count,
            wins,
            open_trades,
            resolved_trades,
        ) = _bounded_trade_windows(getattr(simulator, "trades", []))
        total_trades = open_count + resolved_count
        win_rate = (wins / resolved_count) if resolved_count else 0.0

        header = f"  PREDICTION BOT  |  Session: {session_id}  |  Scan #{scan_num}"
        lines.append("║" + header + " " * max(0, 78 - len(header)) + "║")
        lines.append("╠" + "═" * 78 + "╣")

        # Stats row
        pnl_str = f"${pnl:+.2f}"
        pnl_pct_str = f"({pnl_pct:+.1f}%)"
        wr_str = f"{win_rate:.0%}"

        stats = (
            f"  💰 ${balance:.2f}  |  P&L: {pnl_str} {pnl_pct_str}  |  "
            f"WR: {wr_str}  |  Entry ≤${simulator.max_entry_price:.2f}  |  "
            f"Trades: {total_trades} ({open_count} open / {resolved_count} resolved)"
        )
        lines.append("║" + stats + " " * max(0, 78 - len(stats)) + "║")

        # Divider
        lines.append("╠" + "═" * 78 + "╣")

        # ---- OPEN TRADES ----
        open_label = f"  📋 OPEN TRADES  [{open_count}]"
        lines.append("║" + open_label + " " * max(0, 78 - len(open_label)) + "║")

        if open_trades:
            # Table header
            header_row = "  %-28s %-9s %-7s %-7s %-7s %-8s" % (
                "QUESTION", "SIDE", "ENTRY", "EDGE", "CONF", "SIZE"
            )
            lines.append("║" + header_row + " " * max(0, 78 - len(header_row)) + "║")
            lines.append("║" + "─" * 78 + "║")

            for t in _display_rows(open_trades):
                question = (getattr(t, "question", "") or "")[:28]
                direction = getattr(t, "direction", "UNKNOWN")
                entry = getattr(t, "market_price", 0)
                edge = getattr(t, "edge", 0)
                confidence = getattr(t, "confidence", 0)
                size = getattr(t, "position_size", 0)

                row = "  %-28s %-9s $%-6.2f %-6.1f%% %-6.1f%% $%-6.2f" % (
                    question, direction, entry, edge * 100, confidence * 100, size
                )
                lines.append("║" + row + " " * max(0, 78 - len(row)) + "║")
        else:
            lines.append("║" + "  (none)" + " " * 73 + "║")

        # Divider
        lines.append("╠" + "═" * 78 + "╣")

        # ---- RESOLVED TRADES ----
        if resolved_recent:
            recent_resolved = _cap_recent_trades(resolved_recent)
            resolved_label = f"  ✅ RESOLVED TRADES (recent)  [{len(recent_resolved)}]"
        else:
            recent_resolved = resolved_trades
            resolved_label = f"  ✅ RESOLVED TRADES  [{resolved_count}]"
        lines.append("║" + resolved_label + " " * max(0, 78 - len(resolved_label)) + "║")

        display_resolved = _display_rows(recent_resolved)

        if display_resolved:
            # Table header
            header_row = "  %-26s %-8s %-10s %-12s %-8s %-10s" % (
                "QUESTION", "SIDE", "ENTRY", "OUTCOME", "P&L", "STATUS"
            )
            lines.append("║" + header_row + " " * max(0, 78 - len(header_row)) + "║")
            lines.append("║" + "─" * 78 + "║")

            for t in display_resolved:
                question = (getattr(t, "question", "") or "")[:26]
                direction = getattr(t, "direction", "?")
                entry = getattr(t, "market_price", 0)
                outcome = getattr(t, "outcome", "?")
                pnl_t = getattr(t, "pnl", 0) or 0
                res_type = getattr(t, "resolution_type", "settled")

                # Truncate resolution type
                res_type = res_type[:8]

                pnl_str_t = f"${pnl_t:+.2f}"
                if pnl_t > 0:
                    pnl_str_t = f"+${pnl_t:.2f}"
                elif pnl_t < 0:
                    pnl_str_t = f"-${abs(pnl_t):.2f}"
                else:
                    pnl_str_t = " $0.00"

                row = "  %-26s %-8s $%-7.2f %-12s %-9s %-10s" % (
                    question, direction, entry, outcome, pnl_str_t, res_type
                )
                lines.append("║" + row + " " * max(0, 78 - len(row)) + "║")
        else:
            lines.append("║" + "  (none)" + " " * 73 + "║")

        lines.append("╚" + "═" * 78 + "╝")
        lines.append("")

        output = "\n".join(lines)
        return output

    def print(self, simulator, scan_num: int = 0, resolved_recent: list = None):
        """Render and print to console."""
        output = self.render(simulator, scan_num, resolved_recent)
        self.console.print(output)


def render_simple(
    simulator,
    scan_num: int = 0,
    resolved_recent: list = None,
) -> str:
    """
    Standalone function - returns the dashboard string.
    Can be used by paper_loop to log + display.
    """
    dash = LiveDashboard()
    return dash.render(simulator, scan_num, resolved_recent)
