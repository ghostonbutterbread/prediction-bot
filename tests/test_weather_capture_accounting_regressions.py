"""Regression coverage for fixed-width capture accounting and terminal failures."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bot.feeds.weather_capture_store import CaptureRetentionError, WeatherCaptureStore, _ACCOUNT_OVERHEAD
from scripts import prediction_lab_collect as collector_cli


class CaptureAccountingRegressionTests(unittest.TestCase):
    def test_all_256_content_shards_fit_pending_and_committed_accounting(self):
        bodies = {}
        for index in range(20000):
            body = f'bounded full-shard fixture {index}'.encode()
            bodies.setdefault(hashlib.sha256(body).hexdigest()[:2], body)
            if len(bodies) == 256:
                break
        self.assertEqual(len(bodies), 256)
        with tempfile.TemporaryDirectory() as root:
            store = WeatherCaptureStore(storage_root=root, max_bytes=2 * 1024 * 1024, min_free_bytes=0)
            references = [store.retain(body, metadata={'fixture': 'full-shards'}) for body in bodies.values()]
            state_path = Path(root) / 'data/weather_captures/accounting.json'
            state = json.loads(state_path.read_bytes())
            self.assertEqual(len(state['dirs']), 258)
            self.assertIsNone(state['pending'])
            self.assertLessEqual(state_path.stat().st_size * 2, _ACCOUNT_OVERHEAD)
            total = sum(p.stat().st_size for name in ('blobs', 'receipts')
                        for p in (store.capture_root / name).rglob('*') if p.is_file())
            self.assertEqual(store.usage()['bytes'], total)
            for reference in references:
                self.assertEqual(hashlib.sha256(store.read_verified(reference)).hexdigest(), reference['payload_sha256'])

    def test_failed_pending_publication_does_not_create_unaccounted_shard(self):
        with tempfile.TemporaryDirectory() as root:
            store = WeatherCaptureStore(storage_root=root, max_bytes=2 * 1024 * 1024, min_free_bytes=0)
            store.retain(b'first fixture body', metadata={})
            original = (store.capture_root / 'accounting.json').read_bytes()
            state = json.loads(original)
            new_body = next(f'new body {i}'.encode() for i in range(100)
                            if f"blobs/{hashlib.sha256(f'new body {i}'.encode()).hexdigest()[:2]}" not in state['dirs'])
            with patch.object(store, '_save_state', side_effect=CaptureRetentionError('fixture pending publication failed')):
                with self.assertRaisesRegex(CaptureRetentionError, 'fixture pending publication failed'):
                    store.retain(new_body, metadata={})
            self.assertEqual((store.capture_root / 'accounting.json').read_bytes(), original)
            self.assertEqual(store._directories(), state['dirs'])
            self.assertEqual(store.usage()['bytes'], state['bytes'])

    def test_cli_retention_error_is_terminal_and_actionable(self):
        with patch.object(collector_cli, 'load_dotenv'), \
             patch.object(collector_cli, 'PredictionLabCollectorDaemon') as daemon, \
             patch('sys.argv', ['prediction_lab_collect.py', '--observer']), \
             self.assertLogs(level='ERROR') as logs:
            daemon.return_value.run.side_effect = CaptureRetentionError('fixture accounting untrusted')
            self.assertEqual(collector_cli.main(), 78)
        self.assertIn('fixture accounting untrusted', '\n'.join(logs.output))
        self.assertIn('pause', '\n'.join(logs.output).lower())
