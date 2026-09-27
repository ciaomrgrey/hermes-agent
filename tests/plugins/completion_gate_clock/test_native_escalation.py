"""Real native owner discovery and admission, entirely temporary estate.

No owner/discovery/lock function or subprocess entrypoint is stubbed here.
Only the invoking child environment is pinned to a disposable estate.
"""
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from test_metrics import pkg
from metricsgate import escalation
from hermes_state import SessionDB
from hermes_cli.active_sessions import try_acquire_active_session


class NativeEscalation(unittest.TestCase):
    def test_real_owner_discovery_admission_receipt_and_both_lock_timeouts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / 'profiles/generalist'
            home.mkdir(parents=True)
            caller = root / 'profiles/cody'
            caller.mkdir()
            db = SessionDB(db_path=home / 'state.db')
            db.create_session(session_id='offline', source='cli')
            db.set_session_title('offline', 'Bot Chat')
            db.close()
            lease, refusal = try_acquire_active_session(session_id='offline', surface='desktop', config={}, registry_home=home, metadata={'bot_live_delivery_consumer': True, 'live_session_id': 'offline'})
            self.assertIsNone(refusal)
            event = {'event_id': 1, 'profile': 'cody', 'target': 'generalist', 'task_id': 'offline', 'reason': 'repeat_reason', 'block_count': 1}
            env = {'HERMES_HOME': str(caller), 'PATH': os.environ['PATH'], 'HOME': str(root)}
            try:
                with patch('tools.environments.local.served_profile_child_env', return_value=env):
                    first = escalation.send(event, deadline=time.monotonic() + 5)
                    self.assertEqual(first['status'], 'queued')
                    self.assertEqual(escalation.send(event, deadline=time.monotonic() + 5), first)
                    mailbox = home / 'runtime/bot_live_delivery'
                    record = json.loads((mailbox / (first['delivery_id'] + '.json')).read_text())
                    self.assertEqual(record['owner']['lease_id'], lease.lease_id)
                    self.assertEqual(record['message'], escalation.message(event))
                    for index, path in enumerate([home / 'runtime/active_sessions.lock', mailbox / '.lock'], 2):
                        with path.open('a+b') as lock:
                            fcntl.flock(lock, fcntl.LOCK_EX)
                            start = time.monotonic()
                            try:
                                with self.assertRaises(TimeoutError):
                                    escalation.send({**event, 'event_id': index}, deadline=start + 1)
                                self.assertLess(time.monotonic() - start, 1.8)
                            finally:
                                fcntl.flock(lock, fcntl.LOCK_UN)
                        time.sleep(.2)
                        self.assertEqual(len(list(mailbox.glob('*.json'))), 1)
                    print('Real temporary native owner/receipt/dedup + registry and mailbox locks: PASS', flush=True)
            finally:
                lease.release()
