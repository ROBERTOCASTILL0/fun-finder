from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

from snapshot_schema import normalize_public_snapshot

DEFAULT_REPO = 'ROBERTOCASTILL0/fun-finder'
DEFAULT_BRANCH = 'published-snapshot'
DEFAULT_PATH = 'data/public_snapshot.json'
SAFE_COMMIT_PREFIX = 'Mirror public snapshot'


class GhApiError(RuntimeError):
    def __init__(self, message: str, *, returncode: int = 1, stderr: str = '') -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


Runner = Callable[..., str]


def _run_gh_api(argv: list[str], *, input_text: str | None = None) -> str:
    # Prefer gh's durable credential store. Shared process environments can retain
    # expired token variables, and gh gives those variables precedence over a
    # valid stored login.
    env = os.environ.copy()
    env.pop('GH_TOKEN', None)
    env.pop('GITHUB_TOKEN', None)
    completed = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        env=env,
        input=input_text,
    )
    if completed.returncode != 0:
        raise GhApiError('gh api command failed', returncode=completed.returncode, stderr=completed.stderr.strip())
    return completed.stdout


class SnapshotMirror:
    def __init__(
        self,
        *,
        snapshot_path: str | Path,
        repo: str = DEFAULT_REPO,
        branch: str = DEFAULT_BRANCH,
        remote_path: str = DEFAULT_PATH,
        runner: Callable[..., str] | None = None,
    ) -> None:
        self.snapshot_path = Path(snapshot_path)
        self.repo = repo
        self.branch = branch
        self.remote_path = remote_path.strip('/')
        self.runner = runner or _run_gh_api

    def mirror(self) -> dict[str, Any]:
        snapshot = normalize_public_snapshot(json.loads(self.snapshot_path.read_text(encoding='utf-8')))
        encoded_snapshot = json.dumps(snapshot, indent=2, sort_keys=False).encode('utf-8')
        content_b64 = base64.b64encode(encoded_snapshot).decode('ascii')
        current_sha = self._get_current_sha()
        put_args = [
            'gh',
            'api',
            '--method',
            'PUT',
            f'/repos/{self.repo}/contents/{self.remote_path}',
            '--input',
            '-',
        ]
        request_payload = {
            'branch': self.branch,
            'message': f'{SAFE_COMMIT_PREFIX} {snapshot["snapshot_id"]} to {self.branch}',
            'content': content_b64,
        }
        if current_sha:
            request_payload['sha'] = current_sha
        raw_response = self.runner(put_args, input_text=json.dumps(request_payload))
        payload = json.loads(raw_response or '{}')
        return {
            'ok': True,
            'status': 'mirrored',
            'snapshot_id': snapshot['snapshot_id'],
            'generated_at': snapshot['generated_at'],
            'commit_sha': ((payload.get('commit') or {}).get('sha')),
            'content_sha': ((payload.get('content') or {}).get('sha')),
        }

    def _get_current_sha(self) -> str | None:
        try:
            raw_response = self.runner([
                'gh',
                'api',
                '--method',
                'GET',
                f'/repos/{self.repo}/contents/{self.remote_path}?ref={self.branch}',
            ])
        except GhApiError as exc:
            if '404' in exc.stderr:
                return None
            raise
        payload = json.loads(raw_response or '{}')
        sha = payload.get('sha')
        return sha if isinstance(sha, str) and sha else None


def mirror_snapshot(
    *,
    snapshot_path: str | Path,
    repo: str = DEFAULT_REPO,
    branch: str = DEFAULT_BRANCH,
    remote_path: str = DEFAULT_PATH,
    runner: Callable[..., str] | None = None,
) -> dict[str, Any]:
    return SnapshotMirror(
        snapshot_path=snapshot_path,
        repo=repo,
        branch=branch,
        remote_path=remote_path,
        runner=runner,
    ).mirror()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Mirror public snapshot to GitHub contents API')
    parser.add_argument('--snapshot-path', default=os.environ.get('FUN_FINDER_PUBLIC_SNAPSHOT_PATH'))
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    if not args.snapshot_path:
        raise SystemExit('snapshot path is required')
    result = mirror_snapshot(snapshot_path=args.snapshot_path)
    print(json.dumps(result, indent=2))
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
