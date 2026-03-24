#!/usr/bin/env python3
"""
Send Telegram alerts to Ryushe.
Called by paper_loop.py when events need human attention.

Usage:
    python send_alert.py --message "Alert text"
"""
import subprocess
import sys


def send_alert(message: str) -> bool:
    """Send alert to Ryushe via Telegram using openclaw CLI."""
    cmd = [
        "openclaw", "message", "send",
        "--channel", "telegram",
        "--target", "7104548956",  # Ryushe's Telegram chat ID
        "--message", message,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            print(f"✅ Alert sent")
            return True
        else:
            print(f"❌ Failed: {result.stderr}", file=sys.stderr)
            return False
    except Exception as e:
        print(f"❌ Error: {e}", file=sys.stderr)
        return False


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Send Telegram alert to Ryushe")
    parser.add_argument("--message", "-m", required=True, help="Alert message")
    args = parser.parse_args()

    success = send_alert(args.message)
    sys.exit(0 if success else 1)
