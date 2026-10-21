"""Collector-only defaults: do not activate a paper/live strategy implicitly."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.prediction_lab_collect import PredictionLabCollectorDaemon


class CollectorWeatherDefaultsTests(unittest.TestCase):
    def _load(self, root, *, observer=True, selection=None, config_patch=None):
        config = {
            'runtime': {'storage_root': str(root)},
            'prediction_lab': {'observer_mode': observer, 'mode': 'collector'},
            'strategy': {},
        }
        if selection is not None:
            config['strategy']['weather_source_selection'] = copy.deepcopy(selection)
        daemon = PredictionLabCollectorDaemon(
            root / 'config.yaml', config_loader=lambda _: copy.deepcopy(config),
            config_patch=config_patch,
        )
        return daemon._load_config()

    def test_observer_collector_defaults_to_contextual_source_selection(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {}, clear=True):
            root = Path(tmp)
            config = self._load(root)
            selection = config['strategy'].get('weather_source_selection', {})
            self.assertIs(selection.get('enabled'), True)
            self.assertEqual(selection.get('min_sample_count'), 30)
            self.assertEqual(selection.get('max_history_events'), 100)
            self.assertEqual(selection.get('history_metadata_path'), str(
                root / 'data/derived_reports/auto_source_router_history/current/source_observations/run_metadata.json'))
            self.assertNotIn('trading_enabled', config)
            self.assertNotIn('paper_wallets', config)
            self.assertFalse((root / 'data').exists())

    def test_explicit_disable_and_history_settings_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {}, clear=True):
            root = Path(tmp)
            selection = {'enabled': False, 'history_metadata_path': '/explicit/history.json',
                         'min_sample_count': 45, 'max_history_events': 90}
            self.assertEqual(self._load(root, selection=selection)['strategy']['weather_source_selection'], selection)

    def test_nonobserver_does_not_change_source_selection_or_wallet_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._load(Path(tmp), observer=False)
            self.assertNotIn('weather_source_selection', config['strategy'])
            self.assertNotIn('paper_wallets', config)

    def test_environment_storage_root_controls_default_history_locator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict('os.environ', {'PREDICTION_BOT_COLLECTOR_ROOT': str(root / 'override')}):
                selection = self._load(root)['strategy'].get('weather_source_selection', {})
            self.assertEqual(selection.get('history_metadata_path'), str(
                root / 'override/data/derived_reports/auto_source_router_history/current/source_observations/run_metadata.json'))


if __name__ == '__main__':
    unittest.main()
