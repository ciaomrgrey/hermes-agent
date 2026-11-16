"""``gateway.trust_shared_media_roots: false`` confines a profile's reply attachments to its own cache.

One multiplexed gateway serves every ``<root>/profiles/*``. By default the cache of every profile
and the root-shared Kanban attachment dirs are trusted BEFORE the strict check, so a reply from a
strict, public-facing profile that names ``profiles/<other>/cache/documents/x.pdf`` or
``kanban/boards/<board>/attachments/<task>/x.pdf`` uploads it. The key removes those roots for the
profile that sets it and changes nothing for a profile that does not.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

import gateway.platforms.base as base
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult, validate_media_delivery_path
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner, _async_profile_runtime_scope
from gateway.session import SessionSource, build_session_key
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

STRICT = {"strict": True, "trust_recent_files": False, "media_delivery_allow_dirs": []}
CONFINED = {**STRICT, "trust_shared_media_roots": False}


def _file(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4 fixture")
    return str(path)


def _write_gateway_cfg(home: Path, gateway_cfg: dict | None) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"gateway": gateway_cfg} if gateway_cfg is not None else {}), encoding="utf-8")


@pytest.fixture
def estate(tmp_path, monkeypatch):
    """A root with a launch profile, a confined ``dua``, a neighbour ``emma`` and one Kanban board."""
    root = tmp_path / "hermes"
    profiles = root / "profiles"
    launch, dua, emma = profiles / "generalist", profiles / "dua", profiles / "emma"
    board = root / "kanban" / "boards" / "estate"
    _write_gateway_cfg(launch, None)
    _write_gateway_cfg(dua, CONFINED)
    _write_gateway_cfg(emma, None)
    _file(board / "kanban.db")

    monkeypatch.setenv("HERMES_HOME", str(launch))
    for var in ("HERMES_KANBAN_HOME", "HERMES_KANBAN_ATTACHMENTS_ROOT", "HERMES_MEDIA_DELIVERY_STRICT",
                "HERMES_MEDIA_ALLOW_DIRS", "HERMES_MEDIA_TRUST_RECENT_FILES", "TERMINAL_ENV"):
        monkeypatch.delenv(var, raising=False)
    # The import-time constants describe the launch process: repoint them at this estate.
    monkeypatch.setattr(base, "_HERMES_HOME", launch)
    monkeypatch.setattr(base, "_HERMES_ROOT", root)
    monkeypatch.setattr(base, "MEDIA_DELIVERY_SAFE_ROOTS", tuple(
        launch / "cache" / d for d in base._MEDIA_DELIVERY_CACHE_SUBDIRS))

    paths = {
        "outside": _file(tmp_path / "Dropbox" / "statement.pdf"),
        "other_soul": _file(profiles / "sophia" / "SOUL.md"),
        "other_cache": _file(emma / "cache" / "documents" / "lab-result.pdf"),
        "kanban": _file(board / "attachments" / "t_1" / "handoff.pdf"),
        "launch_memory": _file(launch / "memories" / "MEMORY.md"),
        "launch_cache": _file(launch / "cache" / "documents" / "brief.pdf"),
        "own_cache": _file(dua / "cache" / "documents" / "quote.pdf"),
        # Not a trusted root for a routed profile today (only the launch home's legacy dirs are).
        "own_legacy_cache": _file(dua / "document_cache" / "old-quote.pdf"),
        "own_non_cache": _file(dua / "notes.md"),
    }
    return {"root": root, "launch": launch, "dua": dua, "emma": emma, "paths": paths}


def _deliverable(estate, home: Path | None) -> set[str]:
    token = set_hermes_home_override(str(home)) if home is not None else None
    try:
        return {name for name, path in estate["paths"].items() if validate_media_delivery_path(path)}
    finally:
        if token is not None:
            reset_hermes_home_override(token)


def test_confined_profile_delivers_only_its_own_cache(estate):
    assert _deliverable(estate, estate["dua"]) == {"own_cache"}


def test_launch_scope_is_unchanged_while_another_profile_is_confined(estate):
    """The key is read from the delivering profile: dua's setting never narrows the launch profile."""
    assert _deliverable(estate, None) == set(estate["paths"])


def test_unset_key_keeps_the_shared_roots_for_a_strict_profile(estate):
    _write_gateway_cfg(estate["dua"], STRICT)
    assert _deliverable(estate, estate["dua"]) == {
        "other_cache", "kanban", "launch_cache", "own_cache"}


def test_explicit_true_matches_unset(estate):
    _write_gateway_cfg(estate["dua"], {**STRICT, "trust_shared_media_roots": True})
    assert _deliverable(estate, estate["dua"]) == {
        "other_cache", "kanban", "launch_cache", "own_cache"}


def test_confined_profile_keeps_its_operator_allowlist(estate, tmp_path):
    share = tmp_path / "share"
    estate["paths"]["allowlisted"] = _file(share / "price-list.pdf")
    _write_gateway_cfg(estate["dua"], {**CONFINED, "media_delivery_allow_dirs": [str(share)]})
    assert _deliverable(estate, estate["dua"]) == {"own_cache", "allowlisted"}


def test_symlink_in_own_cache_to_another_profile_is_rejected(estate):
    link = estate["dua"] / "cache" / "documents" / "link.pdf"
    link.symlink_to(estate["paths"]["other_cache"])
    estate["paths"]["own_cache_symlink"] = str(link)
    assert "own_cache_symlink" not in _deliverable(estate, estate["dua"])


@pytest.mark.parametrize("value", ["no", "maybe", 0, "", []])
def test_non_true_value_confines(estate, value):
    """A hand-edited value that is not a recognised true reads as confined, never as shared."""
    _write_gateway_cfg(estate["dua"], {**STRICT, "trust_shared_media_roots": value})
    assert _deliverable(estate, estate["dua"]) == {"own_cache"}


def test_config_load_error_reads_as_confined(estate, monkeypatch):
    import hermes_cli.config as config_mod
    from gateway.media_policy import media_delivery_trust_shared_roots

    def _boom():
        raise OSError("config unreadable")
    monkeypatch.setattr(config_mod, "load_config_readonly", _boom)
    assert media_delivery_trust_shared_roots() is False
    assert estate["emma"] / "cache" / "documents" not in base._media_delivery_allowed_roots()


def test_confinement_only_removes_trusted_roots(estate, monkeypatch):
    """Whatever the scope, the confined root list is a subset of the shared one."""
    legacy = estate["launch"] / "document_cache"
    monkeypatch.setattr(base, "MEDIA_DELIVERY_SAFE_ROOTS", (*base.MEDIA_DELIVERY_SAFE_ROOTS, legacy))

    def _roots(home, gateway_cfg):
        _write_gateway_cfg(home, gateway_cfg)
        token = set_hermes_home_override(str(home))
        try:
            return set(base._media_delivery_allowed_roots())
        finally:
            reset_hermes_home_override(token)

    for home in (estate["dua"], estate["launch"]):
        shared, confined = _roots(home, STRICT), _roots(home, CONFINED)
        assert confined < shared
        assert confined == {home / "cache" / d for d in base._MEDIA_DELIVERY_CACHE_SUBDIRS} | (
            {legacy} if home == estate["launch"] else set())


def test_launch_profile_can_confine_itself(estate):
    """Standalone gateway (no HERMES_HOME override): the launch profile's own key applies."""
    _write_gateway_cfg(estate["launch"], CONFINED)
    from hermes_cli.config import load_config
    from gateway.media_policy import apply_media_policy_env
    apply_media_policy_env(load_config())  # what gateway startup does: bridges strict/recency
    assert _deliverable(estate, None) == {"launch_cache"}


class _Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="t"), Platform.TELEGRAM)
        self.uploads: list[str] = []
        self.texts: list[str] = []

    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.texts.append(content)
        return SendResult(success=True, message_id="m")

    async def send_typing(self, chat_id, metadata=None):
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}

    async def send_document(self, chat_id, file_path, caption=None, file_name=None, reply_to=None,
                            metadata=None, **kw):
        self.uploads.append(Path(file_path).name)
        return SendResult(success=True, message_id="d")


async def _hold_typing(_chat_id, interval=2.0, metadata=None, stop_event=None):
    await (stop_event.wait() if stop_event is not None else asyncio.Event().wait())


async def _reply_uploads(estate, profile: str, reply: str) -> list[str]:
    """Run one routed turn through the adapter's real delivery path and return what was uploaded."""
    runner = GatewayRunner(GatewayConfig(multiplex_profiles=True))
    adapter = _Adapter()
    adapter.gateway_runner = runner
    adapter._keep_typing = _hold_typing

    async def routed_handler(event):  # what _make_profile_message_handler does around _handle_message
        event.source.profile = profile
        async with _async_profile_runtime_scope(estate["root"] / "profiles" / profile):
            return reply

    adapter.set_message_handler(routed_handler)
    event = MessageEvent(
        text="send it", message_type=MessageType.TEXT, message_id="m1",
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="-100", chat_type="group", user_id="leo"))
    await adapter._process_message_background(event, build_session_key(event.source, profile=profile))
    return sorted(adapter.uploads)


@pytest.mark.asyncio
async def test_confined_profile_reply_uploads_nothing_from_outside_its_home(estate):
    """End to end: MEDIA tags and bare paths in a dua reply, through the adapter's delivery side."""
    p = estate["paths"]
    reply = (f"Here you go.\nMEDIA:{p['other_cache']}\nMEDIA:{p['kanban']}\nMEDIA:{p['launch_cache']}\n"
             f"MEDIA:{p['outside']}\nAlso {p['other_cache']} and {p['kanban']}\nMEDIA:{p['own_cache']}")
    assert await _reply_uploads(estate, "dua", reply) == ["quote.pdf"]


@pytest.mark.asyncio
async def test_unconfined_profile_reply_still_uploads_shared_roots(estate):
    p = estate["paths"]
    reply = f"Here you go.\nMEDIA:{p['other_cache']}\nMEDIA:{p['kanban']}"
    assert await _reply_uploads(estate, "emma", reply) == ["handoff.pdf", "lab-result.pdf"]


@pytest.mark.asyncio
async def test_confined_profile_streamed_reply_uploads_only_its_own_cache(estate):
    """The post-stream rescan (``_deliver_media_from_response``) runs inside the routed turn scope."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    p = estate["paths"]
    adapter = SimpleNamespace(
        name="test", extract_media=BasePlatformAdapter.extract_media,
        extract_images=BasePlatformAdapter.extract_images,
        send_document=AsyncMock(return_value=SendResult(success=True, message_id="d")))
    runner = SimpleNamespace(
        _thread_metadata_for_source=lambda source, anchor=None: {},
        _reply_anchor_for_event=lambda event: None)
    event = MessageEvent(
        text="send it", message_type=MessageType.TEXT, message_id="m1",
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="-100", chat_type="group", user_id="leo"))
    reply = f"MEDIA:{p['other_cache']}\nMEDIA:{p['kanban']}\nMEDIA:{p['launch_cache']}\nMEDIA:{p['own_cache']}"

    async with _async_profile_runtime_scope(estate["dua"]):
        await GatewayRunner._deliver_media_from_response(runner, reply, event, adapter)

    assert [Path(c.kwargs["file_path"]).name for c in adapter.send_document.await_args_list] == ["quote.pdf"]
