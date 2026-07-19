from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import snapshot_publisher


class SnapshotPublisherTests(unittest.TestCase):
    def make_snapshot(self) -> dict:
        return {
            'schema_version': 1,
            'snapshot_id': 'snap-publisher-001',
            'generated_at': '2026-07-19T08:00:00-07:00',
            'ok': True,
            'sources': ['City of San Diego'],
            'errors': [],
            'source_status': [
                {
                    'key': 'city',
                    'label': 'City of San Diego',
                    'category': 'family_events',
                    'url': 'https://example.com/city',
                    'status': 'loaded',
                    'count': 2,
                    'parser': 'city',
                }
            ],
            'source_warnings': [],
            'today': {
                'date': '2026-07-19',
                'weekday': 'Sat',
                'label': 'Jul 19',
                'is_today': True,
                'count': 1,
                'summary': 'One event today.',
                'events': [
                    {
                        'title': 'Storytime',
                        'date': '2026-07-19',
                        'url': 'https://example.com/storytime',
                        'source': 'city',
                        'source_label': 'City of San Diego',
                        'venue': 'Balboa Park',
                        'description': 'Family event',
                        'time_text': '10:00 AM',
                        'category': 'Toddler-friendly',
                        'is_free': True,
                        'score': 5,
                        'tags': ['free'],
                        'metadata': {
                            'audience': 'young_children',
                            'age_groups': ['kids', 'toddler'],
                            'features': {
                                'free': True,
                                'outdoor': True,
                                'indoor': False,
                                'dog_friendly': False,
                                'toddler_friendly': True,
                                'stroller_friendly': True,
                                'low_walking': True,
                                'shade': True,
                                'bathrooms': True,
                                'food_nearby': False,
                            },
                            'area': 'balboa',
                            'time_period': 'morning',
                            'source_key': 'city',
                            'metadata_version': 2,
                        },
                    }
                ],
            },
            'calendar': [
                {
                    'date': '2026-07-19',
                    'weekday': 'Sat',
                    'label': 'Jul 19',
                    'is_today': True,
                    'count': 1,
                    'summary': 'One event today.',
                    'events': [],
                }
            ],
            'counts': {
                'total_events': 1,
                'days': 1,
                'free_events': 1,
                'configured_sources': 1,
                'loaded_sources': 1,
            },
            'top_categories': [{'name': 'Toddler-friendly', 'count': 1}],
        }

    def test_publish_uses_bearer_auth_and_verifies_readback(self) -> None:
        runtime_dir = Path(tempfile.mkdtemp(prefix='publisher-test-'))
        snapshot_path = runtime_dir / 'fun_finder_public_snapshot.json'
        publish_state_path = runtime_dir / 'fun_finder_publish_state.json'
        snapshot = self.make_snapshot()
        snapshot['calendar'][0]['events'] = list(snapshot['today']['events'])
        snapshot_path.write_text(json.dumps(snapshot), encoding='utf-8')

        requests: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', '0'))).decode('utf-8')
                requests.append({
                    'method': 'POST',
                    'path': self.path,
                    'authorization': self.headers.get('Authorization'),
                    'body': json.loads(body),
                })
                self.send_response(202)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'ok': True, 'snapshot_id': snapshot['snapshot_id'], 'accepted': True}).encode('utf-8'))

            def do_GET(self):
                requests.append({
                    'method': 'GET',
                    'path': self.path,
                    'authorization': self.headers.get('Authorization'),
                    'snapshot_key': self.headers.get('X-Snapshot-Key'),
                })
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'snapshot_id': snapshot['snapshot_id'], 'events': []}).encode('utf-8'))

            def log_message(self, format, *args):
                return

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            publisher = snapshot_publisher.SnapshotPublisher(
                base_url=f'http://127.0.0.1:{server.server_port}',
                ingest_key='super-secret',
                public_snapshot_path=snapshot_path,
                publish_state_path=publish_state_path,
            )
            result = publisher.publish()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertTrue(result['ok'])
        self.assertEqual(requests[0]['path'], '/internal/snapshot')
        self.assertEqual(requests[0]['authorization'], 'Bearer super-secret')
        self.assertEqual(requests[1]['path'], '/api/events')
        self.assertIsNone(requests[1]['authorization'])
        self.assertIsNone(requests[1]['snapshot_key'])
        state = json.loads(publish_state_path.read_text(encoding='utf-8'))
        self.assertEqual(state['publish']['status'], 'published')
        self.assertEqual(state['publish']['snapshot_id'], snapshot['snapshot_id'])
        self.assertNotIn('super-secret', json.dumps(state))

    def test_main_prints_safe_summary_by_default_and_full_safe_json_with_flag(self) -> None:
        result = {
            'ok': True,
            'status': 'published',
            'snapshot_id': 'snap-publisher-001',
            'generated_at': '2026-07-19T08:00:00-07:00',
            'accepted': {'snapshot_id': 'snap-publisher-001', 'ok': True, 'secret': 'nope'},
            'readback': {'snapshot_id': 'snap-publisher-001', 'event_count': 7},
            'ingest_key': 'super-secret',
        }

        with patch.object(snapshot_publisher.SnapshotPublisher, 'publish', return_value=result):
            with patch('sys.stdout', new_callable=io.StringIO) as stdout:
                exit_code = snapshot_publisher.main([])
        self.assertEqual(exit_code, 0)
        default_payload = json.loads(stdout.getvalue())
        self.assertEqual(
            default_payload,
            {
                'ok': True,
                'status': 'published',
                'snapshot_id': 'snap-publisher-001',
                'generated_at': '2026-07-19T08:00:00-07:00',
                'accepted': {'snapshot_id': 'snap-publisher-001', 'ok': True},
                'readback': {'snapshot_id': 'snap-publisher-001', 'event_count': 7},
            },
        )
        self.assertNotIn('super-secret', stdout.getvalue())
        self.assertNotIn('secret', stdout.getvalue())

        with patch.object(snapshot_publisher.SnapshotPublisher, 'publish', return_value=result):
            with patch('sys.stdout', new_callable=io.StringIO) as stdout:
                exit_code = snapshot_publisher.main(['--json'])
        self.assertEqual(exit_code, 0)
        json_payload = json.loads(stdout.getvalue())
        self.assertEqual(json_payload, default_payload)
        self.assertNotIn('super-secret', stdout.getvalue())
        self.assertNotIn('secret', stdout.getvalue())


if __name__ == '__main__':
    unittest.main()
