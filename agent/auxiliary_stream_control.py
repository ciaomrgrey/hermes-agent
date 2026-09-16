"""Request-local ownership of raw SDK streams, including caller-idle time.

No shared-client closure: a sync observer only shuts down owned sockets while
another thread reads. Wrapper closure is serialized after that reader unwinds.
"""
import asyncio
import logging
import threading


def cancellation():
    from agent.auxiliary_client import AuxiliaryExplicitCancellation
    return AuxiliaryExplicitCancellation()


class SyncStream:
    def __init__(self, stream, attempt, cancel_check):
        self.stream, self.attempt = stream, attempt
        self.cancel_check = cancel_check
        self.lock = threading.Lock()
        self.finished = threading.Event()
        self.closed = False
        self.observer = threading.Thread(target=self._observe, name='hermes-aux-stream-owner', daemon=True)

    def start(self):
        self.observer.start()
        return self

    def _observe(self):
        try:
            while not self.finished.wait(.02):
                if self.cancel_check():
                    self.attempt.abort()
                    self._cleanup()
                    self.attempt.provider_thread.join(2)
                    logging.getLogger(__name__).warning('Auxiliary local cancellation evidence: %s',
                        dict(tcp_force_closed=self.attempt.tcp_force_closed,
                             worker_ended=not self.attempt.provider_thread.is_alive(), reader_quiesced=True,
                             remote_cancel_ack=None, billing_cessation=None))
                    return
        finally:
            self.finished.set()

    def _cleanup(self):
        from agent.auxiliary_control import _LOCK, _ATTEMPTS
        with self.lock:
            if self.closed:
                return
            self.closed = True
            try:
                self.stream.close()
            finally:
                # No concurrent read remains. Transfer final cleanup ownership,
                # never close a TLS reader's FD from the observer during recv.
                with self.attempt.registry.lock:
                    self.attempt.registry.owner_tid = threading.get_ident()
                self.attempt.registry.close_once('raw stream lifetime complete')
                with _LOCK:
                    _ATTEMPTS.pop(id(self.attempt), None)
                self.finished.set()

    def __iter__(self):
        return self

    def __next__(self):
        try:
            with self.lock:
                if self.cancel_check():
                    raise cancellation()
                if self.closed:
                    raise StopIteration
                value = next(self.stream)
                if self.cancel_check():
                    raise cancellation()
                return value
        except BaseException:
            self.close()
            if self.cancel_check():
                raise cancellation() from None
            raise

    def close(self):
        # A caller may close while another thread is blocked in next().
        # Shutdown wakes that reader; full close still waits for its lock.
        if self.lock.acquire(blocking=False):
            self.lock.release()
        else:
            self.attempt.abort()
        self._cleanup()
        if threading.current_thread() is not self.observer:
            self.observer.join(2)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __getattr__(self, name):
        return getattr(self.stream, name)


class AsyncStream:
    def __init__(self, stream, client, check, registration):
        self.stream, self.client, self.check = stream, client, check
        self.registration = registration
        self.closed = False
        self.cancelled = False
        self.reader = None
        self.observer = asyncio.create_task(self._observe())

    async def _observe(self):
        try:
            while not self.closed:
                if self.check():
                    self.cancelled = True
                    if self.reader is not None:
                        self.reader.cancel()
                        await asyncio.wait({self.reader}, timeout=2)
                    await self._cleanup()
                    return
                await asyncio.sleep(.02)
        finally:
            await self._cleanup()

    async def _cleanup(self):
        from agent.auxiliary_control import _LOCK, _ATTEMPTS
        if self.closed:
            return
        self.closed = True
        try:
            await self.stream.close()
        finally:
            await self.client.close()
            with _LOCK:
                _ATTEMPTS.pop(id(self.registration), None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.cancelled or self.check():
            self.cancelled = True
            await self.close()
            raise cancellation()
        if self.closed:
            raise StopAsyncIteration
        self.reader = asyncio.create_task(anext(self.stream))
        try:
            value = await self.reader
            if self.cancelled or self.check():
                self.cancelled = True
                raise cancellation()
            return value
        except BaseException:
            await self.close()
            if self.cancelled or self.check():
                raise cancellation() from None
            raise
        finally:
            self.reader = None

    async def close(self):
        await self._cleanup()
        if asyncio.current_task() is not self.observer:
            self.observer.cancel()
            await asyncio.gather(self.observer, return_exceptions=True)

    async def aclose(self):
        await self.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    def __getattr__(self, name):
        return getattr(self.stream, name)
