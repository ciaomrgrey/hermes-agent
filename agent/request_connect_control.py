"""Request-local pre-pool TLS abort handle, using native httpcore trace events.

The owned duplicate is never the TLS reader FD. Stranger-thread abort only
shutdowns it; the request owner closes it under the same lock. Shared pools
are untouched. DNS/TCP connect before connect_tcp.complete is not killable.
"""
import socket
import threading

from agent import provider_control as pc


class ConnectAbort:
    def __init__(self, check):
        self.lock = threading.RLock()
        self.check = check
        self.duplicate = None
        self.cancelled = False
        self.shutdown_done = False

    def clear(self):
        with self.lock:
            if self.duplicate is not None:
                self.duplicate.close()  # our duplicate, never the SSL BIO descriptor
                self.duplicate = None

    def abort(self):
        with self.lock:
            self.cancelled = True
            if self.duplicate is None or self.shutdown_done:
                return 0
            try:
                self.duplicate.shutdown(socket.SHUT_RDWR)
                self.shutdown_done = True
                return 1
            except OSError:
                return 0

    def trace(self, event, info):
        if event.endswith('.failed'):
            self.clear()
            return
        if not event.endswith(('connect_tcp.complete', 'start_tls.started', 'start_tls.complete', 'send_request_headers.started')):
            return
        stream = info.get('return_value')
        if event.endswith('connect_tcp.complete') and stream is not None:
            with self.lock:
                self.duplicate = stream.get_extra_info('socket').dup()
        try:
            self.check()
            if self.cancelled:
                raise pc.HeldProvider('Request cancelled before HTTP dispatch')
        except pc.HeldProvider:
            self.abort()
            # This callback is the provider OWNER, not a stranger. If TCP just
            # completed, no pool has adopted the stream: close it before raising.
            if event.endswith('connect_tcp.complete') and stream is not None:
                stream.close()
            raise
        finally:
            if event.endswith('send_request_headers.started') or event.endswith('.failed'):
                self.clear()

    def hook(self, request):
        previous = request.extensions.get('trace')
        def trace(event, info):
            self.trace(event, info)
            if previous is not None:
                previous(event, info)
        request.extensions['trace'] = trace


def bind(client, provider):
    policy = pc.current_policy()
    if not pc.controls_request(policy, provider):
        return
    scope = pc._SCOPE.get()
    def check():
        if scope is not None:
            scope.check()
        policy.check(provider)
    http = getattr(client, '_client', client)
    previous = getattr(http, '_hermes_connect_abort', None)
    if isinstance(previous, ConnectAbort):
        previous.clear()
        http.event_hooks['request'].remove(previous.hook)
    handle = ConnectAbort(check)
    http._hermes_connect_abort = handle
    http.event_hooks['request'].append(handle.hook)


def clear(client):
    handle = getattr(getattr(client, '_client', client), '_hermes_connect_abort', None)
    if isinstance(handle, ConnectAbort):
        handle.clear()
