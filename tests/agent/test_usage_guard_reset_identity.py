"""Live-shaped timestamps, synthetic utilization; no provider or delivery calls."""
import pytest

from agent.usage_guard import Guard, parse_weekly, rolling


@pytest.mark.parametrize('first_reset,next_reset', [
    (1789772400.24, 1789772399.9),
    (1789772399.9, 1789772400.24),
])
def test_reset_jitter_preserves_window_breach_and_incident(tmp_path, first_reset, next_reset):
    cfg = dict(threshold_pp=10, window_seconds=21600, sample_seconds=60,
               timezone='Europe/Zurich', night_start='22:00', night_end='06:00')
    def value(pp, reset):
        return dict(parse_weekly('anthropic', {'seven_day': {
            'utilization': pp, 'resets_at': reset}}), grant='synthetic')
    # Read-side tolerance also covers already-persisted, unnormalized history.
    rows = [dict(value(11*t/21600, first_reset if t < 10800 else next_reset), t=t)
            for t in range(0, 21601, 60)]
    result = rolling(rows, 21600)
    assert (result['pp'], result['coverage']) == (11, 'sampled')
    path = tmp_path/'guard.sqlite3'
    guard = Guard(path, cfg)
    for row in rows:
        guard.sample('anthropic', row['t'], {k: v for k, v in row.items() if k != 't'})
    original = guard.outbox()
    assert len(original) == 1
    guard.close()
    guard = Guard(path, cfg)
    # Restart and either-direction jitter cannot generate duplicate originals.
    for t, reset in [(21660, first_reset), (21720, next_reset)]:
        result = guard.sample('anthropic', t, value(12, reset))
        assert result is not None and result['coverage'] == 'sampled'
        assert guard.outbox() == original
    saved = guard.rows('anthropic')
    assert saved[-1]['reset'] == saved[0]['reset']
    assert saved[-1]['raw_reset'] == next_reset
    # A genuine week change must not bridge utilization, and creates a new incident.
    reset = next_reset + 604800
    result = guard.sample('anthropic', 21780, value(12, reset))
    assert result is not None
    assert result['coverage'] == 'partial' and result['pp'] is None
    guard.sample('anthropic', 21840, value(23, reset))
    assert len(guard.outbox()) == 2
    guard.close()


def test_reset_tolerance_requires_same_identity_and_fixed_anchor():
    reset = 1789772400
    base = dict(status='ok', pp=0, reset=reset, duration=604800,
                field='seven_day', grant='synthetic', t=0)
    for changed in [dict(reset=reset+61), dict(duration=18000),
                    dict(field='five_hour'), dict(grant='other')]:
        result = rolling([base, dict(base, t=60, pp=11, **changed)], 60)
        assert result['coverage'] == 'partial' and result['pp'] is None
    # Pairwise tolerance must not chain into a moving window identity.
    rows = [base, dict(base, t=60, pp=1, reset=reset+60),
            dict(base, t=120, pp=11, reset=reset+120)]
    assert rolling(rows[:2], 60)['pp'] == 1
    assert rolling(rows, 120)['pp'] is None
