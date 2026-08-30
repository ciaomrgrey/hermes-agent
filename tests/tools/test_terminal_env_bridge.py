"""Behavioral regressions for the terminal config → env bridge.

``terminal_tool._get_env_config()`` reads TERMINAL_* variables.  The bridge
must let explicitly configured terminal keys override stale launcher/.env
values while preserving environment values for terminal keys omitted from
config.yaml.
"""

import os
import sys

import pytest

import tools.terminal_tool as terminal_tool
from agent import secret_scope
from hermes_constants import (
    get_hermes_home,
    reset_hermes_home_override,
    set_hermes_home_override,
)


@pytest.fixture(autouse=True)
def _reset_bridge_state(monkeypatch):
    """Each test starts with an un-attempted bridge and clean mapped env."""
    monkeypatch.setattr(terminal_tool, "_terminal_config_bridge_attempted", False)
    for name in (
        "TERMINAL_ENV",
        "TERMINAL_CWD",
        "TERMINAL_DOCKER_IMAGE",
        "TERMINAL_SSH_HOST",
    ):
        monkeypatch.delenv(name, raising=False)
    secret_scope.set_multiplex_active(False)
    yield
    secret_scope.set_multiplex_active(False)


def _write_config(text: str) -> None:
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(text)


def test_unset_terminal_env_backfills_backend_from_config():
    _write_config(
        "terminal:\n"
        "  backend: docker\n"
        "  docker_image: custom/image:1\n"
    )

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "docker"
    assert config["docker_image"] == "custom/image:1"
    assert os.environ["TERMINAL_ENV"] == "docker"


def test_explicit_config_backend_overrides_stale_env(monkeypatch):
    _write_config("terminal:\n  backend: docker\n")
    monkeypatch.setenv("TERMINAL_ENV", "local")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "docker"
    assert os.environ["TERMINAL_ENV"] == "docker"


def test_partial_terminal_config_preserves_unrelated_env_values(monkeypatch):
    _write_config("terminal:\n  backend: docker\n")
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_DOCKER_IMAGE", "env/image:2")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "docker"
    assert config["docker_image"] == "env/image:2"
    assert os.environ["TERMINAL_DOCKER_IMAGE"] == "env/image:2"


def test_explicit_config_key_overrides_matching_env_value(monkeypatch):
    _write_config(
        "terminal:\n"
        "  backend: docker\n"
        "  docker_image: config/image:1\n"
    )
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_DOCKER_IMAGE", "env/image:2")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "docker"
    assert config["docker_image"] == "config/image:1"


def test_ssh_config_preserves_remote_tilde_cwd(monkeypatch):
    """SSH ``~`` belongs to the remote user, not the Hermes host/container."""
    _write_config("terminal:\n  backend: ssh\n  cwd: '~'\n")
    monkeypatch.setenv("HOME", "/opt/data/home")
    monkeypatch.setenv("USERPROFILE", r"C:\opt\data\home")

    config = terminal_tool._get_env_config()

    assert os.environ["TERMINAL_CWD"] == "~"
    assert config["cwd"] == "~"


def test_env_is_preserved_when_config_has_no_terminal_section(monkeypatch):
    _write_config("agent:\n  max_turns: 100\n")
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "example.test")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "ssh"
    assert config["ssh_host"] == "example.test"


def test_defaults_backfill_when_neither_config_nor_env_selects_backend():
    _write_config("{}\n")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "local"
    assert os.environ["TERMINAL_ENV"] == "local"


def test_bridge_only_attempted_once(monkeypatch):
    calls = []

    import hermes_cli.config as config_mod

    real = config_mod.apply_terminal_config_to_env

    def _counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(config_mod, "apply_terminal_config_to_env", _counting)
    _write_config("{}\n")

    terminal_tool._get_env_config()
    terminal_tool._get_env_config()

    assert len(calls) == 1


def test_bridge_config_failure_does_not_crash(monkeypatch):
    import hermes_cli.config as config_mod

    monkeypatch.setattr(
        config_mod,
        "read_raw_config",
        lambda: (_ for _ in ()).throw(RuntimeError("config read failed")),
    )
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "example.test")

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "ssh"
    assert config["ssh_host"] == "example.test"


def test_multiplex_scopes_resolve_each_profiles_terminal_config(monkeypatch, tmp_path):
    """A routed profile must not inherit the gateway owner's terminal config."""
    owner = tmp_path / "profiles" / "owner"
    routed = tmp_path / "profiles" / "routed"
    owner.mkdir(parents=True)
    routed.mkdir(parents=True)
    (owner / "config.yaml").write_text(
        f"terminal:\n  backend: local\n  cwd: {owner}\n",
        encoding="utf-8",
    )
    (routed / "config.yaml").write_text(
        "terminal:\n  backend: ssh\n  cwd: '~'\n  ssh_host: routed.example\n",
        encoding="utf-8",
    )

    # Gateway startup bridged its owner's config into the process environment.
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(owner))
    monkeypatch.setattr(terminal_tool, "_terminal_config_bridge_attempted", True)
    secret_scope.set_multiplex_active(True)

    token = set_hermes_home_override(str(routed))
    try:
        config = terminal_tool._get_env_config()
    finally:
        reset_hermes_home_override(token)

    assert config["env_type"] == "ssh"
    assert config["cwd"] == "~"
    assert config["ssh_host"] == "routed.example"
    assert os.environ["TERMINAL_CWD"] == str(owner)


def test_multiplex_omitted_keys_do_not_inherit_gateway_owner(monkeypatch, tmp_path):
    """A partial routed config gets defaults, never owner terminal state."""
    routed = tmp_path / "profiles" / "routed"
    routed.mkdir(parents=True)
    (routed / "config.yaml").write_text(
        "terminal:\n  timeout: 42\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_CWD", "/owner/workspace")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "owner.example")
    monkeypatch.setattr(terminal_tool, "_terminal_config_bridge_attempted", True)
    secret_scope.set_multiplex_active(True)

    token = set_hermes_home_override(str(routed))
    try:
        config = terminal_tool._get_env_config()
    finally:
        reset_hermes_home_override(token)

    assert config["env_type"] == "local"
    assert config["cwd"] != "/owner/workspace"
    assert config["ssh_host"] == ""
    assert config["timeout"] == 42


def test_multiplex_bridge_failure_never_falls_back_to_owner_env(monkeypatch, tmp_path):
    """Config-read failures fail isolated instead of exposing owner settings."""
    import hermes_cli.config as config_mod
    from tools.code_execution_tool import _build_child_env
    from tools.environments.local import _make_run_env, _sanitize_subprocess_env

    routed = tmp_path / "profiles" / "routed"
    (routed / "home").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(routed))
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_CWD", "/owner/workspace")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "owner.example")
    monkeypatch.setenv("TERMINAL_HOME_MODE", "profile")
    monkeypatch.setattr(
        config_mod,
        "load_config_readonly",
        lambda: (_ for _ in ()).throw(RuntimeError("scoped config failed")),
    )
    secret_scope.set_multiplex_active(True)

    config = terminal_tool._get_env_config()

    assert config["env_type"] == "local"
    assert config["cwd"] != "/owner/workspace"
    assert config["ssh_host"] == ""
    child_envs = (
        _make_run_env({}),
        _sanitize_subprocess_env(dict(os.environ)),
        _build_child_env(
            rpc_endpoint="rpc",
            rpc_token="token",
            tmpdir="/tmp/staging",
            child_python=sys.executable,
        ),
    )
    for child_env in child_envs:
        assert child_env.get("TERMINAL_HOME_MODE", "auto") == "auto"
        assert child_env["HOME"] != os.path.join(get_hermes_home(), "home")


def test_multiplex_file_environments_keep_profile_cwds_isolated(monkeypatch, tmp_path):
    """Distinct routed sessions create local envs at their profile's cwd."""
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools import file_tools

    owner = tmp_path / "profiles" / "owner"
    routed = tmp_path / "profiles" / "routed"
    owner.mkdir(parents=True)
    routed.mkdir(parents=True)
    for home in (owner, routed):
        (home / "config.yaml").write_text(
            f"terminal:\n  backend: local\n  cwd: {home}\n",
            encoding="utf-8",
        )

    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(owner))
    monkeypatch.setattr(terminal_tool, "_terminal_config_bridge_attempted", True)
    secret_scope.set_multiplex_active(True)

    def create(profile, home, session_key):
        session_tokens = set_session_vars(session_key=session_key, profile=profile)
        home_token = set_hermes_home_override(str(home))
        try:
            return file_tools._get_file_ops(session_key).env
        finally:
            reset_hermes_home_override(home_token)
            clear_session_vars(session_tokens)

    try:
        owner_env = create("owner", owner, "agent:owner:slack:channel:a")
        routed_env = create("routed", routed, "agent:routed:slack:channel:b")

        assert owner_env is not routed_env
        assert owner_env.cwd == str(owner)
        assert routed_env.cwd == str(routed)
        assert set(terminal_tool._active_environments) == {
            "session:agent:owner:slack:channel:a",
            "session:agent:routed:slack:channel:b",
        }
    finally:
        file_tools.clear_file_ops_cache()
        with terminal_tool._env_lock:
            terminal_tool._active_environments.clear()
            terminal_tool._last_activity.clear()


def test_multiplex_sandbox_and_temp_dirs_use_routed_profile(monkeypatch, tmp_path):
    """Auxiliary terminal paths must not fall back to gateway-owner env."""
    from tools.environments.base import get_sandbox_dir
    from tools.environments.local import LocalEnvironment

    routed = tmp_path / "profiles" / "routed"
    routed_temp = routed / "private-temp"
    routed_sandboxes = routed / "private-sandboxes"
    owner_temp = tmp_path / "owner-temp"
    owner_sandboxes = tmp_path / "owner-sandboxes"
    for path in (routed, routed_temp, routed_sandboxes, owner_temp, owner_sandboxes):
        path.mkdir(parents=True, exist_ok=True)
    (routed / "config.yaml").write_text(
        "terminal:\n"
        f"  temp_dir: {routed_temp}\n"
        f"  sandbox_dir: {routed_sandboxes}\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("TERMINAL_TEMP_DIR", str(owner_temp))
    monkeypatch.setenv("TERMINAL_SANDBOX_DIR", str(owner_sandboxes))
    secret_scope.set_multiplex_active(True)

    token = set_hermes_home_override(str(routed))
    try:
        local = object.__new__(LocalEnvironment)
        local.env = os.environ.copy()
        assert get_sandbox_dir() == routed_sandboxes
        assert local.get_temp_dir() == str(routed_temp)
    finally:
        reset_hermes_home_override(token)


def test_multiplex_relative_file_paths_use_routed_profile_cwd(monkeypatch, tmp_path):
    """File tools must not anchor relative paths to the gateway owner's cwd."""
    from tools import file_tools

    owner = tmp_path / "profiles" / "owner"
    routed = tmp_path / "profiles" / "routed"
    owner.mkdir(parents=True)
    routed.mkdir(parents=True)
    (routed / "config.yaml").write_text(
        f"terminal:\n  backend: local\n  cwd: {routed}\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(owner))
    secret_scope.set_multiplex_active(True)
    token = set_hermes_home_override(str(routed))
    try:
        assert file_tools._resolve_base_dir("agent:routed:test") == routed
    finally:
        reset_hermes_home_override(token)


def test_multiplex_cache_paths_use_routed_profile_backend(monkeypatch, tmp_path):
    """Media cache paths must not use the gateway owner's backend."""
    from tools.credential_files import to_agent_visible_cache_path

    routed = tmp_path / "profiles" / "routed"
    staged = routed / "cache" / "images" / "drop.png"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"png")
    (routed / "config.yaml").write_text(
        "terminal:\n  backend: local\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("TERMINAL_ENV", "docker")
    secret_scope.set_multiplex_active(True)
    token = set_hermes_home_override(str(routed))
    try:
        assert to_agent_visible_cache_path(str(staged)) == str(staged)
    finally:
        reset_hermes_home_override(token)


def test_multiplex_auxiliary_tools_use_routed_terminal_settings(monkeypatch, tmp_path):
    """Execution, media, browser, and delegation helpers use routed settings."""
    from types import SimpleNamespace

    from tools import (
        browser_tool,
        code_execution_tool,
        delegate_tool,
        image_generation_tool,
        vision_tools,
    )

    owner = tmp_path / "profiles" / "owner"
    routed = tmp_path / "profiles" / "routed"
    owner.mkdir(parents=True)
    routed.mkdir(parents=True)
    (routed / "config.yaml").write_text(
        f"terminal:\n  backend: docker\n  cwd: {routed}\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(owner))
    monkeypatch.setattr(browser_tool, "_get_cdp_override_raw", lambda: "")
    monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: None)
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
    secret_scope.set_multiplex_active(True)
    token = set_hermes_home_override(str(routed))
    try:
        assert (
            code_execution_tool._resolve_child_cwd(
                "project", str(tmp_path / "staging"), "agent:routed:test"
            )
            == str(routed)
        )
        assert image_generation_tool._agent_cache_base_for_env(None) == "/root/.hermes"
        assert vision_tools._terminal_backend_is_local() is False
        assert browser_tool._is_local_backend() is False
        assert delegate_tool._resolve_workspace_hint(SimpleNamespace()) == str(routed)
    finally:
        reset_hermes_home_override(token)


def test_multiplex_child_process_envs_never_inherit_owner_terminal_settings(
    monkeypatch, tmp_path
):
    """Terminal and execute_code children must not receive gateway-owner state."""
    from tools.code_execution_tool import _scrub_child_env
    from tools.environments.local import _make_run_env, _sanitize_subprocess_env

    owner = tmp_path / "profiles" / "owner"
    routed = tmp_path / "profiles" / "routed"
    owner.mkdir(parents=True)
    routed.mkdir(parents=True)
    (routed / "config.yaml").write_text(
        f"terminal:\n  backend: local\n  cwd: {routed}\n  home_mode: ''\n",
        encoding="utf-8",
    )

    owner_terminal = {
        "TERMINAL_ENV": "ssh",
        "TERMINAL_CWD": str(owner),
        "TERMINAL_SSH_HOST": "owner.example",
        "TERMINAL_SSH_KEY": "/owner/id_ed25519",
        "TERMINAL_DOCKER_VOLUMES": '["/owner:/workspace"]',
        "TERMINAL_HOME_MODE": "profile",
    }
    for key, value in owner_terminal.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("TERM_PROGRAM", "HermesTest")

    secret_scope.set_multiplex_active(True)
    token = set_hermes_home_override(str(routed))
    try:
        for child_env in (_make_run_env({}), _sanitize_subprocess_env(dict(os.environ))):
            assert child_env["TERMINAL_ENV"] == "local"
            assert child_env["TERMINAL_CWD"] == str(routed)
            assert "TERMINAL_SSH_HOST" not in child_env
            assert "TERMINAL_SSH_KEY" not in child_env
            assert child_env["TERMINAL_DOCKER_VOLUMES"] == "[]"
            assert child_env["TERMINAL_HOME_MODE"] == "auto"

        execute_code_env = _scrub_child_env(dict(os.environ))
        assert execute_code_env["TERM"] == "xterm-256color"
        assert execute_code_env["TERM_PROGRAM"] == "HermesTest"
        assert not any(key.startswith("TERMINAL_") for key in execute_code_env)
    finally:
        reset_hermes_home_override(token)
