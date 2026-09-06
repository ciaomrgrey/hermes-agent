import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


def envelope(*items):
    return json.dumps({"hermes_cron_approval": 1, "recommendations": list(items)})


def recommendation(
    text="⚠️ Pause noisy watcher: 30 calls/1h (trigger 25).",
    job_id="ce58ebbaa272",
    expected_name="Night watcher",
):
    return {
        "text": text,
        "button": "Approve pause",
        "action": {
            "kind": "cron.pause",
            "profile": "gurney",
            "job_id": job_id,
            "expected_name": expected_name,
        },
    }


def test_parser_accepts_at_most_two_exact_bound_actions():
    from cron.approval_cards import parse_approval_card

    card = parse_approval_card(envelope(
        recommendation(),
        recommendation("second trigger 25", "b123456789ab", "Second watcher"),
    ))
    assert card is not None
    assert len(card["recommendations"]) == 2
    assert card["recommendations"][0]["action"]["job_id"] == "ce58ebbaa272"


@pytest.mark.parametrize(
    "payload",
    [
        envelope(),
        envelope(recommendation(), recommendation(), recommendation()),
        envelope({**recommendation(), "action": {"kind": "shell", "command": "rm -rf /"}}),
        envelope({**recommendation(), "action": {**recommendation()["action"], "profile": "../gurney"}}),
        envelope({**recommendation(), "action": {**recommendation()["action"], "job_id": "not-an-id"}}),
    ],
)
def test_parser_rejects_empty_oversized_or_unbounded_actions(payload):
    from cron.approval_cards import parse_approval_card

    with pytest.raises(ValueError):
        parse_approval_card(payload)


def test_plain_cron_output_is_not_a_control_card():
    from cron.approval_cards import parse_approval_card

    assert parse_approval_card("✅ Nothing to report.") is None


def test_confirmation_identity_is_unique_per_execution():
    from cron.approval_cards import _confirmation_ids

    action = recommendation()["action"]
    first = _confirmation_ids(
        {"id": "5291b75fe0f1", "execution_id": "exec-1"}, 0, action)
    second = _confirmation_ids(
        {"id": "5291b75fe0f1", "execution_id": "exec-2"}, 0, action)
    assert first != second


def test_pause_job_exact_checks_bound_name_inside_mutation_lock(monkeypatch):
    from cron import jobs

    stored = [{
        "id": "ce58ebbaa272", "name": "Night watcher",
        "enabled": True, "state": "scheduled",
    }]
    monkeypatch.setattr(jobs, "load_jobs", lambda: stored)
    monkeypatch.setattr(jobs, "save_jobs", lambda value: None)

    with pytest.raises(ValueError):
        jobs.pause_job_exact("ce58ebbaa272", "Renamed watcher")
    assert stored[0]["enabled"] is True

    paused = jobs.pause_job_exact("ce58ebbaa272", "Night watcher")
    assert paused is not None
    assert paused["state"] == "paused"
    assert stored[0]["enabled"] is False


@pytest.mark.asyncio
async def test_native_card_registers_before_render_and_never_offers_always(monkeypatch):
    from cron import approval_cards
    from tools import slash_confirm

    registered = []
    monkeypatch.setattr(
        slash_confirm,
        "register",
        lambda session_key, confirm_id, command, handler: registered.append(
            (session_key, confirm_id, command, handler)
        ),
    )
    adapter = SimpleNamespace(send_slash_confirm=AsyncMock(return_value=SimpleNamespace(success=True)))
    card = approval_cards.parse_approval_card(envelope(recommendation()))

    result = await approval_cards.send_approval_card(
        adapter,
        chat_id="471605389",
        card=card,
        metadata=None,
        source_job={
            "id": "5291b75fe0f1", "execution_id": "exec-1",
            "approval_actions": [recommendation()["action"]],
        },
    )

    assert result.success is True
    assert len(registered) == 1
    kwargs = adapter.send_slash_confirm.call_args.kwargs
    assert kwargs["allow_always"] is False
    assert "trigger 25" in kwargs["message"]
    assert registered[0][2] == "cron.pause:gurney:ce58ebbaa272"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "allowed_actions",
    [
        None,
        [],
        [{**recommendation()["action"], "profile": "ripley"}],
        [{**recommendation()["action"], "job_id": "b123456789ab"}],
        [{**recommendation()["action"], "expected_name": "Renamed watcher"}],
    ],
)
async def test_native_card_rejects_actions_not_exactly_allowed_by_source_job(
    monkeypatch, allowed_actions,
):
    from cron import approval_cards
    from tools import slash_confirm

    register = Mock()
    monkeypatch.setattr(slash_confirm, "register", register)
    adapter = SimpleNamespace(send_slash_confirm=AsyncMock())
    source_job = {"id": "5291b75fe0f1", "execution_id": "exec-denied"}
    if allowed_actions is not None:
        source_job["approval_actions"] = allowed_actions

    with pytest.raises(ValueError, match="not explicitly allowed"):
        await approval_cards.send_approval_card(
            adapter,
            chat_id="471605389",
            card=approval_cards.parse_approval_card(envelope(recommendation())),
            metadata=None,
            source_job=source_job,
        )

    register.assert_not_called()
    adapter.send_slash_confirm.assert_not_awaited()


@pytest.mark.asyncio
async def test_approve_once_pauses_exact_bound_job_and_deny_has_no_effect(monkeypatch):
    from cron import approval_cards
    from tools import slash_confirm

    effects = []
    monkeypatch.setattr(
        approval_cards,
        "_pause_target",
        lambda action: effects.append(dict(action)) or {
            "id": action["job_id"], "name": action["expected_name"], "state": "paused"
        },
    )
    adapter = SimpleNamespace(send_slash_confirm=AsyncMock(return_value=SimpleNamespace(success=True)))
    card = approval_cards.parse_approval_card(envelope(recommendation()))
    await approval_cards.send_approval_card(
        adapter, chat_id="1", card=card, metadata=None,
        source_job={
            "id": "5291b75fe0f1", "execution_id": "exec-2",
            "approval_actions": [recommendation()["action"]],
        },
    )
    session_key, confirm_id = adapter.send_slash_confirm.call_args.kwargs["session_key"], adapter.send_slash_confirm.call_args.kwargs["confirm_id"]
    denied = await slash_confirm.resolve(session_key, confirm_id, "cancel")
    assert "no changes" in denied.lower()
    assert effects == []

    await approval_cards.send_approval_card(
        adapter, chat_id="1", card=card, metadata=None,
        source_job={
            "id": "5291b75fe0f1", "execution_id": "exec-3",
            "approval_actions": [recommendation()["action"]],
        },
    )
    session_key, confirm_id = adapter.send_slash_confirm.call_args.kwargs["session_key"], adapter.send_slash_confirm.call_args.kwargs["confirm_id"]
    approved = await slash_confirm.resolve(session_key, confirm_id, "once")
    assert "paused" in approved.lower()
    assert len(effects) == 1
    assert await slash_confirm.resolve(session_key, confirm_id, "once") is None
    assert len(effects) == 1


def test_scheduler_routes_control_envelope_to_native_live_adapter(monkeypatch):
    from cron import scheduler_delivery as delivery
    from gateway.config import Platform

    card_text = envelope(recommendation())
    target = SimpleNamespace(
        live_adapter_ready=True,
        platform_name="telegram",
        platform=Platform.TELEGRAM,
        chat_id="471605389",
        where="telegram:471605389",
        job={
            "id": "5291b75fe0f1", "no_agent": True,
            "approval_controls": True,
            "approval_actions": [recommendation()["action"]],
        },
    )
    routed = []
    monkeypatch.setattr(delivery, "_resolve_delivery_targets", lambda job, for_failure=False: [
        {"platform": "telegram", "chat_id": "471605389"}
    ])
    monkeypatch.setattr(delivery, "_prepare_target_delivery", lambda *args, **kwargs: target)
    monkeypatch.setattr(
        delivery,
        "_deliver_approval_card_via_live_adapter",
        lambda t, card, errors, execution_id: routed.append((t, card)) or True,
    )
    monkeypatch.setattr(
        delivery,
        "_deliver_via_live_adapter",
        lambda *args, **kwargs: pytest.fail("control card fell through to plain text delivery"),
    )
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: SimpleNamespace())

    error = delivery._deliver_result(
        target.job, card_text, adapters={"telegram": object()}, loop=SimpleNamespace(),
        execution_id="exec-4",
    )

    assert error is None
    assert len(routed) == 1
    assert routed[0][1]["recommendations"][0]["action"]["job_id"] == "ce58ebbaa272"


def test_agent_cron_cannot_mint_approval_controls(monkeypatch):
    from cron import scheduler_delivery as delivery

    monkeypatch.setattr(
        delivery, "_resolve_delivery_targets",
        lambda job, for_failure=False: [{"platform": "telegram", "chat_id": "1"}],
    )
    error = delivery._deliver_result(
        {"id": "agent-job", "no_agent": False, "approval_controls": True},
        envelope(recommendation()),
        execution_id="exec-agent",
    )
    assert "trusted no-agent job" in error
