"""Committed scratch work must survive actual task completion and reclamation."""
from pathlib import Path
import subprocess

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_workspace as kbw


def git(repo, *args, check=True):
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60,
    )
    if check:
        assert result.returncode == 0, result.stderr
    return result


def commit(repo, text):
    (repo / "work.txt").write_text(text)
    git(repo, "add", "work.txt")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com",
        "commit", "-m", text)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board" / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setenv("HERMES_KANBAN_ATTACHMENTS_ROOT", str(tmp_path / "board" / "attachments"))
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb.init_db()
    conn = kb.connect()
    yield conn
    conn.close()


def task(conn):
    tid = kb.create_task(conn, title="scratch commit", assignee="worker")
    wp = kbw.resolve_workspace(kb.get_task(conn, tid))
    kbw.set_workspace_path(conn, tid, wp)
    return tid, wp


def complete(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker")
    assert kb.complete_task(conn, tid, summary="finished")


def clone_with_work(tmp_path, wp):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init")
    commit(origin, "base")
    repo = wp / "repo"
    git(wp, "clone", str(origin), str(repo))
    git(repo, "checkout", "-b", "worker/real-branch")
    sha = commit(repo, "irreplaceable work")
    assert git(origin, "cat-file", "-e", sha + "^{commit}", check=False).returncode != 0
    return origin, repo, sha


def recover(conn, tid, tmp_path, sha, text):
    bundles = kb.list_attachments(conn, tid)
    assert bundles, "completion destroyed the only object store without a recovery bundle"
    recovered = tmp_path / "recovered"
    recovered.mkdir(exist_ok=True)
    git(recovered, "init")
    for bundle in bundles:
        git(recovered, "fetch", bundle.stored_path, "+refs/*:refs/*")
    git(recovered, "cat-file", "-e", sha + "^{commit}")
    assert git(recovered, "show", sha + ":work.txt").stdout == text


def test_isolated_clone_commit_recoverable_after_complete(board, tmp_path):
    tid, wp = task(board)
    origin, repo, sha = clone_with_work(tmp_path, wp)
    complete(board, tid)
    assert not wp.exists()
    assert git(origin, "cat-file", "-e", sha + "^{commit}", check=False).returncode != 0
    recover(board, tid, tmp_path, sha, "irreplaceable work")
