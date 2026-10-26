"""TUI-gateway children run in the gateway's inherited cwd and must not import a checkout found there.

``cli.exec`` and the slash worker spawn ``python -m <hermes module>`` with ``cwd=os.getcwd()``. When the
TUI/backend was started inside a hermes-agent checkout or worktree, bare ``-m`` resolved the module (and
everything it imports) from that tree instead of the running install. Both spawners pin ``-P`` via
``hermes_constants``. The handlers run for real here; each child runs as a real subprocess in a decoy cwd.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def _decoy(root: Path, package: str, marker: Path) -> Path:
    """A cwd shaped like a hermes-agent checkout whose ``package`` records any import and exits 97."""
    ws = root / "decoy-checkout"
    pkg = ws / package
    pkg.mkdir(parents=True)
    hit = f"import pathlib, sys\npathlib.Path({str(marker)!r}).write_text(__file__)\nsys.exit(97)\n"
    (pkg / "__init__.py").write_text(hit, encoding="utf-8")
    for leaf in ("main.py", "slash_worker.py", "entry.py"):
        (pkg / leaf).write_text(hit, encoding="utf-8")
    return ws


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("PYTHONSAFEPATH", raising=False)
    return home


def test_cli_exec_in_checkout_cwd_runs_installed_hermes(isolated_home, tmp_path, monkeypatch):
    """The real ``cli.exec`` handler, real subprocess, cwd = decoy checkout."""
    import tui_gateway.server as server

    marker = tmp_path / "imported"
    ws = _decoy(tmp_path, "hermes_cli", marker)
    monkeypatch.chdir(ws)
    resp = server._methods["cli.exec"]("r1", {"argv": ["--version"], "timeout": 120})
    monkeypatch.chdir(tmp_path)

    result = resp.get("result") or {}
    assert not marker.exists(), f"cli.exec imported the cwd checkout: {marker.read_text()}"
    assert result.get("code") == 0, resp
    assert "Hermes" in result.get("output", ""), resp


def test_slash_worker_in_checkout_cwd_resolves_installed_module(isolated_home, tmp_path, monkeypatch):
    """The real ``_SlashWorker`` argv/env/cwd; the child resolves ``tui_gateway.slash_worker`` as launched."""
    import tui_gateway.server as server

    marker = tmp_path / "imported"
    ws = _decoy(tmp_path, "tui_gateway", marker)
    captured: dict = {}

    class _Popen:
        def __init__(self, argv, **kw):
            captured.update(argv=list(argv), env=dict(kw["env"]), cwd=kw.get("cwd"))
            self.stdout = self.stderr = iter(())
            self.stdin = None

        def poll(self):
            return 0

    monkeypatch.chdir(ws)
    with monkeypatch.context() as m:
        m.setattr(server.subprocess, "Popen", _Popen)
        server._SlashWorker("sess", "")
    monkeypatch.chdir(tmp_path)

    argv = captured["argv"]
    assert captured["cwd"] == str(ws)  # tool semantics keep the inherited cwd
    i = argv.index("-m")
    assert argv[i + 1] == "tui_gateway.slash_worker"
    flags = argv[:i]  # interpreter + its flags exactly as launched
    probe = "import importlib.util as u; print(u.find_spec('tui_gateway.slash_worker').origin)"
    r = subprocess.run([*flags, "-c", probe], cwd=captured["cwd"], env=captured["env"],
                       capture_output=True, text=True, timeout=60)
    assert not marker.exists(), f"slash worker imported the cwd checkout: {marker.read_text()}"
    assert r.returncode == 0, r.stderr
    origin = Path(r.stdout.strip()).resolve()
    assert ws.resolve() not in origin.parents
    assert origin == Path(server.__file__).resolve().parent / "slash_worker.py"


def test_bare_module_form_is_shadowed_control(tmp_path):
    """Control: without ``-P`` the same decoy really wins, so the tests above discriminate."""
    marker = tmp_path / "imported"
    ws = _decoy(tmp_path, "tui_gateway", marker)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONSAFEPATH", "PYTHONPATH")}
    r = subprocess.run([sys.executable, "-m", "tui_gateway.slash_worker"], cwd=ws, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 97 and marker.exists()
