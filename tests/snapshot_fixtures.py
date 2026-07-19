from __future__ import annotations

from copy import deepcopy


def make_snapshot(*, generated_at: str = '2026-07-19T08:01:58-07:00', snapshot_id: str = 'snap-001') -> dict:
    payload = {
        'schema_version': 1,
        'snapshot_id': snapshot_id,
        'generated_at': generated_at,
        'ok': True,
        'sources': ['City of San Diego'],
        'errors': None,
        'source_status': [
            {
                'key': 'city',
                'label': 'City of San Diego',
                'category': 'family_events',
                'url': 'https://www.sandiego.gov/events',
                'status': 'loaded',
                'count': 1,
                'parser': 'city',
                'detail': 'secret detail that must never leak',
                'error': 'token=super-secret',
            }
        ],
        'source_warnings': None,
        'today': {
            'date': '2026-07-19',
            'weekday': 'Sat',
            'label': 'Jul 19',
            'is_today': True,
            'count': 1,
            'summary': 'One fun event today.',
            'events': [
                {
                    'title': 'Storytime in the Park',
                    'date': '2026-07-19',
                    'url': 'https://example.com/events/storytime',
                    'source': 'city',
                    'source_label': 'City of San Diego',
                    'venue': 'Balboa Park',
                    'description': 'Outdoor storytime for families.',
                    'time_text': '10:00 AM',
                    'category': 'Toddler-friendly',
                    'is_free': True,
                    'score': 7,
                    'tags': ['storytime', 'free'],
                    'metadata': {
                        'audience': 'young_children',
                        'age_groups': ['toddler', 'kids'],
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
                'summary': 'One fun event today.',
                'events': [],
            },
            {
                'date': '2026-07-20',
                'weekday': 'Sun',
                'label': 'Jul 20',
                'is_today': False,
                'count': 0,
                'summary': 'No events yet.',
                'events': [],
            },
        ],
        'counts': {
            'total_events': 1,
            'days': 2,
            'free_events': 1,
            'configured_sources': 1,
            'loaded_sources': 1,
        },
        'top_categories': [{'name': 'Toddler-friendly', 'count': 1}],
    }
    payload['calendar'][0]['events'] = deepcopy(payload['today']['events'])
    return payload
