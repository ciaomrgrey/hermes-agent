"""Read-only checkers and bounded auxiliary extraction."""
import importlib.util
from pathlib import Path
import sqlite3
import sys
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "completion-gate"


def module(name):
    path = PLUGIN / f"{name}.py"
    assert path.exists(), f"missing {name} implementation"
    spec = importlib.util.spec_from_file_location(f"cg_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_checkers_read_real_artefacts_and_refuse_execution(tmp_path):
    checks = module("checks")
    target = tmp_path / "f"
    target.write_text("nonempty")
    assert checks.check("file", str(target))[0] == "reproduced"
    assert checks.check("file", str(tmp_path / "missing"))[0] == "failed"
    cfg = tmp_path / "config.json"
    cfg.write_text('{"agent":{"enabled":true}}')
    assert checks.check("config", {"path": str(cfg), "key": "agent.enabled", "expected": True})[0] == "reproduced"
    dbpath = tmp_path / "records.db"
    with sqlite3.connect(dbpath) as db:
        db.execute("CREATE TABLE tasks(id TEXT,status TEXT)")
        db.execute("INSERT INTO tasks VALUES('t_123','done')")
        db.execute("CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,tool_name TEXT,content TEXT)")
        db.execute("INSERT INTO messages VALUES(1,'s','tool','terminal',?)", (json.dumps({"exit_code": 0}),))
    ref = {"db_path": str(dbpath), "table": "tasks", "where": {"id": "t_123", "status": "done"}}
    assert checks.check("sqlite", ref)[0] == "reproduced"
    assert checks.check("kanban", ref)[0] == "reproduced"
    ref["where"]["status"] = "running"
    assert checks.check("kanban", ref)[0] == "failed"
    ref["table"] = "tasks; DROP TABLE tasks"
    assert checks.check("sqlite", ref)[0] == "unverified"
    assert checks.check("command_exit", {"db_path": str(dbpath), "session_id": "s", "message_id": 1, "expected": 0})[0] == "reproduced"
    assert checks.check("command_exit", {"command": f"touch {tmp_path / 'BAD'}"})[0] == "unverified"
    assert not (tmp_path / "BAD").exists()
    assert checks.check("agent_report", "done")[0] == "unverified"


def test_endpoint_and_hard_deadline(tmp_path):
    checks = module("checks")
    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.send_response(204)
            self.end_headers()
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/"
        assert checks.check("http", {"url": url, "status": 204})[0] == "reproduced"
        assert checks.check("socket", {"host": "127.0.0.1", "port": server.server_port})[0] == "reproduced"
        assert checks.bounded_check({"artefact_kind": "http", "artefact_ref": {"url": url, "status": 200}}, timeout=2)[0] == "failed"
        assert checks.bounded_check({"artefact_kind": "file", "artefact_ref": str(tmp_path)}, timeout=0.001)[0] == "unverified"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_extraction_strict_json_and_answer_only(monkeypatch):
    extraction = module("extraction")
    from agent import auxiliary_client
    calls = []
    def call(**kwargs):
        calls.append(kwargs)
        from types import SimpleNamespace
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='[{"claim":"ready","artefact_kind":"file","artefact_ref":"/tmp/f"}]'))])
    monkeypatch.setattr(auxiliary_client, "call_llm", call)
    assert extraction.extract("only answer", timeout=2, max_claims=10)[0]["claim"] == "ready"
    assert calls[0]["task"] == "completion_gate"
    assert calls[0]["messages"][-1] == {"role": "user", "content": "only answer"}
    assert len(calls[0]["messages"]) == 2
    assert not calls[0].get("tools")
    for raw in ('```json\n[]\n```', '{}', '[{"claim":"x"}]', '[{"claim":"x","artefact_kind":"file","artefact_ref":"f","extra":1}]', '[{"claim":"x","claim":"y","artefact_kind":"file","artefact_ref":"f"}]', '[{"claim":"x","artefact_kind":"config","artefact_ref":{"expected":NaN}}]'):
        with pytest.raises(ValueError):
            extraction.parse_claims(raw, max_claims=10)
