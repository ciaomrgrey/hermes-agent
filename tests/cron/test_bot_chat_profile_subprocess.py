"""Cron -> real CLI bootstrap contract, without provider setup or an agent turn.

Port of ac8db1a16fe's eight estate-root cases. The installed-module launcher
now takes precedence over PATH, so intercept only its executable boundary and
run a recording script instead of main(). Profile bootstrap, discovery, env
scrubbing and the child process remain real. This is not live delivery proof.
"""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from cron.scheduler_delivery import _deliver_to_bot_chat
from hermes_constants import (
    _get_platform_default_hermes_home,
    reset_hermes_home_override,
    set_hermes_home_override,
)


@pytest.mark.parametrize("layout,target,served", [
    ("custom", "generalist", False),
    ("native", "generalist", False),
    ("custom", "default", False),
    ("custom", "", False),
    ("custom", "gurney", False),
    ("custom", "ghost", False),
    ("custom", "generalist", True),
    ("custom", "", True),
])
def test_cron_child_resolves_in_same_estate(tmp_path, monkeypatch, layout, target, served):
    from agent.secret_scope import set_multiplex_active

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    root = tmp_path / "custom" if layout == "custom" else _get_platform_default_hermes_home()
    source = root / "profiles" / "gurney"
    sibling = root / "profiles" / "generalist"
    for home in (root, source, sibling):
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    (root / "active_profile").write_text("gurney", encoding="utf-8")
    launch_home = root if served else source
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "test-parent-identity")
    if served:
        (root / ".env").write_text("HERMES_LANGUAGE=launch-only\n", encoding="utf-8")
        monkeypatch.setenv("HERMES_LANGUAGE", "launch-only")
        monkeypatch.setenv("TERMINAL_ENV", "docker")

    receipt = tmp_path / "bootstrap.json"
    launcher = tmp_path / "bootstrap.py"
    repo = Path(__file__).resolve().parents[2]
    launcher.write_text(
        "import json, os, sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(repo)!r})\n"
        "import hermes_cli.main\n"
        "from hermes_constants import get_hermes_home\n"
        "query = Path(sys.argv[sys.argv.index('--query-file') + 1])\n"
        f"Path({str(receipt)!r}).write_text(json.dumps({{\n"
        "'home': str(get_hermes_home().resolve()), 'argv': sys.argv[1:],\n"
        "'module': hermes_cli.main.__file__,\n"
        "'message': query.read_text(), 'query_file': str(query),\n"
        "'task': os.environ.get('HERMES_KANBAN_TASK'),\n"
        "'child': os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'),\n"
        "'language': os.environ.get('HERMES_LANGUAGE'),\n"
        "'terminal': os.environ.get('TERMINAL_ENV')}))\n",
        encoding="utf-8",
    )
    real_run = subprocess.run

    def bootstrap_only(argv, **kwargs):
        assert argv[:3] == [sys.executable, "-m", "hermes_cli.main"]
        return real_run([argv[0], str(launcher), *argv[3:]], **kwargs)

    monkeypatch.setattr(subprocess, "run", bootstrap_only)
    token = set_hermes_home_override(source if served else None)
    set_multiplex_active(served)
    try:
        error = _deliver_to_bot_chat({"id": "root-regression", "name": "proof"}, "payload", target)
    finally:
        reset_hermes_home_override(token)
        set_multiplex_active(False)
    if target == "ghost":
        assert error and "no longer exists" in error
        assert not receipt.exists()
        assert not (root / "profiles" / "ghost").exists()
    else:
        assert error is None, error
        result = json.loads(receipt.read_text())
        expected = {"generalist": sibling, "default": root}.get(target, source)
        assert result["home"] == str(expected.resolve())
        assert Path(result["module"]).resolve() == repo / "hermes_cli" / "main.py"
        assert result["argv"][:6] == ["chat", "--in", "~", "-c", "Bot Chat", "--create-if-missing"]
        assert "payload" in result["message"] and "not the user" in result["message"]
        assert not Path(result["query_file"]).exists()
        assert result["task"] is None
        # Delegation now fences the estate by path, not a boolean marker.
        assert Path(result["child"]).resolve() == root.resolve()
        if served:
            assert result["language"] != "launch-only"
            assert result["terminal"] == "local"
    assert os.environ["HERMES_HOME"] == str(launch_home)
    assert os.environ["HERMES_KANBAN_TASK"] == "test-parent-identity"
    assert not (source / "profiles").exists()
