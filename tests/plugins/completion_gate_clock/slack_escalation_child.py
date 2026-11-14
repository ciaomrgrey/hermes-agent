"""Offline child: the REAL escalation child_main + native ``hermes send`` stack.

Only the outermost network seam is replaced: aiohttp's POST answers like Slack's
chat.postMessage and records the request to ``<HERMES_HOME>/slack-requests.jsonl``.
Everything above it (profile env load, gateway config, target resolution, relay
authorization, the Slack plugin's standalone sender, receipts) is production code.
argv: escalation.py path, mode (ok | refused | lost | reset).
``lost``: Slack accepts the POST but the response read times out (the native sender
returns an error). ``reset``: the connection drops after the request was written.
"""
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys

source, mode = Path(sys.argv[1]), sys.argv[2]
home = Path(os.environ['HERMES_HOME'])


def deny(*args, **kwargs):
    raise AssertionError('offline fixture forbids network')


socket.socket.connect = deny
import aiohttp  # noqa: E402


class _Response:
    def __init__(self, payload):
        self._payload = payload

    async def json(self, *args, **kwargs):
        if mode == 'lost':
            raise TimeoutError('response lost after Slack accepted the POST')
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def post(self, url, *args, headers=None, json=None, **kwargs):
    record = {'url': str(url), 'bearer': (headers or {}).get('Authorization', ''), 'json': json, 'pid': os.getpid()}
    with (home / 'slack-requests.jsonl').open('a') as handle:
        handle.write(__import__('json').dumps(record) + '\n')
    if mode == 'reset':
        raise aiohttp.ServerDisconnectedError()
    if mode == 'refused':
        return _Response({'ok': False, 'error': 'channel_not_found'})
    return _Response({'ok': True, 'channel': (json or {}).get('channel'), 'ts': '1794700000.000100'})


aiohttp.ClientSession.post = post
spec = importlib.util.spec_from_file_location('slack_escalation', source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
# Prewarm the native send stack (imports only) before readiness: the gate clock
# starts after this, exactly like a warm interpreter.
import hermes_cli.send_cmd  # noqa: E402,F401
import tools.send_message_tool  # noqa: E402,F401
import plugins.platforms.slack.adapter  # noqa: E402,F401
(home / 'child-ready').write_text(str(os.getpid()))
module.child_main()
