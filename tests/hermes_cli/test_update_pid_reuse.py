"""Focused inventory identity and restart-discovery regressions; no real signals.

Uses unittest so the bounded regression can also run without pytest installed.
"""
import contextlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gateway import status
from hermes_cli import gateway, profiles, update_inventory as ui, update_receipt
from hermes_cli import update_cmd_fleet as fleet

_REAL_FIND = gateway.find_gateway_pids
_REAL_PROFILES = gateway.find_profile_gateway_processes


class IdentityTests(unittest.TestCase):
    def test_inventory_requires_live_profile_identity(self):
        cases = ("valid", "idle", "recycled", "unrelated", "other-profile", "stopped", "dead",
                 "missing", "malformed", "bad-pid", "bad-start", "unknown", "pidfile_unknown",
                 "pidfile_valid")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as stack:
                home = Path(temp) / "profiles" / "work"
                home.mkdir(parents=True)
                stack.enter_context(patch.dict(os.environ, {"HERMES_HOME": str(home)}))
                pid = os.getpid()  # Real live unrelated process; never signalled.
                start = status.get_process_start_time(pid)
                self.assertIsNotNone(start)
                record = {"pid": pid, "start_time": start, "kind": "hermes-gateway",
                          "argv": ["hermes", "-p", "work", "gateway", "run"],
                          "hermes_home": str(home), "gateway_state": "running", "code_sha": "a" * 40}
                command = "hermes -p work gateway run"
                if case == "recycled":
                    record["start_time"] = start - 1000
                if case == "other-profile":
                    command = "hermes -p other gateway run"
                if case == "stopped":
                    record["gateway_state"] = "stopped"
                if case == "idle":
                    record["updated_at"] = "2000-01-01T00:00:00+00:00"
                if case == "bad-pid":
                    record["pid"] = "invalid"
                if case == "bad-start":
                    record["start_time"] = "invalid"
                if case in {"unknown", "pidfile_unknown"}:
                    command = None
                    record["start_time"] = None
                    stack.enter_context(patch.object(status, "_get_process_start_time", return_value=None))
                if case != "unrelated":
                    stack.enter_context(patch.object(status, "_read_process_cmdline", return_value=command))
                if case == "dead":
                    stack.enter_context(patch.object(status, "_pid_exists", return_value=False))
                state = home / "gateway_state.json"
                if case != "missing":
                    state.write_text("{invalid" if case == "malformed" else json.dumps(record))
                stack.enter_context(patch.object(update_receipt, "_socket_identity", return_value=None))
                stack.enter_context(patch.object(ui, "_supervisor_classifier", return_value=lambda pid: "manual"))
                stack.enter_context(patch.object(gateway, "find_profile_gateway_processes", _REAL_PROFILES))
                stack.enter_context(patch.object(profiles, "list_profiles", return_value=[
                    SimpleNamespace(name="work", path=home)]))
                if case.startswith("pidfile_"):
                    for name in ("gateway.pid", "gateway.lock"):
                        (home / name).write_text(json.dumps(record))
                    stack.enter_context(patch.object(status, "is_gateway_runtime_lock_active", return_value=True))
                before = {p.name: p.read_bytes() for p in home.iterdir()}
                plan = ui.UpdatePlan()
                ui._collect_gateway_runtimes(plan, [("work", home)], set())
                expected = [pid] if case in {"valid", "idle", "pidfile_valid"} else []
                self.assertEqual([r.pid for r in plan.runtimes], expected)
                self.assertEqual({p.name: p.read_bytes() for p in home.iterdir()}, before)

    def test_restart_rediscovers_targets(self):
        """Real scanner/matcher and restart loop, fake process listing and signal sink."""
        stale, valid = 81001, 81002
        signals = []
        with tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"HERMES_HOME": temp}))
            for name, value in {
                "find_gateway_pids": _REAL_FIND,
                "_get_service_pids": lambda **kw: set(),
                "_get_ancestor_pids": lambda: set(),
                "supports_systemd_services": lambda: False,
                "is_windows": lambda: False,
                "_iter_proc_cmdlines": lambda excluded: iter([
                    (stale, "/System/Library/com.apple.audio.SandboxHelper"),
                    (valid, "python -m hermes_cli.main -p work gateway run")]),
                "find_profile_gateway_processes": lambda **kw: [
                    SimpleNamespace(pid=stale, profile="old"), SimpleNamespace(pid=valid, profile="work")],
                "_prepare_profile_gateway_update_restart": lambda *a: "external-supervisor",
                "_wait_for_gateway_exit": lambda **kw: None,
            }.items():
                stack.enter_context(patch.object(gateway, name, value))
            stack.enter_context(patch.object(gateway.os.path, "isdir", lambda path: path == "/proc"))
            stack.enter_context(patch.object(fleet, "_drain_or_signal_gateway_for_update", return_value=False))
            stack.enter_context(patch.object(fleet.os, "kill", lambda pid, sig: signals.append((pid, sig))))
            outcome = fleet._GatewayRestartOutcome(False, [], [], [], [], [], [], set())
            fleet._restart_manual_gateways(outcome, None)
            self.assertEqual([pid for pid, sig in signals], [valid])
            self.assertEqual(outcome.killed_pids, {valid})
            forced = []
            stack.enter_context(patch.object(fleet._time, "sleep", return_value=None))
            stack.enter_context(patch.object(status, "get_process_start_time", return_value=123456))
            stack.enter_context(patch.object(status, "terminate_pid",
                                            lambda pid, **kw: forced.append((pid, kw))))
            fleet._force_kill_stuck_gateways({stale, valid})
            self.assertEqual(forced, [(valid, {"force": True, "expected_start_time": 123456})])
