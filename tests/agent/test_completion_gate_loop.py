"""Exercise the actual agent loop and SQLite transcript, not only gate helpers."""
import json
import pytest
from unittest.mock import Mock

from tests.run_agent.test_81641_text_turn_incremental_persistence import loop_agent  # noqa: F401
from tests.run_agent.test_run_agent import _mock_response
from hermes_cli import plugins


@pytest.mark.parametrize("transform", [False, True])
def test_real_loop_reworks_and_only_persists_fixed_answer(loop_agent, tmp_path, monkeypatch, transform):
    from hermes_state import SessionDB
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = {"plugins": {"enabled": ["completion-gate"], "entries": {"completion-gate": {"settings": {
        "enabled": True, "db_path": str(tmp_path / "gate.db")}}}}}
    (tmp_path / "config.yaml").write_text(json.dumps(cfg))
    manager = plugins.PluginManager()
    manager.discover_and_load()
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    plugin = manager._plugins["completion-gate"].module
    file = tmp_path / "result.txt"
    extracted = []
    def extract(answer, **kw):
        extracted.append(answer)
        return [{"claim": "ready", "artefact_kind": "file", "artefact_ref": str(file)}]
    monkeypatch.setattr(plugin, "bounded_extract", extract)
    suffix = " (transformed)" if transform else ""
    if transform:
        manager._hooks["transform_llm_output"] = [lambda response_text: response_text + suffix]
    a = loop_agent
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(a.session_id, source="cli", model=a.model)
    a._session_db = db
    a._intent_ack_continuation = False
    a._stall_guards = False
    a.stream_delta_callback = Mock()
    a.interim_assistant_callback = Mock()
    turns = []
    def create(**kw):
        turns.append(kw)
        if len(turns) == 1:
            a._fire_stream_delta("file ready, unverified")
            return _mock_response(content="file ready, unverified", finish_reason="stop")
        file.write_text("real artefact")
        return _mock_response(content="file ready, corrected", finish_reason="stop")
    a.client.chat.completions.create.side_effect = create
    try:
        result = a.run_conversation("Create the result file", task_id="completion-proof")
        assert result["final_response"] == "file ready, corrected" + suffix
        assert extracted == ["file ready, unverified" + suffix]
        assert len(turns) == 2
        assert "Completion evidence mismatch" in str(turns[1]["messages"])
        persisted = db.get_messages(a.session_id)
        assert any(m.get("content") == "file ready, corrected" + suffix for m in persisted)
        assert not any("unverified" in (m.get("content") or "") for m in persisted)
        a.stream_delta_callback.assert_not_called()
        a.interim_assistant_callback.assert_not_called()
    finally:
        a._session_db = None
        db.close()
