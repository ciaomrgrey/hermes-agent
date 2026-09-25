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


def _canonical_path(path: str) -> str:
    return str(Path(path).expanduser().resolve())


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
    synthetic_path: bool = False,
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
        "path": path if synthetic_path else _canonical_path(path),
    }
    if any(item.get(key) != value or not value for key, value in required.items()):
        return False
    expected = item.get("sha256")
    if not isinstance(expected, str) or len(expected) != 64:
        return False
    actual = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return hmac.compare_digest(expected.lower(), actual)


def _evaluate(ctx, tool_name: str, args: Any, task_id: str, session_id: str):
    def unresolved(reason: str, label: str):
        _audit(
            ctx, decision="block", classification="unresolved", reason=reason,
            profile=ctx.profile_name, task_id=task_id, session_id=session_id,
            tool_name=tool_name, path="", content_sha256="",
        )
        return _block(label)

    if not isinstance(args, dict):
        if tool_name in {"write_file", "patch", "execute_code", "terminal"}:
            return unresolved(
                f"malformed {tool_name} payload", f"the unresolved {tool_name} payload",
            )
        return None
    # The third field marks an internal synthetic carrier, never a filesystem path.
    candidates: list[tuple[str, str, bool]] = []
    if tool_name == "write_file":
        path, content = args.get("path"), args.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            return unresolved("malformed write_file payload", "the unresolved write_file payload")
        candidates = [(path, content, False)]
    elif tool_name == "patch":
        extracted = patch_candidates(args)
        if extracted is None:
            return unresolved("unresolved patch projection", "the unresolved patch payload")
        candidates = [(path, content, False) for path, content in extracted]
    elif tool_name == "execute_code":
        code = args.get("code")
        if not isinstance(code, str):
            return unresolved("malformed execute_code payload", "the unresolved execute_code payload")
        embedded = embedded_write_candidates(code)
        candidates = [("<execute_code>.py", code, True)]
        if embedded is None:
            audited = _audit(
                ctx, decision="defer_nested", classification="unresolved",
                reason="nested payload deferred to concrete RPC dispatch",
                profile=ctx.profile_name, task_id=task_id, session_id=session_id,
                tool_name=tool_name, path="<execute_code>.py",
                content_sha256=hashlib.sha256(code.encode("utf-8")).hexdigest(),
            )
            if not audited:
                return _block("the unresolved nested-dispatch audit")
        else:
            candidates.extend(embedded)
    elif tool_name == "terminal":
        command = args.get("command")
        if not isinstance(command, str):
            return unresolved("malformed terminal payload", "the unresolved terminal payload")
        workdir = args.get("workdir")
        if workdir is not None and not isinstance(workdir, str):
            return unresolved("malformed terminal workdir", "the unresolved terminal payload")
        extracted = terminal_candidates(command, workdir)
        if extracted is None:
            return unresolved("unresolved terminal authoring target", "the unresolved terminal payload")
        candidates = extracted
    else:
        return None
    exceptions = ctx.get_config("exceptions", [])
    exceptions = exceptions if isinstance(exceptions, list) else []
    for path, content, synthetic_path in candidates:
        effective_path = path if synthetic_path else _canonical_path(path)
        submitted_code_like = (
            Path(path).expanduser().suffix.lower() in CODE_SUFFIXES
            or content.startswith("#!")
        )
        if not (_is_code_like(effective_path, content) or submitted_code_like):
            continue
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if any(_exception_matches(
            item, profile=ctx.profile_name, task_id=task_id,
            session_id=session_id, path=effective_path, content=content,
            synthetic_path=synthetic_path,
        ) for item in exceptions):
            audited = _audit(
                ctx, decision="allow_exception", classification="exception",
                reason="exact bounded exception", profile=ctx.profile_name,
                task_id=task_id, session_id=session_id, tool_name=tool_name,
                path=effective_path, content_sha256=digest,
            )
            if not audited:
                return _block("the unresolved exception audit")
            continue
        classification_paths: list[str] = []
        for classification_path in (path, effective_path):
            if (
                Path(classification_path).expanduser().suffix.lower() in CODE_SUFFIXES
                and classification_path not in classification_paths
            ):
                classification_paths.append(classification_path)
        if content.startswith("#!") and not classification_paths:
            classification_paths.append(path)
        verdicts = [
            classify_source(
                classification_path,
                content,
                allow_native_rpc=synthetic_path,
            )
            for classification_path in classification_paths
        ]
        verdict = next(
            (item for item in verdicts if item.kind == "substantive"),
            next((item for item in verdicts if item.kind == "unresolved"), verdicts[0]),
        )
        if verdict.kind in {"substantive", "unresolved"}:
            _audit(
                ctx, decision="block", classification=verdict.kind,
                reason=verdict.reason, profile=ctx.profile_name,
                task_id=task_id, session_id=session_id, tool_name=tool_name,
                path=effective_path, content_sha256=digest,
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
