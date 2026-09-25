"""Append-only, content-free audit records for policy decisions."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any


_LOCK = threading.Lock()
_MAX_RECORD_BYTES = 4096


def append_record(audit_path: str | Path, **fields: Any) -> None:
    target = Path(audit_path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    record = {"timestamp": time.time(), **fields}
    payload = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(payload) > _MAX_RECORD_BYTES:
        raise ValueError("audit record exceeds bounded size")
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    with _LOCK:
        descriptor = os.open(target, flags, 0o600)
        try:
            written = os.write(descriptor, payload)
            if written != len(payload):
                raise OSError("short audit write")
        finally:
            os.close(descriptor)
