"""t_b833934f: real Telegram batching, gateway interception and clarify wait.

Only outbound Telegram transport, admission policy and model execution are doubles.
No provider calls, live messages or serving-home writes.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from plugins.platforms.telegram.adapter import TelegramAdapter
from tools import clarify_gateway as cm
from tools.clarify_tool import clarify_tool


class Receiver:
    _supports_active_turn_redirect = True

    def __init__(self):
        self.redirects = []
        self.interrupts = []

    def redirect(self, text):
        self.redirects.append(text)
        return True

    def interrupt(self, text):
        self.interrupts.append(text)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['text', 'captioned_photo', 'photo_burst'])
async def test_message_survives_open_clarify(kind, monkeypatch, tmp_path, durable=False):
    from gateway.platforms.base import SendResult
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='offline-test'))
    adapter._media_batch_delay_seconds = 0
    adapter._bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id='1'))
    adapter.pause_typing_for_chat = lambda *a: None
    adapter.resume_typing_for_chat = lambda *a: None
    runner = GatewayRunner(config=GatewayConfig())
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='12345', chat_type='dm', user_id='user1')
    key = runner._session_key_for_source(source)
    runner._delivery_adapter_for = lambda s: adapter
    runner._hm_admit_event = AsyncMock(side_effect=lambda e: (e, e.source, False))
    runner._hm_estop_gate = lambda *a: None
    runner._hm_evict_idle_stale_agent = lambda *a: None
    runner._hm_evict_reaped_agent = lambda *a: None
    receiver = Receiver()
    state = runner._session_state(key)
    state.turn.agent = receiver
    runner._begin_session_run_generation(key)
    runner._effective_busy_input_mode = lambda s: 'interrupt'
    runner._effective_busy_text_mode = lambda s: 'interrupt'
    runner._session_has_compression_in_flight = AsyncMock(return_value=False)
    adapter._message_handler = runner._handle_message
    adapter._active_sessions[key] = asyncio.Event()
    adapter._session_tasks[key] = asyncio.current_task()
    authority = None
    ingress = None
    if durable:
        from gateway.session_authority import initialize_session_authority
        from hermes_state_runtime import list_session_admissions
        from gateway.session_envelope import restore_native
        from tests.waiting import async_wait_until
        runner._is_user_authorized = lambda source, **kwargs: True
        receiver._supports_active_turn_redirect = False
        adapter.gateway_runner = runner
        runner.adapters = {Platform.TELEGRAM: adapter}
        authority = await initialize_session_authority(runner, profile_id='default', instance_id='clarify-proof')
        monkeypatch.setattr(authority, '_schedule', lambda ref: None)
        # Hold execution at the durable FIFO, as if the previous turn is still unwinding.
        def rows():
            return [row for sid in authority.sessions for row in
                    list_session_admissions(authority.db, session_id=sid, pending_only=False)]
    opened = asyncio.Event()
    native_send = adapter.send_clarify

    async def observed_send(**kwargs):
        result = await native_send(**kwargs)
        opened.set()
        return result

    adapter.send_clarify = observed_send
    turn = object.__new__(TurnRunner)
    turn._ctx = SimpleNamespace(
        _status_adapter=adapter, _status_chat_id=source.chat_id,
        _status_thread_metadata=None, session_key=key,
        source=source, stream_consumer_holder=[None], _loop_for_step=asyncio.get_running_loop())
    turn._close_native_stream_boundary = lambda *a, **k: None
    monkeypatch.setattr(cm, 'get_clarify_timeout', lambda: 3600)
    import inspect
    legacy = {'question': ''} if 'question' in inspect.signature(clarify_tool).parameters else {}
    task = asyncio.create_task(asyncio.to_thread(
        clarify_tool, questions=[{'question': 'Which option?', 'choices': ['Alpha', 'Beta', 'Gamma']}],
        callback=turn._clarify_callback_sync, **legacy))
    try:
        await asyncio.wait_for(opened.wait(), 10)
        assert cm.get_pending_for_session(key, include_choice_prompts=True) is not None
        expected_texts = ['Please inspect this instead.']
        expected_images = []
        if kind == 'photo_burst':
            expected_texts.append('And compare this second photo.')
        events = []
        for i, text in enumerate(expected_texts):
            images = []
            if kind != 'text':
                image = tmp_path / f'image-{i}.jpg'
                image.write_bytes(b'\xff\xd8\xff\xd9')
                images = [str(image)]
                expected_images.extend(images)
            event = MessageEvent(text=text, source=source, message_id=str(i + 10),
                                 message_type=MessageType.TEXT if kind == 'text' else MessageType.PHOTO,
                                 media_urls=images, media_types=['image/jpeg'] * len(images))
            events.append(event)
        async def deliver():
            if kind == 'text':
                await adapter.handle_message(events[0])
            else:
                for event in events:
                    await adapter._route_photo_event(SimpleNamespace(media_group_id=None), event)
                await asyncio.gather(*list(adapter._pending_photo_batch_tasks.values()))
        if durable:
            ingress = asyncio.create_task(deliver())
            await async_wait_until(rows, timeout=10, what='the follow-up persisted in the FIFO')
        else:
            await deliver()
        payload = json.loads(await asyncio.wait_for(asyncio.shield(task), 10))
        if durable:
            assert len(rows()) == 1
            pending = restore_native(rows()[0]['payload'], runner)
            pending_text = pending.text
        else:
            pending, pending_text = await runner._run_agent_drain_pending(
                {'final_response': 'old turn finished', 'interrupted': bool(receiver.interrupts),
                 'interrupt_message': receiver.interrupts[-1] if receiver.interrupts else None},
                adapter, source, key)
        delivered_text = '\n'.join(receiver.redirects + ([pending_text] if pending_text else []))
        delivered_images = pending.media_urls if pending else []
        observation = {'kind': kind, 'clarify': payload, 'delivered_text': delivered_text,
                       'image_count': len(delivered_images), 'expected_image_count': len(expected_images)}
        print('OBSERVATION ' + json.dumps(observation, sort_keys=True))
        for text in expected_texts:
            assert text in delivered_text, observation
        assert len(delivered_images) == len(expected_images), observation
        from pathlib import Path
        assert [Path(p).read_bytes() for p in delivered_images] == [Path(p).read_bytes() for p in expected_images]
        assert 'did not respond within' not in json.dumps(payload), observation
        assert payload.get('timed_out') is not True, observation
        assert 'cancel' in json.dumps(payload).lower(), observation
    finally:
        cm.clear_session(key)
        await asyncio.wait_for(task, 10)
        if ingress is not None:
            ingress.cancel()
            await asyncio.gather(ingress, return_exceptions=True)
        adapter._active_sessions.clear()
        adapter._session_tasks.clear()
        if authority is not None:
            authority.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['text', 'captioned_photo', 'photo_burst'])
async def test_message_persisted_by_stock_fifo(kind, monkeypatch, tmp_path):
    pytest.importorskip('gateway.session_authority')
    await test_message_survives_open_clarify(kind, monkeypatch, tmp_path, durable=True)
