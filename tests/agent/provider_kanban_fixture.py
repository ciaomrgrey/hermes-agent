"""Actual native Kanban worker spawn against disposable task/control/session DBs."""
import json
import os
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli.kanban_db_dispatch import _default_spawn
from hermes_constants import get_hermes_home


def main():
    home = get_hermes_home()
    os.environ['HERMES_KANBAN_DB'] = str(home/'fixture-kanban.db')
    os.environ['HERMES_KANBAN_WORKSPACES_ROOT'] = str(home/'workspaces')
    os.environ['HERMES_KANBAN_BOARD'] = 'default'
    # Native documented module fallback, deliberately not the live PATH launcher.
    os.environ['HERMES_BIN'] = 'missing-synthetic-hermes-launcher'
    root = Path(__file__).resolve().parents[2]
    with connect_closing() as conn:
        task_id = kb.create_task(conn, title='Synthetic hold qualification',
            body=os.environ['FIXTURE_QUERY'], assignee='default',
            workspace_kind='dir', workspace_path=str(root), completion_contract='local-only')
        task = kb.get_task(conn, task_id)
        assert task is not None
        before = task.status
        pid = _default_spawn(task, str(root), board='default')
        assert pid is not None
        _, status = os.waitpid(pid, 0)
        log = kb.worker_logs_dir(board='default')/f'{task_id}.log'
        print(log.read_text())
        assert os.waitstatus_to_exitcode(status) == 1
        after = kb.get_task(conn, task_id)
        assert after is not None and after.status == before
        assert after.completed_at is None
        assert after.body == os.environ['FIXTURE_QUERY']
        print(json.dumps({'task_id':task_id, 'state_preserved':True, 'status':after.status}))


if __name__ == '__main__':
    main()
