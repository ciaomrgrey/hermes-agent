"""Offline native mailbox/owner lock integration; no real owner or network."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from test_metrics import pkg, Gate
import hermes_cli.plugins as native
import tools.bot_live_delivery as delivery
from metricsgate import escalation

WORK = float(os.environ.get('CG_TEST_WORK', '1'))
ENVELOPE = float(os.environ.get('CG_TEST_ENVELOPE', '2'))


class EscalationDeadline(unittest.TestCase):
    def run_case(self, mode):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            mailbox = home / 'runtime/bot_live_delivery'
            mailbox.mkdir(parents=True)
            lock_path = home / 'runtime/active_sessions.lock' if mode == 'owner' else mailbox / '.lock'
            lock = lock_path.open('a+b')
            if mode != 'success':
                fcntl.flock(lock, fcntl.LOCK_EX)
            settings = {'enabled': True, 'check_timeout_seconds': WORK, 'db_path': str(home / 'gate.db')}
            ctx = Mock()
            ctx.get_config.side_effect = lambda k, d: settings.get(k, d)
            pkg.register(ctx)
            manager = native.PluginManager()
            manager._hooks['before_turn_end'] = [ctx.register_hook.call_args.args[1]]
            owner = {'profile_home': str(home.resolve()), 'session_id': 'offline', 'lease_id': 'offline', 'live_session_id': 'offline'}
            claim = {'claim': 'offline', 'artefact_kind': 'file', 'artefact_ref': str(home / 'missing')}
            children = []
            real_popen = subprocess.Popen
            # Prewarm only the offline fixture imports, with observed readiness.
            # No work clock exists until invoke_hook below. The real send/run
            # still supplies its unchanged absolute deadline and owns kill/reap.
            prepared = real_popen([sys.executable, str(Path(__file__).with_name('offline_escalation_child.py')),
                                   str(Path(escalation.__file__)), str(home), mode],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
            children.append(prepared)
            def launch(args, **kwargs):
                self.assertEqual(Path(args[1]).name, 'escalation.py')
                return prepared
            try:
                ready_until = time.monotonic() + 10  # setup failure bound, not gate allowance
                while not (home / 'child-ready').exists():
                    self.assertIsNone(prepared.poll(), 'offline fixture exited before readiness')
                    self.assertLess(time.monotonic(), ready_until, 'offline fixture never became ready')
                    time.sleep(.01)
                with patch.object(native, '_resolve_hook_callback_timeout', return_value=ENVELOPE), patch.object(pkg, 'bounded_extract', return_value=[claim]), patch.object(pkg, 'bounded_check', return_value=('failed', 'missing')), patch('hermes_cli.profiles.get_profile_dir', return_value=home), patch.object(delivery, 'find_canonical_live_owner', return_value=owner), patch.object(subprocess, 'Popen', side_effect=launch):
                    start = time.monotonic()
                    manager.invoke_hook('before_turn_end', final_response='offline', session_id='offline', turn_id='one', can_continue=False)
                    elapsed = time.monotonic() - start
                    g = Gate(settings, extract=lambda _: [])
                    rows = g.events()
                    before = {'mode': mode, 'elapsed': elapsed, 'abandoned': bool(manager._hook_abandoned), 'actions': [r['action'] for r in rows], 'causes': [r['diagnostics'].get('cause') for r in rows], 'records': len(list(mailbox.glob('*.json'))), 'children_reaped': all(c.poll() is not None for c in children)}
                    # Release only after the callback has returned and audit was read.
                    fcntl.flock(lock, fcntl.LOCK_UN)
                    time.sleep(.3)
                    after = {'actions': [r['action'] for r in g.events()], 'records': len(list(mailbox.glob('*.json')))}
                    print(json.dumps({'before_unlock': before, 'after_unlock': after}), flush=True)
                    self.assertFalse(before['abandoned'])
                    self.assertTrue(before['children_reaped'])
                    self.assertEqual(after['actions'], before['actions'])
                    self.assertEqual(g.metrics()['evaluated_turns'], 1)
                    if mode != 'success':
                        self.assertGreaterEqual(elapsed, WORK - .1)
                        self.assertLess(elapsed, WORK + .8)
                        self.assertEqual(before['causes'].count('aux_timeout'), 1)
                        self.assertEqual(before['actions'].count('gate_error'), 1)
                        self.assertEqual(after['records'], 0)
                        self.assertTrue((home / ('entered-' + mode)).exists() if children else True)
                    else:
                        self.assertEqual(before['actions'], ['escalation_pending', 'fail_open', 'escalation_queued'])
                        self.assertEqual(after['records'], 1)
                        record = json.loads(next(mailbox.glob('*.json')).read_text())
                        event = next(r for r in rows if r['escalation'])
                        self.assertEqual(record['message'], escalation.message({**event['escalation'], 'event_id': event['id']}))
                        manager.invoke_hook('before_turn_end', final_response='offline', session_id='offline', turn_id='two', can_continue=False)
                        self.assertEqual(len(list(mailbox.glob('*.json'))), 1)
                        self.assertEqual(g.metrics()['evaluated_turns'], 2)
                        self.assertEqual(g.metrics()['escalations'], 1)
            finally:
                if prepared.poll() is None:
                    prepared.kill()
                prepared.communicate()
                fcntl.flock(lock, fcntl.LOCK_UN)
                lock.close()

    def test_mailbox_lock_expiry_has_one_audit_and_no_late_admission(self):
        self.run_case('mailbox')

    def test_owner_lookup_lock_uses_same_deadline(self):
        self.run_case('owner')

    def test_normal_escalation_preserves_receipt_and_deduplication(self):
        self.run_case('success')
