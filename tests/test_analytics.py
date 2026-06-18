import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from analytics import AnalyticsStore


class AnalyticsStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / 'analytics.sqlite3'
        self.store = AnalyticsStore(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_track_persists_valid_page_view_event(self):
        result = self.store.track(
            {
                'session_id': 'sess-1',
                'event_name': 'page_view',
                'path': '/',
                'referrer': 'https://example.com/',
                'metadata': {'screen': 'home'},
            },
            now='2026-06-18T10:00:00',
        )

        summary = self.store.summary(days=30, now='2026-06-18T12:00:00')

        self.assertTrue(result['ok'])
        self.assertEqual(summary['totals']['page_views'], 1)
        self.assertEqual(summary['totals']['unique_sessions'], 1)
        self.assertEqual(summary['top_referrers'][0]['referrer'], 'https://example.com')

    def test_summary_aggregates_filters_sources_clicks_and_daily_trend(self):
        rows = [
            {
                'session_id': 'sess-1',
                'event_name': 'page_view',
                'path': '/',
                'referrer': 'https://google.com/',
                'metadata': {},
            },
            {
                'session_id': 'sess-1',
                'event_name': 'filter_changed',
                'path': '/',
                'metadata': {'filter_key': 'audience', 'filter_value': 'family'},
            },
            {
                'session_id': 'sess-1',
                'event_name': 'event_detail_clicked',
                'path': '/',
                'source_label': 'KPBS',
                'title': 'Storytime by the Bay',
                'category': 'Toddler-friendly',
                'metadata': {'url': 'https://example.com/event-1'},
            },
            {
                'session_id': 'sess-2',
                'event_name': 'page_view',
                'path': '/',
                'referrer': 'direct',
                'metadata': {},
            },
            {
                'session_id': 'sess-2',
                'event_name': 'filter_changed',
                'path': '/',
                'metadata': {'filter_key': 'audience', 'filter_value': 'family'},
            },
            {
                'session_id': 'sess-2',
                'event_name': 'filter_changed',
                'path': '/',
                'metadata': {'filter_key': 'timing', 'filter_value': 'weekend'},
            },
        ]
        timestamps = [
            '2026-06-17T09:00:00',
            '2026-06-17T09:01:00',
            '2026-06-17T09:02:00',
            '2026-06-18T11:00:00',
            '2026-06-18T11:01:00',
            '2026-06-18T11:02:00',
        ]

        for row, ts in zip(rows, timestamps, strict=True):
            self.store.track(row, now=ts)

        summary = self.store.summary(days=30, now='2026-06-18T12:00:00')

        self.assertEqual(summary['totals']['page_views'], 2)
        self.assertEqual(summary['totals']['unique_sessions'], 2)
        self.assertEqual(summary['totals']['outbound_clicks'], 1)
        self.assertEqual(summary['top_filters'][0], {'filter': 'audience', 'value': 'family', 'count': 2})
        self.assertEqual(summary['top_sources'][0], {'source_label': 'KPBS', 'count': 1})
        self.assertEqual(summary['top_titles'][0]['title'], 'Storytime by the Bay')
        self.assertEqual([d['date'] for d in summary['daily']], ['2026-06-17', '2026-06-18'])
        self.assertEqual(summary['daily'][0]['page_views'], 1)
        self.assertEqual(summary['daily'][1]['unique_sessions'], 1)

    def test_invalid_event_name_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.track(
                {
                    'session_id': 'sess-1',
                    'event_name': 'totally_fake',
                    'path': '/',
                    'metadata': {},
                },
                now='2026-06-18T10:00:00',
            )

    def test_email_like_session_id_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.track(
                {
                    'session_id': 'alice@example.com',
                    'event_name': 'page_view',
                    'path': '/',
                    'referrer': 'direct',
                    'metadata': {},
                },
                now='2026-06-18T10:01:00',
            )

    def test_unique_sessions_are_counted_consistently_across_totals_and_daily(self):
        self.store.track(
            {
                'session_id': 'sess-1',
                'event_name': 'page_view',
                'path': '/',
                'referrer': 'direct',
                'metadata': {},
            },
            now='2026-06-18T09:00:00',
        )
        self.store.track(
            {
                'session_id': 'sess-2',
                'event_name': 'filter_changed',
                'path': '/',
                'metadata': {'filter_key': 'price', 'filter_value': 'free'},
            },
            now='2026-06-18T09:05:00',
        )

        summary = self.store.summary(days=30, now='2026-06-18T12:00:00')

        self.assertEqual(summary['totals']['unique_sessions'], 2)
        self.assertEqual(summary['daily'][0]['unique_sessions'], 2)

    def test_track_sanitizes_query_strings_from_stored_path(self):
        self.store.track(
            {
                'session_id': 'sess-3',
                'event_name': 'page_view',
                'path': '/analytics?key=secret-token&email=test@example.com#top',
                'referrer': 'direct',
                'metadata': {},
            },
            now='2026-06-18T10:15:00',
        )

        with closing(sqlite3.connect(self.db_path)) as conn:
            stored_path = conn.execute('SELECT path FROM analytics_events ORDER BY id DESC LIMIT 1').fetchone()[0]

        self.assertEqual(stored_path, '/analytics')

    def test_metadata_is_allowlisted_and_non_url_referrers_are_dropped(self):
        self.store.track(
            {
                'session_id': 'sess-4',
                'event_name': 'filter_changed',
                'path': '/',
                'referrer': 'email me at test@example.com',
                'metadata': {
                    'filter_key': 'price',
                    'filter_value': 'free',
                    'email': 'test@example.com',
                    'notes': 'secret-token-123456',
                },
            },
            now='2026-06-18T10:20:00',
        )

        with closing(sqlite3.connect(self.db_path)) as conn:
            referrer, metadata_json = conn.execute(
                'SELECT referrer, metadata_json FROM analytics_events ORDER BY id DESC LIMIT 1'
            ).fetchone()

        self.assertEqual(referrer, '')
        self.assertEqual(metadata_json, '{"filter_key":"price","filter_value":"free"}')

    def test_event_detail_click_drops_destination_from_server_side_metadata(self):
        self.store.track(
            {
                'session_id': 'sess-5',
                'event_name': 'event_detail_clicked',
                'path': '/',
                'source_label': 'KPBS',
                'title': 'Storytime by the Bay',
                'category': 'Family',
                'metadata': {
                    'destination': 'https://events.example.com/private/path?token=abc123&email=alice@example.com',
                    'timing': 'weekend',
                    'area': 'north-county',
                },
            },
            now='2026-06-18T10:30:00',
        )

        with closing(sqlite3.connect(self.db_path)) as conn:
            metadata_json = conn.execute(
                'SELECT metadata_json FROM analytics_events ORDER BY id DESC LIMIT 1'
            ).fetchone()[0]

        self.assertEqual(metadata_json, '{"timing":"weekend","area":"north-county"}')


if __name__ == '__main__':
    unittest.main()
