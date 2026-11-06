"""Estate pin (t_6e85aa05 + t_56b4a22e), ported onto the v0.23.0 launcher: the real-profile Chrome launches
with MacAppCodeSignClone disabled, and is reused only while its running version matches the installed bundle.

Real launcher argv builder, real Info.plist parsing; only the OS check, the browser binary path, the
/json/version answer and the launcher's registry/stop are faked.
"""
import plistlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools import browser_launcher as launcher
from tools import browser_session_cloud as bs_cloud
from tools import browser_session_real_profile as rp


@pytest.fixture(autouse=True)
def _clean_launcher(monkeypatch):
    monkeypatch.setattr(launcher, "_browsers", {})
    monkeypatch.setattr(launcher, "_procs", {})


def _bundle(tmp_path, version="155.0.8059.40"):
    app = tmp_path / "Google Chrome.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    exe = app / "Contents" / "MacOS" / "Google Chrome"
    exe.write_text("")
    if version is not None:
        with open(app / "Contents" / "Info.plist", "wb") as fh:
            plistlib.dump({"CFBundleShortVersionString": version}, fh)
    return str(exe)


def _mac(monkeypatch, exe):
    monkeypatch.setattr(rp, "_on_macos", lambda: True)
    monkeypatch.setattr("hermes_cli.browser_connect.chromium_executable", lambda _b: exe)


def _running(owned=True, port=41001):
    return SimpleNamespace(cdp_url=f"ws://127.0.0.1:{port}/devtools/browser/x", http_url=f"http://127.0.0.1:{port}",
                           pid=777, owned=owned, user_data_dir="/copy/chrome")


def test_real_profile_argv_disables_code_sign_clone_and_keeps_translate_off():
    argv = launcher.build_chromium_argv("/x/chrome", "/copy", packaged=False, headed=True, sandbox_bypass=False,
                                        extra_args=rp._REAL_PROFILE_EXTRA_FLAGS)
    features = [a for a in argv if a.startswith("--disable-features=")]
    # Chromium honours the LAST --disable-features switch: it must carry both features.
    assert set(features[-1].split("=", 1)[1].split(",")) >= {"Translate", "MacAppCodeSignClone"}


def test_installed_bundle_version_reads_info_plist(tmp_path, monkeypatch):
    _mac(monkeypatch, _bundle(tmp_path, "155.0.8059.40"))
    assert rp._installed_bundle_version("chrome") == "155.0.8059.40"
    assert rp._installed_bundle_version("brave") is None  # product version not comparable: ungated
    monkeypatch.setattr(rp, "_on_macos", lambda: False)
    assert rp._installed_bundle_version("chrome") is None


def test_unreadable_plist_is_empty_not_absent(tmp_path, monkeypatch):
    exe = _bundle(tmp_path, None)
    (tmp_path / "Google Chrome.app" / "Contents" / "Info.plist").write_bytes(b"not a plist")
    _mac(monkeypatch, exe)
    assert rp._installed_bundle_version("chrome") == ""


def _consented(monkeypatch, browser="chrome"):
    monkeypatch.setattr(bs_cloud, "use_real_profile", lambda: True)
    monkeypatch.setattr("tools.browser_lightpanda.using_lightpanda_engine", lambda: False)
    monkeypatch.setattr("hermes_cli.browser_connect.detect_default_chromium", lambda: browser)


def test_matching_version_reuses_the_running_browser(tmp_path, monkeypatch):
    _mac(monkeypatch, _bundle(tmp_path, "155.0.8059.40"))
    _consented(monkeypatch)
    monkeypatch.setattr(rp, "_running_browser_version", lambda _u: "155.0.8059.40")
    running = _running()
    monkeypatch.setattr(launcher, "get_browser", lambda _k: running)
    monkeypatch.setattr(rp, "_launch", lambda _b: pytest.fail("must reuse, not launch"))
    assert rp.real_profile_cdp() == (running.cdp_url, None)


def test_our_outdated_browser_is_restarted_then_relaunched(tmp_path, monkeypatch):
    _mac(monkeypatch, _bundle(tmp_path, "156.0.1.1"))
    _consented(monkeypatch)
    monkeypatch.setattr(rp, "_running_browser_version", lambda _u: "155.0.8059.40")
    state = {"browser": _running(owned=True)}
    monkeypatch.setattr(launcher, "get_browser", lambda _k: state["browser"])
    monkeypatch.setattr(launcher, "running_instance", lambda _d: None)
    stopped = []
    monkeypatch.setattr(rp, "terminate_real_profile_browser", lambda: stopped.append(1) or state.update(browser=None))
    monkeypatch.setattr(rp, "_launch", lambda b: ("ws://fresh", None))
    assert rp.real_profile_cdp() == ("ws://fresh", None)
    assert stopped == [1]


def test_outdated_browser_we_did_not_launch_fails_closed_and_is_not_stopped(tmp_path, monkeypatch):
    _mac(monkeypatch, _bundle(tmp_path, "156.0.1.1"))
    _consented(monkeypatch)
    monkeypatch.setattr(rp, "_running_browser_version", lambda _u: "155.0.8059.40")
    monkeypatch.setattr(launcher, "get_browser", lambda _k: _running(owned=False))
    monkeypatch.setattr(rp, "terminate_real_profile_browser", lambda: pytest.fail("never stop a foreign browser"))
    monkeypatch.setattr(rp, "_launch", lambda _b: pytest.fail("never snapshot over a live profile"))
    cdp, err = rp.real_profile_cdp()
    assert cdp is None and "155.0.8059.40" in err and "156.0.1.1" in err and "left running" in err


def test_unreadable_running_version_fails_closed_without_restart_loop(tmp_path, monkeypatch):
    _mac(monkeypatch, _bundle(tmp_path, "156.0.1.1"))
    _consented(monkeypatch)
    monkeypatch.setattr(rp, "_running_browser_version", lambda _u: "")
    monkeypatch.setattr(launcher, "get_browser", lambda _k: _running(owned=True))
    monkeypatch.setattr(rp, "terminate_real_profile_browser", lambda: pytest.fail("restart cannot cure unknown"))
    cdp, err = rp.real_profile_cdp()
    assert cdp is None and "could not be read" in err


def test_unreadable_installed_version_refuses_every_reuse(tmp_path, monkeypatch):
    exe = _bundle(tmp_path, None)
    (tmp_path / "Google Chrome.app" / "Contents" / "Info.plist").write_bytes(b"junk")
    _mac(monkeypatch, exe)
    _consented(monkeypatch)
    monkeypatch.setattr(launcher, "get_browser", lambda _k: _running(owned=True))
    monkeypatch.setattr(rp, "_launch", lambda _b: pytest.fail("never snapshot without a version"))
    cdp, err = rp.real_profile_cdp()
    assert cdp is None and "installed unknown" in err


def test_launch_that_attached_to_an_outdated_survivor_fails_closed(tmp_path, monkeypatch):
    """The launcher ATTACHES to a browser an earlier Hermes process left on the copy dir: version-gated too."""
    exe = _bundle(tmp_path, "156.0.1.1")
    _mac(monkeypatch, exe)
    monkeypatch.setattr(rp, "_running_browser_version", lambda _u: "155.0.8059.40")
    monkeypatch.setattr("tools.browser_session.ensure_screen_for_headed_chromium", lambda: None)
    monkeypatch.setattr("hermes_cli.browser_connect.real_profile_copy_dir", lambda _b: "/copy/chrome")
    with patch.object(launcher, "launch_chromium", return_value=(_running(owned=False), None)):
        cdp, err = rp._launch("chrome")
    assert cdp is None and "155.0.8059.40" in err


def test_non_mac_or_ungated_browser_is_unaffected(tmp_path, monkeypatch):
    monkeypatch.setattr(rp, "_on_macos", lambda: False)
    _consented(monkeypatch)
    running = _running(owned=False)
    monkeypatch.setattr(launcher, "get_browser", lambda _k: running)
    monkeypatch.setattr(rp, "_running_browser_version", lambda _u: pytest.fail("no probe off macOS"))
    assert rp.real_profile_cdp() == (running.cdp_url, None)
