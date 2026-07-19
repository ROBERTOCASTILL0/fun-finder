#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from snapshot_mirror import SnapshotMirror
from snapshot_schema import normalize_public_snapshot

DEFAULT_BASE_URL = 'https://san-diego-fun-finder.onrender.com'
DEFAULT_PUBLIC_SNAPSHOT_PATH = Path('/opt/data/roberto-ui/data/fun_finder_public_snapshot.json')
DEFAULT_PUBLISH_STATE_PATH = Path('/opt/data/roberto-ui/data/fun_finder_publish_state.json')


def _safe_publish_result(result: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {
        'ok': bool(result.get('ok', False)),
        'status': result.get('status'),
        'snapshot_id': result.get('snapshot_id'),
        'generated_at': result.get('generated_at'),
    }
    accepted = result.get('accepted')
    if isinstance(accepted, dict):
        safe['accepted'] = {
            'snapshot_id': accepted.get('snapshot_id'),
            'ok': bool(accepted.get('ok', False)),
        }
    readback = result.get('readback')
    if isinstance(readback, dict):
        safe['readback'] = {
            'snapshot_id': readback.get('snapshot_id'),
            'event_count': readback.get('event_count'),
        }
    mirror = result.get('mirror')
    if isinstance(mirror, dict):
        safe['mirror'] = {
            'snapshot_id': mirror.get('snapshot_id'),
            'status': mirror.get('status'),
            'ok': bool(mirror.get('ok', False)),
        }
    if result.get('error'):
        safe['error'] = result.get('error')
    return safe


class SnapshotPublisher:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        ingest_key: str | None = None,
        public_snapshot_path: str | Path | None = None,
        publish_state_path: str | Path | None = None,
        opener=urlopen,
        mirror_factory: Callable[..., Any] | None = None,
        allow_test_base_url: bool = False,
    ) -> None:
        self.base_url = (base_url or os.environ.get('FUN_FINDER_PUBLIC_BASE_URL') or DEFAULT_BASE_URL).rstrip('/')
        self.ingest_key = ingest_key or os.environ.get('FUN_FINDER_SNAPSHOT_INGEST_KEY') or ''
        self.public_snapshot_path = Path(public_snapshot_path or os.environ.get('FUN_FINDER_PUBLIC_SNAPSHOT_PATH') or DEFAULT_PUBLIC_SNAPSHOT_PATH)
        self.publish_state_path = Path(publish_state_path or os.environ.get('FUN_FINDER_PUBLISH_STATE_PATH') or DEFAULT_PUBLISH_STATE_PATH)
        self.opener = opener
        self.mirror_factory = mirror_factory or SnapshotMirror
        self.allow_test_base_url = allow_test_base_url

    def publish(self) -> dict[str, Any]:
        snapshot = normalize_public_snapshot(self._load_snapshot())
        try:
            self._validate_base_url()
            if not self.ingest_key:
                raise ValueError('FUN_FINDER_SNAPSHOT_INGEST_KEY is required')
            mirror = self.mirror_factory(snapshot_path=self.public_snapshot_path).mirror()
            accepted = self._post_snapshot(snapshot)
            if accepted.get('snapshot_id') != snapshot['snapshot_id']:
                raise ValueError('publish response snapshot_id mismatch')
            readback = self._get_events()
            if readback.get('snapshot_id') != snapshot['snapshot_id']:
                raise ValueError('readback snapshot_id mismatch')
            result = {
                'ok': True,
                'status': 'published',
                'snapshot_id': snapshot['snapshot_id'],
                'generated_at': snapshot['generated_at'],
                'mirror': mirror,
                'accepted': {
                    'snapshot_id': accepted.get('snapshot_id'),
                    'ok': bool(accepted.get('ok', True)),
                },
                'readback': {
                    'snapshot_id': readback.get('snapshot_id'),
                    'event_count': len(readback.get('events') or []),
                },
            }
        except Exception as exc:
            result = {
                'ok': False,
                'status': 'publish_failed',
                'snapshot_id': snapshot.get('snapshot_id'),
                'generated_at': snapshot.get('generated_at'),
                'error': str(exc),
            }
        self._write_publish_state(result)
        return result

    def _validate_base_url(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != 'https':
            raise ValueError('base URL must use https')
        if not parsed.hostname:
            raise ValueError('base URL must include a hostname')
        if parsed.username or parsed.password:
            raise ValueError('base URL must not include credentials')
        if parsed.path not in {'', '/'} or parsed.params or parsed.query or parsed.fragment:
            raise ValueError('base URL must not include path, query, or fragment')
        hostname = parsed.hostname.rstrip('.').lower()
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            raise ValueError('base URL must not use an IP literal')
        if hostname in {'localhost'}:
            raise ValueError('base URL must not use localhost')
        if hostname.endswith('.localhost'):
            raise ValueError('base URL must not use localhost')
        allowed_hosts = {'san-diego-fun-finder.onrender.com'}
        extra = os.environ.get('FUN_FINDER_ALLOWED_PUBLIC_HOSTS', '')
        for item in [entry.strip().lower() for entry in extra.split(',') if entry.strip()]:
            parsed_item = urlparse(item if '://' in item else f'https://{item}')
            if parsed_item.scheme != 'https' or not parsed_item.hostname:
                raise ValueError('FUN_FINDER_ALLOWED_PUBLIC_HOSTS entries must be https hosts')
            if parsed_item.path not in {'', '/'} or parsed_item.params or parsed_item.query or parsed_item.fragment or parsed_item.username or parsed_item.password:
                raise ValueError('FUN_FINDER_ALLOWED_PUBLIC_HOSTS entries must be bare https hosts')
            allowed_hosts.add(parsed_item.hostname.rstrip('.').lower())
        if self.allow_test_base_url and hostname.endswith('.test'):
            return
        if hostname not in allowed_hosts:
            raise ValueError('base URL host is not allowed')

    def _load_snapshot(self) -> dict[str, Any]:
        return json.loads(self.public_snapshot_path.read_text(encoding='utf-8'))

    def _post_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(snapshot).encode('utf-8')
        request = Request(
            url=f'{self.base_url}/internal/snapshot',
            data=body,
            headers={
                'Authorization': f'Bearer {self.ingest_key}',
                'Content-Type': 'application/json',
                'Accept': 'application/json',
            },
            method='POST',
        )
        response = self.opener(request, timeout=30)
        status = int(getattr(response, 'status', 200) or 0)
        payload = json.loads(response.read().decode('utf-8'))
        if status != 202:
            raise ValueError(f'expected HTTP 202, got {status}')
        return payload

    def _get_events(self) -> dict[str, Any]:
        request = Request(
            url=f'{self.base_url}/api/events',
            headers={
                'Accept': 'application/json',
            },
            method='GET',
        )
        response = self.opener(request, timeout=30)
        status = int(getattr(response, 'status', 200) or 0)
        payload = json.loads(response.read().decode('utf-8'))
        if status != 200:
            raise ValueError(f'expected HTTP 200 from readback, got {status}')
        return payload

    def _write_publish_state(self, result: dict[str, Any]) -> None:
        existing = self._load_state()
        existing['publish'] = {
            'status': result['status'],
            'snapshot_id': result.get('snapshot_id'),
            'generated_at': result.get('generated_at'),
            'ok': result.get('ok', False),
            'updated_at': datetime.now().astimezone().isoformat(timespec='seconds'),
        }
        if result.get('ok'):
            existing['public_snapshot'] = {
                'snapshot_id': result.get('snapshot_id'),
                'generated_at': result.get('generated_at'),
            }
        else:
            existing['publish']['error'] = result.get('error')
        self._atomic_write_json(self.publish_state_path, existing)

    def _load_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.publish_state_path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _atomic_write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, indent=2).encode('utf-8')
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Publish validated Fun Finder public snapshot')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = SnapshotPublisher().publish()
    except (OSError, ValueError, HTTPError, URLError, json.JSONDecodeError) as exc:
        result = {'ok': False, 'status': 'publish_failed', 'error': str(exc)}
    safe_result = _safe_publish_result(result)
    print(json.dumps(safe_result if args.json else safe_result, indent=2))
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
