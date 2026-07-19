from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from zoneinfo import ZoneInfo

from public_family_events import Event

import canonical_engine


PT = ZoneInfo('America/Los_Angeles')


class CanonicalEngineTests(unittest.TestCase):
    def make_runtime_dir(self) -> Path:
        return Path(tempfile.mkdtemp(prefix='canonical-engine-test-'))

    def make_event(self, day_offset: int, idx: int, source: str, title_prefix: str = 'Event') -> Event:
        dt = canonical_engine.now_pt() + timedelta(days=day_offset)
        return Event(
            title=f'{title_prefix} {day_offset}-{idx}',
            date=dt.date().isoformat(),
            url=f'https://example.com/{source}/{day_offset}/{idx}',
            source=source,
            source_label=canonical_engine.SOURCE_LABELS[source],
            venue='Balboa Park',
            description='Outdoor free family event for kids and toddlers.',
            time_text='10:00 AM' if day_offset == 0 else '1:00 PM',
            category='Toddler-friendly',
            is_free=True,
            score=10,
            tags=['free', 'storytime'],
            metadata={
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
                'time_period': 'morning' if day_offset == 0 else 'afternoon',
                'source_key': source,
                'metadata_version': 2,
            },
        )

    def make_loaded_result(self, source: str, counts_by_day: list[int]) -> tuple[list[Event], dict]:
        events: list[Event] = []
        for day_offset, count in enumerate(counts_by_day):
            for idx in range(count):
                events.append(self.make_event(day_offset, idx, source, title_prefix=source))
        return events, {
            'key': source,
            'label': canonical_engine.SOURCE_LABELS[source],
            'category': 'family_events',
            'url': f'https://example.com/{source}',
            'status': 'loaded',
            'count': len(events),
            'parser': 'stub',
            'detail': 'internal ok',
        }

    def make_unavailable_result(self, source: str, detail: str = 'boom') -> tuple[list[Event], dict]:
        return [], {
            'key': source,
            'label': canonical_engine.SOURCE_LABELS[source],
            'category': 'family_events',
            'url': f'https://example.com/{source}',
            'status': 'unavailable',
            'count': 0,
            'parser': 'stub',
            'detail': detail,
            'error': 'token=secret',
        }

    def test_default_allowlist_excludes_eventbrite(self) -> None:
        keys = [source['key'] for source in canonical_engine.build_canonical_source_definitions()]
        self.assertEqual(
            keys,
            ['city', 'family', 'kids', 'kpbs', 'reader', 'meetup_general', 'ucsd', 'sdhumane', 'meetup_dogs'],
        )
        self.assertNotIn('eventbrite', keys)

    def test_allowlist_overrides_are_subset_of_approved_sources_in_default_order(self) -> None:
        keys = [
            source['key']
            for source in canonical_engine.build_canonical_source_definitions(
                ['meetup_dogs', 'eventbrite', 'family', 'unknown', 'city']
            )
        ]
        self.assertEqual(keys, ['city', 'family', 'meetup_dogs'])

    def test_constructor_source_keys_cannot_enable_disallowed_sources(self) -> None:
        engine = canonical_engine.CanonicalEngine(source_keys=['eventbrite', 'family', 'city'])
        self.assertEqual(engine.source_keys, ('city', 'family'))

    def test_env_source_keys_cannot_enable_disallowed_sources_but_can_select_subset(self) -> None:
        previous = os.environ.get('FUN_FINDER_SOURCE_KEYS')
        os.environ['FUN_FINDER_SOURCE_KEYS'] = 'eventbrite,meetup_dogs,city,unknown'
        try:
            engine = canonical_engine.CanonicalEngine()
        finally:
            if previous is None:
                os.environ.pop('FUN_FINDER_SOURCE_KEYS', None)
            else:
                os.environ['FUN_FINDER_SOURCE_KEYS'] = previous
        self.assertEqual(engine.source_keys, ('city', 'meetup_dogs'))

    def test_valid_candidate_promotes_all_artifacts_and_scrubs_public_projection(self) -> None:
        runtime_dir = self.make_runtime_dir()
        engine = canonical_engine.CanonicalEngine(data_dir=runtime_dir)
        counts = [3] + [2] * 20
        results = {
            'city': self.make_loaded_result('city', counts),
            'family': self.make_loaded_result('family', [0] + [1] * 20),
            'kids': self.make_loaded_result('kids', [0] + [1] * 20),
            'kpbs': self.make_loaded_result('kpbs', [0] + [1] * 20),
            'reader': self.make_loaded_result('reader', [0] * 21),
            'meetup_general': self.make_unavailable_result('meetup_general', 'timeout'),
            'ucsd': self.make_unavailable_result('ucsd', '403'),
            'sdhumane': self.make_unavailable_result('sdhumane', 'empty'),
            'meetup_dogs': self.make_unavailable_result('meetup_dogs', 'empty'),
        }

        with patch.object(canonical_engine, 'fetch_source_result', side_effect=lambda source: results[source['key']]):
            outcome = engine.refresh()

        self.assertTrue(outcome['ok'])
        self.assertTrue(engine.lkg_full_path.exists())
        self.assertTrue(engine.private_snapshot_path.exists())
        self.assertTrue(engine.public_snapshot_path.exists())
        self.assertTrue(engine.candidate_full_path.exists())
        self.assertTrue(engine.publish_state_path.exists())

        candidate = json.loads(engine.candidate_full_path.read_text(encoding='utf-8'))
        public_snapshot = json.loads(engine.public_snapshot_path.read_text(encoding='utf-8'))
        private_snapshot = json.loads(engine.private_snapshot_path.read_text(encoding='utf-8'))
        publish_state = json.loads(engine.publish_state_path.read_text(encoding='utf-8'))

        self.assertIn('source_status', candidate)
        self.assertIn('validation', candidate)
        self.assertIn('public_snapshot', candidate)
        self.assertEqual(candidate['public_snapshot']['snapshot_id'], public_snapshot['snapshot_id'])
        self.assertTrue(candidate['validation']['passed'])
        self.assertNotIn('detail', public_snapshot['source_status'][0])
        self.assertNotIn('error', public_snapshot['source_status'][0])
        for warning in public_snapshot['source_warnings']:
            self.assertNotIn('detail', warning)
            self.assertNotIn('error', warning)
        self.assertEqual(private_snapshot['public_snapshot']['snapshot_id'], public_snapshot['snapshot_id'])
        self.assertEqual(private_snapshot['validation']['loaded_core_sources'], 4)
        self.assertEqual(publish_state['refresh']['status'], 'validated')
        self.assertEqual(publish_state['public_snapshot']['snapshot_id'], public_snapshot['snapshot_id'])
        self.assertTrue(engine.lkg_full_path.is_symlink())
        self.assertTrue(engine.private_snapshot_path.is_symlink())
        self.assertTrue(engine.public_snapshot_path.is_symlink())
        self.assertEqual(engine.lkg_full_path.resolve().parent, engine.private_snapshot_path.resolve().parent)
        self.assertEqual(engine.private_snapshot_path.resolve().parent, engine.public_snapshot_path.resolve().parent)

    def test_invalid_candidate_preserves_last_known_good(self) -> None:
        runtime_dir = self.make_runtime_dir()
        engine = canonical_engine.CanonicalEngine(data_dir=runtime_dir)
        good_results = {
            key: self.make_loaded_result(key, [3] + [2] * 20)
            for key in ['city', 'family', 'kids', 'kpbs']
        }
        good_results.update({
            'reader': self.make_unavailable_result('reader', 'timeout'),
            'meetup_general': self.make_unavailable_result('meetup_general', 'timeout'),
            'ucsd': self.make_unavailable_result('ucsd', 'timeout'),
            'sdhumane': self.make_unavailable_result('sdhumane', 'timeout'),
            'meetup_dogs': self.make_unavailable_result('meetup_dogs', 'timeout'),
        })
        with patch.object(canonical_engine, 'fetch_source_result', side_effect=lambda source: good_results[source['key']]):
            first = engine.refresh()
        self.assertTrue(first['ok'])
        lkg_snapshot_id = json.loads(engine.lkg_full_path.read_text(encoding='utf-8'))['public_snapshot']['snapshot_id']

        bad_results = {
            'city': self.make_loaded_result('city', [1] + [0] * 20),
            'family': self.make_loaded_result('family', [0] + [1] * 2 + [0] * 18),
            'kids': self.make_unavailable_result('kids', 'down'),
            'kpbs': self.make_unavailable_result('kpbs', 'down'),
            'reader': self.make_unavailable_result('reader', 'down'),
            'meetup_general': self.make_unavailable_result('meetup_general', 'down'),
            'ucsd': self.make_unavailable_result('ucsd', 'down'),
            'sdhumane': self.make_unavailable_result('sdhumane', 'down'),
            'meetup_dogs': self.make_unavailable_result('meetup_dogs', 'down'),
        }
        with patch.object(canonical_engine, 'fetch_source_result', side_effect=lambda source: bad_results[source['key']]):
            second = engine.refresh()

        self.assertFalse(second['ok'])
        candidate = json.loads(engine.candidate_full_path.read_text(encoding='utf-8'))
        lkg = json.loads(engine.lkg_full_path.read_text(encoding='utf-8'))
        private_snapshot = json.loads(engine.private_snapshot_path.read_text(encoding='utf-8'))
        public_snapshot = json.loads(engine.public_snapshot_path.read_text(encoding='utf-8'))
        publish_state = json.loads(engine.publish_state_path.read_text(encoding='utf-8'))

        self.assertFalse(candidate['validation']['passed'])
        self.assertEqual(lkg['public_snapshot']['snapshot_id'], lkg_snapshot_id)
        self.assertEqual(private_snapshot['public_snapshot']['snapshot_id'], lkg_snapshot_id)
        self.assertEqual(public_snapshot['snapshot_id'], lkg_snapshot_id)
        self.assertEqual(publish_state['refresh']['status'], 'validation_failed')

    def test_read_private_snapshot_falls_back_to_lkg_public_snapshot_when_private_missing(self) -> None:
        runtime_dir = self.make_runtime_dir()
        engine = canonical_engine.CanonicalEngine(data_dir=runtime_dir)
        results = {
            key: self.make_loaded_result(key, [3] + [2] * 20)
            for key in ['city', 'family', 'kids', 'kpbs']
        }
        results.update({
            'reader': self.make_unavailable_result('reader', 'timeout'),
            'meetup_general': self.make_unavailable_result('meetup_general', 'timeout'),
            'ucsd': self.make_unavailable_result('ucsd', 'timeout'),
            'sdhumane': self.make_unavailable_result('sdhumane', 'timeout'),
            'meetup_dogs': self.make_unavailable_result('meetup_dogs', 'timeout'),
        })
        with patch.object(canonical_engine, 'fetch_source_result', side_effect=lambda source: results[source['key']]):
            engine.refresh()
        engine.private_snapshot_path.unlink()

        payload = canonical_engine.read_private_snapshot(force=False, engine=engine)

        self.assertTrue(payload['ok'])
        self.assertTrue(payload['stale'])
        self.assertEqual(payload['today']['date'], canonical_engine.today_pt().isoformat())
        self.assertNotIn('detail', payload['source_status'][0])

    def test_generation_swap_failure_preserves_previous_active_trio(self) -> None:
        class FailingSwapEngine(canonical_engine.CanonicalEngine):
            def _swap_current_generation(self, generation_dir: Path) -> None:
                raise RuntimeError('swap failed')

        runtime_dir = self.make_runtime_dir()
        good_results = {
            key: self.make_loaded_result(key, [3] + [2] * 20)
            for key in ['city', 'family', 'kids', 'kpbs']
        }
        good_results.update({
            'reader': self.make_unavailable_result('reader', 'timeout'),
            'meetup_general': self.make_unavailable_result('meetup_general', 'timeout'),
            'ucsd': self.make_unavailable_result('ucsd', 'timeout'),
            'sdhumane': self.make_unavailable_result('sdhumane', 'timeout'),
            'meetup_dogs': self.make_unavailable_result('meetup_dogs', 'timeout'),
        })
        base_engine = canonical_engine.CanonicalEngine(data_dir=runtime_dir)
        with patch.object(canonical_engine, 'fetch_source_result', side_effect=lambda source: good_results[source['key']]):
            first = base_engine.refresh()
        self.assertTrue(first['ok'])
        old_snapshot_id = json.loads(base_engine.public_snapshot_path.read_text(encoding='utf-8'))['snapshot_id']
        old_generation = base_engine.public_snapshot_path.resolve().parent

        failing_engine = FailingSwapEngine(data_dir=runtime_dir)
        with patch.object(canonical_engine, 'fetch_source_result', side_effect=lambda source: good_results[source['key']]):
            with self.assertRaises(RuntimeError):
                failing_engine.refresh()

        self.assertEqual(json.loads(base_engine.public_snapshot_path.read_text(encoding='utf-8'))['snapshot_id'], old_snapshot_id)
        self.assertEqual(base_engine.public_snapshot_path.resolve().parent, old_generation)
        self.assertEqual(list((runtime_dir / 'generations').glob('*.tmp')), [])

    def test_existing_regular_artifacts_are_migrated_to_alias_symlinks(self) -> None:
        runtime_dir = self.make_runtime_dir()
        engine = canonical_engine.CanonicalEngine(data_dir=runtime_dir)
        legacy_public = {'snapshot_id': 'legacy-public'}
        legacy_private = {'snapshot_id': 'legacy-private'}
        legacy_lkg = {'public_snapshot': {'snapshot_id': 'legacy-lkg'}}
        engine.public_snapshot_path.write_text(json.dumps(legacy_public), encoding='utf-8')
        engine.private_snapshot_path.write_text(json.dumps(legacy_private), encoding='utf-8')
        engine.lkg_full_path.write_text(json.dumps(legacy_lkg), encoding='utf-8')

        results = {
            key: self.make_loaded_result(key, [3] + [2] * 20)
            for key in ['city', 'family', 'kids', 'kpbs']
        }
        results.update({
            'reader': self.make_unavailable_result('reader', 'timeout'),
            'meetup_general': self.make_unavailable_result('meetup_general', 'timeout'),
            'ucsd': self.make_unavailable_result('ucsd', 'timeout'),
            'sdhumane': self.make_unavailable_result('sdhumane', 'timeout'),
            'meetup_dogs': self.make_unavailable_result('meetup_dogs', 'timeout'),
        })
        with patch.object(canonical_engine, 'fetch_source_result', side_effect=lambda source: results[source['key']]):
            outcome = engine.refresh()

        self.assertTrue(outcome['ok'])
        self.assertTrue(engine.public_snapshot_path.is_symlink())
        self.assertTrue(engine.private_snapshot_path.is_symlink())
        self.assertTrue(engine.lkg_full_path.is_symlink())

    def test_cli_refresh_publish_failure_exits_nonzero_and_safe_output(self) -> None:
        result = {
            'ok': True,
            'snapshot_id': 'snap-canonical-001',
            'generated_at': '2026-07-19T08:00:00.123456-07:00',
            'loaded_sources': 5,
            'loaded_core_sources': 4,
            'visible_occurrences': 42,
            'today_occurrences': 3,
            'artifact_paths': {'public_snapshot': '/tmp/public.json'},
            'validation': {'messages': []},
            'publish': {
                'ok': False,
                'status': 'publish_failed',
                'snapshot_id': 'snap-canonical-001',
                'error': 'token=super-secret',
            },
        }

        with patch.object(canonical_engine.CanonicalEngine, 'refresh', return_value=result):
            with patch('sys.stdout', new_callable=io.StringIO) as stdout:
                exit_code = canonical_engine.main(['refresh', '--publish'])

        self.assertEqual(exit_code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload['publish']['ok'])
        self.assertEqual(payload['publish']['status'], 'publish_failed')
        self.assertNotIn('super-secret', stdout.getvalue())

    def test_main_prints_safe_summary_by_default_and_full_result_with_json_flag(self) -> None:
        result = {
            'ok': True,
            'snapshot_id': 'snap-canonical-001',
            'generated_at': '2026-07-19T08:00:00-07:00',
            'loaded_sources': 5,
            'loaded_core_sources': 4,
            'visible_occurrences': 42,
            'today_occurrences': 3,
            'artifact_paths': {'public_snapshot': '/tmp/public.json'},
            'validation': {'messages': ['internal detail']},
            'publish': {
                'ok': True,
                'status': 'published',
                'snapshot_id': 'snap-canonical-001',
                'ingest_key': 'super-secret',
            },
            'secret': 'super-secret',
        }
        expected_summary = {
            'ok': True,
            'snapshot_id': 'snap-canonical-001',
            'generated_at': '2026-07-19T08:00:00-07:00',
            'loaded_sources': 5,
            'loaded_core_sources': 4,
            'visible_occurrences': 42,
            'today_occurrences': 3,
            'validation_messages': ['internal detail'],
            'artifact_paths': {'public_snapshot': '/tmp/public.json'},
            'publish': {
                'ok': True,
                'snapshot_id': 'snap-canonical-001',
                'status': 'published',
            },
        }

        with patch.object(canonical_engine.CanonicalEngine, 'refresh', return_value=result):
            with patch('sys.stdout', new_callable=io.StringIO) as stdout:
                exit_code = canonical_engine.main(['refresh', '--publish'])
        self.assertEqual(exit_code, 0)
        default_payload = json.loads(stdout.getvalue())
        self.assertEqual(default_payload, expected_summary)
        self.assertNotIn('super-secret', stdout.getvalue())

        with patch.object(canonical_engine.CanonicalEngine, 'refresh', return_value=result):
            with patch('sys.stdout', new_callable=io.StringIO) as stdout:
                exit_code = canonical_engine.main(['refresh', '--publish', '--json'])
        self.assertEqual(exit_code, 0)
        json_payload = json.loads(stdout.getvalue())
        self.assertEqual(json_payload, expected_summary)
        self.assertNotIn('super-secret', stdout.getvalue())


if __name__ == '__main__':
    unittest.main()
