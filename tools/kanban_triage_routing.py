"""Default-off, exact-cron routing of never-held Kanban intake.

Runtime dispatch IDs, not model arguments, identify the caller. This is not a
hold-release or reviewer-disposition API. Existing specify/promote APIs are not
wrapped: they admit held records and perform readiness in separate transactions.
"""
from __future__ import annotations

import re

from tools.registry import no_cache_check_fn


def _binding():
    from hermes_cli.config import load_config
    from hermes_cli.profiles import get_active_profile_name
    from gateway.session_context import get_session_env
    from tools.kanban_tools import _check_kanban_orchestrator_mode

    if not _check_kanban_orchestrator_mode() or get_session_env('HERMES_CRON_SESSION') != '1':
        raise ValueError('triage routing requires an authorized cron context')
    binding = load_config().get('kanban', {}).get('triage_routing')
    if not isinstance(binding, dict) or set(binding) != {'profile', 'cron_job_id', 'board'}:
        raise ValueError('triage routing has no exact profile/job/board binding')
    if binding['profile'] != get_active_profile_name():
        raise ValueError('triage routing profile mismatch')
    if not all(isinstance(v, str) and v and v == v.strip() for v in binding.values()):
        raise ValueError('invalid triage routing binding')
    import os
    if os.environ.get('HERMES_KANBAN_DB'):
        raise ValueError('triage routing refuses ambient database overrides')
    if os.environ.get('HERMES_KANBAN_BOARD', binding['board']) != binding['board']:
        raise ValueError('triage routing refuses a different bound board')
    from hermes_cli.kanban_db import _normalize_board_slug
    if _normalize_board_slug(binding['board']) != binding['board']:
        raise ValueError('invalid bound board')
    return binding


@no_cache_check_fn
def check_routing_mode():
    try:
        _binding()
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def handle_route(args, **kw):
    from tools.kanban_tools import (
        _reject_delegated_child_mutation, _require_orchestrator_tool, _board,
        _kanban_handler, _redact_opt, _ok,
    )

    @_kanban_handler('kanban_route_triage')
    def run(args):
        _reject_delegated_child_mutation('kanban_route_triage')
        _require_orchestrator_tool('kanban_route_triage')
        binding = _binding()
        # task_id here is the native execution owner, distinct from args.task_id.
        prefix = 'cron:' + binding['cron_job_id'] + ':'
        owner = kw.get('task_id')
        session = kw.get('session_id')
        if not isinstance(owner, str) or not owner.startswith(prefix) or not owner[len(prefix):] or ':' in owner[len(prefix):]:
            raise ValueError('triage routing cron execution mismatch')
        if not isinstance(session, str) or not session:
            raise ValueError('triage routing requires a native session identity')
        if set(args) - {'task_id', 'expected_event_id', 'action', 'specification', 'assignee', 'board'}:
            raise ValueError('unsupported triage routing arguments')
        if args.get('board', binding['board']) != binding['board']:
            raise ValueError('triage routing board mismatch')
        actor = dict(profile=binding['profile'], cron_job_id=binding['cron_job_id'],
                     execution_id=owner[len(prefix):], session_id=session)
        with _board(binding['board']) as (kb, conn):
            result = route_unheld(conn, task_id=args.get('task_id'),
                                  expected_event_id=args.get('expected_event_id'),
                                  action=args.get('action'), actor=actor,
                                  board=binding['board'],
                                  specification=_redact_opt(args.get('specification')),
                                  assignee=args.get('assignee'))
        return _ok(**result)
    return run(args)


# Negative screening only. Text never grants permission, and comments/unknown
# lifecycle events are always ambiguous. False positives must use owner routing.
_RESTRICTION = re.compile(r'\b(park|hold|blocked|needs[_ -]input|account|security|release|approval|permission|review|stop|wait|do not|don.t|must not)\b', re.I)
_ALLOWED_EVENTS = {'created', 'linked', 'dependency_wait', 'triage_routed'}


def route_unheld(conn, *, task_id, expected_event_id, action, actor, board,
                 specification=None, assignee=None):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import write_txn

    if not isinstance(task_id, str) or not re.fullmatch(r't_[0-9a-f]{8}', task_id):
        raise ValueError('exact task_id required')
    if type(expected_event_id) is not int or expected_event_id < 1:
        raise ValueError('positive expected_event_id required from kanban_show')
    if action not in {'specify', 'promote', 'reassign'}:
        raise ValueError('action must be specify, promote or reassign')
    if specification is not None and (not isinstance(specification, str) or not specification.strip()):
        raise ValueError('specification must be nonblank text')
    if action != 'specify' and specification is not None:
        raise ValueError('only specify accepts specification')
    if assignee is not None and (not isinstance(assignee, str) or not assignee or assignee != assignee.strip()):
        raise ValueError('assignee must be an exact nonblank profile')
    if action == 'reassign' and assignee is None:
        raise ValueError('reassign requires assignee')
    with write_txn(conn):
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)
        if not task or not events or max(e.id for e in events) != expected_event_id:
            raise ValueError('unknown task or stale expected_event_id; read back before retry')
        if task.status not in {'triage', 'todo'}:
            raise ValueError('only ordinary unheld triage/todo intake can be routed')
        if (task.block_kind or task.block_recurrences or task.current_run_id or task.started_at
                or task.completed_at or task.result or task.consecutive_failures or task.last_failure_error
                or task.claim_lock or task.worker_pid or task.workflow_template_id or task.current_step_key):
            raise ValueError('held, previously executed or ambiguous task')
        if (events[0].kind != 'created' or any(e.kind not in _ALLOWED_EVENTS for e in events)
                or kb.list_runs(conn, task_id) or kb.list_comments(conn, task_id)):
            raise ValueError('historical hold/review or ambiguous provenance; owner disposition required')
        created = events[0].payload
        if not isinstance(created, dict) or created.get('status') not in {'triage', 'todo'}:
            raise ValueError('ordinary intake origin cannot be established')
        if any(_RESTRICTION.search(text or '') for text in (task.title, task.body, specification)):
            raise ValueError('explicit restriction requires owner disposition')
        if action == 'specify' and task.status != 'triage':
            raise ValueError('specify requires triage')
        if action == 'promote' and task.status != 'todo':
            raise ValueError('promote requires todo')
        owner = assignee if assignee is not None else task.assignee
        if not owner or owner not in kb.list_profiles_on_disk():
            raise ValueError('routing requires an existing exact assignee')
        parents_satisfied = kb._parents_satisfied(conn, task_id)
        if action == 'promote' and not parents_satisfied:
            raise ValueError('unsatisfied parent dependencies')
        status = task.status if action == 'reassign' else ('ready' if parents_satisfied else 'todo')
        body = task.body
        if specification is not None:
            # Append, never replace an existing restriction or owner requirement.
            body = ((body + '\n\n') if body else '') + specification
        prior = dict(status=task.status, assignee=task.assignee)
        conn.execute('UPDATE tasks SET status=?, assignee=?, body=? WHERE id=?',
                     (status, owner, body, task_id))
        landed = dict(status=status, assignee=owner)
        kb._append_event(conn, task_id, 'triage_routed', dict(
            actor=actor, board=board, action=action, task_id=task_id,
            expected_event_id=expected_event_id, prior=prior, result=landed,
            specification_appended=specification is not None,
        ))
        event_id = conn.execute('SELECT MAX(id) FROM task_events WHERE task_id=?', (task_id,)).fetchone()[0]
        return dict(task_id=task_id, board=board, event_id=event_id, **landed)
