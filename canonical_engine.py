#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from snapshot_schema import SnapshotValidationError, normalize_public_snapshot

from public_family_events import (
    DAY_WINDOW,
    MAX_SOURCE_WORKERS,
    PT,
    SOURCE_LABELS,
    Event,
    NOW,
    build_payload,
    build_source_definitions,
    dedupe,
    fetch_source_result,
)

CORE_SOURCE_KEYS = ('city', 'family', 'kids', 'kpbs')
DEFAULT_SOURCE_KEYS = CORE_SOURCE_KEYS + ('reader', 'meetup_general', 'ucsd', 'sdhumane', 'meetup_dogs')
DEFAULT_DATA_DIR = Path('/opt/data/roberto-ui/data')
DEFAULT_CANDIDATE_FULL = 'fun_finder_candidate_full.json'
DEFAULT_LKG_FULL = 'fun_finder_last_known_good_full.json'
DEFAULT_PRIVATE_SNAPSHOT = 'fun_finder_private_snapshot.json'
DEFAULT_PUBLIC_SNAPSHOT = 'fun_finder_public_snapshot.json'
DEFAULT_PUBLISH_STATE = 'fun_finder_publish_state.json'
DEFAULT_LOCK_NAME = '.fun_finder_canonical.lock'


def now_pt() -> datetime:
    return NOW()


def today_pt() -> date:
    return now_pt().date()


def build_canonical_source_definitions(source_keys: tuple[str, ...] | list[str] | None = None) -> list[dict[str, Any]]:
    allowed = tuple(source_keys or DEFAULT_SOURCE_KEYS)
    all_sources = {source['key']: source for source in build_source_definitions()}
    return [dict(all_sources[key]) for key in allowed if key in all_sources]


class CanonicalEngine:
    def __init__(
        self,
        *,
        data_dir: str | Path | None = None,
        candidate_full_path: str | Path | None = None,
        lkg_full_path: str | Path | None = None,
        private_snapshot_path: str | Path | None = None,
        public_snapshot_path: str | Path | None = None,
        publish_state_path: str | Path | None = None,
        lock_path: str | Path | None = None,
        source_keys: tuple[str, ...] | list[str] | None = None,
        publisher_factory: Callable[..., Any] | None = None,
    ) -> None:
        env_data_dir = os.environ.get('FUN_FINDER_DATA_DIR')
        self.data_dir = Path(data_dir or env_data_dir or DEFAULT_DATA_DIR)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.candidate_full_path = self._resolve_path(candidate_full_path, 'FUN_FINDER_CANDIDATE_FULL_PATH', DEFAULT_CANDIDATE_FULL)
        self.lkg_full_path = self._resolve_path(lkg_full_path, 'FUN_FINDER_LKG_FULL_PATH', DEFAULT_LKG_FULL)
        self.private_snapshot_path = self._resolve_path(private_snapshot_path, 'FUN_FINDER_PRIVATE_SNAPSHOT_PATH', DEFAULT_PRIVATE_SNAPSHOT)
        self.public_snapshot_path = self._resolve_path(public_snapshot_path, 'FUN_FINDER_PUBLIC_SNAPSHOT_PATH', DEFAULT_PUBLIC_SNAPSHOT)
        self.publish_state_path = self._resolve_path(publish_state_path, 'FUN_FINDER_PUBLISH_STATE_PATH', DEFAULT_PUBLISH_STATE)
        self.lock_path = Path(lock_path or os.environ.get('FUN_FINDER_LOCK_PATH') or (self.data_dir / DEFAULT_LOCK_NAME))
        self.source_keys = tuple(source_keys or _env_csv('FUN_FINDER_SOURCE_KEYS') or DEFAULT_SOURCE_KEYS)
        self.publisher_factory = publisher_factory

    def _resolve_path(self, explicit: str | Path | None, env_name: str, default_name: str) -> Path:
        return Path(explicit or os.environ.get(env_name) or (self.data_dir / default_name))

    def refresh(self, *, publish: bool = False) -> dict[str, Any]:
        with self._locked():
            refresh_result = self._refresh_locked()
            if publish and refresh_result.get('ok'):
                from snapshot_publisher import SnapshotPublisher

                publisher_factory = self.publisher_factory or SnapshotPublisher
                publisher = publisher_factory(
                    public_snapshot_path=self.public_snapshot_path,
                    publish_state_path=self.publish_state_path,
                )
                publish_result = publisher.publish()
                refresh_result['publish'] = publish_result
            return refresh_result

    def _refresh_locked(self) -> dict[str, Any]:
        source_defs = build_canonical_source_definitions(self.source_keys)
        fetched_at = now_pt().isoformat(timespec='seconds')
        results_by_key: dict[str, tuple[list[Event], dict[str, Any]]] = {}
        warnings: list[str] = []
        with ThreadPoolExecutor(max_workers=max(1, min(MAX_SOURCE_WORKERS, len(source_defs)))) as pool:
            future_map = {pool.submit(fetch_source_result, source): source for source in source_defs}
            for future in as_completed(future_map):
                source = future_map[future]
                try:
                    results_by_key[source['key']] = future.result()
                except Exception as exc:
                    results_by_key[source['key']] = (
                        [],
                        {
                            'key': source['key'],
                            'label': SOURCE_LABELS[source['key']],
                            'category': source['category'],
                            'url': source['url'],
                            'status': 'unavailable',
                            'count': 0,
                            'detail': f'{type(exc).__name__}: {exc}'[:160],
                        },
                    )

        events: list[Event] = []
        source_status: list[dict[str, Any]] = []
        for source in source_defs:
            parsed, status = results_by_key.get(source['key'], ([], {
                'key': source['key'],
                'label': SOURCE_LABELS[source['key']],
                'category': source['category'],
                'url': source['url'],
                'status': 'unavailable',
                'count': 0,
                'detail': 'source result missing',
            }))
            events.extend(parsed)
            source_status.append(dict(status))
        events = dedupe(events)
        public_snapshot = build_payload(events, [], fetched_at, source_status)
        validation = self._validate_candidate(public_snapshot)
        candidate_full = self._build_candidate_full(source_status, warnings, events, public_snapshot, validation)
        self._atomic_write_json(self.candidate_full_path, candidate_full)

        publish_state = self._load_json(self.publish_state_path) or {}
        publish_state['refresh'] = self._refresh_state(candidate_full)
        publish_state['public_snapshot'] = {
            'snapshot_id': public_snapshot.get('snapshot_id'),
            'generated_at': public_snapshot.get('generated_at'),
        }

        if validation['passed']:
            private_snapshot = self._build_private_snapshot(candidate_full)
            self._atomic_write_json(self.lkg_full_path, candidate_full)
            self._atomic_write_json(self.private_snapshot_path, private_snapshot)
            self._atomic_write_json(self.public_snapshot_path, public_snapshot)
            self._atomic_write_json(self.publish_state_path, publish_state)
            return {
                'ok': True,
                'snapshot_id': public_snapshot['snapshot_id'],
                'generated_at': public_snapshot['generated_at'],
                'loaded_sources': validation['loaded_sources'],
                'loaded_core_sources': validation['loaded_core_sources'],
                'visible_occurrences': validation['visible_occurrences'],
                'today_occurrences': validation['today_occurrences'],
                'artifact_paths': self._artifact_paths(),
            }

        publish_state['refresh']['status'] = 'validation_failed'
        self._atomic_write_json(self.publish_state_path, publish_state)
        return {
            'ok': False,
            'snapshot_id': public_snapshot.get('snapshot_id'),
            'generated_at': public_snapshot.get('generated_at'),
            'validation': validation,
            'artifact_paths': self._artifact_paths(),
        }

    def _validate_candidate(self, public_snapshot: dict[str, Any]) -> dict[str, Any]:
        messages: list[str] = []
        passed = True
        try:
            normalized = normalize_public_snapshot(public_snapshot)
        except SnapshotValidationError as exc:
            normalized = None
            passed = False
            messages.append(f'schema validation failed: {exc}')

        calendar = list((normalized or public_snapshot).get('calendar') or [])
        expected_today = today_pt().isoformat()
        if len(calendar) != DAY_WINDOW:
            passed = False
            messages.append(f'calendar must contain {DAY_WINDOW} days')
        parsed_dates: list[date] = []
        for item in calendar:
            try:
                parsed_dates.append(date.fromisoformat(item['date']))
            except Exception:
                passed = False
                messages.append('calendar contains invalid date values')
                parsed_dates = []
                break
        if parsed_dates:
            if parsed_dates[0].isoformat() != expected_today:
                passed = False
                messages.append(f'calendar must start today ({expected_today})')
            for earlier, later in zip(parsed_dates, parsed_dates[1:]):
                if (later - earlier).days != 1:
                    passed = False
                    messages.append('calendar must span contiguous days')
                    break

        source_status = list((normalized or public_snapshot).get('source_status') or [])
        loaded_keys = {
            item.get('key')
            for item in source_status
            if item.get('status') == 'loaded' and int(item.get('count', 0) or 0) > 0
        }
        loaded_core_sources = len(loaded_keys & set(CORE_SOURCE_KEYS))
        loaded_sources = len(loaded_keys)
        if loaded_core_sources < 3:
            passed = False
            messages.append('at least 3 core sources must load')
        if loaded_sources < 4:
            passed = False
            messages.append('at least 4 total sources must load')

        visible_occurrences = sum(len(day.get('events') or []) for day in calendar)
        today_occurrences = len(((normalized or public_snapshot).get('today') or {}).get('events') or [])
        if visible_occurrences < 30:
            passed = False
            messages.append('at least 30 visible calendar occurrences required')
        if today_occurrences < 3:
            passed = False
            messages.append('at least 3 visible occurrences required today')

        return {
            'passed': passed,
            'messages': messages,
            'calendar_days': len(calendar),
            'expected_today': expected_today,
            'loaded_core_sources': loaded_core_sources,
            'loaded_sources': loaded_sources,
            'visible_occurrences': visible_occurrences,
            'today_occurrences': today_occurrences,
        }

    def _build_candidate_full(
        self,
        source_status: list[dict[str, Any]],
        warnings: list[str],
        events: list[Event],
        public_snapshot: dict[str, Any],
        validation: dict[str, Any],
    ) -> dict[str, Any]:
        loaded_sources = [item['label'] for item in source_status if item.get('status') == 'loaded' and int(item.get('count', 0) or 0) > 0]
        source_warnings = [item for item in source_status if item.get('status') != 'loaded']
        return {
            'ok': validation['passed'],
            'stale': False,
            'snapshot_id': public_snapshot.get('snapshot_id'),
            'generated_at': public_snapshot.get('generated_at'),
            'sources': loaded_sources,
            'errors': warnings,
            'source_status': source_status,
            'source_warnings': source_warnings,
            'today': public_snapshot.get('today'),
            'calendar': public_snapshot.get('calendar'),
            'counts': public_snapshot.get('counts'),
            'top_categories': public_snapshot.get('top_categories'),
            'validation': validation,
            'public_snapshot': public_snapshot,
            'event_occurrences': sum(len(day.get('events') or []) for day in public_snapshot.get('calendar') or []),
            'raw_event_count': len(events),
            'artifacts': self._artifact_paths(),
        }

    def _build_private_snapshot(self, candidate_full: dict[str, Any]) -> dict[str, Any]:
        payload = {key: value for key, value in candidate_full.items() if key not in {'artifacts'}}
        payload['ok'] = True
        payload['stale'] = False
        return payload

    def _refresh_state(self, candidate_full: dict[str, Any]) -> dict[str, Any]:
        validation = candidate_full['validation']
        return {
            'status': 'validated' if validation['passed'] else 'validation_failed',
            'snapshot_id': candidate_full.get('snapshot_id'),
            'generated_at': candidate_full.get('generated_at'),
            'loaded_core_sources': validation['loaded_core_sources'],
            'loaded_sources': validation['loaded_sources'],
            'visible_occurrences': validation['visible_occurrences'],
            'today_occurrences': validation['today_occurrences'],
            'messages': list(validation['messages']),
            'updated_at': now_pt().isoformat(timespec='seconds'),
        }

    def _artifact_paths(self) -> dict[str, str]:
        return {
            'candidate_full': str(self.candidate_full_path),
            'lkg_full': str(self.lkg_full_path),
            'private_snapshot': str(self.private_snapshot_path),
            'public_snapshot': str(self.public_snapshot_path),
            'publish_state': str(self.publish_state_path),
        }

    def _load_json(self, path: Path) -> dict[str, Any] | None:
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            return None

    def _atomic_write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, indent=2, sort_keys=False).encode('utf-8')
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f'.{path.name}.', suffix='.tmp', delete=False) as tmp:
            tmp.write(encoded)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _locked(self):
        return _locked_file(self.lock_path)


@contextmanager
def _locked_file(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _scrub_public_projection(public_snapshot: dict[str, Any], *, stale: bool = False) -> dict[str, Any]:
    safe = normalize_public_snapshot(public_snapshot)
    safe['errors'] = []
    safe['source_warnings'] = [item for item in safe.get('source_status', []) if item.get('status') != 'loaded']
    if stale:
        safe['stale'] = True
    return safe


def read_private_snapshot(force: bool = False, engine: CanonicalEngine | None = None) -> dict[str, Any]:
    engine = engine or CanonicalEngine()
    if force:
        result = engine.refresh(publish=False)
        if result.get('ok'):
            stored = engine._load_json(engine.private_snapshot_path)
            if isinstance(stored, dict):
                return stored
    with engine._locked():
        private_payload = engine._load_json(engine.private_snapshot_path)
        if isinstance(private_payload, dict) and private_payload.get('public_snapshot'):
            try:
                normalize_public_snapshot(private_payload['public_snapshot'])
                return private_payload
            except SnapshotValidationError:
                pass
        lkg_payload = engine._load_json(engine.lkg_full_path)
        if not isinstance(lkg_payload, dict):
            return {'ok': False, 'error': 'canonical private snapshot unavailable'}
        public_snapshot = lkg_payload.get('public_snapshot')
        if not isinstance(public_snapshot, dict):
            return {'ok': False, 'error': 'canonical public snapshot unavailable'}
        safe_public = _scrub_public_projection(public_snapshot, stale=True)
        return {
            'ok': True,
            'stale': True,
            'snapshot_id': safe_public['snapshot_id'],
            'generated_at': safe_public['generated_at'],
            'sources': safe_public['sources'],
            'errors': [],
            'source_status': safe_public['source_status'],
            'source_warnings': safe_public['source_warnings'],
            'today': safe_public['today'],
            'calendar': safe_public['calendar'],
            'counts': safe_public['counts'],
            'top_categories': safe_public['top_categories'],
            'validation': lkg_payload.get('validation', {'passed': True, 'messages': []}),
            'public_snapshot': safe_public,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Canonical Fun Finder engine')
    sub = parser.add_subparsers(dest='command', required=False)
    refresh_parser = sub.add_parser('refresh')
    refresh_parser.add_argument('--publish', action='store_true')
    refresh_parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)

    if args.command in {None, 'refresh'}:
        engine = CanonicalEngine()
        result = engine.refresh(publish=getattr(args, 'publish', False))
        summary = {
            'ok': result.get('ok', False),
            'snapshot_id': result.get('snapshot_id'),
            'generated_at': result.get('generated_at'),
            'loaded_sources': result.get('loaded_sources'),
            'loaded_core_sources': result.get('loaded_core_sources'),
            'visible_occurrences': result.get('visible_occurrences'),
            'today_occurrences': result.get('today_occurrences'),
            'validation_messages': result.get('validation', {}).get('messages', []),
            'artifact_paths': result.get('artifact_paths'),
        }
        if getattr(args, 'publish', False) and result.get('publish'):
            summary['publish'] = {
                'ok': result['publish'].get('ok', False),
                'snapshot_id': result['publish'].get('snapshot_id'),
                'status': result['publish'].get('status'),
            }
        if getattr(args, 'json', False) or True:
            print(json.dumps(summary, indent=2))
        return 0 if result.get('ok') and (not getattr(args, 'publish', False) or result.get('publish', {}).get('ok')) else 1
    parser.print_help()
    return 1


def _env_csv(name: str) -> list[str]:
    value = os.environ.get(name, '').strip()
    if not value:
        return []
    return [item.strip() for item in value.split(',') if item.strip()]


if __name__ == '__main__':
    raise SystemExit(main())
