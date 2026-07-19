from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime
from typing import Any
from urllib.parse import urlparse

SCHEMA_VERSION = 1
MAX_DAYS = 21
MAX_EVENTS_PER_DAY = 12
MAX_METADATA_DEPTH = 6
MAX_COLLECTION_ITEMS = 64
ALLOWED_AUDIENCES = ('adult', 'all_ages', 'family', 'young_children')
ALLOWED_AREAS = ('balboa', 'beach', 'central-san-diego', 'downtown', 'east-county', 'north-county', 'south-bay', 'unknown')
ALLOWED_TIME_PERIODS = ('afternoon', 'evening', 'morning', 'unknown')
ALLOWED_AGE_GROUPS = ('adults', 'kids', 'teens', 'toddler')
REQUIRED_FEATURE_KEYS = (
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


class SnapshotValidationError(ValueError):
    pass


ALLOWED_SOURCE_STATUS_KEYS = ('key', 'label', 'category', 'url', 'status', 'count', 'parser')
ALLOWED_EVENT_KEYS = (
    'title',
    'date',
    'url',
    'source',
    'source_label',
    'venue',
    'description',
    'time_text',
    'category',
    'is_free',
    'score',
    'tags',
    'metadata',
)


def normalize_public_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SnapshotValidationError('snapshot must be a JSON object')
    try:
        schema_version = payload.get('schema_version')
        if schema_version != SCHEMA_VERSION:
            raise SnapshotValidationError('unsupported schema_version')

        snapshot_id = _bounded_string(payload.get('snapshot_id'), 'snapshot_id', 128)
        generated_at = _normalize_datetime(payload.get('generated_at'), 'generated_at')
        if payload.get('ok') is not True:
            raise SnapshotValidationError('ok must be true')

        source_status = _normalize_source_status(payload.get('source_status'))
        calendar = _normalize_calendar(payload.get('calendar'))
        today = _normalize_today(payload.get('today'), calendar)
        all_events = [event for day in calendar for event in day['events']]

        normalized = {
            'schema_version': SCHEMA_VERSION,
            'snapshot_id': snapshot_id,
            'generated_at': generated_at,
            'ok': True,
            'sources': _normalize_sources(payload.get('sources'), source_status, all_events),
            'errors': [],
            'source_status': source_status,
            'source_warnings': [],
            'today': today,
            'calendar': calendar,
            'counts': _normalize_counts(all_events, calendar, source_status),
            'top_categories': _normalize_top_categories(payload.get('top_categories'), all_events),
        }
        _ensure_json_safe(normalized)
        return normalized
    except RecursionError as exc:
        raise SnapshotValidationError('snapshot metadata exceeds max depth') from exc


def _normalize_sources(raw: Any, source_status: list[dict[str, Any]], events: list[dict[str, Any]]) -> list[str]:
    if raw is not None and not isinstance(raw, list):
        raise SnapshotValidationError('sources must be a list')
    derived = [item['label'] for item in source_status if item.get('status') == 'loaded' and item.get('count', 0) > 0]
    if not derived:
        derived = [event['source_label'] for event in events if event.get('source_label')]
    if raw:
        provided = [_bounded_string(item, 'sources[]', 120) for item in raw]
        if not derived:
            derived = provided
    return list(dict.fromkeys(derived))


def _normalize_source_status(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SnapshotValidationError('source_status must be a list')
    normalized = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise SnapshotValidationError(f'source_status[{idx}] must be an object')
        entry = {
            'key': _bounded_string(item.get('key'), f'source_status[{idx}].key', 64),
            'label': _bounded_string(item.get('label'), f'source_status[{idx}].label', 120),
            'category': _bounded_string(item.get('category'), f'source_status[{idx}].category', 64),
            'url': _normalize_url(item.get('url'), f'source_status[{idx}].url'),
            'status': _bounded_string(item.get('status'), f'source_status[{idx}].status', 32),
            'count': _normalize_non_negative_int(item.get('count'), f'source_status[{idx}].count'),
        }
        parser = item.get('parser')
        if parser is not None:
            entry['parser'] = _bounded_string(parser, f'source_status[{idx}].parser', 32)
        normalized.append(entry)
    return normalized


def _normalize_today(raw: Any, calendar: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SnapshotValidationError('today must be an object')
    if not calendar:
        raise SnapshotValidationError('calendar must not be empty')
    calendar_today = None
    for day in calendar:
        if day['is_today']:
            if calendar_today is not None:
                raise SnapshotValidationError('calendar must contain only one today entry')
            calendar_today = day
    if calendar_today is None:
        calendar_today = calendar[0]
    if raw.get('date') != calendar_today['date']:
        raise SnapshotValidationError('today.date must match calendar today date')
    return dict(calendar_today)


def _normalize_calendar(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise SnapshotValidationError('calendar must be a list')
    if not raw:
        raise SnapshotValidationError('calendar must not be empty')
    if len(raw) > MAX_DAYS:
        raise SnapshotValidationError('calendar exceeds max days')

    normalized = []
    parsed_dates: list[date] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise SnapshotValidationError(f'calendar[{idx}] must be an object')
        day_date = _normalize_date(item.get('date'), f'calendar[{idx}].date')
        parsed_dates.append(date.fromisoformat(day_date))
        events = _normalize_day_events(item.get('events'), day_date, idx)
        _normalize_non_negative_int(item.get('count'), f'calendar[{idx}].count')
        day = {
            'date': day_date,
            'weekday': _bounded_string(item.get('weekday'), f'calendar[{idx}].weekday', 16),
            'label': _bounded_string(item.get('label'), f'calendar[{idx}].label', 40),
            'is_today': _normalize_bool(item.get('is_today'), f'calendar[{idx}].is_today'),
            'count': len(events),
            'summary': _bounded_string(item.get('summary'), f'calendar[{idx}].summary', 200),
            'events': events,
        }
        normalized.append(day)

    for earlier, later in zip(parsed_dates, parsed_dates[1:]):
        if later <= earlier:
            raise SnapshotValidationError('calendar dates must be strictly increasing')
        if (later - earlier).days != 1:
            raise SnapshotValidationError('calendar dates must be contiguous')
    return normalized


def _normalize_day_events(raw: Any, day_date: str, day_idx: int) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise SnapshotValidationError(f'calendar[{day_idx}].events must be a list')
    if len(raw) > MAX_EVENTS_PER_DAY:
        raise SnapshotValidationError('day exceeds max events')
    normalized = []
    for idx, event in enumerate(raw):
        normalized_event = _normalize_event(event, f'calendar[{day_idx}].events[{idx}]')
        if normalized_event['date'] != day_date:
            raise SnapshotValidationError('event date must match containing day')
        normalized.append(normalized_event)
    return normalized


def _normalize_event(raw: Any, field: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SnapshotValidationError(f'{field} must be an object')
    normalized = {
        'title': _bounded_string(raw.get('title'), f'{field}.title', 200),
        'date': _normalize_date(raw.get('date'), f'{field}.date'),
        'url': _normalize_url(raw.get('url'), f'{field}.url'),
        'source': _bounded_string(raw.get('source'), f'{field}.source', 64),
        'source_label': _bounded_string(raw.get('source_label'), f'{field}.source_label', 120),
        'venue': _bounded_string(raw.get('venue', ''), f'{field}.venue', 200, allow_empty=True),
        'description': _bounded_string(raw.get('description', ''), f'{field}.description', 12000, allow_empty=True),
        'time_text': _bounded_string(raw.get('time_text', ''), f'{field}.time_text', 120, allow_empty=True),
        'category': _bounded_string(raw.get('category', ''), f'{field}.category', 80, allow_empty=True),
        'is_free': _normalize_bool(raw.get('is_free'), f'{field}.is_free'),
        'score': _normalize_int(raw.get('score', 0), f'{field}.score'),
        'tags': _normalize_tags(raw.get('tags', []), f'{field}.tags'),
        'metadata': _normalize_event_metadata(raw.get('metadata', {}), f'{field}.metadata'),
    }
    return normalized


def _normalize_event_metadata(raw: Any, field: str) -> dict[str, Any]:
    metadata = _normalize_json_object(raw, field)
    metadata['audience'] = _normalize_enum(metadata.get('audience'), f'{field}.audience', ALLOWED_AUDIENCES)
    metadata['area'] = _normalize_enum(metadata.get('area'), f'{field}.area', ALLOWED_AREAS)
    metadata['time_period'] = _normalize_enum(metadata.get('time_period'), f'{field}.time_period', ALLOWED_TIME_PERIODS)
    metadata['features'] = _normalize_features(metadata.get('features'), f'{field}.features')

    age_groups = metadata.get('age_groups')
    if age_groups is not None:
        metadata['age_groups'] = _normalize_age_groups(age_groups, f'{field}.age_groups')

    source_key = metadata.get('source_key')
    if source_key is not None:
        metadata['source_key'] = _bounded_string(source_key, f'{field}.source_key', 64)

    metadata_version = metadata.get('metadata_version')
    if metadata_version is not None:
        metadata['metadata_version'] = _normalize_non_negative_int(metadata_version, f'{field}.metadata_version')
    return metadata


def _normalize_features(raw: Any, field: str) -> dict[str, bool]:
    if not isinstance(raw, dict):
        raise SnapshotValidationError(f'{field} must be an object')
    normalized = dict(raw)
    for key in REQUIRED_FEATURE_KEYS:
        normalized[key] = _normalize_bool(normalized.get(key), f'{field}.{key}')
    return normalized


def _normalize_age_groups(raw: Any, field: str) -> list[str]:
    if not isinstance(raw, list):
        raise SnapshotValidationError(f'{field} must be a list')
    return [_normalize_enum(item, f'{field}[]', ALLOWED_AGE_GROUPS) for item in raw]


def _normalize_top_categories(raw: Any, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if raw is not None and not isinstance(raw, list):
        raise SnapshotValidationError('top_categories must be a list')
    counts = Counter(event['category'] for event in events if event.get('category'))
    return [{'name': name, 'count': count} for name, count in counts.most_common(8)]


def _normalize_counts(events: list[dict[str, Any]], calendar: list[dict[str, Any]], source_status: list[dict[str, Any]]) -> dict[str, int]:
    return {
        'total_events': len(events),
        'days': len(calendar),
        'free_events': sum(1 for event in events if event.get('is_free')),
        'configured_sources': len(source_status),
        'loaded_sources': sum(1 for status in source_status if status.get('status') == 'loaded' and status.get('count', 0) > 0),
    }


def _normalize_tags(raw: Any, field: str) -> list[str]:
    if not isinstance(raw, list):
        raise SnapshotValidationError(f'{field} must be a list')
    return [_bounded_string(item, f'{field}[]', 40) for item in raw]


def _normalize_json_object(raw: Any, field: str, *, depth: int = 0) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SnapshotValidationError(f'{field} must be an object')
    if depth > MAX_METADATA_DEPTH:
        raise SnapshotValidationError(f'{field} exceeds max depth')
    if len(raw) > MAX_COLLECTION_ITEMS:
        raise SnapshotValidationError(f'{field} exceeds max items')
    normalized: dict[str, Any] = {}
    for key, value in raw.items():
        normalized[_bounded_string(key, f'{field}.key', 80)] = _normalize_json_value(value, f'{field}.{key}', depth=depth + 1)
    return normalized


def _normalize_json_value(value: Any, field: str, *, depth: int = 0) -> Any:
    if depth > MAX_METADATA_DEPTH:
        raise SnapshotValidationError(f'{field} exceeds max depth')
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _bounded_string(value, field, 400)
    if isinstance(value, list):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise SnapshotValidationError(f'{field} exceeds max items')
        return [_normalize_json_value(item, f'{field}[]', depth=depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise SnapshotValidationError(f'{field} exceeds max items')
        return {
            _bounded_string(str(key), f'{field}.key', 80): _normalize_json_value(val, f'{field}.{key}', depth=depth + 1)
            for key, val in value.items()
        }
    raise SnapshotValidationError(f'{field} must be JSON-compatible')


def _normalize_url(value: Any, field: str) -> str:
    text = _bounded_string(value, field, 2048)
    parsed = urlparse(text)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        raise SnapshotValidationError(f'{field} must be http/https URL')
    return text


def _normalize_datetime(value: Any, field: str) -> str:
    text = _bounded_string(value, field, 64)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SnapshotValidationError(f'{field} must be ISO-8601 datetime') from exc
    if parsed.utcoffset() is None:
        raise SnapshotValidationError(f'{field} must include timezone offset')
    return text


def _normalize_date(value: Any, field: str) -> str:
    text = _bounded_string(value, field, 10)
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise SnapshotValidationError(f'{field} must be ISO date') from exc
    return text


def _normalize_non_negative_int(value: Any, field: str) -> int:
    normalized = _normalize_int(value, field)
    if normalized < 0:
        raise SnapshotValidationError(f'{field} must be non-negative')
    return normalized


def _normalize_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SnapshotValidationError(f'{field} must be an integer')
    return value


def _normalize_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise SnapshotValidationError(f'{field} must be a boolean')
    return value


def _normalize_enum(value: Any, field: str, allowed: tuple[str, ...]) -> str:
    text = _bounded_string(value, field, 64)
    if text not in allowed:
        raise SnapshotValidationError(f'{field} must be one of: {", ".join(allowed)}')
    return text


def _bounded_string(value: Any, field: str, max_length: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise SnapshotValidationError(f'{field} must be a string')
    text = value.strip()
    if not text and not allow_empty:
        raise SnapshotValidationError(f'{field} must not be empty')
    if len(text) > max_length:
        raise SnapshotValidationError(f'{field} exceeds max length')
    return text


def _ensure_json_safe(payload: dict[str, Any]) -> None:
    try:
        json.dumps(payload, separators=(',', ':'))
    except (TypeError, ValueError) as exc:
        raise SnapshotValidationError('snapshot must be JSON-serializable') from exc
