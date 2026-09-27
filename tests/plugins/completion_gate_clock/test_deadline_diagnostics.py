"""Trusted deadline classification must survive the real SQLite audit sanitizer."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from test_metrics import pkg, Gate
from metricsgate.diagnostics import ExtractionError, sanitize


class DeadlineDiagnostics(unittest.TestCase):
    def test_registered_callback_expired_errors_persist_timeout_and_metrics(self):
        for expired in (True, False):
            for error in (sqlite3.OperationalError('PRIVATE'), ValueError('PRIVATE'),
                          ExtractionError({'exception_class': 'APIConnectionError',
                                           'attempt': 2, 'child_exit_code': 7,
                                           'aux_provider': 'openai',
                                           'aux_model_sha256': 'a' * 64, 'elapsed_s': 12})):
                with self.subTest(expired=expired, error=type(error).__name__), tempfile.TemporaryDirectory() as td:
                    config = {'enabled': True, 'db_path': str(Path(td) / 'gate.db')}
                    ctx = Mock()
                    ctx.get_config.side_effect = lambda k, d: config.get(k, d)
                    pkg.register(ctx)
                    now = [1000.0]
                    def extract(*args, **kwargs):
                        now[0] = 1060.001 if expired else 1059.999
                        raise error
                    with patch.object(pkg.time, 'monotonic', side_effect=lambda: now[0]), patch.object(pkg, 'bounded_extract', side_effect=extract):
                        self.assertIsNone(ctx.register_hook.call_args.args[1]('PRIVATE', session_id='offline', turn_id='one'))
                    g = Gate(config, extract=lambda _: [])
                    rows = g.events()
                    self.assertEqual([r['action'] for r in rows], ['gate_error'])
                    expected = 'aux_timeout' if expired else ('transport' if isinstance(error, ExtractionError) else 'invalid_response' if isinstance(error, ValueError) else 'unknown')
                    data = rows[0]['diagnostics']
                    self.assertEqual(data['cause'], expected)
                    self.assertEqual(sanitize(data), data)
                    self.assertEqual(g.metrics()['gate_error_causes'], {expected: 1})
                    self.assertEqual(g.metrics()['evaluated_turns'], 1)
                    self.assertNotIn('PRIVATE', json.dumps(rows))
                    if isinstance(error, ExtractionError):
                        self.assertEqual(data['attempt'], 2)
                        self.assertEqual(data['child_exit_code'], 7)
                        self.assertEqual(data['aux_provider'], 'openai')
                        self.assertEqual(data['aux_model_sha256'], 'a' * 64)

    def test_untrusted_cause_override_remains_ignored(self):
        self.assertEqual(sanitize({'exception_class': 'ValueError', 'cause': 'aux_timeout'})['cause'], 'invalid_response')
