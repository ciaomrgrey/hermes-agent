import importlib.util
import unittest
from pathlib import Path

class BurnRateTest(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).with_name('burn_rate_watch.py')
        self.assertTrue(path.exists(), 'measurement implementation is missing')
        spec = importlib.util.spec_from_file_location('watch', path)
        self.watch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.watch)

    def test_reported_21_percent_five_hour_burst_flags_without_history(self):
        current = {'at': 100000., 'quota': {'anthropic/five_hour': {'used': 21., 'reset': 110000.}}, 'rows': {}, 'errors': []}
        findings = self.watch.evaluate(current, [])
        self.assertIn('quota:anthropic/five_hour', findings)
        self.assertIn('21%', findings['quota:anthropic/five_hour']['text'])

    def sample(self, at=100000., used=15., reset=110000.):
        return {'at': at, 'quota': {'anthropic/five_hour': {'used': used, 'reset': reset}}, 'rows': {}, 'errors': []}

    def test_ten_point_hourly_rise_alerts_below_absolute_threshold(self):
        now = self.sample(used=15.)
        past = self.sample(at=now['at']-3600, used=5.)
        self.assertIn('burst:anthropic/five_hour', self.watch.evaluate(now, [past]))

    def test_reset_never_compares_two_different_windows(self):
        now = self.sample(used=15.)
        past = self.sample(at=now['at']-900, used=0., reset=100001.)
        self.assertNotIn('burst:anthropic/five_hour', self.watch.evaluate(now, [past]))

    def test_stale_history_not_used_as_hourly_rise(self):
        now = self.sample(used=15.)
        past = self.sample(at=now['at']-7200, used=0.)
        self.assertNotIn('burst:anthropic/five_hour', self.watch.evaluate(now, [past]))

    def test_twenty_point_five_hour_rise_on_weekly_bucket(self):
        now = self.sample(used=25.)
        past = self.sample(at=now['at']-18000, used=5.)
        now['quota']['anthropic/seven_day'] = now['quota'].pop('anthropic/five_hour')
        past['quota']['anthropic/seven_day'] = past['quota'].pop('anthropic/five_hour')
        self.assertIn('burst:anthropic/seven_day', self.watch.evaluate(now, [past]))

    def test_all_sources_absent_is_unmeasured_not_healthy(self):
        now = {'at': 100000., 'quota': {}, 'rows': {}, 'errors': ['Anthropic OAuth quota: endpoint unavailable', 'Codex OAuth quota: endpoint unavailable']}
        findings = self.watch.evaluate(now, [])
        self.assertTrue(findings)
        self.assertTrue(all('UNMEASURED' in f['text'] for f in findings.values()))

    def row(self, calls, first=1000., last=100000.):
        return {'profile': 'gurney', 'provider': 'xai-oauth', 'calls': calls, 'first': first, 'last': last}

    def test_cumulative_old_session_is_not_recent_burn(self):
        now = self.sample()
        now['rows'] = {'r': self.row(500)}
        self.assertNotIn('calls:gurney/xai-oauth:1h', self.watch.evaluate(now, []))

    def test_existing_session_counter_delta_detects_runaway(self):
        now = self.sample()
        past = self.sample(at=99100.)
        now['rows'] = {'r': self.row(530)}
        past['rows'] = {'r': self.row(500, last=99100.)}
        self.assertIn('calls:gurney/xai-oauth:1h', self.watch.evaluate(now, [past]))

    def test_historical_fully_contained_rows_flag_without_baseline(self):
        now = self.sample()
        now['rows'] = {'r': self.row(242, first=70000.)}
        self.assertIn('calls:gurney/xai-oauth:12h', self.watch.evaluate(now, []))

    def test_ledger_counter_reset_is_unknown_not_negative(self):
        now = self.sample()
        past = self.sample(at=99100.)
        now['rows'] = {'r': self.row(2)}
        past['rows'] = {'r': self.row(500)}
        findings = self.watch.evaluate(now, [past])
        self.assertTrue(any('counter' in f['text'] and 'UNMEASURED' in f['text'] for f in findings.values()))

    def test_anthropic_fraction_is_percentage_not_fraction_multiplier(self):
        self.assertTrue(hasattr(self.watch, 'parse_anthropic'), 'raw Anthropic percentage parser missing')
        windows, extra = self.watch.parse_anthropic({'five_hour': {'utilization': .5, 'resets_at': '2026-09-06T16:00:00Z'}, 'seven_day_fable': {'utilization': 7, 'resets_at': '2026-09-11T23:00:00Z'}, 'extra_usage': {'is_enabled': False}})
        self.assertEqual(.5, windows['anthropic/five_hour']['used'])
        self.assertEqual(7, windows['anthropic/seven_day_fable']['used'])
        self.assertFalse(extra['is_enabled'])

    def test_malformed_quota_is_rejected(self):
        self.assertTrue(hasattr(self.watch, 'parse_anthropic'), 'raw Anthropic percentage parser missing')
        for value in (float('nan'), -1, 101, True):
            with self.assertRaises(ValueError):
                self.watch.parse_anthropic({'five_hour': {'utilization': value, 'resets_at': '2026-09-06T16:00:00Z'}})

    def test_unchanged_alert_suppressed_but_critical_escalation_delivered(self):
        self.assertTrue(hasattr(self.watch, 'new_alerts'), 'change-only delivery missing')
        before = {'q': {'level': 'warning', 'text': '75%'}}
        now = {'q': {'level': 'warning', 'text': '77%'}}
        self.assertEqual([], self.watch.new_alerts(now, before))
        now['q']['level'] = 'critical'
        self.assertEqual(['77%'], self.watch.new_alerts(now, before))

    def test_worsening_critical_quota_realerts_at_99_and_100(self):
        previous = self.watch.evaluate(self.sample(used=90.), [])
        at_99 = self.watch.evaluate(self.sample(at=100900., used=99.), [])
        at_100 = self.watch.evaluate(self.sample(at=101800., used=100.), [])
        self.assertTrue(self.watch.new_alerts(at_99, previous))
        self.assertTrue(self.watch.new_alerts(at_100, at_99))

    def test_read_ledger_uses_readonly_tables_and_reports_missing_profiles(self):
        import tempfile, sqlite3
        self.assertTrue(hasattr(self.watch, 'read_ledger'), 'read-only ledger collector missing')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'gurney').mkdir()
            with sqlite3.connect(root/'gurney/state.db') as con:
                con.execute('CREATE TABLE session_model_usage (session_id TEXT, model TEXT, billing_provider TEXT, billing_base_url TEXT, billing_mode TEXT, task TEXT, api_call_count INT, input_tokens INT, output_tokens INT, cache_read_tokens INT, cache_write_tokens INT, reasoning_tokens INT, first_seen REAL, last_seen REAL)')
                con.execute("INSERT INTO session_model_usage VALUES ('private-session','grok','xai-oauth','','','',42,5,6,7,0,1,1,2)")
            rows, errors, counts = self.watch.read_ledger(root, ('gurney', 'missing'))
            self.assertEqual(1, len(rows))
            self.assertEqual(42, next(iter(rows.values()))['calls'])
            self.assertNotIn('private-session', str(rows))
            self.assertTrue(any('missing' in error for error in errors))
            self.assertEqual(1, counts['gurney'])

    def test_run_once_persists_fresh_evidence_but_silences_unchanged(self):
        import tempfile, json
        self.assertTrue(hasattr(self.watch, 'run_once'), 'checkpoint and alert runner missing')
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            first = self.watch.run_once(self.sample(used=21.), directory)
            self.assertIn('21%', first)
            second = self.watch.run_once(self.sample(at=100900., used=21.), directory)
            self.assertEqual('✅ Nothing to report.', second)
            latest = json.loads((directory/'latest.json').read_text())
            self.assertEqual(100900., latest['sample']['at'])
            self.assertIn('quota:anthropic/five_hour', latest['findings'])
            (directory/'state.json').write_text('broken JSON')
            output = self.watch.run_once(self.sample(at=101800., used=21.), directory)
            self.assertIn('UNMEASURED', output)

    def test_clean_first_tick_is_exactly_nothing_to_report(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            current = {'at': 100000., 'quota': {}, 'rows': {}, 'errors': []}
            self.assertEqual(
                '✅ Nothing to report.',
                self.watch.run_once(current, Path(temp)),
            )

    def test_push_contains_one_short_standalone_recommendation(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            current = {
                'at': 100000.,
                'quota': {
                    'anthropic/five_hour': {'used': 21., 'reset': 110000.},
                    'openai-codex/session': {'used': 91., 'reset': 110000.},
                    'other/weekly': {'used': 72., 'reset': 110000.},
                },
                'rows': {},
                'errors': [],
            }
            output = self.watch.run_once(current, Path(temp))
            self.assertEqual(1, len(output.splitlines()))
            self.assertTrue(all('trigger' in line for line in output.splitlines()))
            self.assertNotIn('Source check:', output)
            self.assertNotIn('[observed', output)

    def test_xai_burst_is_independent_of_anthropic_quota(self):
        now = self.sample(used=1.)
        past = self.sample(at=99100., used=1.)
        now['rows'] = {'x': self.row(40), 'a': {
            'profile': 'jared', 'provider': 'anthropic', 'calls': 2,
            'first': 1000., 'last': 100000.,
        }}
        past['rows'] = {'x': self.row(10, last=99100.), 'a': {
            'profile': 'jared', 'provider': 'anthropic', 'calls': 1,
            'first': 1000., 'last': 99100.,
        }}
        findings = self.watch.evaluate(now, [past])
        self.assertIn('calls:gurney/xai-oauth:1h', findings)
        self.assertNotIn('calls:jared/anthropic:1h', findings)

    def test_exact_cron_source_gets_bound_pause_recommendation(self):
        now = self.sample(used=1.)
        past = self.sample(at=99100., used=1.)
        now['rows'] = {'x': {
            **self.row(40), 'source_job_id': 'ce58ebbaa272',
            'source_job_name': 'Night watcher',
        }}
        past['rows'] = {'x': {
            **self.row(10, last=99100.), 'source_job_id': 'ce58ebbaa272',
            'source_job_name': 'Night watcher',
        }}
        finding = self.watch.evaluate(now, [past])['calls:gurney/xai-oauth:1h']
        self.assertEqual('cron.pause', finding['action']['kind'])
        self.assertEqual('ce58ebbaa272', finding['action']['job_id'])

    def test_bound_recommendation_emits_native_control_envelope(self):
        import json, tempfile
        now = self.sample(used=1.)
        now['rows'] = {'x': {
            **self.row(30, first=99500.), 'source_job_id': 'ce58ebbaa272',
            'source_job_name': 'Night watcher',
        }}
        with tempfile.TemporaryDirectory() as temp:
            output = self.watch.run_once(now, Path(temp))
        payload = json.loads(output)
        self.assertEqual(1, payload['hermes_cron_approval'])
        self.assertEqual(
            'ce58ebbaa272',
            payload['recommendations'][0]['action']['job_id'],
        )

    def test_openai_and_dynamic_provider_are_discovered_from_ledgers(self):
        now = self.sample()
        past = self.sample(at=99100.)
        now['rows'] = {
            'o': {'profile': 'cody', 'provider': 'openai-codex', 'calls': 31, 'first': 1., 'last': 100000.},
            'd': {'profile': 'future', 'provider': 'vertex-ai', 'calls': 28, 'first': 1., 'last': 100000.},
        }
        past['rows'] = {
            'o': {'profile': 'cody', 'provider': 'openai-codex', 'calls': 1, 'first': 1., 'last': 99100.},
            'd': {'profile': 'future', 'provider': 'vertex-ai', 'calls': 1, 'first': 1., 'last': 99100.},
        }
        findings = self.watch.evaluate(now, [past])
        self.assertIn('calls:cody/openai-codex:1h', findings)
        self.assertIn('calls:future/vertex-ai:1h', findings)

    def test_accounting_distinguishes_ledger_quota_and_billing_scopes(self):
        rows = {
            'a': {'provider': 'anthropic', 'billing_mode': 'subscription_included'},
            'x': {'provider': 'xai-oauth', 'billing_mode': ''},
            'v': {'provider': 'vertex-ai', 'billing_mode': 'api'},
        }
        status, errors = self.watch.accounting_status(
            rows,
            {'anthropic/five_hour': {'used': 1., 'reset': 2.}},
        )
        self.assertEqual('measured', status['xai-oauth']['calls_and_tokens'])
        self.assertEqual('unmeasured', status['xai-oauth']['quota'])
        self.assertEqual('subscription_included', status['anthropic']['billing'])
        self.assertEqual('unmeasured', status['vertex-ai']['billed_usage'])
        self.assertTrue(any('xai-oauth quota' in error for error in errors))
        self.assertTrue(any('vertex-ai billed usage' in error for error in errors))
        self.assertFalse(any('OpenAI API organization' in error for error in errors))

    def test_collect_sources_independently_and_fail_closed(self):
        import tempfile
        from types import SimpleNamespace
        from datetime import datetime, timezone
        self.assertTrue(hasattr(self.watch, 'collect'), 'live collector missing')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def broken():
                raise TimeoutError('never expose this or credentials')
            def codex():
                return SimpleNamespace(available=True, fetched_at=datetime.now(timezone.utc), windows=[SimpleNamespace(label='Session', used_percent=77., reset_at=datetime(2099,1,1,tzinfo=timezone.utc))])
            current = self.watch.collect(root, anthropic_reader=broken, codex_reader=codex)
            self.assertEqual(77., current['quota']['openai-codex/session']['used'])
            self.assertTrue(any('Anthropic' in e and 'TimeoutError' in e for e in current['errors']))
            self.assertNotIn('never expose', str(current))
            self.assertFalse(any('xAI' in e for e in current['errors']))

    def test_extra_usage_is_not_a_reset_based_quota_window(self):
        windows, extra = self.watch.parse_anthropic({'five_hour': {'utilization': 21, 'resets_at': '2026-09-06T16:00:00Z'}, 'extra_usage': {'is_enabled': True, 'utilization': 35, 'resets_at': None}})
        self.assertEqual(21, windows['anthropic/five_hour']['used'])
        self.assertTrue(extra['is_enabled'])
        self.assertNotIn('anthropic/extra_usage', windows)

    def test_delivery_failure_replays_original_transient_alert_without_nesting(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self.watch.run_once(self.sample(used=21.), directory)
            second = self.watch.run_once(self.sample(at=100900., used=0., reset=120000.), directory, retry_delivery=True)
            self.assertIn('21%', second)
            third = self.watch.run_once(self.sample(at=101800., used=0., reset=120000.), directory, retry_delivery=True)
            self.assertEqual(second.count('21%'), third.count('21%'))
            self.assertEqual('✅ Nothing to report.', self.watch.run_once(self.sample(at=102700., used=0., reset=120000.), directory))

    def test_new_reset_window_realerts_same_absolute_severity(self):
        before = self.watch.evaluate(self.sample(used=21.), [])
        after = self.watch.evaluate(self.sample(at=110001., used=21., reset=128000.), [])
        self.assertTrue(self.watch.new_alerts(after, before))

    def test_null_reset_preserves_percentage_but_disables_delta(self):
        windows, extra = self.watch.parse_anthropic({'nimbus_quill': {'utilization': 0, 'resets_at': None}})
        self.assertEqual({'used': 0., 'reset': None}, windows['anthropic/nimbus_quill'])

if __name__ == '__main__':
    unittest.main()
