import re
import unittest
from pathlib import Path

DASHBOARD_HTML = Path(__file__).resolve().parents[1] / 'public_dashboard.html'


class FrontendAnalyticsHooksTests(unittest.TestCase):
    def setUp(self):
        self.html = DASHBOARD_HTML.read_text(encoding='utf-8')

    def test_dashboard_defines_anonymous_analytics_session_helper(self):
        self.assertIn("function getAnalyticsSessionId()", self.html)
        self.assertIn("safeStorageGet(localStorage,'sdfunfinder_analytics_session')", self.html)
        self.assertIn("safeStorageSet(localStorage,'sdfunfinder_analytics_session',sid)", self.html)
        self.assertIn('navigator.sendBeacon', self.html)

    def test_dashboard_tracks_the_key_user_actions(self):
        expected_patterns = [
            r"trackAnalytics\('page_view'",
            r"trackAnalytics\('refresh_clicked'",
            r"trackAnalytics\('filter_changed'",
            r"trackAnalytics\('day_selected'",
            r"trackAnalytics\('save_profile_clicked'",
            r"trackAnalytics\('clear_profile_clicked'",
            r"trackAnalytics\('sources_modal_opened'",
            r"trackAnalytics\('event_detail_clicked'",
            r"trackAnalytics\('no_results_seen'",
        ]
        for pattern in expected_patterns:
            self.assertRegex(self.html, re.compile(pattern))
        self.assertNotIn('destination:', self.html)

    def test_initial_day_render_is_silent_until_user_selects_a_day(self):
        self.assertIn("function selectDay(i,{track=true}={})", self.html)
        self.assertIn("track&&trackAnalytics('day_selected'", self.html)
        self.assertIn("selectDay(selDay,{track:false});", self.html)

    def test_analytics_storage_access_is_guarded(self):
        self.assertIn("function safeStorageGet(storage,key){", self.html)
        self.assertIn("function safeStorageSet(storage,key,value){", self.html)
        self.assertIn("function safeStorageRemove(storage,key){", self.html)
        self.assertIn("safeStorageGet(localStorage,'sdfunfinder_analytics_session')", self.html)
        self.assertIn("safeStorageSet(localStorage,'sdfunfinder_v1'", self.html)
        self.assertIn("safeStorageRemove(localStorage,'sdfunfinder_v1')", self.html)


if __name__ == '__main__':
    unittest.main()
