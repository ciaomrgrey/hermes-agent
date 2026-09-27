"""Real SQLite measurement contracts; no production database access."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3] / 'external' / 'completion-gate'
# One shared copy per process, however this module is imported (pytest package
# path or sibling import from a peer test), so every test patches the same code.
pkg = sys.modules.get('metricsgate')
if pkg is None or Path(pkg.__file__).resolve() != (ROOT / '__init__.py').resolve():
    spec = importlib.util.spec_from_file_location('metricsgate', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = pkg
    spec.loader.exec_module(pkg)
from metricsgate.gate import Gate


class Metrics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.gate = Gate({'enabled': True, 'db_path': str(Path(self.tmp.name) / 'gate.db')}, extract=lambda _: [])

    def test_bookkeeping_does_not_dilute_errors(self):
        g = self.gate
        g.evaluate('ok', profile='test', task_id='t', turn_id='one')
        def broken(_):
            raise RuntimeError('PRIVATE ANSWER CREDENTIAL REFERENCE')
        g.extract = broken
        self.assertIsNone(g.evaluate('private', profile='test', task_id='t', turn_id='two'))
        self.assertEqual(g.metrics()['gate_error_rate'], 0.5)
        g.mark_advice(profile='test', task_id='t', marker='advised')
        g.mark_advice(profile='test', task_id='t', marker='tried')
        with g.connect() as db:
            g._append(db, 'test', 't', '', 'escalation_queued', [], 0)
        result = g.metrics()
        for scoped in (result, result['per_profile']['test']):
            self.assertEqual(scoped['gate_error_rate'], 0.5)
            self.assertEqual(scoped['evaluated_turns'], 2)
            self.assertNotIn('turns', scoped)
            self.assertEqual(scoped['action_counts'], {'deliver': 1, 'gate_error': 1, 'advice_advised': 1, 'advice_tried': 1, 'escalation_queued': 1})
        self.assertNotIn('PRIVATE', g.path.read_bytes().decode('utf-8', errors='ignore'))

    def insert(self, created, profile, action):
        with self.gate.connect() as db:
            row = self.gate._append(db, profile, 't', str(created), action, [], 0)
            db.execute('UPDATE events SET created=? WHERE id=?', (created, row))

    def test_half_open_windows_and_profile_reconciliation(self):
        for row in [(9.9, 'outside', 'gate_error'), (10, 'a', 'deliver'),
                    (10, 'b', 'gate_error'), (15, 'a', 'reentrant_pass'),
                    (19.9, 'b', 'advice_advised'), (20, 'outside', 'gate_error')]:
            self.insert(*row)
        result = self.gate.metrics(since=10, until=20)
        self.assertEqual(result['window'], {'since': 10.0, 'until': 20.0})
        self.assertEqual(result['evaluated_turns'], 3)
        self.assertEqual(result['gate_error_rate'], 1 / 3)
        self.assertEqual(set(result['per_profile']), {'a', 'b'})
        self.assertEqual(result['per_profile']['a']['action_counts'], {'deliver': 1, 'reentrant_pass': 1})
        self.assertEqual(result['per_profile']['b']['gate_error_rate'], 1)
        self.assertEqual(sum(p['evaluated_turns'] for p in result['per_profile'].values()), 3)
        self.assertEqual(sum(p['gate_errors'] for p in result['per_profile'].values()), 1)
        self.assertEqual(self.gate.metrics(until=10)['evaluated_turns'], 1)
        self.assertEqual(self.gate.metrics(since=20)['evaluated_turns'], 1)
        self.assertEqual(self.gate.metrics(since=10, until=10)['evaluated_turns'], 0)
        self.assertEqual(self.gate.metrics()['window'], {'since': None, 'until': None})

    def test_all_outcomes_and_bookkeeping_only(self):
        from metricsgate.gate import EVALUATED_ACTIONS
        self.assertEqual(EVALUATED_ACTIONS, {'deliver', 'block', 'fail_open', 'reentrant_pass', 'gate_error'})
        self.insert(1, 'bookkeeping', 'escalation_failed')
        for action in sorted(EVALUATED_ACTIONS):
            self.insert(1, 'evaluated', action)
        result = self.gate.metrics()
        self.assertEqual(result['evaluated_turns'], 5)
        self.assertEqual(result['gate_error_rate'], 0.2)
        self.assertEqual(result['per_profile']['bookkeeping']['evaluated_turns'], 0)
        self.assertEqual(result['per_profile']['bookkeeping']['gate_error_rate'], 0)


class CLI(unittest.TestCase):
    setUp = Metrics.setUp
    insert = Metrics.insert
    def invoke(self, *args):
        import argparse
        import contextlib
        import io
        import json
        from metricsgate.cli import register_cli
        class Context:
            def get_config(ctx, key, default):
                return str(self.gate.path) if key == 'db_path' else default
            def register_cli_command(ctx, name, help, setup, handle):
                ctx.parser = argparse.ArgumentParser()
                setup(ctx.parser)
                ctx.handle = handle
        ctx = Context()
        register_cli(ctx)
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            result = ctx.handle(ctx.parser.parse_args(['metrics', *args]))
        self.assertEqual(json.loads(stream.getvalue()), result)
        return result

    def test_cli_numeric_and_iso_windows(self):
        from datetime import datetime
        self.insert(10, 'a', 'deliver')
        self.insert(20, 'b', 'gate_error')
        result = self.invoke('--since', '10.0', '--until', '20')
        self.assertEqual(result['evaluated_turns'], 1)
        self.assertEqual(result['window'], {'since': 10.0, 'until': 20.0})
        local = '2026-09-19T12:30:00'
        self.assertEqual(self.invoke('--since', local)['window']['since'], datetime.fromisoformat(local).timestamp())
        self.assertEqual(self.invoke('--until', '1970-01-01T00:00:20+00:00')['window']['until'], 20.0)
        self.assertEqual(self.invoke()['window'], {'since': None, 'until': None})

    def test_cli_invalid_bounds_rejected(self):
        import contextlib
        import io
        for value in ['nonsense', 'nan', 'inf', '-inf']:
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.invoke('--since=' + value)


if __name__ == '__main__':
    unittest.main(verbosity=2)
