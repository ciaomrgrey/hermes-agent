"""Contract: the served adapter's ONLY clock knob is ``check_timeout_seconds``.

Regression origin (t_9c6b8bb0, 27 Sep 2026): every profile carried
``check_timeout_seconds: 60`` but the git source had silently reverted to the
legacy ``check_timeout``/``extract_timeout``/``total_timeout`` split (10/10/20),
so the saved 60 was ignored and the gate expired at ~20 s (gate_error ~47 %).

These tests go through real native discovery and ``ctx.get_config`` (profile
``config.yaml``), and drive the adapter with a fake monotonic clock so a
simulated 30 s extraction costs no wall time and cannot pass because the
machine happened to be fast or slow.
"""
import json
import sys
import time as real_time
import types

import pytest

from hermes_cli import plugins
from tests.completion_gate_support import no_directive


class Clock:
    def __init__(self):
        self.now = 5000.0

    def __call__(self):
        return self.now


def _load(tmp_path, monkeypatch, settings):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = {"plugins": {"enabled": ["completion-gate"], "entries": {"completion-gate": {
        "settings": {"enabled": True, "db_path": str(tmp_path / "gate.db"), **settings}}}}}
    (tmp_path / "config.yaml").write_text(json.dumps(cfg))
    from tests.completion_gate_support import install
    install(tmp_path)
    manager = plugins.PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins["completion-gate"].module
    assert loaded is not None
    clock = Clock()
    fake = types.SimpleNamespace(monotonic=clock, time=real_time.time, sleep=real_time.sleep)
    monkeypatch.setattr(loaded, "time", fake)
    monkeypatch.setattr(sys.modules[loaded.__name__ + ".gate"], "time", fake)
    seen = {}

    def extract(answer, *, max_claims, deadline, **kwargs):
        # The adapter must hand extraction the absolute single-clock deadline,
        # never a legacy per-phase timeout.
        seen["extract_deadline"] = deadline
        seen["extract_timeout_kwarg"] = kwargs.get("timeout")
        clock.now += 30.0  # a slow but legitimate 30 s auxiliary extraction
        return [{"claim": "file ready", "artefact_kind": "file", "artefact_ref": str(tmp_path)}]

    def check(claim, *, deadline=None, **kwargs):
        seen["check_deadline"] = deadline
        seen["check_timeout_kwarg"] = kwargs.get("timeout")
        return "reproduced", ""

    monkeypatch.setattr(loaded, "bounded_extract", extract)
    monkeypatch.setattr(loaded, "bounded_check", check)
    return manager, loaded, clock, seen


def _last_event(loaded, tmp_path):
    return loaded.Gate({"enabled": True, "db_path": str(tmp_path / "gate.db")},
                       extract=lambda _: []).events()[-1]


def _invoke(manager):
    return manager.invoke_hook("before_turn_end", final_response="file ready",
                               session_id="s", task_id="task", turn_id="one")


def test_defaults_expose_only_the_single_clock_setting(tmp_path, monkeypatch):
    _, loaded, _, _ = _load(tmp_path, monkeypatch, {})
    assert loaded.DEFAULTS["check_timeout_seconds"] == 60
    assert not {"check_timeout", "extract_timeout", "total_timeout"} & loaded.DEFAULTS.keys()


@pytest.mark.parametrize("settings", [
    {"check_timeout_seconds": 60},
    # Legacy keys left in a config must NOT shorten the clock.
    {"check_timeout_seconds": 60, "check_timeout": 10, "extract_timeout": 10, "total_timeout": 20},
    {},  # unset -> default 60
], ids=["configured-60", "legacy-keys-ignored", "default-60"])
def test_configured_60_gives_60s_deadline_and_30s_extraction_is_not_cut(tmp_path, monkeypatch, settings):
    manager, loaded, clock, seen = _load(tmp_path, monkeypatch, settings)
    started = clock.now
    assert no_directive(_invoke(manager))
    assert seen["extract_deadline"] == started + 60
    assert seen["extract_timeout_kwarg"] is None  # no legacy per-phase cap
    assert seen["check_deadline"] == started + 60  # same clock, not a fresh per-phase budget
    assert seen["check_timeout_kwarg"] is None
    event = _last_event(loaded, tmp_path)
    assert event["action"] == "deliver", event
    assert event["claims"][0]["verdict"] == "reproduced"


def test_check_timeout_seconds_is_actually_read(tmp_path, monkeypatch):
    """Discriminator: a 20 s setting must expire on the same 30 s extraction.

    If the adapter ignored ctx.get_config('check_timeout_seconds') this would
    deliver (default 60) instead of recording one gate_error/aux_timeout.
    """
    manager, loaded, clock, seen = _load(tmp_path, monkeypatch, {"check_timeout_seconds": 20})
    started = clock.now
    assert no_directive(_invoke(manager))  # fail-open delivery
    assert seen["extract_deadline"] == started + 20
    assert "check_deadline" not in seen
    event = _last_event(loaded, tmp_path)
    assert event["action"] == "gate_error"
    assert event["diagnostics"]["cause"] == "aux_timeout"
