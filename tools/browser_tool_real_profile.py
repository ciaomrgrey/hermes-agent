"""Real-profile local browsing: snapshot the user's default Chromium profile into a
hermes-owned copy, launch the real browser binary on it, and attach agent-browser.

State (``_REAL_PROFILE_SESSION``, ``_real_profile_cdp_lock``, ``_real_profile_cdp_cache``,
``_real_profile_chrome_procs``) lives in ``tools.browser_tool``; it is read
through ``_bt`` (resolved per call — never import ``tools.browser_tool`` at import time).
"""

import os
import plistlib
import re
import subprocess
import sys
import time
from typing import Optional, Tuple
from agent.proxy_bypass import loopback_request_kwargs
from tools.browser_tool_origin import origin_module as _origin
from tools import browser_tool_cloud as _cloud
from tools import browser_tool_install as _install
from tools import browser_tool_lightpanda_fallback as _lp
from tools import browser_tool_session as _session

_RP = "browser.use_real_profile is on, but "


def _terminate_real_profile_chrome() -> list:
    """Terminate real-browser processes launched for real-profile sessions (idempotent, atexit-safe);
    agent-browser only ATTACHED to them, so its own session cleanup never kills them. Returns the
    terminated Popens (their exit status is what proves those PIDs gone afterwards)."""
    from tools.browser_lightpanda import _terminate
    _bt = _origin()
    terminated = []
    while _bt._real_profile_chrome_procs:
        proc = _bt._real_profile_chrome_procs.pop()
        _terminate(proc, what="real-profile chrome")
        terminated.append(proc)
    return terminated


def _cdp_http_ready(http_cdp: str) -> bool:
    """True when an ``http://host:port`` CDP discovery root answers."""
    from tools.browser_lightpanda import _cdp_ready
    return _cdp_ready(http_cdp, timeout=1.0)


def _real_profile_daemon_env() -> dict:
    """Reaper-visible socket dir + ``owner_pid`` claim like every other lane (agent-browser's
    default dir is invisible to the reaper — #100855). The daemon-side idle timeout is dropped:
    Chrome is launched by Hermes, not the daemon, so a self-exiting daemon would leave Chrome
    holding the copy dir under the next snapshot overlay."""
    _bt = _origin()
    socket_dir = _session._prepare_session_socket_dir(_bt._REAL_PROFILE_SESSION)
    env = _session._agent_browser_command_env(socket_dir)
    env.pop("AGENT_BROWSER_IDLE_TIMEOUT_MS", None)
    return env


def _agent_browser_session_cmd(session_name: str, *cmd: str, log_label: str) -> Optional[subprocess.CompletedProcess]:
    """Run ``agent-browser --session <name> <cmd...>``; None when agent-browser is missing or the run fails."""
    _bt = _origin()
    try:
        browser_cmd = _install._find_agent_browser()
    except FileNotFoundError:
        return None
    try:
        return subprocess.run([*_session._agent_browser_argv(browser_cmd), "--session", session_name, *cmd],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
                              env=_real_profile_daemon_env(), stdin=subprocess.DEVNULL)
    except (subprocess.SubprocessError, OSError) as e:
        _bt.logger.debug("real-profile %s failed: %s", log_label, e)
        return None


def _agent_browser_get_cdp(session_name: str) -> Optional[str]:
    """HTTP CDP discovery root of an agent-browser session (from its ``ws://`` cdp-url), or None."""
    proc = _agent_browser_session_cmd(session_name, "get", "cdp-url", log_label="get cdp-url")
    m = re.search(r"ws://127\.0\.0\.1:(\d+)/", (proc.stdout or "").strip()) if proc is not None else None
    return f"http://127.0.0.1:{m.group(1)}" if m else None


def _read_devtools_port(data_dir: str) -> Optional[str]:
    """First line of Chrome's ``DevToolsActivePort`` in ``data_dir`` (None when unreadable)."""
    try:
        with open(os.path.join(data_dir, "DevToolsActivePort"), encoding="utf-8") as fh:
            return fh.readline().strip()
    except OSError:
        return None


def _surviving_chrome_cdp(data_dir: str) -> Optional[str]:
    """HTTP CDP root of a Chrome still running on ``data_dir``, or None (see ``_surviving_chrome``)."""
    return _surviving_chrome(data_dir)[0]


def _surviving_chrome(data_dir: str, exited: tuple = ()) -> Tuple[Optional[str], bool]:
    """``(http_cdp, uncertain)`` for a Chrome still running on ``data_dir``.

    Only POSITIVE identity is trusted: a well-formed ``DevToolsActivePort`` whose browser id (line 2)
    is what ``/json/version`` reports as ``webSocketDebuggerUrl`` yields ``http_cdp``. Every other
    outcome (no/malformed discovery file, timeout, refused, non-JSON, JSON without a usable
    ``webSocketDebuggerUrl``, or a recycled port answering for a different browser id) proves only
    that THIS endpoint cannot identify our browser, not that it is gone. Absence is then established
    solely by liveness: ``uncertain`` is False only when the ``SingletonLock`` PID is confirmed dead
    (or there is no lock) and no held Popen of ours is still running on ``data_dir``. The caller must
    fail closed on ``uncertain`` (never snapshot over a profile that may still be open).
    ``exited`` are Popens just terminated by this process (their exit status proves their PIDs gone)."""
    try:
        with open(os.path.join(data_dir, "DevToolsActivePort"), encoding="utf-8") as fh:
            port, browser_path = fh.readline().strip(), fh.readline().strip()
    except (OSError, UnicodeDecodeError):
        port = browser_path = ""
    if port.isdigit() and browser_path.startswith("/devtools/browser/"):
        http_cdp = f"http://127.0.0.1:{port}"
        try:
            import requests
            info = requests.get(f"{http_cdp}/json/version", timeout=2, **loopback_request_kwargs(http_cdp)).json()
            ws_url = info.get("webSocketDebuggerUrl") if isinstance(info, dict) else None
        except Exception:
            ws_url = None
        if isinstance(ws_url, str) and ws_url.startswith("ws://") and ws_url.endswith(browser_path):
            return http_cdp, False
    return None, _copy_dir_maybe_live(data_dir, exited)


def _copy_dir_maybe_live(data_dir: str, exited: tuple = ()) -> bool:
    """True unless every browser that could hold ``data_dir`` is confirmed gone: the
    ``SingletonLock`` PID (if any) is dead, and no held Popen of ours runs on ``data_dir``."""
    _bt = _origin()
    flag = f"--user-data-dir={data_dir}"
    for proc in _bt._real_profile_chrome_procs:
        args = getattr(proc, "args", None)
        if isinstance(args, (list, tuple)) and flag in args and proc.poll() is None:
            return True
    pid = _copy_browser_pid(data_dir)
    return pid is not None and _pid_alive(pid, exited)


def _pid_alive(pid: int, exited: tuple = ()) -> bool:
    """True unless ``pid`` is confirmed gone (a held/just-terminated Popen that exited, or no such process)."""
    _bt = _origin()
    for proc in (*_bt._real_profile_chrome_procs, *exited):
        if proc.pid == pid:  # our own Popen is authoritative (and immune to PID reuse)
            return proc.poll() is None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _survivor_unreadable_error(data_dir: str, browser: str) -> str:
    pid = _copy_browser_pid(data_dir)
    installed = _installed_bundle_version(browser) or "unknown"
    return (f"{_RP}a real-profile {browser} may still be running on {data_dir} (PID {pid if pid is not None else 'unknown'}), "
            "but its devtools discovery (DevToolsActivePort + /json/version) did not identify it, so its version "
            f"cannot be checked (running unknown, installed {installed}) and its profile is not snapshotted over. "
            "It was left running: quit that process, then retry.")


# Browsers whose macOS app-bundle CFBundleShortVersionString is the Chromium version that
# /json/version reports (Brave/Edge bundles carry their own product versions: not comparable).
_VERSION_GATED_BROWSERS = ("chrome", "chromium")


def _on_macos() -> bool:
    return sys.platform == "darwin"


def _installed_bundle_version(browser: str) -> Optional[str]:
    """``CFBundleShortVersionString`` of the .app holding ``chromium_executable(browser)``.
    None when the gate does not apply (not macOS, ungated browser, no bundle/Info.plist);
    "" when the plist exists but its version cannot be read (callers fail closed)."""
    from hermes_cli.browser_connect import chromium_executable
    if not _on_macos() or browser not in _VERSION_GATED_BROWSERS:
        return None
    exe = chromium_executable(browser) or ""
    cut = exe.find(".app/Contents/MacOS/")
    plist = exe[:cut + 4] + "/Contents/Info.plist" if cut > 0 else ""
    if not plist or not os.path.isfile(plist):
        return None
    try:
        with open(plist, "rb") as fh:
            version = plistlib.load(fh).get("CFBundleShortVersionString")
    except Exception:
        return ""
    return version.strip() if isinstance(version, str) else ""


def _running_browser_version(http_cdp: str) -> str:
    """Version from ``/json/version`` ``Browser`` (``Chrome/155.0.8059.40`` -> ``155.0.8059.40``); "" if unreadable."""
    try:
        import requests
        product = requests.get(f"{http_cdp}/json/version", timeout=2,
                               **loopback_request_kwargs(http_cdp)).json().get("Browser")
    except Exception:
        return ""
    return product.rsplit("/", 1)[-1].strip() if isinstance(product, str) and "/" in product else ""


def _version_mismatch(http_cdp: str, browser: Optional[str]) -> Optional[Tuple[str, str]]:
    """``(running, installed)`` when the browser at ``http_cdp`` must not be reused because the
    installed bundle changed under it (or either version is unreadable); None when reuse is safe.
    With MacAppCodeSignClone disabled, a browser running across an update execs the NEW bundle for
    its helper/renderer processes and fails code-signature validation (code_sign_clone_manager.mm)."""
    installed = _installed_bundle_version(browser) if browser else None
    if installed is None:
        return None
    running = _running_browser_version(http_cdp)
    return None if installed and running == installed else (running or "unknown", installed or "unknown")


def _copy_browser_pid(data_dir: str) -> Optional[int]:
    """PID of the browser holding ``data_dir``, from its ``SingletonLock`` (``<host>-<pid>``)."""
    try:
        m = re.search(r"-(\d+)$", os.readlink(os.path.join(data_dir, "SingletonLock")))
    except OSError:
        return None
    return int(m.group(1)) if m else None


def _retire_outdated_browser(data_dir: str, browser: str, mismatch: Tuple[str, str],
                             terminated: Optional[list] = None) -> Optional[str]:
    """Version-mismatched browser on ``data_dir``: terminate it when THIS process launched it (we hold
    its Popen) and return None so the normal snapshot+launch runs; otherwise fail closed. A browser
    we did not launch is never killed (ownership unproven) and never snapshotted over. Terminated
    Popens are appended to ``terminated`` (their exit status later proves the copy dir free)."""
    _bt = _origin()
    pid = _copy_browser_pid(data_dir)
    running, installed = mismatch
    if "unknown" in mismatch:  # restarting cannot cure an unreadable version: it would loop
        return (f"{_RP}the version of the real-profile {browser} on {data_dir} (PID {pid if pid is not None else 'unknown'}) "
                f"or of the installed {browser} could not be read (running {running}, installed {installed}), so it "
                "is not reused and was left running. Quit that process (and check the browser install), then retry.")
    if pid is not None and any(p.pid == pid and p.poll() is None for p in _bt._real_profile_chrome_procs):
        _bt.logger.info("real-profile: restarting %s PID %s (running %s, installed %s)", browser, pid, *mismatch)
        done = _terminate_real_profile_chrome()
        if terminated is not None:
            terminated.extend(done)
        return None
    return (f"{_RP}the real-profile {browser} still running on {data_dir} (PID {pid if pid is not None else 'unknown'}) "
            f"is version {running}, but the installed {browser} is {installed}. A browser running across an update "
            "fails code-signature checks, so it is not reused. It was not started by this Hermes process, so it was "
            "left running: quit that process, then retry.")


def _unreadable_install_error(data_dir: str, browser: str, existing: Optional[str]) -> str:
    """Installed Info.plist exists but its version is unreadable: refuse every reuse and snapshot,
    naming whatever identity of a browser on ``data_dir`` is available (PID, running version)."""
    port = _read_devtools_port(data_dir) or ""
    cdp = existing if existing and _cdp_http_ready(existing) else (f"http://127.0.0.1:{port}" if port.isdigit() else "")
    running = (_running_browser_version(cdp) if cdp else "") or "unknown"
    pid = _copy_browser_pid(data_dir)
    return (f"{_RP}the installed {browser} version could not be read from its Info.plist (installed unknown), so the "
            f"real-profile {browser} on {data_dir} (PID {pid if pid is not None else 'unknown'}, running {running}) "
            "cannot be checked against it: it is not reused, not snapshotted over and was left running. "
            "Reinstall the browser (quit that process if it is running), then retry, or turn the toggle off.")


def _cdp_on_data_dir(http_cdp: str, data_dir: str) -> bool:
    """True when the CDP endpoint's browser runs on ``data_dir`` (DevToolsActivePort match proves it
    is our profile copy, not a throwaway temp dir a raced/stale launch fell back to)."""
    m = re.search(r":(\d+)", http_cdp or "")
    return bool(m) and _read_devtools_port(data_dir) == m.group(1)


def _agent_browser_close_session(session_name: str) -> None:
    """Best-effort close of an agent-browser session (stale/wrong-dir cleanup)."""
    _agent_browser_session_cmd(session_name, "close", log_label="session close")


_REAL_PROFILE_CHROME_FLAGS = (
    "--remote-debugging-port=0", "--no-first-run", "--no-default-browser-check",
    "--disable-background-networking", "--disable-component-update", "--disable-default-apps",
    "--disable-hang-monitor", "--disable-popup-blocking", "--disable-prompt-on-repost",
    # MacAppCodeSignClone: macOS Google Chrome copies its whole app bundle (~1.4 GiB logical)
    # into /var/folders/<user>/X/com.google.Chrome.code_sign_clone per launch and deletes it
    # only on an orderly exit; a SIGKILL (our _terminate fallback) leaves it until reboot.
    # Tradeoff: the clone exists so a running Chrome survives an update staged on disk; without
    # it a browser running across an update hits code-signature errors (code_sign_clone_manager.mm).
    # Gate: _version_mismatch compares /json/version with the installed bundle before every reuse;
    # on a mismatch our own browser is restarted, any other one fails closed (_retire_outdated_browser).
    # Unknown feature names are ignored by other Chromium browsers.
    "--disable-sync", "--disable-features=Translate,MacAppCodeSignClone", "--no-startup-window",
)


def _real_profile_unsupported_reason(browser) -> Optional[str]:
    """Fail-closed message when the default browser can't be used, else None.

    A pre-release channel lives in a profile dir we don't resolve; normalizing to the stable
    family would drive a DIFFERENT profile/account (wrong-principal bug), so refuse rather than guess.
    """
    from hermes_cli.browser_connect import UNSUPPORTED_CHANNEL
    if browser is None:
        return (_RP + "your default browser is not a supported Chromium browser (Chrome, Edge, Brave, "
                "Brave Origin, Chromium). Real-profile browsing requires a Chromium default; set one or turn the toggle off.")
    if browser == UNSUPPORTED_CHANNEL:
        return (_RP + "your default browser is a pre-release Chromium channel (Beta / Dev / Canary), which "
                "real-profile browsing does not support. Set your default to a "
                "stable Chrome / Edge / Brave / Brave Origin / Chromium, or turn the toggle off.")
    return None


def _real_profile_snapshot_error(err: str) -> str:
    """User-facing message for a failed profile snapshot; a locked profile adds the approved-close
    command, which the agent must ASK the user about first (it quits their browser)."""
    from hermes_cli.browser_connect import _PROFILE_LOCKED_PREFIX
    if err and err.startswith(_PROFILE_LOCKED_PREFIX):
        return (err[len(_PROFILE_LOCKED_PREFIX):] + " To close it (only after the user approves — it "
                "quits their browser and loses unsaved tabs), run: `hermes browser close-profile`, then retry.")
    return f"{_RP}{err}"


def _launch_real_profile_chrome(real_binary: str, copy_dir: str) -> Tuple[Optional[int], Optional[str]]:
    """Launch the user's REAL browser binary on the profile COPY; return (debug_port, error).

    agent-browser's own launch force-adds --use-mock-keychain / --password-store=basic, which makes
    macOS Chrome drop every keychain-encrypted cookie (signed-out copy); launching the real binary
    ourselves keeps the OS keychain path intact and agent-browser attaches via ``--cdp <port>``.
    Headless by default (a focus-stealing window defeats a background capability); Chrome's NEW
    headless shares the profile's cookie store (legacy --headless does not). browser.headed /
    AGENT_BROWSER_HEADED opts into a window, except on a display-less Linux host (launch would die).
    """
    _bt = _origin()
    try:
        os.unlink(os.path.join(copy_dir, "DevToolsActivePort"))  # stale port confuses reuse probes
    except OSError:
        pass
    chrome_argv = [real_binary, f"--user-data-dir={copy_dir}", *_REAL_PROFILE_CHROME_FLAGS]
    _session._ensure_screen_for_headed_chromium()
    browser_env = _bt._build_browser_env()  # carries the Bot Desktop DISPLAY when one is running
    _has_display = bool(browser_env.get("DISPLAY") or browser_env.get("WAYLAND_DISPLAY"))
    if not (_cloud._is_headed_mode() and (_has_display or not sys.platform.startswith("linux"))):
        chrome_argv.append("--headless=new")
    try:
        chrome_proc = subprocess.Popen(chrome_argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       stdin=subprocess.DEVNULL, start_new_session=True, env=browser_env)
    except (subprocess.SubprocessError, OSError) as e:
        return None, f"{_RP}the launch failed: {e}"
    _bt._real_profile_chrome_procs.append(chrome_proc)

    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        line = _read_devtools_port(copy_dir) or ""
        if line.isdigit():
            return int(line), None
        if chrome_proc.poll() is not None:
            _terminate_real_profile_chrome()
            return None, _RP + "Chrome exited during startup (another instance may hold the profile copy)."
        time.sleep(0.25)
    _terminate_real_profile_chrome()
    return None, _RP + "the real-profile browser did not expose a debug port in time. Retry, or turn the toggle off."


def _attach_agent_browser_to_real_profile(port: int, copy_dir: str) -> Tuple[Optional[str], Optional[str]]:
    """Make agent-browser ATTACH to the running Chrome (never launch its own); returns ``(http_cdp, error)``.

    The daemon may answer with the endpoint of a browser IT spawned (throwaway temp profile);
    the DevToolsActivePort OUR Chrome wrote is authoritative on disagreement.
    """
    _bt = _origin()
    try:
        browser_cmd = _install._find_agent_browser()
    except FileNotFoundError as e:
        return None, f"{_RP}the local browser engine (agent-browser) is not installed: {e}"
    argv = [*_session._agent_browser_argv(browser_cmd), "--session", _bt._REAL_PROFILE_SESSION,
            "--cdp", str(port), "open", "about:blank"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=_bt._get_open_command_timeout(first_open=True), env=_real_profile_daemon_env(),
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, _RP + "the real-profile browser took too long to start. Retry, or turn the toggle off."
    except (subprocess.SubprocessError, OSError) as e:
        return None, f"{_RP}the launch failed: {e}"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return None, f"{_RP}the real-profile browser failed to start: {tail[-1] if tail else f'exit {proc.returncode}'}"
    cdp = _agent_browser_get_cdp(_bt._REAL_PROFILE_SESSION)
    our_port = _read_devtools_port(copy_dir)
    if our_port is not None and (m := re.search(r":(\d+)", cdp or "")) and m.group(1) != our_port:
        cdp = f"http://127.0.0.1:{our_port}"
    if not cdp:
        return None, _RP + "the real-profile browser started without exposing a devtools endpoint. Retry, or turn the toggle off."
    return cdp, None


def _real_profile_cdp() -> tuple:
    """Resolve ``(cdp_url, error)`` for consented real-profile browsing.

    Snapshot -> launch real binary on the copy -> return its HTTP CDP endpoint. The copy is a
    non-default dir, so it sidesteps the Chrome >=136 default-profile remote-debugging block and
    never contends with the user's running browser. One shared agent-browser session is reused
    across calls (cached, re-validated). ``(None, message)`` fail-closed; ``(None, None)`` when consent is off.
    """
    _bt = _origin()
    if not _cloud._use_real_profile():
        # Consent is off: delete any snapshot store (copies of cookies/logins) so
        # revoking consent actually removes the credential copies.
        try:
            from hermes_cli.browser_connect import cleanup_real_profile_snapshots
            cleanup_real_profile_snapshots()
        except Exception as e:
            _bt.logger.debug("real-profile cleanup-on-consent-off failed: %s", e)
        _bt._real_profile_cdp_cache.pop("cdp", None)
        return None, None

    # Lightpanda rejects ``--profile``; check BEFORE default-browser detection so a
    # host with no Chromium default still reports the actionable engine conflict.
    if _lp._using_lightpanda_engine():
        return None, (_RP + "browser.engine is set to 'lightpanda', which cannot load a real Chromium profile. "
                      "Set browser.engine to 'auto' or 'chrome' to use real-profile browsing, or turn the toggle off.")

    from hermes_cli.browser_connect import (chromium_executable, detect_default_chromium,
                                            real_profile_copy_dir, snapshot_real_profile)

    with _bt._real_profile_cdp_lock:
        cached = _bt._real_profile_cdp_cache.get("cdp")
        cached_browser = _bt._real_profile_cdp_cache.get("browser")
        if cached and _cdp_http_ready(cached) and not _version_mismatch(cached, cached_browser):
            # Re-claim the shared daemon's socket dir so the orphan reaper's idle clock sees
            # this process still using it (a cache hit never runs a daemon command).
            _session._prepare_session_socket_dir(_bt._REAL_PROFILE_SESSION)
            return cached, None
        _bt._real_profile_cdp_cache.pop("cdp", None)
        _bt._real_profile_cdp_cache.pop("browser", None)

        browser = detect_default_chromium()
        unsupported = _real_profile_unsupported_reason(browser)
        if unsupported:
            return None, unsupported

        # Reuse BEFORE writing anything. CRITICAL: the snapshot overlay (truncates/rewrites
        # Cookies / Login Data) must NOT run while a live copy-browser (maybe from a previous
        # hermes process) holds the user-data-dir open — that corrupts the databases.
        copy_dir = real_profile_copy_dir(browser)
        existing = _agent_browser_get_cdp(_bt._REAL_PROFILE_SESSION)
        terminated: list = []  # our outdated browsers stopped below; their exit proves the dir free
        if _installed_bundle_version(browser) == "":
            return None, _unreadable_install_error(copy_dir, browser, existing)
        if existing and _cdp_http_ready(existing) and _cdp_on_data_dir(existing, copy_dir):
            mismatch = _version_mismatch(existing, browser)
            if not mismatch:
                _bt._real_profile_cdp_cache.update(cdp=existing, browser=browser)
                return existing, None
            err = _retire_outdated_browser(copy_dir, browser, mismatch, terminated)
            if err:
                return None, err
        if existing:  # stale/wrong-dir session: close it so nothing holds the dir open
            _agent_browser_close_session(_bt._REAL_PROFILE_SESSION)
        # A Chrome from an earlier hermes process can still hold the copy dir after its attach
        # daemon was reaped (that owner died). Re-attach to it rather than overlay a live profile;
        # if the daemon cannot attach, fail closed — never snapshot over an open profile. Not ours
        # to terminate (no Popen handle): it lives until the user closes it, by design — but it is
        # reused only while its version matches the installed bundle (see _retire_outdated_browser).
        surviving, uncertain = _surviving_chrome(copy_dir, tuple(terminated))
        if uncertain:  # discovery could not identify it and it is not proven dead: absence not established
            return None, _survivor_unreadable_error(copy_dir, browser)
        if surviving and (mismatch := _version_mismatch(surviving, browser)):
            err = _retire_outdated_browser(copy_dir, browser, mismatch, terminated)
            if err:
                return None, err
            if any(_surviving_chrome(copy_dir, tuple(terminated))):  # terminated yet still (maybe) alive: never overlay it
                return None, _RP + "the outdated real-profile browser did not exit. Retry, or turn the toggle off."
            surviving = None
        if surviving:
            cdp, err = _attach_agent_browser_to_real_profile(int(surviving.rsplit(":", 1)[1]), copy_dir)
            if not cdp:
                return None, err
            _bt._real_profile_cdp_cache.update(cdp=cdp, browser=browser)
            _bt.logger.info("real-profile: re-attached to surviving Chrome at %s (%s)", cdp, copy_dir)
            return cdp, None

        copy_dir, err = snapshot_real_profile(browser)
        if err or not copy_dir:
            return None, _real_profile_snapshot_error(err)
        real_binary = chromium_executable(browser)
        if real_binary is None:
            return None, f"{_RP}the real browser binary for '{browser}' could not be found. Reinstall it or turn the toggle off."
        port, err = _launch_real_profile_chrome(real_binary, copy_dir)
        if port is None:
            return None, err
        cdp, err = _attach_agent_browser_to_real_profile(port, copy_dir)
        if not cdp:
            return None, err
        _bt._real_profile_cdp_cache.update(cdp=cdp, browser=browser)
        _bt.logger.info("real-profile browser ready for %s at %s (%s)", browser, cdp, copy_dir)
        return cdp, None
