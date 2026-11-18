"""Non-voice attachments are whole messages, not text-only clarify answers."""
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from tools import clarify_gateway as cm


@pytest.mark.asyncio
@pytest.mark.parametrize('prompt', ['choice', 'open', 'other'])
@pytest.mark.parametrize('kind,mimes', [
    (MessageType.PHOTO, ['image/jpeg']),
    (MessageType.DOCUMENT, ['application/pdf']),
    (MessageType.VIDEO, ['video/mp4']),
    (MessageType.AUDIO, ['audio/mpeg']),
    (MessageType.VOICE, ['audio/ogg', 'image/jpeg']),
    (MessageType.VOICE, ['audio/ogg']),
])
async def test_attachment_is_not_consumed_as_a_text_answer(prompt, kind, mimes, monkeypatch):
    runner = GatewayRunner(config=GatewayConfig())
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='12345', chat_type='dm', user_id='user1')
    key = runner._session_key_for_source(source)
    runner._begin_session_run_generation(key)
    monkeypatch.setattr(runner, '_delivery_adapter_for', lambda source: None)
    transcribe = AsyncMock(return_value=('', ['Alpha']))
    monkeypatch.setattr(runner, '_transcribe_pending_audio_event_once', transcribe)
    entry = cm.register('media-question', key, 'Which option?',
                        None if prompt == 'open' else ['Alpha', 'Beta'], authority={})
    if prompt == 'other':
        cm.mark_awaiting_text(entry.clarify_id)
    event = MessageEvent(text='Alpha', source=source, message_type=kind,
                         media_urls=[f'attachment-{i}' for i in range(len(mimes))], media_types=mimes)
    original_urls = list(event.media_urls)
    try:
        reply = await runner._hm_clarify_reply(event, source, key)
        assert entry.event.is_set()
        if kind == MessageType.VOICE and len(mimes) == 1:
            assert reply == ''
            assert entry.response == 'Alpha'
            transcribe.assert_awaited_once()
            assert not hasattr(event, '_clarify_superseded')
        else:
            assert reply is None
            assert entry.response == cm.CANCELLED
            assert event._clarify_superseded == runner._current_session_run_generation(key)
            transcribe.assert_not_awaited()
        assert event.text == 'Alpha'
        assert event.media_urls == original_urls
        assert event.media_types == mimes
    finally:
        cm.clear_session(key)
