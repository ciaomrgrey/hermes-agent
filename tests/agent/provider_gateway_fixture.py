"""Disposable gateway TurnRunner driver; loopback provider, no delivery adapter."""
import json
import os
from types import SimpleNamespace

from run_agent import AIAgent
from hermes_state import SessionDB
from gateway.run_turn_runner import TurnRunner
from gateway.turn_context import TurnContext
from gateway.session import SessionSource
from gateway.config import Platform


def main():
    query = os.environ['FIXTURE_QUERY']
    db = SessionDB()
    agent = AIAgent(provider='anthropic', model='claude-sonnet-4-5',
                    base_url=os.environ['ANTHROPIC_BASE_URL'],
                    api_key=os.environ['ANTHROPIC_API_KEY'],
                    session_db=db, session_id='synthetic-gateway',
                    max_iterations=1, enabled_toolsets=[], skip_memory=True,
                    skip_context_files=True, skip_background_review=True,
                    quiet_mode=True)
    source = SessionSource(platform=Platform.LOCAL, chat_id='fixture', user_id='fixture')
    ctx = TurnContext(source=source, message=query, session_id=agent.session_id,
                      session_key='synthetic-key')
    runner = TurnRunner(SimpleNamespace(_consume_pending_native_image_paths=lambda key: []), ctx)
    try:
        result = runner._run_conversation_with_approval(agent, [], None, None, None)
        print(json.dumps(result, default=str))
        assert result['interrupted'] and result['completed'] is False
        assert result['failure_reason'] == 'provider_held'
        assert result['agent_persisted'] is True
        assert not any(m['role'] == 'assistant' for m in result['messages'])
        sealed = []
        runner._finish_stream_consumer(result, [], SimpleNamespace(finish=lambda *args: sealed.append(args)))
        assert sealed == [()]
    finally:
        agent.close()
        db.close()


if __name__ == '__main__':
    main()
