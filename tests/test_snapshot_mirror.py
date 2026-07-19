from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import snapshot_mirror
from tests.snapshot_fixtures import make_snapshot


class SnapshotMirrorTests(unittest.TestCase):
    def test_default_runner_ignores_stale_environment_tokens(self) -> None:
        captured: dict = {}

        class Completed:
            returncode = 0
            stdout = '{}'
            stderr = ''

        def fake_run(argv, **kwargs):
            captured['argv'] = argv
            captured['env'] = kwargs['env']
            captured['input'] = kwargs['input']
            return Completed()

        with patch.dict('os.environ', {'GH_TOKEN': 'expired-gh', 'GITHUB_TOKEN': 'expired-github'}), \
                patch.object(snapshot_mirror.subprocess, 'run', side_effect=fake_run):
            self.assertEqual(snapshot_mirror._run_gh_api(['gh', 'api', 'user']), '{}')

        self.assertNotIn('GH_TOKEN', captured['env'])
        self.assertNotIn('GITHUB_TOKEN', captured['env'])
        self.assertIsNone(captured['input'])

    def test_mirror_puts_validated_snapshot_to_github_contents_api(self) -> None:
        runtime_dir = Path(tempfile.mkdtemp(prefix='mirror-test-'))
        snapshot_path = runtime_dir / 'public_snapshot.json'
        snapshot = make_snapshot(snapshot_id='mirror-1')
        snapshot_path.write_text(json.dumps(snapshot), encoding='utf-8')
        calls: list[list[str]] = []

        def runner(argv: list[str], *, input_text: str | None = None) -> str:
            calls.append(argv)
            if argv[:4] == ['gh', 'api', '--method', 'GET']:
                return json.dumps({'sha': 'abc123'})
            if argv[:4] == ['gh', 'api', '--method', 'PUT']:
                self.assertEqual(argv[-2:], ['--input', '-'])
                self.assertNotIn('content=', ' '.join(argv))
                request_payload = json.loads(input_text or '{}')
                decoded = base64.b64decode(request_payload['content']).decode('utf-8')
                mirrored = json.loads(decoded)
                self.assertEqual(mirrored['snapshot_id'], 'mirror-1')
                self.assertEqual(request_payload['sha'], 'abc123')
                self.assertEqual(request_payload['branch'], 'published-snapshot')
                self.assertEqual(request_payload['message'], 'Mirror public snapshot mirror-1 to published-snapshot')
                return json.dumps({'content': {'sha': 'def456'}})
            raise AssertionError(f'unexpected call: {argv}')

        result = snapshot_mirror.mirror_snapshot(
            snapshot_path=snapshot_path,
            runner=runner,
        )

        self.assertTrue(result['ok'])
        self.assertEqual(result['snapshot_id'], 'mirror-1')
        self.assertEqual(len(calls), 2)

    def test_missing_remote_file_omits_sha(self) -> None:
        runtime_dir = Path(tempfile.mkdtemp(prefix='mirror-test-'))
        snapshot_path = runtime_dir / 'public_snapshot.json'
        snapshot = make_snapshot(snapshot_id='mirror-create')
        snapshot_path.write_text(json.dumps(snapshot), encoding='utf-8')
        put_args: list[str] = []

        def runner(argv: list[str], *, input_text: str | None = None) -> str:
            if argv[:4] == ['gh', 'api', '--method', 'GET']:
                raise snapshot_mirror.GhApiError('not found', returncode=1, stderr='404 Not Found')
            if argv[:4] == ['gh', 'api', '--method', 'PUT']:
                put_args.extend(json.loads(input_text or '{}').keys())
                return json.dumps({'content': {'sha': 'newsha'}})
            raise AssertionError(f'unexpected call: {argv}')

        result = snapshot_mirror.mirror_snapshot(snapshot_path=snapshot_path, runner=runner)

        self.assertTrue(result['ok'])
        self.assertNotIn('sha', put_args)

    def test_invalid_snapshot_is_rejected_before_gh_calls(self) -> None:
        runtime_dir = Path(tempfile.mkdtemp(prefix='mirror-test-'))
        snapshot_path = runtime_dir / 'public_snapshot.json'
        invalid = make_snapshot(snapshot_id='broken')
        invalid['today']['events'][0]['url'] = 'ftp://broken.example.com'
        invalid['calendar'][0]['events'][0]['url'] = 'ftp://broken.example.com'
        snapshot_path.write_text(json.dumps(invalid), encoding='utf-8')
        calls = 0

        def runner(argv: list[str], **_kwargs) -> str:
            nonlocal calls
            del argv
            calls += 1
            return '{}'

        with self.assertRaises(Exception):
            snapshot_mirror.mirror_snapshot(snapshot_path=snapshot_path, runner=runner)

        self.assertEqual(calls, 0)


if __name__ == '__main__':
    unittest.main()
