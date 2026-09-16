"""Deterministic weekly subscription measurements. Values are percentage points."""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def timestamp(value):
    if isinstance(value, bool):
        raise ValueError('invalid timestamp')
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    if not isinstance(value, str):
        raise ValueError('missing reset')
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('reset timezone missing')
    return dt.timestamp()


def parse_weekly(provider, payload):
    """Whitelist source fields; never retain credentials, dollars or account PII."""
    if provider not in ('anthropic', 'openai-codex'):
        return {'status': 'unsupported', 'reason': 'no equivalent weekly source'}
    try:
        if provider == 'anthropic':
            window = payload.get('seven_day')
            field = 'seven_day'
            pp, reset = window['utilization'], timestamp(window['resets_at'])
            for limit in payload.get('limits', []):
                if limit.get('kind') == 'weekly_all' and limit.get('percent') != pp:
                    raise ValueError('weekly sources disagree')
        else:
            windows = [(k, v) for k, v in payload.get('rate_limit', {}).items()
                       if k in ('primary_window', 'secondary_window') and isinstance(v, dict)
                       and v.get('limit_window_seconds') == 604800]
            if len(windows) != 1:
                raise ValueError('weekly duration missing or ambiguous')
            key, window = windows[0]
            field = 'rate_limit.' + key
            pp, reset = window['used_percent'], timestamp(window['reset_at'])
        if isinstance(pp, bool) or not isinstance(pp, (int, float)) or not math.isfinite(pp) or not 0 <= pp <= 100:
            raise ValueError('invalid percentage')
        return {'status': 'ok', 'pp': float(pp), 'reset': math.floor(reset), 'raw_reset': reset,
                'duration': 604800, 'field': field}
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return {'status': 'unavailable', 'reason': 'missing or invalid weekly fields'}


def rolling(rows, end, window=21600, max_gap=90, resolution=60):
    """No interpolation: delta within the final contiguous monotone grant/reset segment.

    A missing boundary, reset, correction or observation gap yields only a lower
    bound. Even that bound can prove a breach; it can never prove a green window.
    """
    selected = [r for r in rows if end-window <= r['t'] <= end]
    empty = {'pp': None, 'start': None, 'end': end, 'coverage': 'unavailable'}
    if not selected or end-selected[-1]['t'] > max_gap or selected[-1]['status'] != 'ok':
        return empty
    segment = []
    for row in selected:
        if row['status'] != 'ok':
            segment = []
            continue
        if segment:
            prev = segment[-1]
            if (row['grant'] != prev['grant'] or row['reset'] != prev['reset']
                    or row['pp'] < prev['pp'] or row['t']-prev['t'] > max_gap):
                segment = []
        segment.append(row)
    first, last = segment[0], segment[-1]
    if len(segment) < 2:
        return dict(empty, start=first['t'], end=last['t'], coverage='partial')
    span = last['t']-first['t']
    return {'pp': last['pp']-first['pp'], 'start': first['t'], 'end': last['t'],
            'observed_seconds': span, 'sampling_seconds': resolution,
            'coverage': 'sampled' if window-resolution <= span <= window else 'partial'}


class Guard:
    """SQLite transaction is the sample/incident/hold/outbox atomic boundary.

    Delivery is at-least-once: a crash after Telegram accepted but before the
    receipt commits can duplicate the same incident ID. No fictitious exactly-once.
    """
    def __init__(self, path, config):
        self.config = config
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS samples (
                provider TEXT NOT NULL, t REAL NOT NULL, value TEXT NOT NULL,
                PRIMARY KEY(provider,t));
            CREATE TABLE IF NOT EXISTS incidents (
                provider TEXT PRIMARY KEY, id TEXT NOT NULL, epoch TEXT);
            CREATE TABLE IF NOT EXISTS holds (
                provider TEXT NOT NULL, since REAL NOT NULL, incident TEXT NOT NULL,
                account TEXT NOT NULL, PRIMARY KEY(provider,account));
            CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY, body TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                receipt TEXT, last_error TEXT, created REAL NOT NULL DEFAULT 0);
        ''')
        if 'created' not in {r['name'] for r in self.db.execute('PRAGMA table_info(outbox)')}:
            with self.db:
                self.db.execute('ALTER TABLE outbox ADD COLUMN created REAL NOT NULL DEFAULT 0')
        if 'epoch' not in {r['name'] for r in self.db.execute('PRAGMA table_info(incidents)')}:
            with self.db:
                self.db.execute('ALTER TABLE incidents ADD COLUMN epoch TEXT')
        if 'account' not in {r['name'] for r in self.db.execute('PRAGMA table_info(holds)')}:
            with self.db:
                self.db.execute('ALTER TABLE holds RENAME TO legacy_holds')
                self.db.execute('CREATE TABLE holds(provider TEXT NOT NULL, since REAL NOT NULL, '
                                'incident TEXT NOT NULL, account TEXT NOT NULL, PRIMARY KEY(provider,account))')
                self.db.execute("INSERT INTO holds SELECT provider,since,incident,'*' FROM legacy_holds")
                self.db.execute('DROP TABLE legacy_holds')

    def close(self):
        self.db.close()

    def is_night(self, at):
        clock = datetime.fromtimestamp(at, ZoneInfo(self.config['timezone'])).strftime('%H:%M')
        return clock >= self.config['night_start'] or clock < self.config['night_end']

    def rows(self, provider, since=0):
        return [dict(json.loads(r['value']), t=r['t']) for r in self.db.execute(
            'SELECT t,value FROM samples WHERE provider=? AND t>=? ORDER BY t', (provider, since))]

    def sample(self, provider, at, value):
        window = self.config['window_seconds']
        with self.db:
            # Reject replay/backdating; unchanged timestamps are idempotent, not overwrites.
            last = self.db.execute('SELECT MAX(t) FROM samples WHERE provider=?', (provider,)).fetchone()[0]
            if last is not None and at <= last:
                return None
            self.db.execute('INSERT INTO samples VALUES(?,?,?)', (provider, at, json.dumps(value)))
            epoch = json.dumps([value.get('grant'), value.get('reset')])
            if value.get('status') == 'ok':
                self.db.execute('DELETE FROM incidents WHERE provider=? AND (epoch IS NULL OR epoch!=?)',
                                (provider, epoch))
            result = rolling(self.rows(provider, at-window), at, window,
                             self.config['sample_seconds']*1.5, self.config['sample_seconds'])
            breach = result['pp'] is not None and result['pp'] > self.config['threshold_pp']
            if breach:
                incident = self.db.execute('SELECT id FROM incidents WHERE provider=?', (provider,)).fetchone()
                if incident is None:
                    incident_id = f'breach:{provider}:{at}'
                    self.db.execute('INSERT INTO incidents VALUES(?,?,?)', (provider, incident_id, epoch))
                    body = json.dumps(dict(id=incident_id, provider=provider, window=result,
                                           threshold_pp=self.config['threshold_pp'], sampling_seconds=self.config['sample_seconds']))
                    self.db.execute('INSERT INTO outbox(id,body,created) VALUES(?,?,?)', (incident_id, body, at))
                else:
                    incident_id = incident['id']
                if self.is_night(at):
                    self.db.execute('INSERT OR IGNORE INTO holds VALUES(?,?,?,?)',
                                    (provider, at, incident_id, value.get('account') or '*'))
            elif result['coverage'] == 'sampled':
                self.db.execute('DELETE FROM incidents WHERE provider=?', (provider,))
            self.db.execute('DELETE FROM samples WHERE t<?', (at-self.config.get('retention_days', 7)*86400,))
        return result

    def report(self, at):
        zone = ZoneInfo(self.config['timezone'])
        local = lambda t: datetime.fromtimestamp(t, zone).isoformat() if t is not None else None
        report: dict = dict(id='report:'+datetime.fromtimestamp(at, zone).date().isoformat(),
                      at=local(at), lookback_seconds=86400, sampling_seconds=self.config['sample_seconds'],
                      held=self.holds(), providers={}, external_clients='uncontrolled; shared quota can keep increasing')
        for provider in ('anthropic', 'openai-codex'):
            rows = self.rows(provider, at-86400-self.config['window_seconds'])
            points = [rolling(rows, r['t'], self.config['window_seconds'], self.config['sample_seconds']*1.5,
                              self.config['sample_seconds'])
                      for r in rows if at-86400 <= r['t'] <= at]
            usable = [p for p in points if p['pp'] is not None]
            peak = max(usable, key=lambda p: p['pp']) if usable else None
            full = bool(points and rows[0]['t'] <= at-86400-self.config['window_seconds']
                        and rows[-1]['t'] >= at-self.config['sample_seconds']*1.5
                        and all(p['coverage'] == 'sampled' for p in points))
            report['providers'][provider] = dict(
                coverage='sampled' if full else 'partial' if usable else 'unavailable',
                peak_pp=peak['pp'] if peak else None, start=local(peak['start']) if peak else None,
                end=local(peak['end']) if peak else None, peak_coverage=peak['coverage'] if peak else None)
        report['providers']['xai-oauth'] = dict(coverage='unsupported', peak_pp=None)
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO outbox(id,body,created) VALUES(?,?,?)',
                            (report['id'], json.dumps(report), at))
        return report

    def maintain(self, at):
        """Prune acknowledged history only; never erase an undelivered original."""
        cutoff = at-self.config.get('retention_days',7)*86400
        with self.db:
            self.db.execute('DELETE FROM samples WHERE t<?',(cutoff,))
            self.db.execute('DELETE FROM outbox WHERE receipt IS NOT NULL AND created<?',(cutoff,))

    def storage_status(self):
        pending = self.db.execute('SELECT COUNT(*) FROM outbox WHERE receipt IS NULL').fetchone()[0]
        warning = self.config.get('pending_warning_count',10000)
        return dict(pending=pending, saturated=pending>=warning, pending_warning_count=warning,
                    pending_policy='retain-originals; total-storage-not-bounded-during-indefinite-outage')

    def holds(self):
        return [r[0] for r in self.db.execute('SELECT DISTINCT provider FROM holds ORDER BY provider')]

    def outbox(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM outbox WHERE receipt IS NULL ORDER BY rowid')]

    def deliver(self, send):
        for item in self.outbox():
            try:
                receipt = send(item['body'])
                if not receipt:
                    raise ValueError('delivery receipt missing')
                error = None
            except Exception as exc:
                receipt, error = None, type(exc).__name__  # exception messages may contain credentials
            with self.db:
                self.db.execute('UPDATE outbox SET attempts=attempts+1, receipt=?, last_error=? WHERE id=?',
                                (receipt, error, item['id']))
