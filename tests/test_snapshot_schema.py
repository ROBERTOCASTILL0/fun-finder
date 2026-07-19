from __future__ import annotations

import json
import unittest
from pathlib import Path

from snapshot_schema import SCHEMA_VERSION, SnapshotValidationError, normalize_public_snapshot
from tests.snapshot_fixtures import make_snapshot


class SnapshotSchemaTests(unittest.TestCase):
    def test_valid_snapshot_is_normalized_and_scrubbed(self):
        payload = make_snapshot()

        normalized = normalize_public_snapshot(payload)

        self.assertEqual(normalized['schema_version'], SCHEMA_VERSION)
        self.assertEqual(normalized['errors'], [])
        self.assertEqual(normalized['source_warnings'], [])
        self.assertNotIn('detail', normalized['source_status'][0])
        self.assertNotIn('error', normalized['source_status'][0])
        self.assertEqual(normalized['today']['events'][0]['title'], 'Storytime in the Park')

    def test_rejects_day_event_date_mismatch(self):
        payload = make_snapshot()
        payload['calendar'][0]['events'][0]['date'] = '2026-07-20'

        with self.assertRaises(SnapshotValidationError):
            normalize_public_snapshot(payload)

    def test_rejects_non_http_url(self):
        payload = make_snapshot()
        payload['today']['events'][0]['url'] = 'javascript:alert(1)'
        payload['calendar'][0]['events'][0]['url'] = 'javascript:alert(1)'

        with self.assertRaises(SnapshotValidationError):
            normalize_public_snapshot(payload)

    def test_rejects_naive_generated_at_timestamp(self):
        payload = make_snapshot(generated_at='2026-07-19T08:01:58')

        with self.assertRaises(SnapshotValidationError):
            normalize_public_snapshot(payload)

    def test_rejects_metadata_nested_beyond_max_depth(self):
        payload = make_snapshot()
        deep_value = 'leaf'
        for depth in range(7):
            deep_value = {f'level_{depth}': deep_value}
        payload['today']['events'][0]['metadata']['extra'] = deep_value
        payload['calendar'][0]['events'][0]['metadata']['extra'] = deep_value

        with self.assertRaises(SnapshotValidationError):
            normalize_public_snapshot(payload)

    def test_rejects_more_than_21_days(self):
        payload = make_snapshot()
        while len(payload['calendar']) < 22:
            payload['calendar'].append({
                'date': f"2026-08-{len(payload['calendar']) + 18:02d}",
                'weekday': 'Mon',
                'label': 'Aug X',
                'is_today': False,
                'count': 0,
                'summary': 'No events yet.',
                'events': [],
            })
        payload['counts']['days'] = len(payload['calendar'])

        with self.assertRaises(SnapshotValidationError):
            normalize_public_snapshot(payload)

    def test_rejects_more_than_12_events_per_day(self):
        payload = make_snapshot()
        event = payload['today']['events'][0]
        payload['today']['events'] = [dict(event, title=f'Event {idx}') for idx in range(13)]
        payload['calendar'][0]['events'] = list(payload['today']['events'])
        payload['today']['count'] = 13
        payload['calendar'][0]['count'] = 13
        payload['counts']['total_events'] = 13
        payload['counts']['free_events'] = 13

        with self.assertRaises(SnapshotValidationError):
            normalize_public_snapshot(payload)

    def test_rejects_missing_required_metadata_fields(self):
        for field in ('audience', 'area', 'time_period', 'features'):
            with self.subTest(field=field):
                payload = make_snapshot()
                del payload['today']['events'][0]['metadata'][field]
                del payload['calendar'][0]['events'][0]['metadata'][field]

                with self.assertRaises(SnapshotValidationError):
                    normalize_public_snapshot(payload)

    def test_rejects_invalid_required_metadata_value_types(self):
        cases = {
            'audience': ['family'],
            'area': True,
            'time_period': 1,
            'features': [],
        }
        for field, invalid in cases.items():
            with self.subTest(field=field):
                payload = make_snapshot()
                payload['today']['events'][0]['metadata'][field] = invalid
                payload['calendar'][0]['events'][0]['metadata'][field] = invalid

                with self.assertRaises(SnapshotValidationError):
                    normalize_public_snapshot(payload)

    def test_rejects_missing_required_feature_flags(self):
        required_keys = (
            'free',
            'outdoor',
            'indoor',
            'dog_friendly',
            'toddler_friendly',
            'stroller_friendly',
            'low_walking',
            'shade',
            'bathrooms',
            'food_nearby',
        )
        for key in required_keys:
            with self.subTest(feature_key=key):
                payload = make_snapshot()
                del payload['today']['events'][0]['metadata']['features'][key]
                del payload['calendar'][0]['events'][0]['metadata']['features'][key]

                with self.assertRaises(SnapshotValidationError):
                    normalize_public_snapshot(payload)

    def test_rejects_non_boolean_feature_flags(self):
        for invalid in ('yes', 1, None):
            with self.subTest(invalid=invalid):
                payload = make_snapshot()
                payload['today']['events'][0]['metadata']['features']['free'] = invalid
                payload['calendar'][0]['events'][0]['metadata']['features']['free'] = invalid

                with self.assertRaises(SnapshotValidationError):
                    normalize_public_snapshot(payload)

    def test_preserves_existing_filter_metadata_extensions(self):
        payload = make_snapshot()

        normalized = normalize_public_snapshot(payload)
        metadata = normalized['today']['events'][0]['metadata']

        self.assertEqual(metadata['age_groups'], ['toddler', 'kids'])
        self.assertEqual(metadata['source_key'], 'city')
        self.assertEqual(metadata['metadata_version'], 2)

    def test_packaged_snapshot_validates_against_public_contract(self):
        packaged_path = Path(__file__).resolve().parents[1] / 'data' / 'public_snapshot.json'
        payload = json.loads(packaged_path.read_text(encoding='utf-8'))

        normalized = normalize_public_snapshot(payload)

        self.assertEqual(normalized['snapshot_id'], payload['snapshot_id'])

    def test_producer_metadata_shape_validates_against_public_contract(self):
        from public_family_events import normalize_event

        event = normalize_event(
            'Toddler Storytime at Balboa Park',
            '2026-07-19',
            'https://example.com/events/storytime',
            'city',
            'Outdoor family storytime with stroller access, restrooms, and shaded seating.',
            'Balboa Park',
            '10:00 AM',
        )
        payload = make_snapshot()
        payload['today']['events'][0] = event.__dict__
        payload['calendar'][0]['events'][0] = event.__dict__.copy()

        normalized = normalize_public_snapshot(payload)

        self.assertEqual(normalized['today']['events'][0]['metadata']['source_key'], 'city')


if __name__ == '__main__':
    unittest.main()
