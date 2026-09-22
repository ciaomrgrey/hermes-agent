"""Exact-operation owner admissions for native cron intake routing.

No hold adjudication or prose policy. Typed audit events are the admission ledger:
only its latest event can be consumed, atomically with the task update. Model
arguments cannot supply actor identity, mint authority, or an eligibility flag.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

from tools.registry import no_cache_check_fn


def _binding(mint=False):
    from hermes_cli.config import load_config
    from hermes_cli.profiles import get_active_profile_name
    from gateway.session_context import get_session_env
    from tools.kanban_tools import _check_kanban_orchestrator_mode
    from hermes_cli.kanban_db import _normalize_board_slug

    if not _check_kanban_orchestrator_mode() or os.environ.get('HERMES_KANBAN_TASK'):
        raise ValueError('triage routing requires a configured non-worker orchestrator')
    binding = load_config().get('kanban', {}).get('triage_routing')
    keys = {'conductor_profile', 'profile', 'cron_job_id', 'board'}
    if not isinstance(binding, dict) or set(binding) != keys:
        raise ValueError('no exact conductor/router/job/board binding')
    if not all(isinstance(v, str) and v and v == v.strip() for v in binding.values()):
        raise ValueError('invalid triage routing binding')
    if binding['profile'] == binding['conductor_profile']:
        raise ValueError('conductor and scheduled router must be distinct')
    profile = binding['conductor_profile' if mint else 'profile']
    if get_active_profile_name() != profile:
        raise ValueError('triage routing profile mismatch')
    cron = get_session_env('HERMES_CRON_SESSION') == '1'
    if cron == mint:
        raise ValueError('mint requires conductor context; consume requires cron context')
    if os.environ.get('HERMES_KANBAN_DB'):
        raise ValueError('triage routing refuses ambient database overrides')
    if os.environ.get('HERMES_KANBAN_BOARD', binding['board']) != binding['board']:
        raise ValueError('triage routing refuses a different bound board')
    if _normalize_board_slug(binding['board']) != binding['board']:
        raise ValueError('invalid bound board')
    return binding


@no_cache_check_fn
def check_routing_mode():
    return _available(False)


@no_cache_check_fn
def check_admission_mode():
    return _available(True)


def _available(mint):
    try:
        _binding(mint)
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _actor(binding, kw, mint):
    session, owner = kw.get('session_id'), kw.get('task_id')
    if not isinstance(session, str) or not session:
        raise ValueError('native session identity required')
    if not isinstance(owner, str) or not owner:
        raise ValueError('native execution owner required')
    if mint:
        if owner.startswith('cron:'):
            raise ValueError('scheduled callers cannot mint admissions')
        return dict(profile=binding['conductor_profile'], session_id=session, execution_owner=owner)
    prefix = 'cron:' + binding['cron_job_id'] + ':'
    if not owner.startswith(prefix) or not owner[len(prefix):] or ':' in owner[len(prefix):]:
        raise ValueError('triage routing cron execution mismatch')
    return dict(profile=binding['profile'], cron_job_id=binding['cron_job_id'],
                execution_id=owner[len(prefix):], session_id=session)


def handle_admit(args, **kw):
    return _handle(args, kw, mint=True)


def handle_route(args, **kw):
    return _handle(args, kw, mint=False)


def _handle(args, kw, *, mint):
    from tools.kanban_tools import (
        _reject_delegated_child_mutation, _require_orchestrator_tool,
        _board, _kanban_handler, _redact_opt, _ok,
    )
    name = 'kanban_admit_triage' if mint else 'kanban_route_triage'

    @_kanban_handler(name)
    def run(args):
        _reject_delegated_child_mutation(name)
        _require_orchestrator_tool(name)
        binding = _binding(mint)
        actor = _actor(binding, kw, mint)
        allowed = {'task_id', 'expected_event_id', 'action', 'specification', 'assignee', 'board'}
        if mint:
            allowed |= {'owner_disposition_ref', 'expires_at'}
        if set(args) - allowed:
            raise ValueError('unsupported triage routing arguments')
        if args.get('board', binding['board']) != binding['board']:
            raise ValueError('triage routing board mismatch')
        tid, revision = args.get('task_id'), args.get('expected_event_id')
        if not isinstance(tid, str) or not re.fullmatch(r't_[0-9a-f]{8}', tid):
            raise ValueError('exact task_id required')
        if type(revision) is not int or revision < 1:
            raise ValueError('positive expected_event_id required from native readback')
        operation = {k: args.get(k) for k in ('action', 'assignee', 'specification')}
        _validate_operation(operation)
        operation['specification'] = _redact_opt(operation['specification'])
        disposition = args.get('owner_disposition_ref') if mint else None
        expires_at = args.get('expires_at') if mint else None
        historical = disposition is not None or expires_at is not None
        if mint and historical:
            _validate_disposition(disposition, expires_at)
        with _board(binding['board']) as (kb, conn):
            with kb.write_txn(conn):
                if not mint:
                    current_events = sorted(kb.list_events(conn, tid), key=lambda event: event.id)
                    grant = current_events[-1].payload if current_events else None
                    historical = bool(isinstance(grant, dict) and grant.get('historical'))
                    disposition = grant.get('owner_disposition_ref') if historical else None
                    expires_at = grant.get('expires_at') if historical else None
                    if historical:
                        _validate_disposition(disposition, expires_at)
                task, events = _eligible(kb, conn, tid, revision, historical=historical)
                owner = operation['assignee'] or task.assignee
                if not owner or owner not in kb.list_profiles_on_disk():
                    raise ValueError('routing requires an existing exact assignee')
                action = operation['action']
                if historical and action == 'reassign':
                    raise ValueError('historical owner admission does not support reassign')
                required = {'specify': 'triage', 'promote': 'todo'}
                if action in required and task.status != required[action]:
                    raise ValueError('action is invalid in current phase')
                parents_satisfied = kb._parents_satisfied(conn, tid)
                if action == 'promote' and not parents_satisfied:
                    raise ValueError('unsatisfied parent dependencies')
                snapshot = _snapshot(conn, tid)
                payload = dict(version=1, task_id=tid, binding=binding, operation=operation,
                               snapshot=snapshot, actor=actor, expected_event_id=revision)
                if historical:
                    payload.update(version=2, historical=True,
                                   owner_disposition_ref=disposition, expires_at=expires_at)
                if mint:
                    kb._append_event(conn, tid, 'triage_admitted', payload)
                else:
                    admission = events[-1]
                    grant = admission.payload
                    if (admission.kind != 'triage_admitted' or not isinstance(grant, dict)
                            or any(grant.get(k) != payload[k] for k in
                                   ('version', 'task_id', 'binding', 'operation', 'snapshot'))
                            or grant.get('actor', {}).get('profile') != binding['conductor_profile']):
                        raise ValueError('missing, stale, consumed or mismatched owner admission')
                    status = task.status if action == 'reassign' else ('ready' if parents_satisfied else 'todo')
                    body = task.body
                    if operation['specification'] is not None:
                        body = ((body + '\n\n') if body else '') + operation['specification']
                    if historical:
                        conn.execute(
                            'UPDATE tasks SET status=?,assignee=?,body=?,started_at=NULL,'
                            'block_kind=NULL,block_recurrences=0,consecutive_failures=0,'
                            'last_failure_error=NULL,claim_lock=NULL,claim_expires=NULL,worker_pid=NULL,'
                            'worker_started_at=NULL,current_run_id=NULL WHERE id=?',
                            (status, owner, body, tid),
                        )
                    else:
                        conn.execute('UPDATE tasks SET status=?, assignee=?, body=? WHERE id=?',
                                     (status, owner, body, tid))
                    payload.update(admission_event_id=admission.id,
                                   prior=dict(status=task.status, assignee=task.assignee),
                                   result=dict(status=status, assignee=owner))
                    kb._append_event(conn, tid, 'triage_routed', payload)
                event_id = conn.execute('SELECT MAX(id) FROM task_events WHERE task_id=?', (tid,)).fetchone()[0]
                landed = kb.get_task(conn, tid)
                result = dict(task_id=tid, board=binding['board'], event_id=event_id,
                              status=landed.status, assignee=landed.assignee)
        return _ok(**result)
    return run(args)


def _validate_operation(operation):
    action, assignee, spec = (operation[k] for k in ('action', 'assignee', 'specification'))
    if action not in {'specify', 'promote', 'reassign'}:
        raise ValueError('action must be specify, promote or reassign')
    if spec is not None and (not isinstance(spec, str) or not spec.strip() or action != 'specify'):
        raise ValueError('only specify accepts nonblank specification text')
    if assignee is not None and (not isinstance(assignee, str) or not assignee or assignee != assignee.strip()):
        raise ValueError('assignee must be an exact nonblank profile')
    if action == 'reassign' and assignee is None:
        raise ValueError('reassign requires assignee')


def _validate_disposition(reference, expires_at):
    if (not isinstance(reference, str) or not reference.strip()
            or reference != reference.strip() or len(reference) > 500):
        raise ValueError('exact durable owner disposition reference required')
    now = int(time.time())
    if type(expires_at) is not int or expires_at <= now or expires_at > now + 3600:
        raise ValueError('owner disposition expiry must be within the next hour')


_ALLOWED_EVENTS = {'created', 'linked', 'dependency_wait', 'triage_admitted', 'triage_routed'}
_HISTORICAL_EVENTS = _ALLOWED_EVENTS | {
    'commented', 'specified', 'promoted', 'claimed', 'spawned', 'heartbeat', 'attached',
    'blocked', 'unblocked', 'block_loop_detected', 'unlinked',
}


def _canonical_capability_history(events, runs, limit):
    """Validate the one native capability-loop shape this admission supports."""
    passive = {'commented', 'attached', 'linked', 'unlinked'}
    preparation = {'specified', 'promoted', 'dependency_wait'}

    def valid_passive(kind, payload):
        if not isinstance(payload, dict):
            return False
        if kind == 'commented':
            return (isinstance(payload.get('author'), str) and bool(payload['author'].strip())
                    and type(payload.get('len')) is int and payload['len'] >= 0)
        if kind == 'attached':
            by = payload.get('by')
            return (isinstance(payload.get('filename'), str) and bool(payload['filename'].strip())
                    and type(payload.get('size')) is int and payload['size'] >= 0
                    and (by is None or isinstance(by, str)))
        return all(isinstance(payload.get(key), str)
                   and re.fullmatch(r't_[0-9a-f]{8}', payload[key])
                   for key in ('parent', 'child'))

    def valid_preparation(kind, payload):
        if kind in {'specified', 'promoted'}:
            return payload is None
        return (isinstance(payload, dict)
                and isinstance(payload.get('reason'), str) and bool(payload['reason'].strip())
                and isinstance(payload.get('parent'), str)
                and bool(re.fullmatch(r't_[0-9a-f]{8}', payload['parent'])))

    ordered_runs = sorted(runs, key=lambda run: run.id)
    if len(ordered_runs) != limit:
        return False
    if any(run.ended_at is None or run.status != 'blocked' or run.outcome != 'blocked'
           for run in ordered_runs):
        return False
    run_index = 0
    phase = 'waiting'
    active_run = None
    admitted = False
    for event in events[1:]:
        kind, payload = event.kind, event.payload
        if kind in passive:
            if event.run_id is not None or not valid_passive(kind, payload):
                return False
            continue
        if kind in preparation:
            if (phase != 'waiting' or event.run_id is not None
                    or not valid_preparation(kind, payload)):
                return False
            continue
        if kind == 'claimed':
            if phase != 'waiting' or run_index >= limit or not isinstance(payload, dict):
                return False
            run = ordered_runs[run_index]
            if (type(payload.get('run_id')) is not int or payload.get('run_id') != run.id
                    or event.run_id != run.id
                    or not isinstance(payload.get('lock'), str) or not payload.get('lock')
                    or type(payload.get('expires')) is not int
                    or payload.get('source_status') not in (None, 'ready')):
                return False
            active_run = run.id
            phase = 'running'
            continue
        if kind == 'spawned':
            if (phase != 'running' or event.run_id != active_run
                    or not isinstance(payload, dict) or type(payload.get('pid')) is not int
                    or payload.get('pid') < 1 or not isinstance(payload.get('started_at'), str)
                    or not payload.get('started_at')):
                return False
            continue
        if kind == 'heartbeat':
            if (phase != 'running' or event.run_id != active_run
                    or (payload is not None and (
                        not isinstance(payload, dict) or set(payload) != {'note'}
                        or not isinstance(payload.get('note'), str) or not payload['note'].strip()
                    ))):
                return False
            continue
        if kind in {'blocked', 'block_loop_detected'}:
            expected_kind = 'block_loop_detected' if run_index == limit - 1 else 'blocked'
            if (phase != 'running' or kind != expected_kind or event.run_id != active_run
                    or not isinstance(payload, dict)
                    or payload.get('kind') != 'capability'
                    or payload.get('recurrences') != run_index + 1
                    or payload.get('source_status') != 'ready'
                    or not isinstance(payload.get('reason'), str) or not payload.get('reason').strip()
                    or (kind == 'block_loop_detected' and payload.get('limit') != limit)
                    or (kind == 'blocked' and 'limit' in payload)):
                return False
            phase = 'terminal' if kind == 'block_loop_detected' else 'blocked'
            active_run = None
            continue
        if kind == 'unblocked':
            if phase != 'blocked' or event.run_id is not None:
                return False
            if payload is not None and (
                    not isinstance(payload, dict)
                    or payload.get('status') not in {'ready', 'todo'}
                    or payload.get('resume_status') != 'ready'):
                return False
            run_index += 1
            phase = 'waiting'
            continue
        if kind == 'triage_admitted':
            if (phase != 'terminal' or admitted or event is not events[-1]
                    or event.run_id is not None or not isinstance(payload, dict)
                    or payload.get('historical') is not True):
                return False
            admitted = True
            continue
        return False
    return phase == 'terminal' and run_index == limit - 1


def _eligible(kb, conn, tid, revision, *, historical=False):
    task = kb.get_task(conn, tid)
    events = sorted(kb.list_events(conn, tid), key=lambda event: event.id)
    if not task or not events or events[-1].id != revision:
        raise ValueError('unknown task or stale expected_event_id')
    if task.status not in ({'triage'} if historical else {'triage', 'todo'}):
        raise ValueError('only ordinary triage/todo intake can be routed')
    if (task.current_run_id or task.completed_at or task.result or task.consecutive_failures or task.last_failure_error
            or task.claim_lock or task.claim_expires or task.worker_pid
            or task.workflow_template_id or task.current_step_key):
        raise ValueError('held, previously executed or ambiguous task')
    runs = kb.list_runs(conn, tid)
    if historical:
        from hermes_cli.kanban_db import BLOCK_RECURRENCE_LIMIT
        if (task.block_kind != 'capability' or task.block_recurrences != BLOCK_RECURRENCE_LIMIT
                or not task.started_at or any(e.kind not in _HISTORICAL_EVENTS for e in events)
                or not _canonical_capability_history(events, runs, BLOCK_RECURRENCE_LIMIT)
                or not kb._parents_satisfied(conn, tid)):
            raise ValueError('historical owner disposition does not match a safely ended capability record')
    elif (task.block_kind or task.block_recurrences or task.started_at
            or any(e.kind not in _ALLOWED_EVENTS for e in events) or runs or kb.list_comments(conn, tid)):
        raise ValueError('historical hold/review or ambiguous provenance')
    if events[0].kind != 'created':
        raise ValueError('historical hold/review or ambiguous provenance')
    created = events[0].payload
    if not isinstance(created, dict) or created.get('status') not in {'triage', 'todo'}:
        raise ValueError('ordinary intake origin cannot be established')
    return task, events


def _snapshot(conn, tid):
    # Full rows catch native edits which do not emit events. Dependency state is
    # included: parent completion/owner/hold changes invalidate prior admission.
    task = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
    parents = [dict(r) for r in conn.execute(
        'SELECT t.* FROM tasks t JOIN task_links l ON t.id=l.parent_id WHERE l.child_id=? ORDER BY t.id', (tid,))]
    return hashlib.sha256(json.dumps(dict(task=task, parents=parents), sort_keys=True).encode()).hexdigest()
