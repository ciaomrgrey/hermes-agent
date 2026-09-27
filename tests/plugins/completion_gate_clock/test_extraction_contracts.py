"""Exercise real child stdout/exit protocol without provider traffic."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_metrics import pkg, Gate
from metricsgate import extraction, diagnostics


class ExtractionContracts(unittest.TestCase):
    def test_defaults_have_measured_latency_margin(self):
        self.assertEqual(pkg.DEFAULTS['check_timeout_seconds'], 60)
        self.assertFalse({'extract_timeout', 'total_timeout', 'check_timeout'} & pkg.DEFAULTS.keys())

    def wire(self, cause, name, timeout, total, elapsed):
        # Real interpreter emits the observed envelope, exits 1, and native
        # subprocess.run(check=True) raises CalledProcessError with stdout.
        real_run = subprocess.run
        budgets = []
        now = [1000.0]
        wire = {'diagnostics': {'exception_class': name, 'cause': cause,
                'aux_provider': 'anthropic', 'attempt': 999, 'child_exit_code': 999,
                'answer': 'PRIVATE ANSWER', 'message': 'PRIVATE EXCEPTION',
                'credential': 'PRIVATE CREDENTIAL', 'artefact_ref': 'PRIVATE REF',
                'aux_model_sha256': 'PRIVATE MODEL'}}
        def run(command, **kwargs):
            budgets.append(kwargs['timeout'])
            try:
                return real_run([sys.executable, '-c',
                    'import sys; print(' + repr(json.dumps(wire)) + '); sys.exit(1)'], **kwargs)
            finally:
                now[0] += elapsed
        with patch('hermes_cli.config.load_config', return_value={}), \
             patch('hermes_cli.config.cfg_get', return_value=1), \
             patch.object(extraction.time, 'monotonic', side_effect=lambda: now[0]), \
             patch.object(extraction.subprocess, 'run', side_effect=run), \
             self.assertLogs(extraction.logger, level='WARNING') as logs:
            with self.assertRaises(diagnostics.ExtractionError) as caught:
                extraction.bounded_extract('PRIVATE ANSWER', timeout=timeout, deadline=now[0] + total)
        data = caught.exception.diagnostics
        self.assertEqual(data['cause'], cause)
        self.assertEqual(data['child_exit_code'], 1)
        self.assertEqual(data['attempt'], len(budgets))
        self.assertEqual(data, diagnostics.sanitize(data))
        self.assertNotIn('PRIVATE', json.dumps(data) + str(logs.output))
        self.assertIsNone(data['aux_model_sha256'])
        with tempfile.TemporaryDirectory() as td:
            def fail(_):
                raise caught.exception
            gate = Gate({'enabled': True, 'db_path': str(Path(td) / 'gate.db')}, extract=fail)
            self.assertIsNone(gate.evaluate('PRIVATE ANSWER', profile='test', task_id='t', turn_id='one'))
            self.assertEqual(gate.events()[0]['diagnostics'], data)
            self.assertNotIn(b'PRIVATE', gate.path.read_bytes())
        return budgets

    def test_structured_aux_timeout_12_26_wire(self):
        self.assertEqual(self.wire('aux_timeout', 'TimeoutError', 12, 26, 10), [26])

    def test_structured_aux_timeout_default_skips_smaller_retry(self):
        self.assertEqual(self.wire('aux_timeout', 'TimeoutError', 27, 34, 25), [34])

    def test_content_filtered_wire_never_retried(self):
        self.assertEqual(diagnostics.TRANSIENT, {'aux_timeout', 'child_timeout', 'transport', 'server'})
        self.assertEqual(self.wire('content_filtered', 'ContentFiltered', 27, 34, 1), [34])

    def test_fast_transport_still_retries_with_new_defaults(self):
        budgets = self.wire('transport', 'ConnectionError', 27, 34, 0.2)
        self.assertEqual(budgets[0], 34)
        self.assertAlmostEqual(budgets[1], 33.8)

    def test_internal_error_boundaries_fail_open(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as td:
            claim = {'claim': 'private', 'artefact_kind': 'unknown', 'artefact_ref': 'private'}
            gate = Gate({'enabled': True, 'db_path': str(Path(td) / 'gate.db')},
                        extract=lambda _: [claim], check=Mock(side_effect=ValueError('PRIVATE')))
            self.assertIsNone(gate.evaluate('private', profile='test', task_id='t', turn_id='check'))
            self.assertEqual(gate.events()[0]['action'], 'gate_error')
            with patch.object(gate, '_append', side_effect=RuntimeError('PRIVATE')):
                self.assertIsNone(gate.evaluate('private', profile='test', task_id='t', turn_id='write'))
            self.assertNotIn(b'PRIVATE', gate.path.read_bytes())
        ctx = Mock()
        ctx.get_config.side_effect = RuntimeError('PRIVATE')
        pkg.register(ctx)
        callback = ctx.register_hook.call_args.args[1]
        self.assertIsNone(callback('private', session_id='test', turn_id='adapter'))

    def test_audit_failure_still_delivers(self):
        with tempfile.TemporaryDirectory() as td:
            gate = Gate({'enabled': True, 'db_path': td}, extract=lambda _: [])
            self.assertIsNone(gate.evaluate('private', profile='test', task_id='t', turn_id='one'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
