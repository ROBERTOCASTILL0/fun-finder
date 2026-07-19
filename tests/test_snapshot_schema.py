from __future__ import annotations

import unittest

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


if __name__ == '__main__':
    unittest.main()
