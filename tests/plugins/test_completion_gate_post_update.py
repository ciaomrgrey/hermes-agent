"""Post-update gate proof uses persisted same-profile activity."""
import importlib.util
from pathlib import Path
import sqlite3


def test_post_update_requires_hook_and_recent_same_profile_gate(tmp_path):
    from hermes_state import SessionDB
    path = Path(__file__).resolve().parents[2] / 'external/completion-gate-health.py'
    spec = importlib.util.spec_from_file_location('post_update_health', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    home = tmp_path / 'profiles/cody'
    home.mkdir(parents=True)
    db = SessionDB(db_path=home / 'state.db')
    db.create_session('s', source='cli', model='test')
    db.append_message('s', 'assistant', content='persisted final', finish_reason='stop')
    db.close()
    with sqlite3.connect(home / 'state.db') as conn:
        conn.execute('UPDATE messages SET timestamp=3500')
    gate = tmp_path / 'gate.db'
    with sqlite3.connect(gate) as conn:
        conn.execute('CREATE TABLE events(created REAL, profile TEXT)')
        conn.execute("INSERT INTO events VALUES(3500,'gurney')")
        conn.execute("INSERT INTO events VALUES(1000,'cody')")
    check = getattr(module, 'post_update_check', None)
    assert callable(check), 'post-update check missing'
    assert not check([home], gate, now=3600, hooks={'before_turn_end'})['ok']
    with sqlite3.connect(gate) as conn:
        conn.execute("INSERT INTO events VALUES(3500,'cody')")
    assert check([home], gate, now=3600, hooks={'before_turn_end'})['ok']
    assert not check([home], gate, now=3600, hooks=set())['ok']
    assert not check([home], gate, now=6000, hooks={'before_turn_end'})['ok']
