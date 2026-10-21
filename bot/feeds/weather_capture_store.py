"""Collector-owned exact weather bytes; downstream artifacts carry references only.

A capped store requires an empty capture directory or an intact accounting sidecar.
Legacy uncapped stores retain their original behavior; evidence is never pruned.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid


class CaptureRetentionError(RuntimeError):
    """Evidence could not be durably retained or its identity did not verify."""


# Upper bound for the small accounting record, lock, and atomic replacement temps.
# Publication reserves a full second copy of a new body until its temp is removed.
_ACCOUNT_OVERHEAD = 16384


class WeatherCaptureStore:
    @staticmethod
    def _file_digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()

    def __init__(self, *, storage_root: str | Path, max_bytes: int | None = None,
                 min_free_bytes: int | None = None):
        root = Path(storage_root).expanduser()
        if not root.is_absolute():
            raise ValueError('weather capture storage root must be absolute')
        for name, value in (('max_bytes', max_bytes), ('min_free_bytes', min_free_bytes)):
            if value is not None and (type(value) is not int or value < 0 or (name == 'max_bytes' and value == 0)):
                raise ValueError(f'weather_capture_storage.{name} must be a positive integer' if name == 'max_bytes'
                                 else f'weather_capture_storage.{name} must be a nonnegative integer')
        self.storage_root = root.resolve()
        self.max_bytes = max_bytes
        self.min_free_bytes = min_free_bytes
        self.capture_root = self.storage_root / 'data/weather_captures'

    def _write_object(self, relative: str, raw: bytes, *, temporary_name: str | None = None) -> None:
        """Publish complete read-only bytes without replacing an existing object."""
        target = self.storage_root / relative
        temporary = None
        try:
            if not target.parent.resolve().is_relative_to(self.storage_root):
                raise CaptureRetentionError('weather capture directory escapes storage root')
            target.parent.mkdir(parents=True, exist_ok=True)
            if temporary_name is None:
                fd, temporary = tempfile.mkstemp(prefix='.capture-', dir=target.parent)
            else:
                temporary = str(target.parent / temporary_name)
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), 0o444)
            try:
                os.link(temporary, target)
            except FileExistsError:
                if target.is_symlink() or target.read_bytes() != raw:
                    raise CaptureRetentionError('existing weather capture has conflicting bytes')
            directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            raise CaptureRetentionError('weather capture persistence failed; retry after checking storage') from exc
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def _directories(self) -> dict[str, int]:
        """Constant-width directory identity check (256 possible content shards)."""
        result = {}
        # The root's mtime changes on every atomic accounting replacement.
        for name in ('receipts', 'blobs'):
            path = self.capture_root / name
            if path.exists():
                if path.is_symlink() or not path.is_dir():
                    raise CaptureRetentionError('weather capture accounting directory changed; inspect offline')
                result[name] = path.stat().st_mtime_ns
        blobs = self.capture_root / 'blobs'
        if blobs.exists():
            for shard in range(256):
                name = f'blobs/{shard:02x}'
                path = self.capture_root / name
                if path.exists():
                    if path.is_symlink() or not path.is_dir():
                        raise CaptureRetentionError('weather capture accounting shard changed; inspect offline')
                    result[name] = path.stat().st_mtime_ns
        return result

    def _save_state(self, state: dict) -> None:
        state['dirs'] = self._directories()
        unsigned = {key: value for key, value in state.items() if key != 'checksum'}
        state['checksum'] = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        payload = json.dumps(state, sort_keys=True, separators=(',', ':')).encode()
        if len(payload) > _ACCOUNT_OVERHEAD // 2:
            raise CaptureRetentionError('weather capture accounting too large; inspect offline')
        path = self.capture_root / 'accounting.json'
        fd, temporary = tempfile.mkstemp(prefix='.accounting-', dir=self.capture_root)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(self.capture_root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _state(self, *, initialize: bool = True) -> dict:
        path = self.capture_root / 'accounting.json'
        if not path.exists():
            # Never infer a preexisting corpus's size by a partial or repeated scan.
            for name in ('blobs', 'receipts'):
                directory = self.capture_root / name
                if directory.exists() and (directory.is_symlink() or next(directory.iterdir(), None) is not None):
                    raise CaptureRetentionError('weather capture accounting missing for existing evidence; pause and inventory offline')
            state = {'version': 1, 'bytes': 0, 'pending': None}
            if initialize:
                self._save_state(state)
            return state
        try:
            if path.is_symlink() or path.stat().st_size > _ACCOUNT_OVERHEAD // 2:
                raise ValueError('accounting file changed or too large')
            state = json.loads(path.read_bytes())
            checksum = state.get('checksum')
            unsigned = {key: value for key, value in state.items() if key != 'checksum'}
            if checksum != hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(',', ':')).encode()).hexdigest():
                raise ValueError('accounting checksum mismatch')
            if (state.get('version') != 1 or type(state.get('bytes')) is not int
                    or state['bytes'] < 0 or not isinstance(state.get('dirs'), dict)):
                raise ValueError('invalid accounting')
            if state.get('pending'):
                pending = state['pending']
                locator = pending['locator']
                target = self.storage_root / locator
                temporary = target.parent / pending['temporary']
                if not target.is_relative_to(self.capture_root) or not temporary.is_relative_to(self.capture_root):
                    raise ValueError('invalid accounting locator')
                if temporary.exists():
                    if temporary.is_symlink() or not temporary.is_file():
                        raise ValueError('invalid accounting temporary')
                    temporary.unlink()  # only the uniquely named unpublished temp, never evidence
                if target.exists():
                    if target.is_symlink() or target.stat().st_size != pending['size']:
                        raise ValueError('pending evidence size changed')
                    # Read and verify only this one interrupted publication.
                    if self._file_digest(target) != pending['sha256']:
                        raise ValueError('pending evidence digest changed')
                    state['bytes'] += pending['size']
                state['pending'] = None
                self._save_state(state)
            elif state['dirs'] != self._directories():
                raise ValueError('capture directory changed outside accounting lock')
            return state
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise CaptureRetentionError('weather capture accounting untrusted; pause collection and reconcile offline') from exc

    def _locked_state(self):
        self.capture_root.mkdir(parents=True, exist_ok=True)
        if self.capture_root.is_symlink() or not self.capture_root.resolve().is_relative_to(self.storage_root):
            raise CaptureRetentionError('weather capture directory escapes storage root')
        return (self.capture_root / 'accounting.lock').open('a+b')

    def _publish(self, relative: str, raw: bytes) -> None:
        if self.max_bytes is None and self.min_free_bytes is None:
            if (self.capture_root / 'accounting.json').exists():
                raise CaptureRetentionError('weather capture accounting active; configure limits for every writer')
            self._write_object(relative, raw)
            return
        try:
            with self._locked_state() as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                state = self._state()
                target = self.storage_root / relative
                if not target.is_relative_to(self.capture_root) or target.is_symlink():
                    raise CaptureRetentionError('weather capture locator escapes capture root')
                # A dedup still publishes a distinct attempt receipt later; verify its bytes.
                if target.exists():
                    if target.read_bytes() != raw:
                        raise CaptureRetentionError('existing weather capture has conflicting bytes')
                    return
                # Verify the shard/receipt parent is not a symlink before creating anything.
                if not target.parent.resolve().is_relative_to(self.capture_root):
                    raise CaptureRetentionError('weather capture directory escapes capture root')
                extra = len(raw) * 2 + _ACCOUNT_OVERHEAD
                if self.max_bytes is not None and state['bytes'] + extra > self.max_bytes:
                    raise CaptureRetentionError('weather capture max_bytes exhausted; pause collection or increase validated budget')
                if self.min_free_bytes is not None and shutil.disk_usage(self.capture_root).free < self.min_free_bytes + extra:
                    raise CaptureRetentionError('weather capture min_free_bytes reserve reached; pause collection or free storage')
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary_name = '.capture-' + uuid.uuid4().hex
                state['pending'] = {'locator': relative, 'size': len(raw),
                                    'sha256': hashlib.sha256(raw).hexdigest(), 'temporary': temporary_name}
                self._save_state(state)
                try:
                    self._write_object(relative, raw, temporary_name=temporary_name)
                except Exception:
                    # Recover only this one reserved publication, including a linked object.
                    self._state()
                    raise
                self._state()
                if self.min_free_bytes is not None and shutil.disk_usage(self.capture_root).free < self.min_free_bytes:
                    raise CaptureRetentionError('weather capture min_free_bytes reserve reached after publication; pause collection')
        except OSError as exc:
            raise CaptureRetentionError('weather capture accounting unavailable; pause collection and inspect storage') from exc

    def usage(self) -> dict:
        """Bounded diagnostic; no archive traversal. Raises if accounting is untrusted."""
        if self.max_bytes is None and self.min_free_bytes is None:
            return {'bytes': None, 'max_bytes': None, 'min_free_bytes': None, 'free_bytes': None}
        try:
            if not self.capture_root.exists():
                free = shutil.disk_usage(self.storage_root).free
                return {'bytes': 0, 'max_bytes': self.max_bytes,
                        'min_free_bytes': self.min_free_bytes, 'free_bytes': free}
            with self._locked_state() as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                state = self._state(initialize=False)
                free = shutil.disk_usage(self.capture_root).free
                return {'bytes': state['bytes'], 'max_bytes': self.max_bytes,
                        'min_free_bytes': self.min_free_bytes, 'free_bytes': free}
        except OSError as exc:
            raise CaptureRetentionError('weather capture accounting unavailable; pause collection and inspect storage') from exc

    def retain(self, raw: bytes, *, metadata: dict) -> dict:
        if not isinstance(raw, bytes):
            raise TypeError('weather captures require exact bytes')
        digest = hashlib.sha256(raw).hexdigest()
        locator = f'data/weather_captures/blobs/{digest[:2]}/{digest}.bin'
        self._publish(locator, raw)
        receipt = {**metadata, 'schema_version': 1, 'payload_sha256': digest,
                   'payload_bytes': len(raw), 'raw_payload_locator': locator}
        retained = self.retain_receipt(receipt)
        reference = {**receipt, 'capture_receipt_locator': retained['receipt_locator'],
                     'capture_receipt_sha256': retained['receipt_sha256'],
                     'storage': 'collector_content_addressed_v1'}
        self.read_verified(reference)
        return reference

    def retain_receipt(self, receipt: dict) -> dict:
        """Persist an attempt/collection receipt even when no HTTP body exists."""
        encoded = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
        locator = f'data/weather_captures/receipts/{uuid.uuid4().hex}.json'
        reference = {'schema_version': 1, 'receipt_locator': locator,
                     'receipt_sha256': hashlib.sha256(encoded).hexdigest()}
        self._publish(locator, encoded)
        self.read_receipt_verified(reference)
        return reference

    def read_receipt_verified(self, reference: dict) -> bytes:
        raw = self._read(reference['receipt_locator'])
        if hashlib.sha256(raw).hexdigest() != reference['receipt_sha256']:
            raise CaptureRetentionError('weather collection receipt digest mismatch')
        return raw

    def _read(self, locator: str) -> bytes:
        relative = Path(locator)
        if relative.is_absolute() or '..' in relative.parts or not relative.parts or relative.parts[:2] != ('data', 'weather_captures'):
            raise CaptureRetentionError('invalid weather capture locator')
        target = self.storage_root / relative
        if target.is_symlink() or not target.resolve().is_relative_to(self.storage_root):
            raise CaptureRetentionError('weather capture locator escapes storage root')
        try:
            return target.read_bytes()
        except OSError as exc:
            raise CaptureRetentionError('weather capture is unreadable') from exc

    def read_verified(self, reference: dict) -> bytes:
        receipt_bytes = self._read(reference['capture_receipt_locator'])
        if hashlib.sha256(receipt_bytes).hexdigest() != reference['capture_receipt_sha256']:
            raise CaptureRetentionError('weather capture receipt digest mismatch')
        receipt = json.loads(receipt_bytes)
        for key, value in receipt.items():
            if reference.get(key) != value:
                raise CaptureRetentionError('weather capture receipt identity mismatch')
        raw = self._read(receipt['raw_payload_locator'])
        if len(raw) != receipt['payload_bytes'] or hashlib.sha256(raw).hexdigest() != receipt['payload_sha256']:
            raise CaptureRetentionError('weather capture payload digest mismatch')
        return raw
