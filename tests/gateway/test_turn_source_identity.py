"""Gateway source identity reaches the generic turn-end hook boundary."""
from types import SimpleNamespace
from typing import Any, cast

from gateway.config import Platform
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext


def test_turn_runner_binds_exact_inbound_source_before_agent_run():
    captured = {}

    class Agent:
        _current_source_identity = None

        def run_conversation(self, message, **kwargs):
            captured.update(message=message, kwargs=kwargs, source=self._current_source_identity)
            return {"final_response": "done"}

    runner = SimpleNamespace(_consume_pending_native_image_paths=lambda _: [])
    ctx = TurnContext(
        source=SessionSource(platform=Platform.SLACK, chat_id="C0BTEFMAAJX"),
        message="decision", session_key="slack:C0BTEFMAAJX", session_id="source-session",
        inbound_message_id="1789000000.123456", inbound_internal=False,
        persist_user_timestamp=1789000000.123456, mute_notification_reply=False,
    )
    result = TurnRunner(cast(Any, runner), ctx)._run_conversation_with_approval(
        Agent(), [], None, None, None)
    assert result["final_response"] == "done"
    assert captured["source"] == {
        "platform": "slack", "channel_id": "C0BTEFMAAJX",
        "request_id": "1789000000.123456", "timestamp": 1789000000.123456,
        "internal": False,
    }
