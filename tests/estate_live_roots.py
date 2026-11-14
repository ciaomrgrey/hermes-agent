"""Estate test-harness extension: live Hermes roots that are not the platform default.

Upstream's harness treats only the platform default (``~/.hermes``) and its
``profiles/<name>`` as production. This estate serves from a custom root, so a
Kanban worker's ``HERMES_HOME=<estate>/profiles/<name>`` looked "custom" to the
harness and tests ran against the live home (t_70b901a9). This module only ADDS
roots; it never narrows the upstream checks.

Live roots = the built-in estate root(s) below, plus any ``os.pathsep``-separated
paths in ``HERMES_TEST_LIVE_ROOTS`` (used by tests to model an estate in a tmp dir).
"""
import os
from pathlib import Path

ESTATE_LIVE_ROOTS = ('/Users/claudia/hermes/home',)
LIVE_ROOTS_ENV = 'HERMES_TEST_LIVE_ROOTS'


def _resolve(path):
    return Path(os.path.realpath(os.path.expanduser(str(path))))


def live_roots():
    """Resolved live roots: built-in estate roots plus ``HERMES_TEST_LIVE_ROOTS``."""
    raw = list(ESTATE_LIVE_ROOTS)
    raw += [p for p in os.environ.get(LIVE_ROOTS_ENV, '').split(os.pathsep) if p.strip()]
    roots = []
    for item in raw:
        try:
            resolved = _resolve(item.strip())
        except (OSError, ValueError):
            continue
        if resolved not in roots:
            roots.append(resolved)
    return roots


def live_root_for(home):
    """The live root when *home* IS a live Hermes home (the root or ``<root>/profiles/<name>``)."""
    if not home:
        return None
    try:
        resolved = _resolve(home)
    except (OSError, ValueError):
        return None
    for root in live_roots():
        if resolved == root or (resolved.parent.name == 'profiles' and resolved.parent.parent == root):
            return root
    return None


def inside_live_plugin_tree(path):
    """True when *path* resolves into ``<root>/plugins`` or ``<root>/profiles/<name>/plugins``."""
    resolved = _resolve(path)
    for root in live_roots():
        try:
            parts = resolved.relative_to(root).parts
        except ValueError:
            continue
        if parts[:1] == ('plugins',) or (len(parts) >= 3 and parts[0] == 'profiles' and parts[2] == 'plugins'):
            return True
    return False
