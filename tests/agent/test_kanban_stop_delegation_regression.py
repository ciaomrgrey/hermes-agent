"""Stdlib regression coverage for delegated Kanban stop-guard isolation.

Kept runnable with ``unittest`` so this lifecycle regression remains executable
in installations whose approved runtime does not include pytest.
"""

from __future__ import annotations

import os
import threading
import unittest
from unittest.mock import patch

from agent.delegation_context import delegated_child_context
from agent.kanban_stop import build_kanban_stop_nudge, kanban_stop_nudge_enabled


_SENTINEL_REPORT = "SENTINEL_SOURCED_CATCH_UP_REPORT"


class KanbanStopDelegationRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(
            os.environ,
            {
                "HERMES_KANBAN_TASK": "t_parent",
                "HERMES_KANBAN_STOP_NUDGE": "1",
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.delegated_marker = os.environ.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
        self.addCleanup(self._restore_delegated_marker)

    def _restore_delegated_marker(self) -> None:
        if self.delegated_marker is None:
            os.environ.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
        else:
            os.environ["HERMES_DELEGATED_CHILD_CONTEXT"] = self.delegated_marker

    def test_parent_without_terminal_transition_still_receives_guard(self) -> None:
        self.assertTrue(kanban_stop_nudge_enabled())
        nudge = build_kanban_stop_nudge(messages=[])
        self.assertIsNotNone(nudge)
        self.assertIn("kanban_complete", nudge or "")

        completed = [{"role": "tool", "name": "kanban_complete", "content": "done"}]
        self.assertIsNone(build_kanban_stop_nudge(messages=completed))

    def test_real_child_returns_exact_report_while_parent_guard_remains_active(self) -> None:
        from tools import delegate_tool

        child_started = threading.Event()
        parent_checked = threading.Event()
        observations: dict[str, object] = {}

        class Parent:
            _current_task_id = "t_parent"

            def _touch_activity(self, _description: str) -> None:
                observations["parent_guard"] = kanban_stop_nudge_enabled()
                observations["parent_task"] = os.environ.get("HERMES_KANBAN_TASK")
                parent_checked.set()

        class Child:
            tool_progress_callback = None
            _delegate_saved_tool_names: list[str] = []
            _credential_pool = None
            _subagent_id = "sa-stop-guard-regression"
            _delegate_depth = 1
            _parent_subagent_id = None
            session_id = "child-stop-guard-regression"
            model = "test-model"
            session_prompt_tokens = 0
            session_completion_tokens = 0
            session_estimated_cost_usd = 0.0
            session_reasoning_tokens = 0

            def get_activity_summary(self) -> dict[str, object]:
                return {
                    "api_call_count": 0,
                    "max_iterations": 1,
                    "current_tool": None,
                    "last_activity_ts": None,
                }

            def run_conversation(self, **_kwargs: object) -> dict[str, object]:
                observations["child_guard"] = kanban_stop_nudge_enabled()
                observations["child_task"] = os.environ.get("HERMES_KANBAN_TASK")
                child_started.set()
                observations["parent_checked"] = parent_checked.wait(1.0)
                nudge = build_kanban_stop_nudge(messages=[])
                return {
                    "final_response": nudge or _SENTINEL_REPORT,
                    "completed": True,
                    "api_calls": 0,
                    "messages": [],
                }

            def close(self) -> None:
                return None

        with patch.object(delegate_tool, "_HEARTBEAT_INTERVAL", 0.01):
            result = delegate_tool._run_single_child(
                0,
                "return a sourced catch-up report",
                Child(),
                Parent(),
            )

        self.assertTrue(child_started.is_set())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["summary"], _SENTINEL_REPORT)
        self.assertEqual(observations["child_guard"], False)
        self.assertEqual(observations["parent_guard"], True)
        self.assertEqual(observations["parent_checked"], True)
        self.assertEqual(observations["child_task"], "t_parent")
        self.assertEqual(observations["parent_task"], "t_parent")
        self.assertEqual(os.environ["HERMES_KANBAN_TASK"], "t_parent")
        self.assertTrue(kanban_stop_nudge_enabled())


if __name__ == "__main__":
    unittest.main()
