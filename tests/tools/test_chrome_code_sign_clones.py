"""Orphaned Chrome code-sign clone sweep: real directories, real lsof, real processes.

Each preserve case is a negative control for the delete path: an in-use clone (open
file, running executable), an unknown owner/contents, a too-new clone, a reference
that appears mid-reap (concurrent Chrome restart) and an unavailable reference check.
"""
import os
import shutil
import subprocess
import sys
import time

import pytest

from tools import chrome_code_sign_clones as ccs

pytestmark = pytest.mark.skipif(sys.platform != "darwin" or not os.path.exists("/usr/sbin/lsof"),
                                reason="macOS-only (Chrome code-sign clones, lsof +D)")

OLD = time.time() + 3600  # "now" an hour ahead => every fixture clone is old enough


@pytest.fixture
def root(tmp_path, monkeypatch):
    base = tmp_path.resolve()
    monkeypatch.setattr(ccs, "_ROOT_PREFIX", str(base) + "/")
    r = base / ccs.CLONE_SUBDIR
    r.mkdir(parents=True)
    return r


def make_clone(root, name, bundle="Google Chrome.app.bundle"):
    exe_dir = root / name / bundle / "Contents" / "MacOS"
    exe_dir.mkdir(parents=True)
    (exe_dir / "Google Chrome").write_bytes(b"\0" * 4096)
    return root / name


def sweep(root, **kw):
    kw.setdefault("now", OLD)
    return ccs.sweep_code_sign_clones(root=root, **kw)


def test_orphan_is_removed_and_dry_run_touches_nothing(root):
    c = make_clone(root, "code_sign_clone.AAAAAA")
    dry = sweep(root)
    assert dry.would_remove == ["code_sign_clone.AAAAAA"] and dry.removed == [] and c.exists()
    res = sweep(root, apply=True)
    assert res.removed == ["code_sign_clone.AAAAAA"] and not c.exists()
    # Reported recovery is the paired statvfs delta, never du.
    assert res.free_bytes_before is not None and res.free_bytes_after is not None
    assert res.host_free_delta_bytes == res.free_bytes_after - res.free_bytes_before
    assert not any(p.name.startswith(ccs._REAPING_PREFIX) for p in root.iterdir())


def test_clone_with_open_file_is_preserved(root):
    c = make_clone(root, "code_sign_clone.OPENED")
    target = c / "Google Chrome.app.bundle" / "Contents" / "MacOS" / "Google Chrome"
    holder = subprocess.Popen([sys.executable, "-c", "import sys,time; f=open(sys.argv[1],'rb'); "
                               "print('ok',flush=True); time.sleep(60)", str(target)],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "ok"
        res = sweep(root, apply=True)
        assert res.removed == [] and c.exists()
        assert "in use" in res.preserved["code_sign_clone.OPENED"]
    finally:
        holder.kill()
        holder.wait()


def test_clone_running_its_executable_is_preserved(root):
    """The live-browser case: a process whose executable lives inside the clone."""
    c = make_clone(root, "code_sign_clone.RUNNIN")
    exe = c / "Google Chrome.app.bundle" / "Contents" / "MacOS" / "Google Chrome"
    shutil.copyfile("/bin/sleep", exe)
    exe.chmod(0o755)
    # A copied platform binary is SIGKILLed for its Apple signature; ad-hoc re-sign it.
    subprocess.run(["/usr/bin/codesign", "-s", "-", "-f", str(exe)], check=True, capture_output=True)
    proc = subprocess.Popen([str(exe), "60"])
    try:
        time.sleep(0.3)
        assert proc.poll() is None
        assert ccs._exe_referenced(root) == {"code_sign_clone.RUNNIN"}
        res = sweep(root, apply=True)
        assert res.removed == [] and exe.exists()
    finally:
        proc.kill()
        proc.wait()
    assert sweep(root, apply=True).removed == ["code_sign_clone.RUNNIN"]  # once it exits


@pytest.mark.parametrize("setup,reason", [
    (lambda r: (make_clone(r, "code_sign_clone.MARKED") / "KEEP-reviewer").write_text("x"), "unknown contents"),
    (lambda r: make_clone(r, "code_sign_clone.OTHERB", bundle="Something.app"), "unknown contents"),
    (lambda r: make_clone(r, "not_a_clone_dir"), "unknown entry name"),
    (lambda r: make_clone(r, "code_sign_clone.toolongname"), "unknown entry name"),
    (lambda r: (r / "code_sign_clone.SYMLNK").symlink_to(make_clone(r.parent, "elsewhere")), "not a real directory"),
])
def test_unknown_owner_or_shape_is_preserved(root, setup, reason):
    setup(root)
    before = sorted(p.name for p in root.iterdir())
    res = sweep(root, apply=True)
    assert res.removed == []
    assert sorted(p.name for p in root.iterdir()) == before
    assert any(reason in why for why in res.preserved.values())


def test_foreign_uid_is_preserved(root, monkeypatch):
    make_clone(root, "code_sign_clone.FOREIG")
    real_uid = os.getuid()
    # Root check passes with the real uid; the clone check then sees a different uid.
    calls = {"n": 0}

    def fake_getuid():
        calls["n"] += 1
        return real_uid if calls["n"] == 1 else real_uid + 1
    monkeypatch.setattr(ccs.os, "getuid", fake_getuid)
    res = sweep(root, apply=True)
    assert res.removed == [] and "unknown owner" in res.preserved["code_sign_clone.FOREIG"]


def test_new_clone_is_preserved_and_keep_only_respected(root):
    make_clone(root, "code_sign_clone.FRESH1")
    res = ccs.sweep_code_sign_clones(root=root, apply=True)  # real now: just created
    assert res.removed == [] and "too new" in res.preserved["code_sign_clone.FRESH1"]
    make_clone(root, "code_sign_clone.KEEPME")
    make_clone(root, "code_sign_clone.OTHERS")
    res = sweep(root, apply=True, keep=["code_sign_clone.KEEPME"], only=["code_sign_clone.KEEPME", "code_sign_clone.FRESH1"])
    assert res.removed == ["code_sign_clone.FRESH1"]
    assert res.preserved["code_sign_clone.KEEPME"] == "explicitly kept"
    assert res.preserved["code_sign_clone.OTHERS"] == "not selected"


def test_reference_appearing_mid_reap_restores_clone(root, monkeypatch):
    """Concurrent Chrome restart: clean at scan time, referenced after the rename."""
    c = make_clone(root, "code_sign_clone.RACING")
    real = ccs._referenced
    seq = iter([set(), {ccs._REAPING_PREFIX + "code_sign_clone.RACING"}])
    monkeypatch.setattr(ccs, "_referenced", lambda r: next(seq, real(r)))
    res = sweep(root, apply=True)
    assert res.removed == [] and c.exists()
    assert "restored" in res.preserved["code_sign_clone.RACING"]


@pytest.mark.parametrize("broken", ["_lsof_referenced", "_exe_referenced"])
def test_unavailable_reference_check_fails_closed(root, monkeypatch, broken):
    c = make_clone(root, "code_sign_clone.NOLSOF")
    monkeypatch.setattr(ccs, broken, lambda r: None)
    res = sweep(root, apply=True)
    assert res.removed == [] and c.exists() and res.error == "reference check unavailable"


def test_lsof_stderr_diagnostic_fails_closed(root, monkeypatch):
    make_clone(root, "code_sign_clone.STDERR")
    real_run = subprocess.run

    def fake_run(argv, **kw):
        if argv and argv[0] == "/usr/sbin/lsof":
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="lsof: WARNING: can't stat")
        return real_run(argv, **kw)
    monkeypatch.setattr(ccs.subprocess, "run", fake_run)
    assert ccs._lsof_referenced(root) is None
    assert sweep(root, apply=True).removed == []


def test_removal_cap_and_root_validation(root, tmp_path, monkeypatch):
    for i in range(3):
        make_clone(root, f"code_sign_clone.CAPPP{i}")
    res = sweep(root, apply=True, max_remove=2)
    assert len(res.removed) == 2 and list(res.preserved.values()) == ["over per-run removal cap"]
    monkeypatch.setattr(ccs, "_ROOT_PREFIX", "/nonexistent/folders/")
    res = sweep(root, apply=True)  # outside the allowed per-user folder now
    assert res.removed == [] and "outside" in res.error
    assert sweep(tmp_path / "missing" / ccs.CLONE_SUBDIR).error is None  # absent root is benign


def test_interrupted_reap_is_left_for_inspection(root):
    make_clone(root, ccs._REAPING_PREFIX + "code_sign_clone.HALFGN")
    res = sweep(root, apply=True)
    assert res.removed == [] and (root / (ccs._REAPING_PREFIX + "code_sign_clone.HALFGN")).exists()


def test_default_root_comes_from_os_not_tmpdir(monkeypatch):
    monkeypatch.setenv("TMPDIR", "/tmp/elsewhere/")
    r = ccs.default_clone_root()
    assert r is not None and str(r).startswith("/var/folders/") and str(r).endswith(ccs.CLONE_SUBDIR)


def test_sweep_never_raises(root, monkeypatch):
    monkeypatch.setattr(ccs, "_sweep", lambda **kw: 1 / 0)
    assert "sweep failed" in ccs.sweep_code_sign_clones(root=root).error


def test_cli_sweep_clones_dry_run_then_apply(root, monkeypatch, capsys):
    import argparse
    from hermes_cli.subcommands.browser import build_browser_parser
    make_clone(root, "code_sign_clone.CLIONE")
    monkeypatch.setattr(ccs, "default_clone_root", lambda: root)
    monkeypatch.delenv("HERMES_TEST_ISOLATION", raising=False)
    parser = argparse.ArgumentParser()
    build_browser_parser(parser.add_subparsers())
    args = parser.parse_args(["browser", "sweep-clones", "--min-age-minutes", "0"])
    assert args.func(args) == 0 and "would remove: code_sign_clone.CLIONE" in capsys.readouterr().out
    assert (root / "code_sign_clone.CLIONE").exists()
    args = parser.parse_args(["browser", "sweep-clones", "--min-age-minutes", "0", "--apply", "--json"])
    assert args.func(args) == 0
    import json
    out = json.loads(capsys.readouterr().out)
    assert out["removed"] == ["code_sign_clone.CLIONE"] and "host_free_delta_bytes" in out
    assert not (root / "code_sign_clone.CLIONE").exists()


def test_host_root_is_never_swept_under_test_isolation(monkeypatch):
    """Fake browser handles leaked by other tests reach the atexit terminate hook; it must
    not touch the developer's real clone dir. Before this guard, a suite run with 2 leaked
    FakeChrome handles swept the host and removed a real unreferenced orphan clone."""
    monkeypatch.setenv("HERMES_TEST_ISOLATION", "1")
    called = []
    monkeypatch.setattr(ccs, "default_clone_root", lambda: called.append(1))
    res = ccs.sweep_code_sign_clones(apply=True)
    assert called == [] and res.removed == [] and "test isolation" in res.error


# --- Destructive eligibility stays bound to the object being deleted (review 4841) ---

def _hook_referenced(monkeypatch, at_call, action):
    """Run ``action`` during the ``at_call``-th reference scan (1 = pre-rename scan,
    2 = post-rename scan), then report no references."""
    calls = []

    def refs(r):
        calls.append(1)
        if len(calls) == at_call:
            action()
        return set()
    monkeypatch.setattr(ccs, "_referenced", refs)


@pytest.mark.parametrize("at_call", [1, 2])
def test_hold_added_during_reference_scan_is_preserved(root, monkeypatch, at_call):
    c = make_clone(root, "code_sign_clone.HOLDIT")

    def add_hold():
        held = c if at_call == 1 else root / (ccs._REAPING_PREFIX + c.name)
        (held / "KEEP-reviewer").write_text("hold")
    _hook_referenced(monkeypatch, at_call, add_hold)
    res = sweep(root, apply=True)
    assert res.removed == [] and (c / "KEEP-reviewer").exists() and (c / "Google Chrome.app.bundle").exists()
    assert "unknown contents" in res.preserved[c.name]


@pytest.mark.parametrize("at_call", [1, 2])
def test_identically_shaped_replacement_is_preserved(root, monkeypatch, at_call):
    """The pathname is swapped for a DIFFERENT object with the exact Chrome-clone shape;
    only inode identity distinguishes it, and neither object is removed."""
    c = make_clone(root, "code_sign_clone.SWAPPD")

    def swap():
        target = c if at_call == 1 else root / (ccs._REAPING_PREFIX + c.name)
        target.rename(root / "retained-original")
        make_clone(root, target.name)
        (target / "Google Chrome.app.bundle" / "rollback-data").write_text("not ours")
    _hook_referenced(monkeypatch, at_call, swap)
    res = sweep(root, apply=True)
    assert res.removed == [] and "identity mismatch" in res.preserved[c.name]
    assert (root / "retained-original" / "Google Chrome.app.bundle").exists()
    survivors = [p for p in root.rglob("rollback-data")]
    assert len(survivors) == 1


def test_replacement_with_foreign_contents_is_preserved(root, monkeypatch):
    c = make_clone(root, "code_sign_clone.FOREGN")

    def swap():
        c.rename(root / "retained-original")
        c.mkdir()
        (c / "rollback-data").write_text("not a chrome bundle")
    _hook_referenced(monkeypatch, 1, swap)
    res = sweep(root, apply=True)
    assert res.removed == [] and (c / "rollback-data").exists()
    assert (root / "retained-original").exists()


def test_hold_dropped_after_last_check_keeps_the_clone_dir(root, monkeypatch):
    """Final window between the last revalidation and deletion: only the bound bundle
    subtree is deleted and the clone dir is rmdir'd, so a hold file survives."""
    c = make_clone(root, "code_sign_clone.LATEHD")
    parked = root / (ccs._REAPING_PREFIX + c.name)
    real_rmtree = ccs.shutil.rmtree

    def rmtree(path, **kw):
        (parked / "KEEP-reviewer").write_text("late hold")
        return real_rmtree(path, **kw)
    monkeypatch.setattr(ccs.shutil, "rmtree", rmtree)
    res = sweep(root, apply=True)
    assert res.removed == [] and (parked / "KEEP-reviewer").exists()
    assert "partial removal" in res.preserved[c.name]


class _FakeProc:
    def __init__(self, pid, uids, exe=None):
        self.pid, self._uids, self._exe = pid, uids, exe

    def exe(self):
        import psutil
        if self._exe is None:
            raise psutil.AccessDenied(self.pid)
        return self._exe

    def uids(self):
        return self._uids


def _with_procs(monkeypatch, procs):
    import psutil
    monkeypatch.setattr(psutil, "process_iter", lambda: iter(procs))
    monkeypatch.setattr(ccs, "_lsof_referenced", lambda r: set())


@pytest.mark.parametrize("uids", [
    lambda me: (me, me, me),          # same user, unreadable executable
    lambda me: (me + 1, me, me + 1),  # same effective/saved user
    lambda me: (0, 0, 0),             # root can traverse anything
])
def test_unresolved_relevant_process_fails_closed(root, monkeypatch, uids):
    c = make_clone(root, "code_sign_clone.DENIED")
    _with_procs(monkeypatch, [_FakeProc(4242, uids(os.getuid()))])
    res = sweep(root, apply=True)
    assert res.removed == [] and c.exists() and res.error == "reference check unavailable"


def test_unreadable_process_identity_fails_closed(root, monkeypatch):
    c = make_clone(root, "code_sign_clone.NOUIDS")

    class NoIdentity(_FakeProc):
        def uids(self):
            import psutil
            raise psutil.AccessDenied(self.pid)
    _with_procs(monkeypatch, [NoIdentity(4243, None)])
    assert sweep(root, apply=True).removed == [] and c.exists()


def test_known_foreign_process_with_contained_root_does_not_block(root, monkeypatch):
    """Positive control: cleanup is not merely disabled. Another non-root user's
    unreadable process is excluded only when the root is verifiably 0700 with no ACL;
    kernel PID 0 and readable unrelated processes never block."""
    root.chmod(0o700)
    c = make_clone(root, "code_sign_clone.FOREOK")
    me = os.getuid()
    procs = [_FakeProc(0, (0, 0, 0)), _FakeProc(4244, (me + 7, me + 7, me + 7)),
             _FakeProc(4245, (me, me, me), exe="/bin/sleep")]
    _with_procs(monkeypatch, procs)
    assert ccs._root_contained(root) is True
    assert sweep(root, apply=True).removed == [c.name] and not c.exists()


def test_known_foreign_process_with_open_root_fails_closed(root, monkeypatch):
    root.chmod(0o755)
    c = make_clone(root, "code_sign_clone.FOREOP")
    me = os.getuid()
    _with_procs(monkeypatch, [_FakeProc(4246, (me + 7, me + 7, me + 7))])
    assert ccs._root_contained(root) is False
    assert sweep(root, apply=True).removed == [] and c.exists()


def test_acl_on_root_defeats_containment(root):
    root.chmod(0o700)
    assert ccs._root_contained(root) is True
    subprocess.run(["/bin/chmod", "+a", "everyone allow list,search", str(root)], check=True)
    assert ccs._root_contained(root) is False
