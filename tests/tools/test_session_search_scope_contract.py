"""Real-store recall provenance through the tool and both agent dispatch seams."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_state import SessionDB
from tools.session_search_tool import session_search

PROFILES = ('emma', 'sophia', 'jared', 'plutus', 'cody', 'generalist', 'gurney', 'ripley')


@pytest.fixture
def stores(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    paths = {}
    for profile in ('default', *PROFILES):
        folder = home if profile == 'default' else home / 'profiles' / profile
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / 'state.db'
        db = SessionDB(path)
        sid = f'fixture-{profile}'
        db.create_session(sid, source='cli')
        db.append_message(sid, role='user', content=f'provenancemarker{profile}')
        db._conn.commit()
        db.close()
        paths[profile] = path
    return paths


def _call(seam, args, local_db):
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
    def local():
        if local_db is None:
            raise AssertionError('explicit scope must not acquire the caller DB')
        return local_db
    agent._get_session_db_for_recall = local
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


@pytest.mark.parametrize('seam', ['tool', 'registry', 'inline', 'invoke', 'sequential'])
@pytest.mark.parametrize('profile', PROFILES)
def test_explicit_scope_independent_of_local_database(stores, monkeypatch, seam, profile):
    def unavailable():
        raise AssertionError('explicit scope must not acquire the caller DB')
    monkeypatch.setattr('hermes_state_registry.acquire', unavailable)
    before = {p: path.read_bytes() for p, path in stores.items()}
    sid = f'fixture-{profile}'
    marker = f'provenancemarker{profile}'
    browse = _call(seam, {'profile': profile}, None)
    assert browse['success'] is True, browse
    assert [r['session_id'] for r in browse['results']] == [sid]
    read = _call(seam, {'profile': profile, 'session_id': sid}, None)
    assert read['success'] is True, read
    assert read['messages'][0]['content'] == marker
    search = _call(seam, {'profile': profile, 'query': marker, 'detail': 'full'}, None)
    assert search['success'] is True, search
    assert [r['session_id'] for r in search['results']] == [sid]
    assert marker in json.dumps(search)
    scroll = _call(seam, {'profile': profile, 'session_id': sid,
                          'around_message_id': read['messages'][0]['id']}, None)
    assert scroll['success'] is True, scroll
    assert scroll['messages'][0]['content'] == marker
    for result in (browse['results'][0], search['results'][0], read, scroll):
        assert result['link'] == f'@session:{profile}/{sid}'
    linked = _call(seam, {'session_id': f'{profile}/{sid}'}, None)
    assert linked['messages'] == read['messages']
    assert linked['link'] == read['link']
    assert {p: path.read_bytes() for p, path in stores.items()} == before


@pytest.mark.parametrize('seam', ['tool', 'registry', 'inline', 'invoke', 'sequential'])
@pytest.mark.parametrize('healthy_local', [False, True])
def test_scope_errors_never_fallback_or_create_stores(stores, monkeypatch, seam, healthy_local):
    home = stores['default'].parent
    for name in ('missingdb', 'corrupt', 'unreadable', 'empty'):
        folder = home / 'profiles' / name
        folder.mkdir()
        if name == 'corrupt':
            (folder / 'state.db').write_bytes(b'not a sqlite database')
        elif name in ('unreadable', 'empty'):
            seed = SessionDB(folder / 'state.db')
            seed.close()
    unreadable = home / 'profiles' / 'unreadable' / 'state.db'
    unreadable.chmod(0)
    local = SessionDB(stores['default'], read_only=True) if healthy_local else None
    acquired = []
    monkeypatch.setattr('hermes_state_registry.acquire', lambda: acquired.append(True))
    before = {p: p.read_bytes() for p in home.rglob('state.db') if p != unreadable}
    try:
        for profile in ('hermes', 'nonexistent', 'missingdb', 'corrupt', 'unreadable',
                        '../emma', 'emma/../../default', '/absolute', 'bad\\name'):
            for args in ({}, {'query': 'provenancemarkerdefault'},
                         {'session_id': 'fixture-default'},
                         {'session_id': 'fixture-default', 'around_message_id': 1}):
                result = _call(seam, dict(args, profile=profile), local)
                assert result['success'] is False, (profile, args, result)
                assert result['error'] and 'Session database not available' not in result['error']
                assert 'provenancemarkerdefault' not in json.dumps(result)
        for profile in ('empty', 'emma'):
            empty = _call(seam, {'profile': profile, 'query': 'nonmatchingfixturetoken'}, local)
            assert empty['success'] is True and empty['results'] == []
        empty = _call(seam, {'profile': 'empty'}, local)
        assert empty['success'] is True and empty['count'] == 0
        for profile in ('emma', 'empty'):
            miss = _call(seam, {'profile': profile, 'session_id': 'fixture-sophia'}, local)
            assert miss['success'] is False and 'not found' in miss['error']
        assert acquired == []
        assert {p: p.read_bytes() for p in home.rglob('state.db') if p != unreadable} == before
        if local:
            # Supplied connections belong to the caller, even on errors.
            assert local.get_session('fixture-default')
            own = _call(seam, {}, local)
            assert [r['session_id'] for r in own['results']] == ['fixture-default']
            miss = _call(seam, {'session_id': 'fixture-sophia'}, local)
            assert miss['success'] is False
    finally:
        unreadable.chmod(0o600)
        if local:
            local.close()
