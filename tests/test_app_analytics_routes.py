import importlib
import os
import tempfile
import unittest
from pathlib import Path

import app as app_module


class AppAnalyticsRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / 'route-analytics.sqlite3'
        os.environ['FUN_FINDER_ANALYTICS_DB_PATH'] = str(self.db_path)
        os.environ['FUN_FINDER_ANALYTICS_KEY'] = 'dev-analytics-key'
        importlib.reload(app_module)
        app_module.app.config['TESTING'] = True
        self.client = app_module.app.test_client()

    def tearDown(self):
        os.environ.pop('FUN_FINDER_ANALYTICS_DB_PATH', None)
        os.environ.pop('FUN_FINDER_ANALYTICS_KEY', None)
        self.tmp.cleanup()

    def test_post_api_analytics_accepts_valid_event(self):
        resp = self.client.post(
            '/api/analytics',
            json={
                'session_id': 'sess-1',
                'event_name': 'page_view',
                'path': '/',
                'referrer': 'direct',
                'metadata': {'screen': 'home'},
            },
        )

        self.assertEqual(resp.status_code, 202)
        self.assertTrue(resp.get_json()['ok'])

    def test_post_api_analytics_rejects_email_like_session_id(self):
        resp = self.client.post(
            '/api/analytics',
            json={
                'session_id': 'alice@example.com',
                'event_name': 'page_view',
                'path': '/',
                'referrer': 'direct',
                'metadata': {'screen': 'home'},
            },
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()['error'], 'invalid session_id')

    def test_summary_requires_matching_key(self):
        resp = self.client.get('/api/analytics/summary')
        self.assertEqual(resp.status_code, 403)

    def test_summary_rejects_query_string_key_transport(self):
        resp = self.client.get('/api/analytics/summary?key=dev-analytics-key')
        self.assertEqual(resp.status_code, 403)

    def test_summary_with_matching_header_key_returns_data(self):
        self.client.post(
            '/api/analytics',
            json={
                'session_id': 'sess-1',
                'event_name': 'page_view',
                'path': '/',
                'referrer': 'https://google.com/search?q=summer+events',
                'metadata': {'screen': 'home'},
            },
        )
        resp = self.client.get('/api/analytics/summary?days=7', headers={'X-Analytics-Key': 'dev-analytics-key'})

        data = resp.get_json()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(data['ok'])
        self.assertEqual(data['totals']['page_views'], 1)
        self.assertEqual(data['top_referrers'][0]['referrer'], 'https://google.com')

    def test_private_analytics_dashboard_serves_shell_without_secret_in_url(self):
        resp = self.client.get('/analytics')

        self.assertEqual(resp.status_code, 200)
        page = resp.get_data(as_text=True)
        self.assertIn('Fun Finder Analytics', page)
        self.assertIn('Enter analytics key', page)


if __name__ == '__main__':
    unittest.main()
