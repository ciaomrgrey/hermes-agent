"""Bounded local proof of native cancellation gap; no provider request or quota."""
import json
import threading
from agent.auxiliary_client import (
    AuxiliaryExplicitCancellation, aux_interrupt_protection, _run_protected_sync_provider_call,
)

started = threading.Event()
release = threading.Event()
provider_finished = threading.Event()
cancel = threading.Event()
owner_finished = threading.Event()
result = {}


def provider_callback(kwargs):
    started.set()
    release.wait(5)
    provider_finished.set()
    return 'local fixture only'


def owner():
    try:
        with aux_interrupt_protection(cancel_check=cancel.is_set):
            _run_protected_sync_provider_call(provider_callback, {})
    except AuxiliaryExplicitCancellation:
        result['native_owner_cancelled'] = True
    finally:
        owner_finished.set()


thread = threading.Thread(target=owner)
thread.start()
try:
    if not started.wait(3):
        raise RuntimeError('local callback did not start')
    cancel.set()
    if not owner_finished.wait(3):
        raise RuntimeError('owner did not cancel')
    result['provider_callback_still_running_after_owner_cancelled'] = not provider_finished.is_set()
finally:
    release.set()
    thread.join(5)
    result['fixture_callback_cleaned_up'] = provider_finished.wait(3)
print(json.dumps(result, indent=2))
if not result.get('provider_callback_still_running_after_owner_cancelled'):
    raise SystemExit('expected gap not reproduced; re-evaluate blocker')
