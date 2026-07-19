from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from snapshot_schema import SnapshotValidationError, normalize_public_snapshot

ROOT = Path(__file__).resolve().parent
DEFAULT_RUNTIME_SNAPSHOT_PATH = Path('/tmp/sd-fun-finder/public_snapshot.json')
PACKAGED_SNAPSHOT_PATH = ROOT / 'data' / 'public_snapshot.json'
DEFAULT_DURABLE_SNAPSHOT_URL = 'https://raw.githubusercontent.com/ROBERTOCASTILL0/fun-finder/published-snapshot/data/public_snapshot.json'
MAX_DURABLE_BYTES = 1024 * 1024
DURABLE_TIMEOUT_SECONDS = 8
SAFE_USER_AGENT = 'sd-fun-finder/1.0'


Opener = Callable[..., Any]


def runtime_snapshot_path() -> Path:
    return Path(os.environ.get('FUN_FINDER_RUNTIME_SNAPSHOT_PATH', str(DEFAULT_RUNTIME_SNAPSHOT_PATH)))


def runtime_lock_path() -> Path:
    return Path(f'{runtime_snapshot_path()}.lock')


def durable_snapshot_url(*, explicit_test_url: str | None = None) -> str:
    candidate = explicit_test_url or os.environ.get('FUN_FINDER_DURABLE_SNAPSHOT_URL') or DEFAULT_DURABLE_SNAPSHOT_URL
    if explicit_test_url:
        return candidate
    if not candidate.startswith('https://raw.githubusercontent.com/'):
        raise ValueError('durable snapshot URL must use https://raw.githubusercontent.com/')
    return candidate


@contextmanager
def _locked_runtime_file() -> Iterator[int]:
    lock_path = runtime_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def load_active_snapshot(*, durable_url: str | None = None, opener: Opener = urlopen) -> dict[str, Any] | None:
    with _locked_runtime_file():
        runtime = _load_valid_snapshot(runtime_snapshot_path())
        if runtime is not None:
            return runtime
        durable = _fetch_durable_snapshot(durable_url=durable_url, opener=opener)
        if durable is not None:
            _atomic_write_json(runtime_snapshot_path(), durable)
            return durable
        return _load_valid_snapshot(PACKAGED_SNAPSHOT_PATH)


def publish_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_public_snapshot(payload)
    runtime_path = runtime_snapshot_path()
    runtime_path.parent.mkdir(parents=True, exist_ok=True)

    with _locked_runtime_file():
        active = _load_valid_snapshot(runtime_path)
        if active is not None:
            ordering = _compare_snapshots(normalized, active)
            if ordering < 0:
                raise ValueError('snapshot is older than active snapshot')
            if ordering == 0 and normalized['snapshot_id'] != active['snapshot_id']:
                raise ValueError('snapshot timestamp collision with different snapshot_id')
        _atomic_write_json(runtime_path, normalized)
    return normalized


def _fetch_durable_snapshot(*, durable_url: str | None, opener: Opener) -> dict[str, Any] | None:
    request = Request(
        durable_snapshot_url(explicit_test_url=durable_url),
        headers={'Accept': 'application/json', 'User-Agent': SAFE_USER_AGENT},
        method='GET',
    )
    try:
        with opener(request, timeout=DURABLE_TIMEOUT_SECONDS) as response:
            payload = _bounded_read(response)
        return normalize_public_snapshot(json.loads(payload.decode('utf-8')))
    except (OSError, HTTPError, URLError, ValueError, json.JSONDecodeError, SnapshotValidationError):
        return None


def _bounded_read(response: Any) -> bytes:
    chunks = bytearray()
    while True:
        chunk = response.read(min(65536, MAX_DURABLE_BYTES + 1 - len(chunks)))
        if not chunk:
            break
        chunks.extend(chunk)
        if len(chunks) > MAX_DURABLE_BYTES:
            raise ValueError('durable snapshot exceeds size limit')
    return bytes(chunks)


def _load_valid_snapshot(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
        return normalize_public_snapshot(raw)
    except (OSError, json.JSONDecodeError, SnapshotValidationError):
        return None


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
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


def _parse_generated_at(snapshot: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(snapshot['generated_at'])


def _compare_snapshots(left: dict[str, Any], right: dict[str, Any]) -> int:
    left_ts = _parse_generated_at(left)
    right_ts = _parse_generated_at(right)
    if left_ts < right_ts:
        return -1
    if left_ts > right_ts:
        return 1
    return 0
