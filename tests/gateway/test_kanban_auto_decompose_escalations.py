"""Regression coverage for automatic triage decomposition after block escalation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.kanban_watchers_dispatcher import _KanbanDispatcher
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_decompose


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _escalate_same_block_twice(conn, *, kind: str) -> str:
    task_id = kb.create_task(conn, title=f"{kind} escalation", assignee="worker")
    assert kb.block_task(conn, task_id, reason="still unresolved", kind=kind)
    assert kb.unblock_task(conn, task_id)
    assert kb.block_task(conn, task_id, reason="still unresolved", kind=kind)
    assert kb.get_task(conn, task_id).status == "triage"
    return task_id


@pytest.mark.parametrize("kind", ["capability", "needs_input"])
def test_auto_decompose_candidates_exclude_unresolved_block_loop_escalations(
    kanban_home: Path, kind: str,
) -> None:
    with kbc.connect_closing() as conn:
        escalated_id = _escalate_same_block_twice(conn, kind=kind)
        ordinary_id = kb.create_task(
            conn, title="ordinary triage idea", assignee="worker", triage=True,
        )

    assert kanban_decompose.list_triage_ids() == [escalated_id, ordinary_id]
    assert kanban_decompose.list_auto_decompose_triage_ids() == [ordinary_id]


def test_dispatcher_uses_auto_decompose_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    fake_decomposer = SimpleNamespace(
        list_triage_ids=lambda: ["t_escalated"],
        list_auto_decompose_triage_ids=lambda: [],
        decompose_task=lambda task_id, **kwargs: (
            calls.append(task_id)
            or SimpleNamespace(ok=True, fanout=False, child_ids=[])
        ),
    )
    monkeypatch.setattr(kanban_decompose, "list_triage_ids", fake_decomposer.list_triage_ids)
    monkeypatch.setattr(
        kanban_decompose,
        "list_auto_decompose_triage_ids",
        fake_decomposer.list_auto_decompose_triage_ids,
        raising=False,
    )
    monkeypatch.setattr(kanban_decompose, "decompose_task", fake_decomposer.decompose_task)

    dispatcher = _KanbanDispatcher(kb=SimpleNamespace(), settings=SimpleNamespace())
    monkeypatch.setattr(dispatcher, "_board_slugs", lambda: ["default"])

    assert dispatcher.auto_decompose_tick(3) == 0
    assert calls == []


def test_manual_specification_resolves_escalated_triage(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        task_id = _escalate_same_block_twice(conn, kind="capability")

        assert kb.specify_triage_task(
            conn,
            task_id,
            body="Operator supplied the missing capability.",
            author="operator",
        )
        assert kb.get_task(conn, task_id).status == "ready"
