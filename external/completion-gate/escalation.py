"""Deliver an internal escalation to the target's Slack bridge, at most once. Never contact Lars.

The live-owner inbound lane this replaces returned ``unavailable`` for every escalation ever
raised (``find_canonical_live_owner`` finds no owner on the multiplexed estate gateway), so
none reached a recipient. Delivery is now the native ``hermes send`` path
(``send_message_tool``) to the target profile's bridge channel, using the Slack credentials
of the profile whose gateway owns the estate Slack bot.

At-most-once: a durable receipt is reserved in the target profile home BEFORE the send. A
reservation that never resolved (timeout, crash) reads back ``unverified`` and is never
re-sent; only a definite platform refusal is recorded ``unavailable``.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

# Target profile -> its Mission Control bridge channel.
BRIDGES = {"generalist": "C0BTEFMAAJX", "gurney": "C0BTH5ZQH61"}
# Profile whose gateway owns the estate Slack bot (it multiplexes every bridge channel).
SLACK_SENDER_PROFILE = "generalist"
RECEIPT_DIR = "runtime/completion_gate_escalations"


def send(event, *, deadline=None):
    """Reservation and platform send run in a reapable child under the SAME clock.

    The child runs as the Slack sender profile (its home and its own credentials only),
    never with the calling worker's environment.
    """
    if deadline is None:
        return _send(event)
    from hermes_cli.profiles import get_profile_dir
    from tools.environments.local import served_profile_child_env
    import hermes_constants
    env = served_profile_child_env(target_home=get_profile_dir(SLACK_SENDER_PROFILE), inherit_credentials=True)
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
        # run() kills AND reaps before raising; no late send survives.
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


def delivery_id(channel, text):
    return hashlib.sha256((channel + "\n" + text).encode()).hexdigest()


def receipt_path(home, key):
    return Path(home) / RECEIPT_DIR / (key + ".json")


def read_receipt(home, key):
    try:
        return json.loads(receipt_path(home, key).read_text())
    except (OSError, ValueError):
        return None


def _write_receipt(path, record):
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as handle:
        json.dump(record, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _result(record, key):
    status = record.get("status")
    out = {"status": "unverified" if status == "reserved" else status,
           "delivery_id": key, "channel": record.get("channel")}
    if record.get("message_id"):
        out["message_id"] = record["message_id"]
    return out


def _post(channel, text):
    """Exactly ``hermes send -t slack:<channel>``: profile env, gateway config, send_message_tool."""
    from hermes_cli.send_cmd import _load_hermes_env
    from tools.send_message_tool import send_message_tool
    _load_hermes_env()
    return json.loads(send_message_tool({"action": "send", "target": "slack:" + channel, "message": text}))


def _send(event, deadline=None):
    from hermes_cli.profiles import get_profile_dir
    target = event["target"]
    if target not in BRIDGES:
        raise ValueError("invalid_escalation_target")
    channel = BRIDGES[target]
    text = message(event)
    key = delivery_id(channel, text)
    home = get_profile_dir(target)
    path = receipt_path(home, key)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Serializes concurrent senders; the child watchdog bounds the wait (exit releases it).
    with (path.parent / ".lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = read_receipt(home, key)
        if record is not None:
            if record.get("message") != text:
                raise RuntimeError("escalation_receipt_mismatch")
            return _result(record, key)
        record = {"status": "reserved", "channel": channel, "target": target, "message": text,
                  "event_id": event.get("event_id"), "reserved_at": time.time()}
        _write_receipt(path, record)
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError()
        result = _post(channel, text)
        result = result if isinstance(result, dict) else {}
        if (result.get("success") is True and not result.get("skipped") and result.get("message_id")
                and str(result.get("chat_id") or channel) == channel):
            record.update(status="sent", message_id=str(result["message_id"]), sent_at=time.time())
        elif result.get("error") and not result.get("success") and not result.get("partial_success"):
            # A definite refusal (not configured, channel_not_found, auth): nothing was posted.
            record.update(status="unavailable", resolved_at=time.time())
        else:
            record.update(status="unverified", resolved_at=time.time())
        _write_receipt(path, record)
        return _result(record, key)


def child_main():
    """POSIX watchdog also covers the receipt lock and the platform call.

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
        receipt = _send(payload['event'], payload['deadline'])
        if time.monotonic() >= payload['deadline']:
            os._exit(124)
        output.write(json.dumps(receipt))
        output.flush()
        os._exit(0)
    except Exception:
        # Never forward native exception strings, tokens or profile/session contents.
        os._exit(1)


if __name__ == '__main__':
    child_main()
