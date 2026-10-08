"""Offline deterministic registered callback deadline contracts."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from test_metrics import pkg, Gate
from tests.completion_gate_support import no_directive

class Deadline(unittest.TestCase):
    def test_expired_event_scan_is_audited_even_when_empty(self):
        with tempfile.TemporaryDirectory() as td:
            config = {'enabled': True, 'db_path': str(Path(td) / 'gate.db')}
            ctx = Mock()
            ctx.get_config.side_effect = lambda k, d: config.get(k, d)
            pkg.register(ctx)
            now = [1000.0]
            def events(_):
                now[0] += 61
                return []
            with patch.object(pkg.time, 'monotonic', side_effect=lambda: now[0]), patch.object(Gate, 'events', events), patch.object(pkg, 'bounded_extract') as extract:
                self.assertTrue(no_directive(ctx.register_hook.call_args.args[1]('private', session_id='offline', turn_id='scan', user_message='hello')))
                extract.assert_not_called()
            rows = Gate(config, extract=lambda _: []).events()
            self.assertEqual([r['action'] for r in rows], ['gate_error'])
            self.assertEqual(rows[0]['diagnostics']['cause'], 'aux_timeout')

    def test_exact_boundary_and_combined_success(self):
        for cost, action in [(59, 'deliver'), (60, 'gate_error'), (61, 'gate_error')]:
            with self.subTest(cost=cost), tempfile.TemporaryDirectory() as td:
                now = [1000.0]
                def extract(_):
                    now[0] += cost
                    return []
                g = Gate({'enabled': True, 'db_path': str(Path(td) / 'gate.db')}, extract=extract)
                with patch.object(pkg.time, 'monotonic', side_effect=lambda: now[0]):
                    self.assertIsNone(g.evaluate('private', profile='test', task_id='t', turn_id='one', deadline=1060))
                self.assertEqual([r['action'] for r in g.events()], [action])

    def test_database_wait_uses_same_deadline_and_records_timeout(self):
        import sqlite3
        import threading
        import time
        with tempfile.TemporaryDirectory() as td:
            g = Gate({'enabled': True, 'db_path': str(Path(td) / 'gate.db')}, extract=lambda _: [])
            g.events()
            db = sqlite3.connect(g.path, check_same_thread=False)
            db.execute('BEGIN EXCLUSIVE')
            timer = threading.Timer(.2, db.commit)
            timer.start()
            try:
                with patch('metricsgate.gate.sqlite3.connect', wraps=sqlite3.connect) as connect:
                    self.assertIsNone(g.evaluate('private', profile='test', task_id='t', turn_id='one', deadline=time.monotonic() + .05))
                    self.assertLessEqual(connect.call_args_list[0].kwargs['timeout'], .05)
            finally:
                timer.join()
                db.close()
            rows = g.events()
            self.assertEqual([r['action'] for r in rows], ['gate_error'])
            self.assertEqual(rows[0]['diagnostics']['cause'], 'aux_timeout')

    def test_check_subprocess_expiry_propagates(self):
        import subprocess
        from metricsgate import checks
        with patch.object(checks.subprocess, 'run', side_effect=subprocess.TimeoutExpired('private', 60)):
            with self.assertRaises(TimeoutError):
                checks.bounded_check({'artefact_kind': 'file', 'artefact_ref': '/private'}, deadline=pkg.time.monotonic() + 60)

    def test_verification_expiry_is_one_audited_aux_timeout(self):
        with tempfile.TemporaryDirectory() as td:
            config = {'enabled': True, 'db_path': str(Path(td) / 'gate.db')}
            ctx = Mock()
            ctx.get_config.side_effect = lambda k, d: config.get(k, d)
            pkg.register(ctx)
            callback = ctx.register_hook.call_args.args[1]
            now = [1000.0]
            def extract(*args, **kwargs):
                now[0] += 30
                return [{'claim': 'private', 'artefact_kind': 'unknown', 'artefact_ref': None}]
            def check(*args, **kwargs):
                now[0] += 31
                return 'unverified', 'check_unavailable'
            with patch.object(pkg.time, 'monotonic', side_effect=lambda: now[0]), patch.object(pkg, 'bounded_extract', side_effect=extract), patch.object(pkg, 'bounded_check', side_effect=check):
                self.assertTrue(no_directive(callback('private', session_id='offline', turn_id='one')))
            rows = Gate(config, extract=lambda _: []).events()
            self.assertEqual([r['action'] for r in rows], ['gate_error'])
            self.assertEqual(rows[0]['diagnostics']['cause'], 'aux_timeout')
