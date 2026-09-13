"""Real cron -> child CLI bootstrap, stopping before any agent/LLM turn.

The test launcher imports the actual CLI (which applies -p before other
imports), then records the selected home. No profile resolver, live-owner
lookup, env scrubber or subprocess is mocked. This is not live delivery proof.
"""

import json
import os
from pathlib import Path
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
@pytest.mark.macos_only
def test_cron_child_resolves_in_same_estate(tmp_path, monkeypatch, layout, target, served):
    from agent.secret_scope import set_multiplex_active

    # Isolate platform-native fallback too: a dropped custom root must fail,
    # never accidentally find a real user's similarly named profile.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    root = tmp_path / "custom" if layout == "custom" else _get_platform_default_hermes_home()
    source = root / "profiles" / "gurney"
    sibling = root / "profiles" / "generalist"
    for home in (root, source, sibling):
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    # Explicit named/default selection must beat the sticky source identity.
    (root / "active_profile").write_text("gurney", encoding="utf-8")
    launch_home = root if served else source
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "test-parent-identity")
    if served:
        (root / ".env").write_text("HERMES_LANGUAGE=launch-only\n", encoding="utf-8")
        monkeypatch.setenv("HERMES_LANGUAGE", "launch-only")
        monkeypatch.setenv("TERMINAL_ENV", "docker")

    receipt = tmp_path / "bootstrap.json"
    launcher_dir = tmp_path / "bin"
    launcher_dir.mkdir()
    launcher = launcher_dir / "hermes"
    repo = Path(__file__).resolve().parents[2]
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(repo)!r})\n"
        "import hermes_cli.main\n"
        "from hermes_constants import get_hermes_home\n"
        "query = Path(sys.argv[sys.argv.index('--query-file') + 1])\n"
        f"Path({str(receipt)!r}).write_text(json.dumps({{\n"
        "'home': str(get_hermes_home().resolve()), 'argv': sys.argv[1:],\n"
        "'message': query.read_text(), 'query_file': str(query),\n"
        "'task': os.environ.get('HERMES_KANBAN_TASK'),\n"
        "'child': os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'),\n"
        "'language': os.environ.get('HERMES_LANGUAGE'),\n"
        "'terminal': os.environ.get('TERMINAL_ENV')}))\n",
        encoding="utf-8",
    )
    launcher.chmod(0o700)
    monkeypatch.setenv("PATH", str(launcher_dir) + os.pathsep + os.environ["PATH"])
    token = set_hermes_home_override(source if served else None)
    set_multiplex_active(served)
    try:
        error = _deliver_to_bot_chat({"id": "root-regression", "name": "proof"}, "payload", target)
    finally:
        reset_hermes_home_override(token)
        set_multiplex_active(False)
    if target == "ghost":
        assert error and "does not exist" in error
        assert not receipt.exists()
        assert not (root / "profiles" / "ghost").exists()
    else:
        assert error is None, error
        result = json.loads(receipt.read_text())
        expected = {"generalist": sibling, "default": root}.get(target, source)
        assert result["home"] == str(expected.resolve())
        assert result["argv"][:6] == ["chat", "--in", "~", "-c", "Bot Chat", "--create-if-missing"]
        assert "payload" in result["message"] and "not the user" in result["message"]
        assert not Path(result["query_file"]).exists()
        assert result["task"] is None and result["child"] == "1"
        if served:
            assert result["language"] != "launch-only"
            assert result["terminal"] == "local"
    assert os.environ["HERMES_HOME"] == str(launch_home)
    assert os.environ["HERMES_KANBAN_TASK"] == "test-parent-identity"
    assert not (source / "profiles").exists()
