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

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.live import Live
from rich.layout import Layout
from rich.text import Text
from datetime import datetime, timezone
from typing import Optional

console = Console()


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
            resolved_recent: Optional list of recently resolved trades (max 10)

        Returns:
            Rendered string (for logging)
        """
        lines = []
        lines.append("")
        lines.append("╔" + "═" * 78 + "╗")

        # Header
        session_id = getattr(simulator, "session_id", "unknown")
        balance = getattr(simulator, "balance", 0.0)
        report = simulator.report()

        header = f"  PREDICTION BOT  |  Session: {session_id}  |  Scan #{scan_num}"
        lines.append("║" + header + " " * max(0, 78 - len(header)) + "║")
        lines.append("╠" + "═" * 78 + "╣")

        # Stats row
        pnl = report.get("pnl", 0.0)
        pnl_pct = report.get("pnl_pct", 0.0)
        win_rate = report.get("win_rate", 0.0)
        total_trades = report.get("total_trades", 0)
        starting_bal = report.get("starting_balance", 100.0)

        # Calculate resolved vs open
        resolved_trades = [t for t in simulator.trades if getattr(t, "resolved", False)]
        open_trades = [t for t in simulator.trades if not getattr(t, "resolved", False)]

        # Win rate from resolved
        if resolved_trades:
            wins = sum(1 for t in resolved_trades if (getattr(t, "pnl", 0) or 0) > 0)
            win_rate = wins / len(resolved_trades)
        else:
            win_rate = 0.0

        # Average entry price (from all trades)
        entries = [getattr(t, "market_price", 0) for t in simulator.trades]
        avg_entry = sum(entries) / len(entries) if entries else 0.0

        # Stats line
        pnl_str = f"${pnl:+.2f}"
        pnl_pct_str = f"({pnl_pct:+.1f}%)"
        wr_str = f"{win_rate:.0%}"

        stats = (
            f"  💰 ${balance:.2f}  |  P&L: {pnl_str} {pnl_pct_str}  |  "
            f"WR: {wr_str}  |  Entry ≤${simulator.max_entry_price:.2f}  |  "
            f"Trades: {total_trades} ({len(open_trades)} open / {len(resolved_trades)} resolved)"
        )
        lines.append("║" + stats + " " * max(0, 78 - len(stats)) + "║")

        # Divider
        lines.append("╠" + "═" * 78 + "╣")

        # ---- OPEN TRADES ----
        open_label = f"  📋 OPEN TRADES  [{len(open_trades)}]"
        lines.append("║" + open_label + " " * max(0, 78 - len(open_label)) + "║")

        if open_trades:
            # Table header
            header_row = "  %-28s %-9s %-7s %-7s %-7s %-8s" % (
                "QUESTION", "SIDE", "ENTRY", "EDGE", "CONF", "SIZE"
            )
            lines.append("║" + header_row + " " * max(0, 78 - len(header_row)) + "║")
            lines.append("║" + "─" * 78 + "║")

            for t in open_trades[-10:]:  # Max 10
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
            resolved_label = f"  ✅ RESOLVED TRADES (recent)  [{len(resolved_recent)}]"
        else:
            resolved_label = f"  ✅ RESOLVED TRADES  [{len(resolved_trades)}]"
        lines.append("║" + resolved_label + " " * max(0, 78 - len(resolved_label)) + "║")

        display_resolved = (resolved_recent if resolved_recent else resolved_trades)[-10:]

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
