"""Pure source classification for the domain code admission policy."""
from __future__ import annotations

import ast
from collections import defaultdict
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Verdict:
    kind: str
    reason: str


_PYTHON_SUFFIXES = frozenset({".py", ".pyw"})
_SHELL_SUFFIXES = frozenset({".sh", ".bash", ".zsh", ".fish"})
_MUTATING_CALL_NAMES = frozenset({
    "os.remove", "os.unlink", "os.rename", "os.replace", "os.mkdir", "os.makedirs",
    "os.rmdir", "os.system", "os.chmod", "os.chown", "os.truncate",
    "shutil.move", "shutil.copy", "shutil.copy2", "shutil.copyfile", "shutil.copytree",
    "shutil.rmtree", "requests.post", "requests.put", "requests.patch", "requests.delete",
    "httpx.post", "httpx.put", "httpx.patch", "httpx.delete",
    "urllib.request.urlopen",
})
_MUTATING_METHODS = frozenset({
    "commit", "flush", "hardlink_to", "mkdir", "rmdir", "save", "send", "send_message",
    "sendall", "sendmail", "symlink_to", "touch", "truncate", "unlink", "upload",
    "upload_file", "upload_fileobj", "write_bytes", "write_text", "writelines",
    "to_csv", "to_excel", "to_feather", "to_hdf", "to_json", "to_orc", "to_parquet",
    "to_pickle", "to_sql",
})
_STATEFUL_RECEIVERS = frozenset({
    "api", "client", "collection", "conn", "connection", "cursor", "database", "db",
    "ledger", "repo", "repository", "session", "store", "table",
})
_LEXICAL_MUTATION = re.compile(
    r"(?imx)(?:\b(?:delete|insert|update|drop|alter|create)\s+|"
    r"(?:^|[;&|]\s*)(?:rm|mv|cp|install|tee|touch|mkdir|rmdir|chmod|chown|ln|truncate)\s+|"
    r"\b(?:writeFile(?:Sync)?|appendFile(?:Sync)?|unlink|rename|remove|rmtree|commit)\s*\(|"
    r"(?:^|[;&|]\s*|\$\(\s*|`\s*)git\s+"
    r"(?:add|am|branch|checkout|cherry-pick|clean|commit|merge|mv|"
    r"pull|push|rebase|reset|restore|rm|stash|switch|tag)\b|"
    r"(?:^|[;&|]\s*)git\s+(?:blame|diff|log|show)\b[^\n]*"
    r"(?:\s-o(?:\s|=)|\s--output(?:\s|=))|"
    r"\bcurl\b[^\n]*(?:-X\s*(?:POST|PUT|PATCH|DELETE)|--request\s*=?\s*(?:POST|PUT|PATCH|DELETE)|"
    r"-d\b|--data\b|--upload-file\b|-T\b)|"
    r"\bwget\b[^\n]*(?:--post-data|--post-file|--method\s*=?(?:POST|PUT|PATCH|DELETE))|"
    r"\b(?:fetch|axios\.(?:post|put|patch|delete))\s*\()"
)
_LEXICAL_CONTROL = re.compile(r"(?m)\b(?:if|for|while|try|catch|function|class|case)\b|=>")
_LEXICAL_LOGIC = re.compile(
    r"(?m)\b(?:await|return|throw|json|parse|stringify|select|request|response|api|db)\b|"
    r"\.[A-Za-z_$][\w$]*\s*\("
)


def classify_source(path: str, source: str, *, allow_native_rpc: bool = False) -> Verdict:
    """Classify source payloads without executing them."""
    suffix = Path(path).suffix.lower()
    if suffix not in _PYTHON_SUFFIXES:
        if suffix in _SHELL_SUFFIXES and re.search(r"[<>]\(", source):
            return Verdict("unresolved", "source could not be classified")
        if _LEXICAL_MUTATION.search(source):
            return Verdict("substantive", "state-mutating source")
        if suffix in _SHELL_SUFFIXES and re.search(r"(?:^|\s)(?:\d*)>{1,2}\s*\S", source):
            return Verdict("substantive", "state-mutating source")
        if _LEXICAL_CONTROL.search(source) and _LEXICAL_LOGIC.search(source):
            return Verdict("substantive", "substantive control flow and logic")
        if _is_proven_trivial_non_python(suffix, source):
            return Verdict("trivial", "no substantive source semantics")
        return Verdict("unresolved", "source could not be classified")
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError):
        return Verdict("unresolved", "source could not be classified")
    if _has_ordered_mutation(tree):
        return Verdict("substantive", "state-mutating source")
    has_control_flow = any(isinstance(node, _CONTROL_FLOW) for node in ast.walk(tree))
    has_logic = any(_is_logic(node) for node in ast.walk(tree))
    if has_control_flow and has_logic:
        return Verdict("substantive", "substantive control flow and logic")
    if _is_proven_trivial_python(tree, {}, allow_native_rpc):
        return Verdict("trivial", "no substantive source semantics")
    return Verdict("unresolved", "source could not be classified")


def _has_ordered_mutation(tree: ast.Module) -> bool:
    aliases: dict[str, str] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Import):
            for item in statement.names:
                aliases[item.asname or item.name.split(".", 1)[0]] = item.name
            continue
        if isinstance(statement, ast.ImportFrom) and statement.module:
            for item in statement.names:
                aliases[item.asname or item.name] = f"{statement.module}.{item.name}"
            continue
        for node in ast.walk(statement):
            if not isinstance(node, ast.Call):
                continue
            name = _expand_alias(_call_name(node.func), aliases)
            if _is_mutating_call(node, name):
                return True
            if name.rsplit(".", 1)[-1] in {"execute", "executemany", "executescript"} and node.args:
                value = node.args[0]
                if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                    return True
                statements = [
                    part.lstrip().lower() for part in value.value.split(";") if part.strip()
                ]
                if any(
                    not sql_statement.startswith(("select ", "explain "))
                    for sql_statement in statements
                ):
                    return True
        rebound: set[str] = set()
        for node in ast.walk(statement):
            if isinstance(node, ast.Assign):
                rebound.update(*(_assigned_target_names(item) for item in node.targets))
            elif isinstance(node, ast.AnnAssign):
                rebound.update(_assigned_target_names(node.target))
            elif isinstance(node, ast.NamedExpr):
                rebound.update(_assigned_target_names(node.target))
        for name in rebound:
            aliases.pop(name, None)
    return False


_CONTROL_FLOW = (
    ast.AsyncFunctionDef, ast.ClassDef, ast.For, ast.FunctionDef, ast.If,
    ast.Match, ast.Try, ast.While, ast.With, ast.comprehension,
)


def _is_logic(node: ast.AST) -> bool:
    if isinstance(node, ast.Call):
        return _call_name(node.func) not in {"print"}
    return isinstance(node, (ast.BinOp, ast.BoolOp, ast.Compare, ast.comprehension))


_TRIVIAL_SHELL_COMMANDS = frozenset({
    "echo", "false", "printf", "pwd", "true",
})


def _is_proven_trivial_non_python(suffix: str, source: str) -> bool:
    stripped = source.strip()
    comment_prefixes = ("#",) if suffix in _SHELL_SUFFIXES else ("#", "//")
    if not stripped or all(
        not line.strip() or line.lstrip().startswith(comment_prefixes)
        for line in source.splitlines()
    ):
        return True
    if suffix in _SHELL_SUFFIXES:
        for line in source.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if "$(" in line or "`" in line:
                return False
            tokens = _shell_tokens(line)
            if not tokens or any(
                token in {";", "&", "&&", "||", "|", "<", ">", ">>"}
                for token in tokens
            ):
                return False
            executable = tokens[0].rsplit("/", 1)[-1]
            if tokens[0] != executable or executable not in _TRIVIAL_SHELL_COMMANDS:
                return False
        return True
    return bool(
        re.fullmatch(
            r"(?:console\.log|print)\s*\(\s*"
            r"(?:[-+]?\d+(?:\.\d+)?|true|false|null|undefined|['\"][^'\"]*['\"])"
            r"\s*\);?",
            stripped,
        )
        or re.fullmatch(
            r"(?:const|let|var)?\s*[A-Za-z_$][\w$]*\s*=\s*"
            r"(?:[-+]?\d+(?:\.\d+)?|true|false|null|undefined|['\"][^'\"]*['\"]);?",
            stripped,
        )
    )


_SAFE_IMPORT_MODULES = frozenset({"builtins", "hermes_tools", "io", "json", "os"})
_NATIVE_RPC_FUNCTIONS = frozenset({
    "hermes_tools.patch",
    "hermes_tools.read_file",
    "hermes_tools.terminal",
    "hermes_tools.tool_call",
    "hermes_tools.write_file",
})


def _is_proven_trivial_python(
    tree: ast.Module, aliases: dict[str, str], allow_native_rpc: bool,
) -> bool:
    bound: set[str] = set()
    reassigned: set[str] = set()
    safe_receivers: set[str] = set()
    active_aliases: dict[str, str] = {}

    def safe_binding(node: ast.AST) -> bool:
        if isinstance(node, (ast.Constant, ast.Dict, ast.List, ast.Set, ast.Tuple)):
            return True
        if isinstance(node, ast.Name):
            return node.id in safe_receivers
        return bool(
            isinstance(node, ast.Call)
            and allow_native_rpc
            and _is_imported_native_rpc(node, active_aliases, reassigned)
        )

    def safe_expression(node: ast.AST) -> bool:
        if isinstance(node, ast.Constant):
            return True
        if isinstance(node, ast.Name):
            return node.id in safe_receivers
        if isinstance(node, ast.UnaryOp):
            return safe_expression(node.operand)
        if isinstance(node, ast.BinOp):
            return safe_expression(node.left) and safe_expression(node.right)
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return all(safe_expression(item) for item in node.elts)
        if isinstance(node, ast.Dict):
            return all(
                (key is None or safe_expression(key)) and safe_expression(value)
                for key, value in zip(node.keys, node.values)
            )
        if isinstance(node, ast.Attribute):
            root = _call_name(node).partition(".")[0]
            return active_aliases.get(root) == "os" and node.attr == "O_RDONLY"
        if isinstance(node, ast.Call):
            if not _is_proven_trivial_python_call(
                node, active_aliases, allow_native_rpc, bound, reassigned, safe_receivers,
            ):
                return False
            return all(safe_expression(item) for item in node.args) and all(
                keyword.arg is not None and safe_expression(keyword.value)
                for keyword in node.keywords
            )
        return False

    for statement in tree.body:
        if isinstance(statement, ast.Import):
            if any(item.name not in _SAFE_IMPORT_MODULES for item in statement.names):
                return False
            for item in statement.names:
                local_name = item.asname or item.name.split(".", 1)[0]
                active_aliases[local_name] = item.name
                safe_receivers.discard(local_name)
                bound.add(local_name)
                reassigned.discard(local_name)
            continue
        if isinstance(statement, ast.ImportFrom):
            if (
                statement.level
                or statement.module not in _SAFE_IMPORT_MODULES
                or any(item.name == "*" for item in statement.names)
            ):
                return False
            if statement.module == "hermes_tools" and any(
                f"hermes_tools.{item.name}" not in _NATIVE_RPC_FUNCTIONS
                for item in statement.names
            ):
                return False
            for item in statement.names:
                local_name = item.asname or item.name
                active_aliases[local_name] = f"{statement.module}.{item.name}"
                safe_receivers.discard(local_name)
                bound.add(local_name)
                reassigned.discard(local_name)
            continue
        if isinstance(statement, ast.Assign):
            if not all(isinstance(target, ast.Name) for target in statement.targets):
                return False
            if not safe_expression(statement.value):
                return False
            binding_is_safe = safe_binding(statement.value)
            for target in statement.targets:
                if not isinstance(target, ast.Name):
                    return False
                active_aliases.pop(target.id, None)
                if binding_is_safe:
                    safe_receivers.add(target.id)
                else:
                    safe_receivers.discard(target.id)
                bound.add(target.id)
                reassigned.add(target.id)
            continue
        if isinstance(statement, ast.AnnAssign):
            return False
        if isinstance(statement, ast.Expr):
            if not safe_expression(statement.value):
                return False
            continue
        if isinstance(statement, ast.Pass):
            continue
        return False
    return True


def _is_proven_trivial_python_call(
    node: ast.Call,
    aliases: dict[str, str],
    allow_native_rpc: bool,
    bound_names: set[str],
    reassigned_names: set[str],
    safe_receivers: set[str],
) -> bool:
    raw_name = _call_name(node.func)
    name = _expand_alias(raw_name, aliases)
    root = raw_name.partition(".")[0]
    method = name.rsplit(".", 1)[-1]
    receiver = raw_name.rsplit(".", 1)[0].split(".", 1)[0] if "." in raw_name else ""
    if allow_native_rpc and _is_imported_native_rpc(node, aliases, reassigned_names):
        return True
    if raw_name == name and name not in bound_names and name in {
        "abs", "all", "any", "bool", "dict", "enumerate", "float", "int", "len",
        "list", "max", "min", "print", "range", "repr", "set", "sorted", "str",
        "sum", "tuple", "zip",
    }:
        return True
    if name in {"json.dumps", "json.loads"} and root not in reassigned_names:
        imported = aliases.get(root, "")
        if imported == "json" or imported in {"json.dumps", "json.loads"}:
            callback_keywords = {
                "cls", "default", "object_hook", "object_pairs_hook", "parse_constant",
                "parse_float", "parse_int",
            }
            return not any(keyword.arg in callback_keywords for keyword in node.keywords)
    if receiver in safe_receivers and method in {
        "append", "copy", "count", "endswith", "extend", "find", "get", "index", "items",
        "join", "keys", "lower", "lstrip", "removeprefix", "removesuffix", "rstrip",
        "split", "splitlines", "startswith", "strip", "upper", "values",
    }:
        return True
    if method == "update" and receiver in safe_receivers:
        return True
    if method == "replace" and receiver in safe_receivers:
        return True
    if method == "read" and receiver in safe_receivers:
        return True
    if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Call):
        if method == "read":
            return _is_proven_trivial_python_call(
                node.func.value,
                aliases,
                allow_native_rpc,
                bound_names,
                reassigned_names,
                safe_receivers,
            )
    if name == "os.open":
        return aliases.get(root) == "os" and not _is_mutating_call(node, name)
    if raw_name == "open" and raw_name not in bound_names:
        return not _is_mutating_call(node, name)
    if name in {"builtins.open", "io.open"} and aliases.get(root) in {
        "builtins", "builtins.open", "io", "io.open",
    }:
        return not _is_mutating_call(node, name)
    return False


def _assigned_target_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        return set().union(*(_assigned_target_names(item) for item in node.elts))
    return set()


def _is_literal_memory_value(node: ast.AST) -> bool:
    return isinstance(
        node,
        (
            ast.Constant, ast.Dict, ast.DictComp, ast.List, ast.ListComp,
            ast.Set, ast.SetComp, ast.Tuple,
        ),
    )


def _is_imported_native_rpc(
    node: ast.Call, aliases: dict[str, str], reassigned_names: set[str],
) -> bool:
    raw_name = _call_name(node.func)
    root = raw_name.partition(".")[0]
    imported = aliases.get(root, "")
    return bool(
        root not in reassigned_names
        and (imported == "hermes_tools" or imported.startswith("hermes_tools."))
        and _expand_alias(raw_name, aliases) in _NATIVE_RPC_FUNCTIONS
    )


def _python_bindings(
    tree: ast.AST, aliases: dict[str, str], allow_native_rpc: bool,
) -> tuple[set[str], set[str], set[str]]:
    bound = set(aliases)
    reassigned: set[str] = set()
    assignments: dict[str, list[bool]] = defaultdict(list)
    for node in ast.walk(tree):
        targets: set[str] = set()
        value: ast.AST | None = None
        if isinstance(node, ast.Assign):
            targets = set().union(*(_assigned_target_names(item) for item in node.targets))
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = _assigned_target_names(node.target)
            value = node.value
        elif isinstance(node, ast.NamedExpr):
            targets = _assigned_target_names(node.target)
            value = node.value
        elif isinstance(node, ast.arg):
            targets = {node.arg}
        if not targets:
            continue
        bound.update(targets)
        reassigned.update(targets)
        safe = value is not None and (
            _is_literal_memory_value(value)
            or (
                allow_native_rpc
                and isinstance(value, ast.Call)
                and _is_imported_native_rpc(value, aliases, reassigned - targets)
            )
        )
        for target in targets:
            assignments[target].append(safe)
    safe_receivers = {
        name for name, verdicts in assignments.items() if verdicts and all(verdicts)
    }
    return bound, reassigned, safe_receivers


def _expand_alias(name: str, aliases: dict[str, str]) -> str:
    root, separator, remainder = name.partition(".")
    resolved = aliases.get(root)
    if not resolved:
        return name
    return f"{resolved}.{remainder}" if separator else resolved


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
    if name == "os.open":
        flags = node.args[1] if len(node.args) > 1 else next(
            (item.value for item in node.keywords if item.arg == "flags"), None,
        )
        if isinstance(flags, ast.Constant) and isinstance(flags.value, int):
            return flags.value != 0
        flag_names = {
            item.attr for item in ast.walk(flags) if isinstance(item, ast.Attribute)
        } if flags is not None else set()
        return flag_names != {"O_RDONLY"}
    if name == "print":
        file_target = next((item.value for item in node.keywords if item.arg == "file"), None)
        return file_target is not None
    if name in _MUTATING_CALL_NAMES or method in _MUTATING_METHODS:
        return True
    if method in {"post", "put", "delete"}:
        return True
    if method == "patch":
        return "." in name and name != "hermes_tools.patch"
    if method == "request":
        request_method = node.args[0] if node.args else next(
            (item.value for item in node.keywords if item.arg == "method"), None,
        )
        if not isinstance(request_method, ast.Constant) or not isinstance(request_method.value, str):
            return True
        return request_method.value.upper() in {"POST", "PUT", "PATCH", "DELETE"}
    if method == "update" and receiver in _STATEFUL_RECEIVERS:
        return True
    if method in {"rename", "chmod", "chown", "unlink", "touch", "mkdir", "rmdir"}:
        return True
    if method == "replace" and receiver not in {"text", "string", "str", "value", "content"}:
        return True
    if method == "write":
        return True
    if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Call):
        constructor = _call_name(node.func.value.func)
        if constructor in {"open", "Path"} and method in {"rename", "replace", "write"}:
            return True
    if name != "os.open" and (name == "open" or method == "open"):
        is_function_open = (
            isinstance(node.func, ast.Name) and node.func.id == "open"
        ) or name in {"builtins.open", "io.open", "codecs.open"}
        positional_index = 1 if is_function_open else 0
        mode_node = node.args[positional_index] if len(node.args) > positional_index else next(
            (item.value for item in node.keywords if item.arg == "mode"), None,
        )
        if mode_node is None:
            return False
        if not isinstance(mode_node, ast.Constant) or not isinstance(mode_node.value, str):
            return True
        return any(flag in mode_node.value for flag in "wax+")
    subprocess_calls = {
        "subprocess.run", "subprocess.call", "subprocess.check_call",
        "subprocess.check_output", "subprocess.Popen",
    }
    if name in subprocess_calls:
        shell_node = next((item.value for item in node.keywords if item.arg == "shell"), None)
        if not isinstance(shell_node, ast.Constant) and shell_node is not None:
            return True
        if isinstance(shell_node, ast.Constant) and shell_node.value is True:
            return True
        command_node = node.args[0] if node.args else next(
            (item.value for item in node.keywords if item.arg == "args"), None,
        )
        if command_node is None:
            return True
        words = [word.lower() for word in _literal_words(command_node)]
        if not words:
            return True
        command = words[0].rsplit("/", 1)[-1]
        if command in {"sh", "bash", "zsh", "csh", "fish", "pwsh", "powershell", "cmd"}:
            return True
        if command in {
            "rm", "mv", "cp", "install", "tee", "touch", "mkdir", "rmdir",
            "chmod", "chown", "ln", "truncate",
        }:
            return True
        if command == "git":
            if not words[1:] or words[1] not in {
                "status", "log", "diff", "show", "rev-parse", "ls-files", "blame",
            }:
                return True
            return any(
                word == "-o" or word.startswith("--output") for word in words[2:]
            )
        return command not in {
            "ps", "ls", "pwd", "cat", "head", "tail", "grep", "rg", "wc", "which",
            "date", "printf", "echo", "true", "false",
        }
    return False


def _shell_tokens(line: str) -> list[str] | None:
    try:
        return list(shlex.shlex(line, posix=True, punctuation_chars="|><;&"))
    except ValueError:
        return None


def _command_segment(tokens: list[str], end: int | None = None) -> list[str]:
    end = len(tokens) if end is None else end
    start = 0
    for index, token in enumerate(tokens[:end]):
        if token in {";", "&&", "||", "|"}:
            start = index + 1
    return tokens[start:end]


def _command_segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = []
    start = 0
    for index, token in enumerate(tokens):
        if token in {";", "&&", "||", "|"}:
            if tokens[start:index]:
                segments.append(tokens[start:index])
            start = index + 1
    if tokens[start:]:
        segments.append(tokens[start:])
    return segments


def _python_segment(segment: list[str]) -> list[str] | None:
    for index, token in enumerate(segment):
        if not token.rsplit("/", 1)[-1].startswith("python"):
            continue
        if index == 0:
            return segment
        prefix = segment[:index]
        allowed_launchers = {"env", "command", "uv", "run"}
        if all(
            re.fullmatch(r"[A-Za-z_]\w*=.*", token)
            or token.rsplit("/", 1)[-1] in allowed_launchers
            or token.startswith("-")
            for token in prefix
        ):
            return segment[index:]
        return None
    return None


def _python_inline_source(segment: list[str]) -> str | None:
    for index, token in enumerate(segment[1:], start=1):
        if token == "-c":
            return segment[index + 1] if index + 1 < len(segment) else "def <unresolved>(:\npass"
        clustered = re.fullmatch(r"-[bBdEhiIOPqRsSuvVx]*c(.*)", token)
        if clustered:
            return clustered.group(1) or (
                segment[index + 1] if index + 1 < len(segment) else "def <unresolved>(:\npass"
            )
        if token == "<<<":
            return segment[index + 1] if index + 1 < len(segment) else "def <unresolved>(:\npass"
    return None


def _assignments_before(
    tokens: list[str], end: int, existing: dict[str, str],
) -> dict[str, str]:
    found = dict(existing)
    for token in tokens[:end]:
        match = re.fullmatch(r"([A-Za-z_]\w*)=(.*)", token)
        if match:
            found[match.group(1)] = match.group(2)
    return found


def _base_before(tokens: list[str], end: int, initial: Path) -> Path | None:
    base = initial
    start = 0
    previous_separator: str | None = None
    for index, token in enumerate(tokens[:end]):
        if token not in {";", "&&", "||", "|"}:
            continue
        segment = tokens[start:index]
        if token != "|" and segment and segment[0] == "cd":
            if token == "||" or previous_separator in {"&&", "||"}:
                return None
            if len(segment) != 2 or "$" in segment[1]:
                return None
            changed = Path(segment[1]).expanduser()
            candidate = (changed if changed.is_absolute() else base / changed).resolve()
            if not candidate.is_dir():
                return None
            base = candidate
        start = index + 1
        previous_separator = token
    if end == len(tokens):
        segment = tokens[start:end]
        if segment and segment[0] == "cd":
            if previous_separator in {"&&", "||"}:
                return None
            if len(segment) != 2 or "$" in segment[1]:
                return None
            changed = Path(segment[1]).expanduser()
            candidate = (changed if changed.is_absolute() else base / changed).resolve()
            if not candidate.is_dir():
                return None
            base = candidate
    return base


def _tee_targets(tokens: list[str]) -> tuple[list[str], bool] | None:
    append = False
    targets: list[str] = []
    parse_options = True
    for item in tokens:
        if parse_options and item == "--":
            parse_options = False
        elif parse_options and item in {"-a", "--append"}:
            append = True
        elif parse_options and item in {"-i", "--ignore-interrupts"}:
            continue
        elif parse_options and item.startswith("-"):
            return None
        else:
            parse_options = False
            targets.append(item)
    return targets, append


def _resolve_shell_target(target: str, base: Path, variables: dict[str, str]) -> str | None:
    match = re.fullmatch(r"\$(\w+)|\$\{(\w+)\}", target)
    if match:
        target = variables.get(match.group(1) or match.group(2), "")
    if not target or "$" in target:
        return None
    path = Path(target).expanduser()
    if not path.is_absolute():
        path = base / path
    return str(path.absolute())


def _shell_literal_body(tokens: list[str]) -> str | None:
    if not tokens:
        return None
    executable = tokens[0].rsplit("/", 1)[-1]
    if executable == "echo":
        start = 2 if len(tokens) > 1 and tokens[1] in {"-n", "-e"} else 1
        body = " ".join(tokens[start:])
        return body if "-n" in tokens[1:start] else body + "\n"
    if executable == "printf":
        try:
            rendered = subprocess.run(
                ["printf", *tokens[1:]], check=True, capture_output=True,
                text=True, timeout=1,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError):
            return None
        return rendered.stdout

def _effective_redirect_content(path: str, body: str, append: bool) -> str:
    if not append:
        return body
    try:
        return Path(path).expanduser().resolve().read_text() + body
    except (OSError, UnicodeError):
        return "def <unresolved>(:\npass"


def _pipeline_body(tokens: list[str], stop: int) -> str | None:
    start = 0
    for index, token in enumerate(tokens[:stop]):
        if token in {";", "&&", "||"}:
            start = index + 1
    pipeline = tokens[start:stop]
    segments: list[list[str]] = []
    segment_start = 0
    for index, token in enumerate(pipeline):
        if token == "|":
            segments.append(pipeline[segment_start:index])
            segment_start = index + 1
    segments.append(pipeline[segment_start:])
    body = _shell_literal_body(segments[0]) if segments else None
    if body is None:
        return None
    for segment in segments[1:]:
        executable = segment[0].rsplit("/", 1)[-1] if segment else ""
        if executable not in {"cat", "tee"}:
            return None
    return body


def terminal_candidates(
    command: str, workdir: str | None = None,
) -> list[tuple[str, str, bool]] | None:
    """Extract common shell authoring in execution order."""
    lines = command.splitlines()
    found: list[tuple[str, str, bool]] = []
    projected: dict[str, str] = {}
    variables: dict[str, str] = {}
    base = Path(workdir).expanduser().resolve() if workdir else Path.cwd()

    def add_file_candidate(target: str, body: str, append: bool) -> None:
        canonical = str(Path(target).expanduser().resolve())
        if append and canonical in projected:
            content = projected[canonical] + body
        else:
            content = _effective_redirect_content(target, body, append)
        projected[canonical] = content
        found.append((target, content, False))

    index = 0
    while index < len(lines):
        line = lines[index]
        body_source: str | None = None
        next_index = index + 1
        delimiter_match = re.search(
            r"(?<!<)(<<-?)(?!<)\s*['\"]?([A-Za-z_][\w-]*)['\"]?", line,
        )
        if delimiter_match:
            strip_tabs = delimiter_match.group(1) == "<<-"
            delimiter = delimiter_match.group(2)
            body: list[str] = []
            end_index = None
            for body_index, body_line in enumerate(lines[index + 1:], start=index + 1):
                candidate = body_line.lstrip("\t") if strip_tabs else body_line
                if candidate == delimiter:
                    end_index = body_index
                    break
                body.append(body_line)
            if end_index is None:
                body_source = "def <unresolved>(:\npass"
                next_index = len(lines)
            else:
                body_source = "\n".join(body) + ("\n" if body else "")
                next_index = end_index + 1

        tokens = _shell_tokens(line)
        while tokens is None and next_index < len(lines) and delimiter_match is None:
            line += "\n" + lines[next_index]
            next_index += 1
            tokens = _shell_tokens(line)
        if tokens is None:
            return None
        if not tokens:
            index = next_index
            continue

        for raw_segment in _command_segments(tokens):
            segment = _python_segment(raw_segment)
            if segment is None:
                continue
            inline_source = _python_inline_source(segment)
            if inline_source is not None:
                found.append(("<terminal-python>.py", inline_source, True))
            elif body_source is not None:
                found.append(("<terminal-python>.py", body_source, True))

        segment_start = 0
        for segment_end in range(len(tokens) + 1):
            at_boundary = (
                segment_end == len(tokens)
                or tokens[segment_end] in {";", "&&", "||", "|"}
            )
            if not at_boundary:
                continue
            segment = tokens[segment_start:segment_end]
            executable = segment[0].rsplit("/", 1)[-1] if segment else ""
            is_pipeline_fed = segment_start > 0 and tokens[segment_start - 1] == "|"
            if executable == "tee" and not is_pipeline_fed:
                redirect_index = next(
                    (i for i, item in enumerate(segment) if item.startswith("<")),
                    len(segment),
                )
                parsed_tee = _tee_targets(segment[1:redirect_index])
                if parsed_tee is None:
                    return None
                tee_args, append = parsed_tee
                direct_body = body_source
                if direct_body is None and "<<<" in segment:
                    here_index = segment.index("<<<") + 1
                    direct_body = (
                        segment[here_index] + "\n" if here_index < len(segment)
                        else "def <unresolved>(:\npass"
                    )
                if direct_body is None:
                    direct_body = "def <unresolved>(:\npass"
                target_base = _base_before(tokens, segment_start, base)
                if target_base is None:
                    return None
                local_variables = _assignments_before(tokens, segment_start, variables)
                for tee_arg in tee_args:
                    target = _resolve_shell_target(tee_arg, target_base, local_variables)
                    if target is None:
                        return None
                    add_file_candidate(target, direct_body, append)
            segment_start = segment_end + 1

        for redirect, token in enumerate(tokens):
            if token not in {">", ">>", "&>", "&>>", ">&"} or redirect + 1 >= len(tokens):
                continue
            target_base = _base_before(tokens, redirect, base)
            if target_base is None:
                return None
            local_variables = _assignments_before(tokens, redirect, variables)
            target = _resolve_shell_target(
                tokens[redirect + 1], target_base, local_variables,
            )
            if target is None:
                return None
            redirected_body = body_source
            if redirected_body is None:
                redirected_body = (
                    _pipeline_body(tokens, redirect)
                    if "|" in tokens[:redirect]
                    else _shell_literal_body(_command_segment(tokens, redirect))
                )
            if redirected_body is None:
                redirected_body = "def <unresolved>(:\npass"
            add_file_candidate(target, redirected_body, token in {">>", "&>>"})

        for pipe, token in enumerate(tokens):
            if token != "|" or pipe + 1 >= len(tokens):
                continue
            if tokens[pipe + 1].rsplit("/", 1)[-1] != "tee":
                continue
            end = pipe + 2
            while end < len(tokens) and tokens[end] not in {"|", ";", "&&", "||"}:
                end += 1
            parsed_tee = _tee_targets(tokens[pipe + 2:end])
            if parsed_tee is None:
                return None
            tee_args, append = parsed_tee
            piped_body = body_source or _pipeline_body(tokens, pipe)
            if piped_body is None:
                piped_body = "def <unresolved>(:\npass"
            target_base = _base_before(tokens, pipe, base)
            if target_base is None:
                return None
            local_variables = _assignments_before(tokens, pipe, variables)
            for tee_arg in tee_args:
                target = _resolve_shell_target(tee_arg, target_base, local_variables)
                if target is None:
                    return None
                add_file_candidate(target, piped_body, append)

        updated_base = _base_before(tokens, len(tokens), base)
        if updated_base is None:
            return None
        base = updated_base
        variables = _assignments_before(tokens, len(tokens), variables)
        index = next_index
    return found


def patch_candidates(args: dict[str, object]) -> list[tuple[str, str]] | None:
    """Extract code candidates from replace or V4A patch payloads; None means malformed."""
    mode = args.get("mode", "replace")
    if mode == "replace":
        path = args.get("path")
        old_string = args.get("old_string")
        new_string = args.get("new_string")
        replace_all = args.get("replace_all", False)
        if (
            not isinstance(path, str) or not isinstance(old_string, str)
            or not isinstance(new_string, str) or not isinstance(replace_all, bool)
        ):
            return None
        try:
            current = Path(path).expanduser().resolve().read_text()
            from tools.fuzzy_match import fuzzy_find_and_replace
            effective, count, _strategy, error = fuzzy_find_and_replace(
                current, old_string, new_string, replace_all,
            )
        except (OSError, UnicodeError, ValueError):
            return None
        return [(path, effective)] if count and not error else None
    if mode != "patch" or not isinstance(args.get("patch"), str):
        return None
    try:
        from tools.fuzzy_match import fuzzy_find_and_replace, is_already_applied
        from tools.patch_parser import OperationType, parse_v4a_patch
        operations, parse_error = parse_v4a_patch(str(args["patch"]))
    except (ImportError, ValueError, TypeError):
        return None
    if parse_error or not operations:
        return None
    found: list[tuple[str, str]] = []
    overlay: dict[str, str] = {}
    removed: set[str] = set()

    def read_effective(path: str) -> str | None:
        canonical = str(Path(path).expanduser().resolve())
        if canonical in overlay:
            return overlay[canonical]
        if canonical in removed:
            return None
        try:
            return Path(canonical).read_text()
        except (OSError, UnicodeError):
            return None

    for operation in operations:
        path = operation.file_path
        canonical = str(Path(path).expanduser().resolve())
        if operation.operation is OperationType.ADD:
            effective = "\n".join(
                line.content for hunk in operation.hunks
                for line in hunk.lines if line.prefix == "+"
            )
            overlay[canonical] = effective
            removed.discard(canonical)
            found.append((path, effective))
            continue
        if operation.operation is OperationType.UPDATE:
            effective = read_effective(path)
            if effective is None:
                return None
            saw_deletion = False
            saw_addition = False
            for hunk in operation.hunks:
                saw_deletion = saw_deletion or any(line.prefix == "-" for line in hunk.lines)
                saw_addition = saw_addition or any(line.prefix == "+" for line in hunk.lines)
                search_lines = [line.content for line in hunk.lines if line.prefix != "+"]
                replace_lines = [line.content for line in hunk.lines if line.prefix != "-"]
                if search_lines == replace_lines:
                    continue
                if not search_lines:
                    insertion = "\n".join(replace_lines)
                    if hunk.context_hint:
                        occurrences = sum(
                            1 for index in range(len(effective) + 1)
                            if effective.startswith(hunk.context_hint, index)
                        )
                        if occurrences != 1:
                            return None
                        hint_start = effective.find(hunk.context_hint)
                        eol = effective.find("\n", hint_start)
                        if eol == -1:
                            effective = effective + "\n" + insertion
                        else:
                            effective = (
                                effective[:eol + 1] + insertion + "\n"
                                + effective[eol + 1:]
                            )
                    else:
                        effective = effective.rstrip("\n") + "\n" + insertion + "\n"
                    continue
                search = "\n".join(search_lines)
                replacement = "\n".join(replace_lines)
                projected, count, _strategy, error = fuzzy_find_and_replace(
                    effective, search, replacement, replace_all=False,
                )
                if error or not count:
                    if is_already_applied(effective, search, replacement):
                        continue
                    return None
                effective = projected
            overlay[canonical] = effective
            found.append((
                path,
                "def <unresolved>(:\npass" if saw_deletion and not saw_addition else effective,
            ))
            continue
        if operation.operation is OperationType.DELETE:
            if read_effective(path) is None:
                return None
            removed.add(canonical)
            overlay.pop(canonical, None)
            found.append((path, "def <unresolved>(:\npass"))
            continue
        if operation.operation is OperationType.MOVE:
            if not operation.new_path:
                return None
            effective = read_effective(path)
            if effective is None:
                return None
            destination = str(Path(operation.new_path).expanduser().resolve())
            overlay[destination] = effective
            removed.add(canonical)
            overlay.pop(canonical, None)
            found.extend([
                (path, "def <unresolved>(:\npass"),
                (operation.new_path, effective),
            ])
    return found


def _invocation_candidates(name: str, arguments: object) -> list[tuple[str, str, bool]] | None:
    if not isinstance(arguments, dict):
        return None
    short_name = name.rsplit(".", 1)[-1]
    if short_name == "write_file":
        path, content = arguments.get("path"), arguments.get("content")
        return (
            [(path, content, False)]
            if isinstance(path, str) and isinstance(content, str) else None
        )
    if short_name == "patch":
        extracted = patch_candidates(arguments)
        return (
            [(path, content, False) for path, content in extracted]
            if extracted is not None else None
        )
    if short_name == "terminal":
        command = arguments.get("command")
        workdir = arguments.get("workdir")
        if not isinstance(command, str) or (workdir is not None and not isinstance(workdir, str)):
            return None
        return terminal_candidates(command, workdir)
    return []


def embedded_write_candidates(source: str) -> list[tuple[str, str, bool]] | None:
    """Return statically resolvable nested authoring payloads from execute_code."""
    try:
        tree = ast.parse(source, filename="<execute_code>")
    except (SyntaxError, ValueError):
        return None
    found: list[tuple[str, str, bool]] = []
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
