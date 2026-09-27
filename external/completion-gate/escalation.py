"""Use Hermes' native, at-most-once live-owner inbound lane. Never contact Lars."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def send(event, *, deadline=None):
    """Own native discovery/admission in a reapable process under the SAME clock.

    The native mailbox and active-session locks are intentionally blocking APIs.
    Never run them in a callback thread that the dispatcher can only abandon.
    """
    if deadline is None:
        return _send(event)
    from hermes_constants import get_hermes_home
    from tools.environments.local import served_profile_child_env
    import hermes_constants
    env = served_profile_child_env(target_home=get_hermes_home(), inherit_credentials=False)
    native_root = str(Path(hermes_constants.__file__).resolve().parent)
    env['PYTHONPATH'] = os.pathsep.join(filter(None, [native_root, env.get('PYTHONPATH', '')]))
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError()
    try:
        child = subprocess.run([sys.executable, str(Path(__file__).resolve())],
                               input=json.dumps({'event': event, 'deadline': deadline}),
                               env=env, text=True, capture_output=True, timeout=remaining)
    except subprocess.TimeoutExpired:
        # run() kills AND reaps before raising; no late admission thread survives.
        raise TimeoutError() from None
    if child.returncode == 124 or time.monotonic() >= deadline:
        raise TimeoutError()
    if child.returncode:
        raise RuntimeError('escalation_child_failed')
    return json.loads(child.stdout)


def message(event):
    payload = {k: event[k] for k in ("event_id", "profile", "task_id", "reason", "block_count")}
    return ("Completion-gate internal escalation (not a user request).\n" +
            json.dumps(payload, sort_keys=True) +
            "\nRead the corresponding gate.db event. Workers escalate to Hermes; Hermes to Gurney. "
            "Only advice_failed_again warrants Gurney escalating the case directly to Lars. "
            "Otherwise Lars receives aggregate counts, not cases. No deployment permission is granted.")


def _send(event):
    from hermes_cli.profiles import get_profile_dir
    from tools.bot_live_delivery import find_canonical_live_owner, deliver_to_live_owner, read_delivery_result
    target = event["target"]
    if target not in {"generalist", "gurney"}:
        raise ValueError("invalid_escalation_target")
    home = get_profile_dir(target)
    text = message(event)
    key = hashlib.sha256((str(home.resolve()) + text).encode()).hexdigest()
    receipt = read_delivery_result(home, key)
    if receipt is None:
        owner = find_canonical_live_owner(home)
        if owner is None:
            return {"status": "unavailable", "delivery_id": key}
        deliver_to_live_owner(home, owner, text, delivery_id=key,
                              author={"kind": "plugin", "name": "completion-gate"})
        receipt = read_delivery_result(home, key)
    if receipt is None or receipt["message"] != text:
        raise RuntimeError("inbound_receipt_mismatch")
    return {"status": receipt["status"], "delivery_id": key}


def child_main():
    """POSIX watchdog also covers native locks that catch ordinary exceptions.

    SIGALRM exits the dedicated process, not a shared gateway thread. The parent
    watchdog remains authoritative on platforms without setitimer.
    """
    import logging
    import signal
    logging.disable(sys.maxsize)
    output = sys.stdout
    sys.stdout = sys.stderr = open(os.devnull, 'w')
    try:
        payload = json.load(sys.stdin)
        remaining = payload['deadline'] - time.monotonic()
        if remaining <= 0:
            os._exit(124)
        if hasattr(signal, 'setitimer'):
            signal.signal(signal.SIGALRM, lambda *_: os._exit(124))
            signal.setitimer(signal.ITIMER_REAL, remaining)
        receipt = _send(payload['event'])
        if time.monotonic() >= payload['deadline']:
            os._exit(124)
        output.write(json.dumps(receipt))
        output.flush()
        os._exit(0)
    except Exception:
        # Never forward native exception strings or profile/session contents.
        os._exit(1)


if __name__ == '__main__':
    child_main()

