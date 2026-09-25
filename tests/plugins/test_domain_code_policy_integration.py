"""Real-dispatch integration tests for domain-code-policy."""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "external" / "domain-code-policy"


@pytest.fixture
def installed_policy(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    target = home / "plugins" / "domain-code-policy"
    target.parent.mkdir(parents=True)
    shutil.copytree(SOURCE, target)
    config = {
        "plugins": {
            "enabled": ["domain-code-policy"],
            "entries": {
                "domain-code-policy": {
                    "settings": {"enabled": True, "profiles": ["custom", "default"]}
                }
            },
        }
    }
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli import plugins
    plugins._plugin_manager = None
    plugins._plugin_managers_by_home.clear()
    manager = plugins.get_plugin_manager()
    manager.discover_and_load(force=True)
    loaded = manager._plugins["domain-code-policy"]
    assert loaded.enabled and loaded.module is not None, loaded.error
    yield home
    manager.unload()
    plugins._plugin_manager = None
    plugins._plugin_managers_by_home.clear()


def test_blocked_write_file_leaves_target_absent(installed_policy, tmp_path):
    from model_tools import handle_function_call

    target = tmp_path / "retract_240.py"
    result = json.loads(handle_function_call(
        "write_file", {"path": str(target), "content": "conn.commit()\n"},
        task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in result["error"]
    assert not target.exists()


def test_blocked_patch_leaves_original_bytes(installed_policy, tmp_path):
    from model_tools import handle_function_call

    target = tmp_path / "worker.py"
    original = b"def read():\n    return []\n"
    target.write_bytes(original)
    result = json.loads(handle_function_call(
        "patch",
        {
            "mode": "replace", "path": str(target), "old_string": "    return []",
            "new_string": "    conn.execute('DELETE FROM rows')\n    conn.commit()",
        },
        task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in result["error"]
    assert target.read_bytes() == original


def test_non_code_write_dispatches_and_lands(installed_policy, tmp_path):
    from model_tools import handle_function_call

    target = tmp_path / "notes.md"
    result = json.loads(handle_function_call(
        "write_file", {"path": str(target), "content": "ordinary domain notes\n"},
        task_id="t_domain", session_id="s_domain",
    ))

    assert result.get("verified") is True, result
    assert target.read_text() == "ordinary domain notes\n"


def test_permitted_terminal_call_dispatches_exactly_once(installed_policy, tmp_path):
    from model_tools import handle_function_call

    target = tmp_path / "counter.txt"
    result = json.loads(handle_function_call(
        "terminal", {"command": f"printf x >> {target}"},
        task_id="t_domain", session_id="s_domain",
    ))

    assert result["exit_code"] == 0, result
    assert target.read_text() == "x"


def test_execute_code_mutation_is_blocked_before_ledger_change(installed_policy, tmp_path):
    from model_tools import handle_function_call

    ledger = tmp_path / "ledger.db"
    with sqlite3.connect(ledger) as db:
        db.execute("CREATE TABLE sends(id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO sends(id) VALUES(240)")
    code = (
        "import sqlite3\n"
        f"conn = sqlite3.connect({str(ledger)!r})\n"
        "conn.execute('DELETE FROM sends WHERE id=240')\n"
        "conn.commit()\n"
    )

    result = json.loads(handle_function_call(
        "execute_code", {"code": code}, task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in result["error"]
    with sqlite3.connect(ledger) as db:
        assert db.execute("SELECT id FROM sends").fetchall() == [(240,)]


def test_terminal_heredoc_is_blocked_before_file_creation(installed_policy, tmp_path):
    from model_tools import handle_function_call

    target = tmp_path / "worker.py"
    command = f"cat > {target} <<'PY'\nconn.commit()\nPY"

    result = json.loads(handle_function_call(
        "terminal", {"command": command}, task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in result["error"]
    assert not target.exists()
