"""Gate test support can never write into a live plugin tree (t_70b901a9).

Incident 14 Nov: ``tests/completion_gate_support.load()`` ran with
HERMES_HOME=<estate>/profiles/cody, whose ``plugins/completion-gate`` is a symlink
to the live package, and ``install()`` copied an unreviewed worktree over it.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[2]


def _digest(root):
    h = hashlib.sha256()
    for p in sorted(Path(root).rglob('*')):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file():
            h.update(p.read_bytes())
            h.update(oct(p.stat().st_mode).encode())
    return h.hexdigest()


def _estate(tmp_path):
    """A disposable estate shaped like /Users/claudia/hermes/home."""
    root = tmp_path / 'estate'
    sentinel = root / 'plugins/completion-gate'
    sentinel.mkdir(parents=True)
    (sentinel / '__init__.py').write_text('LIVE = "reviewed"\n')
    (sentinel / 'plugin.yaml').write_text('name: completion-gate\n')
    (root / 'config.yaml').write_text('model: root\n')
    profile = root / 'profiles/cody'
    (profile / 'plugins').mkdir(parents=True)
    (profile / 'plugins/completion-gate').symlink_to(sentinel, target_is_directory=True)
    (profile / 'config.yaml').write_text('model: keep\n')
    return root, profile, sentinel


def test_install_refuses_profile_whose_plugin_symlinks_to_a_sentinel(tmp_path):
    from tests.completion_gate_support import install
    profile = tmp_path / 'profiles/cody'
    (profile / 'plugins').mkdir(parents=True)
    sentinel = tmp_path / 'sentinel'
    sentinel.mkdir()
    (sentinel / 'MARKER').write_text('untouched\n')
    (profile / 'plugins/completion-gate').symlink_to(sentinel, target_is_directory=True)
    before = _digest(sentinel)
    with pytest.raises(RuntimeError, match='refusing'):
        install(profile)
    assert _digest(sentinel) == before
    assert sorted(p.name for p in sentinel.iterdir()) == ['MARKER']


def test_load_from_live_profile_home_touches_neither_package_nor_config(tmp_path, monkeypatch):
    from tests import completion_gate_support as support
    root, profile, sentinel = _estate(tmp_path)
    monkeypatch.setenv('HERMES_TEST_LIVE_ROOTS', str(root))
    monkeypatch.setenv('HERMES_HOME', str(profile))
    before = _digest(sentinel)
    with pytest.raises(RuntimeError, match='refusing'):
        support.load()
    assert _digest(sentinel) == before
    assert (profile / 'config.yaml').read_text() == 'model: keep\n'


def test_install_refuses_a_live_home_even_without_a_symlink(tmp_path, monkeypatch):
    from tests.completion_gate_support import install
    root, profile, _ = _estate(tmp_path)
    monkeypatch.setenv('HERMES_TEST_LIVE_ROOTS', str(root))
    (profile / 'plugins/completion-gate').unlink()
    with pytest.raises(RuntimeError, match='refusing'):
        install(profile)
    assert not (profile / 'plugins/completion-gate').exists()
    with pytest.raises(RuntimeError, match='refusing'):
        install(root)


def test_install_refuses_a_pre_existing_plugin_dir_it_did_not_create(tmp_path):
    from tests.completion_gate_support import install
    existing = tmp_path / 'plugins/completion-gate'
    existing.mkdir(parents=True)
    (existing / 'MARKER').write_text('someone else\n')
    before = _digest(existing)
    with pytest.raises(RuntimeError, match='refusing'):
        install(tmp_path)
    assert _digest(existing) == before


def test_install_into_a_fresh_temp_home_still_works_and_is_repeatable(tmp_path):
    from tests.completion_gate_support import SOURCE, install
    target = install(tmp_path)
    assert target == tmp_path / 'plugins/completion-gate'
    assert (target / '__init__.py').read_bytes() == (SOURCE / '__init__.py').read_bytes()
    # Re-installing what this process installed (module() is called repeatedly per test).
    assert install(tmp_path) == target


def test_production_detector_recognises_the_estate_root_and_keeps_upstream(tmp_path, monkeypatch):
    import tests.conftest as harness
    root, profile, _ = _estate(tmp_path)
    monkeypatch.setenv('HERMES_TEST_LIVE_ROOTS', str(root))
    assert harness._hermes_home_points_at_production(str(root))
    assert harness._hermes_home_points_at_production(str(profile))
    # Upstream behaviour is unchanged: platform default still production, custom homes honoured.
    assert harness._hermes_home_points_at_production('')
    assert harness._hermes_home_points_at_production(os.path.expanduser('~/.hermes'))
    assert not harness._hermes_home_points_at_production(str(tmp_path / 'custom'))
    assert not harness._hermes_home_points_at_production(str(root / 'profiles/cody/cache/scratch'))


def test_built_in_estate_root_is_live():
    from tests.estate_live_roots import live_root_for
    assert live_root_for('/Users/claudia/hermes/home') == Path('/Users/claudia/hermes/home').resolve()
    assert live_root_for('/Users/claudia/hermes/home/profiles/cody') == Path('/Users/claudia/hermes/home').resolve()
    assert live_root_for('/Users/claudia/hermes/home/profiles/cody/cache/scratch/x') is None


def test_session_sandbox_engages_for_a_live_estate_profile_and_guards_keep_the_estate(tmp_path):
    """A pytest/conftest import under HERMES_HOME=<estate>/profiles/cody is sandboxed, and
    the kanban/state.db deny-lists still name the estate root (not just ~/.hermes)."""
    root, profile, sentinel = _estate(tmp_path)
    before = _digest(sentinel)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('PYTEST_', 'HERMES_')) and k != 'PYTHONSAFEPATH'}
    env.update(HERMES_HOME=str(profile), HERMES_TEST_LIVE_ROOTS=str(root))
    code = (
        'import json, tests.conftest as c\n'
        'print(json.dumps({"home": c.HERMES_HOME_AT_CONFTEST_IMPORT,'
        ' "kanban": str(c._REAL_KANBAN_ROOT), "state": [str(p) for p in c._estate_state_deny_roots()]}))\n'
    )
    out = subprocess.run([sys.executable, '-c', code], cwd=REPO, env=env, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    seen = json.loads(out.stdout.strip().splitlines()[-1])
    assert Path(seen['home']).resolve() != profile.resolve()
    assert Path(seen['kanban']) == root.resolve()
    assert str(root.resolve()) in seen['state']
    assert _digest(sentinel) == before
