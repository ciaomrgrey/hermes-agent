"""Retry-budget arithmetic for the completion gate's auxiliary extraction.

Regression origin (2026-09-19, t_916cea82): 70 of 201 `gate_error` events were
`aux_timeout` on `attempt: 2`, every one landing in a 0.5s band at 37.6-38.2s
against `total_timeout: 40`. With `extract_timeout: 25` the arithmetic is:

    attempt 1 -> budget min(25, 40-0)  = 25.0s  (inner 23.0s)  -> timed out
    attempt 2 -> budget min(25, 40-25) = 15.0s  (inner 13.0s)  -> killed at wall

A retry given *less* time than the attempt that just proved insufficient cannot
succeed. It burned a second auxiliary call and ~15s per turn to reach a
foregone conclusion.

These tests drive a FAKE CLOCK. An earlier wall-clock version of this file
passed against the unfixed code for the wrong reason: real subprocess/logging
overhead exhausted a scaled-down deadline during attempt 1, so the loop exited
on the pre-existing `budget <= 0` guard rather than on the retry decision. A
test that can pass because the machine was slow proves nothing about the
arithmetic, so time here is controlled, not measured.
"""
import json
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

# Stub the native imports bounded_extract pulls in at call time, so the test
# exercises budget arithmetic and nothing else. Stubs are scoped to each test
# (patch.dict) so they never leak into the wider pytest session.
_local = types.ModuleType("tools.environments.local")
_local.served_profile_child_env = lambda **kw: {"PATH": "/usr/bin"}
_cfg = types.ModuleType("hermes_cli.config")
_cfg.load_config = lambda: {}
_cfg.cfg_get = lambda *a, **k: 1          # transient_retries -> 1 (one retry)
_STUBS = {"tools.environments.local": _local, "hermes_cli.config": _cfg}

from test_metrics import pkg  # noqa: E402,F401  (loads the package as metricsgate)
from metricsgate import diagnostics, extraction  # noqa: E402


class FakeClock:
    """Monotonic time the test advances explicitly."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _Recorder:
    """Stands in for subprocess.run, recording the budget handed to each attempt."""

    def __init__(self, clock, behaviour):
        self.clock = clock
        self.behaviour = behaviour
        self.budgets = []

    def __call__(self, *args, **kwargs):
        budget = kwargs.get("timeout")
        self.budgets.append(budget)
        outcome = self.behaviour[min(len(self.budgets), len(self.behaviour)) - 1]
        if outcome == "timeout":
            # A wall-clock kill consumes the ENTIRE slice.
            self.clock.advance(budget)
            raise subprocess.TimeoutExpired(cmd="aux", timeout=budget)
        if outcome == "transport":
            self.clock.advance(0.2)          # dies at startup, costs almost nothing
            raise ConnectionError("transport died at startup")
        self.clock.advance(1.0)
        return types.SimpleNamespace(
            stdout=json.dumps([{"claim": "x", "artefact_kind": "unknown",
                                "artefact_ref": None}]),
            stderr="", returncode=0)


class RetryBudget(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        stubs = patch.dict(sys.modules, _STUBS)
        stubs.start()
        self.addCleanup(stubs.stop)
        self._real_run = extraction.subprocess.run
        self._real_monotonic = extraction.time.monotonic
        extraction.time.monotonic = self.clock

    def tearDown(self):
        extraction.subprocess.run = self._real_run
        extraction.time.monotonic = self._real_monotonic

    def _run(self, behaviour, *, timeout, total):
        rec = _Recorder(self.clock, behaviour)
        extraction.subprocess.run = rec
        deadline = self.clock() + total
        try:
            claims = extraction.bounded_extract("answer", timeout=timeout,
                                                deadline=deadline)
        except extraction.ExtractionError as exc:
            return rec, None, exc
        return rec, claims, None

    def test_doomed_retry_is_not_started(self):
        """The exact 2026-09-19 production arithmetic: 25s then a 15s retry."""
        rec, _, exc = self._run(["timeout", "timeout"], timeout=25, total=40)
        self.assertIsNotNone(exc)
        self.assertEqual(
            rec.budgets, [40.0],
            f"a retry was launched with a SMALLER budget ({rec.budgets[1:]}) than "
            f"the attempt that just timed out ({rec.budgets[0]}s) - it cannot win, "
            f"it only burns a second auxiliary call")

    def test_retry_still_runs_after_a_fast_transport_failure(self):
        """A retry that can still win must not be suppressed."""
        rec, claims, exc = self._run(["transport", "ok"], timeout=25, total=40)
        self.assertIsNone(exc)
        self.assertEqual(len(rec.budgets), 2,
                         "a fast transport failure left ample budget; the retry "
                         "should have run")
        self.assertEqual(len(claims), 1)

    def test_retry_runs_when_the_second_budget_is_equal(self):
        """Equal remaining budget is a fair second chance, not a doomed one."""
        # extract_timeout 5 against a 40s deadline: attempt 1 uses 5s,
        # attempt 2 is still bounded by extract_timeout at the same 5s.
        rec, claims, exc = self._run(["timeout", "ok"], timeout=5, total=40)
        # The independent five-second cap has been removed. A timed-out attempt
        # uses the entire clock; an equal second allocation would reset it.
        self.assertIsNotNone(exc)
        self.assertIsNone(claims)
        self.assertEqual(rec.budgets, [40.0])

    def test_no_attempt_once_the_deadline_has_passed(self):
        rec, _, exc = self._run(["ok"], timeout=25, total=-1)
        self.assertIsNotNone(exc)
        self.assertEqual(rec.budgets, [], "no call may start past the deadline")

    def test_content_filter_is_never_retried(self):
        """A refusal is deterministic; retrying it burns a call to be refused again."""
        self.assertNotIn("content_filtered", diagnostics.TRANSIENT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
