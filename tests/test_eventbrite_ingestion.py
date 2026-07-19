from __future__ import annotations

import gzip
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

from zoneinfo import ZoneInfo

import public_family_events
from snapshot_schema import normalize_public_snapshot
from tests.snapshot_fixtures import make_snapshot

import eventbrite_ingestion


PT = ZoneInfo('America/Los_Angeles')


class FakeHTTPResponse:
    def __init__(self, payload: bytes, *, encoding: str | None = None, status: int = 200):
        self._payload = payload
        self.status = status
        self.headers = SimpleNamespace(get=lambda key, default=None: encoding if key.lower() == 'content-encoding' else default)

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class EventbriteIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_dir = Path(tempfile.mkdtemp(prefix='eventbrite-ingestion-test-'))
        self.cache_path = self.runtime_dir / 'eventbrite_cache.json'
        self.now = datetime(2026, 7, 19, 8, 0, tzinfo=PT)
        self.env = patch.dict(
            os.environ,
            {
                'BRAVE_SEARCH_API_KEY': 'brave-test-token',
                'EVENTBRITE_PRIVATE_TOKEN': 'eventbrite-test-token',
                'EVENTBRITE_EVENT_CACHE_PATH': str(self.cache_path),
                'EVENTBRITE_CACHE_HOURS': '24',
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()

    def make_search_payload(self, *urls: str) -> dict:
        return {'web': {'results': [{'url': url} for url in urls]}}

    def make_detail_payload(
        self,
        event_id: str,
        *,
        title: str | None = None,
        start_local: str = '2026-07-20T18:30:00',
        venue_city: str = 'San Diego',
        region: str = 'CA',
        online_event: bool = False,
        status: str = 'live',
        listed: bool = True,
        latitude: str = '32.7341',
        longitude: str = '-117.1446',
        venue_name: str = 'Balboa Park Club',
        category: str = 'Community',
        subcategory: str = 'Family',
    ) -> dict:
        return {
            'id': event_id,
            'name': {'text': title or f'Eventbrite Event {event_id}'},
            'summary': 'Outdoor family event with restrooms, shade, and stroller access.',
            'url': f'https://www.eventbrite.com/e/sample-event-{event_id}',
            'status': status,
            'listed': listed,
            'online_event': online_event,
            'start': {'local': start_local, 'timezone': 'America/Los_Angeles', 'utc': '2026-07-21T01:30:00Z'},
            'end': {'local': '2026-07-20T20:00:00', 'timezone': 'America/Los_Angeles', 'utc': '2026-07-21T03:00:00Z'},
            'logo': {'original': {'url': f'https://img.example.com/{event_id}.jpg'}},
            'category': {'name': category},
            'subcategory': {'name': subcategory},
            'organizer': {'name': 'San Diego Organizer'},
            'ticket_availability': {'is_free': True},
            'venue': {
                'name': venue_name,
                'address': {
                    'localized_address_display': f'{venue_name}, {venue_city}, {region}',
                    'city': venue_city,
                    'region': region,
                    'country': 'US',
                    'latitude': latitude,
                    'longitude': longitude,
                },
            },
        }

    def make_urlopen(self, *, search_payloads: list[dict], detail_payloads: dict[str, dict], fail_search: Exception | None = None, fail_detail: dict[str, Exception] | None = None, gzip_search: bool = False):
        calls: list[str] = []
        fail_detail = fail_detail or {}

        def _urlopen(request, timeout=0):
            url = request.full_url
            calls.append(url)
            if 'brave.com/res/v1/web/search' in url:
                if fail_search is not None:
                    raise fail_search
                payload = search_payloads.pop(0)
                encoded = json.dumps(payload).encode('utf-8')
                if gzip_search:
                    encoded = gzip.compress(encoded)
                    return FakeHTTPResponse(encoded, encoding='gzip')
                return FakeHTTPResponse(encoded)
            if '/v3/events/' in url:
                event_id = url.rstrip('/').split('/v3/events/', 1)[1].split('?', 1)[0]
                if event_id in fail_detail:
                    raise fail_detail[event_id]
                return FakeHTTPResponse(json.dumps(detail_payloads[event_id]).encode('utf-8'))
            raise AssertionError(f'unexpected url {url}')

        return calls, _urlopen

    def fetch(self, source: dict | None = None):
        source = source or {'key': 'eventbrite', 'category': 'general_events', 'url': 'https://www.eventbrite.com/d/ca--san-diego/events/', 'parsers': ('eventbrite_api',)}
        return eventbrite_ingestion.fetch_eventbrite_source_result(
            source,
            normalizer=public_family_events.normalize_event,
            source_label=public_family_events.SOURCE_LABELS['eventbrite'],
            now=self.now,
        )

    def test_discovery_uses_exactly_two_bounded_brave_queries_and_dedupes_canonical_ids(self) -> None:
        search_payloads = [
            self.make_search_payload(
                'https://www.eventbrite.com/e/summer-storytime-111',
                'https://www.eventbrite.com/e/summer-storytime-111?aff=oddtdtcreator',
                'https://www.eventbrite.com/e/neighborhood-fair-222',
                'https://www.eventbrite.com/d/ca--san-diego/events/',
            ),
            self.make_search_payload(
                'https://www.eventbrite.com/e/community-festival-333',
                'https://www.google.com/',
                'https://www.eventbrite.com/e/neighborhood-fair-222',
            ),
        ]
        detail_payloads = {
            '111': self.make_detail_payload('111', title='Summer Storytime'),
            '222': self.make_detail_payload('222', title='Neighborhood Fair'),
            '333': self.make_detail_payload('333', title='Community Festival'),
        }
        calls, fake_urlopen = self.make_urlopen(search_payloads=search_payloads, detail_payloads=detail_payloads)

        with patch.object(eventbrite_ingestion, 'urlopen', fake_urlopen):
            events, status = self.fetch()

        self.assertEqual(len(events), 3)
        search_calls = [url for url in calls if 'brave.com/res/v1/web/search' in url]
        detail_calls = [url for url in calls if '/v3/events/' in url]
        self.assertEqual(len(search_calls), 2)
        for url in search_calls:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            self.assertEqual(params['count'], ['20'])
            self.assertIn('July', params['q'][0])
            self.assertIn('August', params['q'][0])
        self.assertEqual([url.split('/v3/events/', 1)[1].split('?', 1)[0] for url in detail_calls], ['111', '222', '333'])
        self.assertEqual(status['status'], 'loaded')
        self.assertEqual(status['count'], 3)

    def test_detail_budget_is_hard_capped_at_24_and_fresh_cache_hits_use_zero_detail_calls(self) -> None:
        os.environ['EVENTBRITE_DETAIL_MAX'] = '99'
        ids = [str(100 + idx) for idx in range(30)]
        cache = {
            'version': 1,
            'events': {
                '100': {'fetched_at': self.now.isoformat(), 'detail': self.make_detail_payload('100', title='Cached 100')},
                '101': {'fetched_at': self.now.isoformat(), 'detail': self.make_detail_payload('101', title='Cached 101')},
            },
        }
        self.cache_path.write_text(json.dumps(cache), encoding='utf-8')
        search_payloads = [self.make_search_payload(*[f'https://www.eventbrite.com/e/event-{event_id}' for event_id in ids[:20]]), self.make_search_payload(*[f'https://www.eventbrite.com/e/event-{event_id}' for event_id in ids[20:]])]
        detail_payloads = {event_id: self.make_detail_payload(event_id, title=f'Event {event_id}') for event_id in ids}
        calls, fake_urlopen = self.make_urlopen(search_payloads=search_payloads, detail_payloads=detail_payloads)

        with patch.object(eventbrite_ingestion, 'urlopen', fake_urlopen):
            events, status = self.fetch()

        detail_ids = [url.split('/v3/events/', 1)[1].split('?', 1)[0] for url in calls if '/v3/events/' in url]
        self.assertEqual(len(detail_ids), 24)
        self.assertNotIn('100', detail_ids)
        self.assertNotIn('101', detail_ids)
        self.assertEqual(len(events), 26)
        self.assertIn('cache_hits=2', status['detail'])

    def test_http_429_halts_further_detail_calls(self) -> None:
        search_payloads = [self.make_search_payload('https://www.eventbrite.com/e/one-111', 'https://www.eventbrite.com/e/two-222'), self.make_search_payload('https://www.eventbrite.com/e/three-333')]
        detail_payloads = {
            '111': self.make_detail_payload('111', title='One'),
            '222': self.make_detail_payload('222', title='Two'),
            '333': self.make_detail_payload('333', title='Three'),
        }
        rate_limited = HTTPError('https://www.eventbrite.com', 429, 'Too Many Requests', hdrs=None, fp=None)
        calls, fake_urlopen = self.make_urlopen(search_payloads=search_payloads, detail_payloads=detail_payloads, fail_detail={'222': rate_limited})

        with patch.object(eventbrite_ingestion, 'urlopen', fake_urlopen):
            events, status = self.fetch()

        detail_ids = [url.split('/v3/events/', 1)[1].split('?', 1)[0] for url in calls if '/v3/events/' in url]
        self.assertEqual(detail_ids, ['111', '222'])
        self.assertEqual(len(events), 1)
        self.assertEqual(status['status'], 'loaded')
        self.assertIn('rate_limited', status['detail'])

    def test_cache_fallback_prunes_outside_window_and_canceled_records(self) -> None:
        valid = self.make_detail_payload('111', title='Cached Future', start_local='2026-07-21T10:00:00')
        stale = self.make_detail_payload('222', title='Past Window', start_local='2026-08-20T10:00:00')
        canceled = self.make_detail_payload('333', title='Canceled', status='canceled')
        self.cache_path.write_text(
            json.dumps(
                {
                    'version': 1,
                    'events': {
                        '111': {'fetched_at': self.now.isoformat(), 'detail': valid},
                        '222': {'fetched_at': self.now.isoformat(), 'detail': stale},
                        '333': {'fetched_at': self.now.isoformat(), 'detail': canceled},
                    },
                }
            ),
            encoding='utf-8',
        )
        calls, fake_urlopen = self.make_urlopen(
            search_payloads=[],
            detail_payloads={},
            fail_search=URLError('search down'),
        )

        with patch.object(eventbrite_ingestion, 'urlopen', fake_urlopen):
            events, status = self.fetch()

        self.assertEqual([event.title for event in events], ['Cached Future'])
        self.assertEqual(status['status'], 'loaded')
        self.assertIn('cache_fallback', status['detail'])
        cache_payload = json.loads(self.cache_path.read_text(encoding='utf-8'))
        self.assertEqual(sorted(cache_payload['events'].keys()), ['111'])

    def test_county_geography_accepts_city_and_rejects_outside_county_or_online_only(self) -> None:
        city = self.make_detail_payload('111', venue_city='San Diego')
        county_city = self.make_detail_payload('222', venue_city='Chula Vista')
        outside = self.make_detail_payload('333', venue_city='Los Angeles', region='CA', latitude='34.0522', longitude='-118.2437')
        online = self.make_detail_payload('444', online_event=True)

        self.assertTrue(eventbrite_ingestion.is_allowed_san_diego_county_event(city))
        self.assertTrue(eventbrite_ingestion.is_allowed_san_diego_county_event(county_city))
        self.assertFalse(eventbrite_ingestion.is_allowed_san_diego_county_event(outside))
        self.assertFalse(eventbrite_ingestion.is_allowed_san_diego_county_event(online))

    def test_normalized_eventbrite_event_validates_against_public_snapshot_schema(self) -> None:
        detail = self.make_detail_payload('555', title='Toddler Storytime on the Lawn', start_local='2026-07-19T10:00:00', category='Books', subcategory='Storytelling')

        event = eventbrite_ingestion.normalize_eventbrite_detail(
            detail,
            normalizer=public_family_events.normalize_event,
            source_label=public_family_events.SOURCE_LABELS['eventbrite'],
            now=self.now,
        )

        self.assertIsNotNone(event)
        payload = make_snapshot()
        payload['sources'] = ['Eventbrite San Diego']
        payload['source_status'][0] = {
            'key': 'eventbrite',
            'label': 'Eventbrite San Diego',
            'category': 'general_events',
            'url': 'https://www.eventbrite.com/d/ca--san-diego/events/',
            'status': 'loaded',
            'count': 1,
            'parser': 'eventbrite_api',
            'detail': 'private diagnostics only',
        }
        payload['today']['events'][0] = event.__dict__
        payload['calendar'][0]['events'][0] = event.__dict__.copy()

        normalized = normalize_public_snapshot(payload)

        metadata = normalized['today']['events'][0]['metadata']
        self.assertEqual(normalized['today']['events'][0]['source'], 'eventbrite')
        self.assertEqual(metadata['source_key'], 'eventbrite')
        self.assertEqual(metadata['metadata_version'], 2)
        self.assertIn('audience', metadata)
        self.assertIn('area', metadata)
        self.assertIn('time_period', metadata)
        for key in ('free', 'outdoor', 'indoor', 'dog_friendly', 'toddler_friendly', 'stroller_friendly', 'low_walking', 'shade', 'bathrooms', 'food_nearby'):
            self.assertIn(key, metadata['features'])
        self.assertNotIn('detail', normalized['source_status'][0])

    def test_eventbrite_fetch_source_result_dispatches_to_api_ingestion(self) -> None:
        source = {'key': 'eventbrite', 'category': 'general_events', 'url': 'https://www.eventbrite.com/d/ca--san-diego/events/', 'parsers': ('eventbrite_api',)}
        expected = ([], {'key': 'eventbrite', 'label': 'Eventbrite San Diego', 'category': 'general_events', 'url': source['url'], 'status': 'no_events', 'count': 0, 'parser': 'eventbrite_api', 'detail': 'private'})

        with patch.object(public_family_events.eventbrite_ingestion, 'fetch_eventbrite_source_result', return_value=expected) as mocked:
            result = public_family_events.fetch_source_result(source)

        self.assertEqual(result, expected)
        mocked.assert_called_once()

    def test_forbidden_write_and_ticketing_paths_are_absent(self) -> None:
        module_text = Path(eventbrite_ingestion.__file__).read_text(encoding='utf-8').lower()
        for forbidden in ('/orders', '/attendees', '/checkout', '/publish', '/create'):
            self.assertNotIn(forbidden, module_text)

    def test_eventbrite_dedupe_keeps_same_title_distinct_venues_but_collapses_cross_posts(self) -> None:
        first = public_family_events.normalize_event('Summer Movie Night', '2026-07-20', 'https://example.com/a', 'eventbrite', 'Outdoor movie night', 'Balboa Park', '6:00 PM')
        second = public_family_events.normalize_event('Summer Movie Night', '2026-07-20', 'https://example.com/b', 'city', 'Outdoor movie night', 'Liberty Station', '6:00 PM')
        duplicate = public_family_events.normalize_event('Summer Movie Night', '2026-07-20', 'https://example.com/c', 'reader', 'Outdoor movie night', 'Balboa Park', '6:00 PM')
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNotNone(duplicate)
        deduped = public_family_events.dedupe([first, second, duplicate])
        self.assertEqual([(event.title, event.venue, event.source) for event in deduped], [('Summer Movie Night', 'Balboa Park', 'eventbrite'), ('Summer Movie Night', 'Liberty Station', 'city')])


if __name__ == '__main__':
    unittest.main()
