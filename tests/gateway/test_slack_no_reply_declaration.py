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

# The original round-0 fixture, unchanged: its payload carries an unquoted question.
ARCHIVE = (
    "*SWITCHBOARD-ARCHIVE-20260930 — CHUNK 1/3*\n\n"
    "Archive backup of Chat's agent-switchboard memory file. No reply expected. No action required.\n\n"
    "*name: agent-switchboard* — does anything here need a question mark? CONSULT vs INSTRUCT."
)
ARCHIVE_QUOTED = (
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
@pytest.mark.parametrize("text", [ARCHIVE, ARCHIVE_QUOTED, WRAPUP], ids=["archive-chunk", "archive-quoted", "wrapup"])
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
    # Round 4 (Gurney): an imperative or a code-quoted question after an untitled header. Admission now
    # needs a positive envelope - an ARCHIVE/BACKUP/WRAP-UP title line - so these never qualify.
    "Archive backup. No reply expected.\n\nAnswer this: `Which commit is pinned?`",
    "Archive backup. No reply expected.\n\nReport the pinned commit.",
    # A titled envelope that itself asks, requests, corrects or addresses someone is not a record.
    "*ARCHIVE BACKUP*\n\nNo reply expected. Please confirm the pinned commit.",
    "*ARCHIVE BACKUP* No reply expected. Report the pinned commit to me.",
    "*ARCHIVE BACKUP*\n\nNo reply expected - actually, a response is required.",
    "*ARCHIVE BACKUP*\n\nNo reply expected. Which commit is pinned?",
    "*ARCHIVE BACKUP*\n\nNo reply expected, but answer the question below.",
    # The declaration must sit in the envelope (title or the paragraph right after it).
    "*ARCHIVE BACKUP*\n\nReport the pinned commit.\n\nNo reply expected.",
    # The title must name the genre itself, not quote it.
    "*`ARCHIVE BACKUP`*\n\nNo reply expected.",
    "What is an \"ARCHIVE BACKUP\"?\n\nNo reply expected.",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", MENTIONS, ids=["blockquote", "slack-escaped-quote", "fenced-code",
                                                "unclosed-fence", "withdrawn-inline", "withdrawn-later",
                                                "inline-code", "actually-please-later", "correction-required-later",
                                                "can-you-far-below", "second-thought-needed", "closing-question",
                                                "no-genre", "request-after-blockquote",
                                                "request-after-escaped-blockquote", "address-after-blockquote",
                                                "question-with-signoff", "question-mid-line", "direct-address",
                                                "round4-code-quoted-question", "round4-imperative",
                                                "envelope-please", "envelope-imperative-address",
                                                "envelope-correction", "envelope-question", "envelope-but",
                                                "declaration-after-request", "title-in-code",
                                                "title-in-quote"])
async def test_quoted_or_withdrawn_declaration_keeps_the_notice(adapter, monkeypatch, tmp_path, text):
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
    (ARCHIVE_QUOTED, True),
    (WRAPUP, True),
    # Everything after the envelope is archived payload: its questions, "no acks" rules, "superseded"
    # notes and imperatives are the archived file's words, not the sender's current request.
    ("*ARCHIVE BACKUP*\n\nNo reply expected.\n\n• Rule: ask why first? Always.\n• ONLY REAL ANSWERS. No acks.", True),
    ("*ARCHIVE BACKUP* · No action needed, nothing expected back.\n\n• Old routing rule: superseded.", True),
    ("*SWITCHBOARD-ARCHIVE-20260928 · CHUNK 1/3* · Archive backup. No action needed, nothing expected back.", True),
    ("*ARCHIVE BACKUP — AGENT-SWITCHBOARD-20260924-01*\n\nArchive only. No action requested, no response "
     "expected.\n\nCurrent contents of the file.\n\n```---\nname: agent-switchboard\n```", True),
    ("NO REPLY REQUIRED — ARCHIVE ONLY", True),
    (f"<@{BOT_USER_ID}> *ARCHIVE COPY*\n\nNo reply expected.", True),
    # No positive envelope (a capitalised ARCHIVE/BACKUP/WRAP-UP title line): never admitted, even when
    # otherwise harmless. Narrower than rounds 1-3, which admitted these; they keep the notice.
    ("Archive only. No action requested, no response expected.", False),
    ("FYI: deploy finished. No response needed.", False),
    ("NO REPLY REQUIRED — archive only", False),
    ("Archive backup · No action needed, nothing expected back.", False),
    ("*STATUS*\n\nNo reply expected.", False),
    (QUESTION, False),
    ("Is no action required on this one?", False),
    ("No reply expected? Tell me if there is.", False),
    ("Please explain what 'no action required' means in the card.", False),
    ("*ARCHIVE*\n\n" + "a" * 400 + " No reply expected.", False),  # past the 300-char header
    ("*ARCHIVE*\n\nIndex.\n\nNo reply expected.", False),  # outside the envelope
    ("", False),
    ("*ARCHIVE*\n\nOld note, no action required, but please ack.", False),
    ("*ARCHIVE*\n\nStatus: no reply expected", False),  # colon-led: a label introducing a mention
    *[(text, False) for text in MENTIONS],
])
def test_declaration_rule(text, declared):
    assert slack_declares_no_reply_expected(text) is declared
