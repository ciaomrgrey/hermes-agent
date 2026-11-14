"""Deliver an internal escalation to the target's Slack bridge, at most once. Never contact Lars.

The live-owner inbound lane this replaces returned ``unavailable`` for every escalation ever
raised (``find_canonical_live_owner`` finds no owner on the multiplexed estate gateway), so
none reached a recipient. Delivery is now the native ``hermes send`` path
(``send_message_tool``) to the target profile's bridge channel, using the Slack credentials
of the profile whose gateway owns the estate Slack bot.

At-most-once: a durable receipt is reserved in the target profile home BEFORE the send. A
reservation that never resolved (timeout, crash) reads back ``unverified`` and is never
re-sent. ``unavailable`` (which re-arms the gate's per-reason dedup) is recorded only for
a POSITIVELY established non-delivery: the native send returned an error before any
platform I/O was dispatched, or Slack itself answered with an API refusal. Every other
outcome (lost response, dropped connection, timeout, unknown shape) is ``unverified``: the
POST may have been accepted, so nothing is re-sent.
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


# Slack chat.postMessage error codes that mean the request was rejected and no message
# was created. Anything else (internal_error, fatal_error, request_timeout,
# service_unavailable, unknown codes, malformed bodies) may have posted.
SLACK_DEFINITE_REFUSALS = frozenset({
    "channel_not_found", "not_in_channel", "is_archived", "msg_too_long", "no_text",
    "invalid_auth", "not_authed", "token_revoked", "token_expired", "account_inactive",
    "missing_scope", "not_allowed_token_type", "restricted_action", "ratelimited",
    "rate_limited", "invalid_arguments", "invalid_blocks", "too_many_attachments"})


# Census entry for a chat.postMessage attempt that was started but never returned a parsed
# body (exception, timeout, cancellation, process exit). It is never a definite refusal.
INCOMPLETE_ATTEMPT = {"incomplete_attempt": True}


def _slack_responses(native, observed):
    """Census of every chat.postMessage attempt the native Slack sender makes.

    Each attempt is entered as INCOMPLETE_ATTEMPT *before* it is awaited and replaced by
    the parsed body only if the call returns, so an exception (or token fallback after a
    refusal followed by a lost response) can never drop an attempt from the evidence.
    Returns an undo callable, or None when the seam cannot be found (then no response is
    observed and no refusal can be proven).
    """
    try:
        from gateway.platform_registry import platform_registry
        sender = platform_registry.get("slack").standalone_sender_fn
        namespace = sender.__globals__
        original = namespace["_slack_json_post"]
    except Exception:
        return None

    async def recording(session, token, method, payload, req_kw):
        if method != "chat.postMessage":
            return await original(session, token, method, payload, req_kw)
        index = len(observed)
        observed.append(INCOMPLETE_ATTEMPT)
        data = await original(session, token, method, payload, req_kw)
        observed[index] = data
        return data

    namespace["_slack_json_post"] = recording
    return lambda: namespace.__setitem__("_slack_json_post", original)


def _post(channel, text):
    """Exactly ``hermes send -t slack:<channel>``: profile env, gateway config, send_message_tool.

    Returns ``(result, dispatched, responses)``. ``dispatched`` is True once platform I/O may
    have started (the native platform dispatcher was entered), or when that cannot be
    observed. ``responses`` are the parsed chat.postMessage bodies Slack returned.
    """
    from hermes_cli.send_cmd import _load_hermes_env
    import tools.send_message_tool as native
    _load_hermes_env()
    request = {"action": "send", "target": "slack:" + channel, "message": text}
    dispatcher = getattr(native, "_send_to_platform", None)
    if dispatcher is None:
        # Cannot observe dispatch: assume it happened (fail towards no re-send).
        return json.loads(native.send_message_tool(request)), True, []
    entered, responses = [], []

    async def observed(*args, **kwargs):
        entered.append(True)
        undo = _slack_responses(native, responses)
        try:
            return await dispatcher(*args, **kwargs)
        finally:
            if undo is not None:
                undo()

    native._send_to_platform = observed
    try:
        raw = native.send_message_tool(request)
    finally:
        native._send_to_platform = dispatcher
    return json.loads(raw), bool(entered), responses


def _definite_refusal(response):
    return (isinstance(response, dict) and response.get("ok") is False
            and response.get("error") in SLACK_DEFINITE_REFUSALS)


def classify(result, dispatched, channel, responses=()):
    """``sent`` | ``unavailable`` (positively not posted) | ``unverified`` (may have posted).

    After dispatch, ``unavailable`` needs Slack's own parsed answer to every
    chat.postMessage attempt to be an explicit ``ok: false`` with a definite-refusal code;
    one INCOMPLETE_ATTEMPT (started, never answered) makes the whole send ``unverified``.
    The native error text alone proves nothing: it is the same for ``{}`` and ``ok: false``.
    """
    result = result if isinstance(result, dict) else {}
    if (result.get("success") is True and not result.get("skipped") and result.get("message_id")
            and str(result.get("chat_id") or channel) == channel):
        return "sent"
    error = result.get("error")
    if not error or result.get("success") or result.get("partial_success"):
        return "unverified"
    if not dispatched:
        # Refused before any platform I/O: target, config, token, relay authorization.
        return "unavailable"
    responses = list(responses or ())
    if responses and all(_definite_refusal(r) for r in responses):
        return "unavailable"
    return "unverified"


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
        result, dispatched, responses = _post(channel, text)
        status = classify(result, dispatched, channel, responses)
        if status == "sent":
            record.update(status="sent", message_id=str(result["message_id"]), sent_at=time.time())
        else:
            record.update(status=status, resolved_at=time.time())
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
