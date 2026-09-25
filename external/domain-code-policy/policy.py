"""Pure source classification for the domain code admission policy."""
from __future__ import annotations

import ast
import re
import shlex
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Verdict:
    kind: str
    reason: str


_MUTATING_SQL = ("delete ", "insert ", "update ", "drop ", "alter ", "create ", "replace ")
_PYTHON_SUFFIXES = frozenset({".py", ".pyw"})
_MUTATING_CALL_NAMES = frozenset({
    "os.remove", "os.unlink", "os.rename", "os.replace", "os.mkdir", "os.makedirs",
    "os.rmdir", "shutil.move", "shutil.copy", "shutil.copy2", "shutil.copytree",
    "shutil.rmtree", "requests.post", "requests.put", "requests.patch", "requests.delete",
    "urllib.request.urlopen",
})
_MUTATING_METHODS = frozenset({
    "commit", "mkdir", "rmdir", "save", "touch", "unlink", "write_bytes", "write_text",
})
_STATEFUL_RECEIVERS = frozenset({
    "api", "client", "collection", "conn", "connection", "cursor", "database", "db",
    "ledger", "repo", "repository", "session", "store", "table",
})
_PATH_RECEIVERS = frozenset({"file", "fp", "path", "stream", "target", "writer"})
_LEXICAL_MUTATION = re.compile(
    r"(?ix)(?:\b(?:delete|insert|update|drop|alter|create)\s+|"
    r"\b(?:writeFile|appendFile|unlink|rename|remove|rmtree|commit)\s*\(|"
    r"\b(?:fetch|axios\.(?:post|put|patch|delete))\s*\()"
)
_LEXICAL_CONTROL = re.compile(r"(?m)\b(?:if|for|while|try|catch|function|class|case)\b|=>")
_LEXICAL_LOGIC = re.compile(
    r"(?m)\b(?:await|return|throw|json|parse|stringify|select|request|response|api|db)\b|"
    r"\.[A-Za-z_$][\w$]*\s*\("
)


def classify_source(path: str, source: str) -> Verdict:
    """Classify source payloads without executing them."""
    suffix = Path(path).suffix.lower()
    if suffix not in _PYTHON_SUFFIXES:
        if _LEXICAL_MUTATION.search(source):
            return Verdict("substantive", "state-mutating source")
        if _LEXICAL_CONTROL.search(source) and _LEXICAL_LOGIC.search(source):
            return Verdict("substantive", "substantive control flow and logic")
        if source.strip() and ("\n" in source.strip() or ";" in source):
            return Verdict("unresolved", "source could not be classified")
        return Verdict("trivial", "no substantive source semantics")
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError):
        return Verdict("unresolved", "source could not be classified")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if _is_mutating_call(node, name):
            return Verdict("substantive", "state-mutating source")
        if name.endswith(".execute") and node.args:
            value = node.args[0]
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                return Verdict("substantive", "state-mutating source")
            if value.value.lstrip().lower().startswith(_MUTATING_SQL):
                return Verdict("substantive", "state-mutating source")
    has_control_flow = any(isinstance(node, _CONTROL_FLOW) for node in ast.walk(tree))
    has_logic = any(_is_logic(node) for node in ast.walk(tree))
    if has_control_flow and has_logic:
        return Verdict("substantive", "substantive control flow and logic")
    return Verdict("trivial", "no substantive source semantics")


_CONTROL_FLOW = (
    ast.AsyncFunctionDef, ast.ClassDef, ast.For, ast.FunctionDef, ast.If,
    ast.Match, ast.Try, ast.While, ast.With,
)


def _is_logic(node: ast.AST) -> bool:
    if isinstance(node, ast.Call):
        return _call_name(node.func) not in {"print"}
    return isinstance(node, (ast.BinOp, ast.BoolOp, ast.Compare, ast.comprehension))


def _literal_words(node: ast.AST) -> list[str]:
    if isinstance(node, (ast.List, ast.Tuple)):
        return [
            item.value for item in node.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        ]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.split()
    return []


def _is_mutating_call(node: ast.Call, name: str) -> bool:
    method = name.rsplit(".", 1)[-1]
    receiver = name.rsplit(".", 1)[0].split(".", 1)[0].lower() if "." in name else ""
    if name in _MUTATING_CALL_NAMES or method in _MUTATING_METHODS:
        return True
    if method in {"delete", "update"} and receiver in _STATEFUL_RECEIVERS:
        return True
    if method in {"rename", "replace", "write"} and receiver in _PATH_RECEIVERS:
        return True
    if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Call):
        constructor = _call_name(node.func.value.func)
        if constructor in {"open", "Path"} and method in {"rename", "replace", "write"}:
            return True
    if name == "open" and len(node.args) >= 2:
        mode = node.args[1]
        return isinstance(mode, ast.Constant) and isinstance(mode.value, str) and any(
            flag in mode.value for flag in "wax+"
        )
    subprocess_calls = {
        "subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.Popen",
    }
    if name in subprocess_calls and node.args:
        words = [word.lower() for word in _literal_words(node.args[0])]
        if not words:
            return True
        command = words[0].rsplit("/", 1)[-1]
        if command in {"rm", "mv", "cp", "install", "tee"}:
            return True
        return command == "git" and any(
            word in {"commit", "push", "merge", "rebase", "reset"} for word in words[1:]
        )
    return False


def terminal_candidates(command: str) -> list[tuple[str, str]]:
    """Extract common non-adversarial heredoc file creations from a shell command."""
    lines = command.splitlines()
    found: list[tuple[str, str]] = []
    for index, line in enumerate(lines):
        delimiter_match = re.search(r"<<-?\s*['\"]?([A-Za-z_][\w-]*)['\"]?", line)
        target_match = re.search(r"(?<![<\d])>(?!>)\s*([^\s]+)", line)
        if not delimiter_match or not target_match:
            continue
        delimiter = delimiter_match.group(1)
        target = target_match.group(1).strip("'\"")
        body: list[str] = []
        terminated = False
        for body_line in lines[index + 1:]:
            if body_line.strip() == delimiter:
                found.append((target, "\n".join(body) + ("\n" if body else "")))
                terminated = True
                break
            body.append(body_line)
        if not terminated:
            found.append((target, "def <unresolved>(:\npass"))
    for line in lines:
        if "<<" in line:
            continue
        try:
            tokens = shlex.split(line)
        except ValueError:
            continue
        if not tokens:
            continue
        executable = tokens[0].rsplit("/", 1)[-1]
        if executable.startswith("python") and "-c" in tokens:
            code_index = tokens.index("-c") + 1
            if code_index >= len(tokens):
                found.append(("<terminal-python>.py", "def <unresolved>(:\npass"))
            else:
                found.append(("<terminal-python>.py", tokens[code_index]))
        redirect = next((i for i, token in enumerate(tokens) if token in {">", ">>"}), -1)
        if redirect <= 0 or redirect + 1 >= len(tokens):
            continue
        if executable == "echo":
            body_tokens = tokens[1:redirect]
        elif executable == "printf" and redirect >= 3:
            body_tokens = tokens[2:redirect]
        else:
            continue
        found.append((tokens[redirect + 1], "\n".join(body_tokens)))
    return found


def patch_candidates(args: dict[str, object]) -> list[tuple[str, str]] | None:
    """Extract code candidates from replace or V4A patch payloads; None means malformed."""
    mode = args.get("mode", "replace")
    if mode == "replace":
        path, content = args.get("path"), args.get("new_string")
        return [(path, content)] if isinstance(path, str) and isinstance(content, str) else None
    if mode != "patch" or not isinstance(args.get("patch"), str):
        return None
    payload = str(args["patch"])
    matches = list(re.finditer(r"(?m)^\*\*\* (?:Add|Update) File: (.+?)\s*$", payload))
    if not matches:
        return None
    found: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(payload)
        segment = payload[match.end():end]
        added_lines = [
            line[1:] for line in segment.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        has_deletion = any(
            line.startswith("-") and not line.startswith("---")
            for line in segment.splitlines()
        )
        added = "\n".join(added_lines)
        if not added_lines and has_deletion:
            added = "def <unresolved>(:\npass"
        found.append((match.group(1).strip(), added))
    return found


def _invocation_candidates(name: str, arguments: object) -> list[tuple[str, str]] | None:
    if not isinstance(arguments, dict):
        return None
    short_name = name.rsplit(".", 1)[-1]
    if short_name == "write_file":
        path, content = arguments.get("path"), arguments.get("content")
        return [(path, content)] if isinstance(path, str) and isinstance(content, str) else None
    if short_name == "patch":
        return patch_candidates(arguments)
    if short_name == "terminal":
        command = arguments.get("command")
        return terminal_candidates(command) if isinstance(command, str) else None
    return []


def embedded_write_candidates(source: str) -> list[tuple[str, str]] | None:
    """Return statically resolvable nested authoring payloads from execute_code."""
    try:
        tree = ast.parse(source, filename="<execute_code>")
    except (SyntaxError, ValueError):
        return None
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        short_name = name.rsplit(".", 1)[-1]
        if short_name not in {"write_file", "patch", "terminal", "tool_call"}:
            continue
        try:
            values = {
                keyword.arg: ast.literal_eval(keyword.value)
                for keyword in node.keywords if keyword.arg is not None
            }
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            return None
        if short_name == "tool_call":
            calls = values.get("calls")
            if not isinstance(calls, list):
                return None
            for call in calls:
                if not isinstance(call, dict):
                    return None
                extracted = _invocation_candidates(str(call.get("name", "")), call.get("arguments"))
                if extracted is None:
                    return None
                found.extend(extracted)
            continue
        extracted = _invocation_candidates(short_name, values)
        if extracted is None:
            return None
        found.extend(extracted)
    return found


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""
