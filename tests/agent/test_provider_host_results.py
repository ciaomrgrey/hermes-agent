"""Native host consumers must receive resumable cancellation, not daemon death."""
import sqlite3
from types import SimpleNamespace

from agent import provider_control as pc
from agent.turn_facade import TurnFacadeMixin


def _policy(tmp_path,monkeypatch):
    db=tmp_path/'holds.sqlite3'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE holds(provider TEXT,since REAL,incident TEXT)')
        conn.execute('INSERT INTO holds VALUES(?,?,?)',('anthropic',1,'fixture'))
    policy=pc.Policy(db,('anthropic',))
    monkeypatch.setattr(pc,'current_policy',lambda:policy)
    return policy


class HeldAgent(TurnFacadeMixin):
    provider='anthropic'
    session_id='fixture-session'


def test_cli_worker_preserves_pending_turn_and_returns_interrupted(tmp_path,monkeypatch):
    from hermes_cli.cli_chat_turn_mixin import CLIChatTurnMixin
    _policy(tmp_path,monkeypatch)
    history=[{'role':'user','content':'resume this exact pending input'}]
    cli=SimpleNamespace(agent=HeldAgent(),conversation_history=history,session_id='fixture-session',
        _sudo_password_callback=None,_approval_callback=None,_secret_capture_callback=None,
        _vault_unlock_callback=None,_vault_save_login_callback=None,_vault_code_callback=None,
        _flush_credit_notices=lambda:None)
    turn=SimpleNamespace(voice_prefix='',stream_callback=None,result=None)
    CLIChatTurnMixin._chat_run_agent(cli,turn,history[-1]['content'])
    assert turn.result['interrupted'] and turn.result['completed'] is False
    assert turn.result['failure_reason']=='provider_held'
    assert turn.result['messages']==history
    assert cli.conversation_history==history
    pending, marker=CLIChatTurnMixin._chat_resolve_interrupt(cli,turn,SimpleNamespace(is_alive=lambda:False),None,'')
    assert pending is None and not marker, 'hold diagnostic must not become an automatic next prompt'
    assert cli._last_turn_interrupted is True


def test_gateway_turn_owner_keeps_input_and_does_not_seal_stream(tmp_path,monkeypatch):
    from gateway.run_turn_runner import TurnRunner
    from gateway.turn_context import TurnContext
    from gateway.session import SessionSource
    from gateway.config import Platform
    _policy(tmp_path,monkeypatch)
    source=SessionSource(platform=Platform.LOCAL,chat_id='fixture',user_id='fixture')
    ctx=TurnContext(source=source,message='pending gateway input',session_id='fixture-session',session_key='fixture-key')
    runner=TurnRunner(SimpleNamespace(_consume_pending_native_image_paths=lambda key:[]),ctx)
    result=runner._run_conversation_with_approval(HeldAgent(),[],None,None,None)
    assert result['interrupted'] and not result['completed']
    assert result['messages']==[{'role':'user','content':'pending gateway input'}]
    assert result['agent_persisted'] is False
    sealed=[]
    runner._finish_stream_consumer(result,[],SimpleNamespace(finish=lambda *args:sealed.append(args)))
    assert sealed==[()], 'cancel diagnostic sealed as completed model answer'


def test_active_hold_discards_result_after_callable_returns(tmp_path,monkeypatch):
    policy=_policy(tmp_path,monkeypatch)
    policy.resume('anthropic')
    agent=HeldAgent()
    @pc.controlled_turn
    def body(agent,user_message,conversation_history=None):
        with sqlite3.connect(policy.database) as conn:
            conn.execute('INSERT INTO holds VALUES(?,?,?)',('anthropic',1,'fixture'))
        return {'final_response':'late-result','messages':[{'role':'assistant','content':'late-result'}]}
    result=body(agent,'pending')
    assert result['interrupted'] and result['completed'] is False
    assert 'late-result' not in repr(result)
