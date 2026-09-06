"""Native, one-shot approval cards for deterministic cron output.

The payload is intentionally narrow: at most two recommendations, each bound to
one exact ``cron.pause`` operation.  Rendering reuses the gateway's existing
slash-confirm callback primitive, which already provides identity authorization,
expiry, and duplicate-click protection.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_JOB_ID_RE = re.compile(r"^[a-f0-9]{12}$")


def parse_approval_card(content: str) -> Optional[dict[str, Any]]:
    """Return a validated control card, or ``None`` for ordinary cron text."""
    stripped = str(content or "").strip()
    if not stripped.startswith("{"):
        return None
    try:
        payload = json.loads(stripped)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("hermes_cron_approval") != 1:
        return None
    if set(payload) != {"hermes_cron_approval", "recommendations"}:
        raise ValueError("cron approval card has unexpected fields")
    recommendations = payload.get("recommendations")
    if not isinstance(recommendations, list) or not 1 <= len(recommendations) <= 2:
        raise ValueError("cron approval card requires one or two recommendations")
    actions_seen: set[str] = set()
    for recommendation in recommendations:
        if not isinstance(recommendation, dict):
            raise ValueError("cron recommendation must be an object")
        if set(recommendation) != {"text", "button", "action"}:
            raise ValueError("cron recommendation has unexpected fields")
        text = recommendation.get("text")
        button = recommendation.get("button")
        action = recommendation.get("action")
        if not isinstance(text, str) or not text.strip() or len(text) > 500:
            raise ValueError("cron recommendation text is missing or too long")
        if not isinstance(button, str) or not button.strip() or len(button) > 64:
            raise ValueError("cron recommendation button is missing or too long")
        if not isinstance(action, dict) or action.get("kind") != "cron.pause":
            raise ValueError("unsupported cron approval action")
        if set(action) != {"kind", "profile", "job_id", "expected_name"}:
            raise ValueError("cron approval action has unexpected fields")
        if not isinstance(action.get("profile"), str) or not _PROFILE_RE.fullmatch(action["profile"]):
            raise ValueError("invalid cron approval profile")
        if not isinstance(action.get("job_id"), str) or not _JOB_ID_RE.fullmatch(action["job_id"]):
            raise ValueError("invalid cron approval job id")
        expected_name = action.get("expected_name")
        if not isinstance(expected_name, str) or not expected_name.strip() or len(expected_name) > 200:
            raise ValueError("cron approval action requires expected_name")
        canonical_action = json.dumps(action, sort_keys=True, separators=(",", ":"))
        if canonical_action in actions_seen:
            raise ValueError("duplicate cron approval action")
        actions_seen.add(canonical_action)
    return payload


def ensure_approval_card_allowed(card: dict[str, Any], source_job: dict[str, Any]) -> None:
    """Fail closed unless every card action exactly matches trusted job config."""
    configured = source_job.get("approval_actions")
    if not isinstance(configured, list):
        configured = []
    allowed = {
        json.dumps(action, sort_keys=True, separators=(",", ":"))
        for action in configured
        if isinstance(action, dict)
    }
    for recommendation in card["recommendations"]:
        canonical = json.dumps(
            recommendation["action"], sort_keys=True, separators=(",", ":"))
        if canonical not in allowed:
            raise ValueError("cron approval action is not explicitly allowed by source job")


def _profile_home(profile: str) -> Path:
    from hermes_constants import get_hermes_home

    current = get_hermes_home().resolve()
    profiles_root = current.parent if current.parent.name == "profiles" else current / "profiles"
    target = (profiles_root / profile).resolve()
    if target.parent != profiles_root or not target.is_dir():
        raise ValueError("target profile is unavailable")
    return target


def _pause_target(action: dict[str, Any]) -> dict[str, Any]:
    """Atomically validate and pause exactly the bound profile/job."""
    from cron.jobs import get_job, pause_job_exact
    from cron.scheduler_provider import _profile_cron_scope

    with _profile_cron_scope(_profile_home(action["profile"])):
        paused = pause_job_exact(
            action["job_id"],
            action["expected_name"],
            reason="Paused by an explicitly approved cron recommendation",
        )
        after = get_job(action["job_id"])
        if not paused or not after or after.get("state") != "paused" or after.get("enabled"):
            raise RuntimeError("cron pause did not persist")
        return after


def _confirmation_ids(
    source_job: dict[str, Any], index: int, action: dict[str, Any],
) -> tuple[str, str]:
    execution_id = source_job.get('execution_id')
    if not isinstance(execution_id, str) or not execution_id:
        raise ValueError('cron approval requires an execution identity')
    canonical_action = json.dumps(action, sort_keys=True, separators=(",", ":"))
    identity = (
        f"{source_job.get('id', '')}:{execution_id}:"
        f"{index}:{canonical_action}")
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return f"cron-approval:{digest}", digest


async def send_approval_card(
    adapter,
    *,
    chat_id: str,
    card: dict[str, Any],
    metadata: Optional[dict[str, Any]],
    source_job: dict[str, Any],
):
    """Register exact handlers first, then render native one-shot controls."""
    from tools import slash_confirm

    ensure_approval_card_allowed(card, source_job)
    sent = []
    registered_session_keys = []
    for index, recommendation in enumerate(card["recommendations"]):
        action = dict(recommendation["action"])
        session_key, confirm_id = _confirmation_ids(source_job, index, action)

        async def handler(choice: str, *, bound_action=action) -> str:
            if choice != "once":
                return "❌ Cancelled; no changes made."
            try:
                paused = _pause_target(bound_action)
            except Exception as exc:
                return f"❌ Approved action failed safely: {type(exc).__name__}."
            return f"✅ Paused {bound_action['profile']}/{paused['name']} ({paused['id']}); state read back: paused."

        command = f"cron.pause:{action['profile']}:{action['job_id']}"
        slash_confirm.register(session_key, confirm_id, command, handler)
        registered_session_keys.append(session_key)
        try:
            result = await adapter.send_slash_confirm(
                chat_id=chat_id,
                title="Cron recommendation approval",
                message=(
                    f"{recommendation['text']}\n\nExact action: pause "
                    f"{action['profile']}/{action['expected_name']} ({action['job_id']})."),
                session_key=session_key,
                confirm_id=confirm_id,
                metadata=metadata,
                allow_always=False,
            )
        except Exception:
            for registered_key in registered_session_keys:
                slash_confirm.clear(registered_key)
            raise
        if not result or not getattr(result, "success", False):
            for registered_key in registered_session_keys:
                slash_confirm.clear(registered_key)
            return SimpleNamespace(success=False, error=getattr(result, "error", "approval render failed"))
        sent.append(result)
    return SimpleNamespace(
        success=True,
        message_id=",".join(str(getattr(result, "message_id", "") or "") for result in sent),
    )
