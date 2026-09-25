"""Behavior tests for the external domain-code-policy plugin."""
from __future__ import annotations

import importlib.util
import hashlib
import json
import sys
import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = ROOT / "external" / "domain-code-policy"


def load_policy():
    namespace = "hermes_plugins.domain_code_policy"
    parent = sys.modules.setdefault("hermes_plugins", types.ModuleType("hermes_plugins"))
    parent.__path__ = []
    package = sys.modules.get(namespace)
    if package is None:
        package = types.ModuleType(namespace)
        package.__path__ = [str(PLUGIN_DIR)]
        sys.modules[namespace] = package
    spec = importlib.util.spec_from_file_location(
        f"{namespace}.policy", PLUGIN_DIR / "policy.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_plugin():
    namespace = "hermes_plugins.domain_code_policy"
    sys.modules.pop(namespace, None)
    spec = importlib.util.spec_from_file_location(
        namespace, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[namespace] = module
    spec.loader.exec_module(module)
    return module


class FakeContext:
    def __init__(self, settings, profile_name="emma"):
        self.settings = settings
        self.profile_name = profile_name
        self.hook = None

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_hook(self, name, callback):
        assert name == "pre_tool_call"
        self.hook = callback


def test_python_state_mutation_is_substantive():
    policy = load_policy()
    source = "conn.execute('DELETE FROM sends WHERE draft_row=240')\nconn.commit()\n"

    verdict = policy.classify_source("retract_240.py", source)

    assert verdict.kind == "substantive"
    assert verdict.reason == "state-mutating source"


def test_control_flow_and_data_transformation_is_substantive():
    policy = load_policy()
    source = "def transform(items):\n    return [item.strip() for item in items if item]\n"

    verdict = policy.classify_source("transform.py", source)

    assert verdict.kind == "substantive"
    assert verdict.reason == "substantive control flow and logic"


@pytest.mark.parametrize(
    "fixture_name", ["build_and_lint.py", "verify_ent_state.py", "retract_240.py"],
)
def test_recorded_domain_fixtures_are_substantive(fixture_name):
    policy = load_policy()
    fixture = ROOT / "tests" / "fixtures" / "domain_code_policy" / fixture_name

    assert policy.classify_source(fixture.name, fixture.read_text()).kind == "substantive"


@pytest.mark.parametrize(
    "source",
    [
        "print('ok')\n", "value = 1\n", "import json\n", "items.append(value)\n",
        "text.replace('a', 'b')\n", "payload.update(other)\n",
    ],
)
def test_trivial_python_controls_pass(source):
    assert load_policy().classify_source("probe.py", source).kind == "trivial"


def test_javascript_control_flow_and_api_logic_is_substantive():
    source = "for (const item of items) { await api.update(item); }\n"
    assert load_policy().classify_source("worker.js", source).kind == "substantive"


def test_unparseable_code_stays_unresolved():
    verdict = load_policy().classify_source("broken.py", "def nope(:\n")
    assert verdict.kind == "unresolved"
    assert verdict.reason == "source could not be classified"


@pytest.mark.parametrize(
    "source",
    [
        "Path('x').write_text('data')\n",
        "os.remove('x')\n",
        "requests.post(url, json=data)\n",
        "subprocess.run(['git', 'commit', '-m', 'x'])\n",
        "subprocess.run(command)\n",
        "cursor.execute(query)\n",
        "api.delete(record_id)\n",
        "client.update(record)\n",
        "open('x', 'w').write('data')\n",
    ],
)
def test_state_mutating_python_apis_are_always_substantive(source):
    verdict = load_policy().classify_source("worker.py", source)
    assert verdict.kind == "substantive"
    assert verdict.reason == "state-mutating source"


def test_enabled_scoped_hook_blocks_substantive_write_file():
    plugin = load_plugin()
    ctx = FakeContext({"enabled": True})
    plugin.register(ctx)

    result = ctx.hook(
        tool_name="write_file",
        args={"path": "retract_240.py", "content": "conn.commit()\n"},
        task_id="t_domain",
        session_id="s_domain",
    )

    assert result["action"] == "block"
    assert result["message"] == (
        "BLOCKED: substantive executable task tooling is Cody-owned. "
        "Reuse or create a Cody Kanban card for retract_240.py."
    )


def test_shebang_marks_extensionless_write_as_code_like():
    plugin = load_plugin()
    ctx = FakeContext({"enabled": True})
    plugin.register(ctx)

    result = ctx.hook(
        tool_name="write_file",
        args={"path": "worker", "content": "#!/usr/bin/env python3\nconn.commit()\n"},
        task_id="t_domain", session_id="s_domain",
    )

    assert result["action"] == "block"


def test_execute_code_nested_write_is_blocked_before_kernel_dispatch():
    plugin = load_plugin()
    ctx = FakeContext({"enabled": True})
    plugin.register(ctx)
    code = (
        "from hermes_tools import write_file\n"
        "write_file(path='/tmp/retract_240.py', "
        "content=\"conn.execute('DELETE FROM sends')\\nconn.commit()\\n\")\n"
    )

    result = ctx.hook(
        tool_name="execute_code", args={"code": code},
        task_id="t_domain", session_id="s_domain",
    )

    assert result["action"] == "block"
    assert "/tmp/retract_240.py" in result["message"]


def test_execute_code_nested_patch_is_blocked_before_kernel_dispatch():
    plugin = load_plugin()
    ctx = FakeContext({"enabled": True})
    plugin.register(ctx)
    code = (
        "from hermes_tools import patch\n"
        "patch(path='worker.py', old_string='old', "
        "new_string='conn.commit()\\n', mode='replace')\n"
    )

    result = ctx.hook(
        tool_name="execute_code", args={"code": code},
        task_id="t_domain", session_id="s_domain",
    )

    assert result["action"] == "block"
    assert "worker.py" in result["message"]


def test_execute_code_nested_tool_call_is_blocked_before_rpc_dispatch():
    plugin = load_plugin()
    ctx = FakeContext({"enabled": True})
    plugin.register(ctx)
    code = """tool_call(calls=[{
    'name': 'write_file',
    'arguments': {'path': 'worker.py', 'content': 'conn.commit()\\n'},
}])
"""

    result = ctx.hook(
        tool_name="execute_code", args={"code": code},
        task_id="t_domain", session_id="s_domain",
    )

    assert result["action"] == "block"
    assert "worker.py" in result["message"]


def test_patch_replace_with_mutating_source_is_blocked():
    plugin = load_plugin()
    ctx = FakeContext({"enabled": True})
    plugin.register(ctx)

    result = ctx.hook(
        tool_name="patch",
        args={
            "mode": "replace", "path": "worker.py", "old_string": "return rows",
            "new_string": "conn.execute('DELETE FROM rows')\nconn.commit()",
        },
        task_id="t_domain", session_id="s_domain",
    )

    assert result["action"] == "block"
    assert "worker.py" in result["message"]


def test_multi_file_patch_blocks_code_candidate_after_prose():
    plugin = load_plugin()
    ctx = FakeContext({"enabled": True})
    plugin.register(ctx)
    payload = """*** Begin Patch
*** Add File: notes.md
+ordinary prose
*** Add File: worker.py
+conn.execute('DELETE FROM rows')
+conn.commit()
*** End Patch
"""

    result = ctx.hook(
        tool_name="patch", args={"mode": "patch", "patch": payload},
        task_id="t_domain", session_id="s_domain",
    )

    assert result["action"] == "block"
    assert "worker.py" in result["message"]


def test_deletion_only_code_patch_is_unresolved_and_blocked():
    plugin = load_plugin()
    ctx = FakeContext({"enabled": True})
    plugin.register(ctx)
    payload = """*** Begin Patch
*** Update File: worker.py
@@
-conn.commit()
*** End Patch
"""

    result = ctx.hook(
        tool_name="patch", args={"mode": "patch", "patch": payload},
        task_id="t_domain", session_id="s_domain",
    )

    assert result["action"] == "block"
    assert "worker.py" in result["message"]


def test_terminal_heredoc_file_creation_is_blocked():
    plugin = load_plugin()
    ctx = FakeContext({"enabled": True})
    plugin.register(ctx)
    command = (
        "cat > /tmp/retract_240.py <<'PY'\n"
        "conn.execute('DELETE FROM sends')\nconn.commit()\nPY\n"
    )

    result = ctx.hook(
        tool_name="terminal", args={"command": command},
        task_id="t_domain", session_id="s_domain",
    )

    assert result["action"] == "block"
    assert "/tmp/retract_240.py" in result["message"]


def test_terminal_printf_file_creation_is_blocked():
    plugin = load_plugin()
    ctx = FakeContext({"enabled": True})
    plugin.register(ctx)

    result = ctx.hook(
        tool_name="terminal",
        args={"command": "printf '%s\\n' 'conn.commit()' > /tmp/worker.py"},
        task_id="t_domain", session_id="s_domain",
    )

    assert result["action"] == "block"
    assert "/tmp/worker.py" in result["message"]


def test_malformed_terminal_code_write_fails_closed():
    plugin = load_plugin()
    ctx = FakeContext({"enabled": True})
    plugin.register(ctx)

    result = ctx.hook(
        tool_name="terminal",
        args={"command": "cat > /tmp/worker.py <<'PY'\nconn.commit()\n"},
        task_id="t_domain", session_id="s_domain",
    )

    assert result["action"] == "block"
    assert "/tmp/worker.py" in result["message"]


def test_terminal_inline_python_mutation_is_blocked():
    plugin = load_plugin()
    ctx = FakeContext({"enabled": True})
    plugin.register(ctx)

    result = ctx.hook(
        tool_name="terminal",
        args={"command": "python -c \"Path('worker.py').write_text('x')\""},
        task_id="t_domain", session_id="s_domain",
    )

    assert result["action"] == "block"
    assert "terminal-python" in result["message"]


def test_exact_unexpired_exception_allows_bound_write(tmp_path):
    plugin = load_plugin()
    path = tmp_path / "approved.py"
    content = "conn.commit()\n"
    exception = {
        "profile": "emma", "task_id": "t_domain", "session_id": "s_domain",
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
        "expires_at": time.time() + 60,
    }
    ctx = FakeContext({"enabled": True, "exceptions": [exception]})
    plugin.register(ctx)

    result = ctx.hook(
        tool_name="write_file", args={"path": str(path), "content": content},
        task_id="t_domain", session_id="s_domain",
    )

    assert result is None


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("profile", "sophia"),
        ("task_id", "t_other"),
        ("session_id", "s_other"),
        ("path", "/tmp/other.py"),
        ("sha256", "0" * 64),
        ("expires_at", 0),
    ],
)
def test_wrong_or_expired_exception_is_denied(tmp_path, field, wrong_value):
    plugin = load_plugin()
    path = tmp_path / "approved.py"
    content = "conn.commit()\n"
    exception = {
        "profile": "emma", "task_id": "t_domain", "session_id": "s_domain",
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
        "expires_at": time.time() + 60,
    }
    exception[field] = wrong_value
    ctx = FakeContext({"enabled": True, "exceptions": [exception]})
    plugin.register(ctx)

    result = ctx.hook(
        tool_name="write_file", args={"path": str(path), "content": content},
        task_id="t_domain", session_id="s_domain",
    )

    assert result["action"] == "block"


@pytest.mark.parametrize(
    ("settings", "profile"),
    [({}, "emma"), ({"enabled": False}, "emma"), ({"enabled": True}, "cody")],
)
def test_default_off_and_out_of_scope_profiles_pass(settings, profile):
    plugin = load_plugin()
    ctx = FakeContext(settings, profile_name=profile)
    plugin.register(ctx)

    result = ctx.hook(
        tool_name="write_file",
        args={"path": "worker.py", "content": "conn.commit()\n"},
        task_id="t_domain", session_id="s_domain",
    )

    assert result is None


@pytest.mark.parametrize(
    ("tool_name", "args"),
    [
        ("write_file", {"path": "notes.md", "content": "ordinary prose"}),
        ("write_file", {"path": "probe.py", "content": "print('ok')\n"}),
        ("terminal", {"command": "ps aux"}),
        ("domain_api", {"action": "update", "record": "native"}),
    ],
)
def test_ordinary_domain_work_passes(tool_name, args):
    plugin = load_plugin()
    ctx = FakeContext({"enabled": True})
    plugin.register(ctx)

    assert ctx.hook(
        tool_name=tool_name, args=args,
        task_id="t_domain", session_id="s_domain",
    ) is None


def test_unresolved_source_is_preserved_in_audit(tmp_path):
    plugin = load_plugin()
    audit_path = tmp_path / "audit.jsonl"
    ctx = FakeContext({"enabled": True, "audit_path": str(audit_path)})
    plugin.register(ctx)

    result = ctx.hook(
        tool_name="write_file",
        args={"path": "broken.py", "content": "def nope(:\n"},
        task_id="t_domain", session_id="s_domain",
    )

    assert result["action"] == "block"
    rows = [json.loads(line) for line in audit_path.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["classification"] == "unresolved"
    assert rows[0]["reason"] == "source could not be classified"
    assert "content" not in rows[0]


def test_classifier_error_fails_closed_for_code_like_write(monkeypatch):
    plugin = load_plugin()
    ctx = FakeContext({"enabled": True})
    plugin.register(ctx)
    monkeypatch.setattr(
        plugin, "classify_source", lambda *_args: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    result = ctx.hook(
        tool_name="write_file",
        args={"path": "worker.py", "content": "conn.commit()\n"},
        task_id="t_domain", session_id="s_domain",
    )

    assert result == {
        "action": "block",
        "message": (
            "BLOCKED: substantive executable task tooling is Cody-owned. "
            "Reuse or create a Cody Kanban card for the unresolved policy evaluation."
        ),
    }


def test_exception_audit_failure_fails_closed(tmp_path, monkeypatch):
    plugin = load_plugin()
    content = "conn.commit()\n"
    exception = {
        "profile": "emma", "task_id": "t_domain", "session_id": "s_domain",
        "path": str((tmp_path / "approved.py").resolve()),
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
        "expires_at": time.time() + 60,
    }
    ctx = FakeContext({"enabled": True, "exceptions": [exception]})
    plugin.register(ctx)
    monkeypatch.setattr(
        plugin, "append_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("full")),
    )

    result = ctx.hook(
        tool_name="write_file",
        args={"path": str(tmp_path / "approved.py"), "content": content},
        task_id="t_domain", session_id="s_domain",
    )

    assert result["action"] == "block"
    assert "unresolved exception audit" in result["message"]


def test_exception_path_binding_canonicalizes_relative_and_symlink_paths(tmp_path, monkeypatch):
    plugin = load_plugin()
    target = tmp_path / "approved.py"
    target.write_text("placeholder\n")
    link = tmp_path / "linked.py"
    link.symlink_to(target)
    content = "conn.commit()\n"
    exception = {
        "profile": "emma", "task_id": "t_domain", "session_id": "s_domain",
        "path": str(target.resolve()),
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
        "expires_at": time.time() + 60,
    }
    ctx = FakeContext({"enabled": True, "exceptions": [exception]})
    plugin.register(ctx)
    monkeypatch.chdir(tmp_path)

    for path in ("approved.py", str(link)):
        assert ctx.hook(
            tool_name="write_file", args={"path": path, "content": content},
            task_id="t_domain", session_id="s_domain",
        ) is None


def test_concurrent_blocks_append_complete_audit_records(tmp_path):
    plugin = load_plugin()
    audit_path = tmp_path / "audit.jsonl"
    ctx = FakeContext({"enabled": True, "audit_path": str(audit_path)})
    plugin.register(ctx)

    def invoke(index):
        return ctx.hook(
            tool_name="write_file",
            args={"path": f"worker_{index}.py", "content": "conn.commit()\n"},
            task_id="t_domain", session_id=f"s_{index}",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(invoke, range(24)))

    assert all(result["action"] == "block" for result in results)
    rows = [json.loads(line) for line in audit_path.read_text().splitlines()]
    assert len(rows) == 24
    assert {row["session_id"] for row in rows} == {f"s_{i}" for i in range(24)}


def test_native_pre_tool_call_timeout_is_fail_closed(monkeypatch, tmp_path):
    import hermes_cli.plugins as plugin_runtime

    manager = plugin_runtime.PluginManager(scope_key=str(tmp_path))
    manager._hooks["pre_tool_call"] = [lambda **_kwargs: time.sleep(0.2)]
    monkeypatch.setattr(plugin_runtime, "_resolve_hook_callback_timeout", lambda: 0.01)

    result = manager.invoke_hook(
        "pre_tool_call", tool_name="write_file", args={}, tool_call_id="call-1",
    )

    assert result == [{
        "action": "block",
        "message": "pre_tool_call plugin callback timed out or is still running",
    }]
