import unittest
from pathlib import Path

DASHBOARD_HTML = Path(__file__).resolve().parents[1] / 'analytics_dashboard.html'


class AnalyticsDashboardAuthTests(unittest.TestCase):
    def setUp(self):
        self.html = DASHBOARD_HTML.read_text(encoding='utf-8')

    def test_dashboard_uses_session_storage_and_header_auth_not_query_string_secret(self):
        self.assertIn("safeStorageGet(sessionStorage,'funFinderAnalyticsKey')", self.html)
        self.assertIn("safeStorageSet(sessionStorage,'funFinderAnalyticsKey', trimmed)", self.html)
        self.assertIn("'X-Analytics-Key'", self.html)
        self.assertIn('Enter analytics key', self.html)
        self.assertNotIn("params.get('key')", self.html)

    def test_dashboard_storage_access_is_guarded(self):
        self.assertIn("function safeStorageGet(storage,key){", self.html)
        self.assertIn("function safeStorageSet(storage,key,value){", self.html)
        self.assertIn("function safeStorageRemove(storage,key){", self.html)
        self.assertIn("safeStorageGet(sessionStorage,'funFinderAnalyticsKey')", self.html)


if __name__ == '__main__':
    unittest.main()
