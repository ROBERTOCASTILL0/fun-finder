from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.error import URLError

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

        def offline_opener(*_args, **_kwargs):
            raise URLError('durable recovery disabled in route unit test')

        setattr(
            self.app_module,
            'load_active_snapshot',
            lambda: snapshot_store.load_active_snapshot(
                durable_url='https://durable.test/snapshot.json',
                opener=offline_opener,
            ),
        )
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
        x_header_auth = self.client.post(
            '/internal/snapshot',
            json=make_snapshot(snapshot_id='runtime-2', generated_at='2026-07-19T08:01:58.123456-07:00'),
            headers={'X-Snapshot-Key': 'ingest-key'},
        )

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

    def test_internal_snapshot_rejects_naive_generated_at_without_replacing_active(self):
        self.client.post('/internal/snapshot', json=make_snapshot(snapshot_id='runtime-good'), headers={'Authorization': 'Bearer ingest-key'})

        publish = self.client.post(
            '/internal/snapshot',
            json=make_snapshot(snapshot_id='runtime-naive', generated_at='2026-07-19T08:01:58'),
            headers={'Authorization': 'Bearer ingest-key'},
        )
        response = self.client.get('/api/events')

        self.assertEqual(publish.status_code, 400)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['snapshot_id'], 'runtime-good')

    def test_internal_snapshot_rejects_deep_metadata_without_replacing_active(self):
        self.client.post('/internal/snapshot', json=make_snapshot(snapshot_id='runtime-good'), headers={'Authorization': 'Bearer ingest-key'})
        invalid = make_snapshot(snapshot_id='runtime-deep')
        deep_value = 'leaf'
        for depth in range(7):
            deep_value = {f'level_{depth}': deep_value}
        invalid['today']['events'][0]['metadata']['extra'] = deep_value
        invalid['calendar'][0]['events'][0]['metadata']['extra'] = deep_value

        publish = self.client.post('/internal/snapshot', json=invalid, headers={'Authorization': 'Bearer ingest-key'})
        response = self.client.get('/api/events')

        self.assertEqual(publish.status_code, 400)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['snapshot_id'], 'runtime-good')


if __name__ == '__main__':
    unittest.main()
