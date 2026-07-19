from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from snapshot_schema import SnapshotValidationError
from snapshot_store import publish_snapshot, load_active_snapshot
from tests.snapshot_fixtures import make_snapshot


class SnapshotStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime_path = Path(self.tmp.name) / 'runtime.json'
        os.environ['FUN_FINDER_RUNTIME_SNAPSHOT_PATH'] = str(self.runtime_path)

    def tearDown(self):
        os.environ.pop('FUN_FINDER_RUNTIME_SNAPSHOT_PATH', None)
        self.tmp.cleanup()

    def test_publish_valid_snapshot_and_load_runtime_first(self):
        published = publish_snapshot(make_snapshot())

        loaded = load_active_snapshot()

        self.assertEqual(loaded['snapshot_id'], published['snapshot_id'])
        self.assertTrue(self.runtime_path.exists())
        self.assertEqual(json.loads(self.runtime_path.read_text(encoding='utf-8'))['snapshot_id'], published['snapshot_id'])

    def test_packaged_fallback_is_used_when_runtime_missing(self):
        fallback_path = Path(self.tmp.name) / 'packaged.json'
        fallback_path.write_text(json.dumps(make_snapshot(snapshot_id='packaged-1')), encoding='utf-8')

        import snapshot_store

        original = snapshot_store.PACKAGED_SNAPSHOT_PATH
        snapshot_store.PACKAGED_SNAPSHOT_PATH = fallback_path
        try:
            loaded = load_active_snapshot()
        finally:
            snapshot_store.PACKAGED_SNAPSHOT_PATH = original

        self.assertEqual(loaded['snapshot_id'], 'packaged-1')

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
        publish_snapshot(make_snapshot(snapshot_id='newer', generated_at='2026-07-19T08:01:58-07:00'))

        with self.assertRaises(ValueError):
            publish_snapshot(make_snapshot(snapshot_id='older', generated_at='2026-07-18T08:01:58-07:00'))

        loaded = load_active_snapshot()
        self.assertEqual(loaded['snapshot_id'], 'newer')

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
