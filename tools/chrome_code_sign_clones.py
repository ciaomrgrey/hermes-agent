"""Bounded cleanup of orphaned macOS Chrome code-sign clones.

Google Chrome (not Chrome for Testing) on macOS clones its whole app bundle into
``<DARWIN_USER_TEMP_DIR>/../X/com.google.Chrome.code_sign_clone/code_sign_clone.XXXXXX``
for EVERY browser process it starts (``chrome/browser/mac/code_sign_clone_manager.mm``)
and runs the main executable from that clone. On orderly shutdown Chrome launches a
``--type=code-sign-clone-cleanup`` helper that deletes the clone after the browser exits.
A browser that dies without that orderly shutdown (SIGKILL, crash, a terminate that lands
while the clone task is still running during startup) leaves the clone behind until the
next reboot (macOS wipes ``X`` only at boot). Chrome has no reaper of its own
(crbug.com/343784575).

A clone of the CURRENT app version shares APFS extents with ``/Applications`` and frees
almost nothing; a clone of a version that Chrome has since updated away holds unique
blocks. ``du`` reports the logical size either way, so this module only ever reports the
paired ``statvfs`` free-space delta, never ``du``.

A directory is removed only when every gate passes; anything ambiguous is preserved:

* macOS, real (non-symlink) root under ``/private/var/folders/`` owned by this user;
* exact Chrome clone name, real directory owned by this user, containing exactly one
  real directory named like Chrome's clone bundle (any marker/extra file = unknown owner,
  so a reviewer or rollback holder preserves a clone by dropping any file into it);
* not in the caller's keep set, and (if given) in the caller's ``only`` set;
* untouched for ``min_age_s`` (protects a clone that a starting Chrome is still writing);
* no open file / mapped executable (``lsof +D``) and no live process whose executable
  path lies inside it (psutil). If ``lsof`` cannot run, or any process whose executable
  cannot be read is not verifiably excluded (another non-root uid AND a 0700, ACL-free
  root), NOTHING is removed;
* bound to the scanned object: dev/inode of the clone and bundle dirs are recorded at
  scan time, and identity, shape and holds are re-checked on the exact path before the
  rename, after it, and again after the second reference scan; any mismatch preserves
  (and restores, if the original name is free). Deletion removes only the bound bundle
  subtree and then ``rmdir``s the clone dir, so a hold file dropped in at the last
  instant keeps the clone dir.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

CLONE_SUBDIR = "X/com.google.Chrome.code_sign_clone"
_CLONE_NAME = re.compile(r"^code_sign_clone\.[A-Za-z0-9]{6}$")
_BUNDLE_NAMES = frozenset({"Google Chrome.app.bundle", "Google Chrome.app"})
_REAPING_PREFIX = ".hermes-reaping-"
DEFAULT_MIN_AGE_S = 15 * 60
DEFAULT_MAX_REMOVE = 8
_ROOT_PREFIX = "/private/var/folders/"  # tests point this at their tmp dir


@dataclass
class SweepResult:
    root: Optional[str]
    applied: bool
    removed: list = field(default_factory=list)
    would_remove: list = field(default_factory=list)
    preserved: dict = field(default_factory=dict)  # name -> reason
    error: Optional[str] = None
    free_bytes_before: Optional[int] = None
    free_bytes_after: Optional[int] = None

    @property
    def host_free_delta_bytes(self) -> Optional[int]:
        """Paired statvfs delta. Concurrent writers on the volume make this approximate;
        it is the only figure that may be reported as space returned to the host."""
        if self.free_bytes_before is None or self.free_bytes_after is None:
            return None
        return self.free_bytes_after - self.free_bytes_before

    def as_dict(self) -> dict:
        return {"root": self.root, "applied": self.applied, "removed": self.removed,
                "would_remove": self.would_remove, "preserved": self.preserved,
                "error": self.error, "free_bytes_before": self.free_bytes_before,
                "free_bytes_after": self.free_bytes_after,
                "host_free_delta_bytes": self.host_free_delta_bytes}


def _darwin_user_temp_dir() -> Optional[str]:
    # TMPDIR is routinely overridden (Hermes scratch dirs), so ask the OS directly.
    try:
        out = subprocess.run(["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True,
                             text=True, timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    value = (out.stdout or "").strip()
    return value if out.returncode == 0 and value else None


def default_clone_root() -> Optional[Path]:
    """``<user per-dir>/X/com.google.Chrome.code_sign_clone`` (may not exist), or None off macOS."""
    if sys.platform != "darwin":
        return None
    temp = _darwin_user_temp_dir()
    if not temp:
        return None
    return Path(temp.rstrip("/")).parent / CLONE_SUBDIR


def _validate_root(root: Path) -> Optional[str]:
    try:
        st = os.lstat(root)
    except FileNotFoundError:
        return "absent"
    except OSError as e:
        return f"unreadable root: {e}"
    if not stat.S_ISDIR(st.st_mode):
        return "root is not a real directory"
    if st.st_uid != os.getuid():
        return "root not owned by this user"
    real = os.path.realpath(root)
    if not real.startswith(_ROOT_PREFIX) or not real.endswith("/" + CLONE_SUBDIR):
        return f"root outside the macOS per-user folder: {real}"
    return None


def _inspect(clone: Path, uid: int):
    """``(identity, None)`` when ``clone`` looks exactly like a Chrome clone dir, else
    ``(None, reason)``. ``identity`` = (dev, ino) of the clone dir and of its bundle dir,
    the bundle name, and the newest change time of either: it binds a later destructive
    step to THIS object rather than to whatever is at the pathname by then."""
    try:
        st = os.lstat(clone)
    except OSError as e:
        return None, f"unreadable: {e}"
    if not stat.S_ISDIR(st.st_mode):
        return None, "not a real directory"
    if st.st_uid != uid:
        return None, "unknown owner (uid)"
    try:
        entries = os.listdir(clone)
    except OSError as e:
        return None, f"unreadable: {e}"
    if len(entries) != 1 or entries[0] not in _BUNDLE_NAMES:
        return None, "unknown contents (not exactly one Chrome clone bundle)"
    try:
        bst = os.lstat(clone / entries[0])
    except OSError as e:
        return None, f"unreadable: {e}"
    if not stat.S_ISDIR(bst.st_mode) or bst.st_uid != uid:
        return None, "unknown contents (bundle not a real user-owned directory)"
    newest = max(st.st_mtime, st.st_ctime, getattr(st, "st_birthtime", 0.0),
                 bst.st_mtime, bst.st_ctime, getattr(bst, "st_birthtime", 0.0))
    return ((st.st_dev, st.st_ino), (bst.st_dev, bst.st_ino), entries[0], newest), None


def _rebind_reason(path: Path, uid: int, identity) -> Optional[str]:
    """None only when ``path`` is still the exact object authorised at scan time, with the
    same shape and no new hold; else why it must be preserved. Rename keeps the inode
    and does not touch the directory's own ctime, so a parked clone still matches."""
    now_id, reason = _inspect(path, uid)
    if reason:
        return f"changed before removal: {reason}"
    if now_id[:3] != identity[:3]:
        return "replaced before removal (identity mismatch)"
    return None


def _lsof_referenced(root: Path) -> Optional[set]:
    """Top-level entry names under ``root`` with any open file or mapping; None when lsof
    could not give a trustworthy answer (callers then remove nothing)."""
    try:
        proc = subprocess.run(["/usr/sbin/lsof", "-nP", "-Fn", "+D", str(root)], capture_output=True,
                              text=True, errors="replace", timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("code-sign clone sweep: lsof failed: %s", e)
        return None
    # macOS lsof 4.91 exits 1 for "nothing open" AND, with +D, often even when it found
    # files; any diagnostic on stderr (can't stat, usage, permission) means the answer is
    # untrustworthy, so fail closed.
    if proc.returncode not in (0, 1) or (proc.stderr or "").strip():
        return None
    real_root = os.path.realpath(root).rstrip("/") + "/"
    names = set()
    for line in (proc.stdout or "").splitlines():
        if not line.startswith("n"):
            continue
        path = os.path.realpath(line[1:])
        if path.startswith(real_root):
            names.add(path[len(real_root):].split("/", 1)[0])
    return names


def _root_contained(root: Path) -> bool:
    """True only when ``root`` is verifiably inaccessible to other non-root users: mode
    0700 (no group/other bits) and no ACL entries (``ls -lde`` prints none)."""
    try:
        st = os.stat(root)
    except OSError:
        return False
    if st.st_mode & 0o077:
        return False
    try:
        out = subprocess.run(["/bin/ls", "-lde", str(root)], capture_output=True, text=True,
                             timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    if out.returncode != 0 or (out.stderr or "").strip():
        return False
    return len([ln for ln in (out.stdout or "").splitlines() if ln.strip()]) == 1


def _exe_referenced(root: Path) -> Optional[set]:
    """Top-level entry names under ``root`` that a live process runs its executable from.

    None (callers remove nothing) when any process whose executable cannot be read might
    still be using the clone dir. A process is excluded only on verified grounds: PID 0
    (the kernel, no user executable), or every uid of it differs from ours and is non-root
    while ``root`` is verifiably contained (0700, no ACL), so it cannot traverse ``root``."""
    try:
        import psutil
    except ImportError:
        return None
    uid = os.getuid()
    real_root = os.path.realpath(root).rstrip("/") + "/"
    contained = None
    names = set()
    for p in psutil.process_iter():
        try:
            exe = p.exe()
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except (psutil.AccessDenied, OSError):
            if getattr(p, "pid", None) == 0:
                continue
            try:
                uids = tuple(p.uids())
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except Exception:
                return None  # identity unknown: cannot rule it out
            if not uids or uid in uids or 0 in uids:
                return None  # same user (or root) with an unreadable executable
            if contained is None:
                contained = _root_contained(root)
            if not contained:
                return None
            continue
        if exe:
            exe = os.path.realpath(exe)
            if exe.startswith(real_root):
                names.add(exe[len(real_root):].split("/", 1)[0])
    return names


def _referenced(root: Path) -> Optional[set]:
    lsof_names = _lsof_referenced(root)
    exe_names = _exe_referenced(root)
    if lsof_names is None or exe_names is None:
        return None
    return lsof_names | exe_names


def _free_bytes(root: Path) -> Optional[int]:
    try:
        sv = os.statvfs(root)
    except OSError:
        return None
    return sv.f_bavail * sv.f_frsize


def sweep_code_sign_clones(*, apply: bool = False, root: Optional[Path] = None,
                           min_age_s: float = DEFAULT_MIN_AGE_S, max_remove: int = DEFAULT_MAX_REMOVE,
                           keep: Iterable[str] = (), only: Optional[Iterable[str]] = None,
                           now: Optional[float] = None) -> SweepResult:
    """Find (and with ``apply=True`` remove) orphaned Chrome code-sign clones. Never raises."""
    try:
        return _sweep(apply=apply, root=root, min_age_s=min_age_s, max_remove=max_remove,
                      keep=frozenset(keep), only=None if only is None else frozenset(only), now=now)
    except Exception as e:  # cleanup must never break the caller's browser lifecycle
        logger.debug("code-sign clone sweep failed: %s", e, exc_info=True)
        return SweepResult(root=str(root) if root else None, applied=apply, error=f"sweep failed: {e}")


def _sweep(*, apply, root, min_age_s, max_remove, keep, only, now) -> SweepResult:
    if root is None:
        if os.environ.get("HERMES_TEST_ISOLATION"):
            # Test suites leave fake browser handles behind, so the atexit terminate hook
            # would otherwise sweep the developer's REAL clone dir. Tests pass ``root``.
            return SweepResult(root=None, applied=apply, error="host sweep disabled under test isolation")
        root = default_clone_root()
        if root is None:
            return SweepResult(root=None, applied=apply, error="not macOS / no per-user temp dir")
    else:
        root = Path(root)
    result = SweepResult(root=str(root), applied=apply)
    bad = _validate_root(root)
    if bad:
        result.error = None if bad == "absent" else bad
        return result

    uid = os.getuid()
    now = time.time() if now is None else now
    candidates = []
    identities = {}
    for name in sorted(os.listdir(root)):
        clone = root / name
        if name.startswith(_REAPING_PREFIX):
            result.preserved[name] = "interrupted earlier reap (left for inspection)"
            continue
        if not _CLONE_NAME.match(name):
            result.preserved[name] = "unknown entry name"
            continue
        if name in keep:
            result.preserved[name] = "explicitly kept"
            continue
        if only is not None and name not in only:
            result.preserved[name] = "not selected"
            continue
        identity, reason = _inspect(clone, uid)
        if reason:
            result.preserved[name] = reason
            continue
        age = now - identity[3]
        if age < min_age_s:
            result.preserved[name] = f"too new ({int(age)}s < {int(min_age_s)}s)"
            continue
        candidates.append(name)
        identities[name] = identity

    if not candidates:
        return result
    referenced = _referenced(root)
    if referenced is None:
        for name in candidates:
            result.preserved[name] = "reference check unavailable (fail closed)"
        result.error = "reference check unavailable"
        return result
    eligible = []
    for name in candidates:
        if name in referenced:
            result.preserved[name] = "in use (open file, mapping or running executable)"
        else:
            eligible.append(name)
    for name in eligible[max(0, max_remove):]:
        result.preserved[name] = "over per-run removal cap"
    eligible = eligible[:max(0, max_remove)]

    if not apply:
        result.would_remove = eligible
        return result

    result.free_bytes_before = _free_bytes(root)
    for name in eligible:
        src = root / name
        parked = root / f"{_REAPING_PREFIX}{name}"
        identity = identities[name]
        # Every gate is bound to the object authorised at scan time (dev/inode of the
        # clone and bundle dirs) and re-run on the exact path about to be deleted: a
        # pathname that now holds a different object, or the same object with a new hold
        # file or changed shape, is preserved, never removed.
        why = _rebind_reason(src, uid, identity)
        if why:
            result.preserved[name] = why
            continue
        if os.path.lexists(parked):
            result.preserved[name] = "reap name already taken (left for inspection)"
            continue
        try:
            os.rename(src, parked)
        except OSError as e:
            result.preserved[name] = f"rename failed: {e}"
            continue

        def restore(reason):
            # Only put back what we parked; never move a foreign object onto src.
            if os.path.lexists(src):
                result.preserved[name] = reason + f" (left as {parked.name}: original name reused)"
                return
            try:
                os.rename(parked, src)
                result.preserved[name] = reason + " (restored)"
            except OSError as e:
                logger.warning("code-sign clone sweep: could not restore %s: %s", parked, e)
                result.preserved[name] = reason + f" (left as {parked.name})"

        why = _rebind_reason(parked, uid, identity)
        if why:
            restore(why)
            continue
        again = _referenced(root)
        if again is None or (parked.name in again) or (name in again):
            restore("reference appeared during reap")
            continue
        # The reference scan can take up to a minute: revalidate once more at the boundary.
        why = _rebind_reason(parked, uid, identity)
        if why:
            restore(why)
            continue
        # Delete only the bound bundle subtree, then rmdir the clone dir: a hold file
        # dropped in after the last check makes rmdir fail and the clone dir survives.
        bundle = parked / identity[2]
        try:
            bst = os.lstat(bundle)
        except OSError as e:
            restore(f"changed before removal: {e}")
            continue
        if (bst.st_dev, bst.st_ino) != identity[1]:
            restore("replaced before removal (bundle identity mismatch)")
            continue
        shutil.rmtree(bundle, ignore_errors=True)
        try:
            os.rmdir(parked)
        except OSError:
            pass
        if os.path.lexists(parked):
            result.preserved[name] = "partial removal (left as " + parked.name + ")"
            continue
        result.removed.append(name)
    os.sync()
    result.free_bytes_after = _free_bytes(root)
    if result.removed:
        logger.info("removed %d orphaned Chrome code-sign clone(s) %s; host free delta %s bytes",
                    len(result.removed), result.removed, result.host_free_delta_bytes)
    return result
