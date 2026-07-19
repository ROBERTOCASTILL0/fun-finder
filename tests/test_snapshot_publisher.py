from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import snapshot_publisher


class _FakeResponse:
    def __init__(self, payload: dict, *, status: int):
        self._payload = json.dumps(payload).encode('utf-8')
        self.status = status

    def read(self) -> bytes:
        return self._payload


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

    def test_publish_accepts_and_reads_back_before_advancing_durable_mirror(self) -> None:
        runtime_dir = Path(tempfile.mkdtemp(prefix='publisher-test-'))
        snapshot_path = runtime_dir / 'fun_finder_public_snapshot.json'
        publish_state_path = runtime_dir / 'fun_finder_publish_state.json'
        snapshot = self.make_snapshot()
        snapshot['calendar'][0]['events'] = list(snapshot['today']['events'])
        snapshot_path.write_text(json.dumps(snapshot), encoding='utf-8')

        requests: list[dict] = []
        mirror_calls: list[dict] = []
        order: list[str] = []

        def opener(request, timeout=0):
            order.append(request.get_method())
            requests.append({
                'method': request.get_method(),
                'url': request.full_url,
                'authorization': request.get_header('Authorization'),
                'timeout': timeout,
            })
            if request.get_method() == 'POST':
                return _FakeResponse({'ok': True, 'snapshot_id': snapshot['snapshot_id']}, status=202)
            return _FakeResponse({'snapshot_id': snapshot['snapshot_id'], 'events': []}, status=200)

        publisher = snapshot_publisher.SnapshotPublisher(
            base_url='https://publisher.example.test',
            ingest_key='super-secret',
            public_snapshot_path=snapshot_path,
            publish_state_path=publish_state_path,
            allow_test_base_url=True,
            mirror_factory=lambda **kwargs: type('Mirror', (), {
                'mirror': staticmethod(lambda: order.append('MIRROR') or mirror_calls.append(kwargs) or {
                    'ok': True,
                    'status': 'mirrored',
                    'snapshot_id': snapshot['snapshot_id'],
                })
            })(),
            opener=opener,
        )
        result = publisher.publish()

        self.assertTrue(result['ok'])
        self.assertEqual(result['mirror']['status'], 'mirrored')
        self.assertEqual(len(mirror_calls), 1)
        self.assertEqual(requests[0]['url'], 'https://publisher.example.test/internal/snapshot')
        self.assertEqual(requests[0]['authorization'], 'Bearer super-secret')
        self.assertEqual(requests[1]['url'], 'https://publisher.example.test/api/events')
        self.assertIsNone(requests[1]['authorization'])
        self.assertEqual(order, ['POST', 'GET', 'MIRROR'])
        state = json.loads(publish_state_path.read_text(encoding='utf-8'))
        self.assertEqual(state['publish']['status'], 'published')
        self.assertEqual(state['publish']['snapshot_id'], snapshot['snapshot_id'])
        self.assertNotIn('super-secret', json.dumps(state))

    def test_invalid_base_url_rejects_before_any_opener_calls(self) -> None:
        runtime_dir = Path(tempfile.mkdtemp(prefix='publisher-test-'))
        snapshot_path = runtime_dir / 'fun_finder_public_snapshot.json'
        snapshot_path.write_text(json.dumps(self.make_snapshot()), encoding='utf-8')
        calls = 0

        def opener(request, timeout=0):
            nonlocal calls
            del request, timeout
            calls += 1
            raise AssertionError('opener should not be called')

        publisher = snapshot_publisher.SnapshotPublisher(
            base_url='https://user:pw@evil.example.com/path?x=1',
            ingest_key='super-secret',
            public_snapshot_path=snapshot_path,
            publish_state_path=runtime_dir / 'state.json',
            opener=opener,
            allow_test_base_url=True,
        )

        result = publisher.publish()

        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'publish_failed')
        self.assertEqual(calls, 0)
        self.assertIn('base URL', result['error'])

    def test_mirror_failure_after_runtime_acceptance_is_reported_for_retry(self) -> None:
        runtime_dir = Path(tempfile.mkdtemp(prefix='publisher-test-'))
        snapshot_path = runtime_dir / 'fun_finder_public_snapshot.json'
        publish_state_path = runtime_dir / 'fun_finder_publish_state.json'
        snapshot_path.write_text(json.dumps(self.make_snapshot()), encoding='utf-8')
        opener_calls = 0

        def opener(request, timeout=0):
            nonlocal opener_calls
            del timeout
            opener_calls += 1
            if request.get_method() == 'POST':
                return _FakeResponse({'ok': True, 'snapshot_id': 'snap-publisher-001'}, status=202)
            return _FakeResponse({'snapshot_id': 'snap-publisher-001', 'events': []}, status=200)

        publisher = snapshot_publisher.SnapshotPublisher(
            base_url='https://san-diego-fun-finder.onrender.com',
            ingest_key='super-secret',
            public_snapshot_path=snapshot_path,
            publish_state_path=publish_state_path,
            opener=opener,
            mirror_factory=lambda **kwargs: type('Mirror', (), {
                'mirror': staticmethod(lambda: (_ for _ in ()).throw(RuntimeError('mirror failed')))
            })(),
        )

        result = publisher.publish()

        self.assertFalse(result['ok'])
        self.assertEqual(opener_calls, 2)
        state = json.loads(publish_state_path.read_text(encoding='utf-8'))
        self.assertEqual(state['publish']['status'], 'publish_failed')
        self.assertIn('mirror failed', state['publish']['error'])

    def test_runtime_rejection_does_not_advance_durable_mirror(self) -> None:
        runtime_dir = Path(tempfile.mkdtemp(prefix='publisher-test-'))
        snapshot_path = runtime_dir / 'fun_finder_public_snapshot.json'
        snapshot_path.write_text(json.dumps(self.make_snapshot()), encoding='utf-8')
        mirror_calls = 0

        def opener(_request, timeout=0):
            del timeout
            return _FakeResponse({'ok': False, 'error': 'rejected'}, status=400)

        def mirror_factory(**_kwargs):
            nonlocal mirror_calls
            mirror_calls += 1
            raise AssertionError('durable mirror must not advance before runtime acceptance')

        publisher = snapshot_publisher.SnapshotPublisher(
            base_url='https://san-diego-fun-finder.onrender.com',
            ingest_key='super-secret',
            public_snapshot_path=snapshot_path,
            publish_state_path=runtime_dir / 'state.json',
            opener=opener,
            mirror_factory=mirror_factory,
        )

        result = publisher.publish()

        self.assertFalse(result['ok'])
        self.assertEqual(mirror_calls, 0)
        self.assertIn('expected HTTP 202', result['error'])

    def test_main_prints_safe_summary_by_default_and_full_safe_json_with_flag(self) -> None:
        result = {
            'ok': True,
            'status': 'published',
            'snapshot_id': 'snap-publisher-001',
            'generated_at': '2026-07-19T08:00:00-07:00',
            'accepted': {'snapshot_id': 'snap-publisher-001', 'ok': True, 'secret': 'nope'},
            'readback': {'snapshot_id': 'snap-publisher-001', 'event_count': 7},
            'mirror': {'snapshot_id': 'snap-publisher-001', 'status': 'mirrored', 'token': 'hide-me'},
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
                'mirror': {'snapshot_id': 'snap-publisher-001', 'status': 'mirrored', 'ok': False},
            },
        )
        self.assertNotIn('super-secret', stdout.getvalue())
        self.assertNotIn('secret', stdout.getvalue())
        self.assertNotIn('hide-me', stdout.getvalue())

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
