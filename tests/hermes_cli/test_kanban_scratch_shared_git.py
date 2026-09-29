"""Scratch cleanup must not delete a Git object store another workspace uses.

Incident: a worker cloned a repo into card A's scratch workspace, then added a
linked worktree for card B in B's own scratch workspace. Completing A rmtree'd
``A/repo/.git`` -- the only object store B's worktree pointed into -- and B's
unpushed commits vanished. A scratch workspace that holds the common dir of a
live linked worktree OUTSIDE it is preserved (and retried later); once no
outside worktree depends on it, normal cleanup applies.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_ops


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout.strip()


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _scratch_task(conn, title: str) -> tuple[str, Path]:
    tid = kb.create_task(conn, title=title)
    ws = kbw.resolve_workspace(kb.get_task(conn, tid))
    kbw.set_workspace_path(conn, tid, ws)
    return tid, ws


def _repo(at: Path) -> Path:
    """A local-only repo (no remote) with one commit."""
    _git("init", "-q", str(at))
    _git("-C", str(at), "config", "user.email", "t@example.com")
    _git("-C", str(at), "config", "user.name", "t")
    (at / "a.txt").write_text("a\n", encoding="utf-8")
    _git("-C", str(at), "add", "a.txt")
    _git("-C", str(at), "commit", "-qm", "base")
    return at


def _unpushed_commit(wt: Path) -> str:
    (wt / "b.txt").write_text("unpushed work\n", encoding="utf-8")
    _git("-C", str(wt), "add", "b.txt")
    _git("-C", str(wt), "commit", "-qm", "unpushed")
    return _git("-C", str(wt), "rev-parse", "HEAD")


def _cross_card(conn) -> tuple[str, Path, str, Path, str]:
    """Card A's scratch holds the repo; card B's scratch holds a linked worktree."""
    a, a_ws = _scratch_task(conn, "A owns the clone")
    b, b_ws = _scratch_task(conn, "B works in a linked worktree")
    repo = _repo(a_ws / "repo")
    wt = b_ws / "repo"
    _git("-C", str(repo), "worktree", "add", "-q", "-b", f"wt/{b}", str(wt))
    sha = _unpushed_commit(wt)
    return a, a_ws, b, b_ws, sha


def test_completion_preserves_store_used_by_open_cards_worktree(kanban_home):
    with kbc.connect() as conn:
        a, a_ws, b, b_ws, sha = _cross_card(conn)
        assert kb.complete_task(conn, a, result="A done")
        assert kb.get_task(conn, b).status != "done"

    assert (a_ws / "repo" / ".git").is_dir(), "shared object store was deleted"
    wt = b_ws / "repo"
    assert _git("-C", str(wt), "rev-parse", "HEAD") == sha
    assert _git("-C", str(wt), "cat-file", "-t", sha) == "commit"
    assert _git("-C", str(a_ws / "repo"), "branch", "--list", f"wt/{b}")


def test_store_becomes_eligible_after_dependent_worktree_is_gone(kanban_home):
    """B finishes first (its worktree dir is removed); A then cleans normally."""
    with kbc.connect() as conn:
        a, a_ws, b, b_ws, _ = _cross_card(conn)
        assert kb.complete_task(conn, b, result="B done")
        assert not b_ws.exists(), "a linked worktree alone is plain scratch"
        assert kb.complete_task(conn, a, result="A done")
    assert not a_ws.exists(), "stale worktree registration must not pin the store"


def test_worktree_inside_same_workspace_does_not_pin_it(kanban_home):
    with kbc.connect() as conn:
        a, a_ws = _scratch_task(conn, "self-contained")
        repo = _repo(a_ws / "repo")
        _git("-C", str(repo), "worktree", "add", "-q", "-b", "side", str(a_ws / "side"))
        assert kb.complete_task(conn, a, result="done")
    assert not a_ws.exists()


def test_locked_missing_worktree_still_pins_store(kanban_home, tmp_path):
    """``git worktree lock`` marks a worktree on removable/offline storage as
    in use even when its directory is absent; prune honours that, so do we."""
    with kbc.connect() as conn:
        a, a_ws = _scratch_task(conn, "A")
        repo = _repo(a_ws / "repo")
        offline = tmp_path / "offline-wt"
        _git("-C", str(repo), "worktree", "add", "-q", "-b", "off", str(offline))
        _git("-C", str(repo), "worktree", "lock", str(offline))
        subprocess.run(["rm", "-rf", str(offline)], check=True)
        assert kb.complete_task(conn, a, result="done")
    assert (a_ws / "repo" / ".git").is_dir()


@pytest.mark.require_symlinks
def test_symlink_to_external_repo_is_not_followed(kanban_home, tmp_path):
    """Containment: a symlink inside the scratch dir to a repo elsewhere neither
    pins the scratch dir nor lets cleanup reach the external repo."""
    external = _repo(tmp_path / "external")
    _git("-C", str(external), "worktree", "add", "-q", "-b", "x", str(tmp_path / "ext-wt"))
    with kbc.connect() as conn:
        a, a_ws = _scratch_task(conn, "A")
        (a_ws / "link").symlink_to(external, target_is_directory=True)
        assert kb.complete_task(conn, a, result="done")
    assert not a_ws.exists()
    assert (external / ".git" / "worktrees").is_dir()
    assert _git("-C", str(tmp_path / "ext-wt"), "rev-parse", "HEAD")


def test_deferred_parent_cleanup_preserves_shared_store(kanban_home):
    with kbc.connect() as conn:
        a, a_ws, b, b_ws, sha = _cross_card(conn)
        child = kb.create_task(conn, title="child", workspace_kind="dir",
                               workspace_path=str(kanban_home / "child"))
        kb.link_tasks(conn, a, child)
        assert kb.complete_task(conn, a, result="A done")
        assert a_ws.exists(), "deferred while child active"
        assert kb.complete_task(conn, child, result="child done")
    assert (a_ws / "repo" / ".git").is_dir()
    assert _git("-C", str(b_ws / "repo"), "cat-file", "-t", sha) == "commit"


def _admin_entry(a_ws: Path) -> Path:
    (entry,) = (a_ws / "repo" / ".git" / "worktrees").iterdir()
    return entry


@pytest.fixture
def restore_modes():
    """chmod targets back to readable so tmp_path teardown can delete them."""
    touched: list[Path] = []
    yield touched
    for p in touched:
        with contextlib.suppress(OSError):
            p.chmod(0o755 if p.is_dir() else 0o644)


def _deny(p: Path, touched: list[Path]) -> None:
    touched.append(p)
    p.chmod(0)
    try:
        with open(p, "rb") if p.is_file() else os.scandir(p):
            pass
    except PermissionError:
        return
    pytest.skip("permission bits not enforced (running as root?)")


# Reviewer counterexample (Gurney, round 1): incomplete inspection of a
# worktree backlink must preserve the store, never read as "no dependency".
@pytest.mark.parametrize("deny", ["gitdir-file", "admin-dir", "worktrees-dir"])
def test_unreadable_worktree_metadata_preserves_store(kanban_home, restore_modes, deny):
    with kbc.connect() as conn:
        a, a_ws, b, b_ws, sha = _cross_card(conn)
        admin = _admin_entry(a_ws)
        target = {
            "gitdir-file": admin / "gitdir",
            "admin-dir": admin,
            "worktrees-dir": admin.parent,
        }[deny]
        _deny(target, restore_modes)
        assert kb.complete_task(conn, a, result="A done")
        assert kb.get_task(conn, b).status != "done"
    target.chmod(0o755 if target.is_dir() else 0o644)
    assert (a_ws / "repo" / ".git").is_dir(), "shared object store was deleted"
    assert _git("-C", str(b_ws / "repo"), "cat-file", "-t", sha) == "commit"


def test_undecodable_gitdir_preserves_store(kanban_home):
    with kbc.connect() as conn:
        a, a_ws, b, b_ws, sha = _cross_card(conn)
        (_admin_entry(a_ws) / "gitdir").write_bytes(b"\xff\xfe\x00bad\n")
        assert kb.complete_task(conn, a, result="A done")
    assert (a_ws / "repo" / ".git").is_dir()
    assert _git("-C", str(b_ws / "repo"), "cat-file", "-t", sha) == "commit"


def test_unreadable_gitdir_preserves_store_in_gc(kanban_home, restore_modes, capsys):
    args = argparse.Namespace(event_retention_days=30, log_retention_days=30)
    with kbc.connect() as conn:
        a, a_ws, b, b_ws, sha = _cross_card(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (a,))
        gitdir = _admin_entry(a_ws) / "gitdir"
        _deny(gitdir, restore_modes)
    assert kanban_ops._cmd_gc(args) == 0
    assert "GC complete: 0 workspace(s)" in capsys.readouterr().out
    gitdir.chmod(0o644)
    assert _git("-C", str(b_ws / "repo"), "cat-file", "-t", sha) == "commit"


def test_admin_entry_without_gitdir_file_is_stale(kanban_home):
    """Control: git prunes an unlocked entry whose gitdir file is absent, so a
    genuinely missing backlink (not an unreadable one) does not pin the store."""
    with kbc.connect() as conn:
        a, a_ws, b, b_ws, _ = _cross_card(conn)
        (_admin_entry(a_ws) / "gitdir").unlink()
        assert kb.complete_task(conn, a, result="A done")
    assert not a_ws.exists()


def test_gc_preserves_archived_store_until_dependent_worktree_gone(kanban_home, capsys):
    args = argparse.Namespace(event_retention_days=30, log_retention_days=30)
    with kbc.connect() as conn:
        a, a_ws, b, b_ws, sha = _cross_card(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (a,))
    assert kanban_ops._cmd_gc(args) == 0
    assert "GC complete: 0 workspace(s)" in capsys.readouterr().out
    assert (a_ws / "repo" / ".git").is_dir()
    assert _git("-C", str(b_ws / "repo"), "cat-file", "-t", sha) == "commit"

    subprocess.run(["rm", "-rf", str(b_ws / "repo")], check=True)
    assert kanban_ops._cmd_gc(args) == 0
    assert "GC complete: 1 workspace(s)" in capsys.readouterr().out
    assert not a_ws.exists()
