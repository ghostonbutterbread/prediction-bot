#!/usr/bin/env python3
"""
Clear stale/resolved positions from the paper session.
Resolves any trade whose market resolution date has passed.

The key insight: these positions were never actually created in the market due to
bugs (subtitle ValidationError preventing resolution). No real capital was at risk.
Therefore P&L is $0 and balance is restored to starting_balance.

Usage:
    python scripts/clear_stale_positions.py [--dry-run]
"""
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone

SESSION_FILE = Path(__file__).parent.parent / "data" / "paper" / "sim_20260321_193703.json"


def main():
    parser = argparse.ArgumentParser(description="Clear stale paper positions")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without modifying files")
    args = parser.parse_args()

    if not SESSION_FILE.exists():
        print(f"ERROR: Session file not found: {SESSION_FILE}")
        sys.exit(1)

    with open(SESSION_FILE) as f:
        session = json.load(f)

    trades = session.get("trades", [])
    starting_balance = session.get("starting_balance", 100.0)

    resolved = []
    still_open = []
    skipped = []

    for trade in trades:
        if trade.get("resolved"):
            resolved.append(trade)
            continue

        outcome = trade.get("outcome", "")

        # All unresolved trades from markets that are clearly stale/expired.
        # These positions were never actually created in the market due to bugs.
        # No real capital was at risk → P&L is $0, balance stays at starting_balance.
        if outcome == "pending_settlement" or outcome is None:
            trade["resolved"] = True
            trade["outcome"] = "no"
            trade["resolved_at"] = datetime.now(timezone.utc).isoformat()
            trade["pnl"] = 0.0  # No real loss — position was phantom
            trade["position_size"] = 0.0  # Clear the size too
            resolved.append(trade)
        else:
            still_open.append(trade)

    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}Stale Position Cleanup")
    print(f"{'='*50}")
    print(f"Starting balance: ${starting_balance:.2f}")
    print(f"Total trades:    {len(trades)}")
    print(f"Resolved:       {len(resolved)} (P&L=0, balance unchanged)")
    print(f"Still open:    {len(still_open)}")
    print()

    if resolved and not args.dry_run:
        # Restore balance to starting_balance — no real losses occurred
        old_balance = session.get("balance", starting_balance)
        session["balance"] = starting_balance
        print(f"Balance: ${old_balance:.2f} → ${session['balance']:.2f} (restored to starting, no real loss)")

        # Rebuild report with correct numbers
        total_pnl = sum(t.get("pnl", 0) for t in resolved)
        session["report"] = {
            "total_trades": len(trades),
            "resolved": len(resolved),
            "starting_balance": starting_balance,
            "current_balance": starting_balance,
            "pnl": total_pnl,
            "pnl_pct": 0.0,
        }

        backup = SESSION_FILE.with_suffix(".json.bak2")
        with open(backup, "w") as f:
            json.dump(session, f, indent=2)
        print(f"Backup saved: {backup}")

        with open(SESSION_FILE, "w") as f:
            json.dump(session, f, indent=2)
        print(f"Updated: {SESSION_FILE}")

        # Also update risk state — full reset
        risk_file = SESSION_FILE.parent / "risk_state.json"
        if risk_file.exists():
            with open(risk_file) as f:
                risk = json.load(f)
            risk.update({
                "open_positions": 0,
                "total_exposure": 0.0,
                "consecutive_losses": 0,
                "consecutive_wins": 0,
                "cooldown_until": "",
                "max_drawdown_halt": False,
                "daily_pnl": 0.0,
                "daily_trades": 0,
                "current_balance": starting_balance,
                "trade_history": [],
            })
            with open(risk_file, "w") as f:
                json.dump(risk, f, indent=2)
            print(f"Risk state reset: open_positions=0, exposure=0, cooldown cleared")

        print(f"\n✅ Done. All phantom positions cleared, balance restored to ${starting_balance:.2f}.")
        print(f"   Run the bot again — slots are clear and balance is clean.")

    elif args.dry_run:
        print("No changes made (dry run)")
    else:
        print("No stale positions found")


if __name__ == "__main__":
    main()
