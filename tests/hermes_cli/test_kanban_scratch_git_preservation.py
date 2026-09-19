"""Committed scratch work must survive actual task completion and reclamation."""
from pathlib import Path
import subprocess

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli.kanban_db_connect import connect


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
    conn = connect()
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
        git(recovered, "fetch", bundle.stored_path, "+refs/*:refs/recovered/*")
    git(recovered, "cat-file", "-e", sha + "^{commit}")
    assert git(recovered, "show", sha + ":work.txt").stdout == text


def test_isolated_clone_commit_recoverable_after_complete(board, tmp_path):
    tid, wp = task(board)
    origin, repo, sha = clone_with_work(tmp_path, wp)
    complete(board, tid)
    assert not wp.exists()
    assert git(origin, "cat-file", "-e", sha + "^{commit}", check=False).returncode != 0
    recover(board, tid, tmp_path, sha, "irreplaceable work")


def test_deferred_parent_recovery(board, tmp_path):
    tid, wp = task(board)
    _, _, sha = clone_with_work(tmp_path, wp)
    child = kb.create_task(board, title="consumer", assignee="worker", parents=[tid])
    complete(board, tid)
    assert wp.exists()
    complete(board, child)
    assert not wp.exists()
    recover(board, tid, tmp_path, sha, "irreplaceable work")


def test_linked_detached_and_no_git_are_silent(board, tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init")
    commit(origin, "base")
    tid, wp = task(board)
    repo = wp / "linked"
    git(origin, "worktree", "add", "--detach", str(repo))
    sha = commit(repo, "detached work")
    refs = git(origin, "show-ref").stdout
    complete(board, tid)
    assert not wp.exists()
    git(origin, "cat-file", "-e", sha + "^{commit}")
    assert git(origin, "show-ref").stdout == refs
    assert not kb.list_attachments(board, tid)
    assert not any(e.kind.startswith("workspace_") for e in kb.list_events(board, tid))
    empty, empty_wp = task(board)
    (empty_wp / "uncommitted.txt").write_text("out of scope")
    complete(board, empty)
    assert not empty_wp.exists()
    assert not kb.list_attachments(board, empty)
    assert not any(e.kind.startswith("workspace_") for e in kb.list_events(board, empty))


def test_bundle_failure_refuses_reclamation(board, tmp_path):
    tid, wp = task(board)
    _, repo, sha = clone_with_work(tmp_path, wp)
    # A real filesystem failure, independent of OS/root permission semantics.
    (tmp_path / "board" / "attachments").write_text("not a directory")
    complete(board, tid)
    assert wp.exists()
    git(repo, "cat-file", "-e", sha + "^{commit}")
    assert any(e.kind == "workspace_cleanup_refused" for e in kb.list_events(board, tid))


def test_reflog_tag_branch_and_nested_repo_recovery(board, tmp_path):
    tid, wp = task(board)
    _, repo, sha = clone_with_work(tmp_path, wp)
    git(repo, "reset", "--hard", "HEAD~1")  # sha is now reflog-only
    branch = commit(repo, "branch-only work")
    git(repo, "branch", "other")
    git(repo, "reset", "--hard", "HEAD~1")
    tagged = commit(repo, "tag-only work")
    git(repo, "tag", "kept")
    git(repo, "reset", "--hard", "HEAD~1")
    (repo / "dirty.txt").write_text("not committed")
    nested = repo / "nested"
    nested.mkdir()
    git(nested, "init")
    nested_sha = commit(nested, "nested work")
    complete(board, tid)
    assert not wp.exists()
    for oid, text in [(sha, "irreplaceable work"), (branch, "branch-only work"),
                      (tagged, "tag-only work"), (nested_sha, "nested work")]:
        recover(board, tid, tmp_path, oid, text)
    assert len(kb.list_attachments(board, tid)) == 2


@pytest.mark.parametrize("shape", ["bare", "inside-worktree", "detached-no-reflog"])
def test_other_self_contained_shapes(board, tmp_path, shape):
    tid, wp = task(board)
    _, repo, _ = clone_with_work(tmp_path, wp)
    if shape == "bare":
        bare = wp / "bare.git"
        git(wp, "clone", "--bare", str(repo), str(bare))
        repo = bare
    elif shape == "inside-worktree":
        linked = wp / "linked"
        git(repo, "worktree", "add", "--detach", str(linked))
        repo = linked
    else:
        git(repo, "checkout", "--detach")
    if shape == "bare":
        sha = git(repo, "rev-parse", "HEAD").stdout.strip()
        text = "irreplaceable work"
    else:
        git(repo, "config", "core.logAllRefUpdates", "false")
        sha = commit(repo, "detached-only")
        text = "detached-only"
    complete(board, tid)
    assert not wp.exists()
    recover(board, tid, tmp_path, sha, text)


@pytest.mark.parametrize("failure", ["create", "verify"])
def test_git_bundle_error_keeps_done_workspace(board, tmp_path, monkeypatch, failure):
    tid, wp = task(board)
    _, repo, sha = clone_with_work(tmp_path, wp)
    original = kbw._git

    def fail_bundle(path, *args, **kwargs):
        if args[:2] == ("bundle", failure):
            return subprocess.CompletedProcess(args, 1, "", "injected git failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(kbw, "_git", fail_bundle)
    complete(board, tid)
    assert wp.exists()
    assert kb.get_task(board, tid).status == "done"
    git(repo, "cat-file", "-e", sha + "^{commit}")
    assert any(e.kind == "workspace_cleanup_refused" for e in kb.list_events(board, tid))
    assert not git(repo, "for-each-ref", "refs/hermes-preservation").stdout
    monkeypatch.setattr(kbw, "_git", original)
    kbw._cleanup_workspace(board, tid)
    assert not wp.exists()
    recover(board, tid, tmp_path, sha, "irreplaceable work")
