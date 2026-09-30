"""A Slack message whose sender declares up front that no reply is expected may end on a bare
silence marker without the "model returned only a silence marker" notice.

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

ARCHIVE = (
    "*SWITCHBOARD-ARCHIVE-20260930 — CHUNK 1/3*\n\n"
    "Archive backup of Chat's agent-switchboard memory file. No reply expected. No action required.\n\n"
    "*name: agent-switchboard* — CONSULT vs INSTRUCT.\n"
    "> Rule quoted from the file: does anything here need a question mark?\n"
    "```\nTrigger: \"what is open?\" -> ask the agents\n```"
)
WRAPUP = "*SESSION-WRAPUP-20260928 — Saxzi GTM (archive, no action required)*\n\nAgent spoken to: Sophia."
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
@pytest.mark.parametrize("text", [ARCHIVE, WRAPUP], ids=["archive-chunk", "wrapup"])
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


# Mentions, not declarations: someone else's words (quote, code) or a declaration the sender withdraws.
# Each is a real request, so the model's [SILENT] must still surface the notice (review of t_6faaeff7).
MENTIONS = [
    "Please explain this quoted policy:\n> No reply expected.\nDoes it apply to me?",
    "Please explain this quoted policy:\n&gt; No reply expected.",
    "What does this template mean?\n```\nNo reply expected.\n```",
    "Check this snippet\n```\nNo reply expected.",  # unclosed fence runs to the end
    "Previous instruction: no reply expected. Ignore that; please confirm the pinned commit.",
    "No reply expected.\n\nActually, scratch that - I need the pinned commit hash.",
    "The header says `no action required`. Is that right",
    # Round 2: a current request or correction in a later paragraph takes the declaration back.
    "No reply expected.\n\nActually, please confirm the pinned commit.",
    "No reply expected.\n\nCorrection: a response is required. What commit is pinned?",
    "Archive backup. No reply expected.\n\n" + "entry " * 80 + "\n\nCan you check the pinned commit",
    "Archive backup. No reply expected.\n\nOn second thought, a reply is needed today.",
    "Archive backup. No reply expected.\n\nWhich commit is pinned?",  # closes on a question
    "No reply expected.\n\nThe pinned commit is 82fb4c03e0.",  # no archive/FYI genre: not admitted
    # Round 3: a block quote hides only its own line(s); own prose after it still counts. And the
    # sender's own prose must read as a record: any own question or direct address is a request.
    "Archive backup. No reply expected.\n\n> Historical note.\n\nActually, please confirm the pinned commit.",
    "Archive backup. No reply expected.\n\n&gt; Historical note.\n\nActually, please confirm the pinned commit.",
    "Archive backup. No reply expected.\n\n> Historical note.\nSend the pinned commit to me.",
    "Archive backup. No reply expected.\n\nWhich commit is pinned?\nThanks.",
    "Archive backup. No reply expected.\n\nWhich commit is pinned? Thanks, much appreciated.",
    "Archive backup. No reply expected.\n\nWe still need the pinned commit hash from you today.",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", MENTIONS, ids=["blockquote", "slack-escaped-quote", "fenced-code",
                                                "unclosed-fence", "withdrawn-inline", "withdrawn-later",
                                                "inline-code", "actually-please-later", "correction-required-later",
                                                "can-you-far-below", "second-thought-needed", "closing-question",
                                                "no-genre", "request-after-blockquote",
                                                "request-after-escaped-blockquote", "address-after-blockquote",
                                                "question-with-signoff", "question-mid-line", "direct-address"])
async def test_quoted_or_withdrawn_declaration_keeps_the_notice(adapter, monkeypatch, tmp_path, text):
    msg_event = await _admit(adapter, text, "1790787600.000001")
    assert msg_event.reply_expected is None
    assert await _deliver_silent(monkeypatch, tmp_path, msg_event) == _UNEXPECTED_SILENCE_REPLY


@pytest.mark.asyncio
async def test_declaration_outranks_a_mention_but_not_a_command(adapter):
    mentioned = await _admit(adapter, f"<@{BOT_USER_ID}> FYI archive copy. No reply expected.", "1.1")
    assert mentioned.reply_expected is False
    adapter.handle_message.reset_mock()
    command = await _admit(adapter, "/status no reply expected", "1.2")
    assert command.reply_expected is True


@pytest.mark.parametrize("text, declared", [
    (ARCHIVE, True),
    (WRAPUP, True),
    # An archive body may carry archived questions and "no ack" rules, provided the questions are quoted
    # or fenced (someone else's words); an unquoted own question is indistinguishable from a current ask.
    ("Archive backup. No reply expected.\n\n• Rule: ask 'why?' first. Always.\n• ONLY REAL ANSWERS. No acks.", True),
    ("Archive backup. No reply expected.\n\n> Is this still open?\n&gt; Who owns it?\n\nRouting table follows.", True),
    ("Archive backup. No reply expected.\n\n• Rule: ask why first? Always.", False),
    ("Archive backup · No action needed, nothing expected back.", True),
    ("FYI: deploy finished. No response needed.", True),
    ("NO REPLY REQUIRED — archive only", True),
    (QUESTION, False),
    ("Is no action required on this one?", False),
    ("No reply expected? Tell me if there is.", False),
    ("Please explain what 'no action required' means in the card.", False),
    ("a" * 400 + " No reply expected.", False),  # declaration buried past the header is not a header
    ("", False),
    ("Archive only. No action requested, no response expected.", True),
    ("Old note, no action required, but please ack.", False),  # request in the same paragraph
    ("Status: no reply expected", False),  # colon-led: a label introducing a mention
    # An archive body far below the header may describe its own entries as superseded.
    ("Archive backup. No reply expected.\n\n" + "entry " * 80 + "\n\nOld routing rule: superseded.", True),
    *[(text, False) for text in MENTIONS],
])
def test_declaration_rule(text, declared):
    assert slack_declares_no_reply_expected(text) is declared
