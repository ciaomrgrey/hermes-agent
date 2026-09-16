"""Known-answer synthetic fixtures; never contacts a provider."""
import importlib
import importlib.util


def test_weekly_source_identity_and_percent_units():
    spec = importlib.util.find_spec('agent.usage_guard')
    assert spec is not None, 'weekly guard is not implemented'
    g = importlib.import_module('agent.usage_guard')
    a = g.parse_weekly('anthropic', {'seven_day': {'utilization': .5, 'resets_at': '2026-09-18T23:00:00Z'}, 'five_hour': {'utilization': 99}})
    assert a['pp'] == .5
    a_jitter = g.parse_weekly('anthropic', {'seven_day': {'utilization': .5, 'resets_at': '2026-09-18T23:00:00.643485Z'}})
    assert a_jitter['reset'] == a['reset']  # live endpoint jitters subsecond reset
    assert a_jitter['raw_reset'] != a['raw_reset']
    c = g.parse_weekly('openai-codex', {'rate_limit': {'primary_window': {'used_percent': 32, 'limit_window_seconds': 604800, 'reset_at': 1789772400}, 'secondary_window': None}})
    assert c['pp'] == 32
    assert c['field'] == 'rate_limit.primary_window'
    assert g.parse_weekly('xai-oauth', {})['status'] == 'unsupported'
    assert g.parse_weekly('anthropic', {'seven_day': None})['status'] == 'unavailable'


def test_rolling_every_offset_and_incomplete_segments():
    from agent import usage_guard as g
    assert hasattr(g, 'rolling'), 'rolling calculation missing'
    # Synthetic peak is 01:00–07:00 UTC, not aligned to six-hour buckets.
    rows = [dict(t=t, pp=max(0, min(12, (t-3600)/1800)), reset=604800,
                 grant='synthetic-account', status='ok') for t in range(0, 108001, 60)]
    peaks = [g.rolling(rows, t, 21600, 90) for t in range(8640, 108001, 60)]
    peak = max(peaks, key=lambda p: p['pp'] if p['pp'] is not None else -1)
    assert (peak['pp'], peak['start'], peak['end'], peak['coverage']) == (12, 3600, 25200, 'sampled')
    assert g.rolling(rows[:10], 540, 21600, 90)['coverage'] == 'partial'
    broken = [dict(r) for r in rows[:361]]
    broken[200]['status'] = 'unavailable'
    assert g.rolling(broken, 21600, 21600, 90)['coverage'] == 'partial'
    broken = [dict(r) for r in rows[:361]]
    broken[300]['grant'] = 'changed-account'
    assert g.rolling(broken, 21600, 21600, 90)['coverage'] == 'partial'
    broken = [dict(r, reset=700000 if r['t'] >= 12000 else 604800) for r in rows[:361]]
    assert g.rolling(broken, 21600, 21600, 90)['coverage'] == 'partial'
    assert g.rolling(rows, 109000, 21600, 90)['pp'] is None
    assert g.rolling(rows[:1], 0, 21600, 90)['pp'] is None  # one reading cannot establish burn


def test_malformed_and_counter_correction_are_not_green():
    from agent import usage_guard as g
    for pp in (None, True, float('nan'), float('inf'), -1, 101, '0.5'):
        assert g.parse_weekly('anthropic', {'seven_day': {'utilization': pp, 'resets_at': '2026-09-18T23:00:00Z'}})['status'] == 'unavailable'
    for payload in ({}, {'rate_limit': None}, {'rate_limit': {'primary_window': {'used_percent': 90, 'limit_window_seconds': 18000}}}):
        assert g.parse_weekly('openai-codex', payload)['status'] == 'unavailable'
    rows = [dict(t=t, pp=20 if t < 12000 else 1, reset=604800, grant='synthetic', status='ok') for t in range(0,21601,60)]
    result = g.rolling(rows,21600)
    assert result['coverage'] == 'partial' and result['pp'] == 0


def test_durable_breach_outbox_and_night_hold(tmp_path):
    from agent import usage_guard as g
    assert hasattr(g, 'Guard'), 'durable guard missing'
    at = g.timestamp('2026-09-16T22:00:00+02:00')
    cfg = dict(threshold_pp=10.0, window_seconds=21600, sample_seconds=60,
               timezone='Europe/Zurich', night_start='22:00', night_end='06:00')
    db = tmp_path / 'state.sqlite3'
    guard = g.Guard(db, cfg)
    row = dict(status='ok', pp=1, reset=at+604800, grant='synthetic')
    for t in range(int(at)-21600, int(at), 60):
        guard.sample('anthropic', t, row)
    guard.sample('anthropic', at, dict(row, pp=11))
    assert guard.outbox() == []  # strict >, not >=
    guard.sample('anthropic', at+60, dict(row, pp=11.0001))
    assert len(guard.outbox()) == 1
    assert guard.holds() == ['anthropic']
    first = guard.outbox()[0]
    guard.close()
    guard = g.Guard(db, cfg)
    guard.sample('anthropic', at+120, dict(row, pp=12))
    assert guard.outbox()[0]['body'] == first['body']
    def failed(_body):
        raise OSError('synthetic failure')
    guard.deliver(failed)
    assert len(guard.outbox()) == 1
    guard.deliver(lambda body: 'synthetic-telegram-receipt')
    assert guard.outbox() == []
    assert guard.holds() == ['anthropic']
    for stamp, expected in [('2026-09-16T21:59:00+02:00', False),
                            ('2026-09-16T22:00:00+02:00', True),
                            ('2026-09-17T05:59:00+02:00', True),
                            ('2026-09-17T06:00:00+02:00', False),
                            ('2026-10-25T02:30:00+02:00', True),
                            ('2026-10-25T02:30:00+01:00', True)]:
        assert guard.is_night(g.timestamp(stamp)) == expected
    guard.close()


def test_daily_report_always_enqueues_and_names_partial_peak(tmp_path):
    from agent import usage_guard as g
    assert hasattr(g.Guard, 'report'), 'daily report missing'
    cfg = dict(threshold_pp=10, window_seconds=21600, sample_seconds=60,
               timezone='Europe/Zurich', night_start='22:00', night_end='06:00')
    guard = g.Guard(tmp_path/'state.sqlite3', cfg)
    now = g.timestamp('2026-09-17T06:05:00+02:00')
    report = guard.report(now)
    assert report['providers']['anthropic']['coverage'] == 'unavailable'
    assert report['providers']['xai-oauth']['coverage'] == 'unsupported'
    assert len(guard.outbox()) == 1
    guard.report(now+60)
    assert len(guard.outbox()) == 1
    guard.report(now+86400)
    assert len(guard.outbox()) == 2
    guard.close()


def test_real_sampling_jitter_retains_resolution_coverage():
    from agent import usage_guard as g
    rows = [dict(t=t+.25, pp=t/21600*10, reset=604800, grant='synthetic', status='ok') for t in range(0,21601,60)]
    result = g.rolling(rows, 21600.8)
    assert result['coverage'] == 'sampled'
    assert result['end']-result['start'] <= 21600


def test_new_grant_or_reset_is_a_new_incident(tmp_path):
    from agent import usage_guard as g
    cfg = dict(threshold_pp=10, window_seconds=21600, sample_seconds=60,
               timezone='Europe/Zurich', night_start='22:00', night_end='06:00')
    guard = g.Guard(tmp_path/'state.sqlite3', cfg)
    base = dict(status='ok', pp=0, reset=604800, grant='synthetic')
    guard.sample('anthropic', 0, base)
    guard.sample('anthropic', 60, dict(base, pp=11))
    assert len(guard.outbox()) == 1
    guard.sample('anthropic', 120, dict(base, reset=1209600))
    guard.sample('anthropic', 180, dict(base, pp=11, reset=1209600))
    assert len(guard.outbox()) == 2
    guard.sample('anthropic', 240, dict(base, grant='new-synthetic-account'))
    guard.sample('anthropic', 300, dict(base, pp=12, grant='new-synthetic-account'))
    assert len(guard.outbox()) == 3
    guard.close()


def test_report_includes_zurich_0100_to_0700_peak(tmp_path):
    import json
    from agent import usage_guard as g
    cfg = dict(threshold_pp=10, window_seconds=21600, sample_seconds=60,
               timezone='Europe/Zurich', night_start='22:00', night_end='06:00')
    guard = g.Guard(tmp_path/'state.sqlite3', cfg)
    at = g.timestamp('2026-09-18T06:05:00+02:00')
    start = g.timestamp('2026-09-17T01:00:00+02:00')
    samples = [('anthropic', t, json.dumps(dict(status='ok', grant='synthetic', reset=at+604800,
                 pp=max(0,min(12,(t-start)/1800))))) for t in range(int(at)-108000,int(at)+1,60)]
    with guard.db:
        guard.db.executemany('INSERT INTO samples VALUES(?,?,?)', samples)
    report = guard.report(at)['providers']['anthropic']
    assert report['peak_pp'] == 12
    assert report['start'] == '2026-09-17T01:00:00+02:00'
    assert report['end'] == '2026-09-17T07:00:00+02:00'
    assert report['coverage'] == 'sampled'
    guard.close()
