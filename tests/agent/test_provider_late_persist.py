"""Hold at native finalization boundary must not durably commit an answer.

Synthetic completed response, real finalizer and native SQLite message flush;
no provider traffic or transport timing interposition.
"""
import sqlite3

from agent import provider_control as pc
from agent.turn_finalizer import finalize_turn
from hermes_state import SessionDB
from tests.agent.test_load_time_durability_stamp_92231 import _make_flush_agent
from tests.agent.test_turn_finalizer_iteration_limit_exit import _LimitAgent


def test_hold_before_native_finalizer_persist_rejects_late_answer(tmp_path, monkeypatch):
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
    agent._cleanup_task_resources = hold_before_persist

    @pc.controlled_turn
    def finish(agent, user_message):
        return finalize_turn(agent, final_response='late answer', api_call_count=1,
            interrupted=False, failed=False,
            messages=[dict(role='user', content=user_message)], conversation_history=[],
            effective_task_id='fixture', turn_id='fixture', user_message=user_message,
            original_user_message=user_message, _should_review_memory=False,
            _turn_exit_reason='text_response(fixture)')
    try:
        result = finish(agent, 'pending input')
        assert result['interrupted'] and not result['completed']
        rows = db.get_messages(agent.session_id)
        assert any(row['role']=='user' for row in rows), rows
        assert not any(row['role']=='assistant' and row['content']=='late answer' for row in rows), rows
        assert 'late answer' not in repr(result), result
    finally:
        db.close()
