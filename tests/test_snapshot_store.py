from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from snapshot_schema import SnapshotValidationError
import snapshot_store
from snapshot_store import load_active_snapshot, publish_snapshot
from tests.snapshot_fixtures import make_snapshot


class _FakeHttpResponse:
    def __init__(self, payload: bytes, *, status: int = 200, headers: dict[str, str] | None = None):
        self._payload = payload
        self._offset = 0
        self.status = status
        self.headers = headers or {}

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = len(self._payload) - self._offset
        chunk = self._payload[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class SnapshotStoreTests(unittest.TestCase):
    def test_default_durable_url_targets_public_github_contents_endpoint(self) -> None:
        self.assertEqual(
            snapshot_store.durable_snapshot_url(),
            'https://api.github.com/repos/ROBERTOCASTILL0/fun-finder/contents/data/public_snapshot.json?ref=published-snapshot',
        )

    def test_unapproved_durable_url_is_rejected(self) -> None:
        with patch.dict(os.environ, {'FUN_FINDER_DURABLE_SNAPSHOT_URL': 'https://example.com/snapshot.json'}):
            with self.assertRaises(ValueError):
                snapshot_store.durable_snapshot_url()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime_path = Path(self.tmp.name) / 'runtime.json'
        self.packaged_path = Path(self.tmp.name) / 'packaged.json'
        self.packaged_path.write_text(json.dumps(make_snapshot(snapshot_id='packaged-1')), encoding='utf-8')
        os.environ['FUN_FINDER_RUNTIME_SNAPSHOT_PATH'] = str(self.runtime_path)
        self.original_packaged_path = snapshot_store.PACKAGED_SNAPSHOT_PATH
        snapshot_store.PACKAGED_SNAPSHOT_PATH = self.packaged_path

    def tearDown(self):
        snapshot_store.PACKAGED_SNAPSHOT_PATH = self.original_packaged_path
        os.environ.pop('FUN_FINDER_RUNTIME_SNAPSHOT_PATH', None)
        os.environ.pop('FUN_FINDER_DURABLE_SNAPSHOT_URL', None)
        self.tmp.cleanup()

    def test_publish_valid_snapshot_and_load_runtime_first(self):
        published = publish_snapshot(make_snapshot())

        loaded = load_active_snapshot()

        self.assertEqual(loaded['snapshot_id'], published['snapshot_id'])
        self.assertTrue(self.runtime_path.exists())
        self.assertEqual(json.loads(self.runtime_path.read_text(encoding='utf-8'))['snapshot_id'], published['snapshot_id'])

    def test_packaged_fallback_is_used_when_runtime_and_durable_missing(self):
        loaded = load_active_snapshot(opener=lambda request, timeout=0: (_ for _ in ()).throw(URLError('offline')))

        self.assertEqual(loaded['snapshot_id'], 'packaged-1')

    def test_restart_with_missing_runtime_recovers_from_durable_snapshot(self):
        durable = make_snapshot(snapshot_id='durable-1')
        opener_calls: list[dict[str, object]] = []

        def opener(request, timeout=0):
            opener_calls.append({
                'url': request.full_url,
                'timeout': timeout,
                'user_agent': request.get_header('User-agent'),
            })
            return _FakeHttpResponse(json.dumps(durable).encode('utf-8'))

        loaded = load_active_snapshot(durable_url='https://raw.githubusercontent.com/example/repo/main/public.json', opener=opener)

        self.assertEqual(loaded['snapshot_id'], 'durable-1')
        cached = json.loads(self.runtime_path.read_text(encoding='utf-8'))
        self.assertEqual(cached['snapshot_id'], 'durable-1')
        self.assertEqual(opener_calls[0]['timeout'], 8)
        self.assertEqual(opener_calls[0]['user_agent'], 'sd-fun-finder/1.0')

    def test_invalid_or_unavailable_durable_falls_back_to_packaged(self):
        invalid_payload = b'{"snapshot_id": "broken"}'
        attempts = [
            _FakeHttpResponse(invalid_payload),
            URLError('offline'),
        ]

        def opener(request, timeout=0):
            next_item = attempts.pop(0)
            if isinstance(next_item, Exception):
                raise next_item
            return next_item

        first = load_active_snapshot(durable_url='https://raw.githubusercontent.com/example/repo/main/public.json', opener=opener)
        second = load_active_snapshot(durable_url='https://raw.githubusercontent.com/example/repo/main/public.json', opener=opener)

        self.assertEqual(first['snapshot_id'], 'packaged-1')
        self.assertEqual(second['snapshot_id'], 'packaged-1')
        self.assertFalse(self.runtime_path.exists())

    def test_durable_newer_snapshot_survives_simulated_restart(self):
        publish_snapshot(make_snapshot(snapshot_id='runtime-1', generated_at='2026-07-19T08:01:58.111111-07:00'))
        self.runtime_path.unlink()
        durable = make_snapshot(snapshot_id='durable-2', generated_at='2026-07-19T08:01:58.222222-07:00')

        loaded = load_active_snapshot(
            durable_url='https://raw.githubusercontent.com/example/repo/main/public.json',
            opener=lambda request, timeout=0: _FakeHttpResponse(json.dumps(durable).encode('utf-8')),
        )

        self.assertEqual(loaded['snapshot_id'], 'durable-2')
        self.assertEqual(load_active_snapshot()['snapshot_id'], 'durable-2')

    def test_invalid_snapshot_does_not_replace_active(self):
        publish_snapshot(make_snapshot(snapshot_id='good-1'))
        invalid = make_snapshot(snapshot_id='bad-1')
        invalid['today']['events'][0]['url'] = 'ftp://not-allowed.example.com'
        invalid['calendar'][0]['events'][0]['url'] = 'ftp://not-allowed.example.com'

        with self.assertRaises(SnapshotValidationError):
            publish_snapshot(invalid)

        loaded = load_active_snapshot()
        self.assertEqual(loaded['snapshot_id'], 'good-1')

    def test_older_snapshot_does_not_replace_active(self):
        publish_snapshot(make_snapshot(snapshot_id='newer', generated_at='2026-07-19T08:01:58.100000-07:00'))

        with self.assertRaises(ValueError):
            publish_snapshot(make_snapshot(snapshot_id='older', generated_at='2026-07-18T08:01:58.999999-07:00'))

        loaded = load_active_snapshot()
        self.assertEqual(loaded['snapshot_id'], 'newer')

    def test_equal_timestamp_same_snapshot_id_is_idempotent(self):
        payload = make_snapshot(snapshot_id='same-id', generated_at='2026-07-19T08:01:58.123456-07:00')
        publish_snapshot(payload)

        published = publish_snapshot(payload)

        self.assertEqual(published['snapshot_id'], 'same-id')
        self.assertEqual(load_active_snapshot()['snapshot_id'], 'same-id')

    def test_equal_timestamp_different_snapshot_id_is_rejected(self):
        publish_snapshot(make_snapshot(snapshot_id='first-id', generated_at='2026-07-19T08:01:58.123456-07:00'))

        with self.assertRaises(ValueError):
            publish_snapshot(make_snapshot(snapshot_id='second-id', generated_at='2026-07-19T08:01:58.123456-07:00'))

        self.assertEqual(load_active_snapshot()['snapshot_id'], 'first-id')

    def test_naive_generated_at_does_not_replace_active(self):
        publish_snapshot(make_snapshot(snapshot_id='good-1'))

        with self.assertRaises(SnapshotValidationError):
            publish_snapshot(make_snapshot(snapshot_id='bad-naive', generated_at='2026-07-19T08:01:58'))

        loaded = load_active_snapshot()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded['snapshot_id'], 'good-1')

    def test_deep_metadata_does_not_replace_active(self):
        publish_snapshot(make_snapshot(snapshot_id='good-1'))
        invalid = make_snapshot(snapshot_id='bad-deep')
        deep_value = 'leaf'
        for depth in range(7):
            deep_value = {f'level_{depth}': deep_value}
        invalid['today']['events'][0]['metadata']['extra'] = deep_value
        invalid['calendar'][0]['events'][0]['metadata']['extra'] = deep_value

        with self.assertRaises(SnapshotValidationError):
            publish_snapshot(invalid)

        loaded = load_active_snapshot()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded['snapshot_id'], 'good-1')


if __name__ == '__main__':
    unittest.main()
