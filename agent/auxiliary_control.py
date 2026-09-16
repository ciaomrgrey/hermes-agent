"""Temporary native aux-control seam: remove when upstream owns aux attempts.

New SDK/httpx view, shared inner pool. Stranger threads shutdown only owned
sockets; the provider worker closes its wrapper. No shared-client mutation.
"""
import threading
import time
from contextlib import contextmanager

from agent.chat_completion_helpers import _RequestClientRegistry
from agent.agent_runtime_helpers import force_close_tcp_sockets
from agent import provider_control

_LOCK = threading.RLock()
_ATTEMPTS = {}


def owned_client(client):
    from openai import OpenAI
    from agent.auxiliary_client import CodexAuxiliaryClient, AnthropicAuxiliaryClient
    from agent.process_bootstrap import build_keepalive_http_client
    real = getattr(client, '_real_client', client)
    url = str(real.base_url)
    http = build_keepalive_http_client(url)
    common = dict(base_url=url, http_client=http, max_retries=0, timeout=real.timeout,
                  default_headers=dict(getattr(real, '_custom_headers', {}) or {}),
                  default_query=dict(getattr(real, '_custom_query', {}) or {}))
    try:
        if isinstance(real, OpenAI):
            credential = vars(real).get('_api_key_provider') or real.api_key
            fresh = OpenAI(api_key=credential, organization=real.organization, project=real.project, **common)
            if isinstance(client, CodexAuxiliaryClient):
                return CodexAuxiliaryClient(fresh, client.chat.completions._model), fresh
            if type(client) is OpenAI:
                return fresh, fresh
            fresh.close()
        elif isinstance(client, AnthropicAuxiliaryClient):
            from anthropic import Anthropic
            fresh = Anthropic(api_key=real.api_key, auth_token=real.auth_token, **common)
            adapter = client.chat.completions
            return AnthropicAuxiliaryClient(fresh, adapter._model, client.api_key, client.base_url,
                                            is_oauth=adapter._is_oauth), fresh
        raise provider_control.HeldProvider('Unsupported controlled auxiliary transport; dispatch refused')
    except BaseException:
        http.close()
        raise


class Attempt:
    def __init__(self, client, provider, policy):
        self.provider, self.policy = provider, policy
        self.scope = provider_control._SCOPE.get()
        # Unknown account is deliberately conservative: a hold for any account
        # of this provider applies. Never derive grant identity from rotating tokens.
        self.account = None
        self.client, self.wire = owned_client(client)
        self.registry = _RequestClientRegistry(self)
        self.cancelled = threading.Event()
        self.tcp_force_closed = 0
        self.worker_ended = False
        self.worker_started = False
        self.stream_lifetime = None

    def _abort_request_openai_client(self, client, *, reason):
        self.tcp_force_closed += force_close_tcp_sockets(client)

    def _close_request_openai_client(self, client, *, reason):
        client.close()

    def cancel_requested(self):
        if self.cancelled.is_set():
            return True
        try:
            if self.scope is not None:
                self.scope.check()
            self.policy.check(self.provider)
        except provider_control.HeldProvider:
            self.cancelled.set()
        return self.cancelled.is_set()

    def abort(self):
        self.cancelled.set()
        self.registry.close_once('provider hold / auxiliary cancellation')

    @contextmanager
    def registered(self):
        try:
            with _LOCK:
                self.policy.check(self.provider)
                _ATTEMPTS[id(self)] = self
            yield self
        finally:
            with _LOCK:
                if self.stream_lifetime is None:
                    _ATTEMPTS.pop(id(self), None)
            if not self.worker_started:
                self.wire.close()

    @contextmanager
    def worker(self):
        self.worker_started = True
        self.registry.set_client(self.wire)
        try:
            yield
        finally:
            if self.stream_lifetime is None:
                self.registry.close_once('auxiliary worker complete')
            self.worker_ended = True
            if self.stream_lifetime is not None:
                self.stream_lifetime.start()

    def retain_stream(self, result, cancel_check):
        from openai import Stream
        if isinstance(result, Stream):
            from agent.auxiliary_stream_control import SyncStream
            self.stream_lifetime = SyncStream(result, self, cancel_check)
            return self.stream_lifetime
        return result

    def abort_and_join(self, worker):
        # A hold may land between registration and socket assignment. Repeat
        # owner-stamped shutdown while joining, not a one-shot zero-socket claim.
        deadline = time.monotonic()+2
        while worker.is_alive() and time.monotonic() < deadline:
            self.abort()
            worker.join(.02)
        return dict(tcp_force_closed=self.tcp_force_closed, worker_ended=not worker.is_alive(),
                    remote_cancel_ack=None, billing_cessation=None)


def raw_sync(client, kwargs, provider):
    from agent.auxiliary_client import _run_protected_sync_provider_call
    provider_control.check_request(provider)
    policy = provider_control.current_policy()
    if not provider_control.controls_request(policy, provider):
        return client.chat.completions.create(**kwargs)
    attempt = Attempt(client, provider, policy)
    with attempt.registered():
        return _run_protected_sync_provider_call(
            lambda request: attempt.client.chat.completions.create(**request), kwargs, attempt=attempt)


async def run_async(client, kwargs, provider):
    """Native async SDK ownership: cancel task and close on its owning loop."""
    import asyncio
    from openai import AsyncOpenAI
    from agent.process_bootstrap import build_keepalive_http_client
    from agent import auxiliary_client as aux
    policy = provider_control.current_policy()
    provider_control.check_request(provider)
    if isinstance(client, (aux.AsyncCodexAuxiliaryClient, aux.AsyncAnthropicAuxiliaryClient)):
        # These native adapters already run a SYNC SDK on a thread. Preserve
        # that transport, but give its protected owner the same cancel edge.
        adapter = client.chat.completions._sync
        if isinstance(client, aux.AsyncCodexAuxiliaryClient):
            template = aux.CodexAuxiliaryClient(client._real_client, adapter._model)
        else:
            template = aux.AnthropicAuxiliaryClient(client._real_client, adapter._model,
                client.api_key, client.base_url, is_oauth=adapter._is_oauth)
        stopped = threading.Event()
        original_cancel = aux._capture_aux_cancel_check()
        def run_sync_adapter():
            attempt = Attempt(template, provider, policy)
            with attempt.registered(), aux.aux_interrupt_protection(
                    cancel_check=lambda: stopped.is_set() or (callable(original_cancel) and original_cancel())):
                return aux._run_protected_sync_provider_call(
                    lambda request: aux._create_with_progress(attempt.client, request), kwargs, attempt=attempt)
        worker = asyncio.create_task(asyncio.to_thread(run_sync_adapter))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            stopped.set()
            await asyncio.wait({worker}, timeout=2.1)
            if worker.done():
                try:
                    worker.result()
                except BaseException:
                    pass
            raise
    if type(client) is not AsyncOpenAI:
        raise provider_control.HeldProvider('Controlled async adapter not qualified; dispatch refused')
    http = build_keepalive_http_client(str(client.base_url), async_mode=True)
    fresh = AsyncOpenAI(api_key=vars(client).get('_api_key_provider') or client.api_key,
                        base_url=client.base_url, organization=client.organization, project=client.project,
                        timeout=client.timeout, max_retries=0, http_client=http,
                        default_headers=dict(client._custom_headers), default_query=dict(client._custom_query))
    task = None
    registration = object()
    cancel_check = aux._capture_aux_cancel_check()
    retained = False
    scope = provider_control._SCOPE.get()
    def cancelled():
        try:
            if scope is not None:
                scope.check()
            policy.check(provider)
        except provider_control.HeldProvider:
            return True
        return callable(cancel_check) and cancel_check()
    try:
        with _LOCK:
            provider_control.check_request(provider)
            task = asyncio.create_task(aux._acreate_with_progress(fresh, kwargs))
            _ATTEMPTS[id(registration)] = dict(provider=provider, account=None, abort=task.cancel,
                                              owner_tid=threading.get_ident(), kind='async')
        while True:
            try:
                provider_control.check_request(provider)
            except provider_control.HeldProvider:
                raise aux.AuxiliaryExplicitCancellation() from None
            if callable(cancel_check) and cancel_check():
                raise aux.AuxiliaryExplicitCancellation()
            if task.done():
                result = task.result()
                from openai import AsyncStream
                if isinstance(result, AsyncStream):
                    from agent.auxiliary_stream_control import AsyncStream as OwnedStream
                    result = OwnedStream(result, fresh, cancelled, registration)
                    retained = True
                return result
            await asyncio.wait({task}, timeout=.02)
    finally:
        if task is not None and not task.done():
            task.cancel()
        if not retained:
            await fresh.close()
        if task is not None:
            await asyncio.wait({task}, timeout=2)
            if task.done():
                # Retrieve discarded cancellation/error so no unhandled-task warning.
                try:
                    task.result()
                except BaseException:
                    pass
        with _LOCK:
            if not retained:
                _ATTEMPTS.pop(id(registration), None)
