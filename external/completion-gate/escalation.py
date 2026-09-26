"""Use Hermes' native, at-most-once live-owner inbound lane. Never contact Lars."""
import hashlib
import json


def message(event):
    payload = {k: event[k] for k in ("event_id", "profile", "task_id", "reason", "block_count")}
    return ("Completion-gate internal escalation (not a user request).\n" +
            json.dumps(payload, sort_keys=True) +
            "\nRead the corresponding gate.db event. Workers escalate to Hermes; Hermes to Gurney. "
            "Only advice_failed_again warrants Gurney escalating the case directly to Lars. "
            "Otherwise Lars receives aggregate counts, not cases. No deployment permission is granted.")


def send(event):
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
