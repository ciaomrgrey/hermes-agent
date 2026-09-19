"""Behavioral regressions for the terminal config → env bridge.

``terminal_tool._get_env_config()`` reads TERMINAL_* variables.  The bridge
must let explicitly configured terminal keys override stale launcher/.env
values while preserving environment values for terminal keys omitted from
config.yaml.
"""

import os

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
    ripley = tmp_path / "profiles" / "ripley"
    gurney = tmp_path / "profiles" / "gurney"
    ripley.mkdir(parents=True)
    gurney.mkdir(parents=True)
    (ripley / "config.yaml").write_text(
        f"terminal:\n  backend: local\n  cwd: {ripley}\n",
        encoding="utf-8",
    )
    (gurney / "config.yaml").write_text(
        "terminal:\n  backend: ssh\n  cwd: '~'\n  ssh_host: gurney.example\n",
        encoding="utf-8",
    )

    # Reproduce the live gateway state: startup bridged Ripley's config into
    # process-global env before the Gurney-routed turn entered its profile scope.
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(ripley))
    monkeypatch.setattr(terminal_tool, "_terminal_config_bridge_attempted", True)
    secret_scope.set_multiplex_active(True)

    token = set_hermes_home_override(str(ripley))
    try:
        ripley_config = terminal_tool._get_env_config()
    finally:
        reset_hermes_home_override(token)

    token = set_hermes_home_override(str(gurney))
    try:
        gurney_config = terminal_tool._get_env_config()
    finally:
        reset_hermes_home_override(token)

    assert ripley_config["cwd"] == str(ripley)
    assert gurney_config["env_type"] == "ssh"
    assert gurney_config["cwd"] == "~"
    assert gurney_config["ssh_host"] == "gurney.example"
    assert os.environ["TERMINAL_CWD"] == str(ripley)


def test_multiplex_omitted_keys_do_not_inherit_gateway_owner(monkeypatch, tmp_path):
    """A partial routed config gets profile defaults, never owner terminal state."""
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


def test_multiplex_bridge_failure_never_falls_back_to_owner_env(monkeypatch):
    """Config-read failures fail isolated instead of exposing owner settings."""
    import hermes_cli.config as config_mod

    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_CWD", "/owner/workspace")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "owner.example")
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


def test_multiplex_file_environments_keep_profile_cwds_isolated(monkeypatch, tmp_path):
    """Distinct routed sessions create distinct local envs at their own cwd."""
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools import file_tools

    ripley = tmp_path / "profiles" / "ripley"
    gurney = tmp_path / "profiles" / "gurney"
    ripley.mkdir(parents=True)
    gurney.mkdir(parents=True)
    for home in (ripley, gurney):
        (home / "config.yaml").write_text(
            f"terminal:\n  backend: local\n  cwd: {home}\n",
            encoding="utf-8",
        )

    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(ripley))
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
        ripley_env = create("ripley", ripley, "agent:ripley:slack:channel:a")
        gurney_env = create("gurney", gurney, "agent:gurney:slack:channel:b")

        assert ripley_env is not gurney_env
        assert ripley_env.cwd == str(ripley)
        assert gurney_env.cwd == str(gurney)
        assert set(terminal_tool._active_environments) == {
            "session:agent:ripley:slack:channel:a",
            "session:agent:gurney:slack:channel:b",
        }
    finally:
        file_tools.clear_file_ops_cache()
        with terminal_tool._env_lock:
            terminal_tool._active_environments.clear()
            terminal_tool._last_activity.clear()
