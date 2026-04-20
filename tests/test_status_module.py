import unittest

from bot.status import build_snapshot, format_status_message


class StatusModuleTests(unittest.TestCase):
    def test_format_status_message_renders_shared_fields(self):
        snapshot = build_snapshot(
            mode="🟡 PAPER",
            trading_enabled=True,
            tradable_cap="$10.00",
            max_position_size="$3.00",
            balance=12.34,
            available_cash="$8.00",
            reserved_capital="$4.34",
            exposure="$4.34 (35.2%)",
            pnl=2.34,
            pnl_pct=23.4,
            win_rate_pct=66.0,
            total_trades=3,
            open_trades=1,
            resolved_trades=2,
            scan_num=42,
            session_id="sess-123",
            extra={"source": "paper"},
        )

        msg = format_status_message(snapshot, reason="hourly cadence")

        self.assertIn("Bot status update (hourly cadence)", msg)
        self.assertIn("mode=🟡 PAPER", msg)
        self.assertIn("trading_enabled=True", msg)
        self.assertIn("tradable_cap=$10.00", msg)
        self.assertIn("max_position=$3.00", msg)
        self.assertIn("balance=$12.34", msg)
        self.assertIn("trades=3 (1 open / 2 resolved)", msg)
        self.assertIn("session=sess-123", msg)
        self.assertIn("source=paper", msg)


if __name__ == "__main__":
    unittest.main()
