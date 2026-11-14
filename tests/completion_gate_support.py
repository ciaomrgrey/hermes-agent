"""Stage the distribution in a temporary profile and use native discovery.

Writes ONLY into a fresh temporary home (t_70b901a9): ``install`` refuses a target
that resolves (following symlinks) into a live plugin tree or a plugin directory this
process did not create, and ``load`` refuses a live Hermes home before touching its
``config.yaml``. Live roots: ``tests/estate_live_roots.py``.
"""
import json
import os
from pathlib import Path
import shutil
import sys

from tests.estate_live_roots import inside_live_plugin_tree, live_root_for

SOURCE = Path(__file__).resolve().parents[1] / 'external/completion-gate'

# Resolved plugin dirs this process created; re-installing those is allowed.
_INSTALLED = set()


def _refuse(why, path):
    raise RuntimeError(f'completion_gate_support: refusing to write {path}: {why}. '
                       'Point HERMES_HOME (or the install home) at a temporary directory.')


def install(home):
    home = Path(home)
    if live_root_for(home) is not None:
        _refuse('it is a live Hermes home', home)
    target = home / 'plugins/completion-gate'
    if os.path.islink(target):
        _refuse(f'it is a symlink to {os.path.realpath(target)}', target)
    resolved = Path(os.path.realpath(target))
    if inside_live_plugin_tree(resolved):
        _refuse('it resolves into a live plugin tree', resolved)
    if resolved.exists() and resolved not in _INSTALLED:
        _refuse('it is an existing plugin directory this test run did not create', resolved)
    shutil.copytree(SOURCE, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__'))
    _INSTALLED.add(resolved)
    return target


def load():
    from hermes_constants import get_hermes_home
    from hermes_cli import plugins
    try:
        from hermes_yaml import safe_load
    except ImportError:
        from yaml import safe_load
    home = get_hermes_home()
    if live_root_for(home) is not None:
        _refuse('HERMES_HOME is a live Hermes home', home)
    target = install(home)
    config = home / 'config.yaml'
    cfg = safe_load(config.read_text()) if config.exists() else {}
    cfg = cfg or {}
    enabled = cfg.setdefault('plugins', {}).setdefault('enabled', [])
    if 'completion-gate' not in enabled:
        enabled.append('completion-gate')
    config.write_text(json.dumps(cfg))
    manager = plugins.PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins['completion-gate']
    assert loaded.enabled and loaded.module is not None, loaded.error
    assert loaded.manifest.source == 'user'
    assert Path(loaded.module.__file__).parent == target
    return loaded.module


def module(name):
    plugin = load()
    return sys.modules[plugin.__name__ + '.' + name]


def no_directive(value):
    """Delivery contract: no block directive. A hook result is None or exactly
    ``{"gate_outcome": {...}}`` (display-only, ignored by control flow); a list of
    results (``invoke_hook``) must contain only such entries."""
    def single(item):
        return item is None or (isinstance(item, dict) and set(item) == {"gate_outcome"}
                                and isinstance(item["gate_outcome"], dict)
                                and item["gate_outcome"].get("action") != "block")
    if isinstance(value, list):
        return all(single(item) and item is not None for item in value)
    return single(value)
