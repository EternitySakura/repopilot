import errno
import hashlib
import json
import os
import stat
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from repopilot.agent.demo import FIXTURE_ROOT, run_demo
from repopilot.agent.model_adapter import FakeModel
from repopilot.contracts.action import Action
from repopilot.dispatcher import Dispatcher
from repopilot.observability.events import EventLog
from repopilot.tools.policy import ReadPolicy, ToolFailure


def snapshot(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def records(events):
    return [json.loads(line) for line in events.path.read_text(encoding='utf-8').splitlines()]


def test_real_demo_and_unchanged_fixture(tmp_path):
    before = snapshot(FIXTURE_ROOT)
    events, results = run_demo(tmp_path / 'runs')
    assert all(r.ok and r.workspace_generation == 0 for r in results)
    assert any(m['path'] == 'src/fixture_text/parser.py' and m['line'] == 1
               and 'def parse_words' in m['text'] for m in results[1].data['matches'])
    assert results[2].data['sha256'] == before['src/fixture_text/parser.py']
    rows = records(events)
    assert [r['event'] for r in rows] == [
        'run.started', 'tool.intent', 'tool.completed', 'tool.intent',
        'tool.completed', 'tool.intent', 'tool.completed', 'run.finished',
    ]
    for result, intent, completed in zip(results, rows[1::2], rows[2::2], strict=False):
        assert intent['tool_call_id'] == completed['tool_call_id'] == str(result.tool_call_id)
        assert completed['result'] == result.model_dump(mode='json')
    assert all(r['run_id'] == events.run_id and r['timestamp'] for r in rows)
    assert rows[-1]['bug_fixed'] is False
    assert snapshot(FIXTURE_ROOT) == before
    other, _ = run_demo(tmp_path / 'runs', FakeModel([]))
    assert other.run_id != events.run_id


@pytest.fixture
def sandbox(tmp_path):
    root = tmp_path / 'repo'
    (root / 'src').mkdir(parents=True)
    (root / 'src/a.py').write_text('needle\n' * 120, encoding='utf-8')
    (root / 'tests').mkdir()
    (root / 'tests/public.py').write_text('public', encoding='utf-8')
    (root / 'tests/hidden.py').write_text('needle SECRET', encoding='utf-8')
    policy = ReadPolicy(root, public_sources=frozenset({'src/a.py', 'src/link.py'}),
                        public_tests=frozenset({'tests/public.py'}))
    return root, Dispatcher(policy, EventLog(tmp_path / 'runs'))


def call(dispatcher, kind='read_file', **args):
    return dispatcher.dispatch({'type': kind, 'arguments': args})


@pytest.mark.parametrize('path', [
    '../secret', '/etc/passwd', 'C:/secret', 'C:secret', 'src/../secret',
    'src\\a.py', '.git/config', '.env', '.env.example', '__pycache__/x',
    'tests/hidden.py', 'verifier/private.py', 'src/private.py',
])
def test_denied_paths(sandbox, path):
    _, dispatcher = sandbox
    assert call(dispatcher, path=path).error.code == 'PATH_DENIED'


def test_public_tests_and_expected_errors(sandbox):
    root, dispatcher = sandbox
    assert call(dispatcher, path='tests/public.py').data['lines'][0]['text'] == 'public'
    assert call(dispatcher, path='src/link.py').error.code == 'NOT_FOUND'
    assert call(dispatcher, path='src').error.code == 'NOT_FILE'
    assert call(dispatcher, 'list_directory', path='src/a.py').error.code == 'NOT_DIRECTORY'
    assert call(dispatcher, path='src/a.py', query='x').error.code == 'INVALID_ARGUMENTS'
    assert call(dispatcher, path='src/a.py', start_line=3, end_line=2).error.code == 'INVALID_ARGUMENTS'
    listing = call(dispatcher, 'list_directory', path='tests')
    assert listing.data['entries'] == [{'path': 'tests/public.py', 'type': 'file'}]
    assert call(dispatcher, 'search_code', path='tests', query='SECRET').data['matches'] == []
    assert root.exists()


def test_limits_and_literal_search(sandbox):
    _, dispatcher = sandbox
    result = call(dispatcher, 'search_code', path='src', query='needle', limit=3)
    assert result.truncated and len(result.data['matches']) == 3
    assert [m['line'] for m in result.data['matches']] == [1, 2, 3]
    assert not call(dispatcher, 'search_code', path='src', query='n.*').data['matches']
    dispatcher.policy.max_bytes = 20
    result = call(dispatcher, path='src/a.py')
    assert result.truncated and result.data['truncated'] and result.data['sha256'] is None
    assert sum(len(line['text']) for line in result.data['lines']) <= 20
    assert call(dispatcher, 'search_code', path='src', query='absent').truncated
    assert call(dispatcher, 'list_directory', path='.', limit=1).truncated
    assert call(dispatcher, 'list_directory', depth=4).error.code == 'INVALID_ARGUMENTS'
    assert call(dispatcher, 'search_code', query='x', limit=101).error.code == 'INVALID_ARGUMENTS'


def test_symlink_denied(sandbox, tmp_path):
    root, dispatcher = sandbox
    outside = tmp_path / 'secret'
    outside.write_text('secret', encoding='utf-8')
    try:
        (root / 'src/link.py').symlink_to(outside)
    except OSError as exc:
        if getattr(exc, 'winerror', None) not in {5, 1314} and exc.errno not in {errno.EACCES, errno.EPERM}:
            raise
        pytest.skip(f'OS denied symlink creation: {exc}')
    assert call(dispatcher, path='src/link.py').error.code == 'PATH_DENIED'
    assert call(dispatcher, 'search_code', path='src', query='secret').data['matches'] == []


@pytest.mark.skipif(os.name != 'nt', reason='Windows junction test')
def test_windows_junction_denied(sandbox, tmp_path):
    root, dispatcher = sandbox
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'a.py').write_text('secret', encoding='utf-8')
    junction = root / 'src/junction'
    completed = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(outside)],
                               capture_output=True, check=False)
    if completed.returncode != 0:
        pytest.skip(f'OS denied junction creation: {completed.stderr!r}')
    dispatcher.policy.public_sources |= {'src/junction/a.py'}
    try:
        assert call(dispatcher, path='src/junction/a.py').error.code == 'PATH_DENIED'
        assert call(dispatcher, 'list_directory', path='src/junction').error.code == 'PATH_DENIED'
    finally:
        # Remove only the junction itself, never recursively touch its target.
        junction.rmdir()
    assert (outside / 'a.py').read_text(encoding='utf-8') == 'secret'


def test_intent_precedes_execution_and_fatal_errors_propagate(sandbox):
    _, dispatcher = sandbox

    def broken(*args):
        assert records(dispatcher.events)[-1]['event'] == 'tool.intent'
        raise RuntimeError('diagnostic sentinel')

    with patch('repopilot.dispatcher.read_file', broken), pytest.raises(
        RuntimeError, match='diagnostic sentinel'
    ):
        call(dispatcher, path='src/a.py')
    row = records(dispatcher.events)[-1]
    assert row['event'] == 'tool.failed' and row['fatal']
    assert 'diagnostic sentinel' in row['diagnostic']


def test_demo_failure_finishes_run(tmp_path):
    action = Action.model_validate({'type': 'read_file', 'arguments': {'path': 'src/fixture_text/parser.py'}})
    with patch('repopilot.dispatcher.read_file', side_effect=RuntimeError('fatal')), pytest.raises(
        RuntimeError, match='fatal'
    ):
        run_demo(tmp_path / 'runs', FakeModel([action]))
    rows = [json.loads(line) for line in next((tmp_path / 'runs').glob('*/events.jsonl')).read_text().splitlines()]
    assert rows[-1]['event'] == 'run.finished' and rows[-1]['status'] == 'failed'


@pytest.mark.parametrize(('mode', 'attributes'), [(stat.S_IFLNK, 0), (stat.S_IFDIR, 0x400)])
def test_link_metadata_denied_on_all_platforms(tmp_path, mode, attributes):
    info = SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
    with patch('pathlib.Path.lstat', return_value=info), pytest.raises(ToolFailure) as error:
        ReadPolicy._check_node(tmp_path / 'link')
    assert error.value.code == 'PATH_DENIED'


def test_search_depth_is_bounded(sandbox):
    root, dispatcher = sandbox
    deep = root / 'src/one/two/three/a.py'
    deep.parent.mkdir(parents=True)
    deep.write_text('SECRET', encoding='utf-8')
    dispatcher.policy.public_sources |= {'src/one/two/three/a.py'}
    result = call(dispatcher, 'search_code', path='src', query='SECRET')
    assert result.truncated and result.data['matches'] == []
