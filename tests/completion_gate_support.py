"""Stage the distribution in a temporary profile and use native discovery.

Writes ONLY into a fresh temporary home (t_70b901a9). Every actual write destination
is confined, not just the outer plugin directory:

* ``install`` refuses a live Hermes home, a ``plugins`` or ``plugins/completion-gate``
  path that is a symlink, a target that resolves into a live plugin tree, and an
  existing plugin directory this process did not create. A plugin directory it did
  create is removed (``rmtree`` unlinks, never follows, links) and restaged as a fresh
  copy, so no destination file can ever be a link planted after the first install.
* ``load`` refuses a live Hermes home, and a ``config.yaml`` that is a symlink, before
  reading or writing it; the config is written to a new ``O_EXCL|O_NOFOLLOW`` file and
  ``os.replace``d into place, so a hard link to an outside file is never written through.

Live roots: ``tests/estate_live_roots.py``.
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


def _refuse_live_home(home):
    if live_root_for(home) is not None:
        _refuse('it is a live Hermes home', home)


def install(home):
    home = Path(home)
    _refuse_live_home(home)
    plugins_dir = home / 'plugins'
    target = plugins_dir / 'completion-gate'
    for path in (plugins_dir, target):
        if os.path.islink(path):
            _refuse(f'it is a symlink to {os.path.realpath(path)}', path)
    resolved = Path(os.path.realpath(target))
    if inside_live_plugin_tree(resolved):
        _refuse('it resolves into a live plugin tree', resolved)
    if os.path.lexists(target):
        if resolved not in _INSTALLED:
            _refuse('it is an existing plugin directory this test run did not create', resolved)
        # Restage from scratch: never copy over destination files (any could be a link).
        shutil.rmtree(target)
    shutil.copytree(SOURCE, target, symlinks=True, ignore=shutil.ignore_patterns('__pycache__'))
    _INSTALLED.add(resolved)
    return target


def _write_config(config, text):
    """Replace *config* with a new regular file; never writes through a link."""
    if os.path.islink(config):
        _refuse(f'config.yaml is a symlink to {os.path.realpath(config)}', config)
    staging = config.with_name(f'.config.yaml.{os.getpid()}.tmp')
    fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        with os.fdopen(fd, 'w') as fh:
            fh.write(text)
        os.replace(staging, config)
    except BaseException:
        if os.path.lexists(staging):
            os.unlink(staging)
        raise


def load():
    from hermes_constants import get_hermes_home
    from hermes_cli import plugins
    try:
        from hermes_yaml import safe_load
    except ImportError:
        from yaml import safe_load
    home = get_hermes_home()
    _refuse_live_home(home)
    config = home / 'config.yaml'
    if os.path.islink(config):
        _refuse(f'config.yaml is a symlink to {os.path.realpath(config)}', config)
    target = install(home)
    cfg = safe_load(config.read_text()) if config.exists() else {}
    cfg = cfg or {}
    enabled = cfg.setdefault('plugins', {}).setdefault('enabled', [])
    if 'completion-gate' not in enabled:
        enabled.append('completion-gate')
    _write_config(config, json.dumps(cfg))
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
