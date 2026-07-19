#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

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
    ) -> None:
        self.base_url = (base_url or os.environ.get('FUN_FINDER_PUBLIC_BASE_URL') or DEFAULT_BASE_URL).rstrip('/')
        self.ingest_key = ingest_key or os.environ.get('FUN_FINDER_SNAPSHOT_INGEST_KEY') or ''
        self.public_snapshot_path = Path(public_snapshot_path or os.environ.get('FUN_FINDER_PUBLIC_SNAPSHOT_PATH') or DEFAULT_PUBLIC_SNAPSHOT_PATH)
        self.publish_state_path = Path(publish_state_path or os.environ.get('FUN_FINDER_PUBLISH_STATE_PATH') or DEFAULT_PUBLISH_STATE_PATH)
        self.opener = opener

    def publish(self) -> dict[str, Any]:
        snapshot = normalize_public_snapshot(self._load_snapshot())
        if not self.ingest_key:
            result = {
                'ok': False,
                'status': 'publish_failed',
                'snapshot_id': snapshot['snapshot_id'],
                'error': 'FUN_FINDER_SNAPSHOT_INGEST_KEY is required',
            }
            self._write_publish_state(result)
            return result

        try:
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
                'snapshot_id': snapshot['snapshot_id'],
                'generated_at': snapshot['generated_at'],
                'error': str(exc),
            }
        self._write_publish_state(result)
        return result

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
