"""A CLI removal must survive a long-lived pool's next refresh/status writes."""
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from agent.credential_pool import PooledCredential, load_pool
from hermes_cli.auth import read_credential_pool, write_credential_pool


@pytest.mark.parametrize("with_sibling", [False, True])
@pytest.mark.parametrize("shared_profiles", [False, pytest.param(True, marks=pytest.mark.platforms("posix"))])
def test_cli_remove_survives_stale_writer_and_concurrent_add(tmp_path, monkeypatch, with_sibling, shared_profiles):
    root = tmp_path / "estate"
    root.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("HERMES_SHARED_AUTH_DIR", str(tmp_path / "shared"))
    row = dict(id="removed-seat", label="duplicate-label", auth_type="oauth", priority=0,
               source="manual:hermes_pkce", access_token="fixture-dead-access",
               refresh_token="fixture-dead-refresh", last_status="dead", last_status_at=time.time(),
               last_error_reason="invalid_grant", expires_at_ms=(time.time() + 86400) * 1000)
    sibling = dict(row, id="live-seat", access_token="fixture-live-access", last_status=None)
    write_credential_pool("anthropic", [row, sibling] if with_sibling else [row])
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    homes = [root, root]
    if shared_profiles:
        homes = [root / "profiles" / name for name in ("alpha", "beta")]
        for home in homes:
            home.mkdir(parents=True)
            (home / "auth.json").symlink_to(root / "auth.json")
    pools = []
    for home in homes:
        token = set_hermes_home_override(home)
        try:
            pools.append(load_pool("anthropic"))
        finally:
            reset_hermes_home_override(token)
    # A real separate process runs the CLI handler while this pool remains alive.
    code = '''from types import SimpleNamespace
from hermes_cli.auth_commands import auth_remove_command
from agent.credential_pool import load_pool, PooledCredential
auth_remove_command(SimpleNamespace(provider="anthropic", target="removed-seat"))
pool = load_pool("anthropic")
pool.add_entry(PooledCredential(provider="anthropic", id="new-seat", label="new", auth_type="api_key",
    source="manual", priority=0, access_token="fixture-new-key"))
'''
    proc = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[2],
                          env=os.environ.copy(), capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    expected = {"new-seat", "live-seat"} if with_sibling else {"new-seat"}
    assert {e["id"] for e in read_credential_pool("anthropic")} == expected
    # More than one flush matters: a discarded id must not become a 'new' row
    # once the writer replaces its last-persisted generation map.
    for index in (0, 1, 0):
        token = set_hermes_home_override(homes[index])
        try:
            stale = pools[index]
            stale._persist()
            assert {e["id"] for e in read_credential_pool("anthropic")} == expected
            assert "removed-seat" not in {e.id for e in stale.entries()}
        finally:
            reset_hermes_home_override(token)
    pools[0].add_entry(PooledCredential(provider="anthropic", id="fresh-seat", label="fresh", auth_type="api_key",
                                    source="manual", priority=0, access_token="fixture-fresh-key"))
    assert {e["id"] for e in read_credential_pool("anthropic")} == expected | {"fresh-seat"}
