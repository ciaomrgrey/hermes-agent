"""Refresh must reach the actual summary request, including an idle-rebuilt /model agent.

Regression for upstream #121756 / 4b842d3. No real credentials or remote API calls.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import time

import pytest

from gateway.run import GatewayRunner
from run_agent import AIAgent


@pytest.mark.parametrize("separate_compressor", [False, True])
def test_rebuilt_override_refresh_reaches_summary_request(separate_compressor):
    old, new, other = "fixture-token-A", "fixture-token-B", "fixture-other-route"
    runner = object.__new__(GatewayRunner)
    runner._session_state("fixture-session").conversation.model_override = {
        "model": "claude-sonnet-4-6", "provider": "anthropic", "api_key": old,
        "base_url": "https://api.anthropic.com", "api_mode": "anthropic_messages",
    }
    with (
        patch("gateway.run._resolve_gateway_model", return_value="default-model"),
        patch("gateway.run._credential_pool_for_provider", return_value=None),
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()),
        patch("agent.anthropic_credentials.resolve_anthropic_token", return_value=new),
    ):
        # Repeat without clearing conversation state: idle eviction rebuilds AIAgent, not /model.
        for _ in range(2):
            model, runtime = runner._resolve_session_agent_runtime(session_key="fixture-session")
            agent = AIAgent(
                model=model, **runtime, quiet_mode=True,
                skip_context_files=True, skip_memory=True,
            )
            cc = agent.context_compressor
            if separate_compressor:
                cc.api_key = other
                agent._primary_runtime["compressor_api_key"] = other
            # Exercise the per-request entry point, not only the refresh helper.
            agent._create_request_anthropic_client(reason="fixture")
            response = SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="The retained task remains in progress."),
                finish_reason="stop",
            )])
            with patch("agent.context_compressor.call_llm", return_value=response) as call:
                assert cc._call_summary_llm("Summarize the retained task.", time.monotonic())
            expected = other if separate_compressor else new
            assert call.call_args.kwargs["main_runtime"]["api_key"] == expected
            assert agent.api_key == agent._anthropic_api_key == new
            assert agent._primary_runtime["api_key"] == new
            assert agent._primary_runtime["anthropic_api_key"] == new
            assert agent._primary_runtime["compressor_api_key"] == expected
            agent._close_cached_request_anthropic_client(reason="fixture_teardown")


def test_failed_client_rebuild_does_not_partially_move_holders():
    old = "fixture-token-A"
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()),
    ):
        agent = AIAgent(
            model="claude-sonnet-4-6", provider="anthropic", api_mode="anthropic_messages",
            api_key=old, base_url="https://api.anthropic.com", quiet_mode=True,
            skip_context_files=True, skip_memory=True,
        )
    with (
        patch("agent.anthropic_credentials.resolve_anthropic_token", return_value="fixture-token-B"),
        patch.object(agent, "_build_direct_anthropic_client", side_effect=RuntimeError("fixture build failure")),
    ):
        assert not agent._try_refresh_anthropic_client_credentials()
    assert agent.api_key == agent._anthropic_api_key == agent.context_compressor.api_key == old
    assert all(agent._primary_runtime[k] == old for k in ("api_key", "anthropic_api_key", "compressor_api_key"))
