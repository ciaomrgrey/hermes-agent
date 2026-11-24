"""24 Nov outage: an auto title retry must not charge a dead key's 401 to its successor."""
import asyncio
import json
import logging
import secrets
import time

import httpx
import pytest

from agent import anthropic_credentials as ac
from agent import auxiliary_client as aux
from agent.credential_pool import load_pool
from hermes_cli.auth import read_credential_pool, write_credential_pool


@pytest.fixture(params=["manual:hermes_pkce", "hermes_pkce"])
def credentials(tmp_path, monkeypatch, request):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr(ac, "read_claude_code_credentials", lambda: None)
    monkeypatch.setattr(aux, "_client_cache", {})
    # Generated in-memory stand-ins, never live credentials or recorded token fixtures.
    keys = ["sk-ant-oat01-" + secrets.token_hex(24) for _ in range(2)]
    refreshes = [secrets.token_hex(24) for _ in range(2)]
    rows = [dict(id=ident, label="anthropic-oauth-4", auth_type="oauth",
                 priority=i, source="manual:hermes_pkce", access_token=keys[i],
                 refresh_token=refreshes[i], expires_at_ms=int((time.time() + 3600) * 1000))
            for i, ident in enumerate(("571061", "39f957"))]
    rows[0].update(source=request.param, last_status="dead", last_error_reason="invalid_grant", last_status_at=time.time())
    if request.param == "hermes_pkce":
        ac._write_hermes_oauth_credentials(keys[0], refreshes[0], rows[0]["expires_at_ms"])
    write_credential_pool("anthropic", rows)
    refreshed = []

    def refresh(token, **kwargs):
        refreshed.append(refreshes.index(token))
        raise ac.AnthropicOAuthError(400, "invalid_grant", "Refresh token expired", what="refresh")

    monkeypatch.setattr(ac, "refresh_anthropic_oauth_pure", refresh)
    return keys, refreshed


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("retry_status", [200, 401])
def test_same_label_dead_oauth_recovers_on_wire(credentials, monkeypatch, caplog, async_mode, retry_status):
    keys, refreshed = credentials
    sent = []

    def send(client, request, **kwargs):
        assert request.url.host == "api.anthropic.com"
        key = request.headers.get("authorization", "").removeprefix("Bearer ") or request.headers.get("x-api-key")
        index = keys.index(key)
        sent.append(index)
        if index == 1 and retry_status == 401:
            # A peer adds a higher-priority login while our retry is in flight.
            # A freshly loaded pool has no current pointer: never charge it.
            rows = read_credential_pool("anthropic")
            rows.append(dict(id="peer", label="peer", auth_type="oauth", priority=-1,
                             source="manual:hermes_pkce", access_token=secrets.token_hex(24),
                             expires_at_ms=int((time.time() + 3600) * 1000)))
            write_credential_pool("anthropic", rows)
        if index == 0 or retry_status == 401:
            return httpx.Response(401, request=request, json={"type": "error", "error": {
                "type": "authentication_error", "message": "OAuth access token has expired. Re-authenticate to continue."}})
        body = json.loads(request.content)
        message = dict(id="msg_test", type="message", role="assistant", model=body["model"],
                       content=[{"type": "text", "text": "Recovered title"}], stop_reason="end_turn",
                       stop_sequence=None, usage={"input_tokens": 1, "output_tokens": 2})
        if body.get("stream"):
            events = [("message_start", {"type": "message_start", "message": {**message, "content": []}}),
                      ("content_block_start", {"type": "content_block_start", "index": 0,
                       "content_block": {"type": "text", "text": ""}}),
                      ("content_block_delta", {"type": "content_block_delta", "index": 0,
                       "delta": {"type": "text_delta", "text": "Recovered title"}}),
                      ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                      ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                       "usage": {"output_tokens": 2}}), ("message_stop", {"type": "message_stop"})]
            data = "".join(f"event: {kind}\ndata: {json.dumps(event)}\n\n" for kind, event in events)
            return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"}, content=data)
        return httpx.Response(200, request=request, json=message)

    monkeypatch.setattr(httpx.Client, "send", send)
    runtime = dict(provider="anthropic", model="claude-sonnet-4-5", api_key=keys[0],
                   base_url="https://api.anthropic.com", api_mode="anthropic_messages")
    kwargs = dict(task="title_generation", provider="auto", main_runtime=runtime,
                  messages=[{"role": "user", "content": "A title please"}], max_tokens=16)
    error = None
    with caplog.at_level(logging.INFO):
        try:
            if async_mode:
                result = asyncio.run(aux._async_call_llm_impl(**kwargs))
            else:
                result = aux._call_llm_impl(**kwargs)
        except Exception as exc:
            error = type(exc).__name__
    rows = {row["id"]: row for row in read_credential_pool("anthropic")}
    if retry_status == 200:
        assert error is None, (error, sent, {k: v.get("last_status") for k, v in rows.items()})
        assert result.choices[0].message.content == "Recovered title"
        assert 1 not in refreshed
        assert rows["39f957"].get("last_status") not in {"dead", "exhausted"}
        assert load_pool("anthropic").select().id == "39f957"
    else:
        assert error == "AuthenticationError"
        assert rows["39f957"]["last_status"] == "dead"
        assert rows["peer"].get("last_status") not in {"dead", "exhausted"}
        assert load_pool("anthropic").select().id == "peer"
    assert sent == [0, 1]
    assert rows["571061"]["last_status"] == "dead"
    assert rows["571061"]["last_error_reason"] == "invalid_grant"
    assert runtime["api_key"] == keys[0]  # request-local recovery, not a session rewrite
    assert all(key not in caplog.text for key in keys)


@pytest.mark.parametrize("identity", ["absent", "unknown", "dead"])
def test_recovery_never_refreshes_shared_current_for_another_key(credentials, monkeypatch, identity):
    keys, refreshed = credentials
    pool = load_pool("anthropic")
    assert pool.select().id == "39f957"
    monkeypatch.setattr(aux, "load_pool", lambda provider: pool)
    error = RuntimeError("OAuth access token has expired")
    error.status_code = 401
    hint = {"absent": "", "unknown": secrets.token_hex(24), "dead": keys[0]}[identity]
    recovered = aux._recover_provider_pool("anthropic", error, failed_api_key=hint)
    assert 1 not in refreshed
    assert pool.select().id == "39f957"
    assert next(e for e in pool.entries() if e.id == "571061").last_status == "dead"
    assert bool(recovered) == (identity == "dead")
