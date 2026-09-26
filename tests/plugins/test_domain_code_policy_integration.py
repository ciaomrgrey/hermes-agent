"""Real-dispatch integration tests for domain-code-policy."""
from __future__ import annotations

import hashlib
import json
import shlex
import shutil
import sqlite3
import time
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
    from tools import approval, terminal_tool
    monkeypatch.setattr(
        approval, "check_execute_code_guard",
        lambda *_args, **_kwargs: {"approved": True},
    )
    monkeypatch.setattr(
        terminal_tool, "_check_all_guards_impl",
        lambda *_args, **_kwargs: {"approved": True},
    )

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


def test_patch_classifies_effective_artifact_before_mutating(installed_policy, tmp_path):
    from model_tools import handle_function_call

    target = tmp_path / "worker.py"
    original = b"open('ledger.txt', 'r')\n"
    target.write_bytes(original)

    result = json.loads(handle_function_call(
        "patch",
        {
            "mode": "replace", "path": str(target),
            "old_string": "'r'", "new_string": "'w'",
        },
        task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in result["error"]
    assert target.read_bytes() == original


def test_v4a_update_classifies_effective_artifact(installed_policy, tmp_path):
    from model_tools import handle_function_call

    target = tmp_path / "worker.py"
    original = b"open('ledger.txt', 'r')\n"
    target.write_bytes(original)
    payload = f"""*** Begin Patch
*** Update File: {target}
@@
-'r'
+'w'
*** End Patch"""

    result = json.loads(handle_function_call(
        "patch", {"mode": "patch", "patch": payload},
        task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in result["error"]
    assert target.read_bytes() == original


@pytest.mark.parametrize(
    ("suffix", "addition", "blocked"),
    [
        (".md", "ordinary note", False),
        (".py", "conn.commit()", True),
    ],
)
def test_v4a_addition_only_update_uses_effective_artifact(
    installed_policy, tmp_path, suffix, addition, blocked,
):
    from model_tools import handle_function_call

    target = tmp_path / f"artifact{suffix}"
    original = b"Anchor\n"
    target.write_bytes(original)
    payload = f"""*** Begin Patch
*** Update File: {target}
@@ Anchor
+{addition}
*** End Patch"""

    result = json.loads(handle_function_call(
        "patch", {"mode": "patch", "patch": payload},
        task_id="t_domain", session_id="s_domain",
    ))

    if blocked:
        assert "Cody-owned" in result["error"]
        assert target.read_bytes() == original
    else:
        assert "error" not in result, result
        assert target.read_text() == f"Anchor\n{addition}\n"


@pytest.mark.parametrize("operation", ["Delete", "Move"])
def test_v4a_code_delete_and_move_are_blocked_before_mutation(
    installed_policy, tmp_path, operation,
):
    from model_tools import handle_function_call

    target = tmp_path / "worker.py"
    original = b"print('safe')\n"
    target.write_bytes(original)
    if operation == "Delete":
        directive = f"*** Delete File: {target}"
        moved = None
    else:
        moved = tmp_path / "moved.py"
        directive = f"*** Move File: {target} -> {moved}"
    payload = f"*** Begin Patch\n{directive}\n*** End Patch"

    result = json.loads(handle_function_call(
        "patch", {"mode": "patch", "patch": payload},
        task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in result["error"]
    assert target.read_bytes() == original
    if moved is not None:
        assert not moved.exists()


def test_non_code_write_dispatches_and_lands(installed_policy, tmp_path):
    from model_tools import handle_function_call

    target = tmp_path / "notes.md"
    result = json.loads(handle_function_call(
        "write_file", {"path": str(target), "content": "ordinary domain notes\n"},
        task_id="t_domain", session_id="s_domain",
    ))

    assert result.get("verified") is True, result
    assert target.read_text() == "ordinary domain notes\n"


def test_symlink_uses_canonical_target_for_code_gating(installed_policy, tmp_path):
    from model_tools import handle_function_call

    target = tmp_path / "worker.py"
    original = b"print('safe')\n"
    target.write_bytes(original)
    link = tmp_path / "notes.txt"
    link.symlink_to(target)

    result = json.loads(handle_function_call(
        "write_file", {"path": str(link), "content": "conn.commit()\n"},
        task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in result["error"]
    assert target.read_bytes() == original


@pytest.mark.parametrize(
    ("name", "source"),
    [
        ("worker.js", "require('fs').writeFileSync('ledger.txt', 'changed')\n"),
        ("worker.js", "console.log(customOperation(record))\n"),
        ("worker.sh", "git commit -am change\n"),
        ("worker.sh", "echo $(git commit -am change)\n"),
        ("worker.sh", "echo <(custom-command record)\n"),
        ("worker.sh", "echo >(custom-command record)\n"),
        ("worker.sh", "git diff --output=/tmp/ledger.patch\n"),
        ("worker.py", "df.to_csv('ledger.csv')\n"),
        ("worker.py", "import json\njson.loads(payload, object_hook=custom_operation)\n"),
        ("worker.py", "import custom_module\n"),
        ("worker.py", "record['approved'] = True\n"),
        ("worker.py", "record.approved = True\n"),
        ("worker.py", "record << payload\n"),
        (
            "worker.py",
            "import os\nimport hermes_tools\nhermes_tools.terminal = os.system\n",
        ),
    ],
)
def test_native_nontrivial_source_is_blocked_before_creation(
    installed_policy, tmp_path, name, source,
):
    from model_tools import handle_function_call

    target = tmp_path / name
    result = json.loads(handle_function_call(
        "write_file", {"path": str(target), "content": source},
        task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in result["error"]
    assert not target.exists()


@pytest.mark.parametrize(
    "shadow", ["terminal", "hermes_tools", "member", "module_attr", "annotation"],
)
def test_execute_code_shadowed_native_name_is_blocked_before_side_effect(
    installed_policy, tmp_path, shadow,
):
    from model_tools import handle_function_call

    target = tmp_path / "escaped.txt"
    if shadow == "terminal":
        source = (
            "import os\nterminal = os.system\n"
            f"terminal('touch {target}')\n"
        )
    elif shadow == "hermes_tools":
        source = (
            "import os\nhermes_tools = os\n"
            f"hermes_tools.system('touch {target}')\n"
        )
    elif shadow == "member":
        source = (
            "import os\nimport hermes_tools\nhermes_tools.terminal = os.system\n"
            f"hermes_tools.terminal('touch {target}')\n"
        )
    elif shadow == "module_attr":
        source = (
            "import hermes_tools\n"
            f"hermes_tools.os.system('touch {target}')\n"
        )
    else:
        source = (
            "import hermes_tools\n"
            f"x: hermes_tools.os.system('touch {target}') = 1\n"
        )

    result = json.loads(handle_function_call(
        "execute_code", {"code": source}, task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in result["error"]
    assert not target.exists()


@pytest.mark.parametrize("tool_name", ["write_file", "patch", "terminal"])
def test_executable_alias_to_data_target_preserves_language_identity(
    installed_policy, tmp_path, tool_name,
):
    from model_tools import handle_function_call

    target = tmp_path / "payload.txt"
    original = b"original\n"
    target.write_bytes(original)
    alias = tmp_path / "worker.py"
    alias.symlink_to(target)
    source = "open('ledger.txt', 'w')\n"
    if tool_name == "write_file":
        read = json.loads(handle_function_call(
            "read_file", {"path": str(alias)}, task_id="t_domain", session_id="s_domain",
        ))
        assert "original" in read.get("content", ""), read
        args = {"path": str(alias), "content": source}
    elif tool_name == "patch":
        args = {
            "mode": "replace", "path": str(alias),
            "old_string": "original", "new_string": source.rstrip(),
        }
    else:
        args = {"command": f"printf '%s\\n' \"open('ledger.txt', 'w')\" > {alias}"}

    result = json.loads(handle_function_call(
        tool_name, args, task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in result["error"]
    assert target.read_bytes() == original


def test_data_alias_to_executable_target_still_uses_canonical_language(
    installed_policy, tmp_path,
):
    from model_tools import handle_function_call

    target = tmp_path / "worker.py"
    original = b"print('safe')\n"
    target.write_bytes(original)
    alias = tmp_path / "payload.txt"
    alias.symlink_to(target)

    read = json.loads(handle_function_call(
        "read_file", {"path": str(alias)}, task_id="t_domain", session_id="s_domain",
    ))
    assert "safe" in read.get("content", ""), read

    result = json.loads(handle_function_call(
        "write_file", {"path": str(alias), "content": "df.to_csv('ledger.csv')\n"},
        task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in result["error"]
    assert target.read_bytes() == original


def test_patch_through_data_alias_uses_executable_target_language(
    installed_policy, tmp_path,
):
    from model_tools import handle_function_call

    target = tmp_path / "worker.py"
    original = b"print('safe')\n"
    target.write_bytes(original)
    alias = tmp_path / "payload.txt"
    alias.symlink_to(target)

    result = json.loads(handle_function_call(
        "patch",
        {
            "mode": "replace", "path": str(alias),
            "old_string": "print('safe')", "new_string": "df.to_csv('ledger.csv')",
        },
        task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in result["error"]
    assert target.read_bytes() == original


def test_non_code_symlink_remains_allowed(installed_policy, tmp_path):
    from model_tools import handle_function_call

    target = tmp_path / "payload.txt"
    target.write_text("old\n")
    alias = tmp_path / "notes.md"
    alias.symlink_to(target)
    read = json.loads(handle_function_call(
        "read_file", {"path": str(alias)}, task_id="t_domain", session_id="s_domain",
    ))
    assert "old" in read.get("content", ""), read
    result = json.loads(handle_function_call(
        "write_file", {"path": str(alias), "content": "ordinary domain notes\n"},
        task_id="t_domain", session_id="s_domain",
    ))

    assert result.get("verified") is True, result
    assert target.read_text() == "ordinary domain notes\n"


def test_permitted_terminal_call_dispatches_once_without_approval_side_effects(
    installed_policy,
):
    from model_tools import handle_function_call

    result = json.loads(handle_function_call(
        "terminal", {"command": "printf domain-control"},
        task_id="t_domain", session_id="s_domain",
    ))

    assert result["exit_code"] == 0, result
    assert result["output"] == "domain-control"


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


def test_actual_nested_rpc_preserves_prose_write(installed_policy, tmp_path):
    from model_tools import handle_function_call

    target = tmp_path / "notes.md"
    code = (
        "from hermes_tools import write_file\n"
        f"path = {str(target)!r}\n"
        "result = write_file(path=path, content='ordinary notes\\n')\n"
        "print(result.get('verified'))\n"
    )

    result = json.loads(handle_function_call(
        "execute_code", {"code": code}, task_id="t_domain", session_id="s_domain",
    ))

    assert result["exit_code"] == 0, result
    assert "True" in result["output"]
    assert target.read_text() == "ordinary notes\n"


def test_actual_nested_rpc_blocks_dynamic_code_write(installed_policy, tmp_path):
    from model_tools import handle_function_call

    target = tmp_path / "worker.py"
    code = (
        "from hermes_tools import write_file\n"
        f"path = {str(target)!r}\n"
        "content = 'conn.commit()\\n'\n"
        "result = write_file(path=path, content=content)\n"
        "print(result.get('error'))\n"
    )

    result = json.loads(handle_function_call(
        "execute_code", {"code": code}, task_id="t_domain", session_id="s_domain",
    ))

    assert result["exit_code"] == 0, result
    assert "Cody-owned" in result["output"]
    assert not target.exists()


def test_nested_rpc_cannot_reuse_exception_without_exact_session_identity(
    installed_policy, tmp_path,
):
    from hermes_cli import plugins
    from model_tools import handle_function_call

    target = tmp_path / "worker.py"
    content = "conn.commit()\n"
    config_path = installed_policy / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["plugins"]["entries"]["domain-code-policy"]["settings"]["exceptions"] = [
        {
            "profile": profile,
            "task_id": "t_domain",
            "session_id": "s_domain",
            "path": str(target.resolve()),
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
            "expires_at": time.time() + 60,
        }
        for profile in ("custom", "default")
    ]
    config_path.write_text(yaml.safe_dump(config))
    plugins.get_plugin_manager().unload()
    plugins._plugin_manager = None
    plugins._plugin_managers_by_home.clear()
    plugins.get_plugin_manager().discover_and_load(force=True)

    direct = json.loads(handle_function_call(
        "write_file", {"path": str(target), "content": content},
        task_id="t_domain", session_id="s_domain",
    ))
    assert direct.get("verified") is True, direct
    target.unlink()

    code = (
        "from hermes_tools import write_file\n"
        f"path = {str(target)!r}\n"
        f"content = {content!r}\n"
        "result = write_file(path=path, content=content)\n"
        "print(result.get('error'))\n"
    )
    nested = json.loads(handle_function_call(
        "execute_code", {"code": code}, task_id="t_domain", session_id="s_domain",
    ))

    assert nested["exit_code"] == 0, nested
    assert "Cody-owned" in nested["output"]
    assert not target.exists()


def test_terminal_heredoc_is_blocked_before_file_creation(installed_policy, tmp_path):
    from model_tools import handle_function_call

    target = tmp_path / "worker.py"
    command = f"cat > {target} <<'PY'\nconn.commit()\nPY"

    result = json.loads(handle_function_call(
        "terminal", {"command": command}, task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in result["error"]
    assert not target.exists()


def test_terminal_workdir_resolves_symlink_before_code_gating(
    installed_policy, tmp_path,
):
    from model_tools import handle_function_call

    target = tmp_path / "worker.py"
    original = b"print('safe')\n"
    target.write_bytes(original)
    (tmp_path / "notes.txt").symlink_to(target)

    result = json.loads(handle_function_call(
        "terminal",
        {"command": "echo 'conn.commit()' > notes.txt", "workdir": str(tmp_path)},
        task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in result["error"]
    assert target.read_bytes() == original


def test_terminal_resolves_redirection_before_later_cd(
    installed_policy, tmp_path,
):
    from model_tools import handle_function_call

    target = tmp_path / "worker.py"
    original = b"print('safe')\n"
    target.write_bytes(original)
    (tmp_path / "notes.txt").symlink_to(target)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    result = json.loads(handle_function_call(
        "terminal",
        {
            "command": f"echo 'conn.commit()' > notes.txt; cd {elsewhere} && printf done",
            "workdir": str(tmp_path),
        },
        task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in result["error"]
    assert target.read_bytes() == original


def test_conditional_cd_before_relative_redirection_fails_closed(
    installed_policy, tmp_path,
):
    from model_tools import handle_function_call

    target = tmp_path / "worker.py"
    original = b"print('safe')\n"
    target.write_bytes(original)
    (tmp_path / "notes.txt").symlink_to(target)
    (tmp_path / "sub").mkdir()

    result = json.loads(handle_function_call(
        "terminal",
        {
            "command": "false && cd sub; echo 'conn.commit()' > notes.txt",
            "workdir": str(tmp_path),
        },
        task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in result["error"]
    assert target.read_bytes() == original


def test_failed_cd_or_branch_before_relative_redirection_fails_closed(
    installed_policy, tmp_path,
):
    from model_tools import handle_function_call

    target = tmp_path / "worker.py"
    original = b"print('safe')\n"
    target.write_bytes(original)
    (tmp_path / "notes.txt").symlink_to(target)

    result = json.loads(handle_function_call(
        "terminal",
        {
            "command": "cd missing || echo 'conn.commit()' > notes.txt",
            "workdir": str(tmp_path),
        },
        task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in result["error"]
    assert target.read_bytes() == original


def test_failed_cd_with_unconditional_separator_fails_closed(
    installed_policy, tmp_path,
):
    from model_tools import handle_function_call

    target = tmp_path / "worker.py"
    original = b"print('safe')\n"
    target.write_bytes(original)
    (tmp_path / "notes.txt").symlink_to(target)

    result = json.loads(handle_function_call(
        "terminal",
        {
            "command": "cd missing; echo 'conn.commit()' > notes.txt",
            "workdir": str(tmp_path),
        },
        task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in result["error"]
    assert target.read_bytes() == original


def test_terminal_newline_cd_applies_to_following_heredoc(
    installed_policy, tmp_path,
):
    from model_tools import handle_function_call

    subdir = tmp_path / "sub"
    subdir.mkdir()
    target = subdir / "worker.py"
    original = b"print('safe')\n"
    target.write_bytes(original)
    (subdir / "notes.txt").symlink_to(target)
    command = "cd sub\ncat > notes.txt <<'PY'\nconn.commit()\nPY"

    result = json.loads(handle_function_call(
        "terminal", {"command": command, "workdir": str(tmp_path)},
        task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in result["error"]
    assert target.read_bytes() == original


def test_formatted_printf_trivial_probe_is_allowed(installed_policy, tmp_path):
    from model_tools import handle_function_call

    target = tmp_path / "probe.py"
    result = json.loads(handle_function_call(
        "terminal",
        {"command": f"printf '%s\\n' 'print(1)' > {target}"},
        task_id="t_domain", session_id="s_domain",
    ))

    assert result["error"] is None, result
    assert target.read_text() == "print(1)\n"


def test_piped_tee_trivial_probe_is_allowed(installed_policy, tmp_path):
    from model_tools import handle_function_call

    target = tmp_path / "probe.py"
    result = json.loads(handle_function_call(
        "terminal", {"command": f"printf 'print(1)\\n' | tee {target}"},
        task_id="t_domain", session_id="s_domain",
    ))

    assert result["error"] is None, result
    assert target.read_text() == "print(1)\n"


@pytest.mark.parametrize("preexisting", [False, True])
def test_piped_python_stdin_authoring_is_blocked_before_mutation(
    installed_policy, tmp_path, preexisting,
):
    from model_tools import handle_function_call

    target = tmp_path / "worker.py"
    original = b"print('safe')\n" if preexisting else None
    if original is not None:
        target.write_bytes(original)
    ledger = tmp_path / "ledger.txt"
    ledger.write_bytes(b"unchanged\n")
    source = (
        "from pathlib import Path; "
        f"Path({str(target)!r}).write_text('conn.commit()\\n')"
    )
    command = "printf '%s\\n' " + shlex.quote(source) + " | python3"

    result = json.loads(handle_function_call(
        "terminal", {"command": command}, task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in (result.get("error") or ""), result
    assert (target.read_bytes() if target.exists() else None) == original
    assert ledger.read_bytes() == b"unchanged\n"


def test_piped_trivial_python_stdin_dispatches_normally(installed_policy):
    from model_tools import handle_function_call

    result = json.loads(handle_function_call(
        "terminal", {"command": "printf '%s\\n' 'print(1 + 1)' | python3"},
        task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" not in (result.get("error") or ""), result
    assert result.get("exit_code") == 0, result
    assert result.get("output", "").strip() == "2"


@pytest.mark.parametrize("pipe", ["|", "|&"])
def test_piped_python_with_shell_syntax_is_blocked_before_dispatch(
    installed_policy, tmp_path, pipe,
):
    from model_tools import handle_function_call

    redirected = tmp_path / "python.out"
    redirect = f" > {redirected}" if pipe == "|" else ""
    result = json.loads(handle_function_call(
        "terminal",
        {"command": f"printf '%s\\n' 'conn.commit()' {pipe} python3{redirect}"},
        task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in (result.get("error") or ""), result
    assert not redirected.exists()


@pytest.mark.parametrize("mode", ["script", "module"])
def test_python_mode_arguments_named_c_are_not_interpreter_source(
    installed_policy, tmp_path, mode,
):
    from model_tools import handle_function_call

    if mode == "script":
        script = tmp_path / "consumer.py"
        script.write_text("print('script ran')\n")
        command = f"python3 {shlex.quote(str(script))} -c 'conn.commit()'"
    else:
        command = "python3 -mjson.tool -c 'conn.commit()'"

    result = json.loads(handle_function_call(
        "terminal", {"command": command}, task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" not in (result.get("error") or ""), result


@pytest.mark.parametrize("mode", ["script", "module", "version"])
def test_python_heredoc_to_non_stdin_source_modes_is_data(
    installed_policy, tmp_path, mode,
):
    from model_tools import handle_function_call

    if mode == "script":
        script = tmp_path / "consumer.py"
        script.write_text("print('script ran')\n")
        invocation = f"python3 {shlex.quote(str(script))}"
    elif mode == "module":
        invocation = "python3 -mjson.tool"
    else:
        invocation = "python3 -VV"
    command = f"{invocation} <<'DATA'\nconn.commit()\nDATA"

    result = json.loads(handle_function_call(
        "terminal", {"command": command}, task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" not in (result.get("error") or ""), result


@pytest.mark.parametrize(
    "command",
    [
        "python3 -VV -c 'conn.commit()'",
        "python3 -VV <<< 'conn.commit()'",
    ],
    ids=["command-argument", "here-string"],
)
def test_python_attached_version_flag_terminates_inline_source_scanning(
    installed_policy, command,
):
    from model_tools import handle_function_call

    result = json.loads(handle_function_call(
        "terminal", {"command": command}, task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" not in (result.get("error") or ""), result
    assert result.get("exit_code") == 0, result


@pytest.mark.parametrize(
    "command",
    [
        "python3 -Vc 'conn.commit()'",
        "python3 -VVc 'conn.commit()'",
        "python3 -iV <<< 'conn.commit()'",
    ],
    ids=["single-version", "double-version", "inspect-version"],
)
def test_python_clustered_version_flag_terminates_source_scanning(
    installed_policy, command,
):
    from model_tools import handle_function_call

    result = json.loads(handle_function_call(
        "terminal", {"command": command}, task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" not in (result.get("error") or ""), result
    assert result.get("exit_code") == 0, result


@pytest.mark.parametrize(
    ("invocation", "blocked"),
    [("-cpass", False), ("-Bcpass", False), ("-icpass", True)],
    ids=["command", "command-with-flag", "inspect-command"],
)
def test_python_attached_command_controls_followup_stdin_classification(
    installed_policy, tmp_path, invocation, blocked,
):
    from model_tools import handle_function_call

    target = tmp_path / "worker.py"
    source = f"from pathlib import Path; Path({str(target)!r}).write_text('conn.commit()\\n')"
    command = "printf '%s\\n' " + shlex.quote(source) + f" | python3 {invocation}"

    result = json.loads(handle_function_call(
        "terminal", {"command": command}, task_id="t_domain", session_id="s_domain",
    ))

    assert ("Cody-owned" in (result.get("error") or "")) is blocked, result
    if not blocked:
        assert result.get("exit_code") == 0, result
    assert not target.exists()


@pytest.mark.parametrize("invocation", ["-icpass", "-i -c pass"])
@pytest.mark.parametrize("carrier", ["here-string", "heredoc"])
def test_python_inspect_command_classifies_redirected_stdin_source(
    installed_policy, tmp_path, invocation, carrier,
):
    from model_tools import handle_function_call

    target = tmp_path / "worker.py"
    source = f"from pathlib import Path; Path({str(target)!r}).write_text('conn.commit()\\n')"
    if carrier == "here-string":
        command = f"python3 {invocation} <<< " + shlex.quote(source)
    else:
        command = f"python3 {invocation} <<'PY'\n{source}\nPY"

    result = json.loads(handle_function_call(
        "terminal", {"command": command}, task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in (result.get("error") or ""), result
    assert not target.exists()


@pytest.mark.parametrize("option", ["-Wignore", "-Ximporttime"])
def test_python_attached_option_argument_does_not_enable_inspect_mode(
    installed_policy, tmp_path, option,
):
    from model_tools import handle_function_call

    target = tmp_path / "worker.py"
    source = f"from pathlib import Path; Path({str(target)!r}).write_text('conn.commit()\\n')"
    command = "printf '%s\\n' " + shlex.quote(source) + f" | python3 {option} -cpass"

    result = json.loads(handle_function_call(
        "terminal", {"command": command}, task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" not in (result.get("error") or ""), result
    assert result.get("exit_code") == 0, result
    assert not target.exists()


@pytest.mark.parametrize("preexisting", [False, True])
def test_python_interspersed_output_redirect_does_not_hide_inline_source(
    installed_policy, tmp_path, preexisting,
):
    from model_tools import handle_function_call

    target = tmp_path / "worker.py"
    original = b"print('safe')\n" if preexisting else None
    if original is not None:
        target.write_bytes(original)
    output = tmp_path / "python.out"
    ledger = tmp_path / "ledger.txt"
    ledger.write_bytes(b"unchanged\n")
    source = (
        "from pathlib import Path; "
        f"Path({str(target)!r}).write_text('conn.commit()\\n')"
    )
    command = f"python3 > {shlex.quote(str(output))} -c " + shlex.quote(source)

    result = json.loads(handle_function_call(
        "terminal", {"command": command}, task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in (result.get("error") or ""), result
    assert (target.read_bytes() if target.exists() else None) == original
    assert not output.exists()
    assert ledger.read_bytes() == b"unchanged\n"


@pytest.mark.parametrize("invocation", ["-cpass", "-VV"])
def test_python_interspersed_output_redirect_preserves_non_stdin_modes(
    installed_policy, tmp_path, invocation,
):
    from model_tools import handle_function_call

    target = tmp_path / "worker.py"
    output = tmp_path / "python.out"
    source = f"from pathlib import Path; Path({str(target)!r}).write_text('conn.commit()\\n')"
    command = (
        "printf '%s\\n' " + shlex.quote(source)
        + f" | python3 > {shlex.quote(str(output))} {invocation}"
    )

    result = json.loads(handle_function_call(
        "terminal", {"command": command}, task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" not in (result.get("error") or ""), result
    assert result.get("exit_code") == 0, result
    assert not target.exists()
    assert output.exists()


@pytest.mark.parametrize("mode", ["script", "command", "module"])
def test_python_inspect_mode_classifies_followup_stdin_source(
    installed_policy, tmp_path, mode,
):
    from model_tools import handle_function_call

    target = tmp_path / "worker.py"
    source = f"from pathlib import Path; Path({str(target)!r}).write_text('conn.commit()\\n')"
    if mode == "script":
        script = tmp_path / "consumer.py"
        script.write_text("print('script ran')\n")
        invocation = f"python3 -i {shlex.quote(str(script))}"
    elif mode == "command":
        invocation = "python3 -i -c pass"
    else:
        invocation = "python3 -i -m this"
    command = "printf '%s\\n' " + shlex.quote(source) + f" | {invocation}"

    result = json.loads(handle_function_call(
        "terminal", {"command": command}, task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in (result.get("error") or ""), result
    assert not target.exists()


def test_python_option_with_argument_preserves_stdin_source_classification(
    installed_policy, tmp_path,
):
    from model_tools import handle_function_call

    target = tmp_path / "worker.py"
    source = f"from pathlib import Path; Path({str(target)!r}).write_text('conn.commit()\\n')"
    command = (
        "printf '%s\\n' " + shlex.quote(source)
        + " | python3 --check-hash-based-pycs always"
    )

    result = json.loads(handle_function_call(
        "terminal", {"command": command}, task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in (result.get("error") or ""), result
    assert not target.exists()


@pytest.mark.parametrize("consumer", ["script", "module", "version"])
def test_piped_data_to_non_stdin_python_modes_dispatches_normally(
    installed_policy, tmp_path, consumer,
):
    from model_tools import handle_function_call

    if consumer == "script":
        script = tmp_path / "consumer.py"
        script.write_text("import sys\nprint(sys.stdin.read())\n")
        command = f"printf 'ordinary domain data' | python3 {shlex.quote(str(script))}"
    elif consumer == "module":
        command = "printf 'ordinary domain data' | python3 -m json.tool"
    else:
        command = "printf 'ordinary domain data' | python3 --version"

    result = json.loads(handle_function_call(
        "terminal", {"command": command}, task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" not in (result.get("error") or ""), result
    if consumer != "module":
        assert result.get("exit_code") == 0, result


def test_sequential_redirects_classify_cumulative_artifact(installed_policy, tmp_path):
    from model_tools import handle_function_call

    target = tmp_path / "worker.py"
    command = (
        f"echo 'if True: pass' > {target}; "
        f"echo 'x = foo()' >> {target}"
    )
    result = json.loads(handle_function_call(
        "terminal", {"command": command}, task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in result["error"]
    assert not target.exists()


@pytest.mark.parametrize(
    "command_factory",
    [
        lambda target: (
            "python -c \"from pathlib import Path; "
            f"Path({str(target)!r}).write_text('conn.commit()\\\\n')\"; printf done"
        ),
        lambda target: f"false && cd {target.parent / 'sub'}; echo 'conn.commit()' > {target}",
    ],
    ids=["python-before-semicolon", "conditional-cd"],
)
def test_complex_terminal_authoring_fails_closed(
    installed_policy, tmp_path, command_factory,
):
    from model_tools import handle_function_call

    target = tmp_path / "worker.py"
    command = command_factory(target)
    result = json.loads(handle_function_call(
        "terminal", {"command": command, "workdir": str(tmp_path)},
        task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in result["error"]
    assert not target.exists()


def test_space_indented_heredoc_delimiter_fails_closed(installed_policy, tmp_path):
    from model_tools import handle_function_call

    target = tmp_path / "worker.sh"
    command = f"cat > {target} <<'SH'\necho safe\n SH\ntouch /tmp/not-run\nSH"
    result = json.loads(handle_function_call(
        "terminal", {"command": command}, task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in result["error"]
    assert not target.exists()


@pytest.mark.parametrize(
    ("command_factory", "preexisting"),
    [
        (lambda target: f"tee {target} <<'PY'\nconn.commit()\nPY", False),
        (lambda target: f"tee -a {target} <<'PY'\nconn.commit()\nPY", True),
        (lambda target: f"tee {target} <<< 'conn.commit()'", False),
    ],
    ids=["direct-tee-heredoc", "direct-tee-append", "direct-tee-here-string"],
)
def test_direct_tee_authoring_is_blocked(
    installed_policy, tmp_path, command_factory, preexisting,
):
    from model_tools import handle_function_call

    target = tmp_path / "worker.py"
    original = b"print('safe')\n" if preexisting else None
    if original is not None:
        target.write_bytes(original)

    result = json.loads(handle_function_call(
        "terminal", {"command": command_factory(target)},
        task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in result["error"]
    if original is None:
        assert not target.exists()
    else:
        assert target.read_bytes() == original


@pytest.mark.parametrize(
    "shape", ["compact-c", "multiline-c", "clustered-Bc", "clustered-qc"],
)
def test_python_c_source_creation_is_blocked_before_mutation(
    installed_policy, tmp_path, shape,
):
    from model_tools import handle_function_call

    target = tmp_path / "worker.py"
    ledger = tmp_path / "ledger.txt"
    ledger.write_bytes(b"unchanged\n")
    source = (
        "from pathlib import Path; "
        f"Path({str(target)!r}).write_text('conn.commit()\\n')"
    )
    if shape == "compact-c":
        command = "python3 -c" + shlex.quote(source)
    elif shape.startswith("clustered-"):
        command = "python3 -" + shape.removeprefix("clustered-") + shlex.quote(source)
    else:
        command = "python3 -c " + shlex.quote(source.replace("; ", "\n"))

    result = json.loads(handle_function_call(
        "terminal", {"command": command}, task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in result["error"]
    assert not target.exists()
    assert ledger.read_bytes() == b"unchanged\n"


@pytest.mark.parametrize("shape", ["attached-multiline", "continued-command"])
@pytest.mark.parametrize("preexisting", [False, True])
def test_composed_python_c_source_creation_is_blocked_before_mutation(
    installed_policy, tmp_path, shape, preexisting,
):
    from model_tools import handle_function_call

    target = tmp_path / "worker.py"
    original = b"print('safe')\n" if preexisting else None
    if original is not None:
        target.write_bytes(original)
    ledger = tmp_path / "ledger.txt"
    ledger.write_bytes(b"unchanged\n")
    source = (
        "from pathlib import Path\n"
        f"Path({str(target)!r}).write_text('conn.commit()\\n')"
    )
    command = "python3 -c" + shlex.quote(source)
    if shape == "continued-command":
        command = "python3 \\\n-c " + shlex.quote(source)

    result = json.loads(handle_function_call(
        "terminal", {"command": command}, task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in (result.get("error") or ""), result
    if original is None:
        assert not target.exists()
    else:
        assert target.read_bytes() == original
    assert ledger.read_bytes() == b"unchanged\n"


@pytest.mark.parametrize("carrier", ["write_file", "execute_code", "terminal"])
def test_literal_arithmetic_dispatches_normally(installed_policy, tmp_path, carrier):
    from model_tools import handle_function_call

    source = "print(1 + 1)\n"
    target = tmp_path / "arithmetic.py"
    if carrier == "write_file":
        args = {"path": str(target), "content": source}
    elif carrier == "execute_code":
        args = {"code": source}
    else:
        args = {"command": "python3 -c " + shlex.quote(source)}

    result = json.loads(handle_function_call(
        carrier, args, task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" not in (result.get("error") or ""), result


def test_multiline_python_c_non_authoring_dispatches_normally(installed_policy):
    from model_tools import handle_function_call

    result = json.loads(handle_function_call(
        "terminal",
        {"command": "python3 -c 'print(1 + 1)\nprint(3 + 4)'"},
        task_id="t_domain",
        session_id="s_domain",
    ))

    assert "Cody-owned" not in (result.get("error") or ""), result


@pytest.mark.parametrize(
    "command",
    [
        "python3 -c'print(1 + 1)\nprint(3 + 4)'",
        "python3 \\\n-c 'print(1 + 1)'",
    ],
)
def test_composed_python_c_non_authoring_dispatches_normally(installed_policy, command):
    from model_tools import handle_function_call

    result = json.loads(handle_function_call(
        "terminal", {"command": command}, task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" not in (result.get("error") or ""), result


def test_python_warning_option_before_c_dispatches_normally(installed_policy):
    from model_tools import handle_function_call

    result = json.loads(handle_function_call(
        "terminal",
        {"command": "python3 -Wignore::DeprecationWarning -c 'print(1 + 1)'"},
        task_id="t_domain",
        session_id="s_domain",
    ))

    assert "Cody-owned" not in (result.get("error") or ""), result


def test_ordinary_nested_read_dispatches_normally(installed_policy, tmp_path):
    from model_tools import handle_function_call

    notes = tmp_path / "notes.txt"
    notes.write_text("ordinary notes\n")
    code = (
        "from hermes_tools import read_file\n"
        f"print(read_file({str(notes)!r}))\n"
    )

    result = json.loads(handle_function_call(
        "execute_code", {"code": code}, task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" not in (result.get("error") or ""), result


@pytest.mark.parametrize(
    "command_factory",
    [
        lambda target: f"target={target}; echo 'conn.commit()' > \"$target\"",
        lambda target: f"cd {target.parent} && echo 'conn.commit()' > {target.name}",
        lambda target: f"printf 'conn.commit()\\n' | tee -- {target}",
        lambda target: f"printf 'conn.commit()\\n' | tee -i {target.with_suffix('.txt')} {target}",
        lambda target: f"printf 'conn.commit()\\n' | cat | tee {target}",
        lambda target: (
            f"env python3 -c \"from pathlib import Path; Path(r'{target}').write_text('x')\""
        ),
        lambda target: (
            f"uv run python -c \"from pathlib import Path; Path(r'{target}').write_text('x')\""
        ),
        lambda target: (
            f"PYTHONPATH=. python -c \"from pathlib import Path; Path(r'{target}').write_text('x')\""
        ),
        lambda target: (
            f"command python -c \"from pathlib import Path; Path(r'{target}').write_text('x')\""
        ),
        lambda target: (
            f"FOO=1 command python -c \"from pathlib import Path; Path(r'{target}').write_text('x')\""
        ),
        lambda target: (
            "python <<'PY'\n"
            "from pathlib import Path\n"
            f"Path(r'{target}').write_text('x')\n"
            "PY"
        ),
        lambda target: (
            f"echo 'conn.commit()' > {target}; cd {target.parent / 'elsewhere'} && printf done"
        ),
    ],
    ids=[
        "variable-target", "chained-cd", "tee-double-dash",
        "tee-option-multiple-targets", "multi-stage-tee",
        "env-python", "uv-python", "assignment-python", "command-python",
        "assignment-command-python", "python-heredoc-no-dash",
        "redirection-before-later-cd",
    ],
)
def test_additional_common_terminal_authoring_is_blocked(
    installed_policy, tmp_path, command_factory,
):
    from model_tools import handle_function_call

    target = tmp_path / "worker.py"
    command = command_factory(target)

    result = json.loads(handle_function_call(
        "terminal", {"command": command}, task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in result["error"]
    assert not target.exists()


@pytest.mark.parametrize(
    ("command_factory", "preexisting"),
    [
        (lambda target: f"echo 'conn.commit()' >{target}", False),
        (lambda target: f"echo 'conn.commit()' &> {target}", False),
        (lambda target: f"echo 'conn.commit()' >& {target}", False),
        (lambda target: f"printf 'conn.commit()\\n' > {target}", False),
        (lambda target: (
            "python - <<'PY'\nfrom pathlib import Path\n"
            f"Path({str(target)!r}).write_text('conn.commit()\\n')\nPY"
        ), False),
        (lambda target: f"echo 'conn.commit()' >> {target}", True),
        (lambda target: f"printf 'conn.commit()\\n' | tee {target}", False),
    ],
    ids=["echo", "all-output", "dup-output", "printf", "python-stdin", "append", "tee"],
)
def test_common_terminal_authoring_is_blocked_before_mutation(
    installed_policy, tmp_path, command_factory, preexisting,
):
    from model_tools import handle_function_call

    target = tmp_path / "worker.py"
    original = b"print('safe')\n" if preexisting else None
    if original is not None:
        target.write_bytes(original)
    command = command_factory(target)

    result = json.loads(handle_function_call(
        "terminal", {"command": command}, task_id="t_domain", session_id="s_domain",
    ))

    assert "Cody-owned" in result["error"]
    if original is None:
        assert not target.exists()
    else:
        assert target.read_bytes() == original
