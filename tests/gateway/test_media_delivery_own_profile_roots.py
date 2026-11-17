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
import hermes_yaml as yaml

from gateway.platforms import base
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


def test_confined_profile_needs_no_other_setting(estate):
    """The key alone confines: a confined profile that is not strict (every fixture file is also
    seconds old, so recency trust would admit them all) still delivers only its own cache."""
    _write_gateway_cfg(estate["dua"], {"trust_shared_media_roots": False})
    assert _deliverable(estate, estate["dua"]) == {"own_cache"}
    _write_gateway_cfg(estate["dua"], {"strict": True, "trust_recent_files": True,
                                       "trust_shared_media_roots": False})
    assert _deliverable(estate, estate["dua"]) == {"own_cache"}


def test_confined_profile_ignores_an_inherited_media_environment(estate, tmp_path, monkeypatch):
    """A separate process for the profile (``hermes -p dua send``, a cron run, a Kanban worker):
    HERMES_HOME is the profile, no override is bound, and the process inherits the gateway's
    ``HERMES_MEDIA_*`` variables. Those are another profile's policy and must not widen this one."""
    dua = estate["dua"]
    monkeypatch.setenv("HERMES_HOME", str(dua))
    monkeypatch.setattr(base, "_HERMES_HOME", dua)
    monkeypatch.setattr(base, "MEDIA_DELIVERY_SAFE_ROOTS", tuple(
        dua / "cache" / d for d in base._MEDIA_DELIVERY_CACHE_SUBDIRS))
    monkeypatch.setenv("HERMES_MEDIA_DELIVERY_STRICT", "0")
    monkeypatch.setenv("HERMES_MEDIA_TRUST_RECENT_FILES", "1")
    monkeypatch.setenv("HERMES_MEDIA_ALLOW_DIRS", str(tmp_path / "Dropbox"))
    assert _deliverable(estate, None) == {"own_cache"}


SHARED_STRICT = {"other_cache", "kanban", "launch_cache", "own_cache"}


# Two files the real loader cannot use. It does not raise for either: it serves a fallback
# (``_load_config_impl``). MALFORMED also fails the routed turn's terminal-policy read, which
# refuses the whole turn; NOT_A_MAPPING parses as YAML, so a routed turn reaches the validator.
MALFORMED = "gateway: [broken\n"
NOT_A_MAPPING = "- strict\n- trust_recent_files\n"
BROKEN = pytest.mark.parametrize("broken", [MALFORMED, NOT_A_MAPPING], ids=["malformed", "not-a-mapping"])


def _break_config(home: Path, content: str = MALFORMED) -> None:
    (home / "config.yaml").write_text(content, encoding="utf-8")


def _fail_policy_reads(monkeypatch) -> None:
    """Make every config read made BY THE MEDIA POLICY raise (the rest of the turn reads normally)."""
    import sys

    import hermes_cli.config as config_mod
    real = config_mod.load_config_readonly

    def _boom():
        if sys._getframe(1).f_globals.get("__name__") == "gateway.media_policy":
            raise OSError("config unreadable")
        return real()
    monkeypatch.setattr(config_mod, "load_config_readonly", _boom)


def _forget_loaded_config(home: Path) -> None:
    """What a process restart does to the in-memory last-known-good (the on-disk backup stays)."""
    import hermes_cli.config as config_mod
    for memo in (config_mod._LOAD_CONFIG_CACHE, config_mod._LAST_EXPANDED_CONFIG_BY_PATH):
        memo.pop(str(home / "config.yaml"), None)


@BROKEN
def test_unusable_config_without_last_known_good_delivers_only_own_cache(estate, broken):
    """Defaults stand in for the file, and defaults say shared + non-strict: nothing of the
    profile's policy is left, so the validator must not fall back to them."""
    _break_config(estate["dua"], broken)
    assert _deliverable(estate, estate["dua"]) == {"own_cache"}


def test_policy_read_exception_delivers_only_own_cache(estate, monkeypatch):
    _fail_policy_reads(monkeypatch)
    assert _deliverable(estate, estate["dua"]) == {"own_cache"}


def test_non_mapping_gateway_section_delivers_only_own_cache(estate):
    (estate["dua"] / "config.yaml").write_text("gateway: [strict, trust_recent_files]\n", encoding="utf-8")
    assert _deliverable(estate, estate["dua"]) == {"own_cache"}


def test_unreadable_policy_confines_whichever_profile_is_delivering(estate):
    """With no policy left to read, a profile that was never confined cannot be told apart from
    one that was: both keep only their own cache until config.yaml is fixed. Launch scope too,
    whatever the env bridge says."""
    _break_config(estate["emma"])
    assert _deliverable(estate, estate["emma"]) == {"other_cache"}  # emma's own cache document
    _break_config(estate["launch"])
    assert _deliverable(estate, None) == {"launch_cache"}


@BROKEN
def test_last_known_good_policy_is_kept_when_the_file_breaks(estate, tmp_path, broken):
    """A last-known-good fallback IS the profile's policy: honoured as loaded, not replaced by the
    unreadable-policy rule. In-process first, then from the on-disk backup after a 'restart'."""
    share = tmp_path / "share"
    estate["paths"]["allowlisted"] = _file(share / "price-list.pdf")
    _write_gateway_cfg(estate["dua"], {**CONFINED, "media_delivery_allow_dirs": [str(share)]})
    _write_gateway_cfg(estate["emma"], STRICT)
    assert _deliverable(estate, estate["dua"]) == {"own_cache", "allowlisted"}
    assert _deliverable(estate, estate["emma"]) == SHARED_STRICT

    for home in (estate["dua"], estate["emma"]):
        _break_config(home, broken)
    assert _deliverable(estate, estate["dua"]) == {"own_cache", "allowlisted"}
    assert _deliverable(estate, estate["emma"]) == SHARED_STRICT

    for home in (estate["dua"], estate["emma"]):
        _forget_loaded_config(home)
    assert _deliverable(estate, estate["dua"]) == {"own_cache", "allowlisted"}
    assert _deliverable(estate, estate["emma"]) == SHARED_STRICT


def test_profile_without_a_config_file_is_unchanged(estate):
    """No config.yaml is a healthy default profile, not a failed read: today's non-strict behaviour."""
    (estate["emma"] / "config.yaml").unlink()
    assert _deliverable(estate, estate["emma"]) == set(estate["paths"])


def test_unreadable_policy_does_not_fetch_from_a_remote_sandbox(estate, monkeypatch):
    """``fetch_remote_media`` copies a sandbox file into the launch cache unless strict."""
    from gateway import media_fetch

    def _no_env():
        raise AssertionError("remote sandbox consulted under an unreadable policy")
    monkeypatch.setattr(media_fetch, "_active_remote_env", _no_env)
    _break_config(estate["dua"])
    token = set_hermes_home_override(str(estate["dua"]))
    try:
        assert media_fetch.fetch_remote_media("/workspace/report.pdf") is None
    finally:
        reset_hermes_home_override(token)


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


def _all_tags(estate) -> str:
    return "\n".join(f"MEDIA:{path}" for path in estate["paths"].values())


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [NOT_A_MAPPING, "gateway: [strict, trust_recent_files]\n"],
                         ids=["not-a-mapping", "gateway-section-not-a-mapping"])
async def test_reply_under_unusable_config_uploads_only_own_cache(estate, broken):
    _break_config(estate["dua"], broken)
    assert await _reply_uploads(estate, "dua", _all_tags(estate)) == ["quote.pdf"]


@pytest.mark.asyncio
async def test_reply_under_malformed_config_uploads_nothing(estate):
    """Malformed YAML never reaches the validator in a routed turn: the profile's terminal policy
    cannot be built either, and that refusal fails the reply's delivery step (own cache included)."""
    _break_config(estate["dua"], MALFORMED)
    assert await _reply_uploads(estate, "dua", _all_tags(estate)) == []


@pytest.mark.asyncio
async def test_reply_under_policy_read_exception_uploads_only_own_cache(estate, monkeypatch):
    _fail_policy_reads(monkeypatch)
    assert await _reply_uploads(estate, "dua", _all_tags(estate)) == ["quote.pdf"]


@pytest.mark.asyncio
async def test_reply_after_the_file_breaks_keeps_the_last_known_good_policy(estate):
    """Control for the tests above: a confined profile stays confined on its last-known-good, and
    an unconfined strict one keeps the shared roots it had."""
    _write_gateway_cfg(estate["emma"], STRICT)
    for profile in ("dua", "emma"):
        assert _deliverable(estate, estate[profile])  # one good load: in-process LKG + backup
        _break_config(estate[profile], NOT_A_MAPPING)
    assert await _reply_uploads(estate, "dua", _all_tags(estate)) == ["quote.pdf"]
    assert await _reply_uploads(estate, "emma", _all_tags(estate)) == [
        "brief.pdf", "handoff.pdf", "lab-result.pdf", "quote.pdf"]


@pytest.mark.asyncio
async def test_unconfined_profile_reply_still_uploads_shared_roots(estate):
    p = estate["paths"]
    reply = f"Here you go.\nMEDIA:{p['other_cache']}\nMEDIA:{p['kanban']}"
    assert await _reply_uploads(estate, "emma", reply) == ["handoff.pdf", "lab-result.pdf"]


@pytest.mark.asyncio
async def test_confined_profile_kanban_artifacts_upload_only_its_own_cache(estate):
    """The Kanban notifier's artifact upload goes through the same validator. Its caller
    (``kanban_watchers_notifier.deliver``) binds the subscriber profile's scope; this drives the
    upload step inside that scope with artifacts named both as a list and in the summary prose."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    p = estate["paths"]
    adapter = SimpleNamespace(
        extract_local_files=lambda text: BasePlatformAdapter.extract_local_files(text),
        send_document=AsyncMock(return_value=SendResult(success=True, message_id="d")),
        send_video=AsyncMock(), send_multiple_images=AsyncMock())
    payload = {"artifacts": [p["kanban"], p["other_cache"], p["launch_cache"], p["own_cache"]],
               "summary": f"Report at {p['outside']} and {p['kanban']}"}

    async with _async_profile_runtime_scope(estate["dua"]):
        await GatewayRunner._deliver_kanban_artifacts(
            SimpleNamespace(), adapter=adapter, chat_id="-100", metadata={}, event_payload=payload, task=None)

    assert [Path(c.kwargs["file_path"]).name for c in adapter.send_document.await_args_list] == ["quote.pdf"]


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
