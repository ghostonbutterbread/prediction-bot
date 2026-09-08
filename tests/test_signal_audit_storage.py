"""The real strategy audit writer must honor the selected data root."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot.config import load_config
from bot.strategies.enhanced import EnhancedStrategyEngine, strategy_config_with_policy
from bot.strategies.signal_validator import SignalAuditLog, ValidationResult



class SignalAuditStorageTests(unittest.TestCase):
    def test_enabled_strategy_audit_persists_outside_both_checkouts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            storage = root / "storage"
            config = root / "config.json"
            config.write_text(json.dumps({"runtime": {"storage_root": str(storage), "base_dir": "data/cohort"}, "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False}}))
            original = Path.cwd()
            self.addCleanup(os.chdir, original)
            with patch.dict(os.environ, {"SIGNAL_AUDIT_ENABLED": "true"}, clear=True):
                for name in ("code-a", "code-b"):
                    cwd = root / name
                    cwd.mkdir()
                    os.chdir(cwd)
                    engine = EnhancedStrategyEngine(strategy_config_with_policy(load_config(config)))
                    expected = storage / "data/cohort/paper/signal_audit.jsonl"
                    self.assertEqual(engine.signal_audit.path, expected)
                    engine.signal_audit.write(SimpleNamespace(id=name, yes_price=0.5), "fixture", {}, {}, ValidationResult(True, 0.8, 0.7))
                    self.assertFalse((cwd / "data").exists())
            rows = [json.loads(line) for line in expected.read_text().splitlines()]
            self.assertEqual([row["market_id"] for row in rows], ["code-a", "code-b"])

    def test_disabled_audit_constructor_has_no_directory_side_effect(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            path = Path(temp) / "absent" / "signal.jsonl"
            audit = SignalAuditLog(str(path))
            self.assertFalse(audit.enabled)
            self.assertFalse(path.parent.exists())
