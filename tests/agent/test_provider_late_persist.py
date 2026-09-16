"""Hold at native finalization boundary must not durably commit an answer.

Synthetic completed response, real finalizer and native SQLite message flush;
no provider traffic or transport timing interposition.
"""
import sqlite3

import pytest

from agent import provider_control as pc
from agent.turn_finalizer import finalize_turn
from hermes_state import SessionDB
from tests.agent.test_load_time_durability_stamp_92231 import _make_flush_agent
from tests.agent.test_turn_finalizer_iteration_limit_exit import _LimitAgent


@pytest.mark.parametrize('hold_edge', ['cleanup', 'before_finalize', 'write_failure', 'output_hook', 'compression', 'fallback', 'replaced'])
def test_hold_before_native_finalizer_persist_rejects_late_answer(tmp_path, monkeypatch, hold_edge):
    control = tmp_path / 'holds.sqlite3'
    with sqlite3.connect(control) as conn:
        conn.execute('CREATE TABLE holds(provider TEXT, since REAL, incident TEXT)')
    policy = pc.Policy(control, ('anthropic',))
    monkeypatch.setattr(pc, 'current_policy', lambda: policy)
    monkeypatch.setattr('hermes_cli.plugins.invoke_hook', lambda *a, **kw: [])
    db = SessionDB(db_path=tmp_path / 'state.db')
    db.create_session('late-persist', source='cli')
    agent = _LimitAgent()
    agent.provider = 'anthropic'
    agent.session_id = 'late-persist'
    flush_agent = _make_flush_agent(db, agent.session_id)
    # Use native durability, not a mocked persistence recorder.
    agent._persist_session = lambda messages, history: flush_agent._flush_messages_to_session_db(messages, history)
    def hold_before_persist(*args):
        with sqlite3.connect(control) as conn:
            conn.execute('INSERT INTO holds VALUES(?,?,?)', ('anthropic', 1, 'synthetic'))
    agent._cleanup_task_resources = hold_before_persist if hold_edge in ('cleanup', 'compression', 'fallback') else lambda *a: None
    if hold_edge == 'replaced':
        agent._cleanup_task_resources = lambda *a: setattr(db, '_db_replaced', True)
    if hold_edge == 'compression':
        agent.context_compressor._micro_compact_enabled = True
        agent.context_compressor._micro_compact = lambda messages: pc.check_request('anthropic')
    trajectories = []
    agent._save_trajectory = lambda messages, query, completed: trajectories.append(completed)

    @pc.controlled_turn
    def finish(agent, user_message):
        if hold_edge == 'fallback':
            agent.provider = 'xai-oauth'
        if hold_edge == 'before_finalize':
            hold_before_persist()
        messages = [dict(role='user', content=user_message)]
        agent._persist_session(messages, [])  # real native turn-start durability
        agent._session_messages = messages
        return finalize_turn(agent, final_response='late answer', api_call_count=1,
            interrupted=False, failed=False,
            messages=messages, conversation_history=[],
            effective_task_id='fixture', turn_id='fixture', user_message=user_message,
            original_user_message=user_message, _should_review_memory=False,
            _turn_exit_reason='text_response(fixture)')
    hooks = []
    if hold_edge == 'output_hook':
        def hook(name, **kwargs):
            hooks.append(name)
            if name == 'transform_llm_output':
                hold_before_persist()
            return []
        monkeypatch.setattr('hermes_cli.lifecycle.invoke_hook', hook)
    if hold_edge == 'write_failure':
        # Genuine SQLite transaction abort, not a persistence recorder/fake return.
        db._conn.execute("""CREATE TRIGGER fail_fixture_answer BEFORE INSERT ON messages
            WHEN NEW.role='assistant' BEGIN
            SELECT RAISE(ABORT, 'synthetic fixture refuses assistant insert'); END""")
    try:
        result = finish(agent, 'pending input')
        assert result['interrupted'] and not result['completed']
        if hold_edge == 'replaced':
            from hermes_constants import get_hermes_home
            # Inject native replaced latch; exercise actual native recovery-file writer.
            recovery = get_hermes_home()/'sessions'/'late-persist.jsonl'
            assert not recovery.exists() or 'late answer' not in recovery.read_text()
            assert 'late answer' not in repr(result)
            return
        rows = db.get_messages(agent.session_id)
        assert any(row['role']=='user' for row in rows), rows
        if hold_edge == 'output_hook':
            assert 'post_llm_call' not in hooks and 'on_session_end' not in hooks, hooks
            assert result['final_response'] != 'late answer'
            assert any(row['content']=='late answer' for row in rows)  # legitimate pre-hold commit
            return
        assert not any(row['role']=='assistant' and row['content']=='late answer' for row in rows), rows
        assert 'late answer' not in repr(result), result
        assert 'late answer' not in repr(agent._session_messages)
        if hold_edge == 'before_finalize':
            assert not any(trajectories), trajectories
    finally:
        db.close()
