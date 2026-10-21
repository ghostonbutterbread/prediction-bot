"""Raw transport evidence is immutable and compact at the producer boundary."""
import hashlib
import json
import shutil
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from unittest.mock import patch
from types import SimpleNamespace
import httpx

from bot.file_ops import append_jsonl
from bot.prediction_lab import PredictionLab
from bot.weather.registry import WeatherRegistry
from tests.test_weather_broad_collection import gray_page

from bot.feeds import weather_pro


def _process_receipt(root, index):
    from bot.feeds.weather_capture_store import WeatherCaptureStore, CaptureRetentionError
    try:
        WeatherCaptureStore(storage_root=root, max_bytes=20000, min_free_bytes=0).retain_receipt(
            {'index': index, 'payload': 'x' * 1000})
        return True
    except CaptureRetentionError:
        return False


class CaptureStoreTests(unittest.TestCase):
    def test_process_writers_share_one_account_and_do_not_overspend(self):
        from bot.feeds.weather_capture_store import WeatherCaptureStore
        with tempfile.TemporaryDirectory() as root:
            with ProcessPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(_process_receipt, [root] * 12, range(12)))
            self.assertTrue(any(results))
            self.assertTrue(any(not value for value in results))
            store = WeatherCaptureStore(storage_root=root, max_bytes=20000, min_free_bytes=0)
            receipts = list((Path(root) / 'data/weather_captures/receipts').iterdir())
            self.assertEqual(len(receipts), sum(results))
            self.assertEqual(store.usage()['bytes'], sum(path.stat().st_size for path in receipts))

    def test_capped_store_counts_distinct_receipts_and_shared_blob(self):
        from bot.feeds.weather_capture_store import WeatherCaptureStore, CaptureRetentionError
        with tempfile.TemporaryDirectory() as root:
            store = WeatherCaptureStore(storage_root=root, max_bytes=30000, min_free_bytes=0)
            first = store.retain(b'a' * 100, metadata={'city': 'austin'})
            second = WeatherCaptureStore(storage_root=root, max_bytes=30000, min_free_bytes=0).retain(
                b'a' * 100, metadata={'city': 'vegas'})
            store.retain_receipt({'collection': 'attempt'})
            self.assertEqual(store.read_verified(first), b'a' * 100)
            self.assertEqual(store.read_verified(second), b'a' * 100)
            self.assertEqual(store.usage()['bytes'], 100 + sum(p.stat().st_size for p in
                (Path(root) / 'data/weather_captures/receipts').iterdir()))
            self.assertEqual(len(list((Path(root) / 'data/weather_captures/blobs').rglob('*.bin'))), 1)
            with self.assertRaisesRegex(CaptureRetentionError, 'max_bytes'):
                store.retain(b'b' * 29000, metadata={})

    def test_capped_concurrent_stores_serialize_and_failures_retry(self):
        from bot.feeds.weather_capture_store import WeatherCaptureStore, CaptureRetentionError
        with tempfile.TemporaryDirectory() as root:
            stores = [WeatherCaptureStore(storage_root=root, max_bytes=21000, min_free_bytes=0) for _ in range(8)]
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda item: self._try_receipt(*item), enumerate(stores)))
            self.assertTrue(any(results))
            self.assertEqual(stores[0].usage()['bytes'], sum(p.stat().st_size for p in
                (Path(root) / 'data/weather_captures/receipts').iterdir()))
            with patch.object(stores[0], '_write_object', side_effect=OSError('fixture')):
                with self.assertRaises(CaptureRetentionError):
                    stores[0].retain_receipt({'failed': True})
            stores[1].retain_receipt({'retry': True})

    @staticmethod
    def _try_receipt(index, store):
        try:
            store.retain_receipt({'index': index, 'payload': 'a' * 1000})
            return True
        except Exception as exc:
            from bot.feeds.weather_capture_store import CaptureRetentionError
            if not isinstance(exc, CaptureRetentionError):
                raise
            return False

    def test_existing_unaccounted_root_fails_closed(self):
        from bot.feeds.weather_capture_store import WeatherCaptureStore, CaptureRetentionError
        with tempfile.TemporaryDirectory() as root:
            WeatherCaptureStore(storage_root=root).retain(b'old', metadata={})
            capped = WeatherCaptureStore(storage_root=root, max_bytes=30000, min_free_bytes=0)
            with self.assertRaisesRegex(CaptureRetentionError, 'accounting'):
                capped.retain(b'new', metadata={})

    def test_reserve_and_actual_lab_configuration(self):
        from bot.feeds.weather_capture_store import CaptureRetentionError
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root:
            config = {'runtime': {'storage_root': root, 'base_dir': 'lab'},
                      'prediction_lab': {'mode': 'collector', 'observer_mode': True,
                                         'weather_capture_storage': {'max_bytes': 30000, 'min_free_bytes': 10}}}
            lab = PredictionLab(config)
            self.assertEqual(lab.capture_store.max_bytes, 30000)
            self.assertEqual(lab.capture_store.min_free_bytes, 10)
            with patch('bot.feeds.weather_capture_store.shutil.disk_usage', return_value=shutil._ntuple_diskusage(100000, 99995, 5)):
                with self.assertRaisesRegex(CaptureRetentionError, 'min_free_bytes'):
                    lab.capture_store.retain(b'new', metadata={})
            self.assertEqual(lab.capture_store.usage()['bytes'], 0)
            self.assertEqual(lab.capture_store.retain(b'new', metadata={})['payload_bytes'], 3)
            self.assertEqual(lab.capture_store.usage()['bytes'], lab.storage_usage()['weather_capture']['bytes'])

    def test_boundary_receipt_and_interrupted_publication_retry(self):
        from bot.feeds.weather_capture_store import WeatherCaptureStore, CaptureRetentionError
        with tempfile.TemporaryDirectory() as root:
            encoded = b'{"test":1}'
            store = WeatherCaptureStore(storage_root=root, max_bytes=16384 + 2 * len(encoded), min_free_bytes=0)
            store.retain_receipt({'test': 1})
            self.assertEqual(store.usage()['bytes'], len(encoded))
            with self.assertRaisesRegex(CaptureRetentionError, 'max_bytes'):
                store.retain_receipt({'test': 2})
            receipt_root = Path(root) / 'data/weather_captures/receipts'
            self.assertEqual(len(list(receipt_root.iterdir())), 1)
        with tempfile.TemporaryDirectory() as root:
            store = WeatherCaptureStore(storage_root=root, max_bytes=30000, min_free_bytes=0)
            with patch('bot.feeds.weather_capture_store.os.link', side_effect=OSError('fixture')):
                with self.assertRaises(CaptureRetentionError):
                    store.retain_receipt({'test': 1})
            self.assertEqual(store.usage()['bytes'], 0)
            self.assertEqual(list((Path(root) / 'data/weather_captures/receipts').iterdir()), [])
            store.retain_receipt({'test': 1})
            self.assertEqual(store.usage()['bytes'], len(encoded))

    def test_default_scope_validation_and_untrusted_accounting(self):
        from bot.feeds.weather_capture_store import WeatherCaptureStore, CaptureRetentionError
        with tempfile.TemporaryDirectory() as root:
            base = {'runtime': {'storage_root': root, 'base_dir': 'lab'}}
            limited = PredictionLab({**base, 'prediction_lab': {'mode': 'collector', 'observer_mode': True}})
            self.assertEqual((limited.capture_store.max_bytes, limited.capture_store.min_free_bytes),
                             (2147483648, 10737418240))
            legacy = PredictionLab({**base, 'prediction_lab': {'mode': 'seed_and_watch'}})
            self.assertIsNone(legacy.capture_store.max_bytes)
            self.assertIsNone(legacy.capture_store.min_free_bytes)
            with self.assertRaises(ValueError):
                PredictionLab({**base, 'prediction_lab': {'weather_capture_storage': {'max_bytes': True}}})
            with self.assertRaises(ValueError):
                PredictionLab({**base, 'prediction_lab': {'weather_capture_storage': {'min_free_bytes': -1}}})
            limited.capture_store.retain_receipt({'city': 'austin'})
            with self.assertRaisesRegex(CaptureRetentionError, 'configure limits'):
                legacy.capture_store.retain(b'uncapped writer', metadata={})
            (Path(root) / 'data/weather_captures/accounting.json').write_text('{}')
            with self.assertRaisesRegex(CaptureRetentionError, 'accounting untrusted'):
                limited.capture_store.retain_receipt({'city': 'vegas'})
    def test_verified_capture_binds_metadata_to_the_immutable_attempt_receipt(self):
        from bot.feeds.weather_capture_store import WeatherCaptureStore, CaptureRetentionError
        with tempfile.TemporaryDirectory() as root:
            store = WeatherCaptureStore(storage_root=root)
            ref = store.retain(b'original', metadata={'http_status': 200, 'retrieval_url':'https://weather.invalid/a'})
            with self.assertRaises(CaptureRetentionError):
                store.read_verified({**ref, 'http_status':503})

    def test_missing_corrupt_or_conflicting_evidence_never_hydrates_as_verified(self):
        from bot.feeds.weather_capture_store import WeatherCaptureStore, CaptureRetentionError
        with tempfile.TemporaryDirectory() as root:
            store = WeatherCaptureStore(storage_root=root)
            ref = store.retain(b'exact original', metadata={'http_status':200})
            blob = Path(root) / ref['raw_payload_locator']
            blob.chmod(0o644)
            blob.write_bytes(b'corrupted')
            with self.assertRaises(CaptureRetentionError): store.read_verified(ref)
            with self.assertRaises(CaptureRetentionError): store.retain(b'exact original', metadata={})
            self.assertEqual(blob.read_bytes(), b'corrupted', 'never silently overwrite a conflicting object')
            blob.unlink()
            with self.assertRaises(CaptureRetentionError): store.read_verified(ref)
            ref = store.retain(b'recovered original', metadata={})
            receipt = Path(root) / ref['capture_receipt_locator']
            receipt.chmod(0o644); receipt.write_bytes(b'{}')
            with self.assertRaises(CaptureRetentionError): store.read_verified(ref)

    def test_capture_store_does_not_write_through_an_escaping_storage_subdirectory(self):
        from bot.feeds.weather_capture_store import WeatherCaptureStore, CaptureRetentionError
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'collector'
            outside = Path(temporary) / 'foreign'
            (root / 'data').mkdir(parents=True)
            outside.mkdir()
            (root / 'data/weather_captures').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(CaptureRetentionError):
                WeatherCaptureStore(storage_root=root).retain(b'original', metadata={})
            self.assertEqual(list(outside.iterdir()), [], 'containment must be checked before writing any bytes')

    def test_failed_collection_receipt_publication_does_not_poison_forecast_cache(self):
        from bot.feeds.weather_capture_store import WeatherCaptureStore, CaptureRetentionError
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root:
            store = WeatherCaptureStore(storage_root=root)
            engine = weather_pro.ProWeatherEngine(capture_store=store)
            self.addCleanup(engine.close)
            with patch.object(store, 'retain_receipt', side_effect=CaptureRetentionError('disk unavailable')):
                with self.assertRaises(CaptureRetentionError):
                    engine.get_forecast('austin', require_target=True)
            self.assertEqual(engine._cache, {}, 'an incomplete receipt cannot be reused as a successful collection')
            recovered = engine.get_forecast('austin', require_target=True)
            self.assertIn('collection_receipt', recovered.details)
            store.read_receipt_verified(recovered.details['collection_receipt'])

    def test_retention_failure_cannot_be_reported_as_an_ordinary_provider_failure(self):
        from bot.feeds.weather_capture_store import WeatherCaptureStore, CaptureRetentionError
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root:
            store = WeatherCaptureStore(storage_root=root)
            engine = weather_pro.ProWeatherEngine(capture_store=store)
            self.addCleanup(engine.close)
            engine.open_meteo.http.close()
            engine.open_meteo.http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={'hourly':{}})))
            with patch.object(store, 'retain', side_effect=CaptureRetentionError('disk unavailable')):
                with self.assertRaises(CaptureRetentionError):
                    engine.get_forecast('austin', target_date='2026-10-19', measurement_kind='high', station_resolution={})

    def test_repeated_real_producer_lab_rows_hold_only_verified_capture_references(self):
        from bot.feeds.weather_capture_store import WeatherCaptureStore
        from datetime import datetime, timezone
        with tempfile.TemporaryDirectory() as root:
            store = WeatherCaptureStore(storage_root=root)
            engine = weather_pro.ProWeatherEngine(capture_store=store)
            self.addCleanup(engine.close)
            engine.open_meteo.get_forecast = lambda *a, **kw: None
            engine.nws.get_forecast = lambda *a, **kw: None
            engine.nws.get_station_observation = lambda *a: None
            engine.owm.available = False
            body = (gray_page() + '<!--' + 'original response bytes ' * 10000 + '-->').encode()
            engine.nws.http.close()
            engine.nws.http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=body)))
            lab = PredictionLab.__new__(PredictionLab)
            lab.config = {}; lab.collector_interval_seconds = 900; lab.observer_mode = True
            lab.mode = 'collector'; lab.paper_lab_mode = 'opportunity'; lab.hypothetical_mode = 'flat'
            lab.opportunity_bankroll_usd = 100; lab.fresh_wallet_bankroll_usd = 100
            market = SimpleNamespace(id='TEST-CAPTURE', question='Will Las Vegas high temperature be above 85° on Oct 19, 2026?', metadata={'market_group':'weather'})
            output = Path(root) / 'rows.jsonl'
            for index in range(2):
                # A second actual HTTP response, not merely a second serialization.
                engine._cache.clear()
                weather = engine.score_temperature_market_with_context(market.question, .5)
                self.assertIsNotNone(weather)
                signal = {'direction':'SKIP', 'signal_details':{'live':weather}, 'question':market.question, 'market_id':market.id}
                artifact = {'strategy_signal':signal, 'final_action':'SKIP'}
                lab._attach_weather_source_snapshot(artifact, market)
                row = lab._build_market_snapshot_row(str(index), market, signal, decision_type='skip', prediction_recorded=False,
                    decision_artifact=artifact, observed_at=datetime.now(timezone.utc).isoformat())
                append_jsonl(output, row)
            rows = [json.loads(line) for line in output.read_text().splitlines()]
            refs = []
            def check(value):
                if isinstance(value, dict):
                    self.assertFalse({'payload_text', 'payload_base64'} & value.keys())
                    if 'raw_payload_locator' in value:
                        self.assertEqual(store.read_verified(value), body)
                        refs.append(value)
                    for child in value.values(): check(child)
                elif isinstance(value, list):
                    for child in value: check(child)
            for row in rows: check(row)
            self.assertEqual(len(rows), 2)
            self.assertEqual(len(refs), 10)  # Five existing artifact wrappers, references only.
            self.assertEqual(len({ref['raw_payload_locator'] for ref in refs}), 1)
            self.assertEqual(len({ref['capture_receipt_locator'] for ref in refs}), 2)
            self.assertEqual(len(list((Path(root) / 'data/weather_captures/blobs').rglob('*.bin'))), 1)

    def test_exact_binary_bytes_retained_once_with_distinct_attempt_receipts(self):
        from bot.feeds.weather_capture_store import WeatherCaptureStore
        with tempfile.TemporaryDirectory() as root:
            store = WeatherCaptureStore(storage_root=root)
            raw = b'\xff\x00weather\r\n'
            first = store.retain(raw, metadata={'retrieval_url': 'https://weather.invalid', 'http_status': 200})
            second = store.retain(raw, metadata={'retrieval_url': 'https://weather.invalid', 'http_status': 503})
            self.assertEqual(first['payload_sha256'], hashlib.sha256(raw).hexdigest())
            self.assertEqual(first['raw_payload_locator'], second['raw_payload_locator'])
            self.assertNotEqual(first['capture_receipt_locator'], second['capture_receipt_locator'])
            self.assertFalse(Path(first['raw_payload_locator']).is_absolute())
            self.assertEqual(store.read_verified(first), raw)
            self.assertEqual(store.read_verified(second), raw)
            self.assertEqual(list((Path(root) / 'data/weather_captures/blobs').rglob('*.bin')),
                             [Path(root) / first['raw_payload_locator']])
            self.assertNotIn('payload_text', json.dumps(first))
            self.assertNotIn('payload_base64', json.dumps(first))
