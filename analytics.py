from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ALLOWED_EVENTS = {
    'page_view',
    'refresh_clicked',
    'filter_changed',
    'day_selected',
    'save_profile_clicked',
    'clear_profile_clicked',
    'sources_modal_opened',
    'event_detail_clicked',
    'no_results_seen',
}

ALLOWED_METADATA_FIELDS = {
    'page_view': {'timing', 'area', 'sources', 'total_events'},
    'refresh_clicked': {'timing', 'area'},
    'filter_changed': {'filter_key', 'filter_value', 'active', 'change'},
    'day_selected': {'date', 'weekday', 'count'},
    'save_profile_clicked': {'audience', 'price', 'setting', 'feature', 'timing', 'area', 'kids', 'dogs', 'seniors'},
    'clear_profile_clicked': {'had_saved_profile'},
    'sources_modal_opened': {'loaded_sources'},
    'event_detail_clicked': {'timing', 'area'},
    'no_results_seen': {'reason', 'audience', 'price', 'setting', 'feature', 'timing', 'area', 'kids', 'dogs', 'seniors'},
}

MAX_TEXT = 240
EMAIL_RE = re.compile(r'(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b')
TOKEN_RE = re.compile(r'\b(?=[A-Za-z0-9_-]{20,}\b)(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9_-]+\b')
SESSION_ID_RE = re.compile(r'^(?:sdfun|sess)-[A-Za-z0-9_-]{1,120}$|^[0-9a-f]{32}$|^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE)


class AnalyticsStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS analytics_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    event_name TEXT NOT NULL,
                    path TEXT NOT NULL,
                    referrer TEXT NOT NULL DEFAULT '',
                    source_label TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                )
                '''
            )
            conn.execute('CREATE INDEX IF NOT EXISTS idx_analytics_created_at ON analytics_events(created_at)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_analytics_event_name ON analytics_events(event_name)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_analytics_session_id ON analytics_events(session_id)')
            conn.commit()

    def track(self, payload: dict[str, Any], now: str | datetime | None = None) -> dict[str, Any]:
        created_at = _normalize_timestamp(now)
        row = _normalize_payload(payload)
        if row['event_name'] not in ALLOWED_EVENTS:
            raise ValueError(f"unsupported analytics event: {row['event_name']}")
        with closing(self._connect()) as conn:
            cur = conn.execute(
                '''
                INSERT INTO analytics_events (
                    created_at, session_id, event_name, path, referrer, source_label, title, category, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    created_at,
                    row['session_id'],
                    row['event_name'],
                    row['path'],
                    row['referrer'],
                    row['source_label'],
                    row['title'],
                    row['category'],
                    json.dumps(row['metadata'], separators=(',', ':')),
                ),
            )
            lastrowid = cur.lastrowid
            conn.commit()
        return {'ok': True, 'id': lastrowid, 'created_at': created_at, 'event_name': row['event_name']}

    def summary(self, days: int = 30, now: str | datetime | None = None) -> dict[str, Any]:
        window_days = max(1, int(days or 30))
        end_dt = _as_datetime(now)
        start_dt = datetime(end_dt.year, end_dt.month, end_dt.day) - timedelta(days=window_days - 1)
        start_iso = start_dt.isoformat(timespec='seconds')
        end_iso = end_dt.isoformat(timespec='seconds')
        with closing(self._connect()) as conn:
            rows = [dict(row) for row in conn.execute(
                '''
                SELECT created_at, session_id, event_name, path, referrer, source_label, title, category, metadata_json
                FROM analytics_events
                WHERE created_at >= ? AND created_at <= ?
                ORDER BY created_at ASC, id ASC
                ''',
                (start_iso, end_iso),
            )]

        parsed_rows: list[dict[str, Any]] = []
        for row in rows:
            row['metadata'] = _safe_json_loads(row.pop('metadata_json', '{}'))
            row['date'] = str(row['created_at'])[:10]
            parsed_rows.append(row)

        page_views = [row for row in parsed_rows if row['event_name'] == 'page_view']
        outbound_clicks = [row for row in parsed_rows if row['event_name'] == 'event_detail_clicked']
        unique_sessions = {row['session_id'] for row in parsed_rows}

        top_referrers = Counter(
            row['referrer'] for row in page_views if row.get('referrer')
        )
        top_sources = Counter(
            row['source_label'] for row in outbound_clicks if row.get('source_label')
        )
        top_titles = Counter(
            row['title'] for row in outbound_clicks if row.get('title')
        )
        top_events = Counter(row['event_name'] for row in parsed_rows)

        filter_counts: Counter[tuple[str, str]] = Counter()
        for row in parsed_rows:
            if row['event_name'] != 'filter_changed':
                continue
            metadata = row.get('metadata') or {}
            key = _clean_text(metadata.get('filter_key'))
            value = _clean_text(metadata.get('filter_value'))
            if key and value:
                filter_counts[(key, value)] += 1

        daily_events: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                'date': '',
                'page_views': 0,
                'outbound_clicks': 0,
                'unique_sessions': set(),
            }
        )
        for row in parsed_rows:
            bucket = daily_events[row['date']]
            bucket['date'] = row['date']
            bucket['unique_sessions'].add(row['session_id'])
            if row['event_name'] == 'page_view':
                bucket['page_views'] += 1
            if row['event_name'] == 'event_detail_clicked':
                bucket['outbound_clicks'] += 1

        daily = []
        for date in sorted(daily_events):
            bucket = daily_events[date]
            daily.append(
                {
                    'date': date,
                    'page_views': bucket['page_views'],
                    'outbound_clicks': bucket['outbound_clicks'],
                    'unique_sessions': len(bucket['unique_sessions']),
                }
            )

        return {
            'ok': True,
            'window_days': window_days,
            'generated_at': end_iso,
            'totals': {
                'events': len(parsed_rows),
                'page_views': len(page_views),
                'unique_sessions': len(unique_sessions),
                'outbound_clicks': len(outbound_clicks),
            },
            'top_events': _counter_rows(top_events, 'name'),
            'top_referrers': _counter_rows(top_referrers, 'referrer'),
            'top_sources': _counter_rows(top_sources, 'source_label'),
            'top_titles': _counter_rows(top_titles, 'title'),
            'top_filters': [
                {'filter': key, 'value': value, 'count': count}
                for (key, value), count in sorted(filter_counts.items(), key=lambda item: (-item[1], item[0][0], item[0][1]))[:10]
            ],
            'daily': daily,
        }


def _counter_rows(counter: Counter[str], label: str) -> list[dict[str, Any]]:
    rows = []
    for value, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:10]:
        rows.append({label: value, 'count': count})
    return rows


def _normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    session_id = _sanitize_session_id(payload.get('session_id'))
    event_name = _clean_text(payload.get('event_name'))
    path = _sanitize_path(payload.get('path') or '/')
    if not session_id:
        raise ValueError('session_id is required')
    if not event_name:
        raise ValueError('event_name is required')
    raw_metadata = payload.get('metadata')
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    allow_text_fields = event_name == 'event_detail_clicked'
    return {
        'session_id': session_id,
        'event_name': event_name,
        'path': path,
        'referrer': _sanitize_referrer(payload.get('referrer') or ''),
        'source_label': _sanitize_text_payload(payload.get('source_label') or '') if allow_text_fields else '',
        'title': _sanitize_text_payload(payload.get('title') or '') if allow_text_fields else '',
        'category': _sanitize_text_payload(payload.get('category') or '') if allow_text_fields else '',
        'metadata': _allowlisted_metadata(event_name, metadata),
    }


def _sanitize_session_id(value: Any) -> str:
    session_id = _clean_text(value)
    if not session_id:
        return ''
    if not SESSION_ID_RE.fullmatch(session_id):
        raise ValueError('invalid session_id')
    return session_id


def _allowlisted_metadata(event_name: str, metadata: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = ALLOWED_METADATA_FIELDS.get(event_name, set())
    cleaned: dict[str, Any] = {}
    for key, value in metadata.items():
        key_text = str(key)[:64]
        if key_text not in allowed_keys:
            continue
        cleaned[key_text] = _clean_metadata_value(value)
    return cleaned


def _clean_metadata_value(value: Any) -> Any:
    if value is None:
        return ''
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, list):
        return [_clean_metadata_value(v) for v in value[:12]]
    if isinstance(value, dict):
        return {str(k)[:64]: _clean_metadata_value(v) for k, v in list(value.items())[:20]}
    return _sanitize_text_payload(value)


def _clean_text(value: Any) -> str:
    text = str(value or '').strip()
    return text[:MAX_TEXT]


def _sanitize_text_payload(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return ''
    text = EMAIL_RE.sub('[redacted-email]', text)
    text = TOKEN_RE.sub('[redacted-token]', text)
    return text[:MAX_TEXT]


def _sanitize_referrer(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return ''
    if text == 'direct':
        return 'direct'
    parsed = urlsplit(text)
    if parsed.scheme and parsed.netloc:
        return f'{parsed.scheme}://{parsed.netloc}'[:MAX_TEXT]
    return ''


def _sanitize_path(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return '/'
    parsed = urlsplit(text)
    path = parsed.path or '/'
    if not path.startswith('/'):
        path = '/' + path.lstrip('/')
    return path[:MAX_TEXT]


def _as_datetime(value: str | datetime | None) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value)
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _normalize_timestamp(value: str | datetime | None) -> str:
    return _as_datetime(value).isoformat(timespec='seconds')


def _safe_json_loads(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or '{}')
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}
