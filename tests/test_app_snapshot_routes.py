from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from tests.snapshot_fixtures import make_snapshot


class AppSnapshotRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime_path = Path(self.tmp.name) / 'runtime.json'
        self.packaged_path = Path(self.tmp.name) / 'packaged.json'
        self.packaged_path.write_text(json.dumps(make_snapshot(snapshot_id='packaged-bootstrap')), encoding='utf-8')
        os.environ['FUN_FINDER_RUNTIME_SNAPSHOT_PATH'] = str(self.runtime_path)
        os.environ['FUN_FINDER_SNAPSHOT_INGEST_KEY'] = 'ingest-key'
        self.original_app_module = sys.modules.get('app')
        self.original_public_family_events = sys.modules.get('public_family_events')
        sys.modules.pop('app', None)
        sys.modules.pop('public_family_events', None)
        self.app_module = importlib.import_module('app')
        import snapshot_store

        self.snapshot_store = snapshot_store
        self.original_packaged_snapshot_path = snapshot_store.PACKAGED_SNAPSHOT_PATH
        snapshot_store.PACKAGED_SNAPSHOT_PATH = self.packaged_path
        self.app_module.app.config['TESTING'] = True
        self.client = self.app_module.app.test_client()

    def tearDown(self):
        self.snapshot_store.PACKAGED_SNAPSHOT_PATH = self.original_packaged_snapshot_path
        sys.modules.pop('app', None)
        if self.original_app_module is not None:
            sys.modules['app'] = self.original_app_module
        sys.modules.pop('public_family_events', None)
        if self.original_public_family_events is not None:
            sys.modules['public_family_events'] = self.original_public_family_events
        os.environ.pop('FUN_FINDER_RUNTIME_SNAPSHOT_PATH', None)
        os.environ.pop('FUN_FINDER_SNAPSHOT_INGEST_KEY', None)
        self.tmp.cleanup()

    def test_get_api_events_reads_snapshot_without_importing_scraper(self):
        response = self.client.get('/api/events')

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('public_family_events', sys.modules)
        self.assertEqual(response.get_json()['snapshot_id'], 'packaged-bootstrap')

    def test_get_api_events_returns_503_when_no_valid_snapshot_exists(self):
        self.snapshot_store.PACKAGED_SNAPSHOT_PATH = Path(self.tmp.name) / 'missing.json'
        response = self.client.get('/api/events')

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_internal_snapshot_auth_matrix(self):
        no_auth = self.client.post('/internal/snapshot', json=make_snapshot())
        query_auth = self.client.post('/internal/snapshot?key=ingest-key', json=make_snapshot())
        header_auth = self.client.post('/internal/snapshot', json=make_snapshot(snapshot_id='runtime-1'), headers={'Authorization': 'Bearer ingest-key'})
        x_header_auth = self.client.post('/internal/snapshot', json=make_snapshot(snapshot_id='runtime-2'), headers={'X-Snapshot-Key': 'ingest-key'})

        self.assertEqual(no_auth.status_code, 403)
        self.assertEqual(query_auth.status_code, 403)
        self.assertEqual(header_auth.status_code, 202)
        self.assertEqual(x_header_auth.status_code, 202)
        self.assertEqual(header_auth.headers['Vary'], 'Authorization, X-Snapshot-Key')
        self.assertEqual(header_auth.headers['Cache-Control'], 'no-store')

    def test_valid_publish_is_visible_via_get(self):
        publish = self.client.post('/internal/snapshot', json=make_snapshot(snapshot_id='runtime-visible'), headers={'Authorization': 'Bearer ingest-key'})

        response = self.client.get('/api/events')

        self.assertEqual(publish.status_code, 202)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['snapshot_id'], 'runtime-visible')


if __name__ == '__main__':
    unittest.main()
