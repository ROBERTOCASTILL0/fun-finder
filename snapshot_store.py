from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from snapshot_schema import SnapshotValidationError, normalize_public_snapshot

ROOT = Path(__file__).resolve().parent
DEFAULT_RUNTIME_SNAPSHOT_PATH = Path('/tmp/sd-fun-finder/public_snapshot.json')
PACKAGED_SNAPSHOT_PATH = ROOT / 'data' / 'public_snapshot.json'


def runtime_snapshot_path() -> Path:
    return Path(os.environ.get('FUN_FINDER_RUNTIME_SNAPSHOT_PATH', str(DEFAULT_RUNTIME_SNAPSHOT_PATH)))


def runtime_lock_path() -> Path:
    return Path(f'{runtime_snapshot_path()}.lock')


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


def load_active_snapshot() -> dict[str, Any] | None:
    with _locked_runtime_file():
        runtime = _load_valid_snapshot(runtime_snapshot_path())
        if runtime is not None:
            return runtime
        return _load_valid_snapshot(PACKAGED_SNAPSHOT_PATH)


def publish_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_public_snapshot(payload)
    runtime_path = runtime_snapshot_path()
    runtime_path.parent.mkdir(parents=True, exist_ok=True)

    with _locked_runtime_file():
        active = _load_valid_snapshot(runtime_path)
        if active is None:
            active = _load_valid_snapshot(PACKAGED_SNAPSHOT_PATH)
        if active is not None and _parse_generated_at(normalized) < _parse_generated_at(active):
            raise ValueError('snapshot is older than active snapshot')
        _atomic_write_json(runtime_path, normalized)
    return normalized


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
