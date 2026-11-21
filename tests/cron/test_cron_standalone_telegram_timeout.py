"""Standalone Telegram's lost response is not evidence of non-delivery."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Bot
from telegram.error import TimedOut
from telegram.request import HTTPXRequest

from cron import jobs, scheduler, scheduler_delivery
from gateway import config
from hermes_cli.cron import _cron_doctor_issues_for_job, _job_warnings
from tools import send_message_senders, send_message_tool


def _configure(monkeypatch, token="123:fake"):
    monkeypatch.delenv("_HERMES_CRON_EXTERNAL_WORKER", raising=False)
    monkeypatch.setattr(scheduler, "load_config", lambda: {"cron": {"wrap_response": False}})
    monkeypatch.setattr(config, "load_gateway_config", lambda: config.GatewayConfig(
        platforms={config.Platform.TELEGRAM: config.PlatformConfig(enabled=True, token=token)}))


def _assert_outcome(job, error, unverified):
    assert jobs.mark_job_run(job["id"], success=True, delivery_error=error)
    stored = jobs.get_job(job["id"])
    assert stored is not None
    outcome = scheduler._classify_delivery_outcome(
        delivery_error=error, should_deliver=True, unresolved_origin=False,
        normalized_deliver=job["deliver"], incident_acked=False, success=True)
    warnings = "\n".join(_job_warnings(stored) + _cron_doctor_issues_for_job(stored))
    if unverified:
        assert error is None
        assert stored["last_delivery_unverified"] == [job["deliver"]]
        assert stored["last_status"] == "ok"
        assert outcome != "failed"
        assert "unverified" in warnings.lower()
        assert "--deliver" not in warnings
        assert "not delivered" not in warnings
        assert "adapter acked" not in warnings
    else:
        assert error
        assert not stored.get("last_delivery_unverified")
        assert stored["last_status"] == "delivery_failed"
        assert outcome == "failed"
        assert "--deliver" in warnings


@pytest.mark.parametrize("reply", [
    "lost", "retry-lost", "missing-token", "chat not found", "Forbidden: bot was blocked", "Bad Request",
])
def test_standalone_http_response_controls_delivery_certainty(monkeypatch, reply):
    """A real POST arrives once, but its response is lost; rejections remain failures."""
    _configure(monkeypatch, token="" if reply == "missing-token" else "123:fake")
    received = []
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            if reply == "lost" or (reply == "retry-lost" and len(received) == 3):
                release.wait(10)
                return
            status = 502 if reply == "retry-lost" else (403 if reply.startswith("Forbidden") else 400)
            description = "Bad Gateway" if status == 502 else reply
            payload = json.dumps({"ok": False, "error_code": status, "description": description}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    requests = []

    def local_bot(token):
        request = HTTPXRequest(read_timeout=0.2, httpx_kwargs={"trust_env": False})
        requests.append(request)
        return Bot(token, base_url=f"http://127.0.0.1:{server.server_port}/bot", request=request)

    monkeypatch.setattr(send_message_senders, "_telegram_bot", local_bot)
    job = jobs.create_job("report", "every 1h", deliver="telegram:12345")
    # A previous failure must not keep telling the operator to change a valid target.
    jobs.mark_job_run(job["id"], success=True, delivery_error="previous failure")
    try:
        error = scheduler_delivery._deliver_result(job, "Nightly report")
        # Only explicit 502 rejections are retried, never the subsequent lost response.
        assert len(received) == {"missing-token": 0, "retry-lost": 3}.get(reply, 1)
        _assert_outcome(job, error, unverified=reply in {"lost", "retry-lost"})
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        for request in requests:
            asyncio.run(request.shutdown())


@pytest.mark.parametrize("failure,unverified", [
    ({"error": "Telegram send failed: Timed out"}, True),
    (TimedOut("response unavailable"), True),
    ({"error": "No token configured"}, False),
    ({"error": "Invalid chat_id"}, False),
    ({"error": "Telegram send failed: Bad Request: timeout is not a valid chat"}, False),
])
def test_timeout_type_and_legacy_text_preserve_uncertainty(monkeypatch, failure, unverified):
    _configure(monkeypatch)
    if isinstance(failure, Exception):
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=failure))
        monkeypatch.setattr(send_message_senders, "_telegram_bot", lambda token: bot)
        send = bot.send_message
    else:
        send = AsyncMock(return_value=failure)
        monkeypatch.setattr(send_message_tool, "_send_to_platform", send)
    job = jobs.create_job("report", "every 1h", deliver="telegram:12345")
    error = scheduler_delivery._deliver_result(job, "Nightly report")
    send.assert_awaited_once()
    _assert_outcome(job, error, unverified)
