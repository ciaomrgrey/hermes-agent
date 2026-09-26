"""Real plugin discovery and native live-owner inbound receipts, no estate sends."""
import json

from hermes_cli import plugins




def load_escalation():
    from tests.completion_gate_support import module
    return module('escalation')


def test_shared_discovery_and_hot_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = tmp_path / "config.yaml"
    settings = {"enabled": True, "db_path": str(tmp_path / "gate.db")}
    def save():
        cfg.write_text(json.dumps({"plugins": {"enabled": ["completion-gate"], "entries": {"completion-gate": {"settings": settings}}}}))
    save()
    from tests.completion_gate_support import install
    install(tmp_path)
    manager = plugins.PluginManager()
    manager.discover_and_load()
    assert manager.has_hook("before_turn_end"), "shared plugin not discoverable"
    loaded = manager._plugins["completion-gate"].module
    calls = []
    monkeypatch.setattr(loaded, "bounded_extract", lambda *a, **k: calls.append(a) or [{"claim": "file ready", "artefact_kind": "file", "artefact_ref": str(tmp_path / "missing")}])
    result = manager.invoke_hook("before_turn_end", final_response="file ready", session_id="s", task_id="task", turn_id="one")
    assert any(isinstance(r, dict) and r.get("action") == "block" for r in result)
    settings["enabled"] = False
    save()
    manager.invoke_hook("before_turn_end", final_response="file ready", session_id="s", task_id="task2", turn_id="two")
    assert len(calls) == 1
    import argparse
    cli = next(c for c in manager._cli_commands.values() if c["name"] == "completion-gate")
    parser = argparse.ArgumentParser()
    cli["setup_fn"](parser)
    args = parser.parse_args(["metrics"])
    summary = cli["handler_fn"](args)
    assert summary["blocks"] == 1
    assert summary["per_profile"]


def test_native_inbound_receipt_readback_and_no_owner_is_honest(tmp_path, monkeypatch):
    esc = load_escalation()
    from hermes_cli import profiles
    from tools import bot_live_delivery as live
    monkeypatch.setattr(profiles, "get_profile_dir", lambda profile: tmp_path / profile)
    home = tmp_path / "generalist"
    home.mkdir()
    owner = dict(profile_home=str(home.resolve()), session_id="s", lease_id="lease", live_session_id="live")
    monkeypatch.setattr(live, "find_canonical_live_owner", lambda _: owner)
    event = {"event_id": 1, "target": "generalist", "profile": "cody", "task_id": "task", "reason": "retry_ceiling", "block_count": 2}
    receipt = esc.send(event)
    assert receipt["status"] == "queued"
    actual = live.read_delivery_result(home, receipt["delivery_id"])
    assert actual["message"] == esc.message(event)
    assert actual["status"] == "queued"  # admission is NOT completed work
    monkeypatch.setattr(live, "find_canonical_live_owner", lambda _: None)
    assert esc.send({**event, "event_id": 2})["status"] == "unavailable"
    assert len(list((home / "runtime" / "bot_live_delivery").glob("*.json"))) == 1


def test_aux_extraction_uses_real_native_router_in_bounded_child(tmp_path, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(data)
            content = json.dumps([{"claim": "ready", "artefact_kind": "file", "artefact_ref": str(tmp_path / "f")}])
            payload = {"id": "local-proof", "object": "chat.completion", "created": 1, "model": "local-test",
                       "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(json.dumps({"auxiliary": {"completion_gate": {
        "provider": "custom", "model": "local-test", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}}}))
    try:
        from tests.completion_gate_support import module
        mod = module('extraction')
        claims = mod.bounded_extract("Only the already-written answer.", timeout=10)
        assert claims[0]["artefact_kind"] == "file"
        assert requests[0]["messages"][-1]["content"] == "Only the already-written answer."
        assert len(requests[0]["messages"]) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
