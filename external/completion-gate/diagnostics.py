"""Closed-vocabulary diagnostics. Never persist child text, even class/model names."""
import hashlib
import math
import re
import subprocess

# Keys are the only exception names permitted across the audit boundary.
CAUSES = {
    'ContentFiltered': 'content_filtered',
    'TimeoutExpired': 'child_timeout', 'TimeoutError': 'aux_timeout',
    'APITimeoutError': 'aux_timeout', 'ReadTimeout': 'aux_timeout',
    'ConnectTimeout': 'aux_timeout', 'APIConnectionError': 'transport',
    'ConnectionError': 'transport', 'ConnectError': 'transport',
    'RemoteProtocolError': 'transport', 'InternalServerError': 'server',
    'AuthenticationError': 'authentication', 'PermissionDeniedError': 'permission',
    'UnscopedSecretError': 'scope', 'RateLimitError': 'rate_limit',
    'ModuleNotFoundError': 'import', 'ImportError': 'import',
    'FileNotFoundError': 'missing_file', 'JSONDecodeError': 'invalid_response',
    'ValueError': 'invalid_response', 'TypeError': 'invalid_response',
    'AttributeError': 'invalid_response', 'KeyError': 'invalid_response',
    'BadRequestError': 'request', 'NotFoundError': 'request',
    'APIStatusError': 'provider', 'APIError': 'provider',
    'HTTPStatusError': 'provider', 'RuntimeError': 'runtime',
    'CalledProcessError': 'child_exit', 'unknown': 'unknown',
}
PROVIDERS = {'openai', 'openai-codex', 'anthropic', 'anthropic-oauth',
             'google', 'gemini', 'nous', 'openrouter', 'custom', 'xai',
             'copilot', 'zai', 'minimax', 'deepseek', 'unknown'}
TRANSIENT = {'aux_timeout', 'child_timeout', 'transport', 'server'}


class ContentFiltered(Exception):
    """The provider refused extraction; never retry or route around its filter."""


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest() if isinstance(value, str) and value else None


def sanitize(data):
    """Revalidate at every ingress; arbitrary dictionaries are never audit payloads."""
    data = data if isinstance(data, dict) else {}
    name = data.get('exception_class')
    name = name if isinstance(name, str) and name in CAUSES else 'unknown'
    provider = data.get('aux_provider')
    model = data.get('aux_model_sha256')
    elapsed = data.get('elapsed_s', 0)
    code = data.get('child_exit_code')
    attempt = data.get('attempt', 1)
    return {
        'exception_class': name, 'cause': CAUSES[name],
        'child_exit_code': code if type(code) is int and -255 <= code <= 255 else None,
        'elapsed_s': round(min(86400, max(0, elapsed)), 3)
            if type(elapsed) in (int, float) and math.isfinite(elapsed) else 0,
        'attempt': attempt if type(attempt) is int and attempt in (1, 2) else 1,
        'aux_provider': provider if isinstance(provider, str) and provider in PROVIDERS else 'unknown',
        'aux_model_sha256': model if isinstance(model, str) and re.fullmatch('[a-f0-9]{64}', model) else None,
    }


class ExtractionError(Exception):
    def __init__(self, diagnostics):
        self.diagnostics = sanitize(diagnostics)
        super().__init__(self.diagnostics['cause'])


def failure(exc, *, elapsed=0.0, route=None, attempt=1):
    if isinstance(exc, ExtractionError):
        return sanitize(exc.diagnostics)
    name = type(exc).__name__
    code = None
    if isinstance(exc, subprocess.CalledProcessError):
        code = exc.returncode
        # Legacy/early-startup traceback: only a terminal class token, not its message.
        text = exc.stderr
        if isinstance(text, bytes):
            text = text.decode('utf-8', errors='replace')
        matches = re.findall(r'^(?:[A-Za-z_][\w]*\.)*([A-Za-z_][\w]*):', (text or '')[-8192:], re.MULTILINE)
        if matches:
            name = matches[-1]
    route = route or {}
    return sanitize({'exception_class': name, 'child_exit_code': code,
                     'elapsed_s': elapsed, 'attempt': attempt,
                     'aux_provider': route.get('provider'),
                     'aux_model_sha256': fingerprint(route.get('model'))})
