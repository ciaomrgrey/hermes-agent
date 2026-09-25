"""Pre-dispatch admission policy for domain-seat authored executable tooling."""
from __future__ import annotations

import hashlib
import hmac
import logging
import math
import time
from pathlib import Path
from typing import Any

from .audit import append_record
from .policy import (
    classify_source, embedded_write_candidates, patch_candidates, terminal_candidates,
)


DEFAULT_PROFILES = ("emma", "sophia", "plutus", "jared")
logger = logging.getLogger(__name__)
CODE_SUFFIXES = frozenset({
    ".py", ".pyw", ".sh", ".bash", ".zsh", ".fish", ".js", ".mjs", ".cjs",
    ".ts", ".tsx", ".jsx", ".sql", ".rb", ".php", ".go", ".rs", ".java",
    ".kt", ".kts", ".swift", ".c", ".h", ".cc", ".cpp", ".cs",
})


def _is_code_like(path: str, content: str) -> bool:
    return Path(path).suffix.lower() in CODE_SUFFIXES or content.startswith("#!")


def _block(path: str) -> dict[str, str]:
    return {
        "action": "block",
        "message": (
            "BLOCKED: substantive executable task tooling is Cody-owned. "
            f"Reuse or create a Cody Kanban card for {path}."
        ),
    }


def _audit(ctx, **fields: Any) -> bool:
    try:
        configured = ctx.get_config("audit_path", None)
        if isinstance(configured, str) and configured:
            path = configured
        else:
            from hermes_constants import get_hermes_home
            path = get_hermes_home() / "state" / "domain-code-policy" / "audit.jsonl"
        append_record(path, **fields)
        return True
    except Exception:
        logger.warning("domain-code-policy audit write failed", exc_info=True)
        return False


def _exception_matches(
    item: Any, *, profile: str, task_id: str, session_id: str, path: str, content: str,
) -> bool:
    if not isinstance(item, dict):
        return False
    expires = item.get("expires_at")
    if isinstance(expires, bool) or not isinstance(expires, (int, float)):
        return False
    if not math.isfinite(float(expires)) or float(expires) <= time.time():
        return False
    required = {
        "profile": profile,
        "task_id": task_id,
        "session_id": session_id,
        "path": str(Path(path).resolve()),
    }
    if any(item.get(key) != value or not value for key, value in required.items()):
        return False
    expected = item.get("sha256")
    if not isinstance(expected, str) or len(expected) != 64:
        return False
    actual = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return hmac.compare_digest(expected.lower(), actual)


def _evaluate(ctx, tool_name: str, args: Any, task_id: str, session_id: str):
    if not isinstance(args, dict):
        return _block("the unresolved code-like write") if tool_name in {
            "write_file", "patch", "execute_code"
        } else None
    candidates: list[tuple[str, str]] = []
    if tool_name == "write_file":
        path, content = args.get("path"), args.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            return _block("the unresolved code-like write")
        candidates = [(path, content)]
    elif tool_name == "patch":
        extracted = patch_candidates(args)
        if extracted is None:
            return _block("the unresolved code-like patch")
        candidates = extracted
    elif tool_name == "execute_code":
        code = args.get("code")
        if not isinstance(code, str):
            return _block("the unresolved code-like write")
        embedded = embedded_write_candidates(code)
        if embedded is None:
            return _block("the unresolved execute_code source")
        candidates = [("<execute_code>.py", code), *embedded]
    elif tool_name == "terminal":
        command = args.get("command")
        if not isinstance(command, str):
            return None
        candidates = terminal_candidates(command)
    else:
        return None
    exceptions = ctx.get_config("exceptions", [])
    exceptions = exceptions if isinstance(exceptions, list) else []
    for path, content in candidates:
        if not _is_code_like(path, content):
            continue
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if any(_exception_matches(
            item, profile=ctx.profile_name, task_id=task_id,
            session_id=session_id, path=path, content=content,
        ) for item in exceptions):
            audited = _audit(
                ctx, decision="allow_exception", classification="exception",
                reason="exact bounded exception", profile=ctx.profile_name,
                task_id=task_id, session_id=session_id, tool_name=tool_name,
                path=str(Path(path).resolve()), content_sha256=digest,
            )
            if not audited:
                return _block("the unresolved exception audit")
            continue
        verdict = classify_source(path, content)
        if verdict.kind in {"substantive", "unresolved"}:
            _audit(
                ctx, decision="block", classification=verdict.kind,
                reason=verdict.reason, profile=ctx.profile_name,
                task_id=task_id, session_id=session_id, tool_name=tool_name,
                path=str(Path(path).resolve()), content_sha256=digest,
            )
            return _block(path)
    return None


def register(ctx) -> None:
    def pre_tool_call(
        tool_name: str = "", args: Any = None, task_id: str = "",
        session_id: str = "", **_: Any,
    ) -> dict[str, str] | None:
        if ctx.get_config("enabled", False) is not True:
            return None
        profiles = ctx.get_config("profiles", list(DEFAULT_PROFILES))
        if not isinstance(profiles, list) or ctx.profile_name not in profiles:
            return None
        try:
            return _evaluate(ctx, tool_name, args, task_id, session_id)
        except Exception:
            logger.warning("domain-code-policy evaluation failed closed", exc_info=True)
            _audit(
                ctx, decision="block", classification="unresolved",
                reason="policy evaluation error", profile=ctx.profile_name,
                task_id=task_id, session_id=session_id, tool_name=tool_name,
                path="", content_sha256="",
            )
            return _block("the unresolved policy evaluation")

    ctx.register_hook("pre_tool_call", pre_tool_call)
