"""Deterministic clock, bookkeeping, and non-timeout escalation contracts."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from test_metrics import pkg, Gate
from tests.completion_gate_support import no_directive


class EscalationContracts(unittest.TestCase):
    def test_combined_clock_timeout_keeps_dedup_and_one_evaluated_outcome(self):
        with tempfile.TemporaryDirectory() as td:
            settings = {'enabled': True, 'db_path': str(Path(td) / 'gate.db')}
            ctx = Mock()
            ctx.get_config.side_effect = lambda k, d: settings.get(k, d)
            pkg.register(ctx)
            callback = ctx.register_hook.call_args.args[1]
            now = [1000.0]
            claim = {'claim': 'private', 'artefact_kind': 'file', 'artefact_ref': '/private/missing'}
            def extract(*args, **kwargs):
                self.assertEqual(kwargs['deadline'], 1060)
                now[0] += 30
                return [claim]
            def check(*args, **kwargs):
                self.assertEqual(kwargs['deadline'], 1060)
                now[0] += 20
                return 'failed', 'missing'
            def send(event, *, deadline):
                self.assertEqual(deadline, 1060)
                self.assertEqual(now[0], 1050)
                now[0] = 1060
                raise TimeoutError('PRIVATE MUST NOT PERSIST')
            with patch.object(pkg.time, 'monotonic', side_effect=lambda: now[0]), patch.object(pkg, 'bounded_extract', side_effect=extract), patch.object(pkg, 'bounded_check', side_effect=check), patch.object(pkg, 'send', side_effect=send) as sent:
                self.assertTrue(no_directive(callback('private', session_id='test', turn_id='one', can_continue=False)))
                self.assertEqual(sent.call_count, 1)
            g = Gate(settings, extract=lambda _: [claim], check=lambda _: ('failed', 'missing'), escalate=Mock())
            self.assertEqual([r['action'] for r in g.events()], ['escalation_pending', 'gate_error'])
            self.assertEqual(g.metrics()['evaluated_turns'], 1)
            self.assertEqual(g.metrics()['gate_error_causes'], {'aux_timeout': 1})
            self.assertNotIn(b'PRIVATE MUST NOT PERSIST', g.path.read_bytes())
            row = g.events()[0]
            g.evaluate('private', profile=row['profile'], task_id=row['task_id'], turn_id='two', can_continue=False)
            g.escalate.assert_not_called()
            self.assertEqual([r['action'] for r in g.events()], ['escalation_pending', 'gate_error', 'fail_open'])

    def test_non_timeout_failure_preserves_fail_open_and_failed_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            claim = {'claim': 'file', 'artefact_kind': 'file', 'artefact_ref': '/private/missing'}
            g = Gate({'enabled': True, 'db_path': str(Path(td) / 'gate.db')}, extract=lambda _: [claim], check=lambda _: ('failed', 'missing'), escalate=Mock(side_effect=RuntimeError('private')))
            self.assertIsNone(g.evaluate('private', profile='test', task_id='t', turn_id='one', can_continue=False))
            self.assertEqual([r['action'] for r in g.events()], ['escalation_pending', 'fail_open', 'escalation_failed'])
            self.assertEqual(g.metrics()['evaluated_turns'], 1)
            self.assertEqual(g.metrics()['gate_errors'], 0)
            self.assertEqual(g.metrics()['claims'], 1)
