"""Collector-owned exact weather bytes; downstream artifacts carry references only.

The caller selects one persistent collector root. This module never infers a
checkout, runtime, or archive root, deletes evidence, or imposes a retention cap.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid


class CaptureRetentionError(RuntimeError):
    """Evidence could not be durably retained or its identity did not verify."""


class WeatherCaptureStore:
    def __init__(self, *, storage_root: str | Path):
        root = Path(storage_root).expanduser()
        if not root.is_absolute():
            raise ValueError('weather capture storage root must be absolute')
        self.storage_root = root.resolve()

    def _publish(self, relative: str, raw: bytes) -> None:
        """Publish complete read-only bytes without replacing an existing object."""
        target = self.storage_root / relative
        temporary = None
        try:
            if not target.parent.resolve().is_relative_to(self.storage_root):
                raise CaptureRetentionError('weather capture directory escapes storage root')
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix='.capture-', dir=target.parent)
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
            raise CaptureRetentionError('weather capture persistence failed') from exc
        finally:
            if temporary is not None:
                os.unlink(temporary)

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
