"""Recall scope is independent of caller storage on every native dispatch seam."""
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_state import SessionDB
from tools.session_search_tool import session_search

SEAMS = ('tool', 'registry', 'inline', 'invoke', 'sequential')


@pytest.fixture
def stores(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    def no_network(*args, **kwargs):
        raise AssertionError('synthetic recall must not access network')
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    monkeypatch.setattr(socket, 'getaddrinfo', no_network)
    paths = {}
    for profile in ('default', 'alpha', 'beta', 'empty'):
        folder = home if profile == 'default' else home / 'profiles' / profile
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / 'state.db'
        db = SessionDB(path)
        if profile != 'empty':
            sid = f'fixture-{profile}'
            db.create_session(sid, source='cli')
            db.append_message(sid, role='user', content=f'provenancemarker{profile}')
            db.create_session(f'{sid}-child', source='cli', parent_session_id=sid)
            db.append_message(f'{sid}-child', role='user', content=f'childmarker{profile}')
        db.close()
        paths[profile] = path
    for profile in ('missingdb', 'corrupt'):
        folder = home / 'profiles' / profile
        folder.mkdir()
        if profile == 'corrupt':
            (folder / 'state.db').write_bytes(b'not sqlite')
    from tools import session_search_tool
    resolve = session_search_tool._resolve_profile_db
    opened = []
    def tracked_resolve(profile):
        db = resolve(profile)
        if db is not None:
            assert db.read_only
            opened.append(db)
        return db
    monkeypatch.setattr(session_search_tool, '_resolve_profile_db', tracked_resolve)
    yield paths
    assert all(db._conn is None for db in opened), 'tool-owned handles leaked'


def _call(seam, args, local_db, acquire=None):
    if seam == 'registry':
        from tools.registry import registry
        return json.loads(registry.dispatch('session_search', args, db=local_db))
    if seam == 'tool':
        return json.loads(session_search(db=local_db, **args))
    from run_agent import AIAgent
    with (patch('model_tools.get_tool_definitions', return_value=[]),
          patch('model_tools.check_toolset_requirements', return_value={}),
          patch('agent.process_bootstrap.OpenAI')):
        agent = AIAgent(api_key='test-key', base_url='http://localhost:1/v1',
                        quiet_mode=True, skip_context_files=True, skip_memory=True,
                        session_db=local_db, session_id='active-fixture', platform='acp')
    agent._get_session_db_for_recall = acquire or (lambda: local_db)
    if seam == 'inline':
        from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
        return json.loads(INLINE_TOOL_EXECUTORS['session_search'](
            agent, args, InlineToolContext(effective_task_id='fixture')))
    if seam == 'invoke':
        return json.loads(agent._invoke_tool('session_search', args, 'fixture'))
    tc = SimpleNamespace(id='recall-fixture', function=SimpleNamespace(
        name='session_search', arguments=json.dumps(args)))
    messages = []
    agent._execute_tool_calls_sequential(SimpleNamespace(tool_calls=[tc]), messages, 'fixture')
    return json.loads(messages[-1]['content'])


@pytest.mark.parametrize('seam', SEAMS)
@pytest.mark.parametrize('caller', ['healthy', 'absent', 'unavailable', 'corrupt'])
@pytest.mark.parametrize('mode', ['browse', 'read', 'search', 'qualified', 'override', 'errors', 'filters', 'unscoped'])
def test_scope_owns_storage_not_caller(stores, monkeypatch, seam, caller, mode):
    local = SessionDB(stores['default'], read_only=True) if caller == 'healthy' else None
    calls = []
    def acquire():
        calls.append(True)
        if caller == 'corrupt':
            return SessionDB(stores['default'].parent / 'profiles' / 'corrupt' / 'state.db', read_only=True)
        if caller == 'unavailable':
            raise OSError('caller unavailable')
        return local
    monkeypatch.setattr('hermes_state_registry.acquire', acquire)
    home = stores['default'].parent
    before = {p: p.read_bytes() for p in home.rglob('state.db')}
    try:
        if mode == 'unscoped':
            # Native inline getter returns None on unavailable storage; tool handles acquisition errors.
            result = _call(seam, {}, local, lambda: local)
            if local:
                assert result['success'] is True
                assert {r['session_id'] for r in result['results']} == {'fixture-default'}  # Native browse collapses lineage.
                miss = _call(seam, {'session_id': 'fixture-beta'}, local)
                assert miss['success'] is False  # No foreign bare-ID scan.
            else:
                assert result['success'] is False
            return
        if mode == 'errors':
            for profile in ('nonexistent', 'missingdb', 'corrupt', '../alpha', '/absolute', 'bad\\name'):
                for args in ({}, {'query': 'provenancemarkerdefault'}, {'session_id': 'fixture-default'},
                             {'session_id': 'fixture-default', 'around_message_id': 1}):
                    result = _call(seam, dict(args, profile=profile), local, acquire)
                    assert result['success'] is False and result['error'], result
                    assert 'provenancemarkerdefault' not in json.dumps(result)
            for profile in ('empty', 'alpha'):
                result = _call(seam, {'profile': profile, 'query': 'nonmatchingfixturetoken'}, local, acquire)
                assert result['success'] is True and result['results'] == []
            result = _call(seam, {'profile': 'empty'}, local, acquire)
            assert result['success'] is True and result['count'] == 0
        else:
            # A -> B -> A makes cached or owner-bound routing visible.
            for profile in ('alpha', 'beta', 'alpha'):
                sid, marker = f'fixture-{profile}', f'provenancemarker{profile}'
                args = {'profile': profile}
                if mode == 'read': args['session_id'] = sid
                if mode in ('search', 'filters'): args.update(query=marker, detail='full')
                if mode == 'qualified': args = {'session_id': f'{profile}/{sid}'}
                if mode == 'override': args['session_id'] = f'nonexistent/{sid}'
                result = _call(seam, args, local, acquire)
                assert result['success'] is True, result
                target = next(r for r in result['results'] if r['session_id'] == sid) if 'results' in result else result
                assert target['link'] == f'@session:{profile}/{sid}'
                assert marker in json.dumps(result), result
                assert 'provenancemarkerdefault' not in json.dumps(result)
                if mode == 'filters':
                    for bounds in ({'after': '2999-01-01'}, {'before': '1970-01-01'}, {'exclude_session_ids': [sid]}):
                        filtered = _call(seam, dict(args, **bounds), local, acquire)
                        assert filtered['success'] is True and filtered['results'] == [], filtered
                    invalid = _call(seam, dict(args, after='not-a-time'), local, acquire)
                    assert invalid['success'] is False
        assert calls == []  # Explicit scope never even attempts caller acquisition.
    finally:
        assert {p: p.read_bytes() for p in home.rglob('state.db')} == before
        if local:
            assert local.get_session('fixture-default')  # Caller retains ownership on all paths.
            local.close()


@pytest.mark.parametrize('seam', SEAMS)
@pytest.mark.parametrize('rebind', [False, True])
@pytest.mark.parametrize('healthy_local', [False, True])
def test_scroll_provenance_follows_anchor_owner(stores, monkeypatch, seam, rebind, healthy_local):
    target = SessionDB(stores['alpha'], read_only=True)
    owner = 'fixture-alpha-child' if rebind else 'fixture-alpha'
    anchor = target._conn.execute('SELECT id FROM messages WHERE session_id=?', (owner,)).fetchone()[0]
    target.close()
    def unavailable():
        raise AssertionError('explicit scroll must not acquire caller database')
    monkeypatch.setattr('hermes_state_registry.acquire', unavailable)
    local = SessionDB(stores['default'], read_only=True) if healthy_local else None
    before = {p: p.read_bytes() for p in stores.values()}
    try:
        result = _call(seam, {'session_id': 'alpha/fixture-alpha', 'around_message_id': anchor}, local, unavailable)
        assert result['success'] is True, result
        assert result['session_id'] == owner
        assert result['messages'][0]['content'] == ('childmarkeralpha' if rebind else 'provenancemarkeralpha')
        assert result.get('link') == f'@session:alpha/{owner}', result
        assert bool(result.get('warning')) == rebind
    finally:
        assert {p: p.read_bytes() for p in stores.values()} == before
        if local:
            assert local.get_session('fixture-default')
            local.close()
