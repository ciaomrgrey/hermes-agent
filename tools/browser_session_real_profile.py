"""Real-profile local browsing: snapshot the user's default Chromium profile into a Hermes-owned copy and
launch the user's REAL browser binary on it through the launcher.

The real binary (not the packaged Chromium, and never ``--password-store=basic``/``--use-mock-keychain``)
keeps the OS keychain path intact, so macOS cookies decrypt and the copy stays signed in. One shared
browser per profile home serves every session of that profile (concurrent tasks must not launch rival
Chromiums on one copy dir); it is terminated at exit or when THAT profile turns consent off.
"""

from __future__ import annotations

import logging
import os
import plistlib
import sys
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_RP = "browser.use_real_profile is on, but "
# Keep lock contention well inside the agent's 420-second outer tool deadline (an abandoned holder
# may be a daemon worker Python cannot terminate).
_LOCK_TIMEOUT_S = 30.0
# One launch lock per profile home (the real-profile browser and its copy dir are per home): a slow or
# abandoned launch in one profile must never make another wait, then fall back to a signed-out browser.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
# Extra flags on top of the launcher's base set: no hang monitor / popup blocking / repost prompts on an
# unattended browser driving the user's own sessions.
# MacAppCodeSignClone: macOS Google Chrome copies its whole app bundle (~1.4 GiB logical) into
# /var/folders/<user>/X/com.google.Chrome.code_sign_clone per launch and deletes it only on an orderly
# exit; a SIGKILL leaves it until reboot. Tradeoff: the clone exists so a running Chrome survives an update
# staged on disk; without it a browser running across an update hits code-signature errors
# (code_sign_clone_manager.mm). Gate: ``_version_mismatch`` compares /json/version with the installed
# bundle before every reuse (``_gate``). Chromium keeps the LAST ``--disable-features``, so this one
# restates the launcher's base ``Translate``. Unknown feature names are ignored by other Chromium browsers.
_REAL_PROFILE_EXTRA_FLAGS = ("--disable-hang-monitor", "--disable-popup-blocking", "--disable-prompt-on-repost",
                             "--disable-features=Translate,MacAppCodeSignClone")
# Browsers whose macOS app-bundle CFBundleShortVersionString is the Chromium version /json/version reports
# (Brave/Edge bundles carry their own product versions: not comparable).
_VERSION_GATED_BROWSERS = ("chrome", "chromium")


def real_profile_fallback_note(err: str) -> str:
    """Tool-result note when the real profile can't load and a fresh browser serves instead: the browser keeps
    working (a consented user is never left with no browser), and the model is told it is signed out."""
    return (f"{err} Using a fresh browser without the user's logins for this session instead; "
            "tell the user if the task needs their accounts.")


def _unsupported_reason(browser) -> Optional[str]:
    """Why the default browser's profile can't be used, else None. A pre-release channel lives in a profile
    dir we don't resolve; normalizing it to the stable family would drive a DIFFERENT account."""
    from hermes_cli.browser_connect import UNSUPPORTED_CHANNEL
    if browser is None:
        return (_RP + "your default browser is not a supported Chromium browser (Chrome, Edge, Brave, "
                "Brave Origin, Chromium). Real-profile browsing requires a Chromium default; set one or turn the toggle off.")
    if browser == UNSUPPORTED_CHANNEL:
        return (_RP + "your default browser is a pre-release Chromium channel (Beta / Dev / Canary), which "
                "real-profile browsing does not support. Set your default to a "
                "stable Chrome / Edge / Brave / Brave Origin / Chromium, or turn the toggle off.")
    return None


def _snapshot_error(err: str) -> str:
    """A locked profile adds the approved-close command, which the agent must ASK the user about first."""
    from hermes_cli.browser_connect import _PROFILE_LOCKED_PREFIX
    if err and err.startswith(_PROFILE_LOCKED_PREFIX):
        return (err[len(_PROFILE_LOCKED_PREFIX):] + " To close it (only after the user approves — it "
                "quits their browser and loses unsaved tabs), run: `hermes browser close-profile`; the next "
                "browser session then uses the profile.")
    return f"{_RP}{err}"


def _home_lock() -> threading.Lock:
    from hermes_constants import hermes_home_key
    with _locks_guard:
        return _locks.setdefault(hermes_home_key(), threading.Lock())


def real_profile_key() -> str:
    """Launcher key of the CURRENT profile's real-profile browser. Per home: one process serves many profiles,
    so a consent-off profile must never stop another's browser, nor a consenting one reuse its cookie jar."""
    from hermes_constants import hermes_home_key
    return f"hermes-real-profile@{hermes_home_key()}"


def terminate_real_profile_browser() -> None:
    """Stop this profile's real-profile browser if this process launched it (idempotent)."""
    from tools.browser_launcher import stop_browser
    stop_browser(real_profile_key())


def _consent_revoked() -> tuple[None, None]:
    """Consent off: stop the copy-browser and delete the snapshot store (copies of cookies/logins), so
    revoking consent actually removes the credential copies."""
    terminate_real_profile_browser()
    try:
        from hermes_cli.browser_connect import cleanup_real_profile_snapshots
        cleanup_real_profile_snapshots()
    except OSError as exc:
        logger.debug("real-profile cleanup-on-consent-off failed: %s", exc)
    return None, None


def _on_macos() -> bool:
    return sys.platform == "darwin"


def _installed_bundle_version(browser: Optional[str]) -> Optional[str]:
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
    except Exception:  # health: allow BLE001 -- an unreadable plist is "unknown", which fails closed
        return ""
    return version.strip() if isinstance(version, str) else ""


def _running_browser_version(http_url: str) -> str:
    """Version from ``/json/version`` ``Browser`` (``Chrome/155.0.8059.40`` -> ``155.0.8059.40``); "" if unreadable."""
    try:
        import requests
        from agent.proxy_bypass import loopback_request_kwargs
        url = f"{http_url}/json/version"
        product = requests.get(url, timeout=2, **loopback_request_kwargs(url)).json().get("Browser")
    except Exception:  # health: allow BLE001 -- unreadable is "unknown", which fails closed
        return ""
    return product.rsplit("/", 1)[-1].strip() if isinstance(product, str) and "/" in product else ""


def _version_mismatch(http_url: str, browser: Optional[str]) -> Optional[tuple[str, str]]:
    """``(running, installed)`` when the browser at ``http_url`` must not be reused because the installed
    bundle changed under it (or either version is unreadable); None when reuse is safe. With
    MacAppCodeSignClone disabled, a browser running across an update execs the NEW bundle for its
    helper/renderer processes and fails code-signature validation."""
    installed = _installed_bundle_version(browser)
    if installed is None:
        return None
    running = _running_browser_version(http_url)
    return None if installed and running == installed else (running or "unknown", installed or "unknown")


def _gate(launched, browser: Optional[str]) -> tuple[Optional[str], bool]:
    """Version gate for a launcher browser about to be reused: ``(error, restart)``. ``restart`` means the
    outdated browser is this process's own launch and was stopped, so a fresh launch may follow. A browser
    this process did not launch is never stopped here (ownership unproven) and is refused instead; an
    unreadable version is refused too (restarting cannot cure it: it would loop)."""
    mismatch = _version_mismatch(launched.http_url, browser)
    if mismatch is None:
        return None, False
    running, installed = mismatch
    where = f"the real-profile {browser} on {launched.user_data_dir} (PID {launched.pid or 'unknown'})"
    if "unknown" in mismatch:
        return (f"{_RP}the version of {where} or of the installed {browser} could not be read (running "
                f"{running}, installed {installed}), so it is not reused and was left running. Quit that process "
                "(and check the browser install), then retry."), False
    if launched.owned:
        logger.info("real-profile: restarting %s PID %s (running %s, installed %s)", browser, launched.pid, *mismatch)
        terminate_real_profile_browser()
        from tools.browser_launcher import get_browser, running_instance
        if get_browser(real_profile_key()) is None and running_instance(launched.user_data_dir) is None:
            return None, True
        return f"{_RP}the outdated real-profile browser did not exit. Retry, or turn the toggle off.", False
    return (f"{_RP}{where} is version {running}, but the installed {browser} is {installed}. A browser running "
            "across an update fails code-signature checks, so it is not reused. It was not started by this Hermes "
            "process, so it was left running: quit that process, then retry."), False


def _unreadable_install_error(browser: str) -> str:
    return (f"{_RP}the installed {browser} version could not be read from its Info.plist (installed unknown), so a "
            f"real-profile {browser} cannot be checked against it: none is reused or snapshotted over. Reinstall the "
            "browser (quit it if it is running), then retry, or turn the toggle off.")


def _launch(browser_name: str) -> tuple[Optional[str], Optional[str]]:
    from hermes_cli.browser_connect import chromium_executable, real_profile_copy_dir, snapshot_real_profile
    from tools.browser_launcher import launch_chromium
    from tools.browser_session import ensure_screen_for_headed_chromium
    real_binary = chromium_executable(browser_name)
    if real_binary is None:
        return None, f"{_RP}the real browser binary for '{browser_name}' could not be found. Reinstall it or turn the toggle off."
    snapshot_failure: list[str] = []

    def overlay() -> Optional[str]:
        # The snapshot overlay rewrites Cookies / Login Data, so it runs inside the launcher's cross-process
        # step on the copy dir: never beside another process's copy, never under a live copy-browser (maybe
        # from an earlier Hermes process; the launcher ATTACHES to that one instead).
        copied, err = snapshot_real_profile(browser_name)
        if err or not copied:
            snapshot_failure.append(_snapshot_error(err))
            return snapshot_failure[0]
        return None

    ensure_screen_for_headed_chromium()
    launched, err = launch_chromium(real_profile_key(), executable=real_binary,
                                    user_data_dir=real_profile_copy_dir(browser_name), ephemeral=False,
                                    extra_args=_REAL_PROFILE_EXTRA_FLAGS, timeout=30.0, sandbox_bypass=False,
                                    prepare=overlay)
    if launched is None:
        return None, snapshot_failure[0] if snapshot_failure else f"{_RP}the real-profile browser failed to start: {err}"
    # The launcher may have ATTACHED to a browser already on the copy dir (an earlier Hermes process's):
    # reused only while its version matches the installed bundle.
    gate_err, _ = _gate(launched, browser_name) if not launched.owned else (None, False)
    if gate_err:
        return None, gate_err
    logger.info("real-profile browser ready for %s at %s (%s)", browser_name, launched.http_url, launched.user_data_dir)
    return launched.cdp_url, None


def real_profile_cdp() -> tuple[Optional[str], Optional[str]]:
    """``(cdp_url, error)`` for consented real-profile browsing.

    Snapshot → launch the real binary on the copy (or attach to the one already running) → its CDP URL.
    ``(None, message)`` when it can't (callers fall back to a fresh browser and report the message);
    ``(None, None)`` when consent is off."""
    from tools.browser_session_cloud import use_real_profile
    if not use_real_profile():
        return _consent_revoked()
    # Lightpanda cannot load a Chromium profile; checked before default-browser detection so a host with no
    # Chromium default still reports the actionable engine conflict.
    from tools.browser_lightpanda import using_lightpanda_engine
    if using_lightpanda_engine():
        return None, (_RP + "browser.engine is set to 'lightpanda', which cannot load a real Chromium profile. "
                      "Set browser.engine to 'auto' to use real-profile browsing, or turn the toggle off.")
    lock = _home_lock()
    if not lock.acquire(timeout=_LOCK_TIMEOUT_S):
        return None, (_RP + "the real-profile browser is already being prepared by another call that has not "
                      "finished. Retry after that call completes, or restart Hermes if it was abandoned.")
    try:
        from tools.browser_launcher import get_browser
        from hermes_cli.browser_connect import detect_default_chromium
        running = get_browser(real_profile_key())
        browser_name = detect_default_chromium()
        if _installed_bundle_version(browser_name) == "":
            return None, _unreadable_install_error(browser_name)
        if running is not None:
            gate_err, restart = _gate(running, browser_name)
            if gate_err:
                return None, gate_err
            if not restart:
                return running.cdp_url, None
        unsupported = _unsupported_reason(browser_name)
        if unsupported:
            return None, unsupported
        return _launch(browser_name)
    finally:
        lock.release()
