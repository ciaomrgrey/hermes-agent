"""Offline subprocess seam: real candidate code and native file locking."""
import importlib.util
import os
from pathlib import Path
import socket
import sys
from unittest.mock import patch

source, home, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
def deny(*args, **kwargs):
    raise AssertionError('offline fixture forbids network')
socket.socket.connect = deny
spec = importlib.util.spec_from_file_location('offline_escalation', source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
import tools.bot_live_delivery as delivery
from hermes_cli.active_sessions import _FileLock
owner = {'profile_home': str(home.resolve()), 'session_id': 'offline', 'lease_id': 'offline', 'live_session_id': 'offline'}
real_deliver = delivery.deliver_to_live_owner
def lookup(_):
    if mode == 'owner':
        (home / 'entered-owner').write_text(str(os.getpid()))
        with _FileLock(home / 'runtime/active_sessions.lock'):
            pass
    return owner
def deliver(*args, **kwargs):
    (home / 'entered-mailbox').write_text(str(os.getpid()))
    return real_deliver(*args, **kwargs)
with patch('hermes_cli.profiles.get_profile_dir', return_value=home), patch.object(delivery, 'find_canonical_live_owner', side_effect=lookup), patch.object(delivery, 'deliver_to_live_owner', side_effect=deliver):
    # Fixture readiness precedes callback start; child_main still receives and
    # enforces the original callback deadline, never a replacement clock.
    (home / 'child-ready').write_text(str(os.getpid()))
    module.child_main()
