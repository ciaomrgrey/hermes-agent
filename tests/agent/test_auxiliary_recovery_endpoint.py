"""Recovered keys stay on their effective endpoint; mismatch leaves fallback reachable."""
from types import SimpleNamespace
import secrets

import pytest

from agent import auxiliary_client as aux
from agent.credential_pool import PooledCredential


@pytest.mark.parametrize("same_endpoint", [False, True])
def test_credential_rung_honors_nous_effective_proxy_or_falls_through(monkeypatch, same_endpoint):
    proxy = "https://proxy.example/v1"
    canonical = "https://inference-api.nousresearch.com/v1"
    monkeypatch.setenv("NOUS_INFERENCE_BASE_URL", proxy if same_endpoint else canonical)
    failed = secrets.token_hex(24)
    recovered = PooledCredential(provider="nous", id="next", label="next", auth_type="api_key",
                                 access_token=secrets.token_hex(24), base_url=canonical,
                                 priority=1, source="manual")
    error = RuntimeError("credits exhausted")
    error.status_code = 402
    client = SimpleNamespace(api_key=failed, base_url=proxy)
    monkeypatch.setattr(aux, "_recover_provider_pool", lambda *a, **kw: recovered)
    route = aux._LadderRoute(
        client=client, task="title_generation", tag="", async_mode=False, base_info=proxy,
        resolved_provider="nous", resolved_model="test-model", resolved_base_url=proxy,
        resolved_api_key=None, resolved_api_mode=None, final_model="test-model",
        main_runtime=None, route_info=None, timeout=10,
    )
    rung = aux._ladder_credential_rungs(error, route, {}, client_is_nous=True)
    if same_endpoint:
        step = next(rung)
        assert step.kind == "retry_same_provider" and step.credential is recovered
        response = object()
        with pytest.raises(StopIteration) as done:
            rung.send(response)
        assert done.value.value == (response, None)
    else:
        with pytest.raises(StopIteration) as done:
            next(rung)
        # The outer ladder receives the original failure and continues fallback,
        # rather than seeing an unrelated RuntimeError from retry construction.
        assert done.value.value == (None, error)
