"""A length-continuation the turn-end seam did not rewrite replays the pieces the model wrote.

Through a real ``AIAgent`` + ``SessionDB`` against the loopback model and a resumed second turn:
the user sees (and history shows) the stitched answer, while the next request's wire history keeps
each fragment exactly once (``publish_held_final_row`` keeps the last piece in ``api_content``).
The estate ``before_turn_end``/``append_turn_footer`` seam must not overwrite the row with the
stitched text when nothing rewrote it. Controls: an ordinary single-piece reply, a real rewrite and
an appended footer keep their delivered == stored contract.
"""

from tests.agent.test_hold_stream_exits import _agent, held
from tests.fakes.fake_llm_provider import Text

FIRST = "The first part is complete. "
REST = "The second part completes the answer."

_NOOP = "def same(response_text, **_):\n    return None\n"
_HELD = "    ctx.register_hook('transform_llm_output', same, hold_stream=True)\n"
_GATE = "def gate(final_response, **_):\n    return None\n"
_FOOTER = "def footer(response_text, **_):\n    return '\\n\\n-- footer'\n"
_UPPER = "def same(response_text, **_):\n    return response_text.upper()\n"


def _plugin(*, gate=False, footer=False, transform=_NOOP):
    body = transform + (_GATE if gate else "") + (_FOOTER if footer else "") + "def register(ctx):\n" + _HELD
    if gate:
        body += "    ctx.register_hook('before_turn_end', gate)\n"
    if footer:
        body += "    ctx.register_hook('append_turn_footer', footer)\n"
    return body


def _resume(server, db, rows_session="held-exit"):
    rows = db.get_messages_as_conversation(rows_session)
    _agent(server, db, rows_session).run_conversation("thanks", conversation_history=rows)
    return [m["content"] for m in server.main_requests()[-1]["messages"] if m["role"] == "assistant"]


def _stored_final(db):
    return [r["content"] for r in db.get_messages("held-exit") if r["role"] == "assistant"][-1]


def test_unrewritten_continuation_replays_each_piece_once_without_gate(held):
    result, _, _, server, db = held(
        [Text(FIRST, finish_reason="length"), Text(REST), Text("Acknowledged.")], plugin_body=_plugin())
    assert result["final_response"] == FIRST + REST
    assert _stored_final(db) == FIRST + REST
    assert _resume(server, db) == [FIRST.strip(), REST]


def test_unrewritten_continuation_replays_each_piece_once_through_turn_end_gate(held):
    result, _, _, server, db = held(
        [Text(FIRST, finish_reason="length"), Text(REST), Text("Acknowledged.")], plugin_body=_plugin(gate=True))
    assert result["final_response"] == FIRST + REST
    assert _stored_final(db) == FIRST + REST
    assert _resume(server, db) == [FIRST.strip(), REST]


def test_control_single_piece_reply_through_gate_is_unchanged(held):
    result, _, _, server, db = held([Text(REST), Text("Acknowledged.")], plugin_body=_plugin(gate=True))
    assert result["final_response"] == REST
    assert _stored_final(db) == REST
    assert _resume(server, db) == [REST]


def test_control_rewritten_continuation_stores_and_replays_the_rewrite(held):
    result, _, _, server, db = held(
        [Text(FIRST, finish_reason="length"), Text(REST), Text("Acknowledged.")],
        plugin_body=_plugin(gate=True, transform=_UPPER))
    assert result["final_response"] == (FIRST + REST).upper()
    assert _stored_final(db) == (FIRST + REST).upper()
    assert _resume(server, db)[-1] == (FIRST + REST).upper()


def test_control_footer_is_delivered_and_stored_while_model_keeps_its_last_piece(held):
    result, _, _, server, db = held(
        [Text(FIRST, finish_reason="length"), Text(REST), Text("Acknowledged.")],
        plugin_body=_plugin(gate=True, footer=True))
    assert result["final_response"] == FIRST + REST + "\n\n-- footer"
    assert _stored_final(db) == FIRST + REST + "\n\n-- footer"
    assert _resume(server, db) == [FIRST.strip(), REST]
