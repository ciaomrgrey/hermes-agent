"""Hermes children launched with ``python -m hermes_cli.main`` must import the INSTALLED tree.

``-m`` puts the child's cwd first on ``sys.path``. The Kanban dispatcher spawns workers with
``cwd=<task workspace>``; when that workspace is a hermes-agent worktree the worker imported the
candidate tree instead of the install (Gurney review runs 2046/2047 on t_cb37fb5f ran unreleased
v0.22.5 home maintenance against a live profile). Every module-form spawner shares
``hermes_constants.hermes_cli_module_argv``, which pins ``-P``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest



def _installed_pkg(env: dict, neutral_cwd: Path) -> Path:
    """Where this interpreter's install resolves ``hermes_cli`` with no cwd on the path.

    (The pytest process itself may import the tree under test via rootdir insertion, so its own
    ``hermes_cli.__file__`` is not the reference.)"""
    r = subprocess.run([sys.executable, "-I", "-c", "import hermes_cli; print(hermes_cli.__file__)"],
                       cwd=neutral_cwd, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return Path(r.stdout.strip()).resolve().parent


def _decoy_workspace(root: Path, marker: Path) -> Path:
    """A directory that looks like a hermes-agent checkout whose ``hermes_cli`` records its import."""
    ws = root / "decoy-worktree"
    pkg = ws / "hermes_cli"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        "import pathlib, sys\n"
        f"pathlib.Path({str(marker)!r}).write_text(__file__)\n"
        "sys.exit(97)\n",
        encoding="utf-8",
    )
    (pkg / "main.py").write_text("", encoding="utf-8")
    return ws


def _task(kb):
    return kb.Task(
        id="t_shadow", title="cwd shadow", body=None, assignee="w", status="running", priority=0,
        created_by="test", created_at=1, started_at=None, completed_at=None, workspace_kind="dir",
        workspace_path=None, claim_lock="lock", claim_expires=None, tenant=None, current_run_id=1,
    )


def test_shared_module_argv_pins_safe_path():
    from hermes_constants import hermes_cli_module_argv

    assert hermes_cli_module_argv() == [sys.executable, "-P", "-m", "hermes_cli.main"]
    assert hermes_cli_module_argv("/venv/bin/python")[:2] == ["/venv/bin/python", "-P"]


def test_decoy_shadows_the_unpinned_form(tmp_path):
    """Control: the bare ``-m`` form really imports the cwd tree (the test can discriminate)."""
    marker = tmp_path / "imported"
    ws = _decoy_workspace(tmp_path, marker)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONSAFEPATH", "PYTHONPATH")}
    r = subprocess.run([sys.executable, "-m", "hermes_cli.main", "--version"], cwd=ws, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 97
    assert marker.exists()


def test_kanban_worker_spawned_in_hermes_worktree_imports_installed_package(monkeypatch, tmp_path):
    """Real dispatcher spawn path (argv + env + cwd from ``_default_spawn``), real subprocess."""
    root = tmp_path / ".hermes"
    (root / "profiles" / "w").mkdir(parents=True)
    (root / "profiles" / "w" / "config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    (root / "config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("HERMES_BIN", raising=False)
    monkeypatch.delenv("PYTHONSAFEPATH", raising=False)

    marker = tmp_path / "imported"
    ws = _decoy_workspace(tmp_path, marker)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    captured: dict = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured.update(cmd=list(cmd), env=dict(kwargs["env"]), cwd=kwargs.get("cwd"))
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    kbd._default_spawn(_task(kb), str(ws))
    monkeypatch.undo()  # restore the real Popen before launching the child

    cmd = captured["cmd"]
    assert captured["cwd"] == str(ws)  # tool semantics keep the workspace cwd
    launcher = cmd[: cmd.index("-p")]
    assert launcher == [sys.executable, "-P", "-m", "hermes_cli.main"]

    probe = ("import hermes_cli; print(hermes_cli.__file__)")
    r = subprocess.run([*launcher[:2], "-c", probe], cwd=captured["cwd"], env=captured["env"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    imported = Path(r.stdout.strip()).resolve().parent
    assert imported == _installed_pkg(captured["env"], tmp_path)
    assert ws.resolve() not in imported.parents

    r = subprocess.run([*launcher, "--version"], cwd=captured["cwd"], env=captured["env"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, (r.returncode, r.stdout[-500:], r.stderr[-500:])
    assert not marker.exists(), f"worker imported the workspace tree: {marker.read_text()}"


def test_child_tools_keep_cwd_import_semantics(tmp_path):
    """``-P`` is a flag, not ``PYTHONSAFEPATH``: the worker's own tool subprocesses are unaffected."""
    from hermes_constants import hermes_cli_module_argv

    assert "PYTHONSAFEPATH" not in " ".join(hermes_cli_module_argv())


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: __import__("gateway.run", fromlist=["x"])._resolve_hermes_bin(), id="gateway._resolve_hermes_bin"),
        pytest.param(lambda: __import__("hermes_cli.relaunch", fromlist=["x"]).build_relaunch_argv([], original_argv=[]), id="relaunch"),
        pytest.param(lambda: __import__("hermes_cli.gateway", fromlist=["x"])._gateway_run_args_for_profile("p"), id="gateway_run_args"),
    ],
)
def test_sibling_module_spawners_pin_safe_path(monkeypatch, build):
    import hermes_cli.relaunch as relaunch

    monkeypatch.setattr(relaunch, "resolve_hermes_bin", lambda: None)
    argv = build()
    i = argv.index("-m")
    assert argv[i + 1] == "hermes_cli.main"
    assert argv[i - 1] == "-P"
