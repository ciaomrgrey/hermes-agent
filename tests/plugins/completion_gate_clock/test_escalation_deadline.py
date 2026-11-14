"""Escalation send is bounded by the hook clock; real child, real native send stack, no network."""
import fcntl
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from test_metrics import pkg, Gate
import hermes_cli.plugins as native
from metricsgate import escalation
import slack_fixture

WORK = float(os.environ.get('CG_TEST_WORK', '1'))
ENVELOPE = float(os.environ.get('CG_TEST_ENVELOPE', '2'))


class EscalationDeadline(unittest.TestCase):
    def run_case(self, mode):
        with tempfile.TemporaryDirectory() as td:
            root = slack_fixture.estate(td)
            folder = root / 'profiles/gurney' / escalation.RECEIPT_DIR
            folder.mkdir(parents=True)
            lock = (folder / '.lock').open('a+b')
            if mode == 'lock':
                fcntl.flock(lock, fcntl.LOCK_EX)
            # The lock case measures the clock bound; the success case needs room for a real send.
            work, envelope = (WORK, ENVELOPE) if mode == 'lock' else (20.0, 25.0)
            settings = {'enabled': True, 'check_timeout_seconds': work, 'db_path': str(root / 'gate.db')}
            ctx = Mock()
            ctx.get_config.side_effect = lambda k, d: settings.get(k, d)
            pkg.register(ctx)
            manager = native.PluginManager()
            manager._hooks['before_turn_end'] = [ctx.register_hook.call_args.args[1]]
            claim = {'claim': 'offline', 'artefact_kind': 'file', 'artefact_ref': str(root / 'missing')}
            prepared = slack_fixture.popen(root, escalation.__file__, 'ok')
            launched = []

            def launch(args, **kwargs):
                self.assertEqual(Path(args[1]).name, 'escalation.py')
                launched.append(args)
                return prepared
            profiles = {name: root / 'profiles' / name for name in ('generalist', 'gurney', 'cody')}
            try:
                ready_until = time.monotonic() + 15  # setup failure bound, not gate allowance
                while not (profiles['generalist'] / 'child-ready').exists():
                    self.assertIsNone(prepared.poll(), 'fixture exited before readiness')
                    self.assertLess(time.monotonic(), ready_until, 'fixture never became ready')
                    time.sleep(.01)
                with patch.object(native, '_resolve_hook_callback_timeout', return_value=envelope), \
                        patch.object(pkg, 'bounded_extract', return_value=[claim]), \
                        patch.object(pkg, 'bounded_check', return_value=('failed', 'missing')), \
                        patch('hermes_cli.profiles.get_profile_dir', side_effect=profiles.__getitem__), \
                        patch('tools.environments.local.served_profile_child_env',
                              return_value=slack_fixture.child_env(root)), \
                        patch.object(subprocess, 'Popen', side_effect=launch):
                    start = time.monotonic()
                    # generalist chain -> escalation target gurney (#gurney-bridge)
                    with patch('hermes_constants.profile_name_for_home', return_value='generalist'):
                        manager.invoke_hook('before_turn_end', final_response='offline', session_id='offline',
                                            turn_id='one', can_continue=False)
                    elapsed = time.monotonic() - start
                    g = Gate(settings, extract=lambda _: [])
                    actions = [r['action'] for r in g.events()]
                    causes = [r['diagnostics'].get('cause') for r in g.events()]
                    self.assertFalse(manager._hook_abandoned)
                    self.assertIsNotNone(prepared.poll(), 'child was not reaped')
                    if mode == 'lock':
                        fcntl.flock(lock, fcntl.LOCK_UN)
                        time.sleep(.3)
                        self.assertGreaterEqual(elapsed, WORK - .1)
                        self.assertLess(elapsed, WORK + .8)
                        self.assertEqual(actions, ['escalation_pending', 'gate_error'])
                        self.assertEqual(causes.count('aux_timeout'), 1)
                        self.assertEqual(slack_fixture.requests(root), [])  # nothing posted late
                        self.assertEqual(slack_fixture.receipts(root), [])
                        self.assertEqual(g.metrics()['evaluated_turns'], 1)
                    else:
                        self.assertEqual(actions, ['escalation_pending', 'fail_open', 'escalation_sent'])
                        [receipt] = slack_fixture.receipts(root)
                        event = next(r for r in g.events() if r['escalation'])
                        self.assertEqual(event['escalation']['target'], 'gurney')
                        self.assertEqual(receipt['message'],
                                         escalation.message({**event['escalation'], 'event_id': event['id']}))
                        self.assertEqual((receipt['status'], receipt['channel']), ('sent', 'C0BTH5ZQH61'))
                        [request] = slack_fixture.requests(root)
                        self.assertEqual(request['json']['channel'], 'C0BTH5ZQH61')
                        # Same chain, same reason, later turn: deduplicated, never re-sent.
                        with patch('hermes_constants.profile_name_for_home', return_value='generalist'):
                            manager.invoke_hook('before_turn_end', final_response='offline', session_id='offline',
                                                turn_id='two', can_continue=False)
                        self.assertEqual(len(launched), 1)
                        self.assertEqual(len(slack_fixture.requests(root)), 1)
                        self.assertEqual(g.metrics()['evaluated_turns'], 2)
                        self.assertEqual(g.metrics()['escalations'], 1)
            finally:
                if prepared.poll() is None:
                    prepared.kill()
                prepared.communicate()
                fcntl.flock(lock, fcntl.LOCK_UN)
                lock.close()

    def test_receipt_lock_expiry_has_one_audit_and_no_late_send(self):
        self.run_case('lock')

    def test_normal_escalation_posts_once_with_receipt_and_deduplication(self):
        self.run_case('success')
