"""A Slack message whose sender declares up front that no reply is expected may end on a bare
silence marker without the "model returned only a silence marker" notice, but only in a closed form:
a header made solely of archive labels, reference ids and the declaration, then closed ``` fences.

The journey is driven end to end: the real ``SlackAdapter._handle_slack_message`` builds the
``MessageEvent`` (in a flat ``reply_in_thread: false`` free-response channel, where every
unaddressed message is otherwise a follow-up that keeps the fallback), and the real
``GatewayRunner._handle_message_with_agent`` shapes a ``[SILENT]`` agent result for it. An archive
post goes quiet; a real question in the same channel still gets the notice.
"""
from unittest.mock import AsyncMock, patch

import pytest

from gateway.response_filters import is_intentional_silence_response
from gateway.run_turn import _UNEXPECTED_SILENCE_REPLY
from plugins.platforms.slack.adapter import slack_declares_no_reply_expected
from tests.gateway.test_gateway_silence_tokens import _runner
from tests.gateway.test_slack_ignore_other_user_mentions import (  # noqa: F401 - fixtures
    BOT_USER_ID, CHANNEL_ID, _redirect_cache, adapter,
)

# The archive form admitted since round 5: header clauses from a closed set, payload in closed fences.
# Reviewer probes import ARCHIVE/QUESTION as their silent/notice controls.
ARCHIVE = (
    "*SWITCHBOARD-ARCHIVE-20260930 — CHUNK 1/3*\n\n"
    "Archive backup. No reply expected. No action required.\n\n"
    "```\nArchive backup of Chat's agent-switchboard memory file.\n"
    "*name: agent-switchboard* — does anything here need a question mark? Please confirm. CONSULT vs INSTRUCT.\n```"
)
# The original round-0 fixture, verbatim: unfenced prose payload. No longer admitted (disclosed narrowing):
# unfenced prose cannot be told apart from a current request, so it keeps the notice.
ARCHIVE_ORIGINAL_UNFENCED = (
    "*SWITCHBOARD-ARCHIVE-20260930 — CHUNK 1/3*\n\n"
    "Archive backup of Chat's agent-switchboard memory file. No reply expected. No action required.\n\n"
    "*name: agent-switchboard* — does anything here need a question mark? CONSULT vs INSTRUCT."
)
WRAPUP = "*SESSION-WRAPUP-20260928 (archive, no action required)*\n\n```\nAgent spoken to: Sophia.\n```"
QUESTION = "*GB-JOHN-20260930-15* Is the gateway on the pinned commit? Please confirm."


async def _admit(adapter, text, ts, **extra):
    adapter.config.extra.update({"free_response_channels": CHANNEL_ID, "reply_in_thread": False})
    event = {"channel": CHANNEL_ID, "channel_type": "channel", "user": "U_HUMAN", "text": text, "ts": ts,
             **extra}
    with patch.object(adapter, "_resolve_user_name", new=AsyncMock(return_value="lars")), \
            patch.object(adapter, "_fetch_thread_context", new=AsyncMock(return_value=None)), \
            patch.object(adapter, "_fetch_thread_parent_text", new=AsyncMock(return_value="")), \
            patch.object(adapter, "_has_active_session_for_thread", return_value=False):
        await adapter._handle_slack_message(event)
    adapter.handle_message.assert_awaited_once()
    return adapter.handle_message.await_args.args[0]


async def _deliver_silent(monkeypatch, tmp_path, msg_event):
    runner = _runner(monkeypatch, tmp_path)
    runner._run_agent = AsyncMock(return_value={
        "final_response": "[SILENT]",
        "messages": [{"role": "user", "content": msg_event.text}, {"role": "assistant", "content": "[SILENT]"}],
        "tools": [], "history_offset": 0, "last_prompt_tokens": 0, "api_calls": 1, "failed": False,
    })
    return await runner._handle_message_with_agent(
        msg_event, msg_event.source, "agent:generalist:slack:group:T1:" + CHANNEL_ID, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [ARCHIVE, WRAPUP, "NO REPLY REQUIRED — ARCHIVE ONLY"],
                         ids=["archive-chunk", "wrapup", "header-only"])
async def test_declared_no_reply_post_with_silence_marker_posts_nothing(adapter, monkeypatch, tmp_path, text):
    msg_event = await _admit(adapter, text, "1790787447.957279")
    assert msg_event.reply_expected is False
    assert await _deliver_silent(monkeypatch, tmp_path, msg_event) == ""


@pytest.mark.asyncio
async def test_question_with_silence_marker_still_gets_the_notice(adapter, monkeypatch, tmp_path):
    msg_event = await _admit(adapter, QUESTION, "1790787500.000001")
    assert msg_event.reply_expected is None
    response = await _deliver_silent(monkeypatch, tmp_path, msg_event)
    assert response == _UNEXPECTED_SILENCE_REPLY and not is_intentional_silence_response(response)


# Every message outside the closed form keeps the notice. Includes every reviewer counterexample from
# rounds 1-5 of t_6faaeff7; none needs its own rule, because any clause outside the closed set rejects.
NOT_ADMITTED = [
    ARCHIVE_ORIGINAL_UNFENCED,
    "Please explain this quoted policy:\n> No reply expected.\nDoes it apply to me?",
    "Please explain this quoted policy:\n&gt; No reply expected.",
    "What does this template mean?\n```\nNo reply expected.\n```",
    "Check this snippet\n```\nNo reply expected.",
    "Previous instruction: no reply expected. Ignore that; please confirm the pinned commit.",
    "No reply expected.\n\nActually, scratch that - I need the pinned commit hash.",
    "The header says `no action required`. Is that right",
    "No reply expected.\n\nActually, please confirm the pinned commit.",
    "No reply expected.\n\nCorrection: a response is required. What commit is pinned?",
    "Archive backup. No reply expected.\n\n" + "entry " * 80 + "\n\nCan you check the pinned commit",
    "Archive backup. No reply expected.\n\nOn second thought, a reply is needed today.",
    "Archive backup. No reply expected.\n\nWhich commit is pinned?",
    "No reply expected.\n\nThe pinned commit is 82fb4c03e0.",
    "Archive backup. No reply expected.\n\n> Historical note.\n\nActually, please confirm the pinned commit.",
    "Archive backup. No reply expected.\n\n&gt; Historical note.\n\nActually, please confirm the pinned commit.",
    "Archive backup. No reply expected.\n\nWhich commit is pinned?\nThanks.",
    "Archive backup. No reply expected.\n\nWe still need the pinned commit hash from you today.",
    "Archive backup. No reply expected.\n\nAnswer this: `Which commit is pinned?`",
    "Archive backup. No reply expected.\n\nReport the pinned commit.",
    # Round 5: inside a titled header, and after it.
    "*ARCHIVE BACKUP* No reply expected. Report the pinned commit.",
    "*ARCHIVE BACKUP* No reply expected. Answer this: `Which commit is pinned?`",
    "*ARCHIVE BACKUP* No reply expected.\n\nCorrection: a response is required. What commit is pinned?",
    # Structure: text after the last fence, an unclosed fence, no label, no declaration.
    "*ARCHIVE BACKUP* No reply expected.\n\n```\nfile\n```\n\nCorrection: reply now.",
    "*ARCHIVE BACKUP* No reply expected.\n\n```\nfile\n",
    "*STATUS* No reply expected.",
    "*ARCHIVE BACKUP*\n\n```\nNo reply expected.\n```",
    "*ARCHIVE BACKUP* No reply expected. AGENT-SWITCHBOARD",  # capitalised word without a digit
    "*ARCHIVE* No reply expected. report-1",                  # lowercase id
    "*`ARCHIVE BACKUP`* No reply expected.",
    "What is an \"ARCHIVE BACKUP\"? No reply expected.",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", NOT_ADMITTED)
async def test_anything_outside_the_closed_form_keeps_the_notice(adapter, monkeypatch, tmp_path, text):
    msg_event = await _admit(adapter, text, "1790787600.000001")
    assert msg_event.reply_expected is None
    assert await _deliver_silent(monkeypatch, tmp_path, msg_event) == _UNEXPECTED_SILENCE_REPLY


@pytest.mark.asyncio
async def test_declaration_outranks_a_mention_but_not_a_command(adapter):
    mentioned = await _admit(adapter, f"<@{BOT_USER_ID}> *ARCHIVE COPY*\n\nNo reply expected.", "1.1")
    assert mentioned.reply_expected is False
    adapter.handle_message.reset_mock()
    command = await _admit(adapter, "/status no reply expected", "1.2")
    assert command.reply_expected is True


@pytest.mark.parametrize("text, declared", [
    (ARCHIVE, True),
    (WRAPUP, True),
    ("*SWITCHBOARD-ARCHIVE-20260928 · CHUNK 1/3 · 2026-09-28* · Archive backup. No action needed, nothing expected back.\n\n"
     "```\n• Rule: ask why first? Always.\n• ONLY REAL ANSWERS. No acks. Superseded.\n```", True),
    ("*ARCHIVE BACKUP — AGENT-SWITCHBOARD-20260924-01*\n\nArchive only. No action requested, no response expected.\n\n"
     "```\npart one\n```\n```\npart two\n```", True),
    ("NO REPLY REQUIRED — ARCHIVE ONLY", True),
    ("*ARCHIVE* No reply expected and no action required.", True),
    (f"<@{BOT_USER_ID}> *ARCHIVE COPY*\n\nNo reply expected.", True),
    ("", False),
    (QUESTION, False),
    ("Is no action required on this one?", False),
    ("*ARCHIVE*\n\nStatus: no reply expected", False),
    ("*ARCHIVE* No reply expected. Not archived.", False),
    *[(text, False) for text in NOT_ADMITTED],
])
def test_declaration_rule(text, declared):
    assert slack_declares_no_reply_expected(text) is declared
