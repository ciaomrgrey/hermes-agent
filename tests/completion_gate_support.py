"""Stage the distribution in a temporary profile and use native discovery."""
import json
from pathlib import Path
import shutil
import sys

SOURCE = Path(__file__).resolve().parents[1] / 'external/completion-gate'


def install(home):
    target = Path(home) / 'plugins/completion-gate'
    shutil.copytree(SOURCE, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__'))
    return target


def load():
    from hermes_constants import get_hermes_home
    from hermes_cli import plugins
    try:
        from hermes_yaml import safe_load
    except ImportError:
        from yaml import safe_load
    home = get_hermes_home()
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
