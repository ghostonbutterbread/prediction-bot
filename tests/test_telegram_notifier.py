import unittest
from unittest.mock import patch

from bot.telegram_notifier import TelegramNotifier


class TelegramNotifierTests(unittest.TestCase):
    def test_disabled_notifier_does_not_send(self):
        notifier = TelegramNotifier({"telegram_enabled": False})
        self.assertFalse(notifier.send("hello"))

    @patch("bot.telegram_notifier.subprocess.run")
    def test_enabled_notifier_invokes_openclaw_message_send(self, mock_run):
        mock_run.return_value.returncode = 0
        notifier = TelegramNotifier(
            {
                "telegram_enabled": True,
                "telegram_target": "-1003763915138",
                "telegram_channel": "telegram",
                "telegram_thread_id": "8",
                "telegram_silent": True,
            }
        )
        ok = notifier.send("hello world")
        self.assertTrue(ok)
        cmd = mock_run.call_args.args[0]
        self.assertIn("openclaw", cmd)
        self.assertIn("message", cmd)
        self.assertIn("send", cmd)
        self.assertIn("--thread-id", cmd)
        self.assertIn("8", cmd)
        self.assertIn("--silent", cmd)


if __name__ == "__main__":
    unittest.main()
