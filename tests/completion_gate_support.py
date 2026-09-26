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
    import yaml
    home = get_hermes_home()
    target = install(home)
    config = home / 'config.yaml'
    cfg = yaml.safe_load(config.read_text()) if config.exists() else {}
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
